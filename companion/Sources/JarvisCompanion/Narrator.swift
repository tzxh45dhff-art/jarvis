import Foundation

/// Speaks short, truthful progress while a request runs, the way a person
/// would: "On it." ... "Looking that up." ... "Almost done."
///
/// Restraint is the feature. Nothing is said for requests that finish in
/// about a second and a half, no more than one line every seven seconds,
/// never the same line twice in a row, and never over the answer itself.
@MainActor
final class Narrator {
    private let speaker: Speaker
    private var turn = 0
    private var startedAt = Date.distantPast
    private var lastSpoke = Date.distantPast
    private var lastLine = ""
    private var troubleSaid = false
    private var checkSaid = false
    private var spokeAny = false
    private var voice = false
    var trace: ((String) -> Void)?

    static let ackAfter: TimeInterval = 1.6
    static let stepsAfter: TimeInterval = 4.0
    static let gap: TimeInterval = 7.0

    init(speaker: Speaker) { self.speaker = speaker }

    /// A request started. `voice`: spoken updates are wanted (asked by voice,
    /// spoken answers on); otherwise this only keeps time.
    func begin(voice: Bool) {
        turn += 1
        let id = turn
        startedAt = Date()
        lastSpoke = .distantPast
        lastLine = ""
        troubleSaid = false
        checkSaid = false
        spokeAny = false
        self.voice = voice
        guard voice else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.ackAfter) { [weak self] in
            MainActor.assumeIsolated {
                guard let self, self.turn == id, !self.spokeAny else { return }
                self.speak(HUDText.acknowledgements.randomElement() ?? "On it.", force: true)
            }
        }
    }

    func step(tool: String, args: [String: Any]) {
        guard voice, Date().timeIntervalSince(startedAt) >= Self.stepsAfter,
              let line = HUDText.spokenStep(forTool: tool, args: args) else { return }
        speak(line)
    }

    func trouble() {
        guard voice, !troubleSaid, Date().timeIntervalSince(startedAt) >= Self.stepsAfter else { return }
        troubleSaid = true
        speak(HUDText.trouble.randomElement() ?? "", force: true)
    }

    func checking() {
        guard voice, !checkSaid, Date().timeIntervalSince(startedAt) >= Self.stepsAfter else { return }
        checkSaid = true
        speak(HUDText.checking.randomElement() ?? "")
    }

    func planned() {
        guard voice else { return }
        speak(HUDText.longJob.randomElement() ?? "")
    }

    /// The answer (or a stop) is here: no more filler for this request.
    func end() {
        turn += 1
        if spokeAny { speaker.stop() }       // never talk over the answer
        voice = false
    }

    private func speak(_ line: String, force: Bool = false) {
        guard !line.isEmpty, line != lastLine else { return }
        if !force && Date().timeIntervalSince(lastSpoke) < Self.gap { return }
        lastSpoke = Date()
        lastLine = line
        spokeAny = true
        trace?("narrate: \(line)")
        speaker.say(line)
    }
}
