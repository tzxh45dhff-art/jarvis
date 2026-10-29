import Foundation

/// Everything the HUD says, kept free of UI so it can be tested without a
/// screen: friendly step names, markdown clean-up, yes/no parsing, timing.
enum HUDText {

    // MARK: step names

    private static let tools: [String: String] = [
        "active_editor": "Checking your editor",
        "browser_click": "Clicking on the page",
        "browser_find": "Looking at the page",
        "browser_js": "Running a page script",
        "browser_open": "Opening a page",
        "browser_read": "Reading the page",
        "browser_select": "Choosing an option",
        "browser_set_editor": "Typing into the editor",
        "browser_tabs": "Checking your tabs",
        "browser_type": "Typing on the page",
        "check_availability": "Checking your calendar",
        "check_calendar": "Checking your calendar",
        "check_job": "Checking a background job",
        "check_reminders": "Checking your reminders",
        "choose_menu": "Using a menu",
        "click": "Clicking",
        "click_text": "Clicking",
        "complete_reminder": "Completing a reminder",
        "control_playback": "Controlling playback",
        "copy_file": "Copying a file",
        "create_event": "Adding a calendar event",
        "create_note": "Creating a note",
        "create_reminder": "Adding a reminder",
        "delete_event": "Removing a calendar event",
        "delete_reminder": "Removing a reminder",
        "download_file": "Downloading a file",
        "drag": "Dragging",
        "edit_file": "Editing a file",
        "fetch_many": "Reading pages",
        "fetch_page": "Reading a web page",
        "file_dialog": "Using a file dialog",
        "file_info": "Checking a file",
        "find_contact": "Looking up a contact",
        "find_files": "Searching for files",
        "find_on_screen": "Looking for it on screen",
        "forget": "Forgetting that",
        "get_defaults": "Checking your settings",
        "get_weather": "Checking the weather",
        "inspect_ui": "Looking at the app",
        "leetcode_next": "Finding the next problem",
        "list_apps": "Checking open apps",
        "list_dir": "Looking in a folder",
        "list_installed_apps": "Checking installed apps",
        "list_memories": "Recalling what I know",
        "list_shortcuts": "Checking your shortcuts",
        "lock_screen": "Locking the screen",
        "look_at_image": "Looking at an image",
        "make_dir": "Making a folder",
        "mouse_move": "Moving the pointer",
        "move_file": "Moving a file",
        "news_search": "Searching the news",
        "notify": "Sending a notification",
        "now_playing": "Checking what's playing",
        "open_app": "Opening an app",
        "open_file": "Opening a file",
        "open_url": "Opening a link",
        "paste_text": "Pasting text",
        "play_music": "Playing music",
        "playbook": "Looking up the steps",
        "press_element": "Pressing a button",
        "press_key": "Pressing a key",
        "python_eval": "Calculating",
        "quit_app": "Quitting an app",
        "quit_apps": "Quitting apps",
        "read_clipboard": "Reading the clipboard",
        "read_file": "Reading a file",
        "read_messages": "Reading messages",
        "recall": "Remembering",
        "remember": "Remembering that",
        "run_background": "Starting a background job",
        "run_shell": "Running a command",
        "run_shortcut": "Running a shortcut",
        "screen_info": "Checking the display",
        "screenshot": "Taking a screenshot",
        "scroll": "Scrolling",
        "search_music": "Searching music",
        "search_notes": "Searching your notes",
        "search_papers": "Searching papers",
        "search_text": "Searching text",
        "see_screen": "Looking at the screen",
        "select_option": "Choosing an option",
        "send_email": "Sending an email",
        "send_message": "Sending a message",
        "set_brightness": "Setting the brightness",
        "set_dark_mode": "Switching appearance",
        "set_default_app": "Setting a default app",
        "set_field": "Filling in a field",
        "set_focus": "Setting your focus",
        "set_volume": "Setting the volume",
        "set_wifi": "Switching Wi-Fi",
        "show_steps": "Preparing a walkthrough",
        "stop_job": "Stopping a job",
        "system_status": "Checking the system",
        "trash_files": "Moving files to the Trash",
        "type_into": "Typing",
        "type_text": "Typing",
        "ui_elements": "Looking at the app",
        "update_event": "Updating a calendar event",
        "wait_for_download": "Waiting for the download",
        "web_search": "Searching the web",
        "write_clipboard": "Copying to the clipboard",
        "write_document": "Writing a document",
        "write_file": "Writing a file",
    ]

    /// A short human phrase for a tool name; never leaks `snake_case`.
    static func step(forTool name: String) -> String {
        if let s = tools[name] { return s }
        let words = name.replacingOccurrences(of: "_", with: " ")
            .trimmingCharacters(in: .whitespaces)
        return words.isEmpty ? "Working on it" : words.prefix(1).uppercased() + words.dropFirst()
    }

    // MARK: spoken progress

    /// What to say out loud while a step runs: short, varied, and true to what
    /// is actually happening. `nil` for steps not worth narrating (lookups of
    /// tool lists, file sizes, internal bookkeeping).
    static func spokenStep(forTool name: String, args: [String: Any],
                           pick: (Int) -> Int = { Int.random(in: 0..<$0) }) -> String? {
        func one(_ options: [String]) -> String { options[pick(options.count)] }
        func arg(_ k: String) -> String {
            let v = (args[k] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            return v.count > 40 ? String(v.prefix(40)) : v
        }
        switch name {
        case "web_answer", "web_search", "news_search", "wikipedia", "fetch_many":
            return one(["Looking that up.", "Searching for that now.", "Let me check the web.",
                        "Give me a sec, looking it up."])
        case "fetch_page", "browser_read", "read_document":
            return one(["Reading through it.", "Going through the page.", "Reading that now."])
        case "open_app":
            let n = arg("name")
            return n.isEmpty ? "Opening it." : one(["Opening \(n).", "Bringing up \(n)."])
        case "open_url", "browser_open":
            let u = arg("url")
            let host = URL(string: u)?.host?.replacingOccurrences(of: "www.", with: "") ?? ""
            return host.isEmpty ? "Opening the page." : "Opening \(host)."
        case "play_music":
            let q = arg("query")
            return q.isEmpty ? "Starting your music." : one(["Putting on \(q).", "Finding \(q)."])
        case "make_document", "write_document":
            return one(["Putting the document together.", "Building the PDF now.",
                        "Laying out the document."])
        case "make_presentation", "edit_presentation":
            return one(["Working on the slides.", "Building the deck."])
        case "make_chart":
            return "Drawing the chart."
        case "find_images":
            return one(["Finding a good photo.", "Looking for a picture."])
        case "find_contact":
            return "Checking your contacts."
        case "check_calendar", "check_availability":
            return "Checking your calendar."
        case "check_reminders":
            return "Looking at your reminders."
        case "get_weather":
            return "Checking the weather."
        case "see_screen", "find_on_screen", "screenshot":
            return one(["Taking a look at your screen.", "Let me see what's on screen."])
        case "click", "click_text", "press_element", "browser_click":
            return one(["Clicking that.", "Pressing it."])
        case "type_text", "type_into", "browser_type", "set_field":
            return "Typing it in."
        case "run_shell", "python_eval":
            return one(["Running that.", "Working it out."])
        case "blender_script":
            return "Building it in Blender."
        case "find_files", "search_text":
            return one(["Searching your files.", "Looking through your files."])
        case "directions":
            return "Checking the route."
        case "download_file":
            return "Downloading it."
        case "convert_file", "image_edit", "video_tool":
            return "Converting it."
        default:
            return nil
        }
    }

    /// One-tap follow-ups for an answer, from what was just done. Empty when
    /// nothing obvious comes next (better none than filler).
    static func chips(afterTools tools: [String], answer: String) -> [String] {
        let t = Set(tools)
        if !t.isDisjoint(with: ["play_music", "control_playback"]) {
            return ["Pause", "Next song", "Play something similar"]
        }
        if !t.isDisjoint(with: ["make_document", "write_document"]) { return ["Open it", "Show it in Finder"] }
        if !t.isDisjoint(with: ["make_presentation", "edit_presentation"]) { return ["Open it", "Add a slide"] }
        if t.contains("get_weather") { return ["What about tomorrow?"] }
        if t.contains("web_answer") || t.contains("web_search") { return ["Tell me more", "What are the sources?"] }
        if t.contains("check_calendar") { return ["What about tomorrow?", "Am I free this evening?"] }
        if t.contains("make_chart") { return ["Open it"] }
        return []
    }

    static let acknowledgements = ["On it.", "Okay, one sec.", "Sure, give me a moment.",
                                   "Got it, working on it.", "Alright, on it."]
    static let trouble = ["Hmm, that didn't work. Trying another way.",
                          "That didn't go through, trying something else.",
                          "Hit a snag, trying another way."]
    static let checking = ["Almost done, just double-checking.", "Nearly there, checking it worked."]
    static let longJob = ["This one has a few steps. I'll keep you posted.",
                          "Okay, a few steps to this one. Bear with me."]

    /// Engine status lines worth showing, rewritten; `nil` means stay quiet.
    static func status(_ raw: String) -> String? {
        let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        let l = t.lowercased()
        if t.isEmpty { return nil }
        if l.hasPrefix("recalled ") { return nil }
        if l == "verified" || l == "listening" || l == "transcribing" { return nil }
        if l.hasPrefix("planned ") { return "Planning the steps" }
        if l == "checking the result" { return "Double-checking" }
        if l.hasPrefix("corrected") { return "Fixing a detail" }
        if l == "interrupted" { return "Stopped" }
        if l.hasPrefix("step budget") { return "That's taking a lot of steps" }
        if l.hasPrefix("warming") || l.hasPrefix("loading") || l.hasPrefix("starting") {
            return "Waking JARVIS up"
        }
        if l.hasPrefix("default set") { return nil }
        return t
    }

    /// The one argument worth showing someone deciding whether to allow a call.
    static func preview(ofArgs args: [String: Any]) -> String {
        let keys = ["command", "cmd", "script", "paths", "files", "path", "src", "dest", "to", "recipient",
                    "url", "query", "app", "name", "title", "text", "content", "body"]
        for k in keys {
            if let v = args[k] {
                let raw: String = (v as? [Any]).map { $0.map { "\($0)" }.joined(separator: ", ") } ?? "\(v)"
                let s = raw.replacingOccurrences(of: "\n", with: " ⏎ ")
                    .trimmingCharacters(in: .whitespaces)
                if !s.isEmpty { return k == "command" || k == "cmd" || k == "script"
                                ? s.prefix(140) + (s.count > 140 ? "…" : "")
                                : s.prefix(120) + (s.count > 120 ? "…" : "") }
            }
        }
        return ""
    }

    // MARK: markdown

    /// Chat bubbles render inline markdown only, so block syntax is rewritten
    /// into plain text that still reads well.
    static func prepareMarkdown(_ s: String) -> String {
        var out: [String] = []
        var inFence = false
        for line in s.components(separatedBy: "\n") {
            let t = line.trimmingCharacters(in: .whitespaces)
            if t.hasPrefix("```") { inFence.toggle(); continue }
            if inFence { out.append("    " + line); continue }
            var l = line
            if t.hasPrefix("#") {
                l = String(t.drop(while: { $0 == "#" })).trimmingCharacters(in: .whitespaces)
                l = "**" + l + "**"
            } else if t.hasPrefix("- ") || t.hasPrefix("* ") {
                let indent = line.prefix(while: { $0 == " " })
                l = indent + "• " + t.dropFirst(2)
            }
            out.append(l)
        }
        return out.joined(separator: "\n")
    }

    /// Plain text with no markdown markers, for speech and the one-line capsule.
    static func plain(_ s: String) -> String {
        var t = prepareMarkdown(s)
        for m in ["**", "__", "`"] { t = t.replacingOccurrences(of: m, with: "") }
        // [label](url) -> label
        if let re = try? NSRegularExpression(pattern: #"\[([^\]]+)\]\([^)]+\)"#) {
            t = re.stringByReplacingMatches(in: t, range: NSRange(t.startIndex..., in: t),
                                            withTemplate: "$1")
        }
        return t.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: repeats and stops

    private static func words(_ s: String) -> Set<String> {
        Set(s.lowercased().split(whereSeparator: { !$0.isLetter && !$0.isNumber })
            .map(String.init).filter { $0.count > 2 })
    }

    /// Whether two requests ask for the same thing - someone re-saying a job
    /// that is already under way because it is taking a while.
    static func similar(_ a: String, _ b: String) -> Bool {
        let x = words(a), y = words(b)
        guard x.count >= 2, y.count >= 2 else { return false }
        let shared = Double(x.intersection(y).count)
        return shared / Double(min(x.count, y.count)) >= 0.7
    }

    static func isStopIntent(_ s: String) -> Bool {
        let t = s.lowercased().trimmingCharacters(in: .whitespacesAndNewlines.union(.punctuationCharacters))
        let phrases: Set<String> = ["stop", "cancel", "never mind", "nevermind", "forget it",
                                    "stop that", "stop it", "cancel that", "abort", "enough",
                                    "stop doing that", "leave it", "that's enough"]
        return phrases.contains(t)
    }

    /// The user means "drop what you are doing and do this instead".
    static func isReplaceIntent(_ s: String) -> Bool {
        let t = s.lowercased().trimmingCharacters(in: .whitespaces)
        let starts = ["actually", "instead", "wait,", "wait ", "no,", "no ", "no wait",
                      "cancel that", "scratch that", "forget that", "never mind that",
                      "stop that and", "stop and ", "change of plan", "rather "]
        return starts.contains { t.hasPrefix($0) } || t.contains(" instead")
    }

    // MARK: spoken yes / no

    enum Intent: Equatable { case allow, deny, unknown }

    static func confirmIntent(_ spoken: String) -> Intent {
        let cleaned = spoken.lowercased().unicodeScalars
            .map { CharacterSet.letters.contains($0) || $0 == " " || $0 == "'" ? Character($0) : " " }
        let words = Set(String(cleaned).split(separator: " ").map(String.init))
        let deny: Set<String> = ["no", "nope", "deny", "cancel", "stop", "don't", "dont",
                                 "never", "negative", "decline", "abort"]
        let allow: Set<String> = ["yes", "yeah", "yep", "yup", "allow", "approve", "ok",
                                  "okay", "sure", "confirm", "proceed", "continue",
                                  "affirmative", "go"]
        let d = !words.isDisjoint(with: deny)
        let a = !words.isDisjoint(with: allow)
        if d { return .deny }          // "no" always wins: refusing is the safe side
        if a { return .allow }
        return .unknown
    }

    /// A yes / no short enough to be an answer to an "Allow?" card rather
    /// than a request ("go to my settings" is a request, "go ahead" is not).
    static func shortConfirmAnswer(_ spoken: String) -> Intent {
        let words = spoken.split(whereSeparator: { $0 == " " || $0 == "," })
        guard !words.isEmpty, words.count <= 4 else { return .unknown }
        return confirmIntent(spoken)
    }

    // MARK: spoken form

    /// What to say aloud for an answer. Long answers are read in part - a
    /// listener cannot skim, and the whole text is on screen anyway.
    static func spoken(_ answer: String, maxChars: Int = 420) -> String {
        let t = plain(answer)
        guard t.count > maxChars else { return t }
        let head = String(t.prefix(maxChars))
        // Prefer the last sentence end; otherwise the last word break.
        var best: String.Index?
        for end in [". ", "! ", "? "] {
            if let r = head.range(of: end, options: .backwards),
               best == nil || r.upperBound > best! { best = r.upperBound }
        }
        let chosen = best ?? head.lastIndex(of: " ") ?? head.endIndex
        let piece = String(head[..<chosen]).trimmingCharacters(in: .whitespaces)
        return piece + " The rest is on screen."
    }

    // MARK: timing

    /// How long a finished answer stays up before fading.
    static func dwell(forAnswer text: String) -> Double {
        let words = text.split(whereSeparator: { $0 == " " || $0 == "\n" }).count
        return min(max(5.0, 2.5 + Double(words) * 0.30), 14.0)
    }

    /// Orb label while a task runs: just "working" until it is long enough
    /// that a number is reassuring rather than noisy.
    static func workingLabel(elapsed: Int) -> String {
        elapsed >= 8 ? "working · \(elapsed)s" : "working"
    }

    // MARK: self test

    /// Returns a description of every check that failed (empty means all good).
    /// Whether an Esc should stop JARVIS: typed on a keyboard (source pid 0)
    /// or posted by some other program, but not posted by the engine (or by
    /// this app) - the engine presses Escape itself to close menus mid-task.
    static func escIsFromUser(sourcePID: Int64, ownPID: Int64, enginePID: Int64?) -> Bool {
        if sourcePID == 0 { return true }
        return sourcePID != ownPID && sourcePID != enginePID
    }

    static func selfTest() -> [String] {
        var bad: [String] = []
        func check(_ name: String, _ ok: Bool) { if !ok { bad.append(name) } }

        check("keyboard Esc stops", escIsFromUser(sourcePID: 0, ownPID: 10, enginePID: 20))
        check("engine's own Esc does not stop", !escIsFromUser(sourcePID: 20, ownPID: 10, enginePID: 20))
        check("own Esc does not stop", !escIsFromUser(sourcePID: 10, ownPID: 10, enginePID: nil))
        check("another app's Esc stops", escIsFromUser(sourcePID: 30, ownPID: 10, enginePID: 20))
        check("Esc with no engine pid", escIsFromUser(sourcePID: 30, ownPID: 10, enginePID: nil))

        check("friendly tool", step(forTool: "web_search") == "Searching the web")
        check("unknown tool is humanised", step(forTool: "do_odd_thing") == "Do odd thing")
        check("empty tool", step(forTool: "") == "Working on it")
        check("no snake_case leaks", !tools.values.contains { $0.contains("_") })
        check("status hides memory chatter", status("recalled 2 memory(ies)") == nil)
        check("status rewrites plan", status("planned 4 steps") == "Planning the steps")
        check("status keeps unknown", status("Resizing the window") == "Resizing the window")
        let first = { (_: Int) in 0 }
        check("spoken: app by name",
              spokenStep(forTool: "open_app", args: ["name": "Spotify"], pick: first) == "Opening Spotify.")
        check("spoken: site by host",
              spokenStep(forTool: "browser_open", args: ["url": "https://www.netflix.com/browse"], pick: first) == "Opening netflix.com.")
        check("spoken: bookkeeping stays quiet", spokenStep(forTool: "find_tools", args: [:]) == nil)
        check("chips after music", chips(afterTools: ["play_music"], answer: "").first == "Pause")
        check("no chips for chit-chat", chips(afterTools: [], answer: "Hi!").isEmpty)
        check("spoken: never snake_case",
              ["web_answer", "make_document", "play_music", "see_screen"].allSatisfy {
                  !(spokenStep(forTool: $0, args: [:]) ?? "").contains("_") })

        check("yes", confirmIntent("Yes, go ahead") == .allow)
        check("okay", confirmIntent("okay") == .allow)
        check("no", confirmIntent("No.") == .deny)
        check("no beats yes", confirmIntent("yes no wait") == .deny)
        check("don't", confirmIntent("don't do that") == .deny)
        check("gibberish", confirmIntent("purple monkey") == .unknown)
        check("short yes answers a card", shortConfirmAnswer("Yes, go ahead.") == .allow)
        check("short no answers a card", shortConfirmAnswer("No") == .deny)
        check("a request is not a card answer",
              shortConfirmAnswer("Open Safari and go to the settings page") == .unknown)
        check("empty is not a card answer", shortConfirmAnswer("") == .unknown)
        check("empty", confirmIntent("") == .unknown)

        check("short answers are spoken whole", spoken("All done.") == "All done.")
        let long = String(repeating: "This is a sentence about something. ", count: 40)
        let said = spoken(long)
        check("long answers are trimmed at a sentence", said.hasSuffix("The rest is on screen.")
              && said.count < 480 && said.contains("something. The rest"))
        check("no sentence boundary still trims", spoken(String(repeating: "word ", count: 200))
              .hasSuffix("The rest is on screen."))

        check("a re-said request is recognised",
              similar("Could you open Netflix login into my ID and resume the episode of the mentalist?",
                      "Open Netflix with my ID and play resume the episode of the mentalist"))
        check("a different request is not", !similar("open YouTube and play lo-fi music",
                                                    "open Netflix and resume the mentalist"))
        check("a one-word reply is never 'the same'", !similar("no", "no"))
        check("'actually' replaces", isReplaceIntent("Actually, open Safari"))
        check("'instead' replaces", isReplaceIntent("play jazz instead"))
        check("a follow-up does not replace",
              !isReplaceIntent("After you're done, set the volume to minimum and brightness to max")
              && !isReplaceIntent("Also put it on full screen"))
        check("stop is a stop", isStopIntent("Stop.") && isStopIntent("never mind") && isStopIntent("Cancel that!"))
        check("a normal request is not a stop", !isStopIntent("stop the music and open Safari"))

        check("dwell floor", dwell(forAnswer: "ok") == 5.0)
        check("dwell cap", dwell(forAnswer: String(repeating: "word ", count: 200)) == 14.0)
        check("label short", workingLabel(elapsed: 3) == "working")
        check("label long", workingLabel(elapsed: 12) == "working · 12s")

        check("heading", prepareMarkdown("# Title") == "**Title**")
        check("bullet", prepareMarkdown("- one") == "• one")
        check("fence dropped", !prepareMarkdown("```\nx\n```").contains("```"))
        check("plain strips bold", plain("**Done** and `ls`") == "Done and ls")
        check("plain strips link", plain("see [docs](https://x.y)") == "see docs")

        check("preview command", preview(ofArgs: ["command": "ls -la"]) == "ls -la")
        check("preview truncates", preview(ofArgs: ["command": String(repeating: "a", count: 300)]).count <= 141)
        check("preview empty", preview(ofArgs: [:]) == "")
        check("preview list", preview(ofArgs: ["paths": ["/a", "/b"]]) == "/a, /b")
        return bad
    }
}
