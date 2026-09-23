// swift-tools-version:5.9
import PackageDescription

// ─────────────────────────────────────────────────────────────────────────────
// 왜 SwiftPM 패키지를 따로 두는가
//
// 개발자(AI)는 Windows 에서 Swift 를 컴파일할 수 없습니다. 모든 컴파일 오류를
// CI 왕복으로만 잡아야 하므로, CI 가 최대한 빨라야 합니다.
//
// iOS 앱 테스트를 XCTest 로 돌리려면 시뮬레이터를 부팅해야 하고 2~3분이 걸립니다.
// 반면 순수 로직(시각 계산, 규약 인코딩)은 UIKit/AVFoundation 이 필요 없으므로
// **macOS 네이티브로 `swift test`** 하면 수십 초에 끝납니다.
//
// 그래서 순수 로직만 MocapSyncCore 로 분리했습니다.
// 이 소스들은 iOS 앱 타깃에도 그대로 포함됩니다 (XcodeGen 이 같은 파일을 가리킴).
// 패키지 의존성이 아니라 "같은 파일을 두 빌드 시스템이 본다"는 구조입니다.
// -> CI 에서 SPM 의존성 해석 실패 같은 문제가 생기지 않습니다.
//
// MocapSyncCore 에 절대 넣지 말 것: UIKit, SwiftUI, AVFoundation, Network
// ─────────────────────────────────────────────────────────────────────────────

let package = Package(
    name: "MocapSyncCore",
    platforms: [
        .macOS(.v12),
        .iOS(.v16)
    ],
    products: [
        .library(name: "MocapSyncCore", targets: ["MocapSyncCore"])
    ],
    targets: [
        .target(
            name: "MocapSyncCore",
            path: "Sources/MocapSyncCore"
        ),
        .testTarget(
            name: "MocapSyncCoreTests",
            dependencies: ["MocapSyncCore"],
            path: "Tests/MocapSyncCoreTests"
        )
    ]
)
