import XCTest
@testable import MocapSyncCore

/// 사이드카 구조와 검증을 시험합니다.
///
/// ★ 왜 이 테스트가 중요한가
///
/// 사이드카는 조용히 망가집니다. 프레임 0개, 시각 역행, 낡은 오프셋 —
/// 전부 예외 없이 그냥 "이상한 3D"로 끝납니다. 그래서 각 실패 모드를
/// **일부러 만들어** 검증이 잡는지 확인합니다.
///
/// 방식: 정상 사이드카를 하나 만들고 한 항목씩 망가뜨립니다.
/// 정상본이 깨끗하다는 것도 함께 확인해야 합니다. 안 그러면
/// "항상 경고가 나오는 검증"이 되어 아무도 안 봅니다.
final class SidecarTests: XCTestCase {

    let MS = NS.perMilli

    /// 60fps 로 정확히 찍힌 프레임들.
    ///
    /// 시작 시각을 부팅 후 1000초로 잡습니다. 실제 폰이 그 정도 켜져 있고,
    /// 무엇보다 테스트에서 "동기를 5분 전에 했다" 같은 상황을 만들려면
    /// 앞쪽에 여유가 있어야 합니다 (10초로 두면 음수가 됩니다).
    func makeFrames(count: Int, startNs: Int64 = 1_000_000_000_000,
                    fps: Double = 60) -> [[Int64]] {
        let step = Int64(1e9 / fps)
        return (0..<count).map { [Int64($0), startNs + Int64($0) * step] }
    }

    /// 모든 검사를 통과해야 하는 사이드카
    func makeValid(frameCount: Int = 600) -> Sidecar {
        let frames = makeFrames(count: frameCount)
        return Sidecar(
            deviceId: "6DC32E3A1234", deviceName: "iPhone", model: "iPhone12,1",
            osVersion: "17.5.1", appVersion: "0.3.0 (11) abc1234",
            sessionId: "S20260924-1", role: "slave",
            clock: "CLOCK_UPTIME_RAW",
            clockOffsetNs: 5_522_186_169_000,
            clockUncertaintyNs: 2_038_000,      // 실측 최고 기록
            clockMinRttNs: 4_076_000,
            clockMeasuredAtNs: 999_000_000_000,  // 첫 프레임 1초 전
            sleepAtSyncNs: 1_234_567,
            sleepAtRecordStartNs: 1_234_567,    // 안 잤음
            timestampSource: "CMSampleBufferPresentationTimeStamp",
            timestampDomainDeltaNs: -458,       // 아이폰 11 실측
            targetFps: 60, width: 1920, height: 1080,
            cameraDeviceType: "AVCaptureDeviceTypeBuiltInWideAngleCamera",
            fieldOfViewDeg: 69.7, isBinned: false,
            exposureDurationNs: 2_000_000,      // 1/500초
            iso: 400, lensPosition: 0.42,
            focusLocked: true, whiteBalanceLocked: true, exposureLocked: true,
            stabilization: "off",
            // 마스터시각 = 슬레이브시각 + 오프셋
            requestedStartAtMasterNs: 1_000_000_000_000 + 5_522_186_169_000,
            requestedStartAtSlaveNs: 1_000_000_000_000,
            firstFramePtsNs: frames[0][1],
            droppedFrameCount: 0,
            thermalAtStart: "nominal", thermalAtEnd: "fair",
            batteryAtStart: 0.8, batteryAtEnd: 0.78,
            frames: frames)
    }

    func codes(_ s: Sidecar) -> Set<String> { Set(s.validate().map(\.code)) }
    func fatals(_ s: Sidecar) -> Set<String> {
        Set(s.validate().filter { $0.severity == .fatal }.map(\.code))
    }

    // MARK: - 정상본

    /// ★ 이게 먼저 통과해야 나머지 테스트가 의미를 가집니다.
    ///
    /// 기준: **경고와 치명이 하나도 없어야** 합니다.
    /// info 는 허용합니다 — 실측 오차 상한 2.038 ms 가 설계 목표 2 ms 를
    /// 구조적으로 넘기 때문에(DESIGN.md §3.12) 정상 촬영에도 info 가 하나 붙습니다.
    /// 그걸 경고로 올리면 모든 촬영에 경고가 붙어 경고가 무의미해집니다.
    func testValidSidecarHasNoWarningsOrFatals() {
        let s = makeValid()
        let bad = s.validate().filter { $0.severity != .info }
        XCTAssertEqual(bad, [], "정상본에 경고/치명이 잡힙니다: \(s.validationReport())")
        XCTAssertTrue(s.isUsable)
    }

    /// 실측 최고 기록(2.038 ms)은 info 까지만. 경고면 안 됩니다.
    func testMeasuredBestUncertaintyIsInfoOnly() {
        let s = makeValid()
        XCTAssertEqual(s.clockUncertaintyNs, 2_038_000)
        let i = s.validate().first { $0.code == "clock_uncertainty_over_target" }
        XCTAssertNotNil(i)
        XCTAssertEqual(i?.severity, .info)
    }

    /// 실측 기준선(3 ms)보다 나쁘면 경고. 링크가 평소보다 안 좋다는 신호.
    func testDegradedUncertaintyWarns() {
        var s = makeValid()
        s.clockUncertaintyNs = 3_500_000
        let codesSet = codes(s)
        XCTAssertTrue(codesSet.contains("clock_uncertainty_degraded"))
        XCTAssertFalse(codesSet.contains("clock_uncertainty_over_target"),
                       "두 판정이 동시에 나오면 중복 보고입니다")
        XCTAssertTrue(s.isUsable, "3.5ms 는 반 프레임 안이므로 쓸 수 있어야 합니다")
    }

    func testDerivedValues() {
        let s = makeValid(frameCount: 600)
        XCTAssertEqual(s.frameCount, 600)
        // 600프레임 60fps = 599 간격 = 9.983초
        XCTAssertEqual(Double(s.durationNs) / 1e9, 599.0 / 60.0, accuracy: 1e-6)
        XCTAssertEqual(s.intervalStats.estimatedFps, 60, accuracy: 0.5)
        XCTAssertFalse(s.sleptSinceSync)
        XCTAssertEqual(s.sleepSinceSyncNs, 0)
    }

    func testToMasterConversion() {
        let s = makeValid()
        let slave = s.timestampsNs[0]
        XCTAssertEqual(s.toMasterNs(slave), slave + s.clockOffsetNs)
        XCTAssertEqual(s.timestampsMasterNs[0], slave + s.clockOffsetNs)
    }

    // MARK: - 왕복 인코딩

    func testJSONRoundTrip() throws {
        let a = makeValid(frameCount: 120)
        let b = try Sidecar.decoded(from: try a.encoded())
        XCTAssertEqual(a, b, "JSON 왕복에서 값이 변했습니다")
    }

    /// Python 구현과 키가 같아야 하므로 키 이름을 못박습니다.
    /// 여기서 실패하면 server/mocapsync/sidecar.py 도 같이 고쳐야 합니다.
    func testJSONKeysArePinned() throws {
        let d = try makeValid(frameCount: 2).encoded()
        let j = try JSONSerialization.jsonObject(with: d) as! [String: Any]
        let expected: Set<String> = [
            "schemaVersion",
            "deviceId", "deviceName", "model", "osVersion", "appVersion",
            "sessionId", "role",
            "clock", "clockOffsetNs", "clockUncertaintyNs", "clockMinRttNs",
            "clockMeasuredAtNs", "sleepAtSyncNs", "sleepAtRecordStartNs",
            "timestampSource", "timestampDomainDeltaNs",
            "targetFps", "width", "height", "cameraDeviceType",
            "fieldOfViewDeg", "isBinned", "exposureDurationNs", "iso",
            "lensPosition", "focusLocked", "whiteBalanceLocked",
            "exposureLocked", "stabilization",
            "requestedStartAtMasterNs", "requestedStartAtSlaveNs",
            "firstFramePtsNs",
            "droppedFrameCount", "thermalAtStart", "thermalAtEnd",
            "batteryAtStart", "batteryAtEnd",
            "frames",
        ]
        XCTAssertEqual(Set(j.keys), expected,
            "키가 달라졌습니다. 추가/삭제된 키: "
            + Set(j.keys).symmetricDifference(expected).sorted().joined(separator: ", "))
    }

    /// 프레임이 [번호, 시각] 2개 배열로 나가는지. 객체 배열이 되면 크기가 2배 됩니다.
    func testFramesAreArrayPairs() throws {
        let d = try makeValid(frameCount: 3).encoded()
        let j = try JSONSerialization.jsonObject(with: d) as! [String: Any]
        let frames = j["frames"] as! [[NSNumber]]
        XCTAssertEqual(frames.count, 3)
        XCTAssertEqual(frames[0].count, 2)
        XCTAssertEqual(frames[1][0].intValue, 1)
    }

    // MARK: - 치명 실패들

    func testNoFramesIsFatal() {
        var s = makeValid()
        s.frames = []
        XCTAssertTrue(fatals(s).contains("no_frames"))
        XCTAssertFalse(s.isUsable)
    }

    func testNonMonotonicTimestampsIsFatal() {
        var s = makeValid(frameCount: 10)
        s.frames[5][1] = s.frames[4][1] - 1_000      // 시각 역행
        XCTAssertTrue(fatals(s).contains("non_monotonic"))
    }

    func testDuplicateTimestampIsFatal() {
        var s = makeValid(frameCount: 10)
        s.frames[5][1] = s.frames[4][1]              // 같은 시각 = 보간 불가
        XCTAssertTrue(fatals(s).contains("non_monotonic"))
    }

    /// ★ 2026-09-24 실측으로 알게 된 실패 모드.
    /// 폰이 자면 CLOCK_UPTIME_RAW 가 멈춰서 이전 오프셋이 무효가 됩니다.
    func testSleptSinceSyncIsFatal() {
        var s = makeValid()
        s.sleepAtRecordStartNs = s.sleepAtSyncNs + 34 * 60 * NS.perSecond
        XCTAssertTrue(s.sleptSinceSync)
        XCTAssertTrue(fatals(s).contains("slept_since_sync"))
        // 보고는 초 단위로 찍습니다. 34분 = 2040.0초.
        XCTAssertTrue(s.validationReport().joined().contains("2040.0"),
                      "잔 시간이 보고에 나와야 합니다: \(s.validationReport())")
    }

    func testMissingClockSyncIsFatal() {
        var s = makeValid()
        s.clockUncertaintyNs = 0
        XCTAssertTrue(fatals(s).contains("no_clock_sync"))
    }

    /// 반 프레임(8.333ms)을 넘으면 프레임 짝짓기가 모호해집니다.
    func testClockUncertaintyOverHalfFrameIsFatal() {
        var s = makeValid()
        s.clockUncertaintyNs = 9 * MS
        XCTAssertTrue(fatals(s).contains("clock_uncertainty_over_half_frame"))
    }

    /// 실측 음성우선 값 2.331ms 도 쓸 수 있어야 합니다.
    func testMeasuredVoicePriorityUncertaintyIsUsable() {
        var s = makeValid()
        s.clockUncertaintyNs = 2_331_000
        XCTAssertTrue(codes(s).contains("clock_uncertainty_over_target"))
        XCTAssertTrue(s.isUsable, "2.331ms 는 쓸 수 있어야 합니다")
        XCTAssertEqual(s.validate().filter { $0.severity == .warning }, [],
                       "실측 정상값에 경고가 붙으면 안 됩니다")
    }

    /// 실측 최악값 2.995ms 도 치명은 아니어야 합니다 (18:58 측정).
    func testMeasuredWorstUncertaintyIsNotFatal() {
        var s = makeValid()
        s.clockUncertaintyNs = 2_995_000
        XCTAssertTrue(s.isUsable)
        XCTAssertFalse(codes(s).contains("clock_uncertainty_degraded"),
                       "2.995ms 는 실측 범위 안이라 경고 대상이 아닙니다")
    }

    func testClockDomainMismatchIsFatal() {
        var s = makeValid()
        s.timestampDomainDeltaNs = 5 * MS
        XCTAssertTrue(fatals(s).contains("clock_domain_mismatch"))
    }

    func testStabilizationOnIsFatal() {
        var s = makeValid()
        s.stabilization = "standard"
        XCTAssertTrue(fatals(s).contains("stabilization_on"))
    }

    func testVirtualCameraIsFatal() {
        var s = makeValid()
        s.cameraDeviceType = "AVCaptureDeviceTypeBuiltInDualWideCamera"
        XCTAssertTrue(fatals(s).contains("virtual_camera"))
    }

    func testStartedEarlierThanRequestedIsFatal() {
        var s = makeValid()
        s.requestedStartAtSlaveNs = s.frames[0][1] + 5 * MS
        XCTAssertTrue(fatals(s).contains("started_early"))
    }

    func testEmptyDeviceIdIsFatal() {
        var s = makeValid()
        s.deviceId = ""
        XCTAssertTrue(fatals(s).contains("no_device_id"))
    }

    // MARK: - 경고들

    func testFpsMismatchWarns() {
        var s = makeValid()
        s.frames = makeFrames(count: 600, fps: 30)   // 목표는 60
        XCTAssertTrue(codes(s).contains("fps_mismatch"))
        XCTAssertTrue(s.isUsable, "fps 가 달라도 데이터 자체는 쓸 수 있습니다")
    }

    func testTargetFpsBelow60Warns() {
        var s = makeValid()
        s.targetFps = 30
        s.frames = makeFrames(count: 600, fps: 30)
        XCTAssertTrue(codes(s).contains("fps_below_60"))
    }

    func testFrameDropDetected() {
        var s = makeValid(frameCount: 100)
        // 50번 프레임 뒤에 한 프레임 분량을 건너뜁니다
        for i in 50..<100 { s.frames[i][1] += 16_666_666 }
        XCTAssertTrue(codes(s).contains("frame_drops"))
    }

    func testReportedDropsWarn() {
        var s = makeValid()
        s.droppedFrameCount = 7
        XCTAssertTrue(codes(s).contains("reported_drops"))
    }

    func testSlowShutterWarns() {
        var s = makeValid()
        s.exposureDurationNs = 8_000_000          // 1/125초
        XCTAssertTrue(codes(s).contains("shutter_too_slow"))
    }

    func testTooShortWarns() {
        let s = makeValid(frameCount: 60)          // 1초
        XCTAssertTrue(codes(s).contains("too_short"))
        XCTAssertTrue(s.isUsable)
    }

    func testStartedLateWarns() {
        var s = makeValid()
        s.requestedStartAtSlaveNs = s.frames[0][1] - 100 * MS
        XCTAssertTrue(codes(s).contains("started_late"))
    }

    func testExposureNotLockedWarns() {
        var s = makeValid()
        s.exposureLocked = false
        XCTAssertTrue(codes(s).contains("exposure_not_locked"))
    }

    /// ★ 고정초점 렌즈는 focusLocked=false 가 **정상**입니다.
    /// 초광각·전면은 초점 기구가 없어 잠글 대상이 없습니다 (DESIGN.md §3.10).
    /// 여기서 경고를 내면 정상 촬영에 거짓 경고가 붙습니다.
    func testFixedFocusLensDoesNotWarn() {
        var s = makeValid()
        s.focusLocked = false
        s.cameraDeviceType = "AVCaptureDeviceTypeBuiltInUltraWideCamera"
        XCTAssertFalse(codes(s).contains("focus_not_locked"))
        XCTAssertTrue(s.isUsable)
    }

    // MARK: - 동기 나이 / 드리프트

    /// ★ 가장 안 보이는 실패 모드.
    /// 측정 상한은 2.038ms 로 좋은데, 5분 전 측정이면 드리프트가 12ms 쌓여
    /// 실효 상한이 반 프레임을 넘습니다. 측정값만 보면 알 수 없습니다.
    func testSyncTooOldIsFatal() {
        var s = makeValid()
        s.clockMeasuredAtNs = s.frames[0][1] - 300 * NS.perSecond
        let f = fatals(s)
        XCTAssertTrue(f.contains("sync_too_old"), "\(s.validationReport())")
        XCTAssertFalse(s.isUsable)
    }

    /// 60초를 넘으면 경고. 촬영 자체는 막지 않습니다.
    func testSyncAgingWarns() {
        var s = makeValid()
        s.clockMeasuredAtNs = s.frames[0][1] - 120 * NS.perSecond
        XCTAssertTrue(codes(s).contains("sync_aging"))
        XCTAssertTrue(s.isUsable, "120초는 실효 6.84ms 로 반 프레임 안입니다")
    }

    /// 60초 안이면 아무 말도 하지 않아야 합니다.
    func testRecentSyncDoesNotWarn() {
        var s = makeValid()
        s.clockMeasuredAtNs = s.frames[0][1] - 50 * NS.perSecond
        XCTAssertFalse(codes(s).contains("sync_aging"))
        XCTAssertFalse(codes(s).contains("sync_too_old"))
    }

    /// ★ 앱이 경고하는 기준과 검증이 판정하는 기준이 같아야 합니다.
    /// 다르면 폰에서는 통과했는데 PC 에서 거부되는 일이 생깁니다.
    func testDriftConstantsAgreeAcrossTypes() {
        XCTAssertEqual(Sidecar.assumedDriftPpm,
                       ClockOffsetRecord.worstCaseDriftPpm,
                       "Sidecar 와 ClockOffsetRecord 의 드리프트 가정이 다릅니다")
    }

    func testFramesBeforeSyncWarns() {
        var s = makeValid()
        s.clockMeasuredAtNs = s.frames[0][1] + NS.perSecond
        XCTAssertTrue(codes(s).contains("frames_before_sync"))
    }

    func testSchemaVersionMismatchWarns() {
        var s = makeValid()
        s.schemaVersion = 99
        XCTAssertTrue(codes(s).contains("schema_version"))
    }

    // MARK: - 보고 형식

    /// 오차 상한이 목표(2ms) 안이면 info 조차 없어야 합니다.
    ///
    /// 실측 기본값 2.038ms 는 목표를 살짝 넘어 info 가 하나 붙으므로,
    /// 이 테스트는 "목표를 만족했을 때"의 이상적 상태를 확인합니다.
    /// (폰↔폰이나 유선 경로에서 이 값이 나올 수 있습니다)
    func testValidationReportOnFullyCleanSidecar() {
        var s = makeValid()
        s.clockUncertaintyNs = 1_900_000
        s.clockMinRttNs = 3_800_000
        XCTAssertEqual(s.validationReport(), ["문제 없음 ✔"],
                       "목표를 만족했는데도 문제가 보고됩니다: \(s.validationReport())")
    }

    /// 실측 기본값에서는 info 하나만 나와야 합니다.
    func testValidationReportOnMeasuredSidecar() {
        let r = makeValid().validationReport()
        XCTAssertEqual(r.count, 1, "실측 정상본에 보고가 여러 건입니다: \(r)")
        XCTAssertTrue(r[0].hasPrefix("[참고]"), r[0])
    }

    func testValidationReportMarksSeverity() {
        var s = makeValid()
        s.frames = []
        let r = s.validationReport().joined(separator: "\n")
        XCTAssertTrue(r.contains("[치명]"), r)
    }
}
