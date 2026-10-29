import AppKit

/// Carries one request through the engine and keeps the HUD truthful about it.
///
/// Knows nothing about microphones or hotkeys: it is given text, however it
/// arrived (spoken, typed, or from the `--say` test flag), and reports back.
@MainActor
final class TurnRunner {
    let hud: VoiceHUDController
    let daemon: DaemonClient
    weak var overlay: OverlayController?
    let speaker = Speaker()
    lazy var narrator = Narrator(speaker: speaker)

    var trace: ((String) -> Void)?
    /// Fired once per finished request with what was asked and answered.
    var onResult: ((String, String) -> Void)?

    private(set) var turnID = 0
    private(set) var isWorking = false
    /// What the turn in progress was asked.
    private(set) var currentRequest = ""
    /// Requests said while a task was running ("after you're done, ...",
    /// "also ..."). They run in order once the current one finishes - they
    /// used to replace it, abandoning the job half done.
    private(set) var pending: [String] = []
    static let maxPending = 3
    private var answerBuffer = ""
    private var heard = ""
    private var finalAnswer: String?
    private var failed = false
    private var toolsUsed: [String] = []

    init(hud: VoiceHUDController, daemon: DaemonClient, overlay: OverlayController?) {
        self.hud = hud
        self.daemon = daemon
        self.overlay = overlay
    }

    // MARK: running a request

    /// Keep a request for after the current task. False if the queue is full.
    @discardableResult
    func enqueue(_ raw: String) -> Bool {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, pending.count < Self.maxPending else { return false }
        pending.append(text)
        hud.noteQueued(text)
        trace?("queued (\(pending.count)): \(text)")
        return true
    }

    func run(_ raw: String, viaVoice: Bool = true, alreadyShown: Bool = false,
             completion: (() -> Void)? = nil) {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { completion?(); return }
        speaker.stop()
        turnID += 1
        let id = turnID
        heard = text
        currentRequest = text
        answerBuffer = ""
        finalAnswer = nil
        failed = false
        toolsUsed = []
        isWorking = true
        trace?("run: \(text)")
        hud.beginWork(heard: alreadyShown ? "" : text)
        narrator.trace = trace
        narrator.begin(voice: viaVoice && Settings.shared.speakAnswers)

        ensureEngine(id) { [weak self] ok in
            guard let self, self.turnID == id else { return }
            guard ok else {
                self.finishTurn(id, problem: "JARVIS couldn't start. Try `jarvis start` in Terminal.")
                completion?()
                return
            }
            self.daemon.run(
                text: text, voice: true,
                onConfirm: { [weak self] ev, reply in
                    guard let self, self.turnID == id else { reply(false, false); return }
                    self.askConfirmation(ev, reply: reply)
                },
                onEvent: { [weak self] ev in
                    guard let self, self.turnID == id else { return }
                    self.handle(ev)
                },
                onFinish: { [weak self] in
                    guard let self, self.turnID == id else { return }
                    self.finishTurn(id, problem: nil)
                    completion?()
                })
        }
    }

    /// Abandon the turn in progress. Bumping the id orphans every callback
    /// still in flight, so nothing from the old turn can draw again.
    func cancel(interrupt: Bool, showStopped: Bool = true, keepQueue: Bool = false) {
        let wasWorking = isWorking
        if !keepQueue { pending.removeAll() }
        turnID += 1
        speaker.stop()
        // Answer an open "Allow?" even when its card is not showing: the
        // socket thread is blocked on it, so the next request would sit
        // behind the stopped one for minutes.
        if hud.confirmPending { hud.resolveConfirm(approve: false) }
        if interrupt && wasWorking { daemon.interrupt() }
        isWorking = false
        answerBuffer = ""
        narrator.end()
        overlay?.stopPointing()
        overlay?.clearSteps()
        if wasWorking && showStopped && hud.chatOpen {
            hud.showNotice("Stopped.", seconds: 0, icon: "exclamationmark.circle.fill")
        } else {
            hud.dismiss()
        }
    }

    // MARK: engine

    private func engineReachable() -> Bool {
        guard FileManager.default.fileExists(atPath: daemon.socketPath) else { return false }
        let probe = DaemonClient()
        let ok = probe.connect()
        probe.disconnect()
        return ok
    }

    /// Start the engine if it is not running and wait for its socket, so a
    /// spoken request on a cold Mac just works instead of failing.
    private func ensureEngine(_ id: Int, done: @escaping (Bool) -> Void) {
        if engineReachable() { done(true); return }
        trace?("engine down — starting")
        hud.setCaption("Waking JARVIS up")
        guard EngineLauncher.start() else { done(false); return }

        let deadline = Date().addingTimeInterval(150)
        func poll() {
            guard turnID == id else { return }
            if engineReachable() { done(true); return }
            if Date() > deadline { done(false); return }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
                MainActor.assumeIsolated { poll() }
            }
        }
        poll()
    }

    // MARK: events

    private func askConfirmation(_ ev: DaemonClient.Event, reply: @escaping (Bool, Bool) -> Void) {
        let what = HUDText.step(forTool: ev.tool).lowercased()
        let args = ev.raw["args"] as? [String: Any] ?? [:]
        hud.askConfirm(title: "Allow JARVIS to do this?",
                       detail: ev.text.isEmpty ? what.prefix(1).uppercased() + what.dropFirst()
                                               : ev.text,
                       preview: HUDText.preview(ofArgs: args),
                       reply: reply)
        Cue.play(.start)
    }

    private func handle(_ ev: DaemonClient.Event) {
        switch ev.type {
        case "status":
            if let s = HUDText.status(ev.text) { hud.setCaption(s) }
            let l = ev.text.lowercased()
            if l == "checking the result" { narrator.checking() }
        case "plan":
            narrator.planned()
        case "tool_start":
            hud.setCaption(HUDText.step(forTool: ev.tool))
            hud.noteStepStarted(HUDText.step(forTool: ev.tool))
            toolsUsed.append(ev.tool)
            narrator.step(tool: ev.tool, args: ev.raw["args"] as? [String: Any] ?? [:])
        case "tool_end":
            hud.noteStepEnded(ok: ev.ok)
            if !ev.ok {
                hud.setCaption("Trying another way")
                narrator.trouble()
            }
            if let ui = ev.raw["ui"] as? [String: Any], let overlay {
                // A walkthrough: outline every step and tour them. Snapping to
                // real control frames happens here because only this process
                // holds the Accessibility grant — and off the main thread,
                // because a menu bar lookup queries every running app.
                let located = StepHighlight.parse(ui)
                let turn = turnID
                Task.detached(priority: .userInitiated) { [weak self] in
                    let snapped = ControlFrameSnapper.snap(located)
                    await MainActor.run {
                        guard let self, self.turnID == turn else { return }
                        overlay.showSteps(snapped)
                    }
                }
            }
            // Clicks JARVIS makes itself are not drawn: a glowing ring with a
            // tool name on it is noise. Only walkthroughs (show_steps) mark
            // the screen, because those exist to be followed.
        case "text":
            // Narration between tool calls is not the answer yet; keep the
            // orb working and show its tail as the live caption.
            answerBuffer += ev.text
            let tail = answerBuffer.suffix(120).replacingOccurrences(of: "\n", with: " ")
            hud.setCaption(HUDText.plain(String(tail)))
        case "done":
            let final = (ev.text.isEmpty ? answerBuffer : ev.text)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            answerBuffer = ""
            finalAnswer = final
        case "confirm_timeout":
            // Nobody answered in time and the engine was told no: take the
            // card down rather than leave a question that no longer works.
            if hud.confirmPending { hud.resolveConfirm(approve: false) }
        case "error":
            answerBuffer = ""
            failed = true
            finalAnswer = nil
            hud.showNotice(ev.text, seconds: 8)
            Cue.play(.problem)
        default:
            break
        }
    }

    private func finishTurn(_ id: Int, problem: String?) {
        guard turnID == id else { return }
        isWorking = false
        narrator.end()
        overlay?.stopPointing()
        defer { startNextQueued() }
        if let problem {
            hud.showNotice(problem, seconds: 9)
            Cue.play(.problem)
            return
        }
        if failed { return }
        guard let answer = finalAnswer, !answer.isEmpty else {
            // A request that ends with no answer and no error used to vanish
            // silently, which looks exactly like JARVIS being broken. Say so.
            trace?("finished with no answer (tools used: \(toolsUsed.joined(separator: ",")))")
            if toolsUsed.isEmpty {
                hud.showNotice("I didn't get an answer that time. Try again.", seconds: 6)
                Cue.play(.problem)
            } else {
                hud.showNotice("Done.", seconds: 3, icon: "checkmark.circle.fill")
            }
            return
        }
        hud.chips = HUDText.chips(afterTools: toolsUsed, answer: answer)
        hud.showAnswer(answer)
        hud.scheduleDismissAfterAnswer()
        Cue.play(.done)
        if Settings.shared.speakAnswers { speaker.say(HUDText.spoken(answer)) }
        onResult?(heard, answer)
        trace?("answered (\(answer.count) chars)")
    }

    /// Run the next request that was said while the last one was working.
    /// A short pause first, so the previous answer can be seen.
    /// The request stays in the queue until it actually starts, so one that
    /// is overtaken by a new request runs after that one instead of vanishing.
    private func startNextQueued() {
        guard !pending.isEmpty else { return }
        let id = turnID
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [weak self] in
            MainActor.assumeIsolated {
                guard let self, self.turnID == id, !self.isWorking, !self.pending.isEmpty
                else { return }
                // The user is talking: starting now would swap the listening
                // orb for a working one mid-sentence. Wait until they finish.
                if self.hud.phase == .listening { self.startNextQueued(); return }
                let next = self.pending.removeFirst()
                self.trace?("running queued request")
                self.run(next, alreadyShown: true)
            }
        }
    }

    // MARK: pointing

    /// The daemon reports where it looked or clicked; mirror that on screen.
    /// Coordinates arrive top-left origin (CoreGraphics); AppKit wants
    /// bottom-left, so they are flipped against the screen that contains them.
    private func pointFrom(_ ev: DaemonClient.Event) -> (CGPoint, String)? {
        guard let result = ev.raw["result"] as? [String: Any] ?? ev.raw["content"] as? [String: Any]
        else { return pointFromDisplay(ev) }
        guard let x = numeric(result["x"]), let y = numeric(result["y"]) else {
            return pointFromDisplay(ev)
        }
        let label = (result["clicked"] as? String) ?? (result["text"] as? String) ?? ev.tool
        return (flip(CGPoint(x: x, y: y)), label)
    }

    private func pointFromDisplay(_ ev: DaemonClient.Event) -> (CGPoint, String)? {
        let text = ev.text
        guard let open = text.range(of: "("), let close = text.range(of: ")"),
              open.upperBound < close.lowerBound else { return nil }
        let inside = text[open.upperBound..<close.lowerBound]
        let parts = inside.split(separator: ",").map {
            $0.trimmingCharacters(in: .whitespaces) }
        guard parts.count == 2, let x = Double(parts[0]), let y = Double(parts[1])
        else { return nil }
        return (flip(CGPoint(x: x, y: y)), ev.tool)
    }

    private func numeric(_ v: Any?) -> Double? {
        if let d = v as? Double { return d }
        if let i = v as? Int { return Double(i) }
        if let s = v as? String { return Double(s) }
        return nil
    }

    private func flip(_ p: CGPoint) -> CGPoint {
        let screen = NSScreen.screens.first {
            p.x >= $0.frame.minX && p.x <= $0.frame.maxX
        } ?? NSScreen.main
        guard let s = screen else { return p }
        return CGPoint(x: p.x, y: s.frame.maxY - p.y)
    }
}


/// Starts the engine as a child of this app, so it inherits this app's
/// permissions. The marker variable stops `jarvis start` from handing the
/// launch straight back here.
@MainActor
enum EngineLauncher {
    @discardableResult
    static func start() -> Bool {
        let bin = Settings.shared.jarvisBin
        guard FileManager.default.isExecutableFile(atPath: bin) else { return false }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/bash")
        task.arguments = ["-lc", "\"\(bin)\" start >/dev/null 2>&1"]
        var env = ProcessInfo.processInfo.environment
        env["JARVIS_LAUNCHED_BY_APP"] = "1"
        task.environment = env
        do { try task.run() } catch { return false }
        return true
    }
}
