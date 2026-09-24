import XCTest
@testable import MocapSyncCore

/// 저장된 클럭 오프셋의 신선도 판정과 드리프트 실측을 시험합니다.
///
/// ★ 왜 이 로직이 필요했나
///
/// 3단계 첫 실기기 시험에서 사이드카가 "사용 불가"로 나왔습니다.
/// 원인은 동기 화면과 녹화 화면이 서로 몰라서 오프셋이 0 이었던 것입니다.
/// 고치면서, 단순히 값을 전달하는 것만으로는 부족하다는 것이 드러났습니다.
///
/// 오프셋은 세 가지로 낡습니다.
///   재부팅   -> CLOCK_UPTIME_RAW 가 0 으로 리셋. 완전히 무의미
///   절전     -> 잔 만큼 어긋남 (실측으로 확인된 실패 모드)
///   드리프트 -> 수정발진자 주파수 차이로 조용히 누적. 가장 안 보임
///
/// 앞의 둘은 값이 크게 튀어 탐지가 쉽지만, 드리프트는 소리 없이 쌓입니다.
final class ClockOffsetRecordTests: XCTestCase {

    let S = NS.perSecond
    let MS = NS.perMilli

    func rec(offsetNs: Int64 = 1_000_000_000,
             uncertaintyNs: Int64 = 2_038_000,
             measuredAtSlaveNs: Int64 = 100 * NS.perSecond,
             sleepAtSyncNs: Int64 = 5_000_000,
             bootTimeEpochNs: Int64 = 1_700_000_000_000_000_000) -> ClockOffsetRecord {
        ClockOffsetRecord(
            offsetNs: offsetNs, uncertaintyNs: uncertaintyNs, minRttNs: 4_076_000,
            measuredAtSlaveNs: measuredAtSlaveNs, sleepAtSyncNs: sleepAtSyncNs,
            bootTimeEpochNs: bootTimeEpochNs, masterId: "6de6379f76b8")
    }

    /// 기본 조건: 아무 일도 없었고 10초 지났음
    func fresh(_ r: ClockOffsetRecord, after ageNs: Int64 = 10 * NS.perSecond)
        -> ClockOffsetRecord.Freshness {
        r.freshness(nowSlaveNs: r.measuredAtSlaveNs + ageNs,
                    nowSleepNs: r.sleepAtSyncNs,
                    nowBootTimeEpochNs: r.bootTimeEpochNs)
    }

    // MARK: - 정상

    func testFreshRightAfterMeasurement() {
        let f = fresh(rec(), after: 3 * S)
        XCTAssertEqual(f, .fresh(ageNs: 3 * S))
        XCTAssertTrue(f.canRecord)
    }

    func testFreshUpTo60Seconds() {
        XCTAssertTrue(fresh(rec(), after: 59 * S).canRecord)
        if case .fresh = fresh(rec(), after: 59 * S) {} else {
            XCTFail("59초는 신선해야 합니다")
        }
    }

    // MARK: - 드리프트로 인한 노화

    /// 60초를 넘으면 aging. 여전히 촬영 가능하지만 경고합니다.
    func testAgingAfter60Seconds() {
        let f = fresh(rec(), after: 120 * S)
        guard case .aging(let age, let drift) = f else {
            return XCTFail("120초는 aging 이어야 합니다: \(f)")
        }
        XCTAssertEqual(age, 120 * S)
        // 40 ppm x 120초 = 4.8 ms
        XCTAssertEqual(drift, 4_800_000)
        XCTAssertTrue(f.canRecord, "aging 은 촬영을 막지 않습니다")
    }

    /// 10분을 넘으면 촬영을 막습니다.
    /// 40 ppm x 600초 = 24 ms = 1.4 프레임. 프레임을 잘못 짝짓게 됩니다.
    func testTooOldAfter10Minutes() {
        let f = fresh(rec(), after: 700 * S)
        guard case .tooOld = f else { return XCTFail("700초는 tooOld: \(f)") }
        XCTAssertFalse(f.canRecord)
    }

    /// ★ 드리프트 예산 계산을 못박습니다.
    /// 40 ppm 에서 2 ms 를 소진하는 시간이 50초이므로 신선 기준을 60초로 잡았습니다.
    func testDriftBudgetRationale() {
        let ppm = ClockOffsetRecord.worstCaseDriftPpm
        XCTAssertEqual(ppm, 40)
        // 2ms 를 소진하는 시간
        let secondsToBurn2ms = 2e-3 / (ppm / 1e6)
        XCTAssertEqual(secondsToBurn2ms, 50, accuracy: 0.01)
        // 신선 기준이 그보다 조금 길되 반 프레임에는 한참 못 미쳐야 합니다
        let atFreshLimit = Double(ClockOffsetRecord.freshMaxAgeNs) / 1e9 * ppm / 1e6
        XCTAssertLessThan(atFreshLimit, 8.333e-3, "신선 기준에서 반 프레임을 넘으면 안 됩니다")
    }

    // MARK: - 절전 (실측으로 확인된 실패 모드)

    /// 2026-09-24 실측: 52분 중 34분을 자면서 오프셋이 34분 이동했습니다.
    func testSleptIsDetected() {
        let r = rec()
        let f = r.freshness(nowSlaveNs: r.measuredAtSlaveNs + 10 * S,
                            nowSleepNs: r.sleepAtSyncNs + 34 * 60 * S,
                            nowBootTimeEpochNs: r.bootTimeEpochNs)
        guard case .slept(let ns) = f else { return XCTFail("절전을 못 잡았습니다: \(f)") }
        XCTAssertEqual(ns, 34 * 60 * S)
        XCTAssertFalse(f.canRecord)
        XCTAssertTrue(f.summary.contains("2040.0"), f.summary)
    }

    /// 두 시계를 읽는 사이의 미세한 차이로 절전이 오탐되면 안 됩니다.
    func testTinySleepDeltaIsNotFlagged() {
        let r = rec()
        let f = r.freshness(nowSlaveNs: r.measuredAtSlaveNs + 5 * S,
                            nowSleepNs: r.sleepAtSyncNs + 500_000,  // 0.5ms
                            nowBootTimeEpochNs: r.bootTimeEpochNs)
        XCTAssertTrue(f.canRecord, "0.5ms 차이를 절전으로 오탐했습니다: \(f)")
    }

    // MARK: - 재부팅

    func testRebootIsDetected() {
        let r = rec()
        let f = r.freshness(nowSlaveNs: 5 * S,               // uptime 이 작아졌음
                            nowSleepNs: 0,
                            nowBootTimeEpochNs: r.bootTimeEpochNs + 3600 * S)
        XCTAssertEqual(f, .rebooted)
        XCTAssertFalse(f.canRecord)
    }

    /// ★ 재부팅 검사가 절전·나이 검사보다 먼저여야 합니다.
    ///   재부팅하면 measuredAtSlaveNs 가 미래 값처럼 보여 나이가 음수가 됩니다.
    ///   순서가 틀리면 엉뚱한 판정이 나옵니다.
    func testRebootCheckedBeforeOthers() {
        let r = rec(measuredAtSlaveNs: 3600 * NS.perSecond)   // 부팅 1시간 뒤 측정
        let f = r.freshness(nowSlaveNs: 10 * S,               // 재부팅 후 10초
                            nowSleepNs: 0,
                            nowBootTimeEpochNs: r.bootTimeEpochNs + 7200 * S)
        XCTAssertEqual(f, .rebooted, "재부팅이 다른 판정에 가려졌습니다: \(f)")
    }

    /// 시계가 뒤로 갔는데 부팅시각을 못 읽은 경우도 재부팅으로 봅니다.
    func testClockWentBackwardsIsRebooted() {
        let r = rec(measuredAtSlaveNs: 3600 * NS.perSecond, bootTimeEpochNs: 0)
        let f = r.freshness(nowSlaveNs: 10 * S, nowSleepNs: r.sleepAtSyncNs,
                            nowBootTimeEpochNs: 0)
        XCTAssertEqual(f, .rebooted)
    }

    /// 부팅시각을 못 읽으면(0) 그 검사는 건너뛰고 나머지로 판정합니다.
    func testUnknownBootTimeDoesNotFalselyFlag() {
        let r = rec(bootTimeEpochNs: 0)
        let f = r.freshness(nowSlaveNs: r.measuredAtSlaveNs + 5 * S,
                            nowSleepNs: r.sleepAtSyncNs,
                            nowBootTimeEpochNs: 0)
        XCTAssertTrue(f.canRecord, "부팅시각 미확인을 재부팅으로 오탐했습니다: \(f)")
    }

    // MARK: - 무효

    func testZeroUncertaintyIsInvalid() {
        XCTAssertEqual(fresh(rec(uncertaintyNs: 0)), .invalid)
        XCTAssertFalse(ClockOffsetRecord.Freshness.invalid.canRecord)
    }

    /// ★ 3단계 첫 시험에서 실제로 일어난 상황.
    ///   동기를 안 한 채 녹화하면 오프셋이 0 이고 상한도 0 입니다.
    func testNeverSyncedIsInvalid() {
        let r = ClockOffsetRecord(offsetNs: 0, uncertaintyNs: 0, minRttNs: 0,
                                  measuredAtSlaveNs: 0, sleepAtSyncNs: 0,
                                  bootTimeEpochNs: 0, masterId: "")
        XCTAssertEqual(fresh(r), .invalid)
        XCTAssertTrue(fresh(r).summary.contains("동기"), fresh(r).summary)
    }

    // MARK: - 실효 오차 상한

    /// 측정 직후의 상한만 적으면 낙관적입니다. 지난 시간만큼 드리프트를 더해야 합니다.
    func testEffectiveUncertaintyAddsDrift() {
        let r = rec(uncertaintyNs: 2_038_000)
        XCTAssertEqual(r.effectiveUncertaintyNs(ageNs: 0), 2_038_000)
        // 40 ppm x 60초 = 2.4 ms 추가
        XCTAssertEqual(r.effectiveUncertaintyNs(ageNs: 60 * S), 2_038_000 + 2_400_000)
    }

    func testEffectiveUncertaintyIgnoresNegativeAge() {
        let r = rec()
        XCTAssertEqual(r.effectiveUncertaintyNs(ageNs: -100), r.uncertaintyNs)
    }

    // MARK: - 드리프트 실측

    /// ★ 규격 최악값 40 ppm 은 추측입니다. 두 번 재면 실제 값이 나옵니다.
    func testDriftMeasuredFromTwoRecords() {
        let a = rec(offsetNs: 1_000_000_000, measuredAtSlaveNs: 100 * S)
        // 200초 뒤, 오프셋이 2 ms 증가 -> 2ms / 200s = 10 ppm
        let b = rec(offsetNs: 1_000_000_000 + 2 * MS, measuredAtSlaveNs: 300 * S)
        let ppm = ClockOffsetRecord.driftPpm(from: a, to: b)
        XCTAssertNotNil(ppm)
        XCTAssertEqual(ppm!, 10, accuracy: 0.001)
    }

    func testDriftSignConvention() {
        let a = rec(offsetNs: 0, measuredAtSlaveNs: 0)
        let b = rec(offsetNs: -1 * MS, measuredAtSlaveNs: 100 * S)
        let ppm = ClockOffsetRecord.driftPpm(from: a, to: b)!
        XCTAssertLessThan(ppm, 0, "오프셋이 줄면 음수여야 합니다")
    }

    /// 간격이 너무 짧으면 측정잡음과 드리프트를 구분할 수 없습니다.
    func testDriftRefusedWhenIntervalTooShort() {
        let a = rec(measuredAtSlaveNs: 100 * S)
        let b = rec(measuredAtSlaveNs: 110 * S)      // 10초
        XCTAssertNil(ClockOffsetRecord.driftPpm(from: a, to: b))
    }

    /// 절전을 거친 두 기록은 비교하면 안 됩니다. 변화의 대부분이 절전 때문입니다.
    func testDriftRefusedAcrossSleep() {
        let a = rec(measuredAtSlaveNs: 100 * S, sleepAtSyncNs: 0)
        let b = rec(measuredAtSlaveNs: 300 * S, sleepAtSyncNs: 60 * S)
        XCTAssertNil(ClockOffsetRecord.driftPpm(from: a, to: b))
    }

    func testDriftRefusedAcrossReboot() {
        let a = rec(measuredAtSlaveNs: 100 * S, bootTimeEpochNs: 1)
        let b = rec(measuredAtSlaveNs: 300 * S, bootTimeEpochNs: 2)
        XCTAssertNil(ClockOffsetRecord.driftPpm(from: a, to: b))
    }

    /// ★ 잡음을 드리프트로 오해하지 않기 위한 신뢰 구간.
    ///   두 측정 상한이 각 2 ms 이고 간격이 200초면, 20 ppm 이하의 값은
    ///   측정잡음과 구분할 수 없습니다.
    func testDriftUncertaintyTellsWhenMeasurementIsMeaningless() {
        let a = rec(uncertaintyNs: 2 * MS, measuredAtSlaveNs: 100 * S)
        let b = rec(uncertaintyNs: 2 * MS, measuredAtSlaveNs: 300 * S)
        let unc = ClockOffsetRecord.driftUncertaintyPpm(from: a, to: b)!
        // (2ms + 2ms) / 200s = 20 ppm
        XCTAssertEqual(unc, 20, accuracy: 0.001)

        let measured = ClockOffsetRecord.driftPpm(from: a, to: b)!
        XCTAssertLessThan(abs(measured), unc,
            "이 경우 실측값이 신뢰 구간 안이므로 '드리프트를 측정하지 못했다'고 말해야 합니다")
    }

    /// 간격을 늘리면 신뢰 구간이 좁아집니다 = 드리프트를 실제로 측정할 수 있게 됩니다.
    func testLongerIntervalGivesBetterDriftResolution() {
        let a = rec(uncertaintyNs: 2 * MS, measuredAtSlaveNs: 0)
        let short = rec(uncertaintyNs: 2 * MS, measuredAtSlaveNs: 100 * S)
        let long = rec(uncertaintyNs: 2 * MS, measuredAtSlaveNs: 2000 * S)
        let us = ClockOffsetRecord.driftUncertaintyPpm(from: a, to: short)!
        let ul = ClockOffsetRecord.driftUncertaintyPpm(from: a, to: long)!
        XCTAssertLessThan(ul, us)
        XCTAssertEqual(ul, 2, accuracy: 0.001)   // (4ms)/2000s = 2 ppm
    }

    // MARK: - 저장/복원

    func testCodableRoundTrip() throws {
        let a = rec()
        let d = try JSONEncoder().encode(a)
        let b = try JSONDecoder().decode(ClockOffsetRecord.self, from: d)
        XCTAssertEqual(a, b)
    }
}
