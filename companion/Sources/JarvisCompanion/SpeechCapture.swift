import AVFoundation
import Speech

/// Push-to-talk speech capture, done entirely inside the companion.
///
/// Apple's on-device recogniser streams partial words while the key is held,
/// costs the Python engine no memory at all, and keeps audio on the Mac. The
/// daemon only ever sees the finished text, exactly as if it had been typed.
///
/// Contract: after `start()`, exactly one of `onFinal` / `onProblem` fires —
/// unless `cancel()` is called first, in which case neither does.
@MainActor
final class SpeechCapture {
    var onLevel: ((CGFloat) -> Void)?
    var onPartial: ((String) -> Void)?
    var onFinal: ((String) -> Void)?
    var onProblem: ((String) -> Void)?
    /// Fires once, when the first audio actually arrives: the moment it is
    /// safe to start talking.
    var onReady: (() -> Void)?
    var trace: ((String) -> Void)?

    private enum Phase { case idle, starting, running, finishing }

    private let engine = AVAudioEngine()
    /// Every engine operation runs here, in order. Starting the microphone
    /// takes a noticeable fraction of a second and must never block the main
    /// thread (which draws the HUD); a serial queue also guarantees a stop
    /// queued after a start always runs after it.
    private let audioQueue = DispatchQueue(label: "jarvis.audio", qos: .userInteractive)
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var phase: Phase = .idle
    private var released = false
    private var latest = ""
    /// Bumped on every start and cancel so late callbacks from an abandoned
    /// capture are recognisable and dropped.
    private var generation = 0
    private var fallback: Timer?

    private var routeObserver: NSObjectProtocol?

    init() {
        // A headset connecting or disconnecting stops the engine under us.
        // Deliver whatever was heard rather than waiting for audio that will
        // never come.
        routeObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self, self.phase == .running else { return }
                self.trace?("audio route changed mid-capture")
                self.finishCapture()
            }
        }
    }

    /// Words the recogniser would otherwise mangle: product and app names.
    private static let vocabulary = [
        "JARVIS", "LeetCode", "Antigravity", "Safari", "Chrome", "Finder",
        "Spotify", "Apple Music", "TextEdit", "Xcode", "VS Code", "GitHub",
        "Gemini", "Azure", "Claude", "Python", "Swift", "JavaScript", "Java",
        "PDF", "Wi-Fi", "Bluetooth", "Spotlight", "System Settings",
    ]

    // MARK: - permissions

    /// `nil` when both permissions are in place, otherwise a sentence the user
    /// can act on. Asks the system the first time, never again after a refusal.
    private func ensureAuthorized(_ done: @escaping @MainActor (String?) -> Void) {
        func speechStep() {
            switch SFSpeechRecognizer.authorizationStatus() {
            case .authorized:
                done(nil)
            case .notDetermined:
                SFSpeechRecognizer.requestAuthorization { status in
                    DispatchQueue.main.async {
                        MainActor.assumeIsolated {
                            done(status == .authorized ? nil
                                 : "Allow Speech Recognition for JARVIS Companion in "
                                   + "System Settings → Privacy & Security")
                        }
                    }
                }
            default:
                done("Allow Speech Recognition for JARVIS Companion in "
                     + "System Settings → Privacy & Security")
            }
        }
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            speechStep()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                DispatchQueue.main.async {
                    MainActor.assumeIsolated {
                        if granted { speechStep() }
                        else { done("Allow the Microphone for JARVIS Companion in "
                                    + "System Settings → Privacy & Security") }
                    }
                }
            }
        default:
            done("Allow the Microphone for JARVIS Companion in "
                 + "System Settings → Privacy & Security")
        }
    }

    // MARK: - lifecycle

    func start() {
        teardown()
        generation += 1
        let gen = generation
        released = false
        latest = ""
        phase = .starting
        ensureAuthorized { [weak self] problem in
            guard let self, self.generation == gen else { return }
            if let problem {
                self.phase = .idle
                self.onProblem?(problem)
                return
            }
            // The key can come up while a permission sheet is on screen.
            if self.released {
                self.phase = .idle
                self.onFinal?("")
                return
            }
            self.begin(gen)
        }
    }

    /// The key was released: stop recording and deliver whatever was heard.
    func stop() {
        released = true
        guard phase == .running else { return }
        finishCapture()
    }

    /// Abandon without delivering anything.
    func cancel() {
        generation += 1
        teardown()
        phase = .idle
    }

    // MARK: - internals

    private final class Once: @unchecked Sendable {
        private let lock = NSLock()
        private var done = false
        func fire() -> Bool {
            lock.lock(); defer { lock.unlock() }
            if done { return false }
            done = true
            return true
        }
    }

    private nonisolated static func installTap(
        on input: AVAudioInputNode, format: AVAudioFormat,
        request: SFSpeechAudioBufferRecognitionRequest,
        first: @escaping @Sendable () -> Void,
        level: @escaping @Sendable (Float) -> Void
    ) {
        let once = Once()
        // Runs on the audio thread: no actor state, no allocation to speak of.
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
            request.append(buffer)
            if once.fire() { first() }
            guard let ch = buffer.floatChannelData?[0] else { return }
            let n = Int(buffer.frameLength)
            if n == 0 { return }
            var sum: Float = 0
            for i in 0..<n { sum += ch[i] * ch[i] }
            level((sum / Float(n)).squareRoot())
        }
    }

    /// Do the slow one-time work (loading the on-device model, opening the
    /// audio device) at launch, so the first real press is as fast as the rest.
    func warmUp() {
        guard SFSpeechRecognizer.authorizationStatus() == .authorized,
              AVCaptureDevice.authorizationStatus(for: .audio) == .authorized,
              let r = recognizer, r.isAvailable else { return }
        let req = SFSpeechAudioBufferRecognitionRequest()
        if r.supportsOnDeviceRecognition { req.requiresOnDeviceRecognition = true }
        let t = r.recognitionTask(with: req) { _, _ in }
        req.endAudio()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { t.cancel() }
        let eng = engine
        audioQueue.async { _ = eng.inputNode.outputFormat(forBus: 0) }
    }

    private func begin(_ gen: Int) {
        guard let recognizer, recognizer.isAvailable else {
            phase = .idle
            onProblem?("Speech recognition is not available right now")
            return
        }
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        req.addsPunctuation = true
        req.contextualStrings = Self.vocabulary
        if recognizer.supportsOnDeviceRecognition {
            req.requiresOnDeviceRecognition = true
        }
        request = req
        recognizer.queue = .main
        task = recognizer.recognitionTask(with: req) { [weak self] result, error in
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self, self.generation == gen else { return }
                    if let result {
                        self.latest = result.bestTranscription.formattedString
                        if result.isFinal { self.deliver() }
                        else { self.onPartial?(self.latest) }
                    } else if error != nil {
                        // "No speech detected" and friends: hand back what we
                        // have, which may be nothing.
                        self.deliver()
                    }
                }
            }
        }

        // Open and start the microphone off the main thread.
        let eng = engine
        let started = Date()
        audioQueue.async { [weak self] in
            let format = eng.inputNode.outputFormat(forBus: 0)
            guard format.sampleRate > 0, format.channelCount > 0 else {
                DispatchQueue.main.async {
                    MainActor.assumeIsolated {
                        guard let self, self.generation == gen else { return }
                        self.teardown()
                        self.phase = .idle
                        self.onProblem?("No microphone found")
                    }
                }
                return
            }
            Self.installTap(on: eng.inputNode, format: format, request: req, first: {
                DispatchQueue.main.async {
                    MainActor.assumeIsolated {
                        guard let self, self.generation == gen else { return }
                        let ms = Int(Date().timeIntervalSince(started) * 1000)
                        self.trace?("first audio after \(ms) ms")
                        self.onReady?()
                    }
                }
            }, level: { rms in
                // -50 dB is a quiet room, -10 dB is shouting at the laptop.
                let db = 20 * log10(max(rms, 1e-6))
                let norm = CGFloat(min(max((db + 50) / 40, 0), 1))
                DispatchQueue.main.async {
                    MainActor.assumeIsolated { self?.onLevel?(norm) }
                }
            })
            eng.prepare()
            do {
                try eng.start()
            } catch {
                DispatchQueue.main.async {
                    MainActor.assumeIsolated {
                        guard let self, self.generation == gen else { return }
                        self.teardown()
                        self.phase = .idle
                        self.onProblem?("Could not start the microphone: \(error.localizedDescription)")
                    }
                }
                return
            }
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self, self.generation == gen else { return }
                    self.phase = .running
                    let ms = Int(Date().timeIntervalSince(started) * 1000)
                    self.trace?("engine running after \(ms) ms")
                    // Released while the engine was spinning up: deliver now.
                    if self.released { self.finishCapture() }
                }
            }
        }
    }

    private func finishCapture() {
        guard phase == .running else { return }
        phase = .finishing
        stopEngine()
        request?.endAudio()
        onLevel?(0)
        // The final result normally lands within a few hundred ms. Never let
        // a recogniser that goes quiet hold the user's request hostage.
        let gen = generation
        fallback?.invalidate()
        fallback = Timer.scheduledTimer(withTimeInterval: 1.6, repeats: false) { [weak self] _ in
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self, self.generation == gen, self.phase == .finishing else { return }
                    self.deliver()
                }
            }
        }
    }

    private func deliver() {
        guard phase == .finishing || phase == .running else { return }
        let text = latest.trimmingCharacters(in: .whitespacesAndNewlines)
        teardown()
        phase = .idle
        generation += 1
        onFinal?(text)
    }

    /// Queued behind any in-flight start, so it can never be overtaken by it.
    private func stopEngine() {
        let eng = engine
        audioQueue.async {
            eng.inputNode.removeTap(onBus: 0)   // a no-op when none is installed
            if eng.isRunning { eng.stop() }
        }
    }

    private func teardown() {
        fallback?.invalidate()
        fallback = nil
        stopEngine()
        task?.cancel()
        task = nil
        request = nil
    }
}
