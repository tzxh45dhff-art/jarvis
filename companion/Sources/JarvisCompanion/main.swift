import AppKit
import AVFoundation
import Speech
import SwiftUI

/// JARVIS companion: a menu-bar-only app. Hold ⌃⌥ to talk; answers appear
/// under the notch. It renders and listens; the Python daemon does the thinking.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let overlay = OverlayController()
    private let hud = VoiceHUDController()
    private let speech = SpeechCapture()
    private let hotkey = HotkeyMonitor()
    private let daemon = DaemonClient()
    private lazy var runner = TurnRunner(hud: hud, daemon: daemon, overlay: overlay)
    private lazy var suggestions = SuggestionWatcher(hud: hud, runner: runner)
    private let ducker = AudioDucker()
    private var statusItem: NSStatusItem?

    private enum Turn { case idle, listening }
    private var turn: Turn = .idle
    private var answeringConfirm = false
    /// The microphone is open: from a press until its release, cancel or
    /// cap. (`turn` stays `.listening` until the words arrive.)
    private var micOpen = false
    private var listenStart = Date()
    private var maxListen: Timer?
    private var lastAnswer = ""
    /// Longest a single hold may last; a stuck key must never leave the mic open.
    private let maxListenSeconds: TimeInterval = 60

    func applicationDidFinishLaunching(_ notification: Notification) {
        // One instance only: a login item and a manual launch must not both
        // register hotkeys and answer every request twice.
        if let id = Bundle.main.bundleIdentifier {
            let me = ProcessInfo.processInfo.processIdentifier
            let others = NSRunningApplication.runningApplications(withBundleIdentifier: id)
                .filter { $0.processIdentifier != me }
            if !others.isEmpty { NSApp.terminate(nil); return }
        }
        NSApp.setActivationPolicy(.accessory)   // no Dock icon, no main window
        // `jarvis start` / `restart` from a terminal asks this app to launch
        // the engine: macOS grants permissions (Contacts, Calendars, Automation)
        // to the app that launched a process, so an engine started anywhere
        // else silently loses them.
        DistributedNotificationCenter.default().addObserver(
            forName: Notification.Name("com.example.jarvis.startEngine"),
            object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.logEvent("engine start requested")
                EngineLauncher.start()
            }
        }
        overlay.install()
        installStatusItem()
        wireVoice()
        ducker.trace = { [weak self] m in self?.logEvent(m) }
        suggestions.trace = { [weak self] m in self?.logEvent(m) }
        suggestions.start()
        hud.onNewChat = { [weak self] in self?.newConversation() }
        hud.onProfileLoad = { [weak self] in self?.loadProfile() }
        hud.onProfileForget = { [weak self] id in
            guard let self else { return }
            let msg: [String: Any] = id.map { ["type": "forget_fact", "id": $0] } ?? ["type": "forget_fact", "all": true]
            self.engineCall(msg) { _ in }
        }
        hud.onLearningToggle = { [weak self] on in self?.setEngineSetting("learning", on ? "on" : "off") }

        hotkey.trace = { [weak self] m in self?.logEvent(m) }
        daemon.trace = { [weak self] m in self?.logEvent(m) }
        runner.trace = { [weak self] m in self?.logEvent(m) }
        hud.trace = { [weak self] m in self?.logEvent(m) }
        hotkey.onPress = { [weak self] in
            self?.logEvent("PRESS")
            self?.startListening()
        }
        hotkey.onRelease = { [weak self] in
            self?.logEvent("RELEASE")
            self?.stopListening()
        }
        hotkey.onCancel = { [weak self] in
            self?.logEvent("chord used as a shortcut — listening cancelled")
            self?.abortListening()
        }
        hotkey.isListening = { [weak self] in self?.micOpen ?? false }
        hotkey.onModeChange = { [weak self] holdWorks in
            guard let self else { return }
            self.hud.showNotice(holdWorks ? "Hold ⌃⌥ to talk" : "Press ⌃⌥Space to talk",
                                seconds: 3, icon: "mic.fill")
            self.refreshMenu()
        }
        hotkey.start()
        speech.warmUp()

        // Say plainly what is available. Silence was the original problem: the
        // tap failed, nothing was reported, and the hotkey did nothing.
        let engineUp = FileManager.default.fileExists(atPath: daemon.socketPath)
        var msg: String
        if hotkey.holdToTalkActive {
            msg = "Ready — hold ⌃⌥ to talk"
        } else {
            msg = "Press ⌃⌥Space to talk · grant Accessibility for hold-to-talk"
            HotkeyMonitor.promptForAccessibility()
        }
        if !engineUp { msg += " · JARVIS wakes on your first request" }
        hud.showNotice(msg, seconds: 6, icon: "mic.fill")
        if Settings.shared.speakAnswers { runner.speaker.prewarm() }
        writeState()
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.writeState() }
        }
    }

    // MARK: - menu bar

    private func installStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.isVisible = true
        item.behavior = []
        item.menu = buildMenu()
        statusItem = item
        setIcon("cursorarrow.rays")
        refreshMenu()
    }

    /// The menu bar glyph mirrors what JARVIS is doing.
    private func setIcon(_ symbol: String) {
        guard let button = statusItem?.button else { return }
        let cfg = NSImage.SymbolConfiguration(pointSize: 15, weight: .regular)
        if let img = NSImage(systemSymbolName: symbol, accessibilityDescription: "JARVIS")?
            .withSymbolConfiguration(cfg) {
            img.isTemplate = true
            img.size = NSSize(width: 17, height: 17)
            button.image = img
            button.imagePosition = .imageOnly
        } else {
            button.title = "◆"
        }
        button.toolTip = "JARVIS — hold ⌃⌥ to talk"
    }

    private func item(_ title: String, _ action: Selector?, tag: Int, key: String = "") -> NSMenuItem {
        let it = NSMenuItem(title: title, action: action, keyEquivalent: key)
        it.target = self
        it.tag = tag
        return it
    }

    private func buildMenu() -> NSMenu {
        let menu = NSMenu()
        menu.autoenablesItems = false

        let status = item("Checking engine…", nil, tag: 1)
        status.isEnabled = false
        menu.addItem(status)
        let hint = item("…", nil, tag: 5)
        hint.isEnabled = false
        menu.addItem(hint)
        menu.addItem(.separator())

        menu.addItem(item("Show conversation", #selector(showConversation), tag: 10))
        menu.addItem(item("Copy last answer", #selector(copyLast), tag: 11))
        menu.addItem(item("New conversation", #selector(newConversation), tag: 12))
        menu.addItem(.separator())

        menu.addItem(item("Sounds", #selector(toggleSounds), tag: 20))
        menu.addItem(item("Speak answers", #selector(toggleSpeak), tag: 21))
        let voiceItem = item("Voice style", nil, tag: 23)
        let sub = NSMenu()
        sub.autoenablesItems = false
        for (i, (name, _)) in Self.voiceStyles.enumerated() {
            let it = NSMenuItem(title: name, action: #selector(pickVoiceStyle(_:)), keyEquivalent: "")
            it.target = self
            it.tag = 100 + i
            sub.addItem(it)
        }
        sub.addItem(.separator())
        let hear = NSMenuItem(title: "Hear a sample", action: #selector(hearSample), keyEquivalent: "")
        hear.target = self
        sub.addItem(hear)
        voiceItem.submenu = sub
        menu.addItem(voiceItem)
        menu.addItem(item("Open at login", #selector(toggleLogin), tag: 22))
        menu.addItem(item("Larger text", #selector(toggleLargeText), tag: 25))
        menu.addItem(item("Lower other sounds while I talk", #selector(toggleDuck), tag: 26))
        let sugItem = item("Suggestions", nil, tag: 24)
        let sugMenu = NSMenu()
        sugMenu.autoenablesItems = false
        for (i, (title, _)) in Self.suggestionModes.enumerated() {
            let it = NSMenuItem(title: title, action: #selector(pickSuggestionMode(_:)), keyEquivalent: "")
            it.target = self
            it.tag = 200 + i
            sugMenu.addItem(it)
        }
        sugMenu.addItem(.separator())
        let learn = NSMenuItem(title: "Learn about me", action: #selector(toggleLearning), keyEquivalent: "")
        learn.target = self
        learn.tag = 210
        sugMenu.addItem(learn)
        sugItem.submenu = sugMenu
        menu.addItem(sugItem)
        menu.addItem(.separator())

        menu.addItem(item("Start engine", #selector(toggleEngine), tag: 3))
        menu.addItem(item("Restart engine", #selector(restartEngine), tag: 6))
        menu.addItem(item("Grant Accessibility…", #selector(openAccessibility), tag: 4))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit JARVIS",
                                action: #selector(NSApplication.terminate(_:)),
                                keyEquivalent: "q"))
        menu.delegate = self
        return menu
    }

    /// Display name and the helper's style id.
    private static let voiceStyles: [(String, String)] = [
        ("Bubbly", "bubbly"), ("Warm", "warm"), ("Soft", "soft"),
    ]

    @objc private func pickVoiceStyle(_ sender: NSMenuItem) {
        let i = sender.tag - 100
        guard Self.voiceStyles.indices.contains(i) else { return }
        Settings.shared.voiceStyle = Self.voiceStyles[i].1
        refreshMenu()
        hearSample()
    }

    @objc private func hearSample() {
        runner.speaker.say("Hi! I'm ready when you are. Sorry, I couldn't find that one. "
                           + "All done! Which one would you like?")
    }

    private func loginEnabled() -> Bool {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["list", "com.example.jarvis.companion"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run(); p.waitUntilExit() } catch { return false }
        return p.terminationStatus == 0
    }

    /// Reflect real state in the menu rather than guessing.
    func refreshMenu() {
        guard let menu = statusItem?.menu else { return }
        let engineUp = FileManager.default.fileExists(atPath: daemon.socketPath)
        menu.item(withTag: 1)?.title = engineUp ? "Engine: ready" : "Engine: asleep (wakes on request)"
        menu.item(withTag: 3)?.title = engineUp ? "Stop engine" : "Start engine"
        menu.item(withTag: 4)?.isHidden = hotkey.holdToTalkActive
        menu.item(withTag: 5)?.title = hotkey.holdToTalkActive ? "Hold ⌃⌥ to talk"
                                                               : "Press ⌃⌥Space to talk"
        menu.item(withTag: 11)?.isEnabled = !lastAnswer.isEmpty
        menu.item(withTag: 20)?.state = Settings.shared.sounds ? .on : .off
        menu.item(withTag: 21)?.state = Settings.shared.speakAnswers ? .on : .off
        menu.item(withTag: 22)?.state = loginEnabled() ? .on : .off
        menu.item(withTag: 25)?.state = Settings.shared.largeText ? .on : .off
        menu.item(withTag: 26)?.state = Settings.shared.duckAudio ? .on : .off
        if let sub = menu.item(withTag: 23)?.submenu {
            for (i, (_, id)) in Self.voiceStyles.enumerated() {
                sub.item(withTag: 100 + i)?.state = Settings.shared.voiceStyle == id ? .on : .off
            }
        }
        if let sub = menu.item(withTag: 24)?.submenu {
            let mode = engineSettings["suggestions"] ?? "suggest"
            for (i, (_, id)) in Self.suggestionModes.enumerated() {
                sub.item(withTag: 200 + i)?.state = mode == id ? .on : .off
            }
            sub.item(withTag: 210)?.state = engineSettings["learning"] == "off" ? .off : .on
        }
        refreshEngineSettings()
    }

    func menuWillOpen(_ menu: NSMenu) { refreshMenu() }

    @objc private func showConversation() { hud.openChat() }

    @objc private func copyLast() {
        guard !lastAnswer.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(HUDText.plain(lastAnswer), forType: .string)
    }

    /// Restarts the engine as a child of this app (so it keeps this app's
    /// permissions). For a stuck or misbehaving engine.
    @objc private func restartEngine() {
        logEvent("restart engine requested from the menu")
        runner.cancel(interrupt: true, showStopped: false)
        let bin = Settings.shared.jarvisBin
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/bash")
        task.arguments = ["-lc", "\"\(bin)\" stop >/dev/null 2>&1"]
        task.terminationHandler = { _ in
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    EngineLauncher.start()
                    self.hud.showNotice("Restarting JARVIS…", seconds: 3,
                                        icon: "arrow.clockwise.circle.fill")
                }
            }
        }
        try? task.run()
    }

    /// Quitting the app stops whatever it asked the engine to do: nobody is
    /// left to see the result, and the engine must be free for the next start.
    func applicationWillTerminate(_ notification: Notification) {
        ducker.restore()
        if runner.isWorking { daemon.interrupt() }
    }

    static let suggestionModes: [(String, String)] = [
        ("Off", "off"), ("Suggest things", "suggest"), ("Do small things automatically", "auto")]
    private var engineSettings: [String: String] = [:]

    private func refreshEngineSettings() {
        let socket = daemon.socketPath
        DispatchQueue.global(qos: .utility).async {
            let c = DaemonClient()
            c.socketPathOverride = socket
            let r = c.oneShot(["type": "get_settings"], timeout: 2)
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    if let s = r?["settings"] as? [String: String] { self.engineSettings = s }
                }
            }
        }
    }

    private func setEngineSetting(_ key: String, _ value: String) {
        engineSettings[key] = value
        let socket = daemon.socketPath
        DispatchQueue.global(qos: .utility).async {
            let c = DaemonClient()
            c.socketPathOverride = socket
            _ = c.oneShot(["type": "set_setting", "key": key, "value": value], timeout: 2)
        }
    }

    /// A short request to the engine off the main thread; reply on main.
    private func engineCall(_ msg: [String: Any], done: @escaping ([String: Any]?) -> Void) {
        let socket = daemon.socketPath
        DispatchQueue.global(qos: .userInitiated).async {
            let c = DaemonClient()
            c.socketPathOverride = socket
            let r = c.oneShot(msg, timeout: 3)
            DispatchQueue.main.async { MainActor.assumeIsolated { done(r) } }
        }
    }

    private func loadProfile() {
        engineCall(["type": "get_profile"]) { [weak self] r in
            guard let self, let r else { return }
            let facts = (r["facts"] as? [[String: Any]] ?? []).compactMap { d -> ProfileFact? in
                guard let id = d["id"] as? Int, let item = d["item"] as? String else { return nil }
                return ProfileFact(id: id, category: d["category"] as? String ?? "Other", item: item,
                                   polarity: d["polarity"] as? Int ?? 1, detail: d["detail"] as? String ?? "")
            }
            self.hud.profileFacts = facts
            self.hud.profileRoutines = (r["routines"] as? [String: Any])?.keys.sorted() ?? []
            self.hud.learningOn = (r["learning"] as? String) != "off"
        }
    }

    @objc private func pickSuggestionMode(_ sender: NSMenuItem) {
        let i = sender.tag - 200
        guard Self.suggestionModes.indices.contains(i) else { return }
        setEngineSetting("suggestions", Self.suggestionModes[i].1)
    }

    @objc private func toggleLearning() {
        setEngineSetting("learning", engineSettings["learning"] == "off" ? "on" : "off")
    }

    @objc private func newConversation() {
        runner.cancel(interrupt: true, showStopped: false)
        hud.clearConversation()
        lastAnswer = ""
        DispatchQueue.global().async { [daemon] in daemon.reset() }
        hud.showNotice("Started a fresh conversation", seconds: 2.5, icon: "sparkles")
    }

    @objc private func toggleDuck() {
        Settings.shared.duckAudio.toggle()
        if !Settings.shared.duckAudio { ducker.restore() }
    }

    @objc private func toggleLargeText() {
        Settings.shared.largeText.toggle()
        hud.objectWillChange.send()
    }

    @objc private func toggleSounds() {
        Settings.shared.sounds.toggle()
        if Settings.shared.sounds { Cue.play(.done) }
        refreshMenu()
    }

    @objc private func toggleSpeak() {
        Settings.shared.speakAnswers.toggle()
        hud.speakOn = Settings.shared.speakAnswers
        if Settings.shared.speakAnswers { runner.speaker.prewarm() }
        else { runner.speaker.stop() }
        refreshMenu()
    }

    @objc private func toggleLogin() {
        let enable = !loginEnabled()
        let bin = Settings.shared.jarvisBin
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/bash")
        task.arguments = ["-lc", "\"\(bin)\" companion \(enable ? "enable" : "disable") >/dev/null 2>&1"]
        // A login shell plus the CLI takes a second or two: waiting for it
        // here froze the menu bar and the HUD. Report back when it is done.
        task.terminationHandler = { [weak self] t in
            let ok = t.terminationStatus == 0
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self else { return }
                    if ok {
                        self.hud.showNotice(enable ? "JARVIS will open at login"
                                                   : "JARVIS won't open at login",
                                            seconds: 3, icon: "power")
                    } else {
                        self.hud.showNotice("Couldn't change the login setting", seconds: 4)
                    }
                    self.refreshMenu()
                }
            }
        }
        do { try task.run() } catch {
            hud.showNotice("Couldn't change the login setting", seconds: 4)
        }
    }

    @objc private func toggleEngine() {
        let up = FileManager.default.fileExists(atPath: daemon.socketPath)
        let bin = Settings.shared.jarvisBin
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/bash")
        task.arguments = ["-lc", up ? "\"\(bin)\" stop" : "\"\(bin)\" start >/dev/null 2>&1 &"]
        try? task.run()
        hud.showNotice(up ? "Stopping the engine…" : "Starting the engine…", seconds: 3, icon: "power")
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
            MainActor.assumeIsolated { self?.refreshMenu() }
        }
    }

    @objc private func openAccessibility() {
        HotkeyMonitor.promptForAccessibility()
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
    }

    // MARK: - state + log files

    /// Publish live state where the CLI (and I) can read it.
    func writeState() {
        let state: [String: Any] = [
            "pid": ProcessInfo.processInfo.processIdentifier,
            "accessibility": HotkeyMonitor.hasAccessibilityPermission,
            "holdToTalk": hotkey.holdToTalkActive,
            "engine": FileManager.default.fileExists(atPath: daemon.socketPath),
            "microphone": AVCaptureDevice.authorizationStatus(for: .audio).rawValue,
            "speech": SFSpeechRecognizer.authorizationStatus().rawValue,
            "hotkey": hotkey.holdToTalkActive ? "hold ⌃⌥" : "press ⌃⌥Space",
            "updated": Date().timeIntervalSince1970,
        ]
        let dir = (NSHomeDirectory() as NSString).appendingPathComponent(".jarvis/run")
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let path = (dir as NSString).appendingPathComponent("companion.json")
        if let d = try? JSONSerialization.data(withJSONObject: state, options: .prettyPrinted) {
            try? d.write(to: URL(fileURLWithPath: path))
        }
    }

    /// Append a timestamped event so hotkey delivery can be observed from
    /// outside the process (`open` detaches stderr).
    func logEvent(_ what: String) {
        let dir = (NSHomeDirectory() as NSString).appendingPathComponent(".jarvis/run")
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let path = (dir as NSString).appendingPathComponent("companion-events.log")
        let line = "\(String(format: "%.3f", Date().timeIntervalSince1970)) \(what)\n"
        guard let d = line.data(using: .utf8) else { return }
        if let h = FileHandle(forWritingAtPath: path) {
            h.seekToEndOfFile(); h.write(d); try? h.close()
        } else {
            try? d.write(to: URL(fileURLWithPath: path))
        }
    }

    // MARK: - push to talk

    private func wireVoice() {
        speech.onLevel = { [weak self] v in self?.hud.level = v }
        speech.onPartial = { [weak self] t in self?.hud.setPartial(t) }
        speech.onProblem = { [weak self] msg in
            guard let self else { return }
            self.logEvent("speech problem: \(msg)")
            self.endListening()
            Cue.play(.problem)
            // Answering an "Allow?" by voice: keep the card (a notice would
            // replace it and decline it when it timed out) and say why there.
            if self.hud.confirmPending {
                self.hud.showWorking()
                self.hud.confirmDetail = msg
                return
            }
            self.hud.showNotice(msg, seconds: 9)
        }
        speech.onFinal = { [weak self] text in self?.handleTranscript(text) }
        speech.trace = { [weak self] m in self?.logEvent("speech: \(m)") }
        speech.onReady = { [weak self] in
            guard let self else { return }
            let ms = Int(Date().timeIntervalSince(self.listenStart) * 1000)
            self.logEvent("ready to talk +\(ms) ms after press")
            self.hud.markListeningReady()
            Cue.play(.start)
        }

        hud.onCancel = { [weak self] in
            guard let self else { return }
            self.logEvent("cancelled by user")
            self.endListening()
            self.speech.cancel()
            self.runner.cancel(interrupt: true)
        }
        hud.onSubmitText = { [weak self] text in
            guard let self else { return }
            if self.runner.isWorking { self.runner.cancel(interrupt: true, showStopped: false) }
            self.runner.run(text, viaVoice: false)
        }
        hud.onSpeakToggle = { [weak self] on in
            if on { self?.runner.speaker.prewarm() } else { self?.runner.speaker.stop() }
            self?.refreshMenu()
        }
        runner.onResult = { [weak self] _, answer in
            self?.lastAnswer = answer
            self?.setIcon("cursorarrow.rays")
        }
    }

    private func startListening() {
        // Everything else goes quiet while the user talks; back on release.
        if Settings.shared.duckAudio { ducker.duck() }
        runner.speaker.stop()
        // Load the voice while the user is still talking, so the reply is
        // spoken the moment it is ready.
        if Settings.shared.speakAnswers { runner.speaker.prewarm() }
        // Answering an "Allow?" prompt by voice: keep the prompt, take a word.
        if hud.phase == .confirm || hud.confirmPending {
            if hud.phase != .confirm { hud.showWorking() }   // puts the card back
            // A capture still finishing is dropped by the new one; leaving
            // `turn` at .listening let a later stray release show a working
            // orb that nothing would ever take down.
            turn = .idle
            logEvent("startListening: answering a confirmation")
            micOpen = true
            answeringConfirm = true
            hud.confirmListening = true
            hud.level = 0
            listenStart = Date()
            speech.start()
            armMaxListen()
            setIcon("mic.fill")
            return
        }
        // A task already under way keeps running while the user talks: what
        // they say decides whether it is replaced, left alone or stopped
        // (see handleTranscript). Cancelling on the key press threw away
        // work whenever someone just re-said what they had already asked.
        if turn == .listening { speech.cancel() }
        turn = .listening
        micOpen = true
        listenStart = Date()
        overlay.stopPointing()
        overlay.clearSteps()
        hud.level = 0
        logEvent("startListening: turn=\(turn) working=\(runner.isWorking) hud=\(hud.phase) chat=\(hud.chatOpen)")
        hud.showListening(ready: false)
        speech.start()
        armMaxListen()
        setIcon("mic.fill")
    }

    /// The chord was a shortcut after all: drop everything, run nothing.
    private func abortListening() {
        let wasConfirm = answeringConfirm
        endListening()
        speech.cancel()
        if wasConfirm { return }
        // A task still running keeps its orb (dismissing here hid it, and
        // silently declined any "Allow?" it was waiting on).
        if runner.isWorking { hud.showWorking() } else { hud.dismiss() }
    }

    private func stopListening() {
        guard turn == .listening || answeringConfirm else {
            logEvent("release ignored: not listening")
            return
        }
        maxListen?.invalidate()
        micOpen = false
        speech.stop()
        ducker.restore()
        if !answeringConfirm { hud.showWorking() }
    }

    private func armMaxListen() {
        maxListen?.invalidate()
        maxListen = Timer.scheduledTimer(withTimeInterval: maxListenSeconds,
                                         repeats: false) { [weak self] _ in
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self, self.turn == .listening || self.answeringConfirm else { return }
                    self.logEvent("listen cap reached")
                    self.stopListening()
                }
            }
        }
    }

    private func endListening() {
        ducker.restore()                // cancel, abort, shortcut: never leave it quiet
        maxListen?.invalidate()
        maxListen = nil
        turn = .idle
        micOpen = false
        answeringConfirm = false
        hud.confirmListening = false
        setIcon("cursorarrow.rays")
    }

    private func handleTranscript(_ raw: String) {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        logEvent("heard: \(text)")
        maxListen?.invalidate()
        // The recogniser can finish while the keys are still held (a long
        // silence, an audio route change). The release that follows is then
        // ignored, so the sound must come back here or it stays turned down.
        micOpen = false
        ducker.restore()

        if answeringConfirm {
            answeringConfirm = false
            hud.confirmListening = false
            setIcon("cursorarrow.rays")
            switch HUDText.confirmIntent(text) {
            case .allow: hud.resolveConfirm(approve: true)
            case .deny:  hud.resolveConfirm(approve: false)
            case .unknown:
                hud.confirmDetail = "Didn't catch that — say “yes” or “no”, or tap a button."
            }
            return
        }

        guard turn == .listening else { return }
        turn = .idle
        setIcon("cursorarrow.rays")
        guard !text.isEmpty else {
            if runner.isWorking {
                hud.showWorking()                    // nothing said: carry on
            } else if Date().timeIntervalSince(listenStart) > 0.8 {
                // A bare tap of the chord does nothing, silently. A real
                // attempt that produced no words says so.
                hud.showNotice("Didn't catch that — try again", seconds: 3, icon: "mic.slash.fill")
            } else {
                hud.dismiss()
            }
            return
        }
        if runner.isWorking {
            if HUDText.isStopIntent(text) {
                runner.cancel(interrupt: true)
                hud.showNotice("Stopped.", seconds: 2, icon: "checkmark.circle")
                return
            }
            // The "Allow?" card can appear just after the keys went down: a
            // short "yes" / "no" said then answers it - it used to be queued
            // as a new request while the card waited for minutes.
            let answer = HUDText.shortConfirmAnswer(text)
            if hud.confirmPending, answer != .unknown {
                logEvent("answered the confirmation by voice")
                hud.resolveConfirm(approve: answer == .allow)
                return
            }
            if HUDText.similar(text, runner.currentRequest) {
                logEvent("same request while working - ignored")
                hud.showWorking()
                hud.setCaption("Still on it")
                return
            }
            if HUDText.isReplaceIntent(text) {
                // "actually / instead / wait ..." - drop the current job.
                logEvent("replacing the running request")
                runner.cancel(interrupt: true, showStopped: false)
                runner.run(text)
                return
            }
            // Anything else ("after you're done...", "also...") waits its
            // turn: the job in progress is finished first.
            if !runner.enqueue(text) {
                hud.showWorking()
                hud.setCaption("Already have \(TurnRunner.maxPending) things lined up")
            }
            return
        }
        runner.run(text)
    }
}

// MARK: - `--say`: drive a request without a microphone

/// `JarvisCompanion --say "text"` runs one request through the same pipeline
/// the hotkey uses (engine start, HUD, confirmations) and prints what happens.
/// An integration check that needs neither a microphone nor key presses.
@MainActor
final class SayDelegate: NSObject, NSApplicationDelegate {
    let text: String
    let hud = VoiceHUDController()
    let daemon = DaemonClient()
    private var runner: TurnRunner?

    init(text: String) { self.text = text }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        let r = TurnRunner(hud: hud, daemon: daemon, overlay: nil)
        r.trace = { print("trace: \($0)") }
        hud.trace = { print("trace: \($0)") }
        r.onResult = { heard, answer in
            print("HEARD: \(heard)\nANSWER: \(answer)")
        }
        runner = r
        // `JARVIS_SAY_CONFIRM=allow|deny` answers any confirmation prompt, so
        // the whole confirmation path can be exercised unattended.
        if let mode = ProcessInfo.processInfo.environment["JARVIS_SAY_CONFIRM"] {
            Timer.scheduledTimer(withTimeInterval: 0.3, repeats: true) { [weak self] _ in
                DispatchQueue.main.async {
                    MainActor.assumeIsolated {
                        guard let self, self.hud.phase == .confirm else { return }
                        print("CONFIRM: \(self.hud.confirmTitle) | \(self.hud.confirmDetail) | \(self.hud.confirmPreview)")
                        self.hud.resolveConfirm(approve: mode == "allow")
                    }
                }
            }
        }
        let openChat = ProcessInfo.processInfo.environment["JARVIS_SAY_OPENCHAT"] != nil
        // `JARVIS_SAY_FOLLOWUP="..."` is said 2 s in, while the first request
        // is still working - it must be queued and run afterwards.
        if let follow = ProcessInfo.processInfo.environment["JARVIS_SAY_FOLLOWUP"] {
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                MainActor.assumeIsolated {
                    print("FOLLOWUP while working=\(r.isWorking): \(follow)")
                    if r.isWorking { r.enqueue(follow) } else { r.run(follow) }
                }
            }
            Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { _ in
                MainActor.assumeIsolated {
                    if !r.isWorking && r.pending.isEmpty && r.turnID >= 2 {
                        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { exit(0) }
                    }
                }
            }
            r.run(text)
            return
        }
        r.run(text) { [weak self] in
            if openChat { self?.hud.openChat() }
            // Let the HUD show the answer for a moment, then leave.
            DispatchQueue.main.asyncAfter(deadline: .now() + (openChat ? 9 : 2.5)) {
                MainActor.assumeIsolated {
                    print("phase at end: \(self?.hud.phase ?? .hidden)")
                    if self?.hud.phase == .notice { print("NOTICE: \(self?.hud.answer ?? "")") }
                    exit(0)
                }
            }
        }
        // Never hang a test run.
        DispatchQueue.main.asyncAfter(deadline: .now() + 240) {
            print("TIMEOUT"); exit(2)
        }
    }
}

// MARK: - entry point

MainActor.assumeIsolated {
    let args = CommandLine.arguments
    // Every socket sets SO_NOSIGPIPE; this also covers anything that writes
    // to a pipe or socket whose reader has gone, which would otherwise kill
    // the app instead of returning an error.
    signal(SIGPIPE, SIG_IGN)

    // `--selftest`: check the pure logic and exit non-zero on any failure.
    if args.contains("--selftest") {
        let bad = HUDText.selfTest()
        if bad.isEmpty { print("selftest: all passed"); exit(0) }
        print("selftest FAILED:\n  " + bad.joined(separator: "\n  "))
        exit(1)
    }

    // `--voice-check`: start the voice helper and confirm it answers, silently.
    if args.contains("--voice-check") {
        let t0 = Date()
        let up = Speaker.helperIsUp(root: Settings.shared.jarvisRoot)
        print("voice helper up: \(up) after \(String(format: "%.1f", Date().timeIntervalSince(t0)))s")
        exit(up ? 0 : 1)
    }

    // `--render-hud <dir>`: draw every HUD state to PNGs and exit, so the look
    // can be checked without a screen recording or any permission.
    if let i = args.firstIndex(of: "--render-hud") {
        let dir = i + 1 < args.count ? args[i + 1] : "/tmp/hud"
        let n = HUDSnapshot.render(to: dir)
        print("rendered \(n) images to \(dir)")
        exit(n > 0 ? 0 : 1)
    }

    let app = NSApplication.shared

    if args.contains("--duck-test") {
        MainActor.assumeIsolated {
            let d = AudioDucker()
            d.trace = { print($0) }
            let dev = AudioDucker.defaultOutput()
            print("before:", dev.flatMap { AudioDucker.volume($0) } ?? -1)
            d.duck()
            RunLoop.main.run(until: Date().addingTimeInterval(0.6))
            print("while held:", dev.flatMap { AudioDucker.volume($0) } ?? -1)
            d.restore()
            RunLoop.main.run(until: Date().addingTimeInterval(0.6))
            print("after release:", dev.flatMap { AudioDucker.volume($0) } ?? -1)
        }
        exit(0)
    }
    if let i = args.firstIndex(of: "--say"), i + 1 < args.count {
        let d = SayDelegate(text: args[i + 1])
        app.delegate = d
        objc_setAssociatedObject(app, "jarvis.say", d, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        app.run()
        exit(0)
    }

    let delegate = AppDelegate()
    app.delegate = delegate
    // Keep a strong reference — NSApplication holds its delegate weakly.
    objc_setAssociatedObject(app, "jarvis.delegate", delegate,
                             .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
    app.run()
}
