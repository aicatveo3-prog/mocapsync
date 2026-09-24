import Foundation

// ─────────────────────────────────────────────────────────────────────────────
// 측정해 둔 클럭 오프셋 한 건과, 그게 **아직 유효한지** 판정하는 규칙.
//
// ★ 왜 별도 타입이 필요한가
//
// 오프셋은 "한 번 재면 계속 쓰는 값"이 아닙니다. 세 가지 이유로 낡습니다.
//
//   (1) 재부팅   CLOCK_UPTIME_RAW 가 0 으로 초기화됩니다. 오프셋이 완전히 무의미.
//   (2) 절전     절전 중 시계가 멈춥니다. 잔 만큼 그대로 어긋납니다.
//                2026-09-24 실측: 52분 중 34분을 자면서 오프셋이 34분 이동.
//   (3) 드리프트  두 기기의 수정발진자 주파수가 미세하게 다릅니다.
//                시간이 지날수록 천천히 어긋납니다. ★이게 가장 안 보이는 문제입니다.
//
// (1)(2)는 값이 크게 튀므로 탐지가 쉽습니다. (3)은 조용히 누적됩니다.
//
// ── 드리프트가 얼마나 문제인가 ──────────────────────────────────────────────
//
// 휴대기기 수정발진자 규격은 보통 ±20 ppm 입니다. 두 기기의 상대 드리프트는
// 최악의 경우 40 ppm 까지 벌어질 수 있습니다.
//
//   40 ppm = 1초에 40 µs 씩 어긋남
//   우리 오차 예산 2 ms 를 다 쓰는 시간 = 2 ms / 40 ppm = 50초
//
// 즉 **오프셋을 재고 1분 안에 촬영을 시작해야** 오차 예산 안에 듭니다.
// 이건 규격 기준 최악값이고 실제 드리프트는 보통 훨씬 작지만, 추측으로
// 넘기지 않습니다. `driftPpm(from:to:)` 로 **실측**할 수 있게 만들었습니다.
// 두 번 측정한 기록을 비교하면 이 기기의 실제 드리프트가 나옵니다.
//
// 설계 문서의 "촬영 5초 이상 ~ 몇 분 이내" 규칙이 여기서 수치로 설명됩니다.
// ─────────────────────────────────────────────────────────────────────────────

public struct ClockOffsetRecord: Codable, Equatable, Sendable {

    /// 마스터시각 = 이 기기 시각 + offsetNs
    public var offsetNs: Int64
    /// 증명된 오차 상한 (= 최소RTT/2)
    public var uncertaintyNs: Int64
    public var minRttNs: Int64
    /// 측정한 시점 (이 기기 시계)
    public var measuredAtSlaveNs: Int64
    /// 측정 시점의 누적 절전시간
    public var sleepAtSyncNs: Int64
    /// 측정 시점의 부팅 시각 (재부팅 탐지용)
    public var bootTimeEpochNs: Int64
    /// 어느 마스터에 붙어서 측정했는지. 다른 마스터의 오프셋을 섞으면 안 됩니다.
    public var masterId: String

    public init(offsetNs: Int64, uncertaintyNs: Int64, minRttNs: Int64,
                measuredAtSlaveNs: Int64, sleepAtSyncNs: Int64,
                bootTimeEpochNs: Int64, masterId: String) {
        self.offsetNs = offsetNs
        self.uncertaintyNs = uncertaintyNs
        self.minRttNs = minRttNs
        self.measuredAtSlaveNs = measuredAtSlaveNs
        self.sleepAtSyncNs = sleepAtSyncNs
        self.bootTimeEpochNs = bootTimeEpochNs
        self.masterId = masterId
    }
}

// MARK: - 신선도 판정

public extension ClockOffsetRecord {

    /// 규격 기준 최악 상대 드리프트. 두 기기 각각 ±20 ppm 가정.
    static let worstCaseDriftPpm: Double = 40

    /// 이 나이까지는 신선하다고 봅니다.
    ///
    /// 근거: 40 ppm 최악값에서 2 ms 를 소진하는 시간이 50초입니다.
    /// 60초로 잡으면 최악의 경우 2.4 ms 누적이라 반 프레임(8.33 ms)에는
    /// 한참 못 미칩니다. 실측 드리프트를 알게 되면 이 값을 올릴 수 있습니다.
    static let freshMaxAgeNs: Int64 = 60 * NS.perSecond

    /// 이 나이를 넘으면 촬영을 막습니다.
    ///
    /// 근거: 40 ppm 에서 10분이면 24 ms = 1.4 프레임. 프레임을 잘못 짝짓게 됩니다.
    static let hardMaxAgeNs: Int64 = 600 * NS.perSecond

    enum Freshness: Equatable, Sendable {
        /// 쓸 수 있습니다.
        case fresh(ageNs: Int64)
        /// 쓸 수 있지만 드리프트가 쌓였을 수 있습니다. 다시 재는 것을 권합니다.
        case aging(ageNs: Int64, estimatedDriftNs: Int64)
        /// 너무 오래됐습니다.
        case tooOld(ageNs: Int64, estimatedDriftNs: Int64)
        /// 재부팅했습니다. 오프셋이 완전히 무의미합니다.
        case rebooted
        /// 절전을 거쳤습니다. 잔 만큼 어긋났습니다.
        case slept(sleptNs: Int64)
        /// 측정 자체가 실패했거나 없습니다.
        case invalid

        public var canRecord: Bool {
            switch self {
            case .fresh, .aging: return true
            default: return false
            }
        }
    }

    /// ★ 지금 이 오프셋을 써도 되는지 판정합니다.
    ///
    /// 검사 순서가 중요합니다. 재부팅과 절전을 **먼저** 걸러야 합니다.
    /// 그 경우 나이 계산 자체가 의미 없기 때문입니다
    /// (재부팅하면 measuredAtSlaveNs 가 미래 값처럼 보일 수 있습니다).
    func freshness(nowSlaveNs: Int64,
                   nowSleepNs: Int64,
                   nowBootTimeEpochNs: Int64,
                   driftPpm: Double = ClockOffsetRecord.worstCaseDriftPpm) -> Freshness {

        if uncertaintyNs <= 0 { return .invalid }

        // (1) 재부팅 — 가장 먼저.
        //     bootTimeEpochNs 가 0 이면 못 읽은 것이므로 판정하지 않습니다.
        if bootTimeEpochNs != 0, nowBootTimeEpochNs != 0,
           bootTimeEpochNs != nowBootTimeEpochNs {
            return .rebooted
        }

        // (2) 절전 — 누적 절전시간이 늘었으면 그만큼 어긋났습니다.
        let slept = nowSleepNs - sleepAtSyncNs
        // 두 시계를 읽는 사이의 미세한 차이가 있으므로 1ms 여유를 둡니다.
        if slept > NS.perMilli { return .slept(sleptNs: slept) }

        // (3) 시계가 뒤로 갔으면 뭔가 잘못된 것입니다 (재부팅을 놓친 경우 등)
        let age = nowSlaveNs - measuredAtSlaveNs
        if age < 0 { return .rebooted }

        // (4) 드리프트 추정
        let drift = Int64(Double(age) * driftPpm / 1e6)

        if age <= ClockOffsetRecord.freshMaxAgeNs { return .fresh(ageNs: age) }
        if age <= ClockOffsetRecord.hardMaxAgeNs {
            return .aging(ageNs: age, estimatedDriftNs: drift)
        }
        return .tooOld(ageNs: age, estimatedDriftNs: drift)
    }

    /// 드리프트를 더한 실효 오차 상한.
    ///
    /// 사이드카에 넣는 값입니다. 측정 직후의 상한만 적으면 낙관적입니다 —
    /// 시간이 지난 만큼 드리프트가 쌓였고, 그건 측정 상한에 포함되지 않습니다.
    func effectiveUncertaintyNs(ageNs: Int64,
                               driftPpm: Double = ClockOffsetRecord.worstCaseDriftPpm) -> Int64 {
        uncertaintyNs + Int64(Double(max(ageNs, 0)) * driftPpm / 1e6)
    }
}

// MARK: - 드리프트 실측

public extension ClockOffsetRecord {

    /// ★ 두 측정으로 **실제** 상대 드리프트를 계산합니다 (ppm).
    ///
    /// 규격 최악값 40 ppm 은 추측입니다. 같은 폰·같은 마스터로 두 번 재면
    /// 실제 값이 나옵니다.
    ///
    ///   드리프트 = (오프셋 변화) / (경과 시간)
    ///
    /// 절전이나 재부팅을 거친 두 기록을 비교하면 무의미하므로 nil 을 돌려줍니다.
    ///
    /// - Returns: ppm. 양수면 마스터 시계가 이 기기보다 빠릅니다. 비교 불가면 nil.
    static func driftPpm(from a: ClockOffsetRecord,
                         to b: ClockOffsetRecord) -> Double? {
        guard a.uncertaintyNs > 0, b.uncertaintyNs > 0 else { return nil }
        guard a.bootTimeEpochNs == b.bootTimeEpochNs else { return nil }
        // 절전을 거쳤으면 오프셋 변화의 대부분이 절전 때문이라 드리프트가 아닙니다
        guard abs(b.sleepAtSyncNs - a.sleepAtSyncNs) <= NS.perMilli else { return nil }

        let dt = b.measuredAtSlaveNs - a.measuredAtSlaveNs
        // 너무 짧으면 측정 불확실도가 드리프트보다 훨씬 커서 의미가 없습니다.
        //
        // 판단 근거: 오프셋 차이의 불확실도는 두 측정 상한의 합입니다.
        // 상한이 각 2 ms 면 4 ms. 그 4 ms 를 40 ppm 으로 나누면 100초.
        // 즉 100초보다 짧은 간격에서는 드리프트와 측정잡음을 구분할 수 없습니다.
        guard dt >= 30 * NS.perSecond else { return nil }

        let dOffset = Double(b.offsetNs - a.offsetNs)
        return dOffset / Double(dt) * 1e6
    }

    /// 드리프트 측정의 신뢰 구간(ppm). 두 측정의 오차 상한 합에서 나옵니다.
    ///
    /// 실측값이 이 값보다 작으면 "드리프트를 측정하지 못했다"고 말해야 합니다.
    /// 그걸 구분하지 않으면 잡음을 드리프트로 오해합니다.
    static func driftUncertaintyPpm(from a: ClockOffsetRecord,
                                   to b: ClockOffsetRecord) -> Double? {
        let dt = b.measuredAtSlaveNs - a.measuredAtSlaveNs
        guard dt > 0 else { return nil }
        let combined = Double(a.uncertaintyNs + b.uncertaintyNs)
        return combined / Double(dt) * 1e6
    }
}

// MARK: - 사람이 읽는 설명

public extension ClockOffsetRecord.Freshness {

    var summary: String {
        switch self {
        case .invalid:
            return "동기 측정이 없습니다. 촬영 전에 클럭 동기를 하세요."
        case .rebooted:
            return "★ 폰이 재부팅됐습니다. CLOCK_UPTIME_RAW 가 0 으로 초기화되므로 "
                 + "저장된 오프셋은 완전히 무의미합니다. 동기를 다시 하세요."
        case .slept(let ns):
            return String(format: "★ 동기 이후 폰이 %.1f초 잠들었습니다. 절전 중에는 "
                          + "시계가 멈추므로 오프셋이 그만큼 어긋났습니다. 동기를 다시 하세요.",
                          Double(ns) / 1e9)
        case .fresh(let age):
            return String(format: "신선합니다 (%.0f초 전 측정).", Double(age) / 1e9)
        case .aging(let age, let drift):
            return String(format: "%.0f초 전에 측정했습니다. 수정발진자 차이로 최악 %.2f ms "
                          + "쯤 어긋났을 수 있습니다. 다시 재는 것을 권합니다.",
                          Double(age) / 1e9, drift.ms)
        case .tooOld(let age, let drift):
            return String(format: "★ %.0f분 전 측정이라 너무 오래됐습니다. 최악 %.1f ms "
                          + "드리프트가 쌓였을 수 있어 프레임을 잘못 짝지을 위험이 있습니다. "
                          + "동기를 다시 하세요.",
                          Double(age) / 6e10, drift.ms)
        }
    }
}
