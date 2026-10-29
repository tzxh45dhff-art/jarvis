import AppKit
import ApplicationServices
import SwiftUI

/// One step of a `show_steps` walkthrough, as drawn on screen.
///
/// `frame` is in CoreGraphics global points — origin at the top-left of the
/// main display, y growing down. That is what the daemon sends and what the
/// accessibility API reports, and because the highlight panel covers exactly
/// the main display it is also the panel's own SwiftUI coordinate space, so
/// drawing needs no conversion at all.
struct StepHighlight: Identifiable, Equatable {
    let number: Int
    let instruction: String
    let element: String
    var frame: CGRect
    /// False when the position is ShowUI's visual guess rather than text OCR
    /// read. A guess is still snapped to the control underneath it, but stays
    /// marked — and is drawn dashed — because *which* control it is remains
    /// the guess; a solid outline would present it as certain.
    var exact: Bool

    var id: Int { number }

    /// Parse the daemon's `ui` payload: `{"kind": "steps", "steps": [...]}`.
    static func parse(_ ui: [String: Any]) -> [StepHighlight] {
        guard ui["kind"] as? String == "steps",
              let steps = ui["steps"] as? [[String: Any]] else { return [] }
        return steps.compactMap { s in
            guard let n = number(s["number"]),
                  let r = s["rect"] as? [String: Any],
                  let x = number(r["x"]), let y = number(r["y"]),
                  let w = number(r["width"]), let h = number(r["height"]),
                  w > 0, h > 0 else { return nil }
            return StepHighlight(
                number: Int(n),
                instruction: s["instruction"] as? String ?? "",
                element: s["element"] as? String ?? "",
                frame: CGRect(x: x, y: y, width: w, height: h),
                exact: s["precision"] as? String == "exact")
        }
    }

    private static func number(_ v: Any?) -> Double? {
        if let d = v as? Double { return d }
        if let i = v as? Int { return Double(i) }
        if let n = v as? NSNumber { return n.doubleValue }
        return nil
    }
}

/// Grows a located box to the frame of the control it belongs to.
///
/// OCR boxes hug the *text* — the word "Save", not the Save button — and ShowUI
/// gives a point, not a box. The accessibility API knows the real control
/// frame. The daemon cannot ask it (only this app holds the Accessibility
/// grant), so the snap happens here, just before drawing.
enum ControlFrameSnapper {
    /// Roles worth outlining. Containers — windows, groups, scroll areas, web
    /// areas — are deliberately absent: outlining those highlights half the
    /// screen, which is worse than the plain text box.
    private static let controlRoles: Set<String> = [
        "AXButton", "AXMenuBarItem", "AXMenuItem", "AXMenuButton",
        "AXPopUpButton", "AXCheckBox", "AXRadioButton", "AXTextField",
        "AXSearchField", "AXComboBox", "AXLink", "AXDisclosureTriangle",
        "AXSlider", "AXIncrementor", "AXCell", "AXRow", "AXTab", "AXImage",
    ]

    /// Anything larger than this is a container that happens to carry a
    /// control role (a table row spanning the window, an image backdrop).
    private static let maxControlSize = CGSize(width: 620, height: 160)

    /// Synchronous and potentially slow (menu bar lookups query every running
    /// app) — call it off the main thread.
    static func snap(_ steps: [StepHighlight]) -> [StepHighlight] {
        guard AXIsProcessTrusted() else { return steps }
        let system = AXUIElementCreateSystemWide()
        // An unresponsive app would otherwise stall for the default six
        // seconds per query.
        AXUIElementSetMessagingTimeout(system, 0.25)
        return steps.compactMap { step in
            var s = step
            switch probe(step, system: system) {
            case .control(let frame):
                s.frame = frame
            case .opaque:
                break
            case .nothing:
                // Text boxes were read off the screen by OCR, so the text is
                // really there even if no control owns it. A visual guess is
                // different: ShowUI always answers, even for things that do not
                // exist, and a native app with no control at that point is
                // telling us the guess is wrong. Drawing it would send the user
                // to a random spot.
                if !step.exact { return nil }
            }
            return s
        }
    }

    private enum Probe {
        case control(CGRect)
        /// The app exposes no usable accessibility tree here (Electron, some
        /// Catalyst apps), so nothing can be confirmed or ruled out.
        case opaque
        /// The app does expose its tree, and there is no control at this point.
        case nothing
    }

    /// Roles an app reports when its content is invisible to accessibility —
    /// a bare window, or a web area / group with nothing inside.
    private static let opaqueRoles: Set<String> = ["AXWindow", "AXWebArea", "AXGroup"]

    private static func probe(_ step: StepHighlight, system: AXUIElement) -> Probe {
        let point = CGPoint(x: step.frame.midX, y: step.frame.midY)
        var hit: AXUIElement?
        guard AXUIElementCopyElementAtPosition(system, Float(point.x), Float(point.y),
                                               &hit) == .success,
              let deepest = hit, !isOwnWindow(deepest) else { return .opaque }

        if let frame = controlFrame(from: deepest, for: step) { return .control(frame) }

        let role = string(deepest, kAXRoleAttribute) ?? ""
        if role == "AXMenuBar" || role == "AXMenuBarItem" {
            // Other processes' menu bar icons hide behind the frontmost app's
            // menu bar in a hit test.
            if let item = statusItemFrame(around: step) { return .control(item) }
            return .nothing
        }
        if opaqueRoles.contains(role) && children(of: deepest).isEmpty { return .opaque }
        return .nothing
    }

    /// Menu bar icons — Wi-Fi, Bluetooth, battery, the clock — belong to other
    /// processes, so a system-wide hit test there returns the frontmost app's
    /// menu bar as a whole. Each owner lists its icons under AXExtrasMenuBar.
    private static func statusItemFrame(around step: StepHighlight) -> CGRect? {
        let menuBarBottom = NSStatusBar.system.thickness + 8
        guard step.frame.midY <= menuBarBottom else { return nil }
        let probe = CGPoint(x: step.frame.midX, y: step.frame.midY)

        var best: (frame: CGRect, distance: CGFloat)?
        for app in NSWorkspace.shared.runningApplications {
            let owner = AXUIElementCreateApplication(app.processIdentifier)
            AXUIElementSetMessagingTimeout(owner, 0.15)
            guard let bar = element(owner, "AXExtrasMenuBar") else { continue }
            for item in children(of: bar) {
                guard let f = frame(of: item), f.width < 200 else { continue }
                let fits = step.exact
                    ? f.insetBy(dx: -2, dy: -2).contains(step.frame)
                    : distance(from: probe, to: f) <= 12
                guard fits else { continue }
                let d = distance(from: probe, to: f)
                if d < (best?.distance ?? .infinity) { best = (f, d) }
            }
        }
        return best?.frame
    }

    private static func distance(from p: CGPoint, to r: CGRect) -> CGFloat {
        let dx = max(r.minX - p.x, 0, p.x - r.maxX)
        let dy = max(r.minY - p.y, 0, p.y - r.maxY)
        return (dx * dx + dy * dy).squareRoot()
    }

    private static func controlFrame(from deepest: AXUIElement,
                                     for step: StepHighlight) -> CGRect? {
        var element = deepest
        // The hit is the deepest element — often a text label inside the button.
        // Walk up a few levels to the control that owns it.
        for _ in 0..<6 {
            if isOwnWindow(element) { return nil }
            if let role = string(element, kAXRoleAttribute),
               controlRoles.contains(role),
               let frame = frame(of: element),
               accept(frame, for: step) {
                return frame
            }
            guard let parent = parent(of: element) else { break }
            element = parent
        }
        return nil
    }

    private static func accept(_ frame: CGRect, for step: StepHighlight) -> Bool {
        guard frame.width > 4, frame.height > 4,
              frame.width <= maxControlSize.width,
              frame.height <= maxControlSize.height else { return false }
        if step.exact {
            // The control has to contain the text we found, or it is some
            // other element that merely overlaps it.
            return frame.insetBy(dx: -2, dy: -2).contains(step.frame)
        }
        // A visual guess: accept any small control under the guessed point.
        return frame.contains(CGPoint(x: step.frame.midX, y: step.frame.midY))
    }

    /// The overlay panels ignore the mouse, but a hit test landing on them
    /// would snap the box to our own full-screen window.
    private static func isOwnWindow(_ element: AXUIElement) -> Bool {
        var pid: pid_t = 0
        return AXUIElementGetPid(element, &pid) == .success
            && pid == ProcessInfo.processInfo.processIdentifier
    }

    private static func string(_ e: AXUIElement, _ attr: String) -> String? {
        var v: CFTypeRef?
        guard AXUIElementCopyAttributeValue(e, attr as CFString, &v) == .success
        else { return nil }
        return v as? String
    }

    private static func parent(of e: AXUIElement) -> AXUIElement? {
        element(e, kAXParentAttribute)
    }

    private static func element(_ e: AXUIElement, _ attr: String) -> AXUIElement? {
        var v: CFTypeRef?
        guard AXUIElementCopyAttributeValue(e, attr as CFString, &v) == .success,
              let v, CFGetTypeID(v) == AXUIElementGetTypeID() else { return nil }
        return (v as! AXUIElement)
    }

    private static func children(of e: AXUIElement) -> [AXUIElement] {
        var v: CFTypeRef?
        guard AXUIElementCopyAttributeValue(e, kAXChildrenAttribute as CFString, &v)
                == .success else { return [] }
        return (v as? [AXUIElement]) ?? []
    }

    private static func frame(of e: AXUIElement) -> CGRect? {
        var pv: CFTypeRef?, sv: CFTypeRef?
        guard AXUIElementCopyAttributeValue(e, kAXPositionAttribute as CFString, &pv)
                == .success,
              AXUIElementCopyAttributeValue(e, kAXSizeAttribute as CFString, &sv)
                == .success,
              let pv, let sv,
              CFGetTypeID(pv) == AXValueGetTypeID(),
              CFGetTypeID(sv) == AXValueGetTypeID() else { return nil }
        var origin = CGPoint.zero, size = CGSize.zero
        guard AXValueGetValue(pv as! AXValue, .cgPoint, &origin),
              AXValueGetValue(sv as! AXValue, .cgSize, &size) else { return nil }
        return CGRect(origin: origin, size: size)
    }
}

/// Every step drawn at once: numbered outlines with the instruction beside
/// each. The one the companion is currently at is emphasised, so the order
/// reads at a glance even before the cursor has visited them all.
struct StepHighlightsView: View {
    @ObservedObject var controller: OverlayController

    var body: some View {
        ZStack(alignment: .topLeading) {
            Color.clear
            ForEach(controller.highlights) { step in
                StepOutline(step: step,
                            active: controller.activeStepNumber == step.number,
                            tint: CompanionState.idle.tint)
            }
        }
        .ignoresSafeArea()
    }
}

private struct StepOutline: View {
    let step: StepHighlight
    let active: Bool
    let tint: Color
    @State private var drawn: CGFloat = 0
    @State private var pulse = false

    /// Breathing room so the outline sits around the control, not on its edge.
    private let inset: CGFloat = -4
    private var box: CGRect { step.frame.insetBy(dx: inset, dy: inset) }
    private var shape: RoundedRectangle {
        RoundedRectangle(cornerRadius: min(9, box.height / 2), style: .continuous)
    }

    var body: some View {
        ZStack(alignment: .topLeading) {
            shape
                .fill(tint.opacity(active ? 0.16 : 0.08))
                .frame(width: box.width, height: box.height)
                .offset(x: box.minX, y: box.minY)

            shape
                .trim(from: 0, to: drawn)
                .stroke(tint.opacity(active ? 1 : 0.6),
                        style: StrokeStyle(lineWidth: active ? 2.5 : 1.6,
                                           lineCap: .round,
                                           dash: step.exact ? [] : [5, 4]))
                .frame(width: box.width, height: box.height)
                .offset(x: box.minX, y: box.minY)
                .shadow(color: tint.opacity(active ? 0.7 : 0), radius: pulse ? 9 : 3)

            label
                .fixedSize()
                .offset(x: box.minX - 6, y: labelY)
        }
        .onAppear {
            // Draw the outline in, staggered by step, so the order is visible
            // even to someone who looks away from the cursor.
            withAnimation(.easeOut(duration: 0.45)
                .delay(0.12 * Double(step.number - 1))) { drawn = 1 }
            withAnimation(.easeInOut(duration: 0.9).repeatForever()) { pulse = true }
        }
    }

    private var label: some View {
        HStack(spacing: 6) {
            Text("\(step.number)")
                .font(.system(size: 11, weight: .bold, design: .rounded))
                .foregroundStyle(.black)
                .frame(width: 18, height: 18)
                .background(Circle().fill(tint))
            if !step.instruction.isEmpty {
                Text(step.instruction)
                    .font(.system(size: 12, weight: active ? .semibold : .medium,
                                  design: .rounded))
                    .foregroundStyle(.white.opacity(active ? 1 : 0.8))
                    .lineLimit(2)
                    .frame(maxWidth: 260, alignment: .leading)
            }
        }
        .padding(.leading, 3).padding(.trailing, 9).padding(.vertical, 3)
        .background(Capsule().fill(Color.black.opacity(active ? 0.85 : 0.7)))
        .opacity(drawn)
    }

    /// Above the element, unless that would run off the top of the screen —
    /// menu bar items are the common case — in which case below it, far enough
    /// down to clear the companion, whose tip rests on the element's bottom edge.
    private var labelY: CGFloat {
        let above = box.minY - 28
        return above >= 2 ? above : box.maxY + 20
    }
}
