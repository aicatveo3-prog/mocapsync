import XCTest
@testable import MocapSyncCore

/// RTT 분포 진단(RttProfile)과 time_req 고속 인코더를 검증합니다.
///
/// 이 두 가지는 2026-09-24 아이폰 11 실측에서 나온 문제 때문에 추가됐습니다.
/// 실측값: 최소 RTT 4.810 ms -> 오차 상한 2.405 ms (목표 2 ms 미달).
/// 숫자 하나만으로는 "물리적 바닥"인지 "표본 부족"인지 알 수 없어서
/// 분포를 보게 만들었습니다.
final class RttProfileTests: XCTestCase {

    let MS = NS.perMilli

    /// 원하는 RTT 를 갖는 샘플을 만듭니다. 편도는 대칭(오차 0)으로 둡니다.
    func sample(seq: Int, rttNs: Int64,
                offsetNs: Int64 = 0, procNs: Int64 = 50_000) -> TimeSample {
        let t1: Int64 = 1_000_000_000
        let dUp = rttNs / 2
        let dDn = rttNs - dUp
        let t2 = t1 + dUp + offsetNs
        let t3 = t2 + procNs
        let t4 = t3 + dDn - offsetNs
        return TimeSample(seq: seq, t1: t1, t2: t2, t3: t3, t4: t4)
    }

    // MARK: - 고속 인코더가 JSONEncoder 와 바이트 단위로 같은가

    /// ★ 가장 중요한 테스트.
    /// 고속 인코더는 JSON 을 손으로 조립합니다. 키 순서나 이름이 한 글자라도
    /// 달라지면 마스터가 t1 을 못 읽고, 그러면 t1=0 으로 파싱되어
    /// 오프셋이 완전히 엉뚱하게 나옵니다. 예외는 안 납니다. 조용히 틀립니다.
    func testFastTimeReqEncoderMatchesJSONEncoder() throws {
        let cases: [(Int, Int64)] = [
            (0, 0),
            (1, 123),
            (39, 1_794_233_732_416),   // 실측된 CLOCK_UPTIME_RAW 크기
            (299, Int64.max),
            (7, -5),                   // 워밍업은 음수 seq 를 씁니다
            (12345, Int64.min),
        ]
        for (seq, t1) in cases {
            let fast = WireCodec.encodeTimeReqLine(seq: seq, t1: t1)
            let slow = try WireCodec.encodeLine(TimeReqMsg(seq: seq, t1: t1))
            XCTAssertEqual(
                fast, slow,
                """
                고속 인코더와 JSONEncoder 결과가 다릅니다 (seq=\(seq), t1=\(t1)).
                  고속: \(WireCodec.text(fast))
                  정규: \(WireCodec.text(slow))
                키 이름/순서를 sortedKeys(seq, t1, type)에 맞추세요.
                """)
        }
    }

    /// 고속 인코더 결과가 실제로 파싱 가능한 JSON 인지
    func testFastTimeReqEncoderProducesValidJSON() throws {
        var d = WireCodec.encodeTimeReqLine(seq: 5, t1: 999)
        XCTAssertEqual(d.last, 0x0A, "\\n 으로 끝나야 합니다")
        d.removeLast()
        let obj = try JSONSerialization.jsonObject(with: d) as! [String: Any]
        XCTAssertEqual(Set(obj.keys), ["type", "seq", "t1"])
        XCTAssertEqual(obj["type"] as? String, "time_req")
        XCTAssertEqual(obj["seq"] as? Int, 5)
        XCTAssertEqual(obj["t1"] as? Int, 999)
    }

    // MARK: - 백분위

    func testPercentileNearestRank() {
        // 1..10 ms
        let xs: [Int64] = (1...10).map { Int64($0) * MS }
        XCTAssertEqual(ClockSync.percentile(sorted: xs, 0), 1 * MS)
        XCTAssertEqual(ClockSync.percentile(sorted: xs, 100), 10 * MS)
        // idx = round(0.5 * 9) = round(4.5) = 5  -> 6ms
        XCTAssertEqual(ClockSync.percentile(sorted: xs, 50), 6 * MS)
    }

    func testPercentileAlwaysReturnsObservedValue() {
        let xs: [Int64] = [3 * MS, 7 * MS]
        // 보간하면 5ms 가 나오지만, 5ms 는 관측되지 않은 값입니다.
        // 최근접 순위는 반드시 실제 관측값 중 하나를 돌려줘야 합니다.
        for p in stride(from: 0.0, through: 100.0, by: 5.0) {
            let v = ClockSync.percentile(sorted: xs, p)
            XCTAssertTrue(xs.contains(v), "p\(p) = \(v) 는 관측값이 아닙니다")
        }
    }

    func testPercentileEmpty() {
        XCTAssertEqual(ClockSync.percentile(sorted: [], 50), 0)
    }

    // MARK: - 분포 모양 판정

    /// 실측 재현: 최소 4.8ms, 중앙값도 5ms 대 -> 바닥 도달
    func testNarrowDistributionMeansFloorReached() {
        var s: [TimeSample] = []
        for i in 0..<40 {
            let jitter = Int64(i % 4) * 100_000   // 0~0.3ms
            s.append(sample(seq: i, rttNs: 4_810_000 + jitter))
        }
        let p = ClockSync.rttProfile(s)
        XCTAssertEqual(p.count, 40)
        XCTAssertEqual(p.shape, .narrow)
        XCTAssertTrue(p.diagnosis.contains("유선"),
                      "바닥에 도달했으면 경로를 바꾸라고 안내해야 합니다: \(p.diagnosis)")
        XCTAssertLessThan(p.headroom, 0.25)
    }

    /// 꼬리가 두꺼우면 왕복을 늘리라고 안내해야 합니다
    func testHeavyTailMeansMoreProbesHelp() {
        var s: [TimeSample] = []
        s.append(sample(seq: 0, rttNs: 3 * MS))
        for i in 1..<40 { s.append(sample(seq: i, rttNs: Int64(10 + i % 20) * MS)) }
        let p = ClockSync.rttProfile(s)
        XCTAssertEqual(p.shape, .heavyTail)
        XCTAssertTrue(p.diagnosis.contains("왕복"),
                      "꼬리가 두꺼우면 왕복을 늘리라고 해야 합니다: \(p.diagnosis)")
    }

    func testTooFewSamples() {
        let p = ClockSync.rttProfile([sample(seq: 0, rttNs: 4 * MS)])
        XCTAssertEqual(p.shape, .tooFewSamples)
    }

    func testEmptyProfile() {
        let p = ClockSync.rttProfile([])
        XCTAssertEqual(p, .empty)
        XCTAssertEqual(p.count, 0)
        XCTAssertEqual(p.headroom, 0)
        XCTAssertTrue(p.histogramLines.isEmpty)
    }

    // MARK: - 버킷

    func testBucketsSumToCount() {
        var s: [TimeSample] = []
        for i in 0..<100 { s.append(sample(seq: i, rttNs: Int64(i) * 500_000)) }
        let p = ClockSync.rttProfile(s)
        XCTAssertEqual(p.buckets.reduce(0, +), p.count,
                       "버킷 합이 표본 수와 달라요. 경계 조건에서 샘플을 흘리고 있습니다")
    }

    func testBucketsLengthIsEdgesPlusOne() {
        let p = ClockSync.rttProfile([sample(seq: 0, rttNs: 4 * MS)])
        XCTAssertEqual(p.buckets.count, RttProfile.bucketEdgesMs.count + 1)
    }

    func testOverflowGoesToLastBucket() {
        let p = ClockSync.rttProfile([sample(seq: 0, rttNs: 5 * NS.perSecond)])
        XCTAssertEqual(p.buckets.last, 1, "40ms 초과는 마지막 버킷에 들어가야 합니다")
    }

    // MARK: - 비정상 샘플 배제

    /// isSane 이 거른 샘플은 분포에도 들어가면 안 됩니다.
    /// (안 그러면 히스토그램이 거짓말을 하고, 진단 문장도 틀립니다)
    func testInsaneSamplesExcluded() {
        let good = sample(seq: 0, rttNs: 4 * MS)
        // rtt 가 음수인 불가능한 샘플
        let bad = TimeSample(seq: 1, t1: 1000, t2: 500, t3: 400, t4: 900)
        XCTAssertFalse(bad.isSane)
        let p = ClockSync.rttProfile([good, bad])
        XCTAssertEqual(p.count, 1, "isSane 이 거른 샘플이 분포에 섞였습니다")
    }

    // MARK: - 설정값

    /// 기본 간격을 0 으로 바꾼 결정을 못박아 둡니다.
    /// 5ms 였을 때 iOS WiFi 가 매 왕복마다 절전에 들어가 최소 RTT 가 부풀었습니다.
    func testProbeGapDefaultsToZeroForRadioWakefulness() {
        XCTAssertEqual(SyncConfig.probeGapMs, 0)
        XCTAssertGreaterThan(SyncConfig.warmupCount, 0,
                             "워밍업이 없으면 첫 패킷의 무선 기동시간이 표본에 섞입니다")
    }

    func testSummaryLineContainsAllPercentiles() {
        var s: [TimeSample] = []
        for i in 0..<20 { s.append(sample(seq: i, rttNs: Int64(4 + i) * MS)) }
        let line = ClockSync.rttProfile(s).summaryLine
        for k in ["p0", "p10", "p50", "p90", "max"] {
            XCTAssertTrue(line.contains(k), "요약에 \(k) 가 없습니다: \(line)")
        }
    }

    /// 유선 랜으로 바꾸면 RTT 가 1ms 아래로 갈 수 있습니다.
    /// 그때 전부 한 버킷에 뭉치면 개선 여부를 볼 수 없습니다.
    func testSubMillisecondResolutionExists() {
        var s: [TimeSample] = []
        for i in 0..<20 { s.append(sample(seq: i, rttNs: 200_000 + Int64(i) * 10_000)) }
        let p = ClockSync.rttProfile(s)
        XCTAssertEqual(p.buckets[0], 20, "1ms 미만 구간에 해상도가 없습니다")
    }

    // MARK: - Python 구현과의 수치 일치 (골든 벡터)

    /// ★ server/tests/test_rtt_profile.py::test_golden_vector_matches_swift 와
    ///   **같은 입력, 같은 기대값**입니다. 한쪽을 고치면 다른 쪽이 깨집니다.
    func testGoldenVectorMatchesPython() {
        var s: [TimeSample] = []
        for i in 0..<40 {
            s.append(sample(seq: i, rttNs: 4_810_000 + Int64(i % 4) * 100_000))
        }
        let p = ClockSync.rttProfile(s)
        XCTAssertEqual(p.count, 40)
        XCTAssertEqual(p.p0Ns, 4_810_000)
        XCTAssertEqual(p.p100Ns, 5_110_000)

        let rtts = (0..<40).map { 4_810_000 + Int64($0 % 4) * 100_000 }.sorted()
        XCTAssertEqual(p.p50Ns, rtts[20])   // idx = round(0.5*39) = round(19.5) = 20

        XCTAssertEqual(RttProfile.bucketEdgesMs[5], 5)
        XCTAssertEqual(p.buckets[5], 20)
        XCTAssertEqual(p.buckets[6], 20)
        XCTAssertEqual(p.buckets.reduce(0, +), 40)
    }

    /// ★ 백분위 반올림 규칙이 Python 과 같은지.
    ///
    /// Swift .rounded() 는 0에서 먼 쪽 반올림 -> (4.5).rounded() == 5
    /// Python 내장 round() 는 짝수 반올림    -> round(4.5) == 4
    /// Python 쪽은 floor(x+0.5) 로 맞춰 놨습니다. 여기서 그 값을 못박습니다.
    func testPercentileRoundingMatchesPython() {
        let xs: [Int64] = (0..<10).map { Int64($0) }
        XCTAssertEqual(ClockSync.percentile(sorted: xs, 50), 5,
                       "p50 이 5 가 아니면 Python 구현과 갈라집니다")
    }
}
