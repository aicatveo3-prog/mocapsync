import AVFoundation
import Combine
import SwiftUI
import UIKit

// ─────────────────────────────────────────────────────────────────────────────
// 카메라와 녹화기를 묶어 UI 에 하나의 상태로 보여줍니다.
//
// 역할 분담
//   CameraController  하드웨어 설정 (포맷·노출·초점·안정화)
//   Recorder          파일 기록 + 프레임 타임스탬프 + 사이드카
//   CaptureCoordinator  둘을 순서대로 조립하고 UI 상태를 만듦  ← 이 파일
//
// ★ 순서가 중요합니다. 틀리면 조용히 나쁜 영상이 나옵니다.
//
//   1. 권한
//   2. 세션 구성 (포맷 고정, 안정화 끄기)
//   3. 세션 시작
//   4. **1.5초 기다림**  ← 자동노출/자동초점이 수렴할 시간
//   5. 잠그기 (노출·초점·WB)
//   6. 실제 적용값 되읽기
//   7. 녹화
//
// 4번을 빼먹으면 초기값(대개 엉뚱한 값)으로 굳어버립니다.
// ─────────────────────────────────────────────────────────────────────────────

@MainActor
final class CaptureCoordinator: ObservableObject {

    enum Phase: Equatable {
        case needsPermission
        case permissionDenied
        case configuring
        case converging(secondsLeft: Double)   // 자동노출 수렴 대기
        case ready
        case armed(leadMs: Double)
        case recording
        case finishing
        case done
        case failed(String)
    }

    @Published private(set) var phase: Phase = .needsPermission
    @Published private(set) var applied = CameraController.Applied()
    @Published private(set) var lastMovie: URL?
    @Published private(set) var lastSidecar: URL?
    @Published private(set) var sessions: [String] = []

    /// 동기 결과. ★ SyncStore 에서 **녹화 시작 직전에** 읽습니다.
    ///
    /// 미리 복사해 두면 그 사이에 폰이 자거나 재측정한 것을 놓칩니다.
    /// 그래서 필드로 들고 있지 않고 매번 물어봅니다.
    var sync: Recorder.SyncSnapshot { SyncStore.shared.snapshotForRecording() }

    /// 지금 오프셋이 촬영에 쓸 수 있는 상태인지
    var syncFreshness: ClockOffsetRecord.Freshness { SyncStore.shared.freshness() }

    let camera = CameraController()
    private(set) var recorder: Recorder!

    private var convergeTask: Task<Void, Never>?
    /// ★ Recorder 의 변경을 이 객체의 변경으로 전달합니다.
    ///   이게 없으면 화면이 `cap` 만 보고 있어서 프레임 수·fps 가 갱신되지 않습니다.
    private var recorderObserver: AnyCancellable?
    private var syncObserver: AnyCancellable?

    /// 자동노출/자동초점이 수렴할 시간. 짧으면 엉뚱한 값으로 잠깁니다.
    static let convergeSeconds: Double = 1.5

    init() {
        let id = CaptureCoordinator.stableDeviceId()
        recorder = Recorder(deviceId: id,
                            deviceName: UIDevice.current.name,
                            queue: camera.videoQueue)
        recorder.delegate = self
        recorderObserver = recorder.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }
        // 동기 측정이 갱신되면 녹화 화면의 표시도 바뀌어야 합니다.
        syncObserver = SyncStore.shared.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }
        refreshSessions()
        if CameraController.permissionStatus() == .authorized {
            phase = .configuring
        }
    }

    /// SyncClient 와 같은 규칙으로 기기 ID 를 만듭니다.
    /// PC 가 device_id 로 캘리브레이션을 매칭하므로 두 곳이 반드시 같아야 합니다.
    static func stableDeviceId() -> String {
        let key = "mocapsync.deviceId"
        if let v = UserDefaults.standard.string(forKey: key) { return v }
        let v = (UIDevice.current.identifierForVendor?.uuidString ?? UUID().uuidString)
            .replacingOccurrences(of: "-", with: "")
            .prefix(12).uppercased()
        UserDefaults.standard.set(String(v), forKey: key)
        return String(v)
    }

    // MARK: - 시작

    func requestPermissionAndStart() async {
        let ok = await CameraController.requestPermission()
        guard ok else {
            phase = .permissionDenied
            AppLog.shared.e("Cap", "카메라 권한 거부")
            return
        }
        await start()
    }

    /// 세션을 구성하고 설정을 잠그는 전 과정.
    func start() async {
        guard CameraController.permissionStatus() == .authorized else {
            phase = .needsPermission
            return
        }
        phase = .configuring

        // 1~2) 구성 — 세션 큐에서.
        // ★ MainActor 상태를 클로저 안에서 읽지 않도록 미리 지역 상수로 꺼냅니다.
        let cam = camera
        let rec = recorder!
        do {
            try await withCheckedThrowingContinuation {
                (c: CheckedContinuation<Void, Error>) in
                cam.sessionQueue.async {
                    do {
                        try cam.configure(delegate: rec)
                        c.resume()
                    } catch { c.resume(throwing: error) }
                }
            }
        } catch {
            phase = .failed(error.localizedDescription)
            AppLog.shared.e("Cap", "구성 실패: \(error.localizedDescription)")
            return
        }
        applied = camera.applied

        // 3) 세션 시작
        camera.start()

        // 4) 수렴 대기 — 남은 시간을 화면에 보여줍니다.
        //    사용자가 "멈춘 줄" 알고 버튼을 또 누르면 곤란하니까요.
        convergeTask?.cancel()
        convergeTask = Task { [weak self] in
            guard let self else { return }
            let step = 0.1
            var left = CaptureCoordinator.convergeSeconds
            while left > 0, !Task.isCancelled {
                self.phase = .converging(secondsLeft: left)
                try? await Task.sleep(nanoseconds: UInt64(step * 1e9))
                left -= step
            }
            guard !Task.isCancelled else { return }
            await self.lockAndReadBack()
        }
    }

    /// 5~6) 잠그고 실제 적용값을 되읽습니다.
    func lockAndReadBack() async {
        let cam = camera
        do {
            try await withCheckedThrowingContinuation {
                (c: CheckedContinuation<Void, Error>) in
                cam.sessionQueue.async {
                    do {
                        try cam.lockSettings()
                        // 잠근 값이 하드웨어에 반영될 시간을 조금 줍니다.
                        Thread.sleep(forTimeInterval: 0.25)
                        cam.readBackAppliedValues()
                        c.resume()
                    } catch { c.resume(throwing: error) }
                }
            }
        } catch {
            phase = .failed(error.localizedDescription)
            return
        }
        applied = camera.applied
        phase = .ready
        AppLog.shared.i("Cap", "촬영 준비 완료")
    }

    /// 설정을 다시 잠급니다 (조명이 바뀐 뒤 등)
    func relock() {
        Task { await lockAndReadBack() }
    }

    func stopSession() {
        convergeTask?.cancel()
        camera.stop()
        phase = .configuring
    }

    // MARK: - 녹화

    /// 즉시 녹화 시작 (폰 1대 단독 시험용)
    func recordNow(sessionId: String? = nil) {
        let sid = sessionId ?? CaptureCoordinator.newSessionId()
        do {
            try recorder.prepare(sessionId: sid, camera: applied, sync: sync,
                                 startAtSlaveNs: nil,
                                 requestedStartAtMasterNs: nil)
            phase = .recording
        } catch {
            phase = .failed(error.localizedDescription)
            AppLog.shared.e("Cap", "녹화 준비 실패: \(error.localizedDescription)")
        }
    }

    /// ★ 예약 시작.
    ///
    /// 마스터가 준 마스터시각을 이 기기 시계로 옮겨 넘깁니다.
    /// 실제 시작 판정은 Recorder 가 프레임 PTS 를 비교해서 합니다
    /// (정밀 대기 없음 — Recorder.swift 상단 설명 참고).
    ///
    /// - Returns: 예약을 받아들였는지. 거부 사유는 ScheduleCheck 에 담깁니다.
    @discardableResult
    func recordScheduled(sessionId: String,
                         startAtMasterNs: Int64) -> ScheduleCheck {
        let check = ClockSync.checkSchedule(
            startAtMasterNs: startAtMasterNs,
            offsetNs: sync.offsetNs,
            nowSlaveNs: MonotonicClock.nowNs())

        guard check.ok else {
            AppLog.shared.w("Cap", "예약 거부: \(check.reason)")
            return check
        }
        do {
            try recorder.prepare(sessionId: sessionId, camera: applied, sync: sync,
                                 startAtSlaveNs: check.startAtSlaveNs,
                                 requestedStartAtMasterNs: startAtMasterNs)
            phase = .armed(leadMs: check.leadMs)
        } catch {
            phase = .failed(error.localizedDescription)
            return ScheduleCheck(ok: false,
                                 startAtSlaveNs: check.startAtSlaveNs,
                                 leadNs: check.leadNs,
                                 reason: "prepare 실패: \(error.localizedDescription)")
        }
        return check
    }

    /// ★ 예약 시작 **자체 시험** — 폰 1대로 가능합니다.
    ///
    /// 마스터 없이, 지금부터 `leadMs` 뒤를 예약 시각으로 잡습니다.
    /// 오프셋은 0 으로 두므로 마스터시각 = 이 기기 시각이 됩니다.
    ///
    /// 이걸로 확인되는 것: 프레임 PTS 비교 방식이 실제로 예약 시각에
    /// 맞춰 첫 프레임을 고르는지. 사이드카의 `started_early` / `started_late`
    /// 검증이 통과하면 성공입니다.
    func recordScheduledSelfTest(leadMs: Double = 800) {
        let sid = CaptureCoordinator.newSessionId() + "-selftest"
        let now = MonotonicClock.nowNs()
        let target = now + Int64(leadMs * 1e6)
        do {
            try recorder.prepare(sessionId: sid, camera: applied, sync: sync,
                                 startAtSlaveNs: target,
                                 // 오프셋 0 가정이므로 마스터시각도 같은 값
                                 requestedStartAtMasterNs: target + sync.offsetNs)
            phase = .armed(leadMs: leadMs)
            AppLog.shared.i("Cap", String(format: "예약 자체시험: %.0f ms 뒤 시작", leadMs))
        } catch {
            phase = .failed(error.localizedDescription)
        }
    }

    func stopRecording() {
        phase = .finishing
        recorder.finish()
    }

    // MARK: - 세션 목록

    static func newSessionId() -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyyMMdd-HHmmss"
        return "S" + f.string(from: Date())
    }

    func refreshSessions() {
        do {
            let root = try Recorder.sessionsRoot()
            let items = try FileManager.default.contentsOfDirectory(
                at: root, includingPropertiesForKeys: nil)
            sessions = items.filter { $0.hasDirectoryPath }
                .map { $0.lastPathComponent }
                .sorted(by: >)
        } catch {
            sessions = []
        }
    }

    func files(in sessionId: String) -> [URL] {
        do {
            let d = try Recorder.sessionDirectory(sessionId)
            return try FileManager.default.contentsOfDirectory(
                at: d, includingPropertiesForKeys: [.fileSizeKey])
                .sorted { $0.lastPathComponent < $1.lastPathComponent }
        } catch { return [] }
    }

    func deleteSession(_ sessionId: String) {
        if let d = try? Recorder.sessionDirectory(sessionId) {
            try? FileManager.default.removeItem(at: d)
            AppLog.shared.i("Cap", "세션 삭제: \(sessionId)")
        }
        refreshSessions()
    }

    // MARK: - 진단 텍스트

    /// ★ 개발자에게 붙여줄 진단 한 덩어리.
    ///
    /// 왜 이게 필요한가: 실기기 확인을 사람이 대신하는 구조에서, 필요한 정보를
    /// 화면에서 하나하나 찾아 옮겨 적게 하면 루프가 느려지고 빠뜨리기도 쉽습니다.
    /// 실제로 3단계 시험에서 "사용 불가"라는 결과만 전달되고 사유가 빠져서
    /// 두 번 추측해야 했습니다.
    ///
    /// 그래서 한 번 눌러 전부 복사되게 만듭니다. 성공했을 때도 필요합니다 —
    /// 검증을 통과했어도 fps 나 셔터가 틀렸으면 경고만 뜨고 지나가기 때문입니다.
    func diagnosticText() -> String {
        var s = ""
        s += "── MocapSync 촬영 진단 ──\n"
        s += "빌드      \(BuildInfo.versionFull)\n"
        s += "기기      \(BuildInfo.deviceFriendlyName) (\(BuildInfo.deviceModelIdentifier)) / iOS \(BuildInfo.osVersion)\n"
        s += "발열      \(Recorder.thermalName())\n\n"

        s += "[클럭 동기]\n"
        s += "  상태            \(syncFreshness.summary)\n"
        if let r = SyncStore.shared.latest {
            s += String(format: "  오프셋          %+.3f ms\n", r.offsetNs.ms)
            s += String(format: "  측정 당시 상한  %.3f ms (최소RTT %.3f ms)\n",
                        r.uncertaintyNs.ms, r.minRttNs.ms)
            s += String(format: "  드리프트 포함   %.3f ms\n",
                        SyncStore.shared.displayEffectiveUncertaintyNs.ms)
            if let ppm = SyncStore.shared.measuredDriftPpm,
               let unc = SyncStore.shared.driftUncertaintyPpm {
                s += String(format: "  드리프트 실측   %+.2f ppm (신뢰구간 ±%.2f ppm)%@\n",
                            ppm, unc, abs(ppm) > unc ? "" : "  <- 잡음 안, 측정 불가")
            } else {
                s += "  드리프트 실측   아직 (두 번 측정하면 나옴)\n"
            }
        } else {
            s += "  (측정 없음)\n"
        }
        s += "\n"

        let a = applied
        s += "[잠긴 카메라 설정 — 실제 적용값]\n"
        s += "  카메라        \(a.deviceType)\n"
        s += "  해상도        \(a.width)x\(a.height) @\(a.fps)fps\n"
        s += String(format: "  화각          %.1f도\n", a.fieldOfViewDeg)
        s += "  binned        \(a.isBinned ? "예 (해상감 저하)" : "아니오")\n"
        if a.exposureDurationNs > 0 {
            s += String(format: "  셔터          %.0f µs = 1/%.0f초\n",
                        Double(a.exposureDurationNs) / 1000,
                        1e9 / Double(a.exposureDurationNs))
        } else {
            s += "  셔터          (읽지 못함)\n"
        }
        s += String(format: "  ISO           %.0f\n", a.iso)
        s += String(format: "  렌즈 위치     %.3f\n", a.lensPosition)
        s += "  노출 고정     \(a.exposureLocked ? "예" : "아니오 ★")\n"
        s += "  초점          " + (a.isFixedFocusLens
            ? "고정초점 렌즈 (잠글 기구 없음 — 정상)"
            : (a.focusLocked ? "잠김" : "안 잠김 ★")) + "\n"
        s += "  화이트밸런스  \(a.whiteBalanceLocked ? "잠김" : "안 잠김 ★")\n"
        s += "  안정화        \(a.stabilization)\n"
        if !a.warnings.isEmpty {
            s += "  잠그지 못한 것:\n"
            for w in a.warnings { s += "    · \(w)\n" }
        }
        s += "\n"

        s += "[마지막 녹화]\n"
        s += "  프레임        \(recorder.displayFrameCount)\n"
        s += String(format: "  실측 fps      %.3f\n", recorder.displayFps)
        s += "  버린 프레임   \(recorder.displayDroppedCount)\n"
        if let m = lastMovie {
            let attrs = try? FileManager.default.attributesOfItem(atPath: m.path)
            let bytes = (attrs?[.size] as? NSNumber)?.int64Value ?? 0
            s += String(format: "  파일          %@ (%.1f MB)\n",
                        m.lastPathComponent, Double(bytes) / 1_048_576)
        }
        s += "\n"

        s += "[사이드카 자체검증] \(recorder.lastUsable ? "사용 가능 ✔" : "★ 사용 불가")\n"
        if recorder.lastValidation.isEmpty {
            s += "  (아직 녹화하지 않았습니다)\n"
        } else {
            for l in recorder.lastValidation { s += "  \(l)\n" }
        }
        return s
    }

    /// 남은 저장 공간 (바이트)
    static func freeSpaceBytes() -> Int64 {
        guard let v = try? URL(fileURLWithPath: NSHomeDirectory())
            .resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]),
              let c = v.volumeAvailableCapacityForImportantUsage else { return 0 }
        return c
    }

    /// 1080p60 20Mbps 기준으로 남은 녹화 가능 시간(분)
    static func estimatedMinutesLeft() -> Double {
        Double(freeSpaceBytes()) / (Double(Recorder.defaultBitrate) / 8.0) / 60.0
    }
}

// MARK: - Recorder 콜백

extension CaptureCoordinator: RecorderDelegate {

    func recorderDidUpdate(_ r: Recorder) {
        // Recorder 의 @Published 가 이미 화면을 갱신합니다.
        // 여기서는 단계 전이만 반영합니다.
        switch r.uiState {
        case .recording:
            if phase != .recording { phase = .recording }
        case .finishing:
            phase = .finishing
        default:
            break
        }
    }

    func recorderDidFinish(_ r: Recorder, movie: URL, sidecar: URL) {
        lastMovie = movie
        lastSidecar = sidecar
        phase = .done
        refreshSessions()
        let attrs = try? FileManager.default.attributesOfItem(atPath: movie.path)
        let bytes = (attrs?[.size] as? NSNumber)?.int64Value ?? 0
        AppLog.shared.i("Cap", String(
            format: "저장 완료: %@ (%.1f MB) + %@",
            movie.lastPathComponent,
            Double(bytes) / 1_048_576.0,
            sidecar.lastPathComponent))
    }

    func recorderDidFail(_ r: Recorder, error: Error) {
        phase = .failed(error.localizedDescription)
        AppLog.shared.e("Cap", "녹화 실패: \(error.localizedDescription)")
    }
}
