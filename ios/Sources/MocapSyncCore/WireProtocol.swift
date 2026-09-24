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

    public init(_ e: SyncEstimate) {
        self.offsetNs = e.offsetNs
        self.minRttNs = e.minRttNs
        self.uncertaintyNs = e.uncertaintyNs
        self.spreadNs = e.spreadNs
        self.samplesTotal = e.samplesTotal
        self.samplesUsed = e.samplesUsed
        self.samplesRejected = e.samplesRejected
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
