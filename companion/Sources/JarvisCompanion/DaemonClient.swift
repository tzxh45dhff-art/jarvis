import Foundation

/// Line-delimited JSON over the same Unix socket the Python CLI uses.
///
/// The companion is deliberately a thin client: every bit of intelligence —
/// the model, the tools, memory, safety — already lives in the daemon. Swift
/// owns the things Swift is good at (a borderless overlay, a global event tap,
/// smooth animation) and nothing else. That keeps one brain, not two.
final class DaemonClient {
    struct Event {
        let type: String
        let text: String
        let tool: String
        let ok: Bool
        let meta: [String: Any]
        let raw: [String: Any]
    }

    private var fd: Int32 = -1
    private var readBuffer = Data()
    private let queue = DispatchQueue(label: "jarvis.daemon.io")
    private(set) var isConnected = false
    /// Set by the app so socket failures are visible from outside the process.
    var trace: ((String) -> Void)?

    /// Point at a different socket (the voice helper's) instead of the engine's.
    var socketPathOverride: String?

    var socketPath: String {
        socketPathOverride ?? (NSHomeDirectory() as NSString)
            .appendingPathComponent(".jarvis/run/jarvisd.sock")
    }

    func connect() -> Bool {
        disconnect()
        let path = socketPath
        guard FileManager.default.fileExists(atPath: path) else { return false }

        let sock = socket(AF_UNIX, SOCK_STREAM, 0)
        guard sock >= 0 else { return false }
        // Writing to an engine that has gone away (restarted, crashed) must
        // fail with EPIPE, not raise SIGPIPE - whose default kills this app.
        var noSigPipe: Int32 = 1
        setsockopt(sock, SOL_SOCKET, SO_NOSIGPIPE, &noSigPipe,
                   socklen_t(MemoryLayout<Int32>.size))

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(path.utf8)
        guard pathBytes.count < MemoryLayout.size(ofValue: addr.sun_path) else {
            close(sock); return false
        }
        withUnsafeMutablePointer(to: &addr.sun_path) { tuplePtr in
            tuplePtr.withMemoryRebound(to: CChar.self, capacity: pathBytes.count + 1) { dst in
                for (i, b) in pathBytes.enumerated() { dst[i] = CChar(bitPattern: b) }
                dst[pathBytes.count] = 0
            }
        }
        let size = socklen_t(MemoryLayout<sockaddr_un>.size)
        let ok = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                Darwin.connect(sock, sa, size) == 0
            }
        }
        guard ok else { close(sock); return false }
        fd = sock
        isConnected = true
        readBuffer.removeAll()
        return true
    }

    func disconnect() {
        if fd >= 0 { close(fd) }
        fd = -1
        isConnected = false
    }

    private func send(_ object: [String: Any]) -> Bool {
        guard fd >= 0,
              var data = try? JSONSerialization.data(withJSONObject: object) else { return false }
        data.append(0x0A)
        return data.withUnsafeBytes { buf -> Bool in
            var sent = 0
            let base = buf.bindMemory(to: UInt8.self).baseAddress!
            while sent < data.count {
                let n = write(fd, base + sent, data.count - sent)
                if n <= 0 { return false }
                sent += n
            }
            return true
        }
    }

    /// Read one framed JSON message, blocking until a full line arrives.
    /// nil means the connection ended (or timed out) - never a bad line: one
    /// unparseable line used to end the turn while the engine kept streaming,
    /// and its leftovers were then read as the next request's reply.
    private func readMessage() -> [String: Any]? {
        while true {
            if let idx = readBuffer.firstIndex(of: 0x0A) {
                let line = readBuffer.subdata(in: readBuffer.startIndex..<idx)
                readBuffer.removeSubrange(readBuffer.startIndex...idx)
                if line.isEmpty { continue }
                if let msg = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] {
                    return msg
                }
                trace?("daemon: skipped an unreadable line (\(line.count) bytes)")
                continue
            }
            var chunk = [UInt8](repeating: 0, count: 65536)
            let n = read(fd, &chunk, chunk.count)
            if n < 0 && errno == EINTR { continue }
            if n <= 0 { return nil }
            readBuffer.append(contentsOf: chunk[0..<n])
        }
    }

    /// Connect, send one request, read one reply, disconnect. For small
    /// helpers whose protocol is a single line each way.
    func oneShot(_ object: [String: Any], timeout: TimeInterval = 2) -> [String: Any]? {
        guard connect() else { return nil }
        defer { disconnect() }
        var tv = timeval(tv_sec: Int(timeout), tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        guard send(object) else { return nil }
        return readMessage()
    }

    /// Collects the user's answer to a confirmation from the main thread so the
    /// socket thread can wait for it.
    private final class Decision: @unchecked Sendable {
        private let lock = NSLock()
        private var value: (Bool, Bool)?
        func set(_ approve: Bool, _ remember: Bool) {
            lock.lock(); defer { lock.unlock() }
            if value == nil { value = (approve, remember) }
        }
        var current: (Bool, Bool)? {
            lock.lock(); defer { lock.unlock() }
            return value
        }
    }

    /// Ask the daemon to run a task, streaming every event back on the main queue.
    ///
    /// `onConfirm` is called on the main queue when a tool needs the user's OK.
    /// It must eventually call `reply(approve, remember)`; without a handler,
    /// or if nobody answers in time, the request is declined.
    func run(text: String,
             voice: Bool = true,
             onConfirm: ((Event, @escaping (Bool, Bool) -> Void) -> Void)? = nil,
             onEvent: @escaping (Event) -> Void,
             onFinish: @escaping () -> Void) {
        queue.async { [weak self] in
            guard let self else { return }
            let request: [String: Any] = ["type": "run", "text": text, "voice": voice]
            // The connection is kept between requests, so it can belong to an
            // engine that has since restarted. A request that cannot be sent
            // on it gets one fresh connection before it is reported as failed.
            var sent = (self.isConnected || self.connect()) && self.send(request)
            if !sent { sent = self.connect() && self.send(request) }
            // The engine may be restarting (an update, Restart engine): wait
            // for it to come back rather than failing the request.
            let deadline = Date().addingTimeInterval(10)
            while !sent && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.4)
                sent = self.connect() && self.send(request)
            }
            guard sent else {
                let up = FileManager.default.fileExists(atPath: self.socketPath)
                self.disconnect()
                self.trace?("run: could not reach the engine (socket \(up ? "present" : "missing"))")
                DispatchQueue.main.async {
                    onEvent(Event(type: "error",
                                  text: up ? "Lost the connection to JARVIS. Try again."
                                           : "JARVIS isn't running. Choose Start engine from the menu bar.",
                                  tool: "", ok: false, meta: [:], raw: [:]))
                    onFinish()
                }
                return
            }
            var ended = false
            var gotAny = false
            var retried = false
            while true {
                guard let msg = self.readMessage() else {
                    // Dropped before the engine did anything with it (it was
                    // shutting down for a restart): nothing ran, so send it
                    // once more to the engine that replaces it.
                    if !gotAny && !retried {
                        retried = true
                        self.disconnect()
                        let until = Date().addingTimeInterval(10)
                        var again = false
                        while !again && Date() < until {
                            Thread.sleep(forTimeInterval: 0.4)
                            again = self.connect() && self.send(request)
                        }
                        if again { self.trace?("run: engine restarted; request resent"); continue }
                    }
                    break
                }
                gotAny = true
                let type = msg["type"] as? String ?? ""
                if type == "confirm" {
                    let tool = msg["tool"] as? String ?? ""
                    let ev = Event(type: "needs_confirm",
                                   text: msg["reason"] as? String ?? "",
                                   tool: tool, ok: false, meta: [:], raw: msg)
                    guard let onConfirm else {
                        // Nobody can answer from here: decline rather than hang.
                        _ = self.send(["type": "confirm_reply", "approve": false,
                                       "remember": false])
                        DispatchQueue.main.async { onEvent(ev) }
                        continue
                    }
                    let box = Decision()
                    let gate = DispatchSemaphore(value: 0)
                    DispatchQueue.main.async {
                        onConfirm(ev) { approve, remember in
                            box.set(approve, remember)
                            gate.signal()
                        }
                    }
                    // The daemon itself gives up after 180 s; answer just before.
                    let waited = gate.wait(timeout: .now() + 170)
                    let answer = waited == .success ? (box.current ?? (false, false)) : (false, false)
                    if waited != .success {
                        DispatchQueue.main.async {
                            onEvent(Event(type: "confirm_timeout", text: "", tool: tool,
                                          ok: false, meta: [:], raw: [:]))
                        }
                    }
                    _ = self.send(["type": "confirm_reply", "approve": answer.0,
                                   "remember": answer.0 && answer.1])
                    continue
                }
                let ev = Event(type: type,
                               text: msg["text"] as? String ?? "",
                               tool: msg["tool"] as? String ?? "",
                               ok: msg["ok"] as? Bool ?? true,
                               meta: msg["meta"] as? [String: Any] ?? [:],
                               raw: msg)
                DispatchQueue.main.async { onEvent(ev) }
                if type == "done" || type == "error" { ended = true; break }
            }
            if !ended {
                // The engine hung up mid-request (crashed, restarted, stopped).
                // Drop the dead connection so the next request makes a new
                // one, and say what happened instead of silently vanishing.
                self.disconnect()
                self.trace?("run: engine closed the connection mid-request")
                DispatchQueue.main.async {
                    onEvent(Event(type: "error", text: "Lost the connection to JARVIS. Try again.",
                                  tool: "", ok: false, meta: [:], raw: [:]))
                }
            }
            DispatchQueue.main.async { onFinish() }
        }
    }

    /// Hold-to-talk: the daemon owns the microphone (it already has Whisper and
    /// the VAD), so this just tells it when to start and stop listening.
    func listen(onPartial: @escaping (String) -> Void,
                onTranscript: @escaping (String) -> Void,
                onEvent: @escaping (Event) -> Void,
                onFinish: @escaping () -> Void) {
        queue.async { [weak self] in
            guard let self else { return }
            if !self.isConnected, !self.connect() {
                self.trace?("listen: connect FAILED (socket \(self.socketPath))")
                DispatchQueue.main.async {
                    onEvent(Event(type: "error", text: "engine not running",
                                  tool: "", ok: false, meta: [:], raw: [:]))
                    onFinish()
                }
                return
            }
            self.trace?("listen: connected")
            guard self.send(["type": "listen", "voice": true]) else {
                self.trace?("listen: send FAILED")
                DispatchQueue.main.async { onFinish() }
                return
            }
            self.trace?("listen: request sent")
            while let msg = self.readMessage() {
                let type = msg["type"] as? String ?? ""
                if type == "confirm" {
                    _ = self.send(["type": "confirm_reply", "approve": false,
                                   "remember": false])
                    continue
                }
                if type == "transcript" {
                    let t = msg["text"] as? String ?? ""
                    DispatchQueue.main.async { onTranscript(t) }
                    continue
                }
                if type == "partial" {
                    let t = msg["text"] as? String ?? ""
                    DispatchQueue.main.async { onPartial(t) }
                    continue
                }
                let ev = Event(type: type,
                               text: msg["text"] as? String ?? "",
                               tool: msg["tool"] as? String ?? "",
                               ok: msg["ok"] as? Bool ?? true,
                               meta: msg["meta"] as? [String: Any] ?? [:],
                               raw: msg)
                DispatchQueue.main.async { onEvent(ev) }
                if type == "done" || type == "error" { break }
            }
            DispatchQueue.main.async { onFinish() }
        }
    }

    /// Tell the daemon the user released the key, so it should stop recording.
    func stopListening() {
        // A separate connection: the listening one is blocked reading its
        // stream, and the daemon must hear the release immediately.
        let side = DaemonClient()
        side.trace = trace
        if side.connect() {
            _ = side.send(["type": "stop_listening"])
            side.disconnect()
            trace?("stop_listening sent")
        } else {
            trace?("stop_listening: connect FAILED")
        }
    }

    /// Forget the conversation so far (the engine keeps its tools and memory).
    func reset() {
        let side = DaemonClient()
        if side.connect() {
            _ = side.send(["type": "reset"])
            side.disconnect()
        }
    }

    func interrupt() {
        // A separate short-lived connection, because the main one is mid-stream.
        let side = DaemonClient()
        if side.connect() {
            _ = side.send(["type": "interrupt"])
            side.disconnect()
        }
    }
}
