import AVFoundation
import Combine
import CoreMedia
import UIKit

// ─────────────────────────────────────────────────────────────────────────────
// 녹화 + 프레임 타임스탬프 기록.
//
// ★★ 예약 시작에 대한 설계 변경 (이전 계획보다 나은 방법을 찾았습니다)
//
// 원래 계획: 예약 시각까지 정밀 대기(coarse sleep + spin)한 뒤 녹화를 시작.
// 그래서 PC 쪽에서 스핀 마진 20ms 같은 값을 실측해 뒀습니다.
//
// 그런데 녹화에는 정밀 대기가 **필요하지 않습니다.**
//
// 카메라는 이미 60fps 로 돌고 있습니다. 우리가 할 일은 "언제 세션을 켜느냐"가
// 아니라 "어느 프레임부터 파일에 쓰느냐"입니다. 그래서:
//
//     각 프레임의 PTS 를 보고, PTS >= 예약시각 인 **첫 프레임**부터 씁니다.
//
// 이렇게 하면
//   · 정밀 대기가 아예 필요 없습니다 (스핀으로 CPU 를 태울 이유도 없음)
//   · 명령이 예약시각보다 먼저만 도착하면 지연이 **전혀** 무해합니다
//   · 시작 지점이 프레임 경계에 정확히 맞습니다
//   · 세션 시작에 1초 가까이 걸리는 문제도 사라집니다 (미리 켜 두면 됨)
//
// 남는 오차는 프레임 양자화(16.67ms)뿐이고, 그건 프레임 **타임스탬프**가
// 이미 정확히 알려주므로 리샘플러가 처리합니다. 규약 §4.6 의
// "예약 시작 정밀도는 임계 경로가 아니다"가 여기서 구조적으로 보장됩니다.
//
// 정밀 대기 코드는 PC 마스터 쪽에 남겨 둡니다 (다른 용도로 필요할 수 있음).
//
// ── 또 하나: B프레임을 끕니다 ───────────────────────────────────────────────
//
// H.264 의 B프레임은 디코딩 순서와 표시 순서를 다르게 만듭니다.
// 그러면 PC 에서 "영상의 N번째 프레임"과 "사이드카의 N번째 프레임"이
// 어긋날 수 있습니다. 4단계 리샘플러는 그 1:1 대응에 의존하므로
// `AVVideoAllowFrameReorderingKey = false` 로 B프레임을 없앱니다.
// 용량이 조금 늘지만 대응이 확실해집니다.
// ─────────────────────────────────────────────────────────────────────────────

@MainActor
protocol RecorderDelegate: AnyObject {
    func recorderDidUpdate(_ r: Recorder)
    func recorderDidFinish(_ r: Recorder, movie: URL, sidecar: URL)
    func recorderDidFail(_ r: Recorder, error: Error)
}

/// ★ 스레드 규칙 (지키지 않으면 조용한 경합이 됩니다)
///
/// · `state`, `frames`, `droppedCount` 등 기록 상태는 **videoQueue 에서만** 만집니다.
///   프레임 콜백이 그 큐에서 오고, 그 큐는 직렬이므로 락이 필요 없습니다.
/// · UI 는 `uiState` / `displayFrameCount` 만 봅니다. 이건 MainActor 에서만 씁니다.
/// · 외부에서 부르는 `prepare` / `finish` 는 내부에서 videoQueue 로 넘깁니다.
///
/// 클럭 동기에서 매 왕복마다 @Published 를 건드려 측정값을 망친 적이 있습니다
/// (DESIGN.md §3.11). 같은 함정을 피하려고 표시용 상태를 완전히 분리했습니다.
/// `@unchecked Sendable`: 동시성 안전성을 컴파일러가 아니라 위 규칙으로 보장합니다.
/// 기록 상태는 직렬 큐 하나에서만 만지고, 표시 상태는 MainActor 에서만 만집니다.
final class Recorder: NSObject, ObservableObject, @unchecked Sendable {

    enum State: Equatable {
        case idle
        /// 예약 시각을 기다리는 중 (프레임은 흘려보내고 있음)
        case armed(startAtSlaveNs: Int64)
        case recording
        case finishing
        case finished
        case failed(String)
    }

    enum RecError: LocalizedError {
        case notConfigured
        case writerFailed(String)
        case noFrames

        var errorDescription: String? {
            switch self {
            case .notConfigured: return "녹화기가 준비되지 않았습니다."
            case .writerFailed(let m): return "파일 기록 실패: \(m)"
            case .noFrames: return "프레임이 하나도 기록되지 않았습니다."
            }
        }
    }

    /// 1080p60 기본 비트레이트.
    /// 설계 문서는 crf 30 도 정확도 영향이 거의 없다고 했지만, 저장 공간이
    /// 46GB 남아 있으므로 여유를 둡니다 (20Mbps ≈ 150MB/분).
    static let defaultBitrate = 20_000_000

    weak var delegate: RecorderDelegate?

    // ── 기록 상태 (videoQueue 전용) ──────────────────────────────────────────
    private var state: State = .idle
    /// 기록된 프레임: [[번호, 시각ns], ...]
    private var frames: [[Int64]] = []
    private var droppedCount = 0
    private var firstFramePtsNs: Int64?
    private var lastFramePtsNs: Int64?

    // ── 표시 상태 (MainActor 전용) ───────────────────────────────────────────
    @Published private(set) var uiState: State = .idle
    @Published private(set) var displayFrameCount = 0
    @Published private(set) var displayDroppedCount = 0
    @Published private(set) var displayFps: Double = 0
    @Published private(set) var lastValidation: [String] = []
    @Published private(set) var lastUsable = false

    /// 프레임 콜백이 오는 큐. 모든 상태 변경을 여기로 넘깁니다.
    private let queue: DispatchQueue

    private var writer: AVAssetWriter?
    private var input: AVAssetWriterInput?
    private var movieURL: URL?
    private var sidecarURL: URL?

    private var startAtSlaveNs: Int64?
    private var requestedStartAtMasterNs: Int64?
    private var sessionId = ""
    private var camera: CameraController.Applied = .init()
    private var syncInfo: SyncSnapshot = .empty

    private var thermalAtStart = ""
    private var batteryAtStart: Double = -1
    private var sleepAtRecordStartNs: Int64 = 0
    private var timestampDomainDeltaNs: Int64 = 0
    private var deviceOrientationAtStart = "unknown"

    private let deviceId: String
    private let deviceName: String

    /// 동기 결과를 녹화 시점으로 들고 오는 스냅샷.
    struct SyncSnapshot {
        var offsetNs: Int64 = 0
        var uncertaintyNs: Int64 = 0
        var minRttNs: Int64 = 0
        var measuredAtSlaveNs: Int64 = 0
        var sleepAtSyncNs: Int64 = 0
        static let empty = SyncSnapshot()
        var isValid: Bool { uncertaintyNs > 0 }
    }

    init(deviceId: String, deviceName: String, queue: DispatchQueue) {
        self.deviceId = deviceId
        self.deviceName = deviceName
        self.queue = queue
        super.init()
    }

    /// videoQueue 의 상태를 MainActor 로 옮깁니다.
    private func publish(_ s: State) {
        let n = frames.count
        let d = droppedCount
        // 실측 fps: 기록된 첫 프레임과 마지막 프레임 사이로 계산합니다.
        var fps = 0.0
        if let a = firstFramePtsNs, let b = lastFramePtsNs, b > a, n >= 2 {
            fps = Double(n - 1) * 1e9 / Double(b - a)
        }
        Task { @MainActor in
            self.uiState = s
            self.displayFrameCount = n
            self.displayDroppedCount = d
            self.displayFps = fps
            self.delegate?.recorderDidUpdate(self)
        }
    }

    // MARK: - 시계 도메인 실측

    /// ★ 프레임 PTS 의 시계와 우리 동기 시계가 같은 도메인인지 **매번** 확인합니다.
    ///
    /// 규약 §1 의 전제입니다. 아이폰 11 에서 −458 ns 로 확인됐지만,
    /// 그건 그 기기·그 OS 에서의 관측이고 다른 기기에서 다를 수 있습니다.
    /// 그래서 녹화마다 실측해 사이드카에 넣습니다. 나중에 결과가 이상할 때
    /// "시계가 달랐는지"를 데이터만 보고 판정할 수 있어야 합니다.
    static func measureTimestampDomainDeltaNs() -> Int64 {
        let host = CMClockGetTime(CMClockGetHostTimeClock())
        let uptime = MonotonicClock.nowNs()
        let hostNs = CMTimeConvertScale(host, timescale: 1_000_000_000,
                                        method: .roundHalfAwayFromZero).value
        return uptime - hostNs
    }

    /// CMTime -> 나노초 정수. 부동소수를 거치지 않습니다.
    @inline(__always)
    static func ptsToNs(_ t: CMTime) -> Int64 {
        CMTimeConvertScale(t, timescale: 1_000_000_000,
                           method: .roundHalfAwayFromZero).value
    }

    // MARK: - 준비

    /// 파일과 라이터를 만듭니다. 아직 프레임은 쓰지 않습니다.
    ///
    /// - Parameters:
    ///   - startAtSlaveNs: 이 시각(이 기기 시계) 이후의 첫 프레임부터 기록합니다.
    ///                     nil 이면 다음 프레임부터 바로 기록합니다.
    func prepare(sessionId: String,
                 camera: CameraController.Applied,
                 sync: SyncSnapshot,
                 startAtSlaveNs: Int64?,
                 requestedStartAtMasterNs: Int64?,
                 bitrate: Int = Recorder.defaultBitrate) throws {
        // ★ videoQueue 에서 동기적으로 실행합니다.
        //   프레임 콜백과 같은 큐이므로, 준비가 끝나기 전에 프레임이 들어와
        //   반쯤 준비된 상태를 보는 일이 없습니다.
        var thrown: Error?
        queue.sync {
            do { try self.prepareOnQueue(
                sessionId: sessionId, camera: camera, sync: sync,
                startAtSlaveNs: startAtSlaveNs,
                requestedStartAtMasterNs: requestedStartAtMasterNs,
                bitrate: bitrate) }
            catch { thrown = error }
        }
        if let t = thrown { throw t }
    }

    private func prepareOnQueue(sessionId: String,
                                camera: CameraController.Applied,
                                sync: SyncSnapshot,
                                startAtSlaveNs: Int64?,
                                requestedStartAtMasterNs: Int64?,
                                bitrate: Int) throws {

        self.sessionId = sessionId
        self.camera = camera
        self.syncInfo = sync
        self.startAtSlaveNs = startAtSlaveNs
        self.requestedStartAtMasterNs = requestedStartAtMasterNs

        frames = []
        droppedCount = 0
        firstFramePtsNs = nil
        lastFramePtsNs = nil

        thermalAtStart = Recorder.thermalName()
        batteryAtStart = Double(UIDevice.current.batteryLevel)
        sleepAtRecordStartNs = MonotonicClock.cumulativeSleepNs()
        timestampDomainDeltaNs = Recorder.measureTimestampDomainDeltaNs()
        // ★ 녹화 시작 시점의 기기 방향. 세로로 들고 찍으면 사람이 눕혀 저장됩니다.
        deviceOrientationAtStart = CameraController.orientationName()
        if !CameraController.isLandscapeNow() {
            AppLog.shared.w("Rec", "★ 폰이 가로가 아닙니다 (\(deviceOrientationAtStart)). "
                + "후면 카메라 기준 방향은 가로이므로 사람이 90도 누워 저장됩니다.")
        }

        let dir = try Recorder.sessionDirectory(sessionId)
        let mov = dir.appendingPathComponent("\(deviceId).mov")
        let side = dir.appendingPathComponent("\(deviceId).json")
        try? FileManager.default.removeItem(at: mov)
        try? FileManager.default.removeItem(at: side)
        movieURL = mov
        sidecarURL = side

        let w = try AVAssetWriter(outputURL: mov, fileType: .mov)

        let settings: [String: Any] = [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: camera.width,
            AVVideoHeightKey: camera.height,
            AVVideoCompressionPropertiesKey: [
                AVVideoAverageBitRateKey: bitrate,
                AVVideoExpectedSourceFrameRateKey: camera.fps,
                // 1초마다 키프레임. PC 에서 탐색이 쉬워집니다.
                AVVideoMaxKeyFrameIntervalKey: camera.fps,
                AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
                // ★ B프레임 끄기. 디코딩 순서 = 표시 순서가 되어
                //   "영상 N번째 프레임" = "사이드카 N번째 프레임" 이 보장됩니다.
                //   4단계 리샘플러가 이 대응에 의존합니다.
                AVVideoAllowFrameReorderingKey: false,
            ] as [String: Any],
        ]

        let i = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
        // 실시간 입력임을 알려야 인코더가 프레임을 기다리지 않습니다.
        i.expectsMediaDataInRealTime = true
        // 회전은 넣지 않습니다. 픽셀과 메타데이터를 그대로 둬야
        // PC 쪽 캘리브레이션과 좌표계가 어긋나지 않습니다.
        guard w.canAdd(i) else {
            throw RecError.writerFailed("입력을 라이터에 붙일 수 없습니다")
        }
        w.add(i)
        guard w.startWriting() else {
            throw RecError.writerFailed(w.error?.localizedDescription ?? "startWriting 실패")
        }

        writer = w
        input = i
        state = startAtSlaveNs == nil ? .recording : .armed(startAtSlaveNs: startAtSlaveNs!)

        if let s = startAtSlaveNs {
            let lead = s - MonotonicClock.nowNs()
            AppLog.shared.i("Rec", String(
                format: "예약 대기: %.1f ms 뒤 첫 프레임부터 기록 (PTS 비교 방식)",
                lead.ms))
        } else {
            AppLog.shared.i("Rec", "즉시 기록 시작")
        }
        AppLog.shared.i("Rec", "시계 도메인 실측차 \(timestampDomainDeltaNs) ns")
        AppLog.shared.i("Rec", "파일: \(mov.lastPathComponent)")
        publish(state)
    }

    // MARK: - 종료

    func finish() {
        queue.async { [weak self] in self?.finishOnQueue() }
    }

    private func finishOnQueue() {
        guard let w = writer, let i = input else { return }
        guard state == .recording || isArmed else {
            AppLog.shared.w("Rec", "녹화 중이 아닙니다 (state=\(state))")
            return
        }
        state = .finishing
        publish(state)

        let frameCount = frames.count
        i.markAsFinished()
        // finishWriting 의 콜백은 임의 큐에서 옵니다. 상태를 만지기 전에
        // 반드시 videoQueue 로 되돌립니다.
        w.finishWriting { [weak self] in
            guard let self else { return }
            self.queue.async { self.afterWriterFinished(w, frameCount: frameCount) }
        }
    }

    private func afterWriterFinished(_ w: AVAssetWriter, frameCount: Int) {
        if w.status == .failed {
            let msg = w.error?.localizedDescription ?? "알 수 없는 오류"
            AppLog.shared.e("Rec", "라이터 실패: \(msg)")
            state = .failed(msg)
            publish(state)
            Task { @MainActor in
                self.delegate?.recorderDidFail(self, error: RecError.writerFailed(msg))
            }
            return
        }
        if frameCount == 0 {
            AppLog.shared.e("Rec", "프레임 0개")
            state = .failed("프레임 0개")
            publish(state)
            Task { @MainActor in
                self.delegate?.recorderDidFail(self, error: RecError.noFrames)
            }
            return
        }
        do {
            let side = try writeSidecar()
            state = .finished
            AppLog.shared.i("Rec", "녹화 완료: \(frameCount)프레임, 버린 프레임 \(droppedCount)개")
            publish(state)
            if let mov = movieURL {
                Task { @MainActor in
                    self.delegate?.recorderDidFinish(self, movie: mov, sidecar: side)
                }
            }
        } catch {
            AppLog.shared.e("Rec", "사이드카 쓰기 실패: \(error)")
            state = .failed("\(error)")
            publish(state)
            Task { @MainActor in self.delegate?.recorderDidFail(self, error: error) }
        }
    }

    private var isArmed: Bool {
        if case .armed = state { return true }
        return false
    }

    // MARK: - 사이드카

    private func writeSidecar() throws -> URL {
        guard let url = sidecarURL else { throw RecError.notConfigured }

        let s = Sidecar(
            deviceId: deviceId,
            deviceName: deviceName,
            model: BuildInfo.deviceModelIdentifier,
            osVersion: BuildInfo.osVersion,
            appVersion: BuildInfo.versionFull,
            sessionId: sessionId,
            role: "slave",
            clock: MonotonicClock.name,
            clockOffsetNs: syncInfo.offsetNs,
            clockUncertaintyNs: syncInfo.uncertaintyNs,
            clockMinRttNs: syncInfo.minRttNs,
            clockMeasuredAtNs: syncInfo.measuredAtSlaveNs,
            sleepAtSyncNs: syncInfo.sleepAtSyncNs,
            sleepAtRecordStartNs: sleepAtRecordStartNs,
            timestampSource: "CMSampleBufferPresentationTimeStamp",
            timestampDomainDeltaNs: timestampDomainDeltaNs,
            targetFps: camera.fps,
            width: camera.width,
            height: camera.height,
            cameraDeviceType: camera.deviceType,
            fieldOfViewDeg: camera.fieldOfViewDeg,
            isBinned: camera.isBinned,
            exposureDurationNs: camera.exposureDurationNs,
            iso: camera.iso,
            lensPosition: camera.lensPosition,
            focusLocked: camera.focusLocked,
            whiteBalanceLocked: camera.whiteBalanceLocked,
            exposureLocked: camera.exposureLocked,
            stabilization: camera.stabilization,
            deviceOrientation: deviceOrientationAtStart,
            cameraWarnings: camera.warnings,
            requestedStartAtMasterNs: requestedStartAtMasterNs,
            requestedStartAtSlaveNs: startAtSlaveNs,
            firstFramePtsNs: firstFramePtsNs,
            droppedFrameCount: droppedCount,
            thermalAtStart: thermalAtStart,
            thermalAtEnd: Recorder.thermalName(),
            batteryAtStart: batteryAtStart,
            batteryAtEnd: Double(UIDevice.current.batteryLevel),
            frames: frames)

        try s.encoded().write(to: url, options: .atomic)

        // ★ 쓴 직후 스스로 검증합니다.
        //   PC 까지 올려보내고 나서 문제를 발견하면 재촬영 기회를 놓칩니다.
        //   폰에서 바로 알려줘야 다시 찍을 수 있습니다.
        let report = s.validationReport()
        AppLog.shared.i("Rec", "── 사이드카 자체검증 ──")
        for line in report {
            if line.hasPrefix("[치명]") { AppLog.shared.e("Rec", line) }
            else if line.hasPrefix("[경고]") { AppLog.shared.w("Rec", line) }
            else { AppLog.shared.i("Rec", line) }
        }
        let usable = s.isUsable
        Task { @MainActor in
            self.lastValidation = report
            self.lastUsable = usable
        }
        return url
    }

    // MARK: - 경로

    static func sessionsRoot() throws -> URL {
        let docs = try FileManager.default.url(
            for: .documentDirectory, in: .userDomainMask,
            appropriateFor: nil, create: true)
        let root = docs.appendingPathComponent("sessions", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        return root
    }

    static func sessionDirectory(_ sessionId: String) throws -> URL {
        let d = try sessionsRoot().appendingPathComponent(sessionId, isDirectory: true)
        try FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d
    }

    static func thermalName() -> String {
        switch ProcessInfo.processInfo.thermalState {
        case .nominal: return "nominal"
        case .fair: return "fair"
        case .serious: return "serious"
        case .critical: return "critical"
        @unknown default: return "unknown"
        }
    }
}

// MARK: - 프레임 수신

extension Recorder: AVCaptureVideoDataOutputSampleBufferDelegate {

    /// ★ 이 콜백은 `CameraController.videoQueue` (직렬)에서 호출됩니다.
    ///   여기서 하는 일은 최소여야 합니다. 60fps 면 16.67ms 안에 끝나야
    ///   다음 프레임을 놓치지 않습니다.
    func captureOutput(_ output: AVCaptureOutput,
                       didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {

        let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        let ptsNs = Recorder.ptsToNs(pts)

        // ── 예약 시작: PTS 비교 ─────────────────────────────────────────────
        //
        // 정밀 대기도, 타이머도 없습니다. 프레임의 시각을 보고 판단합니다.
        // 예약 시각 이전 프레임은 그냥 버립니다(파일에 쓰지 않음).
        if case .armed(let startAt) = state {
            if ptsNs < startAt { return }
            state = .recording
            AppLog.shared.i("Rec", String(
                format: "예약 시각 도달: 첫 프레임 PTS 가 예약보다 %+.3f ms",
                (ptsNs - startAt).ms))
            publish(state)
        }

        guard state == .recording,
              let w = writer, let i = input else { return }

        if w.status == .failed {
            let msg = w.error?.localizedDescription ?? "?"
            AppLog.shared.e("Rec", "라이터 실패 상태: \(msg)")
            state = .failed(msg)
            return
        }

        // ★ 첫 프레임에서 세션 시작 시각을 그 프레임의 PTS 로 잡습니다.
        //   이렇게 하면 영상 파일 내부 타임라인이 0 부터 시작하고,
        //   절대 시각은 사이드카가 담당합니다. 역할이 깔끔히 분리됩니다.
        if firstFramePtsNs == nil {
            w.startSession(atSourceTime: pts)
            firstFramePtsNs = ptsNs
        }

        guard i.isReadyForMoreMediaData else {
            // 인코더가 밀렸습니다. 이 프레임은 못 씁니다.
            // 조용히 넘기지 않고 셉니다 — 사이드카에 기록되어야 합니다.
            droppedCount += 1
            AppLog.shared.w("Rec", "인코더 포화로 프레임 버림 (누적 \(droppedCount))")
            return
        }

        if i.append(sampleBuffer) {
            frames.append([Int64(frames.count), ptsNs])
            lastFramePtsNs = ptsNs
            // ★ UI 갱신은 15프레임(약 0.25초)마다.
            //   매 프레임 @Published 를 건드리면 메인 스레드가 리렌더로 바빠져
            //   프레임을 놓칩니다. 클럭 동기에서 정확히 이 함정에 빠졌습니다
            //   (DESIGN.md §3.11 결함 ②).
            if frames.count % 15 == 0 { publish(state) }
        } else {
            droppedCount += 1
            AppLog.shared.w("Rec", "append 실패 (누적 \(droppedCount)): "
                            + (w.error?.localizedDescription ?? "?"))
        }
    }

    /// 카메라가 프레임을 버렸을 때. 발열 스로틀링이나 처리 지연의 신호입니다.
    func captureOutput(_ output: AVCaptureOutput,
                       didDrop sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        guard state == .recording else { return }
        droppedCount += 1
        // ★ 버린 이유를 읽습니다. 발열 스로틀링인지 처리 지연인지 구분되어야
        //   원인을 고칠 수 있습니다. 숫자만 세면 원인을 모릅니다.
        // CMGetAttachment 는 CFTypeRef? 를 줍니다. CFString 은 String 으로 브리징됩니다.
        let raw = CMGetAttachment(
            sampleBuffer,
            key: kCMSampleBufferAttachmentKey_DroppedFrameReason,
            attachmentModeOut: nil)
        let reason = (raw as? String) ?? "?"
        AppLog.shared.w("Rec", "카메라가 프레임 버림 (누적 \(droppedCount)) 이유=\(reason)")
    }
}
