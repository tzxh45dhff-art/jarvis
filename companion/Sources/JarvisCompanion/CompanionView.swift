import SwiftUI

enum CompanionState: Equatable {
    case idle
    case listening
    case thinking
    case speaking
    case pointing(String)

    var tint: Color {
        switch self {
        case .idle:      return Color(red: 0.37, green: 0.92, blue: 0.83)
        case .listening: return Color(red: 0.99, green: 0.75, blue: 0.14)
        case .thinking:  return Color(red: 0.55, green: 0.63, blue: 1.00)
        case .speaking:  return Color(red: 0.29, green: 0.87, blue: 0.50)
        case .pointing:  return Color(red: 0.37, green: 0.92, blue: 0.83)
        }
    }
}

/// An arrow-shaped cursor, drawn in the classic macOS pointer silhouette.
private struct PointerShape: Shape {
    /// The tip, as a fraction of the frame. Shared with the aiming maths so the
    /// point that touches a target is exactly the point that is drawn there.
    static let tip = CGPoint(x: 0.06, y: 0.02)

    func path(in rect: CGRect) -> Path {
        let w = rect.width, h = rect.height
        // A stubbier, rounder pointer than the system one — shorter tail and a
        // wider head, which reads as a friendly little character at small sizes
        // instead of a second system cursor competing with the real one.
        var p = Path()
        p.move(to: CGPoint(x: Self.tip.x * w, y: Self.tip.y * h))
        p.addLine(to: CGPoint(x: 0.06 * w, y: 0.70 * h))
        p.addQuadCurve(to: CGPoint(x: 0.30 * w, y: 0.58 * h),
                       control: CGPoint(x: 0.13 * w, y: 0.66 * h))
        p.addLine(to: CGPoint(x: 0.44 * w, y: 0.97 * h))
        p.addQuadCurve(to: CGPoint(x: 0.62 * w, y: 0.90 * h),
                       control: CGPoint(x: 0.56 * w, y: 0.99 * h))
        p.addLine(to: CGPoint(x: 0.48 * w, y: 0.52 * h))
        p.addQuadCurve(to: CGPoint(x: 0.72 * w, y: 0.46 * h),
                       control: CGPoint(x: 0.66 * w, y: 0.50 * h))
        p.closeSubpath()
        return p
    }
}

/// The companion: a second pointer that trails just behind yours.
///
/// Deliberately small and nearly flat — it shares the screen with the real
/// cursor, so anything larger or glossier competes with it instead of reading
/// as a helper. Colour alone carries the state.
struct CompanionOrb: View {
    let state: CompanionState
    let audioLevel: CGFloat
    @State private var pulse = false

    private var scale: CGFloat {
        switch state {
        case .listening: return 1.0 + min(audioLevel, 1) * 0.16
        default:         return 1.0
        }
    }

    static let pointerSize = CGSize(width: 11, height: 15)

    /// The orb's layout size. Fixed so its centre is always the centre of this
    /// frame — the halo and rings change size with state, the anchor must not.
    static let frameSize = CGSize(width: 24, height: 24)

    /// Where the pointer's tip sits relative to the orb's centre, in AppKit
    /// screen orientation (y up). To make the tip touch a target, put the
    /// centre at `target - tipOffset`.
    static let tipOffset = CGVector(
        dx: PointerShape.tip.x * pointerSize.width - pointerSize.width / 2,
        dy: pointerSize.height / 2 - PointerShape.tip.y * pointerSize.height)

    var body: some View {
        ZStack {
            // A faint halo, enough to stay visible on a busy background.
            Circle()
                .fill(state.tint.opacity(0.18))
                .frame(width: 20, height: 20)
                .blur(radius: 5)
                .offset(x: 2, y: 3)

            if case .listening = state {
                Circle()
                    .strokeBorder(state.tint.opacity(0.45), lineWidth: 1.5)
                    .frame(width: 24, height: 24)
                    .scaleEffect(pulse ? 1.25 : 0.9)
                    .opacity(pulse ? 0.0 : 0.7)
                    .offset(x: 2, y: 3)
                    .animation(.easeOut(duration: 0.9).repeatForever(autoreverses: false),
                               value: pulse)
            }

            PointerShape()
                .fill(state.tint)
                .overlay(PointerShape().stroke(.white.opacity(0.9), lineWidth: 1.1))
                .frame(width: Self.pointerSize.width, height: Self.pointerSize.height)
                .shadow(color: .black.opacity(0.3), radius: 1.5, y: 1)

            if case .thinking = state {
                Circle()
                    .trim(from: 0, to: 0.3)
                    .stroke(state.tint, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                    .frame(width: 19, height: 19)
                    .rotationEffect(.degrees(pulse ? 360 : 0))
                    .offset(x: 3, y: 4)
                    .animation(.linear(duration: 0.85).repeatForever(autoreverses: false),
                               value: pulse)
            }
        }
        .scaleEffect(scale)
        .animation(.spring(response: 0.3, dampingFraction: 0.75), value: scale)
        .onAppear { pulse = true }
    }
}

/// Text bubble that sits beside the orb.
struct CompanionBubble: View {
    let text: String
    let tint: Color
    let subdued: Bool

    var body: some View {
        Text(text)
            .font(.system(size: subdued ? 12 : 13.5, weight: subdued ? .regular : .medium,
                          design: .rounded))
            .foregroundStyle(subdued ? .white.opacity(0.72) : .white)
            .lineLimit(6)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: 340, alignment: .leading)
            .padding(.horizontal, 13)
            .padding(.vertical, 9)
            .background(
                RoundedRectangle(cornerRadius: 13, style: .continuous)
                    .fill(Color.black.opacity(0.82))
                    .overlay(
                        RoundedRectangle(cornerRadius: 13, style: .continuous)
                            .strokeBorder(tint.opacity(0.32), lineWidth: 1)
                    )
            )
            .shadow(color: .black.opacity(0.45), radius: 14, y: 4)
            .transition(.opacity.combined(with: .move(edge: .leading)))
    }
}

/// A ring drawn at whatever JARVIS is pointing at.
struct PointingTarget: View {
    let label: String
    let tint: Color
    @State private var pulse = false

    var body: some View {
        ZStack {
            Circle()
                .strokeBorder(tint, lineWidth: 3)
                .frame(width: 46, height: 46)
                .scaleEffect(pulse ? 1.25 : 0.85)
                .opacity(pulse ? 0 : 0.95)
                .animation(.easeOut(duration: 1.1).repeatForever(autoreverses: false),
                           value: pulse)
            Circle()
                .fill(tint.opacity(0.28))
                .frame(width: 20, height: 20)
            if !label.isEmpty {
                Text(label)
                    .font(.system(size: 11, weight: .semibold, design: .rounded))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .background(Capsule().fill(Color.black.opacity(0.8)))
                    .offset(y: 36)
            }
        }
        .onAppear { pulse = true }
    }
}
