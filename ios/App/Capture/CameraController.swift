import AVFoundation
import UIKit

// ─────────────────────────────────────────────────────────────────────────────
// 카메라 설정.
//
// ★ 이 파일의 목적은 "카메라를 프레임마다 똑같이 만드는 것"입니다.
//
// 3D 복원은 카메라의 내부 파라미터(초점거리, 왜곡, 주점)가 촬영 내내
// **변하지 않는다**고 가정합니다. 캘리브레이션을 한 번 하고 그 값을 계속 쓰니까요.
// iPhone 의 기본 동작은 그 가정을 전부 깨뜨립니다.
//
//   자동초점   -> 초점거리가 변함        -> 캘리브레이션 무효
//   자동노출   -> 밝기가 변함            -> 2D 검출이 흔들림
//   자동 WB    -> 색이 변함              -> 2D 검출이 흔들림
//   안정화     -> 화면을 프레임마다 변형  -> 캘리브레이션 완전 파괴
//   합성 카메라 -> 렌즈가 통째로 바뀜      -> 최악
//
// 그래서 전부 잠급니다. 잠그지 못한 것(OIS)은 사이드카에 기록해서
// 나중에 원인 추적이 되게 합니다.
//
// 측정된 사실은 DESIGN.md §3.10 에 있습니다. 여기서는 그 결론을 실행합니다.
// ─────────────────────────────────────────────────────────────────────────────

/// 카메라를 열고 고정 설정을 적용합니다.
///
/// ★ 스레드 규칙: `configure()` 와 세션 조작은 `sessionQueue` 에서만.
///   AVCaptureSession 은 스레드 안전하지 않고, 메인 스레드에서 만지면
///   UI 가 멈춥니다(세션 시작은 1초 가까이 걸립니다).
/// `@unchecked Sendable`: 세션 조작을 `sessionQueue` 하나로 직렬화해서 보장합니다.
/// 컴파일러가 검사할 수 없는 규칙이므로 위 주석의 스레드 규칙을 지켜야 합니다.
final class CameraController: @unchecked Sendable {

    enum CameraError: LocalizedError {
        case noDevice
        case noFormat(want: String)
        case cannotAddInput
        case cannotAddOutput
        case permissionDenied

        var errorDescription: String? {
            switch self {
            case .noDevice:
                return "후면 광각 카메라를 찾을 수 없습니다."
            case .noFormat(let want):
                return "필요한 포맷을 지원하지 않습니다: \(want)"
            case .cannotAddInput:
                return "카메라 입력을 세션에 붙일 수 없습니다."
            case .cannotAddOutput:
                return "영상 출력을 세션에 붙일 수 없습니다."
            case .permissionDenied:
                return "카메라 권한이 없습니다. 설정 → MocapSync → 카메라를 켜 주세요."
            }
        }
    }

    /// 적용된 설정의 기록. 사이드카에 그대로 들어갑니다.
    struct Applied {
        var deviceType: String = ""
        var width: Int = 0
        var height: Int = 0
        var fps: Int = 0
        var fieldOfViewDeg: Double = 0
        var isBinned: Bool = false
        var exposureDurationNs: Int64 = 0
        var iso: Double = 0
        var lensPosition: Double = 0
        var focusLocked: Bool = false
        var whiteBalanceLocked: Bool = false
        var exposureLocked: Bool = false
        var stabilization: String = "off"
        /// 초점 기구가 아예 없는 렌즈인가 (초광각·전면).
        /// 이 경우 focusLocked=false 가 **정상**입니다 — 잠글 대상이 없습니다.
        var isFixedFocusLens: Bool = false
        /// 잠그지 못한 것들. 화면과 사이드카에 경고로 띄웁니다.
        var warnings: [String] = []
    }

    // ── 방향 ────────────────────────────────────────────────────────────────
    //
    // ★ 2026-09-25 첫 실기기 촬영에서 발견한 함정
    //
    // 첫 영상이 90도 돌아가 저장됐습니다. 폰을 세로로 들고 찍으셨는데,
    // 후면 카메라 센서의 기준 방향은 **가로**입니다. 우리는 회전 메타데이터를
    // 일부러 넣지 않으므로(픽셀과 좌표계를 캘리브레이션과 맞추려고) 센서
    // 방향 그대로 저장됩니다.
    //
    // 그런데 앱 미리보기(AVCaptureVideoPreviewLayer)는 자동으로 회전해
    // 보여줍니다. 그래서 **화면으로는 정상인데 저장되는 픽셀은 돌아간** 상태가
    // 됩니다. 사람이 눕혀 찍히면 2D 자세 추정 정확도가 크게 떨어집니다
    // (RTMPose 는 똑바로 선 사람으로 학습됨).
    //
    // 회전 보정을 코드로 하지 않는 이유: 픽셀을 돌리면 재인코딩이 필요하고
    // (화질 손실 + 시간), 메타데이터만 넣으면 PC 쪽 도구마다 해석이 달라져
    // 캘리브레이션 좌표계가 어긋날 위험이 있습니다. 촬영 시 바르게 드는 것이
    // 가장 확실합니다. 그래서 **경고로 알립니다.**

    /// 현재 기기 방향이 "사람이 똑바로 찍히는" 방향인가.
    static func isLandscapeNow() -> Bool {
        switch UIDevice.current.orientation {
        case .landscapeLeft, .landscapeRight: return true
        default: return false
        }
    }

    /// 사이드카·화면에 기록할 방향 이름.
    static func orientationName() -> String {
        switch UIDevice.current.orientation {
        case .portrait: return "portrait"
        case .portraitUpsideDown: return "portraitUpsideDown"
        case .landscapeLeft: return "landscapeLeft"
        case .landscapeRight: return "landscapeRight"
        case .faceUp: return "faceUp"
        case .faceDown: return "faceDown"
        default: return "unknown"
        }
    }

    // ── 요구 사양 (DESIGN.md §3.10 의 결론) ──────────────────────────────────
    static let wantWidth = 1920
    static let wantHeight = 1080
    static let wantFps = 60
    /// 1/500초. 모션블러 억제. 설계 문서의 촬영 규칙.
    static let wantExposureNs: Int64 = 2_000_000

    let session = AVCaptureSession()
    let sessionQueue = DispatchQueue(label: "mocapsync.session")
    let videoQueue = DispatchQueue(label: "mocapsync.video", qos: .userInitiated)

    private(set) var device: AVCaptureDevice?
    private(set) var videoOutput: AVCaptureVideoDataOutput?
    private(set) var applied = Applied()

    // MARK: - 권한

    static func permissionStatus() -> AVAuthorizationStatus {
        AVCaptureDevice.authorizationStatus(for: .video)
    }

    static func requestPermission() async -> Bool {
        switch permissionStatus() {
        case .authorized: return true
        case .notDetermined:
            return await AVCaptureDevice.requestAccess(for: .video)
        default: return false
        }
    }

    // MARK: - 구성

    /// 세션을 구성합니다. `sessionQueue` 에서 호출하세요.
    ///
    /// - Parameter delegate: 프레임을 받을 대상. Recorder 가 들어옵니다.
    func configure(delegate: AVCaptureVideoDataOutputSampleBufferDelegate) throws {
        guard CameraController.permissionStatus() == .authorized else {
            throw CameraError.permissionDenied
        }

        session.beginConfiguration()
        defer { session.commitConfiguration() }

        // ★ 프리셋을 쓰지 않습니다. `.inputPriority` 로 두고 activeFormat 을
        //   직접 고릅니다. 프리셋은 iOS 가 임의로 포맷을 바꿀 수 있어서
        //   "내가 고른 포맷"이 보장되지 않습니다.
        session.sessionPreset = .inputPriority

        // ★ 와이드컬러 자동 설정 끄기.
        //   켜져 있으면 iOS 가 색공간을 바꿀 수 있고, 그러면 톤이 변합니다.
        session.automaticallyConfiguresCaptureDeviceForWideColor = false

        // ── 1) 물리 카메라 고르기 ───────────────────────────────────────────
        //
        // ★ 반드시 `builtInWideAngleCamera` 를 이름으로 지정합니다.
        //   `.default(for: .video)` 는 기기에 따라 **합성 카메라**를 줄 수 있고,
        //   합성 카메라는 촬영 중 렌즈를 바꿔 캘리브레이션을 파괴합니다.
        //   (DESIGN.md §3.10 — 사용 금지 판정)
        guard let dev = AVCaptureDevice.default(.builtInWideAngleCamera,
                                                for: .video, position: .back) else {
            throw CameraError.noDevice
        }
        device = dev
        applied.deviceType = dev.deviceType.rawValue
        AppLog.shared.i("Cam", "카메라: \(dev.localizedName) [\(dev.deviceType.rawValue)]")

        // ── 2) 입력 ─────────────────────────────────────────────────────────
        // 기존 입력을 걷어냅니다 (재구성 대비)
        for i in session.inputs { session.removeInput(i) }
        let input = try AVCaptureDeviceInput(device: dev)
        guard session.canAddInput(input) else { throw CameraError.cannotAddInput }
        session.addInput(input)

        // ── 3) 포맷 고르기 ──────────────────────────────────────────────────
        let format = try pickFormat(dev)
        let desc = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
        applied.width = Int(desc.width)
        applied.height = Int(desc.height)
        applied.fps = CameraController.wantFps
        applied.fieldOfViewDeg = Double(format.videoFieldOfView)
        applied.isBinned = format.isVideoBinned

        // ── 4) 출력 ─────────────────────────────────────────────────────────
        for o in session.outputs { session.removeOutput(o) }
        let out = AVCaptureVideoDataOutput()
        // BGRA 로 받지 않습니다. 하드웨어 인코더에 그대로 넘길 수 있는
        // 네이티브 YUV(420f)로 받아야 변환 비용이 0 입니다. 60fps 에서 중요합니다.
        out.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String:
                Int(kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange)
        ]
        // ★ false = 늦은 프레임도 버리지 않습니다.
        //   우리는 모든 프레임이 필요합니다. 못 따라가면 didDrop 으로 세어서
        //   사이드카에 기록합니다. 조용히 사라지는 것보다 셀 수 있는 게 낫습니다.
        out.alwaysDiscardsLateVideoFrames = false
        out.setSampleBufferDelegate(delegate, queue: videoQueue)
        guard session.canAddOutput(out) else { throw CameraError.cannotAddOutput }
        session.addOutput(out)
        videoOutput = out

        // ── 5) 안정화 끄기 ★가장 중요 ───────────────────────────────────────
        //
        // 안정화가 켜져 있으면 프레임마다 화면을 잘라내고 변형합니다.
        // 그러면 내부 파라미터가 프레임마다 달라져 캘리브레이션이 **의미를 잃습니다**.
        // OIS 보다 EIS 가 훨씬 파괴적입니다.
        if let conn = out.connection(with: .video) {
            if conn.isVideoStabilizationSupported {
                conn.preferredVideoStabilizationMode = .off
                applied.stabilization = "off"
            } else {
                applied.stabilization = "off"
            }
            // 회전 보정도 끕니다. 픽셀을 건드리는 모든 것을 배제합니다.
            if conn.isVideoMirroringSupported { conn.isVideoMirrored = false }
        }

        // ── 6) 기기별 고정 설정 ─────────────────────────────────────────────
        try dev.lockForConfiguration()
        defer { dev.unlockForConfiguration() }

        dev.activeFormat = format

        // ★ 프레임레이트를 min=max 로 고정합니다.
        //   한쪽만 지정하면 iOS 가 밝기에 따라 fps 를 **떨어뜨립니다**
        //   (어두우면 노출을 늘리려고 30fps 로 내려감). 그러면 프레임 간격이
        //   불규칙해지고 리샘플러 입력이 나빠집니다.
        let d = CMTime(value: 1, timescale: CMTimeScale(CameraController.wantFps))
        dev.activeVideoMinFrameDuration = d
        dev.activeVideoMaxFrameDuration = d

        // HDR 은 톤매핑을 프레임마다 바꿉니다. 끕니다.
        if dev.activeFormat.isVideoHDRSupported {
            dev.automaticallyAdjustsVideoHDREnabled = false
            dev.isVideoHDREnabled = false
        }

        // 색공간 고정 (와이드컬러로 바뀌면 톤이 변합니다)
        if dev.activeFormat.supportedColorSpaces.contains(.sRGB) {
            dev.activeColorSpace = .sRGB
        }

        // 줌 1배 고정. 줌이 걸리면 초점거리가 달라집니다.
        dev.videoZoomFactor = 1.0

        // 저조도 보정은 프레임을 합성합니다. 끕니다.
        if dev.isLowLightBoostSupported {
            dev.automaticallyEnablesLowLightBoostWhenAvailable = false
        }

        AppLog.shared.i("Cam", String(
            format: "포맷 %dx%d @%dfps  화각 %.1f도  binned=%@",
            applied.width, applied.height, applied.fps,
            applied.fieldOfViewDeg, applied.isBinned ? "예" : "아니오"))
    }

    /// 요구 사양에 맞는 포맷을 고릅니다.
    ///
    /// ★ 선택 규칙 (우선순위 순)
    ///   1. 1920x1080 이고 60fps 를 낼 수 있어야 함
    ///   2. **binned 가 아닌 것** — binned 는 센서 픽셀을 묶어 읽어 해상감이 떨어집니다
    ///   3. 화각이 넓은 것 — 피험자가 프레임에 들어와야 합니다
    ///   4. 최단 노출이 짧은 것 — 1/500초를 확보하려면 필요합니다
    ///
    /// 아이폰 11 후면 광각은 1080p60 포맷이 4개(비binned 2 + binned 2)입니다.
    private func pickFormat(_ dev: AVCaptureDevice) throws -> AVCaptureDevice.Format {
        let want = "\(CameraController.wantWidth)x\(CameraController.wantHeight)@\(CameraController.wantFps)fps"

        let candidates = dev.formats.filter { f in
            let dim = CMVideoFormatDescriptionGetDimensions(f.formatDescription)
            guard Int(dim.width) == CameraController.wantWidth,
                  Int(dim.height) == CameraController.wantHeight else { return false }
            // 60fps 를 낼 수 있는지
            return f.videoSupportedFrameRateRanges.contains { r in
                r.maxFrameRate >= Double(CameraController.wantFps) - 0.01
                    && r.minFrameRate <= Double(CameraController.wantFps) + 0.01
            }
        }

        guard !candidates.isEmpty else { throw CameraError.noFormat(want: want) }

        let sorted = candidates.sorted { a, b in
            // binned 가 아닌 것 우선
            if a.isVideoBinned != b.isVideoBinned { return !a.isVideoBinned }
            // 화각 넓은 것 우선
            if abs(a.videoFieldOfView - b.videoFieldOfView) > 0.01 {
                return a.videoFieldOfView > b.videoFieldOfView
            }
            // 최단 노출이 짧은 것 우선
            return CMTimeGetSeconds(a.minExposureDuration)
                 < CMTimeGetSeconds(b.minExposureDuration)
        }

        let chosen = sorted[0]
        AppLog.shared.i("Cam", "1080p60 포맷 \(candidates.count)개 중 선택: "
            + String(format: "화각 %.1f도, binned=%@, 최단노출 %.0f µs",
                     chosen.videoFieldOfView,
                     chosen.isVideoBinned ? "예" : "아니오",
                     CMTimeGetSeconds(chosen.minExposureDuration) * 1e6))
        return chosen
    }

    // MARK: - 잠그기

    /// 노출/초점/화이트밸런스를 잠급니다.
    ///
    /// ★ 반드시 "자동으로 한 번 맞춘 뒤에" 잠가야 합니다.
    ///   세션 시작 직후에 바로 잠그면 초기값(대개 엉뚱한 값)으로 굳어버립니다.
    ///   그래서 호출자가 `session.startRunning()` 후 1~2초 기다린 다음 부릅니다.
    ///
    /// - Parameter targetExposureNs: 목표 셔터 시간. 기본 1/500초.
    func lockSettings(targetExposureNs: Int64 = CameraController.wantExposureNs) throws {
        guard let dev = device else { throw CameraError.noDevice }

        try dev.lockForConfiguration()
        defer { dev.unlockForConfiguration() }

        applied.warnings = []

        // ── 1) 노출 ─────────────────────────────────────────────────────────
        //
        // ★ 여기가 가장 까다롭습니다.
        //
        // 우리는 셔터를 1/500초로 **고정**하고 싶습니다. 모션블러 때문입니다.
        // 그런데 셔터를 짧게 하면 빛이 적게 들어와 어두워집니다.
        // 그 손실을 ISO 로 메워야 합니다.
        //
        // 그래서: 자동노출이 찾아낸 (셔터, ISO) 조합을 읽고, 같은 밝기를
        // 유지하는 새 ISO 를 계산합니다.
        //
        //   노출량 ∝ 셔터시간 × ISO
        //   새 ISO = 기존 ISO × (기존 셔터 / 목표 셔터)
        //
        // 예) 자동이 1/60초·ISO 100 을 골랐다면, 1/500초로 줄이면 빛이 8.3배
        //     줄어드니 ISO 를 833 으로 올려야 같은 밝기입니다.
        //
        // ISO 상한에 걸리면 어두운 영상이 됩니다. 그건 조명을 더 켜야 하는
        // 상황이므로 경고로 알립니다. 셔터를 늘려 몰래 밝게 만들지 않습니다 —
        // 모션블러가 생기면 2D 검출이 망가지고, 그건 조용한 실패입니다.
        let curDur = CMTimeGetSeconds(dev.exposureDuration)
        let curISO = Double(dev.iso)
        let targetSec = Double(targetExposureNs) / 1e9

        let minDur = CMTimeGetSeconds(dev.activeFormat.minExposureDuration)
        let maxDur = CMTimeGetSeconds(dev.activeFormat.maxExposureDuration)
        let clampedTarget = min(max(targetSec, minDur), maxDur)
        if abs(clampedTarget - targetSec) > 1e-9 {
            applied.warnings.append(String(
                format: "셔터 목표 1/%.0f초가 지원 범위를 벗어나 1/%.0f초로 조정됐습니다.",
                1 / targetSec, 1 / clampedTarget))
        }

        if dev.isExposureModeSupported(.custom) {
            var newISO = curISO * (curDur / clampedTarget)
            let minISO = Double(dev.activeFormat.minISO)
            let maxISO = Double(dev.activeFormat.maxISO)
            if newISO > maxISO {
                applied.warnings.append(String(
                    format: "필요한 ISO %.0f 가 상한 %.0f 를 넘습니다. 영상이 어두워집니다 — "
                          + "조명을 더 켜세요. (셔터를 늘리면 모션블러가 생기므로 늘리지 않습니다)",
                    newISO, maxISO))
            }
            newISO = min(max(newISO, minISO), maxISO)

            let dur = CMTime(seconds: clampedTarget, preferredTimescale: 1_000_000)
            // ★ 완료 콜백을 기다리지 않습니다. 잠근 뒤 실제 적용값은
            //   `readBackAppliedValues()` 로 다시 읽습니다.
            dev.setExposureModeCustom(duration: dur, iso: Float(newISO),
                                      completionHandler: nil)
            applied.exposureLocked = true
            AppLog.shared.i("Cam", String(
                format: "노출 고정: 자동값 1/%.0f초·ISO %.0f → 1/%.0f초·ISO %.0f",
                1 / curDur, curISO, 1 / clampedTarget, newISO))
        } else if dev.isExposureModeSupported(.locked) {
            // .custom 을 못 쓰면 최소한 잠그기만 합니다 (합성 카메라 등)
            dev.exposureMode = .locked
            applied.exposureLocked = true
            applied.warnings.append(
                "셔터를 직접 지정할 수 없어 자동값으로 잠갔습니다. 모션블러가 있을 수 있습니다.")
        } else {
            applied.exposureLocked = false
            applied.warnings.append("노출을 잠글 수 없습니다. 밝기가 변합니다.")
        }

        // ── 2) 초점 ─────────────────────────────────────────────────────────
        //
        // ★ 3갈래로 판정합니다 (DESIGN.md §3.10 의 정정 사항)
        //
        //   (가) .locked 지원       -> 잠급니다. 정상
        //   (나) 어떤 초점 모드도 미지원 -> **고정초점 렌즈**. 초점 기구가 물리적으로
        //        없어서 잠글 대상이 없습니다. 초점거리가 변할 수 없으므로
        //        캘리브레이션에 **가장 유리**합니다. 경고 대상이 아닙니다.
        //   (다) 자동은 되는데 .locked 만 미지원 -> 진짜 문제. 경고.
        let anyFocusMode = dev.isFocusModeSupported(.locked)
            || dev.isFocusModeSupported(.autoFocus)
            || dev.isFocusModeSupported(.continuousAutoFocus)

        if dev.isFocusModeSupported(.locked) {
            dev.focusMode = .locked
            applied.focusLocked = true
            applied.isFixedFocusLens = false
        } else if !anyFocusMode {
            applied.focusLocked = false
            applied.isFixedFocusLens = true
            AppLog.shared.i("Cam", "고정초점 렌즈 — 잠글 초점 기구가 없습니다 (캘리브레이션에 유리)")
        } else {
            applied.focusLocked = false
            applied.isFixedFocusLens = false
            applied.warnings.append(
                "초점을 잠글 수 없습니다. 촬영 중 초점거리가 변하면 캘리브레이션이 틀어집니다.")
        }
        // 피사체 추적 자동초점은 초점을 계속 바꿉니다. 끕니다.
        if dev.isSubjectAreaChangeMonitoringEnabled {
            dev.isSubjectAreaChangeMonitoringEnabled = false
        }

        // ── 3) 화이트밸런스 ─────────────────────────────────────────────────
        if dev.isWhiteBalanceModeSupported(.locked) {
            dev.whiteBalanceMode = .locked
            applied.whiteBalanceLocked = true
        } else {
            applied.whiteBalanceLocked = false
            applied.warnings.append("화이트밸런스를 잠글 수 없습니다. 색이 변합니다.")
        }

        // ── 4) OIS — 끌 수 없습니다 ─────────────────────────────────────────
        //
        // 후면 광각에는 광학 손떨림 보정이 들어 있고 **끄는 공개 API 가 없습니다**.
        // 삼각대에 고정하면 보정할 흔들림이 없어 렌즈가 기준 위치에 머물지만,
        // 그게 실제로 그러한지는 측정으로 확인해야 합니다.
        // (재투영 오차가 시간에 따라 배회하는지 — DESIGN.md §3.10)
        // 초광각은 OIS 가 없으므로 문제가 생기면 그쪽으로 옮깁니다.
        if applied.deviceType.contains("WideAngle")
            && !applied.deviceType.contains("UltraWide") {
            applied.warnings.append(
                "후면 광각에는 OIS(광학 손떨림 보정)가 있고 끄는 API 가 없습니다. "
                + "삼각대에 단단히 고정하세요. 재투영 오차가 시간에 따라 배회하면 "
                + "초광각으로 바꿔야 합니다.")
        }
    }

    /// 잠근 뒤 **실제로 적용된** 값을 다시 읽습니다.
    ///
    /// ★ 왜 다시 읽는가: `setExposureModeCustom` 은 요청값을 그대로 쓰지 않을 수
    ///   있습니다(하드웨어가 반올림하거나 범위로 자름). 사이드카에는
    ///   **요청값이 아니라 실제값**이 들어가야 합니다. 그래야 나중에 영상을
    ///   보고 이상하다 싶을 때 기록과 대조할 수 있습니다.
    func readBackAppliedValues() {
        guard let dev = device else { return }
        applied.exposureDurationNs = Int64(CMTimeGetSeconds(dev.exposureDuration) * 1e9)
        applied.iso = Double(dev.iso)
        applied.lensPosition = Double(dev.lensPosition)

        AppLog.shared.i("Cam", String(
            format: "실제 적용: 셔터 %.0f µs (1/%.0f초), ISO %.0f, 렌즈위치 %.3f",
            Double(applied.exposureDurationNs) / 1000,
            1e9 / Double(max(applied.exposureDurationNs, 1)),
            applied.iso, applied.lensPosition))
        for w in applied.warnings { AppLog.shared.w("Cam", w) }
    }

    // MARK: - 세션 제어

    func start() {
        sessionQueue.async { [weak self] in
            guard let self, !self.session.isRunning else { return }
            self.session.startRunning()
            AppLog.shared.i("Cam", "세션 시작")
        }
    }

    func stop() {
        sessionQueue.async { [weak self] in
            guard let self, self.session.isRunning else { return }
            self.session.stopRunning()
            AppLog.shared.i("Cam", "세션 정지")
        }
    }
}
