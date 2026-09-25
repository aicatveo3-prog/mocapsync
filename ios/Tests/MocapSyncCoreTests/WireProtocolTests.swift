import XCTest
@testable import MocapSyncCore

/// 규약 JSON 이 Python 구현(server/mocapsync/protocol.py)과 일치하는지 고정합니다.
///
/// 왜 이렇게까지 하는가:
/// 키 이름이 하나 어긋나면 통신이 **조용히** 실패합니다. 예외도 안 나고
/// 그냥 t2 가 0 으로 들어와서 오프셋이 엉뚱하게 계산됩니다. 그런 버그는
/// 실기기에서 몇 시간을 태웁니다. 그래서 키를 문자열로 못박아 둡니다.
final class WireProtocolTests: XCTestCase {

    func json(_ data: Data) throws -> [String: Any] {
        // 마지막 \n 을 떼고 파싱
        var d = data
        if d.last == 0x0A { d.removeLast() }
        let obj = try JSONSerialization.jsonObject(with: d)
        return obj as! [String: Any]
    }

    // MARK: - 줄 형식

    func testEncodedLineEndsWithNewline() throws {
        let d = try WireCodec.encodeLine(TimeReqMsg(seq: 0, t1: 123))
        XCTAssertEqual(d.last, 0x0A, "메시지는 반드시 \\n 으로 끝나야 합니다")
    }

    func testEncodedLineHasNoInnerNewline() throws {
        let d = try WireCodec.encodeLine(HelloMsg(
            deviceId: "abc", name: "iPhone-1", platform: "ios", model: "iPhone12,1",
            osVersion: "17.5.1", appVersion: "0.2.0", clock: "CLOCK_UPTIME_RAW"))
        let body = d.dropLast()
        XCTAssertFalse(body.contains(0x0A),
                       "메시지 본문에 \\n 이 있으면 프레이밍이 깨집니다")
    }

    // MARK: - 키 이름 고정 (Python 과의 계약)

    func testHelloKeys() throws {
        let d = try WireCodec.encodeLine(HelloMsg(
            deviceId: "DEV1", name: "iPhone-1", platform: "ios", model: "iPhone12,1",
            osVersion: "17.5.1", appVersion: "0.2.0", clock: "CLOCK_UPTIME_RAW"))
        let j = try json(d)
        XCTAssertEqual(j["type"] as? String, "hello")
        XCTAssertEqual(j["proto"] as? Int, 1)
        XCTAssertEqual(j["deviceId"] as? String, "DEV1")
        XCTAssertEqual(j["name"] as? String, "iPhone-1")
        XCTAssertEqual(j["platform"] as? String, "ios")
        XCTAssertEqual(j["model"] as? String, "iPhone12,1")
        XCTAssertEqual(j["osVersion"] as? String, "17.5.1")
        XCTAssertEqual(j["appVersion"] as? String, "0.2.0")
        XCTAssertEqual(j["clock"] as? String, "CLOCK_UPTIME_RAW")
        // 예상 키 집합과 정확히 일치해야 합니다 (오타로 추가된 키 방지)
        XCTAssertEqual(Set(j.keys),
                       ["type", "proto", "deviceId", "name", "platform",
                        "model", "osVersion", "appVersion", "clock"])
    }

    func testTimeReqKeys() throws {
        let d = try WireCodec.encodeLine(TimeReqMsg(seq: 7, t1: 520885686916))
        let j = try json(d)
        XCTAssertEqual(j["type"] as? String, "time_req")
        XCTAssertEqual(j["seq"] as? Int, 7)
        // Int64 가 JSON 에서 손실 없이 표현되는지 (2^53 보다 작으므로 안전)
        XCTAssertEqual((j["t1"] as? NSNumber)?.int64Value, 520885686916)
        XCTAssertEqual(Set(j.keys), ["type", "seq", "t1"])
    }

    func testTimeResultKeys() throws {
        let est = SyncEstimate(
            offsetNs: -1234567, minRttNs: 1420000, uncertaintyNs: 710000,
            spreadNs: 180000, samplesTotal: 40, samplesUsed: 1,
            samplesRejected: 0, medianRttNs: 2000000)
        let d = try WireCodec.encodeLine(TimeResultMsg(est))
        let j = try json(d)
        XCTAssertEqual(j["type"] as? String, "time_result")
        XCTAssertEqual((j["offsetNs"] as? NSNumber)?.int64Value, -1234567)
        XCTAssertEqual((j["minRttNs"] as? NSNumber)?.int64Value, 1420000)
        XCTAssertEqual((j["uncertaintyNs"] as? NSNumber)?.int64Value, 710000)
        XCTAssertEqual((j["spreadNs"] as? NSNumber)?.int64Value, 180000)
        XCTAssertEqual(j["samplesTotal"] as? Int, 40)
        XCTAssertEqual(j["samplesUsed"] as? Int, 1)
        XCTAssertEqual(j["samplesRejected"] as? Int, 0)
        XCTAssertEqual(Set(j.keys),
                       ["type", "offsetNs", "minRttNs", "uncertaintyNs", "spreadNs",
                        "samplesTotal", "samplesUsed", "samplesRejected",
                        "rttP0Ns", "rttP10Ns", "rttP50Ns", "rttP90Ns", "rttP100Ns",
                        "rttShape", "config"])
    }

    /// 측정 설정 문자열이 그대로 실려 나가는지.
    /// 이게 비면 마스터 로그에 "무슨 조건의 숫자인지"가 남지 않습니다.
    /// 업로드 예고 메시지의 키를 못박습니다.
    /// 키가 어긋나면 마스터가 size 를 0 으로 읽고, 그러면 파일이 빈 채로 저장됩니다.
    func testUploadBeginKeys() throws {
        let d = try WireCodec.encodeLine(UploadBeginMsg(
            sessionId: "S20260925-1", name: "ABC.json", size: 30_517))
        let j = try json(d)
        XCTAssertEqual(j["type"] as? String, "upload_begin")
        XCTAssertEqual(j["sessionId"] as? String, "S20260925-1")
        XCTAssertEqual(j["name"] as? String, "ABC.json")
        XCTAssertEqual(j["size"] as? Int, 30_517)
        XCTAssertEqual(Set(j.keys), ["type", "sessionId", "name", "size", "sha256"])
    }

    func testUploadResponsesDecode() throws {
        let ready = Data(#"{"type":"upload_ready","name":"A.json"}"#.utf8)
        XCTAssertEqual(try WireCodec.typeOf(ready), "upload_ready")
        XCTAssertEqual(try WireCodec.decode(UploadReadyMsg.self, from: ready).name, "A.json")

        let done = Data(#"{"type":"upload_done","name":"A.json","size":30517,"ok":true,"message":""}"#.utf8)
        let d = try WireCodec.decode(UploadDoneMsg.self, from: done)
        XCTAssertEqual(d.ok, true)
        XCTAssertEqual(d.size, 30_517)
    }

    func testTimeResultCarriesConfigString() throws {
        let est = SyncEstimate(
            offsetNs: 0, minRttNs: 1, uncertaintyNs: 0, spreadNs: 0,
            samplesTotal: 1, samplesUsed: 1, samplesRejected: 0, medianRttNs: 1)
        let cfg = "400회/20x300ms/워밍업10+3/간격0ms/음성우선"
        let j = try json(try WireCodec.encodeLine(TimeResultMsg(est, .empty, config: cfg)))
        XCTAssertEqual(j["config"] as? String, cfg)
    }

    /// RTT 분포가 실제로 실려 나가는지. 값이 0 으로 새면 마스터가 진단을 못 합니다.
    func testTimeResultCarriesRttProfile() throws {
        let est = SyncEstimate(
            offsetNs: 0, minRttNs: 4_435_000, uncertaintyNs: 2_217_500,
            spreadNs: 485_000, samplesTotal: 120, samplesUsed: 1,
            samplesRejected: 0, medianRttNs: 5_000_000)
        // 2026-09-24 18:22 실측을 닮은 분포 (최소 4.435ms)
        var s: [TimeSample] = []
        for i in 0..<120 {
            s.append(TimeSample(seq: i, t1: 0,
                                t2: 4_435_000 / 2 + Int64(i % 6) * 120_000,
                                t3: 4_435_000 / 2 + Int64(i % 6) * 120_000,
                                t4: 4_435_000 + Int64(i % 6) * 240_000))
        }
        let prof = ClockSync.rttProfile(s)
        let j = try json(try WireCodec.encodeLine(TimeResultMsg(est, prof)))

        XCTAssertEqual((j["rttP0Ns"] as? NSNumber)?.int64Value, prof.p0Ns)
        XCTAssertEqual((j["rttP50Ns"] as? NSNumber)?.int64Value, prof.p50Ns)
        XCTAssertEqual((j["rttP100Ns"] as? NSNumber)?.int64Value, prof.p100Ns)
        XCTAssertEqual(j["rttShape"] as? String, prof.shape.rawValue)
        XCTAssertGreaterThan(prof.p0Ns, 0, "분포가 비어 있으면 이 테스트가 무의미합니다")
    }

    /// 분포를 안 넘기면 0 으로 나가야 합니다 (마스터가 '측정 없음'으로 읽도록).
    func testTimeResultWithoutProfileIsZeroed() throws {
        let est = SyncEstimate(
            offsetNs: 1, minRttNs: 2, uncertaintyNs: 1, spreadNs: 0,
            samplesTotal: 1, samplesUsed: 1, samplesRejected: 0, medianRttNs: 2)
        let j = try json(try WireCodec.encodeLine(TimeResultMsg(est)))
        XCTAssertEqual((j["rttP0Ns"] as? NSNumber)?.int64Value, 0)
        XCTAssertEqual(j["rttShape"] as? String, "tooFewSamples")
    }

    func testStatusKeys() throws {
        let d = try WireCodec.encodeLine(
            StatusMsg(state: "synced", framesCaptured: 0, battery: 0.75, thermal: "nominal"))
        let j = try json(d)
        XCTAssertEqual(j["type"] as? String, "status")
        XCTAssertEqual(j["state"] as? String, "synced")
        XCTAssertEqual(j["framesCaptured"] as? Int, 0)
        XCTAssertEqual(j["battery"] as? Double, 0.75)
        XCTAssertEqual(j["thermal"] as? String, "nominal")
    }

    func testStatusOmitsNilFields() throws {
        let d = try WireCodec.encodeLine(StatusMsg(state: "idle"))
        let j = try json(d)
        // nil 은 키 자체가 빠져야 합니다 (null 이 아니라)
        XCTAssertNil(j["battery"])
        XCTAssertNil(j["thermal"])
        XCTAssertEqual(Set(j.keys), ["type", "state", "framesCaptured"])
    }

    // MARK: - 디코딩

    func testDecodeTimeResp() throws {
        let line = #"{"type":"time_resp","seq":3,"t1":100,"t2":200,"t3":205}"#
            .data(using: .utf8)!
        XCTAssertEqual(try WireCodec.typeOf(line), "time_resp")
        let m = try WireCodec.decode(TimeRespMsg.self, from: line)
        XCTAssertEqual(m.seq, 3)
        XCTAssertEqual(m.t1, 100)
        XCTAssertEqual(m.t2, 200)
        XCTAssertEqual(m.t3, 205)
    }

    func testDecodeHelloAck() throws {
        let line = #"{"type":"hello_ack","proto":1,"serverId":"a3bf7377cd88","impl":"py","sessionId":null}"#
            .data(using: .utf8)!
        let m = try WireCodec.decode(HelloAckMsg.self, from: line)
        XCTAssertEqual(m.proto, 1)
        XCTAssertEqual(m.serverId, "a3bf7377cd88")
        XCTAssertEqual(m.impl, "py")
        XCTAssertNil(m.sessionId)
    }

    /// 전방 호환: 모르는 필드가 있어도 디코딩이 깨지지 않아야 합니다.
    func testDecodeIgnoresUnknownFields() throws {
        let line = #"{"type":"time_resp","seq":1,"t1":1,"t2":2,"t3":3,"futureField":"뭔가"}"#
            .data(using: .utf8)!
        let m = try WireCodec.decode(TimeRespMsg.self, from: line)
        XCTAssertEqual(m.seq, 1)
    }

    func testDecodeScheduleStart() throws {
        let line = #"{"type":"schedule_start","sessionId":"S1","startAtMasterNs":123456789000000,"targetFps":60,"width":1920,"height":1080,"lockAe":true,"lockAwb":true,"lockFocus":true,"stabilization":"off","maxExposureNs":2000000}"#
            .data(using: .utf8)!
        let m = try WireCodec.decode(ScheduleStartMsg.self, from: line)
        XCTAssertEqual(m.sessionId, "S1")
        XCTAssertEqual(m.startAtMasterNs, 123_456_789_000_000)
        XCTAssertEqual(m.targetFps, 60)
        XCTAssertEqual(m.lockFocus, true)
    }

    func testTypeOfRejectsEmpty() {
        XCTAssertThrowsError(try WireCodec.typeOf(Data()))
    }

    // MARK: - 프레이머

    func testFramerSplitsLines() {
        var f = LineFramer()
        let lines = f.feed("{\"a\":1}\n{\"b\":2}\n".data(using: .utf8)!)
        XCTAssertEqual(lines.count, 2)
        XCTAssertEqual(WireCodec.text(lines[0]), "{\"a\":1}")
        XCTAssertEqual(WireCodec.text(lines[1]), "{\"b\":2}")
        XCTAssertEqual(f.pendingBytes, 0)
    }

    /// ★ TCP 는 메시지 경계를 보장하지 않습니다. 반쪽으로 쪼개져 와도 동작해야 합니다.
    func testFramerHandlesSplitAcrossPackets() {
        var f = LineFramer()
        XCTAssertTrue(f.feed("{\"ty".data(using: .utf8)!).isEmpty)
        XCTAssertTrue(f.feed("pe\":\"x\"".data(using: .utf8)!).isEmpty)
        let lines = f.feed("}\n".data(using: .utf8)!)
        XCTAssertEqual(lines.count, 1)
        XCTAssertEqual(WireCodec.text(lines[0]), "{\"type\":\"x\"}")
    }

    /// 두 메시지가 한 패킷에 붙어 오는 경우
    func testFramerHandlesCoalescedPackets() {
        var f = LineFramer()
        let lines = f.feed("{\"a\":1}\n{\"b\":2}\n{\"c\":3".data(using: .utf8)!)
        XCTAssertEqual(lines.count, 2)
        XCTAssertGreaterThan(f.pendingBytes, 0)
        let more = f.feed("}\n".data(using: .utf8)!)
        XCTAssertEqual(more.count, 1)
        XCTAssertEqual(WireCodec.text(more[0]), "{\"c\":3}")
    }

    func testFramerStripsCarriageReturn() {
        var f = LineFramer()
        let lines = f.feed("{\"a\":1}\r\n".data(using: .utf8)!)
        XCTAssertEqual(lines.count, 1)
        XCTAssertEqual(WireCodec.text(lines[0]), "{\"a\":1}")
    }

    func testFramerSkipsBlankLines() {
        var f = LineFramer()
        let lines = f.feed("\n\n{\"a\":1}\n\n".data(using: .utf8)!)
        XCTAssertEqual(lines.count, 1)
    }

    // MARK: - 왕복 전체 (규약 + 계산 결합)

    /// 규약으로 주고받은 값이 추정기에 제대로 흘러가는지 확인합니다.
    func testEndToEndSampleFromWire() throws {
        // 마스터가 보낸 time_resp 를 받아 t4 를 붙여 샘플을 만드는 흐름
        let respLine = #"{"type":"time_resp","seq":5,"t1":1000000,"t2":1001500,"t3":1001550,"t4":0}"#
            .data(using: .utf8)!
        let m = try WireCodec.decode(TimeRespMsg.self, from: respLine)
        let t4: Int64 = 1_003_000
        let s = TimeSample(seq: m.seq, t1: m.t1, t2: m.t2, t3: m.t3, t4: t4)

        // RTT = (t4-t1) - (t3-t2) = 3000 - 50 = 2950 ns
        XCTAssertEqual(s.rttNs, 2950)
        XCTAssertTrue(s.isSane)

        let est = ClockSync.estimate([s])
        XCTAssertEqual(est.minRttNs, 2950)
        XCTAssertEqual(est.uncertaintyNs, 1475)
        XCTAssertTrue(est.meetsTarget(), "1.475us 는 2ms 목표를 가볍게 통과해야 합니다")
    }
}
