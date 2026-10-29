import AppKit
import SwiftUI
import Combine

/// Owns the transparent full-screen panel the companion lives in.
///
/// It is a non-activating borderless panel joined to every Space, so it floats
/// over whatever you are doing without ever stealing focus or appearing in the
/// window list. `ignoresMouseEvents` stays on permanently — the companion is
/// something you look at, never something you have to click past.
@MainActor
final class OverlayController: ObservableObject {
    @Published var state: CompanionState = .idle
    @Published var bubbleText: String = ""
    @Published var bubbleSubdued: Bool = false
    @Published var audioLevel: CGFloat = 0
    @Published var orbPosition: CGPoint = .zero        // screen coords, AppKit origin
    @Published var pointTarget: CGPoint? = nil
    @Published var pointLabel: String = ""
    @Published var visible: Bool = true
    /// True only while the companion is being drawn (a tour or a pointing
    /// moment). The trailing cursor of earlier versions is gone.
    @Published var orbActive: Bool = false
    /// Steps drawn by `show_steps`, and the one the companion is currently at.
    @Published var highlights: [StepHighlight] = []
    @Published var activeStepNumber: Int? = nil

    private var panel: NSPanel?
    /// Separate from `panel`: that one follows the companion between screens,
    /// but step outlines belong to the main display, where they were found,
    /// and must not vanish when the pointer wanders onto another monitor.
    private var highlightPanel: NSPanel?
    private var tourTask: Task<Void, Never>?
    private var highlightExpiry: DispatchWorkItem?
    private var mouseTimer: Timer?
    private var followsMouse = true
    private var hideWorkItem: DispatchWorkItem?
    private var pointExpiry: DispatchWorkItem?

    /// How far behind the pointer the orb trails, so it reads as a companion
    /// rather than a cursor decoration.
    private let trailOffset = CGVector(dx: 16, dy: -16)
    private let followStiffness: CGFloat = 0.26

    func install() {
        guard panel == nil else { return }
        let screen = NSScreen.main ?? NSScreen.screens.first!
        let p = NSPanel(contentRect: screen.frame,
                        styleMask: [.borderless, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = false
        p.level = .statusBar
        p.ignoresMouseEvents = true
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary,
                                .stationary, .ignoresCycle]
        p.isMovableByWindowBackground = false
        p.hidesOnDeactivate = false

        let root = OverlayRoot(controller: self)
        let host = NSHostingView(rootView: root)
        host.frame = screen.frame
        p.contentView = host
        // Deliberately not shown: the panel only comes up while JARVIS is
        // actually pointing at something, and goes away again afterwards.
        panel = p
        orbPosition = currentMouse()
    }

    /// Bring the full-screen layer up on the display the pointer is on.
    private func activatePanel() {
        orbPosition = currentMouse()
        repositionPanelIfNeeded()
        panel?.orderFrontRegardless()
    }

    private func deactivatePanel() {
        orbActive = false
        pointTarget = nil
        pointLabel = ""
        panel?.orderOut(nil)
    }

    func currentMouse() -> CGPoint {
        let m = NSEvent.mouseLocation
        return CGPoint(x: m.x + trailOffset.dx, y: m.y + trailOffset.dy)
    }

    private func startFollowing() {
        mouseTimer?.invalidate()
        // 60 Hz spring toward the pointer. A timer rather than a mouse-moved
        // monitor: it keeps easing after the pointer stops, which is what makes
        // the movement look alive instead of snapping.
        mouseTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 60.0,
                                          repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
        RunLoop.main.add(mouseTimer!, forMode: .common)
    }

    private func tick() {
        guard followsMouse, pointTarget == nil, tourTask == nil else { return }
        let target = currentMouse()
        let dx = target.x - orbPosition.x
        let dy = target.y - orbPosition.y
        if abs(dx) < 0.4 && abs(dy) < 0.4 { return }
        orbPosition = CGPoint(x: orbPosition.x + dx * followStiffness,
                              y: orbPosition.y + dy * followStiffness)
        repositionPanelIfNeeded()
    }

    /// Keep the panel on whichever screen the companion is on.
    private func repositionPanelIfNeeded() {
        guard let panel else { return }
        let p = orbPosition
        guard let screen = NSScreen.screens.first(where: { NSMouseInRect(p, $0.frame, false) })
                ?? NSScreen.main else { return }
        if panel.frame != screen.frame {
            panel.setFrame(screen.frame, display: false)
            panel.contentView?.frame = CGRect(origin: .zero, size: screen.frame.size)
        }
    }

    // MARK: - state transitions

    func beginListening() {
        hideWorkItem?.cancel()
        visible = true
        bubbleText = ""
        pointTarget = nil
        clearSteps()
        withAnimation(.easeOut(duration: 0.18)) { state = .listening }
    }

    func beginThinking(_ note: String = "") {
        withAnimation(.easeOut(duration: 0.18)) {
            state = .thinking
            if !note.isEmpty { bubbleText = note; bubbleSubdued = true }
        }
    }

    func showStatus(_ text: String) {
        withAnimation(.easeOut(duration: 0.15)) {
            bubbleText = text
            bubbleSubdued = true
        }
    }

    func showAnswer(_ text: String) {
        withAnimation(.easeOut(duration: 0.18)) {
            state = .speaking
            bubbleText = text
            bubbleSubdued = false
        }
    }

    func finish(clearAfter seconds: Double = 4.0) {
        withAnimation(.easeOut(duration: 0.2)) { state = .idle }
        hideWorkItem?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            withAnimation(.easeOut(duration: 0.4)) {
                self.bubbleText = ""
                self.pointTarget = nil
                self.pointLabel = ""
            }
        }
        hideWorkItem = work
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: work)
    }

    /// Fly the companion to a point on screen and hold it there.
    func point(at screenPoint: CGPoint, label: String = "") {
        activatePanel()
        orbActive = true
        pointLabel = label
        pointTarget = screenPoint
        withAnimation(.easeOut(duration: 0.2)) { state = .pointing(label) }
        animateOrb(to: orbCentre(forTipAt: screenPoint)) { }
        // A pointing moment is a glance, not a fixture.
        let work = DispatchWorkItem { [weak self] in self?.stopPointing() }
        pointExpiry?.cancel()
        pointExpiry = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.2, execute: work)
    }

    func stopPointing() {
        pointExpiry?.cancel()
        pointExpiry = nil
        if case .pointing = state { state = .idle }
        if tourTask == nil { deactivatePanel() }
    }

    // MARK: - step walkthroughs

    /// How long the outlines stay after the companion has visited every step:
    /// long enough to follow along at the user's own pace.
    private let highlightLifetime: TimeInterval = 8
    /// How long the companion rests on the last step before returning to the
    /// pointer.
    private let holdAfterTour: TimeInterval = 3

    /// Draw every step, then fly the companion to each in turn.
    func showSteps(_ steps: [StepHighlight]) {
        guard !steps.isEmpty else { return }
        clearSteps()
        installHighlightPanelIfNeeded()
        highlightPanel?.orderFrontRegardless()
        pointTarget = nil
        activatePanel()
        orbActive = true
        withAnimation(.easeOut(duration: 0.2)) {
            highlights = steps
            state = .pointing("")
        }

        tourTask = Task { @MainActor [weak self] in
            for step in steps {
                guard let self, !Task.isCancelled else { return }
                self.activeStepNumber = step.number
                await self.flyOrb(toTipAt: self.tipTarget(for: step))
                try? await Task.sleep(nanoseconds: Self.dwell(for: step))
            }
            try? await Task.sleep(nanoseconds: UInt64(self?.holdAfterTour ?? 0) * 1_000_000_000)
            guard let self, !Task.isCancelled else { return }
            // Hand the companion back to the pointer; the outlines stay so the
            // user can still see where to go.
            self.tourTask = nil
            self.activeStepNumber = nil
            self.deactivatePanel()
            self.scheduleHighlightExpiry()
        }
    }

    func clearSteps() {
        tourTask?.cancel()
        tourTask = nil
        highlightExpiry?.cancel()
        activeStepNumber = nil
        if !highlights.isEmpty {
            withAnimation(.easeOut(duration: 0.25)) { highlights = [] }
        }
        deactivatePanel()
        highlightPanel?.orderOut(nil)
    }

    private func scheduleHighlightExpiry() {
        highlightExpiry?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            withAnimation(.easeOut(duration: 0.5)) { self.highlights = [] }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
                self.highlightPanel?.orderOut(nil)
            }
        }
        highlightExpiry = work
        DispatchQueue.main.asyncAfter(deadline: .now() + highlightLifetime, execute: work)
    }

    /// Long enough to read the step's instruction while the reply is spoken.
    private static func dwell(for step: StepHighlight) -> UInt64 {
        let words = Double(step.instruction.split(separator: " ").count)
        let seconds = min(max(1.4 + 0.3 * words, 2.2), 4.5)
        return UInt64(seconds * 1_000_000_000)
    }

    /// Where the tip should land: just inside the bottom edge, centred. That
    /// touches the control without the pointer's body covering its label.
    private func tipTarget(for step: StepHighlight) -> CGPoint {
        let f = step.frame
        let cg = CGPoint(x: f.midX, y: f.maxY - min(3, f.height / 4))
        return appKitPoint(fromScreenPoint: cg)
    }

    /// CoreGraphics global points (top-left of the main display, y down) to
    /// AppKit global points (bottom-left, y up). The main display is always
    /// `NSScreen.screens.first`, whose frame starts at the origin.
    private func appKitPoint(fromScreenPoint p: CGPoint) -> CGPoint {
        let mainHeight = NSScreen.screens.first?.frame.maxY ?? 0
        return CGPoint(x: p.x, y: mainHeight - p.y)
    }

    private func orbCentre(forTipAt tip: CGPoint) -> CGPoint {
        CGPoint(x: tip.x - CompanionOrb.tipOffset.dx,
                y: tip.y - CompanionOrb.tipOffset.dy)
    }

    private func flyOrb(toTipAt tip: CGPoint) async {
        await withCheckedContinuation { (done: CheckedContinuation<Void, Never>) in
            animateOrb(to: orbCentre(forTipAt: tip)) { done.resume() }
        }
    }

    private func installHighlightPanelIfNeeded() {
        guard let main = NSScreen.screens.first else { return }
        if let hp = highlightPanel {
            // Displays can be rearranged while the app runs.
            if hp.frame != main.frame { hp.setFrame(main.frame, display: false) }
            return
        }
        let hp = NSPanel(contentRect: main.frame,
                         styleMask: [.borderless, .nonactivatingPanel],
                         backing: .buffered, defer: false)
        hp.isOpaque = false
        hp.backgroundColor = .clear
        hp.hasShadow = false
        hp.level = .statusBar
        hp.ignoresMouseEvents = true
        hp.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary,
                                 .stationary, .ignoresCycle]
        hp.hidesOnDeactivate = false
        let host = NSHostingView(rootView: StepHighlightsView(controller: self))
        host.frame = CGRect(origin: .zero, size: main.frame.size)
        hp.contentView = host
        hp.orderFrontRegardless()
        // Same window level as the companion; order beneath it so the pointer
        // is never hidden behind an outline.
        if let panel { hp.order(.below, relativeTo: panel.windowNumber) }
        highlightPanel = hp
    }

    /// Arc the orb toward a target rather than teleporting, so the eye can
    /// follow where it went.
    private func animateOrb(to target: CGPoint, completion: @escaping () -> Void) {
        let start = orbPosition
        let steps = 34
        let dur = 0.62
        for i in 1...steps {
            let t = Double(i) / Double(steps)
            let eased = t < 0.5 ? 2 * t * t : 1 - pow(-2 * t + 2, 2) / 2
            // lift the path so it arcs instead of sliding in a straight line
            let arc = sin(eased * .pi) * 46
            DispatchQueue.main.asyncAfter(deadline: .now() + dur * t) { [weak self] in
                guard let self else { return }
                self.orbPosition = CGPoint(
                    x: start.x + (target.x - start.x) * eased,
                    y: start.y + (target.y - start.y) * eased + arc)
                self.repositionPanelIfNeeded()
                if i == steps { completion() }
            }
        }
    }
}

/// Root view: positions the orb, bubble and target ring in screen space.
struct OverlayRoot: View {
    @ObservedObject var controller: OverlayController

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .topLeading) {
                Color.clear

                if controller.orbActive, let target = controller.pointTarget,
                   let origin = screenOrigin() {
                    PointingTarget(label: controller.pointLabel,
                                   tint: controller.state.tint)
                        .position(x: target.x - origin.x,
                                  y: geo.size.height - (target.y - origin.y))
                }

                if controller.orbActive, let origin = screenOrigin() {
                    let x = controller.orbPosition.x - origin.x
                    let y = geo.size.height - (controller.orbPosition.y - origin.y)
                    let orb = CompanionOrb.frameSize
                    // Positioned by the orb's own frame, not the stack's centre.
                    // Centring the stack made the orb's position depend on how
                    // wide the bubble text was, so it slid off whatever it had
                    // just flown to the moment a label appeared.
                    HStack(alignment: .top, spacing: 10) {
                        CompanionOrb(state: controller.state,
                                     audioLevel: controller.audioLevel)
                            .frame(width: orb.width, height: orb.height)
                        if !controller.bubbleText.isEmpty {
                            CompanionBubble(text: controller.bubbleText,
                                            tint: controller.state.tint,
                                            subdued: controller.bubbleSubdued)
                                // line the first line of text up with the orb
                                .offset(y: -5)
                        }
                    }
                    .offset(x: x - orb.width / 2, y: y - orb.height / 2)
                    .animation(.spring(response: 0.28, dampingFraction: 0.8),
                               value: controller.bubbleText)
                }
            }
        }
        .ignoresSafeArea()
    }

    private func screenOrigin() -> CGPoint? {
        let p = controller.orbPosition
        let screen = NSScreen.screens.first(where: { NSMouseInRect(p, $0.frame, false) })
            ?? NSScreen.main
        return screen?.frame.origin
    }
}
