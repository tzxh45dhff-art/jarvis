import AppKit
import AVFoundation

/// The few things a person can reasonably want to change, kept in UserDefaults.
/// Defaults are chosen so a fresh install is quiet and private: text answers,
/// subtle sounds, nothing written to disk.
@MainActor
final class Settings {
    static let shared = Settings()
    private let d = UserDefaults.standard

    /// Turn other sounds down while the talk keys are held.
    var duckAudio: Bool {
        get { d.object(forKey: "duckAudio") as? Bool ?? true }
        set { d.set(newValue, forKey: "duckAudio") }
    }

    /// Bigger type in the conversation and cards (accessibility).
    var largeText: Bool {
        get { d.object(forKey: "largeText") as? Bool ?? false }
        set { d.set(newValue, forKey: "largeText") }
    }

    var sounds: Bool {
        get { d.object(forKey: "sounds") as? Bool ?? true }
        set { d.set(newValue, forKey: "sounds") }
    }
    var speakAnswers: Bool {
        get { d.object(forKey: "speakAnswers") as? Bool ?? false }
        set { d.set(newValue, forKey: "speakAnswers") }
    }
    /// "bubbly", "warm" or "soft" - which voice carries the feeling.
    var voiceStyle: String {
        get { d.string(forKey: "voiceStyle") ?? "bubbly" }
        set { d.set(newValue, forKey: "voiceStyle") }
    }
    /// Where the engine's launcher lives. Overridable for other checkouts.
    var jarvisBin: String {
        if let p = ProcessInfo.processInfo.environment["JARVIS_BIN"], !p.isEmpty { return p }
        return "\(NSHomeDirectory())/Downloads/fuck/jarvis/bin/jarvis"
    }
    /// The checkout the launcher belongs to (one level above `bin/`).
    var jarvisRoot: String {
        ((jarvisBin as NSString).deletingLastPathComponent as NSString).deletingLastPathComponent
    }
}

/// Short, quiet audio cues using the system's own sounds.
@MainActor
enum Cue {
    enum Kind: String { case start = "Tink", done = "Pop", problem = "Basso" }

    static func play(_ kind: Kind) {
        guard Settings.shared.sounds else { return }
        guard let s = NSSound(named: NSSound.Name(kind.rawValue)) else { return }
        s.volume = 0.35
        s.play()
    }
}

/// Speaks answers aloud when the user has asked for it.
///
/// Prefers the expressive voice helper (a separate process that picks voice,
/// pace and pauses per sentence and exits when idle). If that cannot be
/// reached it falls back to the system voice, so a reply is never silent.
@MainActor
final class Speaker {
    private let fallback = AVSpeechSynthesizer()
    private let io = DispatchQueue(label: "jarvis.speak")
    /// Bumped by every `stop()`. A line queued before a stop (still waiting
    /// for the helper to start) is dropped instead of being said late - over
    /// the user's next request or the answer.
    private let epoch = Epoch()
    private final class Epoch: @unchecked Sendable {
        private let lock = NSLock()
        private var n = 0
        func bump() { lock.lock(); n += 1; lock.unlock() }
        var value: Int { lock.lock(); defer { lock.unlock() }; return n }
    }
    private static let socket = "\(NSHomeDirectory())/.jarvis/run/tts.sock"
    private nonisolated(unsafe) static var spawnedAt: Date?

    /// Start the helper in the background so the first reply is not slowed by
    /// loading the voice. Safe to call repeatedly.
    func prewarm() {
        let root = Settings.shared.jarvisRoot
        io.async { _ = Speaker.ensureHelper(root: root) }
    }

    func say(_ text: String) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        let style = Settings.shared.voiceStyle
        let root = Settings.shared.jarvisRoot
        let epoch = self.epoch
        let mine = epoch.value
        io.async { [weak self] in
            var ok = false
            if Speaker.ensureHelper(root: root) {
                guard epoch.value == mine else { return }
                let r = Speaker.request(["cmd": "say", "text": String(t.prefix(1500)),
                                         "style": style], timeout: 4)
                ok = (r?["ok"] as? Bool) == true
            }
            if !ok {
                DispatchQueue.main.async {
                    MainActor.assumeIsolated {
                        guard epoch.value == mine else { return }
                        self?.systemSay(t)
                    }
                }
            }
        }
    }

    /// Start (if needed) and confirm the helper answers; for `--voice-check`.
    nonisolated static func helperIsUp(root: String) -> Bool {
        ensureHelper(root: root)
    }

    func stop() {
        epoch.bump()
        fallback.stopSpeaking(at: .immediate)
        io.async { _ = Speaker.request(["cmd": "stop"], timeout: 1) }
    }

    private func systemSay(_ text: String) {
        let t = HUDText.plain(text)
        guard !t.isEmpty else { return }
        fallback.stopSpeaking(at: .immediate)
        let u = AVSpeechUtterance(string: String(t.prefix(1200)))
        u.voice = AVSpeechSynthesisVoice(language: "en-US")
        fallback.speak(u)
    }

    // MARK: helper process

    private nonisolated static func request(_ obj: [String: Any], timeout: TimeInterval) -> [String: Any]? {
        let c = DaemonClient()
        c.socketPathOverride = socket
        return c.oneShot(obj, timeout: timeout)
    }

    private nonisolated static func ensureHelper(root: String) -> Bool {
        if request(["cmd": "ping"], timeout: 1.5) != nil { return true }
        // Do not start a second copy while one is still coming up.
        if let t = spawnedAt, Date().timeIntervalSince(t) < 6 {
            return waitForHelper(seconds: 8)
        }
        let python = "\(root)/.venv/bin/python"
        guard FileManager.default.isExecutableFile(atPath: python) else { return false }
        spawnedAt = Date()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = ["-W", "ignore", "-m", "jarvis.voice.speak_helper"]
        p.currentDirectoryURL = URL(fileURLWithPath: NSHomeDirectory())
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = root + (env["PYTHONPATH"].map { ":" + $0 } ?? "")
        env["PYTHONUNBUFFERED"] = "1"
        p.environment = env
        let log = NSHomeDirectory() + "/.jarvis/run/tts.log"
        FileManager.default.createFile(atPath: log, contents: nil)
        let out = FileHandle(forWritingAtPath: log)
        p.standardOutput = out
        p.standardError = out
        p.standardInput = FileHandle.nullDevice
        do { try p.run() } catch { return false }
        return waitForHelper(seconds: 8)
    }

    private nonisolated static func waitForHelper(seconds: Double) -> Bool {
        let end = Date().addingTimeInterval(seconds)
        while Date() < end {
            if request(["cmd": "ping"], timeout: 1) != nil { return true }
            Thread.sleep(forTimeInterval: 0.25)
        }
        return false
    }
}
