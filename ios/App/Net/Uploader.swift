import Foundation
import Network
import UIKit

// ─────────────────────────────────────────────────────────────────────────────
// 촬영 파일을 PC 마스터로 보냅니다.
//
// ★ 왜 이걸 지금 만드는가
//
// 3단계의 목적은 "Pose2Sim 에 넣을 수 있는 영상 + 타임스탬프"를 만드는 것입니다.
// 그런데 PC 로 옮길 방법이 없으면 3단계를 끝까지 검증할 수 없습니다.
//
// USB 경로를 먼저 검토했습니다.
//   · iTunes 파일 공유  — 사람이 매번 GUI 를 조작해야 합니다
//   · pymobiledevice3   — Windows 에서 C 컴파일러를 요구해 설치가 막혔습니다
// 어차피 4단계가 WiFi 업로드이므로 그걸 먼저 만드는 것이 낫습니다.
//
// 그리고 더 큰 이득이 있습니다: 마스터가 받은 사이드카를 **Python 검증기**에
// 바로 넣어 판정을 찍습니다. 폰(Swift)과 PC(Python)의 검증 구현이 같은 판정을
// 내는지 업로드마다 자동 대조됩니다. 지금까지는 실제 데이터로 확인한 적이
// 한 번도 없었습니다.
//
// ── 전송 방식 ───────────────────────────────────────────────────────────────
//
//   upload_begin (JSON 한 줄)  →  upload_ready  →  원본 바이트 size 만큼  →  upload_done
//
// base64 로 감싸지 않습니다. 영상이 수십 MB 라 33% 증가가 전송 시간과 메모리에
// 그대로 반영됩니다. 핫스팟처럼 느린 링크에서는 체감이 큽니다.
//
// ★ 메모리: 파일을 통째로 읽지 않고 `FileHandle` 로 조금씩 읽어 보냅니다.
//   25MB 영상을 한 번에 올리면 메모리 압박으로 앱이 죽을 수 있습니다.
// ─────────────────────────────────────────────────────────────────────────────

@MainActor
final class Uploader: ObservableObject {

    enum Phase: Equatable {
        case idle
        case browsing
        case connecting
        case sending(name: String, sent: Int, total: Int)
        case finished(files: Int, bytes: Int)
        case failed(String)

        var isBusy: Bool {
            switch self {
            case .idle, .finished, .failed: return false
            default: return true
            }
        }
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var log: [String] = []

    /// 한 번에 읽어 보낼 크기. 너무 크면 메모리, 너무 작으면 왕복이 많아집니다.
    private static let chunkSize = 256 * 1024

    private let netQueue = DispatchQueue(label: "mocapsync.upload", qos: .utility)
    private var conn: NWConnection?
    private var framer = LineFramer()
    private var inbox: [Data] = []
    private var waiter: CheckedContinuation<Data, Error>?
    private var closedError: Error?

    private let deviceId: String

    init(deviceId: String) { self.deviceId = deviceId }

    // MARK: - 진입점

    /// 세션 폴더의 파일을 전부 올립니다.
    ///
    /// ★ 순서가 중요합니다: **사이드카(.json)를 먼저** 보냅니다.
    ///   영상은 수십 MB 라 느린 링크에서 오래 걸립니다. 그 전에 사이드카가
    ///   도착해 있으면, 전송이 중간에 끊겨도 마스터가 검증 결과를 보여줄 수
    ///   있습니다. 개발 루프에서 가장 급한 정보가 그것입니다.
    func upload(sessionId: String, files: [URL],
                host: String, port: UInt16) {
        guard !phase.isBusy else { return }
        let ordered = files.sorted { a, b in
            let aj = a.pathExtension.lowercased() == "json"
            let bj = b.pathExtension.lowercased() == "json"
            if aj != bj { return aj }
            return a.lastPathComponent < b.lastPathComponent
        }
        log = []
        Task { await session(sessionId: sessionId, files: ordered,
                             host: host, port: port) }
    }

    private func append(_ s: String) {
        log.append(s)
        AppLog.shared.i("Upload", s)
    }

    private func session(sessionId: String, files: [URL],
                         host: String, port: UInt16) async {
        phase = .connecting
        append("연결 시도: \(host):\(port)")

        var sentFiles = 0
        var sentBytes = 0
        do {
            let ep = NWEndpoint.hostPort(host: NWEndpoint.Host(host),
                                        port: NWEndpoint.Port(rawValue: port) ?? .any)
            try await connect(ep)

            // hello 로 규약을 맞춥니다. 마스터가 슬레이브를 등록하는 데도 필요합니다.
            try await sendLine(WireCodec.encodeLine(HelloMsg(
                deviceId: deviceId,
                name: UIDevice.current.name,
                platform: "ios",
                model: BuildInfo.deviceModelIdentifier,
                osVersion: BuildInfo.osVersion,
                appVersion: BuildInfo.versionFull,
                clock: MonotonicClock.name)))

            let ack = try await nextLine()
            guard try WireCodec.typeOf(ack) == Wire.MsgType.helloAck.rawValue else {
                throw NSError(domain: "Upload", code: 1, userInfo: [
                    NSLocalizedDescriptionKey: "마스터가 hello 에 응답하지 않았습니다"])
            }
            append("마스터 연결됨")

            for url in files {
                let bytes = try sendFile(prepare: url)
                try await sendOne(sessionId: sessionId, url: url, size: bytes)
                sentFiles += 1
                sentBytes += bytes
            }

            phase = .finished(files: sentFiles, bytes: sentBytes)
            append("완료: \(sentFiles)개, \(String(format: "%.2f", Double(sentBytes) / 1_048_576)) MB")
        } catch {
            phase = .failed(error.localizedDescription)
            append("실패: \(error.localizedDescription)")
        }
        close()
    }

    /// 파일 크기를 읽어 둡니다 (보내기 전에 size 를 알려야 합니다).
    private func sendFile(prepare url: URL) throws -> Int {
        let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
        return (attrs[.size] as? NSNumber)?.intValue ?? 0
    }

    private func sendOne(sessionId: String, url: URL, size: Int) async throws {
        let name = url.lastPathComponent
        phase = .sending(name: name, sent: 0, total: size)
        append("\(name) \(String(format: "%.2f", Double(size) / 1_048_576)) MB 전송 시작")

        try await sendLine(WireCodec.encodeLine(UploadBeginMsg(
            sessionId: sessionId, name: name, size: size)))

        let ready = try await nextLine()
        let t = try WireCodec.typeOf(ready)
        if t == Wire.MsgType.uploadDone.rawValue {
            // 마스터가 곧바로 거부한 경우 (이름이나 크기가 이상함)
            let d = try WireCodec.decode(UploadDoneMsg.self, from: ready)
            throw NSError(domain: "Upload", code: 2, userInfo: [
                NSLocalizedDescriptionKey: "마스터가 거부: \(d.message ?? "?")"])
        }
        guard t == Wire.MsgType.uploadReady.rawValue else {
            throw NSError(domain: "Upload", code: 3, userInfo: [
                NSLocalizedDescriptionKey: "예상과 다른 응답: \(t)"])
        }

        // ★ 통째로 읽지 않고 조금씩 읽어 보냅니다.
        //   25MB 영상을 한 번에 메모리에 올리면 앱이 죽을 수 있습니다.
        let fh = try FileHandle(forReadingFrom: url)
        defer { try? fh.close() }

        var sent = 0
        while sent < size {
            let want = min(Uploader.chunkSize, size - sent)
            guard let chunk = try fh.read(upToCount: want), !chunk.isEmpty else { break }
            try await sendRaw(chunk)
            sent += chunk.count
            phase = .sending(name: name, sent: sent, total: size)
        }
        if sent != size {
            throw NSError(domain: "Upload", code: 4, userInfo: [
                NSLocalizedDescriptionKey: "파일을 끝까지 읽지 못했습니다 (\(sent)/\(size))"])
        }

        let done = try await nextLine()
        guard try WireCodec.typeOf(done) == Wire.MsgType.uploadDone.rawValue else {
            throw NSError(domain: "Upload", code: 5, userInfo: [
                NSLocalizedDescriptionKey: "완료 응답이 오지 않았습니다"])
        }
        let d = try WireCodec.decode(UploadDoneMsg.self, from: done)
        guard d.ok == true else {
            throw NSError(domain: "Upload", code: 6, userInfo: [
                NSLocalizedDescriptionKey: "마스터가 실패 보고: \(d.message ?? "?")"])
        }
        append("\(name) 전송 완료 (\(d.size ?? 0) 바이트 수신 확인)")
    }

    // MARK: - 소켓

    private func connect(_ endpoint: NWEndpoint) async throws {
        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true
        tcp.connectionTimeout = 15
        let params = NWParameters(tls: nil, tcp: tcp)
        params.includePeerToPeer = true
        // 업로드는 지연보다 처리량이 중요합니다. 시각 동기와 달리 우선순위를
        // 올리지 않습니다 — 대용량 전송을 음성 등급으로 보내면 다른 통신을 밀어냅니다.
        params.serviceClass = .background

        let c = NWConnection(to: endpoint, using: params)
        conn = c
        framer = LineFramer()
        inbox = []
        closedError = nil

        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
            var resumed = false
            c.stateUpdateHandler = { [weak self] st in
                switch st {
                case .ready:
                    if !resumed { resumed = true; cont.resume() }
                    Task { @MainActor in self?.receiveLoop() }
                case .failed(let e):
                    if !resumed { resumed = true; cont.resume(throwing: e) }
                    else { Task { @MainActor in self?.fail(e) } }
                case .cancelled:
                    if !resumed {
                        resumed = true
                        cont.resume(throwing: NSError(domain: "Upload", code: 7, userInfo: [
                            NSLocalizedDescriptionKey: "연결이 취소됐습니다"]))
                    }
                default: break
                }
            }
            c.start(queue: netQueue)
        }
    }

    private func receiveLoop() {
        guard let c = conn else { return }
        c.receive(minimumIncompleteLength: 1, maximumLength: 65536) {
            [weak self] data, _, isComplete, error in
            guard let self else { return }
            Task { @MainActor in
                if let data, !data.isEmpty {
                    for line in self.framer.feed(data) { self.deliver(line) }
                }
                if let error { self.fail(error); return }
                if isComplete {
                    self.fail(NSError(domain: "Upload", code: 8, userInfo: [
                        NSLocalizedDescriptionKey: "마스터가 연결을 닫았습니다"]))
                    return
                }
                self.receiveLoop()
            }
        }
    }

    private func deliver(_ line: Data) {
        if let w = waiter { waiter = nil; w.resume(returning: line) }
        else { inbox.append(line) }
    }

    private func fail(_ e: Error) {
        closedError = e
        if let w = waiter { waiter = nil; w.resume(throwing: e) }
    }

    private func nextLine() async throws -> Data {
        if !inbox.isEmpty { return inbox.removeFirst() }
        if let e = closedError { throw e }
        return try await withCheckedThrowingContinuation { self.waiter = $0 }
    }

    private func sendLine(_ data: Data) async throws { try await sendRaw(data) }

    /// ★ 반드시 완료를 기다립니다.
    ///
    /// 기다리지 않고 다음 덩어리를 밀어넣으면 송신 버퍼가 무한히 쌓여
    /// 메모리를 다 씁니다. 느린 링크(핫스팟)에서 특히 위험합니다.
    /// 여기서 기다리는 것이 곧 흐름 제어입니다.
    private func sendRaw(_ data: Data) async throws {
        guard let c = conn else {
            throw NSError(domain: "Upload", code: 9, userInfo: [
                NSLocalizedDescriptionKey: "연결이 없습니다"])
        }
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
            c.send(content: data, completion: .contentProcessed { err in
                if let err { cont.resume(throwing: err) } else { cont.resume() }
            })
        }
    }

    func close() {
        conn?.cancel()
        conn = nil
    }
}
