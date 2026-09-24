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
    /// ★ 측정 전에 버리는 왕복 횟수.
    ///
    /// iOS 는 WiFi 무선을 공격적으로 절전시킵니다. 유휴 상태에서 첫 패킷은
    /// 무선을 깨우는 시간이 붙어 몇 ms 느립니다. 그 표본이 섞이면 최소 RTT 는
    /// 안 나빠지지만(최소값이니까) 분포 해석이 오염됩니다.
    /// 더 중요하게는, 왕복 사이 간격이 길면 매번 다시 절전에 들어가므로
    /// 워밍업 + 무간격 연사가 최소 RTT 를 실제로 낮춥니다.
    public static let warmupCount = 10
    /// 왕복 사이 간격(ms). 0 = 연사.
    ///
    /// 처음에는 5ms 였습니다. "큐잉 상태를 다양하게 샘플링한다"는 의도였지만,
    /// 우리는 **최소** RTT 만 쓰기 때문에 다양성은 이득이 없고
    /// 무선이 절전에 드는 손해만 있습니다. 기본값을 0 으로 바꿨습니다.
    public static let probeGapMs = 0
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

// ── RTT 분포 (진단 전용) ─────────────────────────────────────────────────────
//
// ★ 왜 필요한가
//
// 최소 RTT 하나만 보면 다음 두 상황을 **구분할 수 없습니다**.
//
//   (가) 이미 물리적 바닥에 도달했다. 왕복을 1000번 해도 더 안 내려간다.
//        -> 코드로는 해결 불가. 네트워크 경로를 바꿔야 한다 (유선 랜, 5GHz).
//
//   (나) 표본이 부족해서 운 나쁘게 높게 나왔다. 분포의 꼬리가 두껍다.
//        -> 왕복 횟수를 늘리면 최소값이 내려간다. 코드로 해결 가능.
//
// 4.810 ms 라는 숫자 하나로는 어느 쪽인지 모릅니다. p0 와 p50 의 간격을 보면
// 압니다. 그래서 이걸 만들었습니다.
//
// 이 계산은 오프셋 추정에 **전혀 영향을 주지 않습니다**. 순수 진단용입니다.
// (따라서 Python 쪽에 대응 구현이 없습니다. clocksync.py 와의 동작 일치
//  요구사항은 SyncEstimate 계산에만 적용됩니다)
// ─────────────────────────────────────────────────────────────────────────────

public struct RttProfile: Equatable, Sendable {
    public let count: Int
    public let p0Ns: Int64
    public let p10Ns: Int64
    public let p50Ns: Int64
    public let p90Ns: Int64
    public let p100Ns: Int64
    /// `bucketEdgesMs` 로 나눈 구간별 개수. 길이는 edges.count + 1 (마지막은 초과분).
    public let buckets: [Int]

    /// 히스토그램 경계 (ms).
    ///
    /// 1 ms 아래까지 촘촘히 둡니다. PC 를 유선 랜으로 바꾸면 RTT 가 1~2 ms 로
    /// 떨어질 수 있는데, 경계가 2 ms 부터면 그 구간이 한 칸에 뭉쳐서
    /// 개선 여부를 볼 수 없습니다.
    /// 4 ms 는 목표 경계선이므로 반드시 경계에 있어야 합니다 (상한 2 ms).
    public static let bucketEdgesMs: [Double] = [0.5, 1, 2, 3, 4, 5, 6, 8, 12, 20, 40]

    public var p0Ms: Double { p0Ns.ms }
    public var p10Ms: Double { p10Ns.ms }
    public var p50Ms: Double { p50Ns.ms }
    public var p90Ms: Double { p90Ns.ms }
    public var p100Ms: Double { p100Ns.ms }

    public static let empty = RttProfile(
        count: 0, p0Ns: 0, p10Ns: 0, p50Ns: 0, p90Ns: 0, p100Ns: 0,
        buckets: Array(repeating: 0, count: RttProfile.bucketEdgesMs.count + 1))

    public init(count: Int, p0Ns: Int64, p10Ns: Int64, p50Ns: Int64,
                p90Ns: Int64, p100Ns: Int64, buckets: [Int]) {
        self.count = count
        self.p0Ns = p0Ns
        self.p10Ns = p10Ns
        self.p50Ns = p50Ns
        self.p90Ns = p90Ns
        self.p100Ns = p100Ns
        self.buckets = buckets
    }

    /// p50 이 p0 보다 얼마나 높은가. 0 이면 분포가 한 점에 모인 것.
    public var headroom: Double {
        guard p0Ns > 0 else { return 0 }
        return Double(p50Ns - p0Ns) / Double(p0Ns)
    }

    /// 분포의 넓이/좁음. 셋 중 하나.
    public enum Shape: String, Sendable {
        case tooFewSamples
        case narrow      // 바닥 도달
        case moderate
        case heavyTail   // 왕복 늘리면 이득
    }

    public var shape: Shape {
        if count < 5 { return .tooFewSamples }
        if headroom < 0.25 { return .narrow }
        if headroom < 1.0 { return .moderate }
        return .heavyTail
    }

    /// ★ "다음에 무엇을 해야 하는가"를 알려주는 문장.
    /// 사용자가 실기기에서 보는 유일한 결론이므로 행동 지시로 씁니다.
    public var diagnosis: String {
        switch shape {
        case .tooFewSamples:
            return "표본이 \(count)개뿐입니다. 판정할 수 없습니다."
        case .narrow:
            return String(format:
                "분포가 좁습니다 (중앙값이 최소값의 %.0f%%). 이미 이 경로의 물리적 바닥입니다. "
                + "왕복 횟수를 늘려도 최소 RTT 는 거의 안 내려갑니다. "
                + "→ 네트워크 경로를 바꿔야 합니다: PC 를 유선 랜에 연결, WiFi 는 5GHz 사용.",
                (1 + headroom) * 100)
        case .moderate:
            return String(format:
                "분포가 보통입니다 (중앙값이 최소값의 %.0f%%). "
                + "왕복 횟수를 2~5배로 늘리면 최소 RTT 가 조금 더 내려갈 여지가 있습니다. "
                + "그것만으로 부족하면 유선 랜을 쓰세요.",
                (1 + headroom) * 100)
        case .heavyTail:
            return String(format:
                "꼬리가 두껍습니다 (중앙값이 최소값의 %.0f%%). 간헐적 지연이 큽니다. "
                + "왕복 횟수를 늘리면 최소 RTT 가 의미 있게 내려갈 가능성이 높습니다. "
                + "공유기 2.4GHz 혼잡과 절전 설정을 의심하세요.",
                (1 + headroom) * 100)
        }
    }

    /// 로그에 넣을 한 줄 요약
    public var summaryLine: String {
        String(format: "RTT n=%d  p0=%.3f p10=%.3f p50=%.3f p90=%.3f max=%.3f ms",
               count, p0Ms, p10Ms, p50Ms, p90Ms, p100Ms)
    }

    /// 로그에 넣을 히스토그램 (텍스트 막대)
    public var histogramLines: [String] {
        guard count > 0 else { return [] }
        let edges = RttProfile.bucketEdgesMs
        var out: [String] = []
        for (i, n) in buckets.enumerated() {
            if n == 0 { continue }
            let label: String
            if i == 0 {
                label = String(format: "      ~%5.1f", edges[0])
            } else if i == buckets.count - 1 {
                label = String(format: "%5.1f~      ", edges[edges.count - 1])
            } else {
                label = String(format: "%5.1f~%5.1f", edges[i - 1], edges[i])
            }
            let bar = String(repeating: "#", count: max(1, n * 30 / count))
            // String(format:) 의 %@ 는 Swift String 브리징에 의존하므로
            // 보간으로 조립합니다. 숫자만 포맷을 씁니다.
            out.append("  \(label) ms | \(String(format: "%3d", n))  \(bar)")
        }
        return out
    }
}

public extension ClockSync {

    /// RTT 분포를 요약합니다. 진단 전용.
    static func rttProfile(_ samples: [TimeSample]) -> RttProfile {
        let rtts = samples.filter { $0.isSane }.map(\.rttNs).sorted()
        guard !rtts.isEmpty else { return .empty }

        let edges = RttProfile.bucketEdgesMs
        var buckets = Array(repeating: 0, count: edges.count + 1)
        for r in rtts {
            let ms = r.ms
            var placed = false
            for (i, e) in edges.enumerated() where ms < e {
                buckets[i] += 1
                placed = true
                break
            }
            if !placed { buckets[edges.count] += 1 }
        }

        return RttProfile(
            count: rtts.count,
            p0Ns: rtts[0],
            p10Ns: percentile(sorted: rtts, 10),
            p50Ns: percentile(sorted: rtts, 50),
            p90Ns: percentile(sorted: rtts, 90),
            p100Ns: rtts[rtts.count - 1],
            buckets: buckets)
    }

    /// 최근접 순위 백분위. 보간하지 않으므로 항상 실제 관측값 중 하나를 돌려줍니다.
    /// (보간하면 "실제로 관측되지 않은 RTT"가 나와서 해석이 애매해집니다)
    static func percentile(sorted xs: [Int64], _ p: Double) -> Int64 {
        guard !xs.isEmpty else { return 0 }
        let idx = Int((p / 100.0 * Double(xs.count - 1)).rounded())
        return xs[min(max(idx, 0), xs.count - 1)]
    }
}
