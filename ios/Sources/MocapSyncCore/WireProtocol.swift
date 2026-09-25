import Foundation

// ─────────────────────────────────────────────────────────────────────────────
// 유선 규약 v1 — Swift 구현.
//
// server/mocapsync/protocol.py 와 **JSON 키가 정확히 일치해야** 합니다.
// 키 하나만 달라도 통신이 조용히 실패합니다 (오류도 안 나고 값만 0 이 됩니다).
// 그래서 WireProtocolTests 가 키 이름을 문자열로 못박아 둡니다.
//
// 이 파일은 MocapSyncCore 에 있습니다 = Network/UIKit 의존성 없음 = macOS CI 에서
// 시뮬레이터 없이 테스트됩니다.
// ─────────────────────────────────────────────────────────────────────────────

public enum Wire {
    public static let protocolVersion = 1
    /// Bonjour/NSNetService 용 (끝에 .local. 없음). Info.plist 의 NSBonjourServices 와 일치해야 합니다.
    public static let serviceType = "_mocapsync._tcp"
    public static let defaultPort: UInt16 = 9001

    public enum MsgType: String {
        case hello
        case helloAck = "hello_ack"
        case timeReq = "time_req"
        case timeResp = "time_resp"
        case timeResult = "time_result"
        case scheduleStart = "schedule_start"
        case startAck = "start_ack"
        case startNack = "start_nack"
        case stop
        case status
        case error
        // ── 파일 업로드 ─────────────────────────────────────────────────────
        //
        // 3단계까지 만든 영상과 사이드카를 PC 로 옮길 방법이 없었습니다.
        // USB 경로는 iTunes GUI 수동 조작이 필요하거나(사람 손), Windows 에서
        // pymobiledevice3 설치가 C 컴파일러를 요구해 막혔습니다.
        // 어차피 4단계가 WiFi 업로드이므로 그걸 먼저 만듭니다.
        case uploadBegin = "upload_begin"
        case uploadReady = "upload_ready"
        case uploadDone = "upload_done"
    }
}

// MARK: - 타입 판별용 (2단 디코딩의 1단계)

public struct WireEnvelope: Decodable {
    public let type: String
}

// MARK: - 보내는 메시지

public struct HelloMsg: Encodable {
    public let type = Wire.MsgType.hello.rawValue
    public let proto = Wire.protocolVersion
    public let deviceId: String
    public let name: String
    public let platform: String
    public let model: String
    public let osVersion: String
    public let appVersion: String
    public let clock: String

    public init(deviceId: String, name: String, platform: String, model: String,
                osVersion: String, appVersion: String, clock: String) {
        self.deviceId = deviceId
        self.name = name
        self.platform = platform
        self.model = model
        self.osVersion = osVersion
        self.appVersion = appVersion
        self.clock = clock
    }
}

public struct TimeReqMsg: Encodable {
    public let type = Wire.MsgType.timeReq.rawValue
    public let seq: Int
    public let t1: Int64

    public init(seq: Int, t1: Int64) {
        self.seq = seq
        self.t1 = t1
    }
}

public struct TimeResultMsg: Encodable {
    public let type = Wire.MsgType.timeResult.rawValue
    public let offsetNs: Int64
    public let minRttNs: Int64
    public let uncertaintyNs: Int64
    public let spreadNs: Int64
    public let samplesTotal: Int
    public let samplesUsed: Int
    public let samplesRejected: Int

    // ★ RTT 분포.
    //
    // 왜 규약에 넣는가: 최소 RTT 하나로는 "물리적 바닥"과 "표본 부족"을
    // 구분할 수 없습니다. 분포가 폰 화면에만 있으면 사용자가 매번 로그를
    // 내보내 붙여야 하고, 그만큼 디버깅 루프가 느려집니다.
    // 마스터 콘솔에서 바로 보이게 만듭니다.
    public let rttP0Ns: Int64
    public let rttP10Ns: Int64
    public let rttP50Ns: Int64
    public let rttP90Ns: Int64
    public let rttP100Ns: Int64
    /// "narrow" / "moderate" / "heavyTail" / "tooFewSamples".
    /// 마스터는 받은 백분위로 같은 판정을 독립 계산해서 이 값과 비교합니다.
    /// 다르면 두 구현이 갈라졌다는 뜻이므로 경고를 찍습니다.
    public let rttShape: String

    /// ★ 이 숫자를 만든 측정 설정. 사람이 읽는 기록용 문자열입니다.
    ///
    /// 왜 문자열 하나인가: 설정 항목이 늘 때마다 규약에 키를 추가하면 양쪽
    /// 구현과 테스트를 매번 고쳐야 합니다. 그리고 진짜 목적은
    /// "이 숫자가 어떤 조건에서 나왔는지"를 **사용자가 말해주지 않아도**
    /// 마스터 로그에 남기는 것입니다.
    public let config: String

    public init(_ e: SyncEstimate, _ p: RttProfile = .empty, config: String = "") {
        self.offsetNs = e.offsetNs
        self.minRttNs = e.minRttNs
        self.uncertaintyNs = e.uncertaintyNs
        self.spreadNs = e.spreadNs
        self.samplesTotal = e.samplesTotal
        self.samplesUsed = e.samplesUsed
        self.samplesRejected = e.samplesRejected
        self.rttP0Ns = p.p0Ns
        self.rttP10Ns = p.p10Ns
        self.rttP50Ns = p.p50Ns
        self.rttP90Ns = p.p90Ns
        self.rttP100Ns = p.p100Ns
        self.rttShape = p.shape.rawValue
        self.config = config
    }
}

public struct StatusMsg: Encodable {
    public let type = Wire.MsgType.status.rawValue
    public let state: String
    public let framesCaptured: Int
    public let battery: Double?
    public let thermal: String?

    public init(state: String, framesCaptured: Int = 0,
                battery: Double? = nil, thermal: String? = nil) {
        self.state = state
        self.framesCaptured = framesCaptured
        self.battery = battery
        self.thermal = thermal
    }
}

public struct StartAckMsg: Encodable {
    public let type = Wire.MsgType.startAck.rawValue
    public let sessionId: String
    public let startAtSlaveNs: Int64
    public let leadNs: Int64

    public init(sessionId: String, startAtSlaveNs: Int64, leadNs: Int64) {
        self.sessionId = sessionId
        self.startAtSlaveNs = startAtSlaveNs
        self.leadNs = leadNs
    }
}

public struct StartNackMsg: Encodable {
    public let type = Wire.MsgType.startNack.rawValue
    public let sessionId: String
    public let reason: String
    public let leadNs: Int64

    public init(sessionId: String, reason: String, leadNs: Int64) {
        self.sessionId = sessionId
        self.reason = reason
        self.leadNs = leadNs
    }
}

/// 파일 하나를 보내겠다는 예고.
///
/// ★ 이 줄 다음에 **정확히 size 바이트의 원본 데이터**를 그대로 보냅니다.
///   줄 단위 JSON 규약 안에 이진 데이터를 섞는 방법입니다. 수신측이
///   그만큼만 정확히 읽고 나면 다시 줄 모드로 돌아옵니다.
///
///   base64 로 감싸지 않는 이유: 영상이 수십 MB 라 33% 증가가 전송 시간과
///   메모리에 그대로 반영됩니다. 핫스팟처럼 느린 링크에서 체감이 큽니다.
public struct UploadBeginMsg: Encodable {
    public let type = Wire.MsgType.uploadBegin.rawValue
    public let sessionId: String
    public let name: String
    public let size: Int
    public let sha256: String

    public init(sessionId: String, name: String, size: Int, sha256: String = "") {
        self.sessionId = sessionId
        self.name = name
        self.size = size
        self.sha256 = sha256
    }
}

// MARK: - 받는 메시지

public struct HelloAckMsg: Decodable {
    public let proto: Int
    public let serverId: String?
    public let impl: String?
    public let sessionId: String?
}

public struct TimeRespMsg: Decodable {
    public let seq: Int
    public let t1: Int64
    public let t2: Int64
    public let t3: Int64
}

public struct ScheduleStartMsg: Decodable {
    public let sessionId: String
    public let startAtMasterNs: Int64
    public let targetFps: Int?
    public let width: Int?
    public let height: Int?
    public let lockAe: Bool?
    public let lockAwb: Bool?
    public let lockFocus: Bool?
    public let stabilization: String?
    public let maxExposureNs: Int64?
}

public struct ErrorMsg: Decodable {
    public let code: String?
    public let message: String?
}

public struct UploadReadyMsg: Decodable {
    public let name: String?
}

public struct UploadDoneMsg: Decodable {
    public let name: String?
    public let size: Int?
    public let ok: Bool?
    public let message: String?
}

// MARK: - 인코딩 / 디코딩

public enum WireCodec {

    public enum WireError: Error, CustomStringConvertible {
        case emptyLine
        case noType
        case protocolMismatch(ours: Int, theirs: Int)

        public var description: String {
            switch self {
            case .emptyLine: return "빈 줄"
            case .noType: return "'type' 필드 없음"
            case .protocolMismatch(let a, let b):
                return "규약 버전 불일치: 우리 \(a) != 상대 \(b)"
            }
        }
    }

    /// 줄바꿈으로 끝나는 한 줄 JSON 을 만듭니다.
    /// Python 쪽은 separators=(",",":") 로 공백을 없애는데, 공백 유무는 파싱에
    /// 영향이 없으므로 맞추지 않아도 됩니다.
    public static func encodeLine<T: Encodable>(_ msg: T) throws -> Data {
        let enc = JSONEncoder()
        // 키 순서를 고정해서 테스트와 로그 비교를 쉽게 합니다.
        enc.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        var d = try enc.encode(msg)
        d.append(0x0A)  // \n
        return d
    }

    public static func typeOf(_ line: Data) throws -> String {
        guard !line.isEmpty else { throw WireError.emptyLine }
        let env = try JSONDecoder().decode(WireEnvelope.self, from: line)
        return env.type
    }

    public static func decode<T: Decodable>(_ t: T.Type, from line: Data) throws -> T {
        try JSONDecoder().decode(t, from: line)
    }

    /// 디버깅용: 바이트를 사람이 읽는 문자열로
    public static func text(_ line: Data) -> String {
        String(data: line, encoding: .utf8) ?? "<비UTF8 \(line.count)바이트>"
    }
}

// MARK: - 줄 단위 프레이머

/// TCP 는 바이트 스트림이라 한 번에 받은 데이터가 메시지 경계와 일치하지 않습니다.
/// 반쪽 메시지가 오거나 두 메시지가 붙어서 올 수 있습니다.
/// 이 프레이머가 \n 기준으로 잘라줍니다.
///
/// ★ 각 줄에 **수신 시각**을 함께 담습니다.
/// t4 를 나중에 찍으면 async 홉 지연이 RTT 에 섞여 들어가므로,
/// 소켓 콜백에서 찍은 시각을 그대로 들고 다녀야 합니다.
public struct LineFramer {
    private var buffer = Data()
    public private(set) var totalBytes = 0

    public init() {}

    /// 받은 데이터를 넣고, 완성된 줄들을 돌려받습니다.
    public mutating func feed(_ data: Data) -> [Data] {
        totalBytes += data.count
        buffer.append(data)
        var lines: [Data] = []
        while let idx = buffer.firstIndex(of: 0x0A) {
            let line = buffer[buffer.startIndex..<idx]
            buffer = buffer[buffer.index(after: idx)...]
            // Windows 쪽에서 \r\n 으로 올 가능성에 대비
            var l = Data(line)
            if l.last == 0x0D { l.removeLast() }
            if !l.isEmpty { lines.append(l) }
        }
        // 슬라이스를 계속 쓰면 인덱스가 어긋나므로 재구성
        buffer = Data(buffer)
        return lines
    }

    public var pendingBytes: Int { buffer.count }
}

// MARK: - time_req 전용 고속 인코더

public extension WireCodec {

    /// `time_req` 한 줄을 직접 문자열로 조립합니다.
    ///
    /// ★ 왜 따로 만드는가
    ///
    /// t1 은 "보내기 직전"에 찍어야 정확합니다. 그런데 t1 을 찍은 **뒤에**
    /// JSONEncoder 가 돌아갑니다. JSONEncoder 는 리플렉션 기반이라 작은 구조체
    /// 하나에도 수십~수백 µs 가 듭니다. 그 시간이 전부 "t1 이후 ~ 패킷 출발 전"에
    /// 들어가므로 측정 RTT 를 그만큼 부풀립니다.
    ///
    /// 부풀어도 오차 상한 |오차| <= RTT/2 는 여전히 참이라 **틀리지는 않습니다**.
    /// 다만 보수적으로 나빠져서, 목표 판정에서 억울하게 미달이 날 수 있습니다.
    /// 왕복당 40회면 누적으로도 무시할 수 없습니다.
    ///
    /// 이 함수는 문자열 이어붙이기만 하므로 수 µs 로 끝납니다.
    ///
    /// ★ 키 순서는 JSONEncoder(.sortedKeys) 와 반드시 같아야 합니다: seq, t1, type.
    ///   두 인코더가 바이트 단위로 같은 결과를 내는지 WireProtocolTests 가 검증합니다.
    ///   (안 그러면 여기만 고치고 규약이 조용히 갈라집니다)
    static func encodeTimeReqLine(seq: Int, t1: Int64) -> Data {
        var s = ""
        s.reserveCapacity(48)
        s += #"{"seq":"#
        s += String(seq)
        s += #","t1":"#
        s += String(t1)
        s += #","type":"time_req"}"#
        s += "\n"
        return Data(s.utf8)
    }
}
