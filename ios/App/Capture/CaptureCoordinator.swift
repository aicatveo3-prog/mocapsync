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

    /// 동기 결과를 여기 넣어두면 녹화 시 사이드카에 들어갑니다.
    @Published var sync = Recorder.SyncSnapshot.empty

    let camera = CameraController()
    private(set) var recorder: Recorder!

    private var convergeTask: Task<Void, Never>?
    /// ★ Recorder 의 변경을 이 객체의 변경으로 전달합니다.
    ///   이게 없으면 화면이 `cap` 만 보고 있어서 프레임 수·fps 가 갱신되지 않습니다.
    private var recorderObserver: AnyCancellable?

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
