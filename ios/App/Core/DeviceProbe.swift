import AVFoundation
import CoreMedia
import Foundation
import UIKit

/// 기기 진단.
///
/// ★ 카메라 권한 없이도 `AVCaptureDevice.formats` 를 읽을 수 있습니다.
/// 세션을 열지 않고 메타데이터만 보는 것이므로, 권한 팝업 전에도 조회됩니다.
/// (안드로이드의 CameraCharacteristics 와 같은 성질)
///
/// 여기서 답을 얻어야 하는 질문들:
///   1. ★★ 카메라 타임스탬프 시계와 우리 동기 시계가 같은 도메인인가  <- 프로젝트 전체의 전제
///   2. 1080p 60fps 가 실제로 되는가
///   3. 노출을 1/500초 이하로 직접 지정할 수 있는가 (.custom 지원)
///   4. 초점을 고정할 수 있는가 (.locked 지원) <- 캘리브레이션에 결정적
///   5. 화이트밸런스를 고정할 수 있는가
///   6. 초광각 카메라가 있는가 (광각의 OIS 가 문제될 때 도피처)
///   7. 안정화를 끌 수 있는가
enum DeviceProbe {

    // MARK: - 판정

    enum Status: String {
        case pass = "통과"
        case warn = "주의"
        case fail = "불가"
        case unknown = "불명"
    }

    struct Check: Identifiable {
        let id = UUID()
        let name: String
        let status: Status
        let detail: String
    }

    // MARK: - ★★ 1. 시계 도메인 검증 (가장 중요)

    struct ClockFacts {
        /// 우리가 동기와 사이드카에 쓰는 시계
        let uptimeRawNs: Int64
        /// AVFoundation 이 프레임에 도장 찍는 시계 (CMClockGetHostTimeClock)
        let hostClockNs: Int64
        /// 두 시계의 차이. **0 에 가까워야 합니다.**
        let deltaNs: Int64
        /// 절전에도 흐르는 시계
        let monotonicRawNs: Int64
        /// monotonicRaw - uptimeRaw = 기기가 절전에 머문 누적 시간
        let sleptNs: Int64

        var sameDomain: Bool { abs(deltaNs) < 5 * NS.perMilli }
    }

    static func clockFacts() -> ClockFacts {
        // 두 시계를 최대한 붙여서 읽습니다. 사이에 코드가 끼면 그만큼 차이로 보입니다.
        let hostTime = CMClockGetTime(CMClockGetHostTimeClock())
        let uptime = MonotonicClock.nowNs()
        let mono = MonotonicClock.realtimeNs()

        // CMTime -> 나노초. timescale 을 1e9 로 변환해서 정수로 얻습니다.
        let hostNs: Int64
        if hostTime.isValid {
            let converted = CMTimeConvertScale(hostTime,
                                               timescale: CMTimeScale(NS.perSecond),
                                               method: .roundHalfAwayFromZero)
            hostNs = converted.value
        } else {
            hostNs = 0
        }

        return ClockFacts(
            uptimeRawNs: uptime,
            hostClockNs: hostNs,
            deltaNs: hostNs - uptime,
            monotonicRawNs: mono,
            sleptNs: mono - uptime)
    }

    // MARK: - 2. 카메라

    struct FormatInfo {
        let width: Int32
        let height: Int32
        let minFrameRate: Double
        let maxFrameRate: Double
        let minExposureNs: Int64
        let maxExposureNs: Int64
        let minISO: Float
        let maxISO: Float
        let supportsStabilizationOff: Bool
        let supportsStabilizationStandard: Bool
        let supportsStabilizationCinematic: Bool
        let isBinned: Bool
        let fovDegrees: Float

        var label: String { "\(width)x\(height)" }
        var is1080p: Bool { (width == 1920 && height == 1080) || (width == 1080 && height == 1920) }
        var is4K: Bool { width >= 3840 || height >= 3840 }
        var supports60: Bool { maxFrameRate >= 59.0 }
    }

    struct CameraInfo: Identifiable {
        let id: String
        let name: String
        let deviceTypeRaw: String
        let position: String
        let isUltraWide: Bool
        let hasOISHardware: Bool?      // nil = 알 수 없음 (공개 API 없음)
        let supportsCustomExposure: Bool
        let supportsLockedFocus: Bool
        let supportsLockedWhiteBalance: Bool
        let supportsAutoFocusSystemPhaseDetect: Bool
        let activeFormatLabel: String
        let formats: [FormatInfo]
        let checks: [Check]

        /// 1080p 60fps 이상을 지원하는 포맷들
        var best1080p60: [FormatInfo] { formats.filter { $0.is1080p && $0.supports60 } }
        var best4K60: [FormatInfo] { formats.filter { $0.is4K && $0.supports60 } }
        var maxFrameRateAny: Double { formats.map(\.maxFrameRate).max() ?? 0 }
    }

    static func cameras() -> [CameraInfo] {
        let types: [AVCaptureDevice.DeviceType] = [
            .builtInWideAngleCamera,
            .builtInUltraWideCamera,
            .builtInTelephotoCamera,
            .builtInDualCamera,
            .builtInDualWideCamera,
            .builtInTripleCamera
        ]
        let session = AVCaptureDevice.DiscoverySession(
            deviceTypes: types,
            mediaType: .video,
            position: .unspecified)

        return session.devices.map { describe($0) }
    }

    private static func describe(_ d: AVCaptureDevice) -> CameraInfo {
        let isUltraWide = (d.deviceType == .builtInUltraWideCamera)

        // 아이폰 11 계열: 광각(f/1.8)에 OIS 있음, 초광각(f/2.4)에 OIS 없음.
        // AVFoundation 에 "OIS 하드웨어가 있는가"를 묻는 공개 API 가 없어서
        // 렌즈 종류로 추정합니다. 추정임을 명시합니다.
        let oisGuess: Bool?
        switch d.deviceType {
        case .builtInUltraWideCamera: oisGuess = false
        case .builtInWideAngleCamera, .builtInTelephotoCamera: oisGuess = true
        default: oisGuess = nil
        }

        let formats: [FormatInfo] = d.formats.map { f in
            let dims = CMVideoFormatDescriptionGetDimensions(f.formatDescription)
            let ranges = f.videoSupportedFrameRateRanges
            return FormatInfo(
                width: dims.width,
                height: dims.height,
                minFrameRate: ranges.map(\.minFrameRate).min() ?? 0,
                maxFrameRate: ranges.map(\.maxFrameRate).max() ?? 0,
                minExposureNs: cmTimeToNs(f.minExposureDuration),
                maxExposureNs: cmTimeToNs(f.maxExposureDuration),
                minISO: f.minISO,
                maxISO: f.maxISO,
                supportsStabilizationOff: f.isVideoStabilizationModeSupported(.off),
                supportsStabilizationStandard: f.isVideoStabilizationModeSupported(.standard),
                supportsStabilizationCinematic: f.isVideoStabilizationModeSupported(.cinematic),
                isBinned: f.isVideoBinned,
                fovDegrees: f.videoFieldOfView)
        }

        let activeDims = CMVideoFormatDescriptionGetDimensions(d.activeFormat.formatDescription)
        let activeLabel = "\(activeDims.width)x\(activeDims.height) "
            + String(format: "@%.0ffps",
                     d.activeFormat.videoSupportedFrameRateRanges.map(\.maxFrameRate).max() ?? 0)

        let customExposure = d.isExposureModeSupported(.custom)
        let lockedFocus = d.isFocusModeSupported(.locked)
        // 케이스 이름은 .locked 입니다 (.lockedWhiteBalance 가 아님)
        let lockedWB = d.isWhiteBalanceModeSupported(.locked)

        // ★ 고정초점 렌즈 판별 (2026-09-24 실측으로 알게 된 것)
        //
        // 아이폰 11 의 초광각과 전면 카메라는 자동초점 기구가 **없는** 고정초점입니다.
        // (초광각 AF 는 아이폰 13 Pro, 전면 AF 는 14 부터)
        // 그래서 isFocusModeSupported(.locked) 가 false 를 돌려줍니다 —
        // "잠글 초점 기구가 없어서" 입니다.
        //
        // 처음엔 이걸 [불가] 로 판정했는데 해석이 거꾸로였습니다.
        // 고정초점은 초점거리가 **물리적으로** 변할 수 없으므로
        // 캘리브레이션 안정성 면에서 '잠글 수 있는 AF'보다 오히려 낫습니다.
        //
        // 구분법: 어떤 초점 모드도 지원하지 않으면 초점 기구가 없는 것입니다.
        let anyFocusMode = d.isFocusModeSupported(.locked)
            || d.isFocusModeSupported(.autoFocus)
            || d.isFocusModeSupported(.continuousAutoFocus)
        let isFixedFocus = !anyFocusMode

        // ★ 합성(가상) 카메라 판별 — 이건 절대 쓰면 안 됩니다.
        //
        // builtInDualWideCamera / DualCamera / TripleCamera 는 물리 렌즈가 아니라
        // 여러 렌즈를 **자동 전환**하는 가상 장치입니다. 촬영 중 렌즈가 바뀌면
        // 초점거리·화각·왜곡이 통째로 바뀌어 캘리브레이션이 무의미해집니다.
        // OIS 보다 훨씬 심각합니다.
        let isComposite: Bool = {
            switch d.deviceType {
            case .builtInDualCamera, .builtInDualWideCamera, .builtInTripleCamera:
                return true
            default:
                return false
            }
        }()

        // ── 판정 ──
        var checks: [Check] = []

        // 합성 카메라는 다른 항목을 볼 필요도 없이 탈락입니다. 맨 위에 둡니다.
        if isComposite {
            checks.append(Check(
                name: "★ 사용 금지 — 합성(가상) 카메라",
                status: .fail,
                detail: "여러 렌즈를 자동 전환하는 가상 장치입니다. 촬영 중 렌즈가 바뀌면 "
                    + "초점거리·화각·왜곡이 통째로 바뀌어 캘리브레이션이 파괴됩니다. "
                    + "개별 물리 카메라(광각 또는 초광각)를 쓰세요."))
        }

        let p1080 = formats.filter { $0.is1080p && $0.supports60 }
        checks.append(Check(
            name: "1080p 60fps",
            status: p1080.isEmpty ? .fail : .pass,
            detail: p1080.isEmpty
                ? "지원 포맷 없음. 이 카메라의 최대 = \(String(format: "%.0f", formats.map(\.maxFrameRate).max() ?? 0))fps"
                : "\(p1080.count)개 포맷이 지원. 최대 \(String(format: "%.0f", p1080.map(\.maxFrameRate).max() ?? 0))fps"))

        let p4k = formats.filter { $0.is4K && $0.supports60 }
        checks.append(Check(
            name: "4K 60fps",
            status: p4k.isEmpty ? .warn : .pass,
            detail: p4k.isEmpty ? "없음 (1080p60 이면 충분합니다)" : "\(p4k.count)개 포맷"))

        // ★ 고프레임 포맷의 화각 크롭 경고 (2026-09-24 실측으로 발견)
        //
        // 아이폰 11 후면 광각에서:
        //   1080p  60fps -> 화각 69.7도  (정상)
        //   1080p 120fps -> 화각 38.4도  (★ 반토막. 피험자가 프레임을 벗어납니다)
        //   1080p 240fps -> 화각 69.7도 이지만 binned (화질 저하)
        // 고프레임이 공짜가 아니라는 사실을 화면에서 바로 보이게 합니다.
        let fov60 = formats.filter { $0.is1080p && $0.maxFrameRate >= 59 && $0.maxFrameRate < 61 }
            .map(\.fovDegrees).max() ?? 0
        let fastFormats = formats.filter { $0.is1080p && $0.maxFrameRate >= 100 }
        if !fastFormats.isEmpty, fov60 > 0 {
            let worstFov = fastFormats.map(\.fovDegrees).min() ?? 0
            let cropped = worstFov < fov60 - 5
            checks.append(Check(
                name: "고프레임(100fps+) 화각 크롭",
                status: cropped ? .warn : .pass,
                detail: cropped
                    ? String(format: "★ 크롭 있음. 60fps 에서 %.1f도 -> 고프레임에서 최소 %.1f도. "
                             + "고프레임을 쓰면 피험자가 프레임을 벗어날 수 있습니다. "
                             + "1080p60 을 권합니다", fov60, worstFov)
                    : String(format: "크롭 없음 (60fps %.1f도, 고프레임 최소 %.1f도)",
                             fov60, worstFov)))
        }

        checks.append(Check(
            name: "셔터 직접 지정 (.custom)",
            status: customExposure ? .pass : .fail,
            detail: customExposure
                ? "지원. 1/500초 이하로 고정 가능 -> 모션블러 억제"
                : "미지원. AE 잠금과 밝은 조명으로 대응해야 합니다"))

        // 1/500초 = 2,000,000 ns
        let shortest = formats.map(\.minExposureNs).min() ?? 0
        checks.append(Check(
            name: "최단 노출시간",
            status: shortest > 0 && shortest <= 2_000_000 ? .pass : .warn,
            detail: shortest > 0
                ? "\(shortest) ns = 1/\(Int(Double(NS.perSecond) / Double(shortest)))초"
                : "미보고"))

        // 초점: 고정초점 / 잠금가능 / 잠금불가 세 갈래로 판정합니다.
        // 앞서 고정초점을 [불가] 로 잘못 판정했던 부분을 바로잡은 것입니다.
        if isFixedFocus {
            checks.append(Check(
                name: "초점 (고정초점 렌즈)",
                status: .pass,
                detail: "자동초점 기구가 없는 고정초점입니다. 초점거리가 물리적으로 변할 수 "
                    + "없으므로 캘리브레이션 안정성이 최상입니다. "
                    + "(아이폰 11 의 초광각·전면이 여기 해당)"))
        } else if lockedFocus {
            checks.append(Check(
                name: "초점 고정 (.locked)",
                status: .pass,
                detail: "AF 를 잠글 수 있습니다. 촬영 전에 잠그면 초점거리가 고정됩니다"))
        } else {
            checks.append(Check(
                name: "초점 고정 (.locked)",
                status: .fail,
                detail: "AF 는 있는데 잠글 수 없습니다. 초점이 변하면 내부 파라미터가 변해 "
                    + "캘리브레이션이 무의미해집니다"))
        }

        checks.append(Check(
            name: "화이트밸런스 고정",
            status: lockedWB ? .pass : .warn,
            detail: lockedWB ? "지원" : "미지원. 색이 변하면 2D 검출이 흔들립니다"))

        let offOK = formats.allSatisfy { $0.supportsStabilizationOff }
        checks.append(Check(
            name: "안정화(EIS) 끄기",
            status: offOK ? .pass : .warn,
            detail: offOK
                ? "모든 포맷에서 .off 지원. 기본값도 .off 입니다"
                : "일부 포맷이 .off 를 지원하지 않습니다"))

        checks.append(Check(
            name: "하드웨어 OIS",
            status: isUltraWide ? .pass : .warn,
            detail: isUltraWide
                ? "초광각은 OIS 미탑재 -> 내부 파라미터가 안 변합니다. 캘리브레이션에 유리"
                : "OIS 탑재 추정. 끄는 공개 API 가 없습니다. "
                  + "삼각대 고정 시 렌즈가 거의 안 움직이지만, 재투영오차가 시간에 따라 "
                  + "배회하는지 실측으로 확인해야 합니다"))

        return CameraInfo(
            id: d.uniqueID,
            name: d.localizedName,
            deviceTypeRaw: d.deviceType.rawValue,
            position: positionName(d.position),
            isUltraWide: isUltraWide,
            hasOISHardware: oisGuess,
            supportsCustomExposure: customExposure,
            supportsLockedFocus: lockedFocus,
            supportsLockedWhiteBalance: lockedWB,
            supportsAutoFocusSystemPhaseDetect:
                d.activeFormat.autoFocusSystem == .phaseDetection,
            activeFormatLabel: activeLabel,
            formats: formats,
            checks: checks)
    }

    private static func cmTimeToNs(_ t: CMTime) -> Int64 {
        guard t.isValid, t.timescale != 0 else { return 0 }
        let c = CMTimeConvertScale(t, timescale: CMTimeScale(NS.perSecond),
                                   method: .roundHalfAwayFromZero)
        return c.value
    }

    private static func positionName(_ p: AVCaptureDevice.Position) -> String {
        switch p {
        case .front: return "전면"
        case .back: return "후면"
        case .unspecified: return "미지정"
        @unknown default: return "알수없음"
        }
    }

    // MARK: - 3. 기기 상태

    struct SystemFacts {
        let thermalState: String
        let batteryLevel: Float
        let batteryState: String
        let lowPowerMode: Bool
        let freeDiskBytes: Int64
        let cameraAuthorized: String
    }

    static func systemFacts() -> SystemFacts {
        UIDevice.current.isBatteryMonitoringEnabled = true

        let thermal: String
        switch ProcessInfo.processInfo.thermalState {
        case .nominal: thermal = "nominal (정상)"
        case .fair: thermal = "fair (약간 더움)"
        case .serious: thermal = "serious (경고 — 프레임 드롭 위험)"
        case .critical: thermal = "critical (심각 — 촬영 불가)"
        @unknown default: thermal = "알수없음"
        }

        let bState: String
        switch UIDevice.current.batteryState {
        case .charging: bState = "충전 중"
        case .full: bState = "완충"
        case .unplugged: bState = "배터리"
        case .unknown: bState = "알수없음"
        @unknown default: bState = "알수없음"
        }

        var free: Int64 = 0
        if let v = try? URL(fileURLWithPath: NSHomeDirectory())
            .resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]),
           let c = v.volumeAvailableCapacityForImportantUsage {
            free = Int64(c)
        }

        let auth: String
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized: auth = "허용됨"
        case .denied: auth = "거부됨"
        case .restricted: auth = "제한됨"
        case .notDetermined: auth = "아직 안 물어봄 (정상 — 진단에는 권한이 필요 없습니다)"
        @unknown default: auth = "알수없음"
        }

        return SystemFacts(
            thermalState: thermal,
            batteryLevel: UIDevice.current.batteryLevel,
            batteryState: bState,
            lowPowerMode: ProcessInfo.processInfo.isLowPowerModeEnabled,
            freeDiskBytes: free,
            cameraAuthorized: auth)
    }

    // MARK: - 텍스트 리포트 (로그 파일 머리말)

    static func reportText() -> String {
        let fmt = DateFormatter()
        fmt.dateFormat = "yyyy-MM-dd HH:mm:ss.SSS"
        fmt.locale = Locale(identifier: "en_US_POSIX")

        var s = ""
        s += "========================================================\n"
        s += " MocapSync 기기 진단 리포트 (iOS)\n"
        s += " 생성시각: \(fmt.string(from: Date()))\n"
        s += "========================================================\n\n"

        s += "[앱]\n"
        s += "  bundleId      = \(BuildInfo.bundleId)\n"
        s += "  version       = \(BuildInfo.versionName) (build \(BuildInfo.buildNumber))\n"
        // ★ 커밋 해시. 새 IPA 가 실제로 깔렸는지 이 줄로 판정합니다.
        s += "  커밋          = \(BuildInfo.gitCommit)\n\n"

        s += "[기기]\n"
        s += "  모델식별자    = \(BuildInfo.deviceModelIdentifier)\n"
        s += "  모델          = \(BuildInfo.deviceFriendlyName)\n"
        s += "  iOS           = \(BuildInfo.osVersion)\n"
        let sys = systemFacts()
        s += "  온도상태      = \(sys.thermalState)\n"
        s += "  배터리        = \(Int(sys.batteryLevel * 100))% (\(sys.batteryState))\n"
        s += "  저전력모드    = \(sys.lowPowerMode ? "켜짐 (★ 성능 제한됨. 끄세요)" : "꺼짐")\n"
        s += "  여유공간      = \(sys.freeDiskBytes / 1_000_000_000) GB\n"
        s += "  카메라권한    = \(sys.cameraAuthorized)\n\n"

        let c = clockFacts()
        s += "[★★ 시계 도메인 검증 — 프로젝트 전체의 전제]\n"
        s += "  CLOCK_UPTIME_RAW        = \(c.uptimeRawNs) ns   <- 동기/사이드카에 사용\n"
        s += "  CMClockGetHostTimeClock = \(c.hostClockNs) ns   <- 프레임 타임스탬프 기준\n"
        s += String(format: "  차이                    = %d ns (%.6f ms)\n",
                    c.deltaNs, Double(c.deltaNs) / Double(NS.perMilli))
        s += "  판정                    = \(c.sameDomain ? "같은 도메인 ✔ (변환 불필요)" : "★ 다른 도메인! 설계 재검토 필요")\n"
        s += "  CLOCK_MONOTONIC_RAW     = \(c.monotonicRawNs) ns\n"
        s += String(format: "  누적 절전시간           = %.3f 초 (monotonic - uptime)\n",
                    Double(c.sleptNs) / Double(NS.perSecond))
        s += "\n"

        let cams = cameras()
        s += "[카메라 \(cams.count)대]\n\n"
        for cam in cams {
            s += "---- \(cam.name) (\(cam.position)) ----\n"
            s += "  uniqueID      = \(cam.id)\n"
            s += "  deviceType    = \(cam.deviceTypeRaw)\n"
            s += "  현재 포맷     = \(cam.activeFormatLabel)\n"
            s += "  포맷 개수     = \(cam.formats.count)\n"
            s += "  .custom 노출  = \(cam.supportsCustomExposure ? "예" : "아니오")\n"
            s += "  .locked 초점  = \(cam.supportsLockedFocus ? "예" : "아니오")\n"
            s += "  WB 고정       = \(cam.supportsLockedWhiteBalance ? "예" : "아니오")\n"
            s += "  OIS(추정)     = \(cam.hasOISHardware.map { $0 ? "탑재" : "미탑재" } ?? "불명")\n"
            s += "  판정:\n"
            for ch in cam.checks {
                s += "    [\(ch.status.rawValue)] \(ch.name) :: \(ch.detail)\n"
            }
            // 60fps 이상 포맷만 나열 (전체를 다 찍으면 너무 깁니다)
            let fast = cam.formats.filter { $0.supports60 }
                .sorted { ($0.width * $0.height) > ($1.width * $1.height) }
            s += "  60fps 이상 포맷 (\(fast.count)개):\n"
            for f in fast.prefix(20) {
                // String(format:) 에 %s + C문자열을 섞으면 Swift 에서 불안정합니다.
                // 문자열 패딩은 Swift 로 직접 처리합니다.
                let res = f.label.padding(toLength: 11, withPad: " ", startingAt: 0)
                let fps = String(format: "%5.0f-%5.0f", f.minFrameRate, f.maxFrameRate)
                let iso = String(format: "%4.0f-%5.0f", f.minISO, f.maxISO)
                let fov = String(format: "%.1f", f.fovDegrees)
                let stab = "\(f.supportsStabilizationOff ? "O" : "X")"
                    + "/\(f.supportsStabilizationStandard ? "O" : "X")"
                    + "/\(f.supportsStabilizationCinematic ? "O" : "X")"
                s += "    \(res) \(fps)fps  노출 \(f.minExposureNs)~\(f.maxExposureNs) ns"
                s += "  ISO \(iso)  FOV \(fov)도  안정화(off/std/cine)=\(stab)"
                s += f.isBinned ? "  [binned]\n" : "\n"
            }
            if fast.count > 20 { s += "    ... 이하 생략\n" }
            s += "\n"
        }

        if cams.isEmpty {
            s += "  카메라를 하나도 찾지 못했습니다. 시뮬레이터에서 실행한 경우 정상입니다.\n\n"
        }

        return s
    }
}
