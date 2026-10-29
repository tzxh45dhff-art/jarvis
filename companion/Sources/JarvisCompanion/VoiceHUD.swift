import AppKit
import SwiftUI

// MARK: - environment

private enum Env {
    static var reduceMotion: Bool { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }
    static var reduceTransparency: Bool {
        NSWorkspace.shared.accessibilityDisplayShouldReduceTransparency
    }
    /// Font size, enlarged when the user turned on Larger text.
    static func text(_ size: CGFloat) -> CGFloat {
        size * (UserDefaults.standard.bool(forKey: "largeText") ? 1.22 : 1.0)
    }
    static var spring: Animation {
        reduceMotion ? .easeOut(duration: 0.12)
                     : .spring(response: 0.38, dampingFraction: 0.82)
    }
}

/// Reads the animation clock, or a frozen time for snapshots.
private struct Clock<Content: View>: View {
    let fixed: Double?
    let content: (Double) -> Content

    init(fixed: Double?, @ViewBuilder content: @escaping (Double) -> Content) {
        self.fixed = fixed
        self.content = content
    }

    var body: some View {
        if let f = fixed {
            content(f)
        } else {
            TimelineView(.animation) { tl in
                content(tl.date.timeIntervalSinceReferenceDate)
            }
        }
    }
}

// MARK: - the orb

/// A glass sphere with a slow iridescent ribbon inside: dark translucent body,
/// a bright rim, a specular highlight, and a wave that follows the voice.
///
/// Pure drawing, driven by `time` and `level`, so the same view renders live
/// and as a still for the snapshot check.
struct SiriOrbView: View {
    enum Mode { case listening, working, calm }

    var time: Double
    var level: CGFloat
    var mode: Mode
    var size: CGFloat
    var label: String = ""

    private static let ribbons: [(Color, Double, Double)] = [
        (Color(red: 1.00, green: 0.55, blue: 0.22), 1.10, 0.0),
        (Color(red: 0.96, green: 0.30, blue: 0.62), 0.85, 1.9),
        (Color(red: 0.30, green: 0.52, blue: 1.00), 1.35, 3.4),
        (Color(red: 0.62, green: 0.40, blue: 1.00), 0.95, 5.0),
        (Color(red: 0.80, green: 0.95, blue: 1.00), 1.60, 2.5),
    ]

    var body: some View {
        ZStack {
            Canvas { ctx, sz in
                let r = min(sz.width, sz.height) / 2
                let c = CGPoint(x: sz.width / 2, y: sz.height / 2)
                let disc = Path(ellipseIn: CGRect(x: c.x - r, y: c.y - r,
                                                  width: 2 * r, height: 2 * r))

                ctx.fill(disc, with: .radialGradient(
                    Gradient(colors: [Color(white: 0.24).opacity(0.96),
                                      Color(white: 0.05).opacity(0.97)]),
                    center: CGPoint(x: c.x - r * 0.30, y: c.y - r * 0.40),
                    startRadius: 0, endRadius: r * 1.5))

                let calm = mode == .calm
                var base: CGFloat = 0.14
                if mode == .listening { base = 0.16 + level * 0.52 }
                if mode == .working { base = 0.20 + 0.06 * CGFloat(sin(time * 2.2)) }
                var pace: Double = 1.0
                if mode == .working { pace = 1.6 }
                if calm { pace = 0.5 }
                if Env.reduceMotion { pace = 0.25 }

                ctx.drawLayer { layer in
                    layer.clip(to: disc)
                    layer.blendMode = .plusLighter
                    for (i, rib) in Self.ribbons.enumerated() {
                        let amp: CGFloat = r * base * (0.75 + 0.12 * CGFloat(i))
                        var p = Path()
                        let steps = 48
                        for s in 0...steps {
                            let u = CGFloat(s) / CGFloat(steps)
                            let x: CGFloat = c.x - r + u * 2 * r
                            let taper = CGFloat(sin(Double(u) * Double.pi))
                            let freq: Double = 3.2 + Double(i) * 0.7
                            let drift: Double = time * rib.1 * pace + rib.2
                            let swell = CGFloat(sin(Double(u) * freq + drift))
                            let y: CGFloat = c.y + r * 0.10 + swell * amp * taper
                            if s == 0 { p.move(to: CGPoint(x: x, y: y)) }
                            else { p.addLine(to: CGPoint(x: x, y: y)) }
                        }
                        layer.drawLayer { soft in
                            soft.addFilter(.blur(radius: r * 0.13))
                            soft.opacity = 0.85
                            soft.stroke(p, with: .color(rib.0),
                                        style: StrokeStyle(lineWidth: r * 0.26, lineCap: .round))
                        }
                    }
                }

                let hi = Path(ellipseIn: CGRect(x: c.x - r * 0.78, y: c.y - r * 0.92,
                                                width: r * 1.15, height: r * 0.72))
                ctx.fill(hi, with: .linearGradient(
                    Gradient(colors: [Color.white.opacity(0.42), Color.white.opacity(0)]),
                    startPoint: CGPoint(x: c.x - r * 0.3, y: c.y - r * 0.9),
                    endPoint: CGPoint(x: c.x - r * 0.1, y: c.y - r * 0.25)))

                let rim = Path(ellipseIn: CGRect(x: c.x - r + 0.75, y: c.y - r + 0.75,
                                                 width: 2 * r - 1.5, height: 2 * r - 1.5))
                ctx.stroke(rim, with: .conicGradient(
                    Gradient(colors: [.white.opacity(0.75), .white.opacity(0.10),
                                      .white.opacity(0.35), .white.opacity(0.10),
                                      .white.opacity(0.75)]),
                    center: c, angle: .degrees(-120)), lineWidth: 1.4)
            }
            .frame(width: size, height: size)
            .shadow(color: .black.opacity(0.40), radius: size * 0.16, y: size * 0.07)

            if !label.isEmpty && size >= 56 {
                Text(label)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.white.opacity(0.92))
                    .shadow(color: .black.opacity(0.6), radius: 2)
                    .offset(y: size * 0.30)
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

private struct LiveOrb: View {
    let mode: SiriOrbView.Mode
    let level: CGFloat
    let size: CGFloat
    let label: String
    let fixedTime: Double?

    var body: some View {
        Clock(fixed: fixedTime) { t in
            SiriOrbView(time: t, level: level, mode: mode, size: size, label: label)
        }
    }
}

// MARK: - model

enum HUDPhase: Equatable {
    case hidden, listening, working, answer, confirm, notice, suggestion
}

/// One step of the request in progress, for the expandable step list.
struct StepItem: Identifiable, Equatable {
    enum State { case running, done, failed }
    let id = UUID()
    let text: String
    var state: State
}

/// A learned fact, as shown on "What JARVIS knows about you".
struct ProfileFact: Identifiable, Equatable {
    let id: Int
    let category: String
    let item: String
    let polarity: Int
    let detail: String
}

struct ChatMessage: Identifiable, Equatable {
    enum Role { case user, assistant, problem }
    let id = UUID()
    let role: Role
    let text: String
}

/// A `NSPanel` that can take the keyboard only while the user is typing.
final class HUDPanel: NSPanel {
    var allowsKey = false
    override var canBecomeKey: Bool { allowsKey }
    override var canBecomeMain: Bool { false }
}

/// Owns the panel that sits just under the notch: a compact orb and capsule
/// for quick answers, and a conversation panel when you click it.
@MainActor
final class VoiceHUDController: ObservableObject {
    // compact HUD
    @Published var phase: HUDPhase = .hidden
    @Published var orbLabel = "listening"
    @Published var caption = ""
    @Published var answer = ""
    @Published var level: CGFloat = 0
    @Published var noticeIcon = "exclamationmark.circle.fill"
    @Published var confirmTitle = ""
    @Published var confirmDetail = ""
    @Published var confirmPreview = ""
    @Published var confirmListening = false
    // conversation panel
    @Published var chatOpen = false
    @Published var chatLarge = false
    @Published var typing = false
    @Published var draft = ""
    @Published var messages: [ChatMessage] = []
    @Published var speakOn = Settings.shared.speakAnswers
    @Published var steps: [StepItem] = []
    @Published var stepsExpanded = false
    @Published var chips: [String] = []
    @Published var showingProfile = false
    @Published var profileFacts: [ProfileFact] = []
    @Published var profileRoutines: [String] = []
    @Published var learningOn = true
    @Published var confirmForgetAll = false
    /// Asks the app to fetch the profile (it then fills profileFacts).
    var onProfileLoad: (() -> Void)?
    /// Forget one fact (id) or everything (nil).
    var onProfileForget: ((Int?) -> Void)?
    var onLearningToggle: ((Bool) -> Void)?
    var onNewChat: (() -> Void)?
    @Published var suggestionText = ""
    @Published var suggestionHasAction = true
    private var suggestionReply: ((String) -> Void)?

    /// Esc, the Deny button, or talking over a task.
    var onCancel: (() -> Void)?
    /// Text typed into the conversation panel.
    var onSubmitText: ((String) -> Void)?
    /// The speaker button.
    var onSpeakToggle: ((Bool) -> Void)?
    /// Frozen clock for snapshots; nil when running live.
    var fixedTime: Double?
    /// Diagnostics sink (the app writes these to its event log).
    var trace: ((String) -> Void)?

    private var decision: ((Bool, Bool) -> Void)?
    private var panel: HUDPanel?
    private var hideWork: DispatchWorkItem?
    private var orderOutWork: DispatchWorkItem?
    private var escMonitor: Any?
    private var localEscMonitor: Any?
    private var resizeObserver: NSObjectProtocol?
    private var clickMonitor: Any?
    private var contentSize = CGSize(width: 120, height: 90)
    private var workStart: Date?
    private var elapsedTimer: Timer?
    private var hovering = false

    static let chatSize = CGSize(width: 340, height: 460)
    static let chatSizeLarge = CGSize(width: 440, height: 620)
    static let shadowPad: CGFloat = 24

    var isBusy: Bool { phase == .working || phase == .confirm }
    /// An "Allow?" question the engine is still waiting on, whether or not
    /// its card is on screen right now.
    var confirmPending: Bool { decision != nil }

    // MARK: compact transitions

    /// `ready` is false until the microphone is actually delivering audio, so
    /// "listening" is never shown before it is true.
    func showListening(ready: Bool = true) {
        cancelTimers()
        // The elapsed clock is left running: talking over a task must not
        // reset its "working · 40s" (the tick only draws while working).
        caption = ""
        orbLabel = ready ? "listening" : ""
        present()
        withAnimation(Env.spring) { phase = .listening }
        updateInteraction()
    }

    func markListeningReady() {
        if phase == .listening { orbLabel = "listening" }
    }

    func setPartial(_ text: String) {
        guard phase == .listening else { return }
        caption = text
    }

    /// A request is being carried out.
    func beginWork(heard: String) {
        cancelTimers()
        declineStaleConfirm()
        steps = []
        stepsExpanded = false
        chips = []
        if !heard.isEmpty {
            append(.init(role: .user, text: heard))
            caption = ""
        }
        present()
        startElapsed()
        withAnimation(Env.spring) { phase = .working }
        updateInteraction()
    }

    func showWorking() {
        cancelTimers()
        // The engine is blocked on an "Allow?" (it can arrive while the user
        // is talking): bring that card back rather than a working orb that
        // nobody can answer - the turn would hang until the 3-minute timeout.
        if confirmPending {
            present()
            withAnimation(Env.spring) { phase = .confirm }
            updateInteraction()
            return
        }
        orbLabel = "working"
        present()
        withAnimation(Env.spring) { phase = .working }
        updateInteraction()
    }

    /// A request said while another is running: show it in the conversation
    /// now and keep the orb on the current job.
    func noteQueued(_ text: String) {
        append(.init(role: .user, text: text))
        if phase != .working { showWorking() }
        setCaption("Got it — I'll do that next")
    }

    func setCaption(_ text: String) {
        guard phase == .working || phase == .listening else { return }
        withAnimation(.easeOut(duration: 0.15)) { caption = text }
    }

    func showAnswer(_ text: String) {
        cancelTimers()
        stopElapsed()
        declineStaleConfirm()
        append(.init(role: .assistant, text: text))
        answer = text
        present()
        withAnimation(Env.spring) { phase = .answer }
        updateInteraction()
        announce(HUDText.plain(text))
    }

    func scheduleDismissAfterAnswer() {
        guard !chatOpen else { return }
        dismiss(after: HUDText.dwell(forAnswer: answer))
    }

    func showNotice(_ text: String, seconds: Double = 5,
                    icon: String = "exclamationmark.circle.fill") {
        cancelTimers()
        stopElapsed()
        noticeIcon = icon
        if chatOpen && icon.hasPrefix("exclamation") {
            append(.init(role: .problem, text: text))
            withAnimation(Env.spring) { phase = .answer }
            return
        }
        answer = text
        present()
        withAnimation(Env.spring) { phase = .notice }
        updateInteraction()
        dismiss(after: seconds)
    }

    /// A proactive offer under the notch: Yes / Not now, gone by itself after
    /// `seconds` (counted as ignored). `reply` gets accepted | dismissed |
    /// ignored | never.
    func showSuggestion(_ text: String, hasAction: Bool, seconds: Double = 12,
                        reply: @escaping (String) -> Void) {
        guard phase == .hidden, !chatOpen else { reply("skipped"); return }
        cancelTimers()
        suggestionText = text
        suggestionHasAction = hasAction
        suggestionReply = reply
        present()
        withAnimation(Env.spring) { phase = .suggestion }
        updateInteraction()
        dismiss(after: seconds)
    }

    func resolveSuggestion(_ outcome: String) {
        let r = suggestionReply
        suggestionReply = nil
        r?(outcome)
        if phase == .suggestion { dismiss() }
    }

    func askConfirm(title: String, detail: String, preview: String,
                    reply: @escaping (_ approve: Bool, _ remember: Bool) -> Void) {
        cancelTimers()
        declineStaleConfirm()
        decision = reply
        confirmTitle = title
        confirmDetail = detail
        confirmPreview = preview
        confirmListening = false
        present()
        withAnimation(Env.spring) { phase = .confirm }
        updateInteraction()
        announce("\(title). \(detail)")
    }

    func resolveConfirm(approve: Bool, remember: Bool = false) {
        guard let d = decision else { return }
        decision = nil
        confirmListening = false
        d(approve, remember)
        orbLabel = "working"
        withAnimation(Env.spring) { phase = .working }
        updateInteraction()
    }

    /// A question whose turn is over (answered, timed out, replaced) must not
    /// come back on screen or be answered later.
    private func declineStaleConfirm() {
        guard let d = decision else { return }
        decision = nil
        confirmListening = false
        d(false, false)
    }

    func dismiss(after seconds: Double = 0) {
        hideWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            MainActor.assumeIsolated { self?.hideNow() }
        }
        hideWork = work
        if seconds <= 0 { work.perform() }
        else { DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: work) }
    }

    private func hideNow() {
        stopElapsed()
        if let d = decision { decision = nil; d(false, false) }
        if let r = suggestionReply { suggestionReply = nil; r("ignored") }
        if chatOpen {
            // The conversation stays up until the user closes it.
            withAnimation(Env.spring) { phase = .answer }
            updateInteraction()
            return
        }
        withAnimation(.easeIn(duration: 0.25)) { phase = .hidden }
        updateInteraction()
        let out = DispatchWorkItem { [weak self] in
            MainActor.assumeIsolated {
                guard let self, self.phase == .hidden, !self.chatOpen else { return }
                self.panel?.orderOut(nil)
            }
        }
        orderOutWork = out
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35, execute: out)
    }

    private func cancelTimers() {
        hideWork?.cancel(); hideWork = nil
        orderOutWork?.cancel(); orderOutWork = nil
    }

    /// Hovering the compact answer holds it on screen; leaving resumes the clock.
    func setHover(_ inside: Bool) {
        hovering = inside
        guard phase == .answer, !chatOpen else { return }
        if inside { hideWork?.cancel() } else { dismiss(after: 2.5) }
    }

    // MARK: elapsed time

    private func startElapsed() {
        workStart = Date()
        orbLabel = "working"
        elapsedTimer?.invalidate()
        elapsedTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self, let t0 = self.workStart, self.phase == .working else { return }
                    self.orbLabel = HUDText.workingLabel(elapsed: Int(Date().timeIntervalSince(t0)))
                }
            }
        }
    }

    private func stopElapsed() {
        elapsedTimer?.invalidate()
        elapsedTimer = nil
        workStart = nil
    }

    // MARK: conversation

    private func append(_ m: ChatMessage) {
        messages.append(m)
        if messages.count > 60 { messages.removeFirst(messages.count - 60) }
    }

    func openChat() {
        cancelTimers()
        guard !chatOpen else { return }
        present()
        withAnimation(Env.spring) { chatOpen = true }
        resizePanel()
        updateInteraction()
    }

    func closeChat() {
        guard chatOpen else { return }
        stopTyping()
        withAnimation(Env.spring) { chatOpen = false }
        if isBusy {
            // Still working: fall back to the compact orb.
            resizePanel()
            updateInteraction()
        } else {
            dismiss()
        }
    }

    func toggleLarge() {
        withAnimation(Env.spring) { chatLarge.toggle() }
        resizePanel()
    }

    func clearConversation() {
        messages.removeAll()
        answer = ""
    }

    func startTyping() {
        typing = true
        panel?.allowsKey = true
        panel?.makeKey()
    }

    func stopTyping() {
        typing = false
        draft = ""
        panel?.allowsKey = false
        panel?.resignKey()
    }

    /// The stop button and Esc: end the running request now.
    func stopRunning() {
        trace?("stop pressed")
        onCancel?()
    }

    // MARK: steps, chips, profile

    func noteStepStarted(_ text: String) {
        if let last = steps.last, last.state == .running, last.text == text { return }
        if let i = steps.indices.last, steps[i].state == .running { steps[i].state = .done }
        steps.append(StepItem(text: text, state: .running))
        if steps.count > 30 { steps.removeFirst(steps.count - 30) }
    }

    func noteStepEnded(ok: Bool) {
        if let i = steps.lastIndex(where: { $0.state == .running }) {
            steps[i].state = ok ? .done : .failed
        }
    }

    func useChip(_ text: String) {
        chips = []
        onSubmitText?(text)
    }

    func openProfile() {
        showingProfile = true
        confirmForgetAll = false
        onProfileLoad?()
    }

    func forgetFact(_ f: ProfileFact) {
        withAnimation(Env.spring) { profileFacts.removeAll { $0.id == f.id } }
        onProfileForget?(f.id)
    }

    func forgetEverything() {
        guard confirmForgetAll else { confirmForgetAll = true; return }
        confirmForgetAll = false
        withAnimation(Env.spring) { profileFacts = [] }
        onProfileForget?(nil)
    }

    func toggleLearning() {
        learningOn.toggle()
        onLearningToggle?(learningOn)
    }

    func submitTyped() {
        let t = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        draft = ""
        typing = false
        panel?.allowsKey = false
        panel?.resignKey()
        onSubmitText?(t)
    }

    func toggleSpeak() {
        speakOn.toggle()
        Settings.shared.speakAnswers = speakOn
        onSpeakToggle?(speakOn)
    }

    // MARK: panel

    private func updateInteraction() {
        let interactive = chatOpen || phase == .answer || phase == .confirm
            || phase == .working || phase == .notice || phase == .suggestion
        panel?.ignoresMouseEvents = !interactive
    }

    private func present() {
        if panel == nil {
            let p = HUDPanel(contentRect: CGRect(x: 0, y: 0, width: 200, height: 160),
                             styleMask: [.borderless, .nonactivatingPanel],
                             backing: .buffered, defer: false)
            p.isOpaque = false
            p.backgroundColor = .clear
            p.hasShadow = false
            p.level = .statusBar
            p.ignoresMouseEvents = true
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary,
                                    .stationary, .ignoresCycle]
            p.hidesOnDeactivate = false
            let host = NSHostingView(rootView: VoiceHUDView(hud: self))
            p.contentView = host
            panel = p
            // The hosting view sizes the window to its content; whenever that
            // changes the width, put the window back in the middle (it would
            // otherwise grow to the right of wherever it started).
            resizeObserver = NotificationCenter.default.addObserver(
                forName: NSWindow.didResizeNotification, object: p, queue: .main
            ) { [weak self] _ in
                MainActor.assumeIsolated { self?.recenter() }
            }
            installMonitors()
        }
        resizePanel()
        panel?.alphaValue = 1
        panel?.orderFrontRegardless()
        verifyVisible()
    }

    /// If the panel was asked to show but the window server did not put it on
    /// screen (a Space change, a display that went away, a stale frame), say
    /// so, and try once more at the pointer's current display.
    private func verifyVisible() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in
            MainActor.assumeIsolated {
                guard let self, let p = self.panel, self.phase != .hidden || self.chatOpen else { return }
                let screens = NSScreen.screens.map { "\(Int($0.frame.minX)),\(Int($0.frame.minY)) \(Int($0.frame.width))x\(Int($0.frame.height))" }
                let onAScreen = NSScreen.screens.contains { $0.frame.intersects(p.frame) }
                let visible = p.isVisible && p.occlusionState.contains(.visible)
                self.trace?("hud: phase=\(self.phase) chat=\(self.chatOpen) visible=\(visible) "
                            + "frame=\(Int(p.frame.minX)),\(Int(p.frame.minY)) \(Int(p.frame.width))x\(Int(p.frame.height)) "
                            + "onScreen=\(onAScreen) screens=\(screens)")
                if !visible || !onAScreen {
                    self.trace?("hud: not visible - re-presenting")
                    self.resizePanel()
                    p.orderOut(nil)
                    p.orderFrontRegardless()
                }
            }
        }
    }

    /// The compact HUD reports how big its content is; the panel hugs it so
    /// the area that can swallow clicks is no bigger than what is drawn.
    func updateContentSize(_ size: CGSize) {
        guard size.width > 1, size.height > 1, size != contentSize else { return }
        contentSize = size
        if !chatOpen { resizePanel() }
    }

    /// Keep the window centred under the notch at its current size.
    private func recenter() {
        guard let panel else { return }
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first(where: { NSMouseInRect(mouse, $0.frame, false) })
            ?? NSScreen.main ?? NSScreen.screens.first
        guard let s = screen else { return }
        var top = s.frame.maxY - s.safeAreaInsets.top
        top = min(top, s.visibleFrame.maxY)
        let f = panel.frame
        let origin = CGPoint(x: (s.frame.midX - f.width / 2).rounded(),
                             y: (top - f.height + Self.shadowPad - 2).rounded())
        if abs(origin.x - f.origin.x) > 0.5 || abs(origin.y - f.origin.y) > 0.5 {
            panel.setFrameOrigin(origin)
        }
    }

    private func resizePanel() {
        guard let panel else { return }
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first(where: { NSMouseInRect(mouse, $0.frame, false) })
            ?? NSScreen.main ?? NSScreen.screens.first
        guard let s = screen else { return }
        var top = s.frame.maxY
        top -= s.safeAreaInsets.top
        top = min(top, s.visibleFrame.maxY)

        let pad = Self.shadowPad
        var body = contentSize
        if chatOpen {
            body = chatLarge ? Self.chatSizeLarge : Self.chatSize
            let room = top - s.frame.minY - 40
            if body.height > room { body.height = max(280, room) }
        }
        let w = max(body.width, 60) + pad * 2
        let h = max(body.height, 60) + pad * 2
        let origin = CGPoint(x: (s.frame.midX - w / 2).rounded(), y: (top - h + pad - 2).rounded())
        panel.setFrame(CGRect(origin: origin, size: CGSize(width: w, height: h)),
                       display: false)
    }

    /// An Esc the user typed, as opposed to one the engine posted. The engine
    /// presses Escape itself (closing menus and popovers mid-task), and those
    /// presses used to cancel the very task that sent them. Posted events
    /// carry the poster's pid, so only the engine's own are ignored.
    static func isUserEsc(_ ev: NSEvent) -> Bool {
        guard ev.keyCode == 53 else { return false }
        guard let pid = ev.cgEvent?.getIntegerValueField(.eventSourceUnixProcessID),
              pid != 0 else { return true }
        let pidFile = (NSHomeDirectory() as NSString).appendingPathComponent(".jarvis/run/jarvisd.pid")
        let engine = (try? String(contentsOfFile: pidFile, encoding: .utf8))
            .flatMap { Int64($0.trimmingCharacters(in: .whitespacesAndNewlines)) }
        return HUDText.escIsFromUser(sourcePID: pid,
                                     ownPID: Int64(ProcessInfo.processInfo.processIdentifier),
                                     enginePID: engine)
    }

    private func installMonitors() {
        localEscMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] ev in
            guard Self.isUserEsc(ev), let self, self.isBusy || self.phase == .listening else { return ev }
            self.onCancel?()
            return nil
        }
        escMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] ev in
            guard Self.isUserEsc(ev) else { return }
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self, self.phase != .hidden || self.chatOpen else { return }
                    if self.isBusy || self.phase == .listening { self.onCancel?() }
                    else if self.chatOpen { self.closeChat() }
                    else { self.dismiss() }
                }
            }
        }
        // A click anywhere else puts the conversation away, like a popover.
        clickMonitor = NSEvent.addGlobalMonitorForEvents(matching: .leftMouseDown) { [weak self] _ in
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self, self.chatOpen, !self.isBusy, !self.typing,
                          let p = self.panel else { return }
                    if !p.frame.contains(NSEvent.mouseLocation) { self.closeChat() }
                }
            }
        }
    }

    /// VoiceOver: say the answer without needing focus.
    private func announce(_ text: String) {
        guard NSWorkspace.shared.isVoiceOverEnabled, !text.isEmpty else { return }
        NSAccessibility.post(element: NSApp as Any, notification: .announcementRequested,
                             userInfo: [.announcement: text,
                                        .priority: NSAccessibilityPriorityLevel.high.rawValue])
    }
}

// MARK: - views

private struct ContentSizeKey: PreferenceKey {
    static var defaultValue: CGSize = .zero
    static func reduce(value: inout CGSize, nextValue: () -> CGSize) { value = nextValue() }
}

struct VoiceHUDView: View {
    @ObservedObject var hud: VoiceHUDController

    var body: some View {
        ZStack(alignment: .top) {
            if hud.chatOpen {
                ChatPanel(hud: hud)
                    .transition(.scale(scale: 0.5, anchor: .top).combined(with: .opacity))
            } else {
                CompactHUD(hud: hud)
                    .fixedSize()
                    .background(GeometryReader { g in
                        Color.clear.preference(key: ContentSizeKey.self, value: g.size)
                    })
                    .onPreferenceChange(ContentSizeKey.self) { size in
                        DispatchQueue.main.async {
                            MainActor.assumeIsolated { hud.updateContentSize(size) }
                        }
                    }
            }
        }
        .padding(VoiceHUDController.shadowPad)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .animation(Env.spring, value: hud.chatOpen)
    }
}

private struct CompactHUD: View {
    @ObservedObject var hud: VoiceHUDController

    private var orbMode: SiriOrbView.Mode {
        switch hud.phase {
        case .listening: return .listening
        case .working, .confirm: return .working
        default: return .calm
        }
    }

    var body: some View {
        VStack(spacing: 10) {
            switch hud.phase {
            case .hidden:
                Color.clear.frame(width: 1, height: 1)
            case .listening, .working:
                LiveOrb(mode: orbMode, level: hud.level, size: 76,
                        label: hud.orbLabel, fixedTime: hud.fixedTime)
                    .transition(.scale(scale: 0.4).combined(with: .opacity))
                    .onTapGesture { if hud.phase == .working { hud.openChat() } }
                if !hud.caption.isEmpty || hud.phase == .working {
                    HStack(spacing: 8) {
                        if !hud.caption.isEmpty {
                            GlassCapsule {
                                Text(hud.caption)
                                    .font(.system(size: Env.text(12.5), weight: .medium))
                                    .foregroundStyle(.white.opacity(0.88))
                                    .lineLimit(2)
                                    .multilineTextAlignment(.center)
                                    .frame(maxWidth: 330)
                            }
                            .onTapGesture { if hud.phase == .working { hud.openChat() } }
                        }
                        if hud.phase == .working {
                            StopButton(size: 30) { hud.stopRunning() }
                        }
                    }
                    .transition(.opacity.combined(with: .move(edge: .top)))
                }
            case .answer, .notice:
                GlassCapsule {
                    HStack(alignment: .top, spacing: 10) {
                        if hud.phase == .notice {
                            Image(systemName: hud.noticeIcon)
                                .foregroundStyle(hud.noticeIcon.hasPrefix("exclamation")
                                                 ? Color.yellow : Color.white.opacity(0.9))
                                .font(.system(size: 18))
                        } else {
                            LiveOrb(mode: .calm, level: 0, size: 26, label: "",
                                    fixedTime: hud.fixedTime)
                        }
                        Text(HUDText.plain(hud.answer))
                            .font(.system(size: Env.text(13.5), weight: .medium))
                            .foregroundStyle(.white)
                            .lineLimit(4)
                            .multilineTextAlignment(.leading)
                            .frame(maxWidth: 360, alignment: .leading)
                    }
                }
                .contentShape(Rectangle())
                .onTapGesture { if hud.phase == .answer { hud.openChat() } }
                .onHover { hud.setHover($0) }
                .help("Click to open the conversation")
                .transition(.scale(scale: 0.6, anchor: .top).combined(with: .opacity))
            case .confirm:
                GlassCapsule { ConfirmCard(hud: hud, compact: true) }
                    .transition(.scale(scale: 0.6, anchor: .top).combined(with: .opacity))
            case .suggestion:
                GlassCapsule { SuggestionCard(hud: hud) }
                    .onHover { hud.setHover($0) }
                    .contextMenu {
                        Button("Don't suggest this again") { hud.resolveSuggestion("never") }
                    }
                    .transition(.scale(scale: 0.6, anchor: .top).combined(with: .opacity))
            }
        }
        .padding(.top, 2)
    }
}

/// Quick replies under an answer: one tap sends the phrase.
private struct ChipRow: View {
    let chips: [String]
    let action: (String) -> Void

    var body: some View {
        HStack(spacing: 6) {
            ForEach(chips, id: \.self) { c in
                Button { action(c) } label: {
                    Text(c)
                        .font(.system(size: Env.text(12), weight: .medium))
                        .foregroundStyle(.white.opacity(0.9))
                        .padding(.horizontal, 11).padding(.vertical, 6)
                        .background(Capsule().fill(Color.white.opacity(0.11)))
                        .overlay(Capsule().strokeBorder(Color.white.opacity(0.12), lineWidth: 1))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Reply: \(c)")
            }
            Spacer(minLength: 0)
        }
    }
}

/// "What JARVIS knows about you": every learned fact, each removable, plus
/// the learning switch and a two-tap "forget everything".
private struct ProfileView: View {
    @ObservedObject var hud: VoiceHUDController

    private var grouped: [(String, [ProfileFact])] {
        var order: [String] = []
        var map: [String: [ProfileFact]] = [:]
        for f in hud.profileFacts {
            if map[f.category] == nil { order.append(f.category) }
            map[f.category, default: []].append(f)
        }
        return order.map { ($0, map[$0] ?? []) }
    }

    var body: some View {
        if hud.fixedTime != nil {
            content                      // snapshots cannot draw a ScrollView
            Spacer(minLength: 0)
        } else {
            ScrollView { content }
        }
    }

    private var content: some View {
            VStack(alignment: .leading, spacing: 14) {
                Text("What I know about you")
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(.white)
                Text("Learned from what you say and do, kept on this Mac. Remove anything that's wrong.")
                    .font(.system(size: 12))
                    .foregroundStyle(.white.opacity(0.5))
                    .fixedSize(horizontal: false, vertical: true)
                if hud.profileFacts.isEmpty {
                    Text("Nothing yet. Tell me what you like, or just use me for a while.")
                        .font(.system(size: 13))
                        .foregroundStyle(.white.opacity(0.55))
                        .padding(.top, 10)
                }
                ForEach(grouped, id: \.0) { cat, facts in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(cat.uppercased())
                            .font(.system(size: 10.5, weight: .semibold))
                            .tracking(0.6)
                            .foregroundStyle(.white.opacity(0.4))
                        ForEach(facts) { f in
                            HStack(spacing: 8) {
                                if f.polarity != 0 {
                                    Image(systemName: f.polarity > 0 ? "heart.fill" : "hand.thumbsdown.fill")
                                        .font(.system(size: 10))
                                        .foregroundStyle(f.polarity > 0 ? Color.pink.opacity(0.8)
                                                                        : Color.white.opacity(0.5))
                                }
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(f.item).font(.system(size: 13.5)).foregroundStyle(.white)
                                    if !f.detail.isEmpty {
                                        Text(f.detail).font(.system(size: 11))
                                            .foregroundStyle(.white.opacity(0.45))
                                    }
                                }
                                Spacer(minLength: 4)
                                Button { hud.forgetFact(f) } label: {
                                    Image(systemName: "xmark")
                                        .font(.system(size: 10, weight: .bold))
                                        .foregroundStyle(.white.opacity(0.55))
                                        .frame(width: 22, height: 22)
                                        .background(Circle().fill(Color.white.opacity(0.08)))
                                }
                                .buttonStyle(.plain)
                                .help("Forget this")
                                .accessibilityLabel("Forget \(f.item)")
                            }
                            .padding(.horizontal, 10).padding(.vertical, 7)
                            .background(RoundedRectangle(cornerRadius: 12, style: .continuous)
                                .fill(Color.white.opacity(0.06)))
                        }
                    }
                }
                if !hud.profileRoutines.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("ROUTINES")
                            .font(.system(size: 10.5, weight: .semibold)).tracking(0.6)
                            .foregroundStyle(.white.opacity(0.4))
                        ForEach(hud.profileRoutines, id: \.self) { r in
                            Text("“\(r)”").font(.system(size: 13)).foregroundStyle(.white.opacity(0.85))
                        }
                    }
                }
                HStack {
                    Toggle(isOn: Binding(get: { hud.learningOn }, set: { _ in hud.toggleLearning() })) {
                        Text("Keep learning about me").font(.system(size: 12.5))
                            .foregroundStyle(.white.opacity(0.8))
                    }
                    .toggleStyle(.switch)
                    .controlSize(.small)
                    Spacer()
                }
                .padding(.top, 6)
                if !hud.profileFacts.isEmpty {
                    Button { hud.forgetEverything() } label: {
                        Text(hud.confirmForgetAll ? "Tap again to forget everything" : "Forget everything")
                            .font(.system(size: 12.5, weight: .medium))
                            .foregroundStyle(hud.confirmForgetAll ? Color.red.opacity(0.9) : .white.opacity(0.6))
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// A proactive offer: one line and two buttons, nothing more.
private struct SuggestionCard: View {
    @ObservedObject var hud: VoiceHUDController

    var body: some View {
        HStack(spacing: 10) {
            LiveOrb(mode: .calm, level: 0, size: 24, label: "", fixedTime: hud.fixedTime)
            Text(hud.suggestionText)
                .font(.system(size: Env.text(13.5), weight: .medium))
                .foregroundStyle(.white)
                .lineLimit(3)
                .frame(maxWidth: 300, alignment: .leading)
            if hud.suggestionHasAction {
                Button { hud.resolveSuggestion("accepted") } label: {
                    Text("Yes")
                        .font(.system(size: Env.text(12.5), weight: .semibold))
                        .foregroundStyle(.black)
                        .padding(.horizontal, 12).padding(.vertical, 6)
                        .background(Capsule().fill(Color.white))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Yes, do it")
            }
            Button { hud.resolveSuggestion("dismissed") } label: {
                Text(hud.suggestionHasAction ? "Not now" : "OK")
                    .font(.system(size: Env.text(12.5), weight: .medium))
                    .foregroundStyle(.white.opacity(0.85))
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .background(Capsule().fill(Color.white.opacity(0.14)))
            }
            .buttonStyle(.plain)
            .help("Right-click for \"Don't suggest this again\"")
        }
    }
}

/// "Allow …?" with what is about to happen spelled out, voice-answerable.
private struct ConfirmCard: View {
    @ObservedObject var hud: VoiceHUDController
    let compact: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                LiveOrb(mode: hud.confirmListening ? .listening : .working,
                        level: hud.level, size: 26, label: "", fixedTime: hud.fixedTime)
                Text(hud.confirmTitle)
                    .font(.system(size: Env.text(13.5), weight: .semibold))
                    .foregroundStyle(.white)
                    .lineLimit(2)
                    .frame(maxWidth: 320, alignment: .leading)
            }
            if !hud.confirmDetail.isEmpty {
                Text(hud.confirmDetail)
                    .font(.system(size: Env.text(12)))
                    .foregroundStyle(.white.opacity(0.7))
                    .lineLimit(3)
                    .frame(maxWidth: 340, alignment: .leading)
            }
            if !hud.confirmPreview.isEmpty {
                Text(hud.confirmPreview)
                    .font(.system(size: Env.text(11.5), design: .monospaced))
                    .foregroundStyle(.white.opacity(0.85))
                    .lineLimit(3)
                    .padding(8)
                    .frame(maxWidth: 340, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 8).fill(Color.white.opacity(0.08)))
            }
            HStack(spacing: 8) {
                Text(hud.confirmListening ? "Listening…" : "Or say yes / no")
                    .font(.system(size: Env.text(11)))
                    .foregroundStyle(.white.opacity(0.5))
                Spacer(minLength: 8)
                Button("Deny") { hud.resolveConfirm(approve: false) }
                    .buttonStyle(HUDButtonStyle(prominent: false))
                Button("Allow") { hud.resolveConfirm(approve: true) }
                    .buttonStyle(HUDButtonStyle(prominent: true))
            }
        }
    }
}

// MARK: conversation panel

private struct ChatPanel: View {
    @ObservedObject var hud: VoiceHUDController
    @FocusState private var fieldFocused: Bool

    private var size: CGSize {
        hud.chatLarge ? VoiceHUDController.chatSizeLarge : VoiceHUDController.chatSize
    }

    private var glowMode: SiriOrbView.Mode {
        switch hud.phase {
        case .listening: return .listening
        case .working, .confirm: return .working
        default: return .calm
        }
    }

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: 38, style: .continuous)
        ZStack {
            shape.fill(Env.reduceTransparency ? AnyShapeStyle(Color.black)
                                              : AnyShapeStyle(.ultraThinMaterial))
            shape.fill(LinearGradient(
                colors: [Color.black.opacity(0.95), Color.black.opacity(0.84),
                         Color(white: 0.13).opacity(0.80)],
                startPoint: .top, endPoint: .bottom))
            Clock(fixed: hud.fixedTime) { t in
                ChatGlow(time: t, level: hud.level, mode: glowMode)
            }
            .frame(width: size.width, height: size.height)
            VStack(spacing: 0) {
                header
                conversation
                bottomBar
            }
            .frame(width: size.width, height: size.height)
        }
        .frame(width: size.width, height: size.height)
        .clipShape(shape)
        .overlay(shape.strokeBorder(Color.white.opacity(0.14), lineWidth: 1))
        .shadow(color: .black.opacity(0.45), radius: 22, y: 8)
        .animation(Env.spring, value: hud.chatLarge)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("JARVIS conversation")
    }

    private var header: some View {
        HStack(spacing: 10) {
            RoundButton(symbol: "xmark", label: "Close") { hud.closeChat() }
            Spacer()
            RoundButton(symbol: hud.showingProfile ? "bubble.left.and.bubble.right.fill" : "person.crop.circle",
                        label: hud.showingProfile ? "Back to the conversation"
                                                  : "What JARVIS knows about you") {
                if hud.showingProfile { hud.showingProfile = false } else { hud.openProfile() }
            }
            Button("") { hud.onNewChat?() }
                .keyboardShortcut("k", modifiers: .command)
                .frame(width: 0, height: 0).opacity(0)
                .accessibilityHidden(true)
            RoundButton(symbol: hud.chatLarge ? "arrow.down.right.and.arrow.up.left"
                                              : "arrow.up.left.and.arrow.down.right",
                        label: hud.chatLarge ? "Make smaller" : "Make larger") {
                hud.toggleLarge()
            }
        }
        .padding(.horizontal, 14)
        .padding(.top, 14)
        .padding(.bottom, 6)
    }

    private var messageStack: some View {
        VStack(alignment: .leading, spacing: 16) {
            if hud.messages.isEmpty && hud.phase != .listening && hud.phase != .working {
                Text("Hold ⌃⌥ and speak, or tap the keyboard to type.")
                    .font(.system(size: Env.text(14)))
                    .foregroundStyle(.white.opacity(0.45))
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(.top, 60)
            }
            ForEach(hud.messages) { m in
                MessageRow(message: m)
            }
            if hud.phase == .answer && !hud.chips.isEmpty {
                ChipRow(chips: hud.chips) { hud.useChip($0) }
                    .transition(.opacity)
            }
            liveRow
            Color.clear.frame(height: 1).id("bottom")
        }
        .padding(.horizontal, 20)
        .padding(.top, 8)
        .padding(.bottom, 16)
    }

    @ViewBuilder
    private var conversation: some View {
        if hud.showingProfile {
            ProfileView(hud: hud)
        } else if hud.fixedTime != nil {
            // Snapshots cannot draw a ScrollView, so show the content directly.
            VStack(spacing: 0) {
                messageStack
                Spacer(minLength: 0)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        } else {
            ScrollViewReader { proxy in
                ScrollView {
                    messageStack
                }
                .onChange(of: hud.messages.count) { _ in
                    withAnimation(.easeOut(duration: 0.25)) { proxy.scrollTo("bottom", anchor: .bottom) }
                }
                .onChange(of: hud.phase) { _ in
                    withAnimation(.easeOut(duration: 0.25)) { proxy.scrollTo("bottom", anchor: .bottom) }
                }
            }
        }
    }

    @ViewBuilder
    private var liveRow: some View {
        switch hud.phase {
        case .listening:
            HStack {
                Spacer(minLength: 40)
                Text(hud.caption.isEmpty ? "…" : hud.caption)
                    .font(.system(size: Env.text(15)))
                    .foregroundStyle(.white.opacity(0.7))
                    .padding(.horizontal, 14).padding(.vertical, 9)
                    .background(RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .fill(Color.white.opacity(0.10)))
            }
        case .working:
            VStack(alignment: .leading, spacing: 4) {
                Clock(fixed: hud.fixedTime) { t in
                    HStack(spacing: 5) {
                        ForEach(0..<3, id: \.self) { i in
                            let phase = (sin(t * 4 - Double(i) * 0.7) + 1) / 2
                            Circle().fill(Color.white.opacity(0.35 + 0.5 * phase))
                                .frame(width: 7, height: 7)
                        }
                    }
                }
                if !hud.caption.isEmpty {
                    Text(hud.caption)
                        .font(.system(size: Env.text(13)))
                        .foregroundStyle(.white.opacity(0.5))
                        .lineLimit(2)
                }
                if hud.steps.count > 1 {
                    Button {
                        withAnimation(Env.spring) { hud.stepsExpanded.toggle() }
                    } label: {
                        HStack(spacing: 4) {
                            Text("\(hud.steps.count) steps")
                            Image(systemName: hud.stepsExpanded ? "chevron.up" : "chevron.down")
                                .font(.system(size: 9, weight: .bold))
                        }
                        .font(.system(size: 11.5, weight: .medium))
                        .foregroundStyle(.white.opacity(0.45))
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(hud.stepsExpanded ? "Hide steps" : "Show steps")
                    if hud.stepsExpanded {
                        VStack(alignment: .leading, spacing: 5) {
                            ForEach(hud.steps) { st in
                                HStack(spacing: 7) {
                                    Image(systemName: st.state == .done ? "checkmark.circle.fill"
                                          : st.state == .failed ? "xmark.circle.fill" : "circle.dotted")
                                        .foregroundStyle(st.state == .failed ? Color.orange.opacity(0.85)
                                                         : Color.white.opacity(st.state == .done ? 0.55 : 0.8))
                                        .font(.system(size: 11))
                                    Text(st.text)
                                        .font(.system(size: Env.text(12)))
                                        .foregroundStyle(.white.opacity(st.state == .running ? 0.85 : 0.5))
                                        .lineLimit(1)
                                }
                            }
                        }
                        .padding(.top, 2)
                        .transition(.opacity)
                    }
                }
            }
        case .confirm:
            ConfirmCard(hud: hud, compact: false)
                .padding(12)
                .background(RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .fill(Color.white.opacity(0.08)))
        default:
            EmptyView()
        }
    }

    private var bottomBar: some View {
        HStack(spacing: 10) {
            if hud.typing {
                RoundButton(symbol: "keyboard.chevron.compact.down", label: "Hide keyboard") {
                    hud.stopTyping()
                }
                TextField("Ask JARVIS", text: $hud.draft)
                    .textFieldStyle(.plain)
                    .font(.system(size: Env.text(14)))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 14).padding(.vertical, 10)
                    .background(Capsule().fill(Color.white.opacity(0.12)))
                    .focused($fieldFocused)
                    .onSubmit { hud.submitTyped() }
                if hud.isBusy && hud.draft.trimmingCharacters(in: .whitespaces).isEmpty {
                    StopButton { hud.stopRunning() }
                } else {
                    RoundButton(symbol: "arrow.up", label: "Send") { hud.submitTyped() }
                }
            } else {
                RoundButton(symbol: hud.speakOn ? "speaker.wave.2.fill" : "speaker.slash.fill",
                            label: hud.speakOn ? "Turn spoken answers off"
                                               : "Turn spoken answers on") {
                    hud.toggleSpeak()
                }
                Spacer()
                RoundButton(symbol: "keyboard", label: "Type a request") { hud.startTyping() }
                if hud.isBusy {
                    StopButton { hud.stopRunning() }
                        .transition(.scale.combined(with: .opacity))
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.bottom, 14)
        .padding(.top, 6)
        .onChange(of: hud.typing) { on in
            if on { DispatchQueue.main.async { fieldFocused = true } }
        }
    }
}

private struct MessageRow: View {
    let message: ChatMessage

    private var attributed: AttributedString {
        let prepared = HUDText.prepareMarkdown(message.text)
        let opts = AttributedString.MarkdownParsingOptions(
            interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: prepared, options: opts))
            ?? AttributedString(prepared)
    }

    var body: some View {
        Group {
            switch message.role {
            case .user:
                HStack {
                    Spacer(minLength: 40)
                    Text(message.text)
                        .font(.system(size: Env.text(15)))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 14).padding(.vertical, 9)
                        .background(RoundedRectangle(cornerRadius: 18, style: .continuous)
                            .fill(Color.white.opacity(0.16)))
                }
            case .assistant:
                Text(attributed)
                    .font(.system(size: Env.text(16)))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .lineSpacing(2)
            case .problem:
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.yellow)
                    Text(message.text)
                        .font(.system(size: Env.text(14)))
                        .foregroundStyle(.white.opacity(0.75))
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .contextMenu {
            Button("Copy") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(HUDText.plain(message.text), forType: .string)
            }
        }
    }
}

/// The warm light along the bottom edge: calm when idle, swelling with the
/// voice while listening, breathing while a task runs.
private struct ChatGlow: View {
    let time: Double
    let level: CGFloat
    let mode: SiriOrbView.Mode

    private var strength: Double {
        switch mode {
        case .listening: return 0.60 + Double(level) * 0.40
        case .working: return Env.reduceMotion ? 0.55 : 0.55 + 0.22 * sin(time * 2.4)
        case .calm: return 0.42
        }
    }

    var body: some View {
        let warm = RadialGradient(
            colors: [Color(red: 1.0, green: 0.94, blue: 0.82).opacity(0.95),
                     Color(red: 1.0, green: 0.60, blue: 0.28).opacity(0.60),
                     Color.clear],
            center: .center, startRadius: 0, endRadius: 150)
        let cool = RadialGradient(
            colors: [Color(red: 0.45, green: 0.50, blue: 1.0).opacity(0.55), Color.clear],
            center: .center, startRadius: 0, endRadius: 120)
        let showCool: Bool = mode != .calm
        ZStack(alignment: .bottom) {
            Color.clear
            Ellipse().fill(cool)
                .frame(width: 340, height: 120)
                .offset(x: CGFloat(sin(time * 0.7)) * 40, y: 60)
                .opacity(showCool ? 0.8 : 0.0)
            Ellipse().fill(warm)
                .frame(width: 420, height: 150)
                .offset(y: 62)
        }
        .opacity(strength)
        .allowsHitTesting(false)
    }
}

/// The square "stop" control, as in ChatGPT and Claude: ends the running
/// request at once.
private struct StopButton: View {
    var size: CGFloat = 40
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            RoundedRectangle(cornerRadius: size * 0.09, style: .continuous)
                .fill(Color.black.opacity(0.85))
                .frame(width: size * 0.32, height: size * 0.32)
                .frame(width: size, height: size)
                .background(Circle().fill(Color.white.opacity(0.92)))
                .shadow(color: .black.opacity(0.25), radius: 4, y: 1)
        }
        .buttonStyle(.plain)
        .help("Stop (Esc)")
        .accessibilityLabel("Stop")
        .keyboardShortcut(.cancelAction)
    }
}

private struct RoundButton: View {
    let symbol: String
    let label: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 40, height: 40)
                .background(Circle().fill(Color.white.opacity(0.14)))
                .overlay(Circle().strokeBorder(Color.white.opacity(0.10), lineWidth: 1))
        }
        .buttonStyle(.plain)
        .help(label)
        .accessibilityLabel(label)
    }
}

/// The dark translucent capsule that carries text under the orb.
private struct GlassCapsule<Content: View>: View {
    let content: Content
    init(@ViewBuilder content: () -> Content) { self.content = content() }

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: 22, style: .continuous)
        content
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .background(
                shape.fill(Env.reduceTransparency ? AnyShapeStyle(Color.black)
                                                  : AnyShapeStyle(.ultraThinMaterial))
                    .overlay(shape.fill(Color.black.opacity(0.55)))
                    .overlay(shape.strokeBorder(
                        LinearGradient(colors: [.white.opacity(0.35), .white.opacity(0.06)],
                                       startPoint: .top, endPoint: .bottom),
                        lineWidth: 1))
            )
            .shadow(color: .black.opacity(0.35), radius: 14, y: 5)
    }
}

private struct HUDButtonStyle: ButtonStyle {
    let prominent: Bool
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: Env.text(12.5), weight: .semibold))
            .foregroundStyle(prominent ? Color.black : Color.white)
            .padding(.horizontal, 14).padding(.vertical, 5)
            .background(Capsule().fill(prominent ? Color.white : Color.white.opacity(0.16)))
            .opacity(configuration.isPressed ? 0.7 : 1)
    }
}

// MARK: - snapshots (check the look without a screen recording)

@MainActor
enum HUDSnapshot {
    /// Renders every state over a light and a dark backdrop to `dir`.
    static func render(to dir: String) -> Int {
        _ = NSApplication.shared
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        var written = 0

        func convo(_ h: VoiceHUDController) {
            h.messages = [
                .init(role: .user, text: "Hello how are you"),
                .init(role: .assistant, text: "I'm fine, thanks."),
                .init(role: .user, text: "Open LeetCode and solve the next problem"),
                .init(role: .assistant, text: "Done. **Longest Common Prefix** is Accepted — 126 of 126 tests passed, in Java."),
            ]
        }

        let states: [(String, CGSize, (VoiceHUDController) -> Void)] = [
            ("1-listening", CGSize(width: 460, height: 220), { h in
                h.phase = .listening; h.orbLabel = "listening"; h.level = 0.7
                h.caption = "open leetcode and solve the next question" }),
            ("2-working", CGSize(width: 460, height: 220), { h in
                h.phase = .working; h.orbLabel = "working · 12s"
                h.caption = "Pressing a button" }),
            ("3-answer", CGSize(width: 460, height: 160), { h in
                h.phase = .answer
                h.answer = "Solved: Longest Common Prefix is Accepted, 126 of 126 tests passed." }),
            ("4-confirm", CGSize(width: 460, height: 260), { h in
                h.phase = .confirm
                h.confirmTitle = "Allow JARVIS to run a command?"
                h.confirmDetail = "runs a shell command that changes files"
                h.confirmPreview = "rm -rf ~/Desktop/old-builds" }),
            ("5-notice", CGSize(width: 460, height: 140), { h in
                h.phase = .notice
                h.answer = "Allow the Microphone for JARVIS Companion in System Settings" }),
            ("6-chat", CGSize(width: 420, height: 540), { h in
                convo(h); h.chatOpen = true; h.phase = .answer }),
            ("7-chat-listening", CGSize(width: 420, height: 540), { h in
                convo(h); h.chatOpen = true; h.phase = .listening; h.level = 0.8
                h.caption = "now play Strawberry Swing" }),
            ("8-chat-working", CGSize(width: 420, height: 540), { h in
                convo(h); h.chatOpen = true; h.phase = .working
                h.caption = "Opening Apple Music" }),
            ("12-chat-steps", CGSize(width: 420, height: 540), { h in
                convo(h); h.chatOpen = true; h.phase = .working
                h.caption = "Putting the document together"
                h.steps = [.init(text: "Searching the web", state: .done),
                           .init(text: "Finding a photo", state: .failed),
                           .init(text: "Finding a photo", state: .done),
                           .init(text: "Writing a document", state: .running)]
                h.stepsExpanded = true }),
            ("13-chat-chips", CGSize(width: 420, height: 540), { h in
                convo(h); h.chatOpen = true; h.phase = .answer
                h.chips = ["Pause", "Next song", "More like this"] }),
            ("14-profile", CGSize(width: 420, height: 540), { h in
                h.chatOpen = true; h.phase = .answer; h.showingProfile = true
                h.profileFacts = [
                    .init(id: 1, category: "Music", item: "Arctic Monkeys", polarity: 1, detail: ""),
                    .init(id: 2, category: "Music", item: "The Weeknd", polarity: 1, detail: "played 6x"),
                    .init(id: 3, category: "Movies & shows", item: "The Mentalist", polarity: 1, detail: "Netflix"),
                    .init(id: 4, category: "Movies & shows", item: "Horror movies", polarity: -1, detail: ""),
                    .init(id: 5, category: "Apps", item: "Spotify for music", polarity: 1, detail: "default music app")]
                h.profileRoutines = ["study mode"] }),
            ("10-suggestion", CGSize(width: 520, height: 380), { h in
                h.phase = .suggestion; h.suggestionText = "Continue The Mentalist?" }),
            ("11-briefing", CGSize(width: 560, height: 380), { h in
                h.phase = .suggestion
                h.suggestionText = "Morning! 2 events today, first Standup at 10:00. Want some Arctic Monkeys?" }),
            ("9-chat-confirm", CGSize(width: 420, height: 540), { h in
                convo(h); h.chatOpen = true; h.phase = .confirm
                h.confirmTitle = "Allow JARVIS to send a message?"
                h.confirmDetail = "sends a message on your behalf"
                h.confirmPreview = "To: Mum — I'll be late, start without me" }),
        ]
        for (name, canvas, setup) in states {
            for dark in [false, true] {
                let hud = VoiceHUDController()
                hud.fixedTime = 3.1
                setup(hud)
                let backdrop: Color = dark ? Color(white: 0.10)
                                           : Color(red: 0.93, green: 0.94, blue: 0.97)
                let view = ZStack(alignment: .top) {
                    backdrop
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(0..<14, id: \.self) { i in
                            Text("Some page text that sits behind the assistant, line \(i + 1)")
                                .font(.system(size: 13))
                                .foregroundStyle(dark ? Color.white.opacity(0.5)
                                                      : Color.black.opacity(0.55))
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(24).padding(.top, 50)
                    Rectangle().fill(.black).frame(width: 190, height: 30)
                    VoiceHUDView(hud: hud).padding(.top, 26)
                }
                .frame(width: canvas.width, height: canvas.height)
                let r = ImageRenderer(content: view)
                r.scale = 2
                if let img = r.nsImage, let tiff = img.tiffRepresentation,
                   let rep = NSBitmapImageRep(data: tiff),
                   let png = rep.representation(using: .png, properties: [:]) {
                    let path = (dir as NSString).appendingPathComponent(
                        "\(name)-\(dark ? "dark" : "light").png")
                    if (try? png.write(to: URL(fileURLWithPath: path))) != nil { written += 1 }
                }
            }
        }
        return written
    }
}
