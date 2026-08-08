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
        .library(
            name: "SaharaMobileApp",
            targets: ["SaharaMobileApp"]
        ),
    ],
    targets: [
        .target(
            name: "SaharaMobileCore"
        ),
        .target(
            name: "SaharaMobileApp",
            dependencies: ["SaharaMobileCore"]
        ),
        .testTarget(
            name: "SaharaMobileCoreTests",
            dependencies: ["SaharaMobileCore"]
        ),
        .testTarget(
            name: "SaharaMobileAppTests",
            dependencies: ["SaharaMobileApp"]
        ),
    ]
)
