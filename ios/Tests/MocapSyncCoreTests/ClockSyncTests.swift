import XCTest
@testable import MocapSyncCore

/// server/tests/test_clocksync.py 와 **같은 성질**을 검증합니다.
/// 한쪽 구현만 고치면 다른 쪽 테스트가 깨져서 드러나게 하는 것이 목적입니다.
///
/// 시뮬레이터가 필요 없으므로 macOS 러너에서 `swift test` 로 수십 초에 끝납니다.
final class ClockSyncTests: XCTestCase {

    let MS = NS.perMilli

    /// 진짜 오프셋과 편도 지연을 지정해 샘플을 합성합니다.
    ///     t2 = t1 + d_up + θ
    ///     t4 = t3 + d_dn - θ
    func makeSample(seq: Int, trueOffsetNs: Int64,
                    dUpNs: Int64, dDnNs: Int64,
                    t1: Int64 = 1_000_000_000,
                    serverProcNs: Int64 = 50_000) -> TimeSample {
        let t2 = t1 + dUpNs + trueOffsetNs
        let t3 = t2 + serverProcNs
        let t4 = t3 + dDnNs - trueOffsetNs
        return TimeSample(seq: seq, t1: t1, t2: t2, t3: t3, t4: t4)
    }

    // ── 1. 기본 수학 ─────────────────────────────────────────────────────────

    func testSymmetricPathIsExact() {
        let trueOffset: Int64 = 12_345_678
        let s = makeSample(seq: 0, trueOffsetNs: trueOffset, dUpNs: MS, dDnNs: MS)
        XCTAssertEqual(s.offsetNs, trueOffset)
        XCTAssertEqual(s.rttNs, 2 * MS)
    }

    func testRttExcludesServerProcessing() {
        let s = makeSample(seq: 0, trueOffsetNs: 0, dUpNs: 3 * MS, dDnNs: 2 * MS,
                           serverProcNs: 7 * MS)
        XCTAssertEqual(s.rttNs, 5 * MS)
        XCTAssertEqual(s.serverProcessingNs, 7 * MS)
    }

    func testAsymmetryErrorMatchesFormula() {
        let cases: [(Double, Double)] = [(1, 1), (1, 3), (3, 1), (0, 4), (4, 0), (0.5, 2.5)]
        let trueOffset: Int64 = -7_777_777
        for (up, dn) in cases {
            let dUp = Int64(up * Double(MS))
            let dDn = Int64(dn * Double(MS))
            let s = makeSample(seq: 0, trueOffsetNs: trueOffset, dUpNs: dUp, dDnNs: dDn)
            let expected = floorDiv(dUp - dDn, 2)
            XCTAssertEqual(s.offsetNs - trueOffset, expected, accuracy: 1,
                           "d_up=\(up)ms d_dn=\(dn)ms")
        }
    }

    /// ★ 가장 중요한 성질.
    /// 어떤 비대칭이어도 |오차| <= RTT/2 를 넘지 않아야 합니다.
    /// 이 덕분에 "최소 RTT 4ms 미만 -> 오차 2ms 미만"을 보장할 수 있습니다.
    func testErrorNeverExceedsHalfRtt() {
        let cases: [(Double, Double)] = [
            (1, 1), (1, 3), (3, 1), (0, 4), (4, 0), (0.1, 9.9), (9.9, 0.1)
        ]
        let trueOffset: Int64 = 3_141_592
        for (up, dn) in cases {
            let dUp = Int64(up * Double(MS))
            let dDn = Int64(dn * Double(MS))
            let s = makeSample(seq: 0, trueOffsetNs: trueOffset, dUpNs: dUp, dDnNs: dDn)
            let err = abs(s.offsetNs - trueOffset)
            XCTAssertLessThanOrEqual(Double(err), Double(s.rttNs) / 2 + 1,
                                     "d_up=\(up) d_dn=\(dn): 오차가 RTT/2 초과")
        }
    }

    // ── 2. 추정기 ────────────────────────────────────────────────────────────

    /// 큐잉이 낀 나쁜 샘플이 많아도 깨끗한 최소 RTT 샘플을 골라야 합니다.
    func testEstimatePicksMinRttSample() {
        let trueOffset: Int64 = 5_000_000
        var samples: [TimeSample] = []
        var seed: UInt64 = 42
        func rnd(_ lo: Double, _ hi: Double) -> Double {
            seed = seed &* 6364136223846793005 &+ 1442695040888963407
            let u = Double(seed >> 11) / Double(UInt64(1) << 53)
            return lo + u * (hi - lo)
        }
        for i in 0..<39 {
            let extra = Int64(rnd(5, 50) * Double(MS))
            samples.append(makeSample(seq: i, trueOffsetNs: trueOffset,
                                      dUpNs: MS + extra, dDnNs: MS))
        }
        samples.append(makeSample(seq: 99, trueOffsetNs: trueOffset,
                                  dUpNs: 400_000, dDnNs: 400_000))

        let est = ClockSync.estimate(samples)
        XCTAssertEqual(est.samplesTotal, 40)
        XCTAssertEqual(est.samplesRejected, 0)
        XCTAssertEqual(est.minRttNs, 800_000)
        XCTAssertEqual(est.uncertaintyNs, 400_000)
        XCTAssertLessThan(abs(est.offsetNs - trueOffset), 1000)
    }

    func testEstimateRejectsInsaneSamples() {
        let good = makeSample(seq: 0, trueOffsetNs: 0, dUpNs: MS, dDnNs: MS)
        let badNegativeRtt = TimeSample(seq: 1, t1: 1000, t2: 2000, t3: 9_000_000, t4: 2000)
        let badBackwards = TimeSample(seq: 2, t1: 5000, t2: 1000, t3: 2000, t4: 3000)
        let est = ClockSync.estimate([good, badNegativeRtt, badBackwards])
        XCTAssertEqual(est.samplesTotal, 3)
        XCTAssertEqual(est.samplesRejected, 2)
        XCTAssertEqual(est.samplesUsed, 1)
    }

    /// ★ Python 쪽에서 실제 버그를 잡은 케이스.
    /// 샘플 0개면 uncertainty=0 이라 "0 < 2ms 이므로 통과"가 되어버립니다.
    /// 측정을 못 했는데 성공으로 표시되는 건 최악입니다.
    func testEmptySamplesMustNotPass() {
        let est = ClockSync.estimate([])
        XCTAssertEqual(est.samplesUsed, 0)
        XCTAssertFalse(est.meetsTarget(), "측정이 없는데 통과로 판정됨 — 거짓 성공 버그")
        XCTAssertTrue(est.verdict().contains("실패"))
    }

    func testMeetsTargetUsesGuaranteedBound() {
        let pass = makeSample(seq: 0, trueOffsetNs: 0,
                              dUpNs: Int64(1.95 * Double(MS)),
                              dDnNs: Int64(1.95 * Double(MS)))
        XCTAssertTrue(ClockSync.estimate([pass]).meetsTarget())

        let fail = makeSample(seq: 0, trueOffsetNs: 0,
                              dUpNs: Int64(2.05 * Double(MS)),
                              dDnNs: Int64(2.05 * Double(MS)))
        XCTAssertFalse(ClockSync.estimate([fail]).meetsTarget())
    }

    // ── 3. 시각 변환 ─────────────────────────────────────────────────────────

    func testConversionRoundtrip() {
        let offset: Int64 = -123_456_789
        let slave: Int64 = 999_000_000_000
        let back = ClockSync.masterToSlaveNs(
            ClockSync.slaveToMasterNs(slave, offsetNs: offset), offsetNs: offset)
        XCTAssertEqual(back, slave)
    }

    /// 마스터 = 슬레이브 + offset.
    /// 마스터가 5ms 앞서면 마스터의 100ms 지점은 슬레이브의 95ms 지점.
    func testConversionSignConvention() {
        let offset = 5 * MS
        XCTAssertEqual(ClockSync.masterToSlaveNs(100 * MS, offsetNs: offset), 95 * MS)
        XCTAssertEqual(ClockSync.slaveToMasterNs(95 * MS, offsetNs: offset), 100 * MS)
    }

    // ── 4. 예약 시작 ─────────────────────────────────────────────────────────

    func testScheduleAcceptsWithEnoughLead() {
        let offset = 2 * MS
        let nowSlave = 1_000 * MS
        let startMaster = ClockSync.slaveToMasterNs(nowSlave, offsetNs: offset) + 500 * MS
        let chk = ClockSync.checkSchedule(startAtMasterNs: startMaster,
                                          offsetNs: offset, nowSlaveNs: nowSlave)
        XCTAssertTrue(chk.ok)
        XCTAssertEqual(chk.leadMs, 500.0, accuracy: 0.001)
    }

    /// ★ 늦게 도착한 명령은 거부해야 합니다.
    /// 억지로 따라가면 동기가 조용히 틀어집니다.
    func testScheduleRejectsLateCommand() {
        let chk = ClockSync.checkSchedule(startAtMasterNs: 1_000 * MS + 50 * MS,
                                          offsetNs: 0, nowSlaveNs: 1_000 * MS)
        XCTAssertFalse(chk.ok)
        XCTAssertTrue(chk.reason.contains("lead_too_small"))
    }

    func testScheduleRejectsPastCommand() {
        let chk = ClockSync.checkSchedule(startAtMasterNs: 500 * MS,
                                          offsetNs: 0, nowSlaveNs: 1_000 * MS)
        XCTAssertFalse(chk.ok)
        XCTAssertLessThan(chk.leadNs, 0)
    }

    func testMinLeadIs300ms() {
        XCTAssertEqual(SyncConfig.minLeadNs, 300 * MS)
    }

    // ── 5. 프레임 간격 ───────────────────────────────────────────────────────

    func testFrameIntervalDetects60fps() {
        let step: Int64 = 16_666_667
        let ts = (0..<60).map { Int64($0) * step }
        let st = ClockSync.frameIntervalStats(timestampsNs: ts)
        XCTAssertEqual(st.estimatedFps, 60.0, accuracy: 0.01)
        XCTAssertEqual(st.suspectedDrops, 0)
    }

    /// 발열 스로틀링으로 프레임을 흘리면 간격이 2배로 벌어집니다.
    func testFrameIntervalDetectsDrops() {
        let step: Int64 = 16_666_667
        var ts: [Int64] = []
        var t: Int64 = 0
        for i in 0..<60 {
            ts.append(t)
            t += step * ([10, 20, 30].contains(i) ? 2 : 1)
        }
        let st = ClockSync.frameIntervalStats(timestampsNs: ts)
        XCTAssertEqual(st.suspectedDrops, 3)
        XCTAssertEqual(st.maxIntervalMs, 33.333, accuracy: 0.01)
    }

    // ── 6. 시계 ─────────────────────────────────────────────────────────────

    func testMonotonicClockAdvances() {
        let a = MonotonicClock.nowNs()
        XCTAssertGreaterThan(a, 0)
        var b = MonotonicClock.nowNs()
        // 단조성: 절대 뒤로 가지 않아야 합니다
        for _ in 0..<1000 {
            let c = MonotonicClock.nowNs()
            XCTAssertGreaterThanOrEqual(c, b)
            b = c
        }
        XCTAssertGreaterThan(b, a)
    }

    func testClockName() {
        XCTAssertEqual(MonotonicClock.name, "CLOCK_UPTIME_RAW")
    }

    // ── 7. floorDiv (Python // 과 동일해야 함) ───────────────────────────────

    /// Swift 의 / 는 0 방향 절삭, Python 의 // 는 내림입니다.
    /// 오프셋이 음수일 때 결과가 달라지므로 반드시 맞춰야 합니다.
    func testFloorDivMatchesPython() {
        XCTAssertEqual(floorDiv(7, 2), 3)
        XCTAssertEqual(floorDiv(-7, 2), -4)   // Swift -7/2 == -3 (다름!)
        XCTAssertEqual(floorDiv(6, 2), 3)
        XCTAssertEqual(floorDiv(-6, 2), -3)
        XCTAssertEqual(floorDiv(-1, 2), -1)   // Swift -1/2 == 0 (다름!)
    }
}
