import AppKit
import Carbon.HIToolbox

/// Global activation, by two independent routes.
///
/// **Hold ⌃⌥** is the nice one, but a modifier-only chord can only be observed
/// with a CGEvent tap, and that needs Accessibility. An ad-hoc signed build
/// changes identity on every rebuild, so that grant keeps evaporating.
///
/// **⌃⌥Space** is therefore always registered too, through Carbon's
/// `RegisterEventHotKey`, which needs no permission whatsoever. It toggles:
/// press once to start listening, again to stop. That means the companion is
/// useful the moment it launches, and simply gets nicer once Accessibility is
/// granted.
final class HotkeyMonitor {
    var onPress: (() -> Void)?
    var onRelease: (() -> Void)?
    /// The chord turned out to be part of a shortcut (a key arrived just after
    /// it fired): stop listening without acting.
    var onCancel: (() -> Void)?
    /// Reports what actually came up, for the UI to explain to the user.
    var onModeChange: ((Bool) -> Void)?
    /// Whether the app is listening right now. The ⌃⌥Space toggle follows
    /// it, because listening also ends by Esc, the time cap or a problem.
    var isListening: (() -> Bool)?

    private var tap: CFMachPort?
    private var runLoopSource: CFRunLoopSource?
    private var isHeld = false
    /// The chord must stay down this long, with no other key, before it counts.
    /// Without it, ⌃⌥+arrow window shortcuts would start the microphone.
    private let holdThreshold: TimeInterval = 0.12
    private var pendingPress: DispatchWorkItem?
    private var pressFired = false
    private var pressedAt = Date.distantPast

    private var hotKeyRef: EventHotKeyRef?
    private var carbonHandler: EventHandlerRef?
    private var toggledOn = false

    private var permissionPoll: Timer?
    private(set) var holdToTalkActive = false

    // MARK: - lifecycle

    /// Always succeeds: the Carbon fallback needs no permission.
    @discardableResult
    func start() -> Bool {
        registerCarbonHotkey()
        startTapIfPermitted()
        if !holdToTalkActive { beginPollingForPermission() }
        return true
    }

    func stop() {
        stopTap()
        if let h = carbonHandler { RemoveEventHandler(h) }
        if let k = hotKeyRef { UnregisterEventHotKey(k) }
        carbonHandler = nil
        hotKeyRef = nil
        permissionPoll?.invalidate()
        permissionPoll = nil
    }

    // MARK: - hold to talk (needs Accessibility)

    @discardableResult
    private func startTapIfPermitted() -> Bool {
        guard tap == nil else { return true }
        guard AXIsProcessTrusted() else { return false }

        let mask = CGEventMask(1 << CGEventType.flagsChanged.rawValue)
            | CGEventMask(1 << CGEventType.keyDown.rawValue)
            | CGEventMask(1 << CGEventType.tapDisabledByTimeout.rawValue)
            | CGEventMask(1 << CGEventType.tapDisabledByUserInput.rawValue)

        let callback: CGEventTapCallBack = { _, type, event, userInfo in
            guard let userInfo else { return Unmanaged.passUnretained(event) }
            let m = Unmanaged<HotkeyMonitor>.fromOpaque(userInfo).takeUnretainedValue()
            switch type {
            case .flagsChanged:
                m.handleFlags(event.flags)
            case .keyDown:
                m.handleKeyDown()
            case .tapDisabledByTimeout, .tapDisabledByUserInput:
                if let t = m.tap { CGEvent.tapEnable(tap: t, enable: true) }
            default:
                break
            }
            return Unmanaged.passUnretained(event)
        }

        guard let created = CGEvent.tapCreate(
            tap: .cgSessionEventTap, place: .headInsertEventTap,
            options: .listenOnly, eventsOfInterest: mask,
            callback: callback,
            userInfo: Unmanaged.passUnretained(self).toOpaque()) else { return false }

        tap = created
        runLoopSource = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, created, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), runLoopSource, .commonModes)
        CGEvent.tapEnable(tap: created, enable: true)
        trace?("tap created and enabled")
        holdToTalkActive = true
        onModeChange?(true)
        return true
    }

    private func stopTap() {
        if let t = tap { CGEvent.tapEnable(tap: t, enable: false) }
        if let s = runLoopSource {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), s, .commonModes)
        }
        tap = nil
        runLoopSource = nil
        holdToTalkActive = false
    }

    /// Set by the app so raw tap traffic can be observed from outside.
    var trace: ((String) -> Void)?

    private func handleFlags(_ flags: CGEventFlags) {
        let held = flags.contains(.maskControl) && flags.contains(.maskAlternate)
        trace?("flags ctrl=\(flags.contains(.maskControl)) alt=\(flags.contains(.maskAlternate)) held=\(held) was=\(isHeld)")
        guard held != isHeld else { return }
        isHeld = held
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            if held {
                // Arm, don't fire: only a chord that survives the threshold
                // with no other key is a request to talk.
                self.pressFired = false
                self.pendingPress?.cancel()
                let work = DispatchWorkItem { [weak self] in
                    guard let self, self.isHeld else { return }
                    self.pressFired = true
                    self.pressedAt = Date()
                    self.onPress?()
                }
                self.pendingPress = work
                DispatchQueue.main.asyncAfter(deadline: .now() + self.holdThreshold,
                                              execute: work)
            } else {
                self.pendingPress?.cancel()
                self.pendingPress = nil
                if self.pressFired {
                    self.pressFired = false
                    self.onRelease?()
                }
            }
        }
    }

    /// Any ordinary key while the chord is still being timed means it is part
    /// of a shortcut, not a request to talk.
    private func handleKeyDown() {
        guard isHeld else { return }
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            if self.pressFired {
                // Fired already: a key within the next moment still means a
                // shortcut (someone pressed the modifiers a beat early).
                if Date().timeIntervalSince(self.pressedAt) < 0.9 {
                    self.pressFired = false
                    self.onCancel?()
                }
                return
            }
            self.pendingPress?.cancel()
            self.pendingPress = nil
        }
    }

    /// Accessibility can be granted while we are running; pick it up without
    /// making the user restart the app.
    private func beginPollingForPermission() {
        permissionPoll?.invalidate()
        permissionPoll = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) {
            [weak self] t in
            guard let self else { t.invalidate(); return }
            if self.startTapIfPermitted() {
                t.invalidate()
                self.permissionPoll = nil
            }
        }
    }

    // MARK: - ⌃⌥Space toggle (no permission required)

    private func registerCarbonHotkey() {
        guard hotKeyRef == nil else { return }
        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                      eventKind: UInt32(kEventHotKeyPressed))
        let handler: EventHandlerUPP = { _, event, userInfo in
            guard let userInfo, let event else { return noErr }
            let m = Unmanaged<HotkeyMonitor>.fromOpaque(userInfo).takeUnretainedValue()
            var hkID = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject),
                              EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &hkID)
            guard hkID.id == 1 else { return noErr }
            DispatchQueue.main.async {
                // A private flag fell out of step whenever listening ended
                // some other way, and the next press became a silent "release".
                m.toggledOn = !(m.isListening?() ?? m.toggledOn)
                m.toggledOn ? m.onPress?() : m.onRelease?()
            }
            return noErr
        }
        InstallEventHandler(GetApplicationEventTarget(), handler, 1, &eventType,
                            Unmanaged.passUnretained(self).toOpaque(), &carbonHandler)

        let id = EventHotKeyID(signature: OSType(0x4A525653), id: 1)   // 'JRVS'
        let status = RegisterEventHotKey(UInt32(kVK_Space),
                                         UInt32(controlKey | optionKey),
                                         id, GetApplicationEventTarget(), 0,
                                         &hotKeyRef)
        trace?("carbon register status=\(status) ref=\(hotKeyRef != nil)")
    }

    // MARK: - permission helpers

    static var hasAccessibilityPermission: Bool { AXIsProcessTrusted() }

    static func promptForAccessibility() {
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue()
        _ = AXIsProcessTrustedWithOptions([key: true] as CFDictionary)
    }
}
