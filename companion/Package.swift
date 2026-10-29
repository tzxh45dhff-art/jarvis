// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "JarvisCompanion",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "JarvisCompanion",
            path: "Sources/JarvisCompanion",
            swiftSettings: [.unsafeFlags(["-suppress-warnings"])]
        )
    ]
)
