import AudioToolbox
import CoreAudio
import Foundation

/// Turns the Mac's sound down while the user is talking to JARVIS, and puts
/// it back exactly where it was when they let go.
///
/// Through Core Audio, not AppleScript: the dip has to land the instant the
/// keys go down. A short fade instead of a jump, so it reads as "the room got
/// quiet" rather than a glitch. Restoring is careful:
///   * only the device that was turned down is turned back up;
///   * if the user changed the volume themselves while holding the keys,
///     their choice wins and nothing is restored;
///   * muted or already-quiet output is left alone entirely.
@MainActor
final class AudioDucker {
    /// Ducked level = current × factor, but never below `floor`.
    static let factor: Float32 = 0.25
    static let floor: Float32 = 0.04
    /// Below this the sound is already quiet enough; leave it.
    static let quietEnough: Float32 = 0.08

    private var device: AudioObjectID = 0
    private var saved: Float32?          // the user's own level, to restore
    private var lastSet: Float32 = -1    // what we last wrote, to spot user changes
    private var ramp: Timer?
    /// Where a restore fade still in flight is heading.
    private var restoringTo: Float32?
    var trace: ((String) -> Void)?

    var isDucked: Bool { saved != nil }

    func duck() {
        guard saved == nil, let dev = Self.defaultOutput(), !Self.isMuted(dev),
              let now = Self.volume(dev) else { return }
        // Pressed again while the last release is still fading back up: the
        // level right now is halfway, not the user's. Saving it would leave
        // the Mac a little quieter after every quick re-press.
        var v = now
        if ramp != nil, let target = restoringTo, dev == device { v = target }
        guard v > Self.quietEnough else { return }
        device = dev
        saved = v
        let target = max(Self.floor, v * Self.factor)
        trace?(String(format: "duck %.2f -> %.2f", v, target))
        fade(from: now, to: target, over: 0.15)
    }

    func restore() {
        guard let original = saved else { return }
        saved = nil
        ramp?.invalidate()
        ramp = nil
        guard let now = Self.volume(device) else { return }
        // A change we did not make means the user set the volume while
        // talking (or the output device changed): keep theirs.
        if lastSet >= 0, abs(now - lastSet) > 0.03 {
            trace?(String(format: "restore skipped: volume was changed to %.2f", now))
            return
        }
        trace?(String(format: "restore %.2f -> %.2f", now, original))
        fade(from: now, to: original, over: 0.2)
        restoringTo = original
    }

    private func fade(from a: Float32, to b: Float32, over seconds: Double) {
        ramp?.invalidate()
        restoringTo = nil
        let steps = 8
        var i = 0
        let dev = device
        let tick = { [weak self] in
            guard let self else { return }
            i += 1
            let t = Float32(i) / Float32(steps)
            let v = a + (b - a) * t
            Self.setVolume(dev, v)
            self.lastSet = v
            if i >= steps { self.ramp?.invalidate(); self.ramp = nil; self.restoringTo = nil }
        }
        tick()
        ramp = Timer.scheduledTimer(withTimeInterval: seconds / Double(steps), repeats: true) { _ in
            MainActor.assumeIsolated { tick() }
        }
    }

    // MARK: Core Audio

    private static func address(_ selector: AudioObjectPropertySelector,
                                scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal)
        -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: scope,
                                   mElement: kAudioObjectPropertyElementMain)
    }

    static func defaultOutput() -> AudioObjectID? {
        var dev = AudioObjectID(0)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        var addr = address(kAudioHardwarePropertyDefaultOutputDevice)
        let err = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr,
                                             0, nil, &size, &dev)
        return err == noErr && dev != 0 ? dev : nil
    }

    static func volume(_ dev: AudioObjectID) -> Float32? {
        var addr = address(kAudioHardwareServiceDeviceProperty_VirtualMainVolume,
                           scope: kAudioDevicePropertyScopeOutput)
        guard AudioObjectHasProperty(dev, &addr) else { return nil }
        var v = Float32(0)
        var size = UInt32(MemoryLayout<Float32>.size)
        return AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &v) == noErr ? v : nil
    }

    static func setVolume(_ dev: AudioObjectID, _ v: Float32) {
        var addr = address(kAudioHardwareServiceDeviceProperty_VirtualMainVolume,
                           scope: kAudioDevicePropertyScopeOutput)
        var settable: DarwinBoolean = false
        guard AudioObjectHasProperty(dev, &addr),
              AudioObjectIsPropertySettable(dev, &addr, &settable) == noErr, settable.boolValue
        else { return }
        var value = min(1, max(0, v))
        AudioObjectSetPropertyData(dev, &addr, 0, nil, UInt32(MemoryLayout<Float32>.size), &value)
    }

    static func isMuted(_ dev: AudioObjectID) -> Bool {
        var addr = address(kAudioDevicePropertyMute, scope: kAudioDevicePropertyScopeOutput)
        guard AudioObjectHasProperty(dev, &addr) else { return false }
        var m = UInt32(0)
        var size = UInt32(MemoryLayout<UInt32>.size)
        return AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &m) == noErr && m != 0
    }
}
