// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "SaharaMobileCore",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
    ],
    products: [
        .library(
            name: "SaharaMobileCore",
            targets: ["SaharaMobileCore"]
        ),
    ],
    targets: [
        .target(
            name: "SaharaMobileCore"
        ),
        .testTarget(
            name: "SaharaMobileCoreTests",
            dependencies: ["SaharaMobileCore"]
        ),
    ]
)
