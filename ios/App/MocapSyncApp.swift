import SwiftUI
import UIKit

@main
struct MocapSyncApp: App {

    init() {
        // 촬영/동기 테스트 중에 화면이 꺼지면 곤란합니다.
        // 또한 CLOCK_UPTIME_RAW 는 절전에 들어가면 멈추므로, 화면을 켜 두는 것이
        // 시계 연속성 유지에도 필요합니다. (docs/PROTOCOL.md §1)
        UIApplication.shared.isIdleTimerDisabled = true

        AppLog.shared.i("App", "앱 시작 v\(BuildInfo.versionName) (build \(BuildInfo.buildNumber))")
        AppLog.shared.i("App", "기기 \(BuildInfo.deviceModelIdentifier) / iOS \(BuildInfo.osVersion)")
        AppLog.shared.i("App", "시계 \(MonotonicClock.name) = \(MonotonicClock.nowNs()) ns")
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .preferredColorScheme(.dark)
        }
    }
}

/// 빌드/기기 식별 정보.
///
/// 실기기 테스트를 사람이 대신 하기 때문에 "지금 어떤 빌드를 깔았는지"를
/// 화면과 로그에서 즉시 확인할 수 있어야 합니다. (안드로이드 버전에서도 같은 이유로
/// git commit 을 화면에 노출했습니다)
enum BuildInfo {
    static let versionName =
        Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
    static let buildNumber =
        Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "?"
    static let bundleId = Bundle.main.bundleIdentifier ?? "?"

    static let osVersion = UIDevice.current.systemVersion

    /// "iPhone12,1" 같은 하드웨어 식별자. UIDevice.model 은 "iPhone" 만 주므로 직접 읽습니다.
    static let deviceModelIdentifier: String = {
        var info = utsname()
        uname(&info)
        let raw = withUnsafePointer(to: &info.machine) { ptr -> String in
            ptr.withMemoryRebound(to: CChar.self, capacity: 1) { String(cString: $0) }
        }
        return raw.isEmpty ? "unknown" : raw
    }()

    /// 사람이 읽는 기기 이름. iPhone12,1 = iPhone 11
    static var deviceFriendlyName: String {
        switch deviceModelIdentifier {
        case "iPhone12,1": return "iPhone 11"
        case "iPhone12,3": return "iPhone 11 Pro"
        case "iPhone12,5": return "iPhone 11 Pro Max"
        case "i386", "x86_64", "arm64":
            return "시뮬레이터"
        default: return deviceModelIdentifier
        }
    }
}
