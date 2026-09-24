import Foundation

// ─────────────────────────────────────────────────────────────────────────────
// 사이드카 — 영상 파일 옆에 붙는 타임스탬프 기록.
//
// ★ 이 파일이 프로젝트에서 가장 중요한 데이터 구조입니다.
//
// 영상은 "무엇이 찍혔는지"만 담습니다. "언제 찍혔는지"는 담지 못합니다.
// (컨테이너의 PTS 는 파일 시작 기준 상대시간이라 다른 기기와 비교할 수 없습니다)
//
// 3D 복원은 "같은 순간에 두 각도에서 본 점"을 삼각측량합니다. 그래서 프레임
// 하나하나가 **공통 시간축에서 몇 시였는지**를 알아야 합니다. 그걸 담는 게 이 파일입니다.
//
// 우리 파이프라인의 차별점(리샘플러)이 이 파일만 보고 동작합니다.
// 여기가 틀리면 그 뒤 전부 틀립니다.
//
// ── 설계 원칙 ───────────────────────────────────────────────────────────────
//
// 1. **변환하지 않고 원본을 저장합니다.**
//    프레임 시각은 그 폰의 시계 그대로, 오프셋은 별도 필드로.
//    미리 더해서 저장하면 나중에 오프셋이 틀렸다는 걸 알아도 되돌릴 수 없습니다.
//
// 2. **판단 근거를 함께 저장합니다.**
//    오차 상한, 최소 RTT, 시계 도메인 실측차 — PC 가 품질을 보고할 수 있게.
//
// 3. **낡은 오프셋을 탐지할 수 있게 저장합니다.**
//    2026-09-24 실측: 폰이 52분 중 34분을 자면서 시계가 멈췄고 오프셋이
//    34분 어긋났습니다. 동기 시점과 녹화 시점의 누적 절전시간을 각각 기록해
//    두면, 그 사이에 잤는지를 **데이터만 보고** 알 수 있습니다.
//
// server/mocapsync/sidecar.py 와 **키가 정확히 일치해야** 합니다.
// ─────────────────────────────────────────────────────────────────────────────

/// 사이드카 최상위 구조.
public struct Sidecar: Codable, Equatable, Sendable {

    public static let currentSchemaVersion = 1

    // ── 오차 상한 판정 기준 3단계 ───────────────────────────────────────────
    //
    // ★ 왜 3단계인가
    //
    // 2026-09-24 실측에서 이 경로의 바닥은 최소 RTT 4.076 ms = 상한 2.038 ms 였고,
    // 설정을 어떻게 바꿔도 4 ms 아래로 내려가지 않았습니다 (DESIGN.md §3.12).
    // 즉 설계 목표 2 ms 는 **구조적으로 거의 항상 초과**합니다.
    //
    // 이걸 경고로 두면 모든 정상 촬영에 경고가 붙고, 그러면 아무도 경고를
    // 보지 않게 됩니다. 늑대가 왔다고 매번 외치는 셈입니다.
    // 그래서 "목표 초과"는 정보, "기준선보다 나쁨"은 경고로 나눴습니다.

    /// 설계 목표. 넘으면 info.
    public static let targetUncertaintyNs: Int64 = 2 * NS.perMilli
    /// 실측 기준선(최악 2.995 ms)보다 나쁘면 warning. 링크 이상 신호.
    public static let degradedUncertaintyNs: Int64 = 3 * NS.perMilli
    /// 60fps 반 프레임. 넘으면 fatal — 프레임 짝짓기가 모호해집니다.
    public static let halfFrame60Ns: Int64 = 8_333_333

    /// 드리프트 판정에 쓰는 상대 주파수 오차 (ppm).
    ///
    /// 휴대기기 수정발진자 규격이 보통 ±20 ppm 이라 두 기기 상대로 40 ppm 을
    /// 최악값으로 잡습니다. `ClockOffsetRecord.worstCaseDriftPpm` 과 같은 값이어야
    /// 합니다 — 앱이 경고하는 기준과 PC 가 판정하는 기준이 다르면
    /// 폰에서는 통과했는데 PC 에서 거부되는 일이 생깁니다.
    public static let assumedDriftPpm: Double = 40

    // ── 형식 ────────────────────────────────────────────────────────────────
    public var schemaVersion: Int

    // ── 기기 ────────────────────────────────────────────────────────────────
    /// PC 가 이 값으로 저장된 내부 캘리브레이션을 찾습니다. 재설치에도 유지되어야 합니다.
    public var deviceId: String
    public var deviceName: String
    public var model: String
    public var osVersion: String
    public var appVersion: String

    // ── 세션 ────────────────────────────────────────────────────────────────
    public var sessionId: String
    /// "master" 또는 "slave"
    public var role: String

    // ── 시계 ────────────────────────────────────────────────────────────────
    /// 프레임 시각과 오프셋이 쓰는 시계 이름. 예: "CLOCK_UPTIME_RAW"
    public var clock: String
    /// 마스터시각 = 이 기기 시각 + clockOffsetNs
    public var clockOffsetNs: Int64
    /// 증명된 오차 상한 (= 최소RTT/2)
    public var clockUncertaintyNs: Int64
    public var clockMinRttNs: Int64
    /// 오프셋을 측정한 시점 (이 기기 시계)
    public var clockMeasuredAtNs: Int64
    /// ★ 동기 시점의 누적 절전시간
    public var sleepAtSyncNs: Int64
    /// ★ 녹화 시작 시점의 누적 절전시간.
    ///   sleepAtSyncNs 와 다르면 그 사이에 폰이 잤다는 뜻 → 오프셋 무효.
    public var sleepAtRecordStartNs: Int64

    /// 프레임 시각을 어디서 얻었는지. 예: "CMSampleBufferPresentationTimeStamp"
    public var timestampSource: String
    /// ★ 시계 도메인 실측차 (CLOCK_UPTIME_RAW − CMClockGetHostTimeClock).
    ///   0 에 가까우면 변환이 필요 없다는 증거. 아이폰 11 실측 −458 ns.
    public var timestampDomainDeltaNs: Int64

    // ── 촬영 설정 ───────────────────────────────────────────────────────────
    public var targetFps: Int
    public var width: Int
    public var height: Int
    public var cameraDeviceType: String
    public var fieldOfViewDeg: Double
    public var isBinned: Bool
    public var exposureDurationNs: Int64
    public var iso: Double
    public var lensPosition: Double
    public var focusLocked: Bool
    public var whiteBalanceLocked: Bool
    public var exposureLocked: Bool
    /// "off" / "standard" / "cinematic"
    public var stabilization: String

    // ── 예약 시작 ───────────────────────────────────────────────────────────
    public var requestedStartAtMasterNs: Int64?
    public var requestedStartAtSlaveNs: Int64?
    /// 실제로 기록된 첫 프레임의 시각 (이 기기 시계)
    public var firstFramePtsNs: Int64?

    // ── 결과 ────────────────────────────────────────────────────────────────
    public var droppedFrameCount: Int
    public var thermalAtStart: String
    public var thermalAtEnd: String
    public var batteryAtStart: Double
    public var batteryAtEnd: Double

    // ── 프레임 ──────────────────────────────────────────────────────────────
    /// `[[프레임번호, 시각ns], ...]`
    ///
    /// 왜 배열의 배열인가: 객체 배열(`[{"i":0,"t":123}]`)보다 2배 이상 작습니다.
    /// 60fps 로 3분이면 10,800 프레임이라 크기가 의미 있게 차이 납니다.
    public var frames: [[Int64]]

    public init(schemaVersion: Int = Sidecar.currentSchemaVersion,
                deviceId: String, deviceName: String, model: String,
                osVersion: String, appVersion: String,
                sessionId: String, role: String,
                clock: String, clockOffsetNs: Int64, clockUncertaintyNs: Int64,
                clockMinRttNs: Int64, clockMeasuredAtNs: Int64,
                sleepAtSyncNs: Int64, sleepAtRecordStartNs: Int64,
                timestampSource: String, timestampDomainDeltaNs: Int64,
                targetFps: Int, width: Int, height: Int,
                cameraDeviceType: String, fieldOfViewDeg: Double, isBinned: Bool,
                exposureDurationNs: Int64, iso: Double, lensPosition: Double,
                focusLocked: Bool, whiteBalanceLocked: Bool, exposureLocked: Bool,
                stabilization: String,
                requestedStartAtMasterNs: Int64? = nil,
                requestedStartAtSlaveNs: Int64? = nil,
                firstFramePtsNs: Int64? = nil,
                droppedFrameCount: Int = 0,
                thermalAtStart: String = "", thermalAtEnd: String = "",
                batteryAtStart: Double = -1, batteryAtEnd: Double = -1,
                frames: [[Int64]] = []) {
        self.schemaVersion = schemaVersion
        self.deviceId = deviceId
        self.deviceName = deviceName
        self.model = model
        self.osVersion = osVersion
        self.appVersion = appVersion
        self.sessionId = sessionId
        self.role = role
        self.clock = clock
        self.clockOffsetNs = clockOffsetNs
        self.clockUncertaintyNs = clockUncertaintyNs
        self.clockMinRttNs = clockMinRttNs
        self.clockMeasuredAtNs = clockMeasuredAtNs
        self.sleepAtSyncNs = sleepAtSyncNs
        self.sleepAtRecordStartNs = sleepAtRecordStartNs
        self.timestampSource = timestampSource
        self.timestampDomainDeltaNs = timestampDomainDeltaNs
        self.targetFps = targetFps
        self.width = width
        self.height = height
        self.cameraDeviceType = cameraDeviceType
        self.fieldOfViewDeg = fieldOfViewDeg
        self.isBinned = isBinned
        self.exposureDurationNs = exposureDurationNs
        self.iso = iso
        self.lensPosition = lensPosition
        self.focusLocked = focusLocked
        self.whiteBalanceLocked = whiteBalanceLocked
        self.exposureLocked = exposureLocked
        self.stabilization = stabilization
        self.requestedStartAtMasterNs = requestedStartAtMasterNs
        self.requestedStartAtSlaveNs = requestedStartAtSlaveNs
        self.firstFramePtsNs = firstFramePtsNs
        self.droppedFrameCount = droppedFrameCount
        self.thermalAtStart = thermalAtStart
        self.thermalAtEnd = thermalAtEnd
        self.batteryAtStart = batteryAtStart
        self.batteryAtEnd = batteryAtEnd
        self.frames = frames
    }
}

// MARK: - 파생 정보

public extension Sidecar {

    var frameCount: Int { frames.count }

    /// 프레임 시각만 뽑기
    var timestampsNs: [Int64] { frames.compactMap { $0.count >= 2 ? $0[1] : nil } }

    /// 이 기기 시각 -> 마스터(공통) 시각
    func toMasterNs(_ slaveNs: Int64) -> Int64 { slaveNs + clockOffsetNs }

    /// 공통 시간축으로 옮긴 프레임 시각
    var timestampsMasterNs: [Int64] { timestampsNs.map(toMasterNs) }

    var durationNs: Int64 {
        let t = timestampsNs
        guard t.count >= 2 else { return 0 }
        return t[t.count - 1] - t[0]
    }

    /// ★ 동기 이후 폰이 잤는가. 잤다면 clockOffsetNs 는 무효입니다.
    ///
    /// ★★ 반드시 허용 오차를 써야 합니다.
    ///
    /// 처음에 `sleepAtRecordStartNs != sleepAtSyncNs` 로 썼는데, 이 값은
    /// 시계 두 개를 연달아 읽어 빼는 것이라 **매번 수십 ns 씩 다릅니다**.
    /// 그래서 잔 적이 없어도 항상 '잤다'가 되어 모든 촬영이 치명으로 거부됐습니다.
    /// (2026-09-24 3단계 시험에서 두 번 연속 "사용 불가"가 난 원인)
    ///
    /// 테스트는 두 값을 똑같이 넣어서 통과했습니다. 현실에서 같을 수 없는
    /// 값을 같게 놓고 시험한 것이 사각지대였습니다.
    var sleptSinceSync: Bool {
        MonotonicClock.didSleep(from: sleepAtSyncNs, to: sleepAtRecordStartNs)
    }

    /// 잔 시간
    var sleepSinceSyncNs: Int64 { sleepAtRecordStartNs - sleepAtSyncNs }

    /// 오프셋 측정 후 녹화 시작까지 걸린 시간 (이 기기 시계 기준)
    func syncAgeNs(atSlaveNs: Int64) -> Int64 { atSlaveNs - clockMeasuredAtNs }

    var intervalStats: FrameIntervalStats {
        ClockSync.frameIntervalStats(timestampsNs: timestampsNs)
    }
}

// MARK: - 검증

/// 사이드카 검증 결과 한 건.
public struct SidecarIssue: Equatable, Sendable {
    public enum Severity: String, Sendable { case fatal, warning, info }
    public let severity: Severity
    public let code: String
    public let message: String

    public init(_ severity: Severity, _ code: String, _ message: String) {
        self.severity = severity
        self.code = code
        self.message = message
    }
}

public extension Sidecar {

    /// ★ 검증.
    ///
    /// 왜 필요한가: 사이드카가 조용히 망가지는 경우가 많습니다.
    /// 프레임이 0개, 시각이 역행, fps 가 목표와 다름, 오프셋이 낡음 —
    /// 전부 예외 없이 그냥 "이상한 3D"로 끝납니다.
    /// PC 파이프라인이 돌기 **전에** 걸러야 합니다.
    ///
    /// server/mocapsync/sidecar.py 의 validate() 와 같은 판정을 내야 합니다.
    func validate(targetFpsTolerance: Double = 0.05) -> [SidecarIssue] {
        var out: [SidecarIssue] = []

        if schemaVersion != Sidecar.currentSchemaVersion {
            out.append(.init(.warning, "schema_version",
                "사이드카 형식 버전이 \(schemaVersion) 입니다 (현재 \(Sidecar.currentSchemaVersion))."))
        }

        if deviceId.isEmpty {
            out.append(.init(.fatal, "no_device_id",
                "deviceId 가 비었습니다. PC 가 캘리브레이션을 매칭할 수 없습니다."))
        }

        // ── 프레임 ──────────────────────────────────────────────────────────
        if frames.isEmpty {
            out.append(.init(.fatal, "no_frames",
                "프레임이 0개입니다. 녹화가 실제로 되지 않았습니다."))
            return out    // 아래 검사들이 의미 없어집니다
        }

        if let bad = frames.first(where: { $0.count != 2 }) {
            out.append(.init(.fatal, "frame_shape",
                "프레임 항목은 [번호, 시각] 2개여야 하는데 \(bad.count)개인 항목이 있습니다."))
            return out
        }

        let ts = timestampsNs

        // 시각이 반드시 증가해야 합니다. 안 그러면 보간이 폭발합니다.
        for i in 1..<ts.count where ts[i] <= ts[i - 1] {
            out.append(.init(.fatal, "non_monotonic",
                "프레임 시각이 증가하지 않습니다: \(i - 1)번 \(ts[i - 1]) → \(i)번 \(ts[i])."
                + " 리샘플러의 보간이 불가능합니다."))
            break
        }

        // 프레임 번호는 0 부터 1씩 증가해야 합니다 (드롭은 droppedFrameCount 로 셉니다)
        let idx = frames.map { $0[0] }
        if idx.first != 0 {
            out.append(.init(.warning, "index_start",
                "프레임 번호가 0 이 아니라 \(idx.first ?? -1) 부터 시작합니다."))
        }
        var gapCount = 0
        for i in 1..<idx.count where idx[i] != idx[i - 1] + 1 { gapCount += 1 }
        if gapCount > 0 {
            out.append(.init(.warning, "index_gap",
                "프레임 번호에 \(gapCount)곳의 건너뜀이 있습니다."))
        }

        // ── fps ─────────────────────────────────────────────────────────────
        let st = intervalStats
        if targetFps > 0, st.estimatedFps > 0 {
            let rel = abs(st.estimatedFps - Double(targetFps)) / Double(targetFps)
            if rel > targetFpsTolerance {
                out.append(.init(.warning, "fps_mismatch",
                    String(format: "실측 %.2f fps 가 목표 %d fps 와 %.1f%% 차이납니다.",
                           st.estimatedFps, targetFps, rel * 100)))
            }
        }
        if targetFps < 60 {
            out.append(.init(.warning, "fps_below_60",
                "목표 fps 가 \(targetFps) 입니다. Pose2Sim 공식 문서는 60Hz 미만에서 정확도가 떨어진다고 밝힙니다."))
        }
        if st.suspectedDrops > 0 {
            let sev: SidecarIssue.Severity =
                Double(st.suspectedDrops) / Double(max(1, st.count)) > 0.02 ? .warning : .info
            out.append(.init(sev, "frame_drops",
                String(format: "간격이 중앙값의 1.5배를 넘는 곳이 %d곳입니다 (최대 %.1f ms). 발열 스로틀링 의심.",
                       st.suspectedDrops, st.maxIntervalMs)))
        }
        if droppedFrameCount > 0 {
            out.append(.init(.warning, "reported_drops",
                "카메라가 \(droppedFrameCount)개 프레임을 버렸다고 보고했습니다."))
        }

        // ── 시계 ────────────────────────────────────────────────────────────
        //
        // ★ 가장 놓치기 쉬운 실패: 오프셋이 낡은 것.
        //   폰이 자면 CLOCK_UPTIME_RAW 가 멈추므로 이전 오프셋이 그 즉시 무효가 됩니다.
        //   (2026-09-24 실측: 52분 중 34분을 자면서 오프셋이 34분 어긋남)
        if sleptSinceSync {
            out.append(.init(.fatal, "slept_since_sync",
                String(format: "동기 측정 후 녹화 시작까지 폰이 %.1f초 잠들었습니다. "
                       + "CLOCK_UPTIME_RAW 는 절전 중 멈추므로 clockOffsetNs 가 무효입니다. "
                       + "촬영 직전에 동기를 다시 해야 합니다.",
                       Double(sleepSinceSyncNs) / 1e9)))
        }

        if clockUncertaintyNs <= 0 {
            out.append(.init(.fatal, "no_clock_sync",
                "clockUncertaintyNs 가 0 이하입니다. 클럭 동기를 하지 않았거나 측정에 실패했습니다."))
        } else if clockUncertaintyNs > Sidecar.halfFrame60Ns {
            // 반 프레임(60fps 에서 8.33ms)을 넘으면 영상 기반 보정으로도
            // 프레임을 잘못 짝지을 수 있습니다.
            out.append(.init(.fatal, "clock_uncertainty_over_half_frame",
                String(format: "오차 상한 %.3f ms 가 반 프레임(8.333 ms)을 넘습니다. "
                       + "프레임 짝짓기가 모호해집니다.", clockUncertaintyNs.ms)))
        } else if clockUncertaintyNs > Sidecar.degradedUncertaintyNs {
            // 실측 기준선(4.076~5.989 ms RTT → 2.038~2.995 ms)보다 나쁩니다.
            // 링크가 평소보다 안 좋다는 신호입니다.
            out.append(.init(.warning, "clock_uncertainty_degraded",
                String(format: "오차 상한 %.3f ms 가 실측 기준선 %.1f ms 보다 나쁩니다. "
                       + "WiFi 상태가 평소보다 안 좋습니다. 촬영 전에 동기를 다시 해 보세요.",
                       clockUncertaintyNs.ms, Sidecar.degradedUncertaintyNs.ms)))
        } else if clockUncertaintyNs > Sidecar.targetUncertaintyNs {
            // ★ 정보로만 남깁니다. 경고로 올리면 모든 정상 촬영에 붙습니다.
            //   실측된 이 경로의 바닥이 4.076 ms RTT = 2.038 ms 상한이므로,
            //   2 ms 목표는 구조적으로 거의 항상 초과합니다 (DESIGN.md §3.12).
            //   항상 울리는 경고는 아무도 보지 않게 되므로 info 로 둡니다.
            out.append(.init(.info, "clock_uncertainty_over_target",
                String(format: "오차 상한 %.3f ms 가 설계 목표 2 ms 를 넘습니다. "
                       + "공유기 WiFi 에서는 정상 범위입니다 (실측 바닥 2.038 ms).",
                       clockUncertaintyNs.ms)))
        }

        if abs(timestampDomainDeltaNs) > NS.perMilli {
            out.append(.init(.fatal, "clock_domain_mismatch",
                String(format: "동기 시계와 프레임 시계의 도메인 차이가 %.3f ms 입니다. "
                       + "같은 도메인이어야 합니다 (규약 §1).", timestampDomainDeltaNs.ms)))
        }

        // 프레임 시각이 오프셋 측정 시각보다 앞서면 순서가 뒤바뀐 것입니다
        if let first = ts.first, first < clockMeasuredAtNs {
            out.append(.init(.warning, "frames_before_sync",
                "첫 프레임이 오프셋 측정보다 먼저 찍혔습니다. 동기 → 녹화 순서를 확인하세요."))
        }

        // ── 동기 나이와 드리프트 ────────────────────────────────────────────
        //
        // ★ 가장 안 보이는 실패 모드입니다.
        //
        // 두 기기의 수정발진자 주파수가 미세하게 다릅니다. 규격은 보통 ±20 ppm 이고
        // 상대 드리프트는 최악 40 ppm 입니다. 1초에 40 µs 씩 조용히 어긋납니다.
        //
        //   2 ms 예산을 소진하는 시간 = 2 ms / 40 ppm = 50초
        //   반 프레임(8.33 ms)을 소진하는 시간 = 약 3분 30초
        //
        // 오프셋을 재고 한참 뒤에 촬영하면 측정 상한은 좋은데 실제로는 어긋납니다.
        // 측정값만 보면 절대 알 수 없으므로 **나이로** 판정합니다.
        //
        // 사이드카에는 드리프트를 더하지 않은 원본 상한이 들어 있고,
        // clockMeasuredAtNs 와 첫 프레임 시각이 있으므로 나이가 계산됩니다.
        if clockUncertaintyNs > 0, let first = ts.first, first >= clockMeasuredAtNs,
           clockMeasuredAtNs > 0 {
            let age = first - clockMeasuredAtNs
            let drift = Int64(Double(age) * Sidecar.assumedDriftPpm / 1e6)
            let effective = clockUncertaintyNs + drift

            if effective > Sidecar.halfFrame60Ns {
                out.append(.init(.fatal, "sync_too_old",
                    String(format: "동기를 %.0f초 전에 측정했습니다. 수정발진자 차이(%.0f ppm 가정)로 "
                           + "최악 %.2f ms 드리프트가 쌓여 실효 상한이 %.2f ms 입니다. "
                           + "반 프레임(8.333 ms)을 넘으므로 프레임을 잘못 짝지을 수 있습니다. "
                           + "촬영 직전에 동기를 다시 하세요.",
                           Double(age) / 1e9, Sidecar.assumedDriftPpm,
                           drift.ms, effective.ms)))
            } else if age > 60 * NS.perSecond {
                out.append(.init(.warning, "sync_aging",
                    String(format: "동기를 %.0f초 전에 측정했습니다. 최악 %.2f ms 드리프트가 "
                           + "쌓였을 수 있어 실효 상한은 %.2f ms 입니다. "
                           + "다음에는 촬영 직전에 동기하세요.",
                           Double(age) / 1e9, drift.ms, effective.ms)))
            }
        }

        // ── 예약 시작 ───────────────────────────────────────────────────────
        if let want = requestedStartAtSlaveNs, let got = firstFramePtsNs {
            let late = got - want
            if late < 0 {
                out.append(.init(.fatal, "started_early",
                    String(format: "첫 프레임이 예약 시각보다 %.3f ms 이릅니다. 논리 오류입니다.",
                           (-late).ms)))
            } else if late > Int64(2.0 * 1e9 / Double(max(targetFps, 1))) {
                // 프레임 2개 분량보다 늦으면 예약이 제대로 안 걸린 것
                out.append(.init(.warning, "started_late",
                    String(format: "첫 프레임이 예약 시각보다 %.3f ms 늦습니다 (프레임 간격 %.3f ms).",
                           late.ms, 1000.0 / Double(max(targetFps, 1)))))
            }
        }

        // ── 촬영 설정 ───────────────────────────────────────────────────────
        if stabilization != "off" {
            out.append(.init(.fatal, "stabilization_on",
                "안정화가 '\(stabilization)' 입니다. 프레임마다 화면이 변형되어 캘리브레이션이 무의미해집니다."))
        }
        if !exposureLocked {
            out.append(.init(.warning, "exposure_not_locked",
                "노출이 고정되지 않았습니다. 밝기가 변하면 2D 검출이 흔들립니다."))
        }
        if !whiteBalanceLocked {
            out.append(.init(.warning, "wb_not_locked", "화이트밸런스가 고정되지 않았습니다."))
        }
        // 고정초점 렌즈는 focusLocked=false 가 정상이므로 경고하지 않습니다.
        // (초광각·전면은 초점 기구가 없어 잠글 대상이 없습니다 — DESIGN.md §3.10)
        if exposureDurationNs > 2_000_000 {
            out.append(.init(.warning, "shutter_too_slow",
                String(format: "셔터 %.0f µs (1/%.0f초) 가 1/500초보다 느립니다. 모션블러가 생깁니다.",
                       Double(exposureDurationNs) / 1000.0,
                       1e9 / Double(max(exposureDurationNs, 1)))))
        }
        if cameraDeviceType.contains("Dual") || cameraDeviceType.contains("Triple") {
            out.append(.init(.fatal, "virtual_camera",
                "합성(가상) 카메라 '\(cameraDeviceType)' 로 촬영했습니다. "
                + "촬영 중 렌즈가 바뀌면 초점거리·왜곡이 통째로 달라집니다."))
        }

        if durationNs < 5 * NS.perSecond {
            out.append(.init(.warning, "too_short",
                String(format: "길이 %.2f초. 촬영 규칙은 5초 이상입니다.",
                       Double(durationNs) / 1e9)))
        }

        return out
    }

    /// 치명 문제가 하나도 없으면 통과.
    var isUsable: Bool { !validate().contains { $0.severity == .fatal } }

    /// 사람이 읽는 검증 보고
    func validationReport() -> [String] {
        let issues = validate()
        if issues.isEmpty { return ["문제 없음 ✔"] }
        return issues.map { i in
            let mark = i.severity == .fatal ? "[치명]" : (i.severity == .warning ? "[경고]" : "[참고]")
            return "\(mark) \(i.code): \(i.message)"
        }
    }
}

// MARK: - 파일 입출력

public extension Sidecar {

    /// JSON 인코딩. 키를 정렬해서 사람이 비교하기 쉽게 합니다.
    func encoded() throws -> Data {
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys, .withoutEscapingSlashes, .prettyPrinted]
        return try enc.encode(self)
    }

    static func decoded(from data: Data) throws -> Sidecar {
        try JSONDecoder().decode(Sidecar.self, from: data)
    }
}
