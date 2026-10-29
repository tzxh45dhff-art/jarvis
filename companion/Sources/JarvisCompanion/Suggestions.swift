import AppKit
import CoreGraphics

/// Notices moments (an app comes to the front, a streaming tab opens, the app
/// starts, the Mac wakes, a minute passes) and asks the engine whether there
/// is one thing worth offering *right now*.
///
/// Speed is the whole point: an offer that turns up after the moment has
/// passed is worse than none. So the engine answers from rules (no model),
/// the request has a 1.5 s budget, and the answer is dropped if the user has
/// moved on to another app or JARVIS is busy with something they asked for.
@MainActor
final class SuggestionWatcher {
    private let hud: VoiceHUDController
    private let runner: TurnRunner
    var trace: ((String) -> Void)?
    private var tick: Timer?
    private var titlePoll: Timer?
    private var lastBrowserTitle = ""
    private var inFlight = false

    static let budget: TimeInterval = 1.5
    static let browsers: Set<String> = ["Safari", "Google Chrome", "Arc", "Firefox",
                                        "Brave Browser", "Microsoft Edge", "Opera"]
    private static let streamingWords = ["netflix", "prime video", "hotstar", "youtube",
                                         "disney+", "jiocinema", "sonyliv", "zee5"]

    init(hud: VoiceHUDController, runner: TurnRunner) {
        self.hud = hud
        self.runner = runner
    }

    func start() {
        let ws = NSWorkspace.shared.notificationCenter
        ws.addObserver(forName: NSWorkspace.didActivateApplicationNotification,
                       object: nil, queue: .main) { [weak self] note in
            let app = (note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication)?
                .localizedName ?? ""
            MainActor.assumeIsolated { self?.appActivated(app) }
        }
        ws.addObserver(forName: NSWorkspace.didWakeNotification, object: nil,
                       queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.ask(["kind": "woke"], frontApp: nil) }
        }
        // First open: give the engine a moment to come up.
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) { [weak self] in
            MainActor.assumeIsolated { self?.ask(["kind": "opened"], frontApp: nil) }
        }
        tick = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.ask(["kind": "tick"], frontApp: nil) }
        }
        // A browser stays in front while its tab changes; watch its title
        // (cheap: one window-list read) only while a browser is frontmost.
        titlePoll = Timer.scheduledTimer(withTimeInterval: 2.5, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.pollBrowserTitle() }
        }
    }

    private func appActivated(_ app: String) {
        lastBrowserTitle = ""
        guard !app.isEmpty, app != "JARVIS Companion" else { return }
        var ev: [String: Any] = ["kind": "app_activated", "app": app]
        if Self.browsers.contains(app), let t = Self.frontWindowTitle() { ev["title"] = t
            lastBrowserTitle = t }
        ask(ev, frontApp: app)
    }

    private func pollBrowserTitle() {
        guard let app = NSWorkspace.shared.frontmostApplication?.localizedName,
              Self.browsers.contains(app), let title = Self.frontWindowTitle(),
              title != lastBrowserTitle else { return }
        lastBrowserTitle = title
        let low = title.lowercased()
        guard Self.streamingWords.contains(where: { low.contains($0) }) else { return }
        ask(["kind": "app_activated", "app": app, "title": title], frontApp: app)
    }

    /// One request, on a background queue, with a hard time budget.
    private func ask(_ event: [String: Any], frontApp: String?) {
        guard !inFlight, !runner.isWorking, hud.phase == .hidden, !hud.chatOpen else { return }
        inFlight = true
        let asked = Date()
        let socket = runner.daemon.socketPath
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let client = DaemonClient()
            client.socketPathOverride = socket
            let reply = client.oneShot(["type": "suggest", "event": event], timeout: Self.budget)
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.inFlight = false
                    self.present(reply, asked: asked, frontApp: frontApp)
                }
            }
        }
    }

    private func present(_ reply: [String: Any]?, asked: Date, frontApp: String?) {
        guard let r = reply, (r["type"] as? String) == "suggestion",
              let id = r["id"] as? String, let text = r["text"] as? String else { return }
        let late = Date().timeIntervalSince(asked) > Self.budget + 0.5
        let movedOn = frontApp != nil
            && NSWorkspace.shared.frontmostApplication?.localizedName != frontApp
        if late || movedOn || runner.isWorking || hud.phase != .hidden || hud.chatOpen {
            trace?("suggestion dropped (\(late ? "late" : "moment passed")): \(text)")
            feedback(id, "skipped")
            return
        }
        let prompt = r["prompt"] as? String ?? ""
        if (r["auto"] as? Bool) == true, !prompt.isEmpty {
            trace?("suggestion auto-run: \(prompt)")
            feedback(id, "accepted")
            runner.run(prompt, viaVoice: false)
            return
        }
        trace?("suggestion shown: \(text)")
        hud.showSuggestion(text, hasAction: !prompt.isEmpty) { [weak self] outcome in
            guard let self else { return }
            self.trace?("suggestion \(outcome): \(text)")
            self.feedback(id, outcome)
            if outcome == "accepted", !prompt.isEmpty {
                self.runner.run(prompt, viaVoice: false)
            }
        }
    }

    private func feedback(_ id: String, _ outcome: String) {
        let socket = runner.daemon.socketPath
        DispatchQueue.global(qos: .utility).async {
            let client = DaemonClient()
            client.socketPathOverride = socket
            _ = client.oneShot(["type": "suggestion_feedback", "id": id, "outcome": outcome],
                               timeout: 2)
        }
    }

    /// Title of the frontmost app's front window (needs Screen Recording for
    /// other apps' titles; nil when unavailable).
    static func frontWindowTitle() -> String? {
        guard let pid = NSWorkspace.shared.frontmostApplication?.processIdentifier,
              let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements],
                                                    kCGNullWindowID) as? [[String: Any]] else { return nil }
        for w in list where (w[kCGWindowOwnerPID as String] as? Int32) == pid
            && (w[kCGWindowLayer as String] as? Int) == 0 {
            if let t = w[kCGWindowName as String] as? String, !t.isEmpty { return t }
        }
        return nil
    }
}
