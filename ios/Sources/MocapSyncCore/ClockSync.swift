import Foundation

// ─────────────────────────────────────────────────────────────────────────────
// 클럭 동기 계산.
//
// server/mocapsync/clocksync.py 의 Swift 포팅입니다.
// **두 구현은 같은 입력에 같은 출력을 내야 합니다.**
// Python 쪽 테스트(server/tests/test_clocksync.py)와 여기 테스트가 같은 성질을
// 검증하므로, 한쪽만 고치면 다른 쪽 테스트가 깨져서 드러납니다.
//
// 수학 (docs/PROTOCOL.md §4):
//     t1 = 슬레이브 송신 (슬레이브 시계)
//     t2 = 마스터 수신   (마스터 시계)
//     t3 = 마스터 송신   (마스터 시계)
//     t4 = 슬레이브 수신 (슬레이브 시계)
//
//     오프셋  θ = ((t2 - t1) + (t3 - t4)) / 2     부호: 마스터 = 슬레이브 + θ
//     왕복시간 δ = (t4 - t1) - (t3 - t2)
//
//     θ̂ = θ + (d_up - d_dn)/2   이고   d_up + d_dn = δ,  d_up,d_dn >= 0
//     ∴ |오차| <= δ/2            <-- 보장된 상한
//
// 즉 최소 RTT 4ms 미만이면 오프셋 오차 2ms 미만이 보장됩니다.
// ─────────────────────────────────────────────────────────────────────────────

public enum SyncConfig {
    /// 설계 목표: 오차 2ms 미만
    public static let targetNs: Int64 = 2 * NS.perMilli
    /// 왕복 횟수 (설계: 20~50회)
    public static let probeCount = 40
    /// 최종 오프셋을 뽑을 최소-RTT 샘플 개수
    public static let bestK = 1
    /// spread 계산에 쓸 샘플 개수
    public static let spreadWindow = 10
    /// 예약 시각까지 최소로 남아 있어야 하는 여유
    public static let minLeadNs: Int64 = 300 * NS.perMilli
    /// 정밀 대기에서 스핀으로 전환하는 남은시간.
    /// iOS 는 mach_wait_until 이 정밀하므로 PC(20ms)보다 작게 잡습니다.
    public static let spinMarginNs: Int64 = 3 * NS.perMilli
}

/// 왕복 1회의 네 시각.
public struct TimeSample: Equatable, Sendable {
    public let seq: Int
    public let t1: Int64
    public let t2: Int64
    public let t3: Int64
    public let t4: Int64

    public init(seq: Int, t1: Int64, t2: Int64, t3: Int64, t4: Int64) {
        self.seq = seq
        self.t1 = t1
        self.t2 = t2
        self.t3 = t3
        self.t4 = t4
    }

    /// 마스터 = 슬레이브 + offsetNs
    public var offsetNs: Int64 {
        // Python 의 // (floor division) 과 맞추기 위해 floorDiv 사용.
        // Swift 의 / 는 0 방향 절삭이라 음수에서 결과가 달라집니다.
        floorDiv((t2 - t1) + (t3 - t4), 2)
    }

    /// 마스터 처리시간을 뺀 순수 네트워크 왕복
    public var rttNs: Int64 { (t4 - t1) - (t3 - t2) }

    public var serverProcessingNs: Int64 { t3 - t2 }

    /// 물리적으로 불가능한 샘플 걸러내기 (시계 점프, 버그)
    public var isSane: Bool {
        if rttNs < 0 { return false }
        if serverProcessingNs < 0 { return false }
        if t4 < t1 { return false }
        if rttNs > 10 * NS.perSecond { return false }
        return true
    }
}

/// Python 의 `//` 와 동일한 내림 나눗셈. 음수에서 Swift `/` 와 다릅니다.
@inlinable
func floorDiv(_ a: Int64, _ b: Int64) -> Int64 {
    let q = a / b
    let r = a % b
    return (r != 0 && ((r < 0) != (b < 0))) ? q - 1 : q
}

/// 최종 동기 결과.
public struct SyncEstimate: Equatable, Sendable {
    public let offsetNs: Int64
    public let minRttNs: Int64
    /// 보장된 오차 상한 = minRtt/2. "이 값보다 나쁠 수 없다"고 말할 수 있는 숫자.
    public let uncertaintyNs: Int64
    /// 최소 RTT 샘플들의 오프셋 최대-최소. 실제 관측된 흔들림.
    public let spreadNs: Int64
    public let samplesTotal: Int
    public let samplesUsed: Int
    public let samplesRejected: Int
    public let medianRttNs: Int64

    public var offsetMs: Double { offsetNs.ms }
    public var minRttMs: Double { minRttNs.ms }
    public var uncertaintyMs: Double { uncertaintyNs.ms }
    public var spreadMs: Double { spreadNs.ms }

    /// 성공 판정.
    ///
    /// ★ samplesUsed == 0 을 반드시 먼저 걸러야 합니다.
    /// 측정이 하나도 없으면 minRtt=0, uncertainty=0 이 되어
    /// "0 < 2ms 이므로 통과"라는 **거짓 성공**이 나옵니다.
    /// (Python 쪽 테스트가 실제로 이 버그를 잡았습니다)
    public func meetsTarget(_ targetNs: Int64 = SyncConfig.targetNs) -> Bool {
        if samplesUsed == 0 { return false }
        return uncertaintyNs < targetNs
    }

    public func verdict(_ targetNs: Int64 = SyncConfig.targetNs) -> String {
        if samplesUsed == 0 { return "측정 실패 (유효 샘플 없음)" }
        if meetsTarget(targetNs) {
            return String(format: "통과 — 오차 상한 %.3f ms < 목표 %.1f ms",
                          uncertaintyMs, targetNs.ms)
        }
        return String(format: "미달 — 오차 상한 %.3f ms >= 목표 %.1f ms (최소 RTT %.3f ms 를 줄여야 합니다)",
                      uncertaintyMs, targetNs.ms, minRttMs)
    }

    public static let empty = SyncEstimate(
        offsetNs: 0, minRttNs: 0, uncertaintyNs: 0, spreadNs: 0,
        samplesTotal: 0, samplesUsed: 0, samplesRejected: 0, medianRttNs: 0)
}

public enum ClockSync {

    /// 샘플 묶음에서 최종 오프셋을 추정합니다.
    ///
    /// 왜 최소 RTT 샘플만 쓰는가: 네트워크 큐잉은 지연을 '늘리기만' 합니다.
    /// 따라서 RTT 가 가장 작은 샘플이 큐잉에 가장 덜 오염되었고,
    /// 그 샘플의 오차 상한(δ/2)도 가장 작습니다.
    public static func estimate(_ samples: [TimeSample],
                                bestK: Int = SyncConfig.bestK) -> SyncEstimate {
        let good = samples.filter { $0.isSane }
        let rejected = samples.count - good.count

        guard !good.isEmpty else {
            return SyncEstimate(
                offsetNs: 0, minRttNs: 0, uncertaintyNs: 0, spreadNs: 0,
                samplesTotal: samples.count, samplesUsed: 0,
                samplesRejected: rejected, medianRttNs: 0)
        }

        let byRtt = good.sorted { $0.rttNs < $1.rttNs }
        let k = max(1, min(bestK, byRtt.count))
        let chosen = Array(byRtt.prefix(k))

        let offset = median(chosen.map(\.offsetNs))
        let minRtt = byRtt[0].rttNs

        let window = Array(byRtt.prefix(min(SyncConfig.spreadWindow, byRtt.count)))
        let offsetsW = window.map(\.offsetNs)
        let spread = (offsetsW.max() ?? 0) - (offsetsW.min() ?? 0)

        return SyncEstimate(
            offsetNs: offset,
            minRttNs: minRtt,
            uncertaintyNs: floorDiv(minRtt, 2),
            spreadNs: spread,
            samplesTotal: samples.count,
            samplesUsed: k,
            samplesRejected: rejected,
            medianRttNs: median(byRtt.map(\.rttNs)))
    }

    /// Python statistics.median 과 같은 동작 (짝수 개면 두 중앙값의 평균)
    static func median(_ xs: [Int64]) -> Int64 {
        guard !xs.isEmpty else { return 0 }
        let s = xs.sorted()
        let n = s.count
        if n % 2 == 1 { return s[n / 2] }
        return floorDiv(s[n / 2 - 1] + s[n / 2], 2)
    }

    // ── 시각 변환 ────────────────────────────────────────────────────────────

    /// 마스터 시각 -> 슬레이브 시각.  마스터 = 슬레이브 + offset 이므로 뺍니다.
    @inlinable
    public static func masterToSlaveNs(_ masterNs: Int64, offsetNs: Int64) -> Int64 {
        masterNs - offsetNs
    }

    @inlinable
    public static func slaveToMasterNs(_ slaveNs: Int64, offsetNs: Int64) -> Int64 {
        slaveNs + offsetNs
    }
}

/// 예약 시작 명령이 실행 가능한지 판정한 결과.
public struct ScheduleCheck: Equatable, Sendable {
    public let ok: Bool
    public let startAtSlaveNs: Int64
    public let leadNs: Int64
    public let reason: String

    public var leadMs: Double { leadNs.ms }

    public init(ok: Bool, startAtSlaveNs: Int64, leadNs: Int64, reason: String = "") {
        self.ok = ok
        self.startAtSlaveNs = startAtSlaveNs
        self.leadNs = leadNs
        self.reason = reason
    }
}

public extension ClockSync {
    /// 예약 시작을 받아들일 수 있는지 판정.
    ///
    /// 늦게 도착한 명령을 억지로 따라가면 오히려 동기가 틀어집니다.
    /// 남은 시간이 부족하면 거부하고 마스터가 다시 예약하게 하는 것이 맞습니다.
    static func checkSchedule(startAtMasterNs: Int64,
                              offsetNs: Int64,
                              nowSlaveNs: Int64,
                              minLeadNs: Int64 = SyncConfig.minLeadNs) -> ScheduleCheck {
        let startAtSlave = masterToSlaveNs(startAtMasterNs, offsetNs: offsetNs)
        let lead = startAtSlave - nowSlaveNs
        if lead < minLeadNs {
            return ScheduleCheck(
                ok: false, startAtSlaveNs: startAtSlave, leadNs: lead,
                reason: String(format: "lead_too_small: 남은 %.1f ms < 최소 %.1f ms",
                               lead.ms, minLeadNs.ms))
        }
        return ScheduleCheck(ok: true, startAtSlaveNs: startAtSlave, leadNs: lead)
    }
}

// ── 프레임 간격 통계 ─────────────────────────────────────────────────────────

public struct FrameIntervalStats: Equatable, Sendable {
    public let count: Int
    public let meanIntervalMs: Double
    public let medianIntervalMs: Double
    public let minIntervalMs: Double
    public let maxIntervalMs: Double
    public let estimatedFps: Double
    /// 중앙값의 1.5배를 넘는 간격 = 프레임을 흘린 것으로 의심
    public let suspectedDrops: Int
}

public extension ClockSync {
    /// 프레임 간격 통계. 발열 스로틀링으로 인한 프레임 드롭을 잡아냅니다.
    static func frameIntervalStats(timestampsNs ts: [Int64]) -> FrameIntervalStats {
        guard ts.count >= 2 else {
            return FrameIntervalStats(count: ts.count, meanIntervalMs: 0,
                                      medianIntervalMs: 0, minIntervalMs: 0,
                                      maxIntervalMs: 0, estimatedFps: 0,
                                      suspectedDrops: 0)
        }
        var d: [Int64] = []
        d.reserveCapacity(ts.count - 1)
        for i in 0..<(ts.count - 1) { d.append(ts[i + 1] - ts[i]) }

        let med = median(d)
        let drops = d.filter { Double($0) > Double(med) * 1.5 }.count
        let mean = Double(d.reduce(0, +)) / Double(d.count)

        return FrameIntervalStats(
            count: ts.count,
            meanIntervalMs: mean / Double(NS.perMilli),
            medianIntervalMs: med.ms,
            minIntervalMs: (d.min() ?? 0).ms,
            maxIntervalMs: (d.max() ?? 0).ms,
            estimatedFps: med > 0 ? 1e9 / Double(med) : 0,
            suspectedDrops: drops)
    }
}
