import Foundation
import Network
import UIKit

/// 슬레이브 측 클럭 동기 클라이언트.
///
/// 상대는 `server/master.py` (PC 테스트 마스터) 또는 나중에 마스터 역할의 다른 아이폰입니다.
/// 규약은 docs/PROTOCOL.md, 계산은 MocapSyncCore.ClockSync 를 씁니다.
///
/// ★ 정확도를 위해 반드시 지킨 것
///
/// 1. **TCP_NODELAY** (`noDelay = true`)
///    Nagle 알고리즘이 작은 패킷을 뭉쳐 보내면서 왕복시간을 수십 ms 부풀립니다.
///
/// 2. **t1/t4 를 소켓 경계에서 찍기**
///    t4 를 async 홉 뒤에서 찍으면 그 지연이 RTT 에 섞여 들어갑니다.
///    NWConnection 의 receive 콜백 **첫 줄**에서 찍어 줄과 함께 들고 다닙니다.
///    t1 은 send 호출 직전에 찍습니다.
///    (이렇게 하면 우리 쪽 처리시간이 RTT 에 조금 포함되는데, RTT 를 과대평가하는
///     방향이라 오차 상한 |오차| <= RTT/2 는 여전히 참입니다. 보수적이라 안전합니다)
///
/// 3. **전용 큐**
///    메인 스레드(UI)에서 처리하면 렌더링 때문에 지연이 튑니다.
@MainActor
final class SyncClient: ObservableObject {

    // MARK: - 상태

    enum Phase: Equatable {
        case idle
        case browsing
        case connecting(String)
        case handshaking
        case syncing(done: Int, total: Int)
        case finished
        case failed(String)

        var isBusy: Bool {
            switch self {
            case .idle, .finished, .failed: return false
            default: return true
            }
        }
    }

    struct Master: Identifiable, Equatable {
        let id: String        // 표시 이름
        let endpoint: NWEndpoint
        static func == (a: Master, b: Master) -> Bool { a.id == b.id }
    }

    @Published var phase: Phase = .idle
    @Published var masters: [Master] = []
    @Published var estimate: SyncEstimate?
    @Published var lastServerId: String?
    /// 로컬 네트워크 권한이 거부됐을 가능성 안내
    @Published var localNetworkHint = false

    // MARK: - 내부

    private let netQueue = DispatchQueue(label: "mocapsync.net", qos: .userInitiated)
    private var browser: NWBrowser?
    private var conn: NWConnection?
    private var framer = LineFramer()

    /// 수신 시각이 붙은 줄. (line, recvNs)
    private var inbox: [(Data, Int64)] = []
    private var waiter: CheckedContinuation<(Data, Int64), Error>?
    private var closedError: Error?

    private let deviceId: String
    private let probeCount: Int

    init(probeCount: Int = SyncConfig.probeCount) {
        self.probeCount = probeCount
        self.deviceId = SyncClient.stableDeviceId()
    }

    /// 기기 고정 ID.
    ///
    /// identifierForVendor 는 앱을 지우면 바뀝니다. 우리는 PC 가 device_id 로
    /// 캘리브레이션을 매칭하므로 재설치에도 유지되어야 합니다.
    /// 완전한 해법은 Keychain 이지만, 지금 단계에서는 UserDefaults 로 충분합니다.
    /// (3단계에서 Keychain 으로 올립니다)
    private static func stableDeviceId() -> String {
        let key = "mocapsync.deviceId"
        if let v = UserDefaults.standard.string(forKey: key) { return v }
        let v = (UIDevice.current.identifierForVendor?.uuidString
                 ?? UUID().uuidString)
            .replacingOccurrences(of: "-", with: "")
            .prefix(12)
            .uppercased()
        UserDefaults.standard.set(String(v), forKey: key)
        return String(v)
    }

    // MARK: - 탐색

    func startBrowsing() {
        stopBrowsing()
        masters = []
        localNetworkHint = false
        phase = .browsing
        AppLog.shared.i("Sync", "Bonjour 탐색 시작: \(Wire.serviceType)")

        let params = NWParameters()
        params.includePeerToPeer = true
        let b = NWBrowser(for: .bonjour(type: Wire.serviceType, domain: nil), using: params)

        b.stateUpdateHandler = { [weak self] st in
            Task { @MainActor in
                switch st {
                case .failed(let e):
                    AppLog.shared.e("Sync", "탐색 실패: \(e)")
                    self?.phase = .failed("탐색 실패: \(e.localizedDescription)")
                case .waiting(let e):
                    // 로컬 네트워크 권한이 거부되면 보통 여기서 멈춥니다
                    AppLog.shared.w("Sync", "탐색 대기: \(e)")
                    self?.localNetworkHint = true
                case .ready:
                    AppLog.shared.i("Sync", "탐색 준비됨")
                default:
                    break
                }
            }
        }

        b.browseResultsChangedHandler = { [weak self] results, _ in
            Task { @MainActor in
                guard let self else { return }
                self.masters = results.compactMap { r in
                    guard case let .service(name, _, _, _) = r.endpoint else { return nil }
                    return Master(id: name, endpoint: r.endpoint)
                }
                AppLog.shared.i("Sync", "마스터 \(self.masters.count)개 발견: "
                                + self.masters.map(\.id).joined(separator: ", "))
                if !self.masters.isEmpty { self.localNetworkHint = false }
            }
        }

        b.start(queue: netQueue)
        browser = b

        // 8초 안에 아무것도 못 찾으면 권한 안내를 띄웁니다
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 8_000_000_000)
            if self.masters.isEmpty, case .browsing = self.phase {
                self.localNetworkHint = true
                AppLog.shared.w("Sync", "8초간 마스터를 찾지 못했습니다")
            }
        }
    }

    func stopBrowsing() {
        browser?.cancel()
        browser = nil
    }

    // MARK: - 연결 + 동기 실행

    func run(master: Master) {
        run(endpoint: master.endpoint, label: master.id)
    }

    func run(host: String, port: UInt16) {
        let ep = NWEndpoint.hostPort(host: NWEndpoint.Host(host),
                                    port: NWEndpoint.Port(rawValue: port) ?? .any)
        run(endpoint: ep, label: "\(host):\(port)")
    }

    private func run(endpoint: NWEndpoint, label: String) {
        guard !phase.isBusy || phase == .browsing else { return }
        stopBrowsing()
        estimate = nil
        Task { await self.session(endpoint: endpoint, label: label) }
    }

    private func session(endpoint: NWEndpoint, label: String) async {
        phase = .connecting(label)
        AppLog.shared.i("Sync", "연결 시도: \(label)")

        do {
            try await connect(endpoint)
            phase = .handshaking

            // 1) hello
            try send(HelloMsg(
                deviceId: deviceId,
                name: UIDevice.current.name,
                platform: "ios",
                model: BuildInfo.deviceModelIdentifier,
                osVersion: BuildInfo.osVersion,
                appVersion: BuildInfo.versionName,
                clock: MonotonicClock.name))

            let (ackLine, _) = try await nextLine()
            let ackType = try WireCodec.typeOf(ackLine)
            if ackType == Wire.MsgType.error.rawValue {
                let e = try WireCodec.decode(ErrorMsg.self, from: ackLine)
                throw NSError(domain: "Sync", code: 1, userInfo: [
                    NSLocalizedDescriptionKey: "마스터가 거부: \(e.code ?? "?") \(e.message ?? "")"])
            }
            guard ackType == Wire.MsgType.helloAck.rawValue else {
                throw NSError(domain: "Sync", code: 2, userInfo: [
                    NSLocalizedDescriptionKey: "예상과 다른 응답: \(ackType)"])
            }
            let ack = try WireCodec.decode(HelloAckMsg.self, from: ackLine)
            guard ack.proto == Wire.protocolVersion else {
                throw WireCodec.WireError.protocolMismatch(
                    ours: Wire.protocolVersion, theirs: ack.proto)
            }
            lastServerId = ack.serverId
            AppLog.shared.i("Sync", "hello_ack (serverId=\(ack.serverId ?? "?"), impl=\(ack.impl ?? "?"))")

            try send(StatusMsg(state: "idle",
                               battery: Double(UIDevice.current.batteryLevel),
                               thermal: thermalName()))

            // 2) 시각 왕복
            var samples: [TimeSample] = []
            samples.reserveCapacity(probeCount)
            let t0 = MonotonicClock.nowNs()

            for seq in 0..<probeCount {
                phase = .syncing(done: seq, total: probeCount)

                // ★ t1 = send 직전
                let t1 = MonotonicClock.nowNs()
                try send(TimeReqMsg(seq: seq, t1: t1))

                let (line, t4) = try await nextLine()   // ★ t4 = 소켓 콜백에서 찍힌 값
                let type = try WireCodec.typeOf(line)
                guard type == Wire.MsgType.timeResp.rawValue else {
                    AppLog.shared.w("Sync", "seq=\(seq) 예상과 다른 응답: \(type)")
                    continue
                }
                let r = try WireCodec.decode(TimeRespMsg.self, from: line)
                samples.append(TimeSample(seq: r.seq, t1: r.t1, t2: r.t2, t3: r.t3, t4: t4))

                // 왕복 사이에 약간 쉬어야 큐잉 상태가 다양하게 샘플링됩니다
                try? await Task.sleep(nanoseconds: 5_000_000)
            }

            let elapsed = MonotonicClock.nowNs() - t0
            let est = ClockSync.estimate(samples)
            estimate = est

            AppLog.shared.i("Sync", String(
                format: "동기 완료 %d회 %.2f초 | 오프셋 %+.3fms | 최소RTT %.3fms | 상한 %.3fms | 흔들림 %.3fms",
                samples.count, Double(elapsed) / 1e9,
                est.offsetMs, est.minRttMs, est.uncertaintyMs, est.spreadMs))
            AppLog.shared.i("Sync", "판정: \(est.verdict())")

            // 3) 결과 보고
            try send(TimeResultMsg(est))
            try send(StatusMsg(state: "synced",
                               battery: Double(UIDevice.current.batteryLevel),
                               thermal: thermalName()))

            phase = .finished
        } catch {
            AppLog.shared.e("Sync", "실패: \(error)")
            phase = .failed(error.localizedDescription)
        }
        close()
    }

    // MARK: - 소켓

    private func connect(_ endpoint: NWEndpoint) async throws {
        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true                 // ★ Nagle 끄기. 안 끄면 RTT 가 부풀어요.
        tcp.connectionTimeout = 10
        tcp.enableKeepalive = true
        tcp.keepaliveIdle = 10

        let params = NWParameters(tls: nil, tcp: tcp)
        params.includePeerToPeer = true
        params.serviceClass = .responsiveData

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
                    if !resumed { resumed = true
                        cont.resume(throwing: NSError(domain: "Sync", code: 3, userInfo: [
                            NSLocalizedDescriptionKey: "연결이 취소됐습니다"])) }
                case .waiting(let e):
                    AppLog.shared.w("Sync", "연결 대기: \(e)")
                default:
                    break
                }
            }
            c.start(queue: netQueue)
        }
    }

    private func receiveLoop() {
        guard let c = conn else { return }
        c.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, isComplete, error in
            // ★★ 여기가 t4 를 찍는 자리입니다. 다른 어떤 코드보다 먼저.
            let recvNs = MonotonicClock.nowNs()

            guard let self else { return }
            Task { @MainActor in
                if let data, !data.isEmpty {
                    for line in self.framer.feed(data) {
                        self.deliver(line, recvNs)
                    }
                }
                if let error {
                    self.fail(error)
                    return
                }
                if isComplete {
                    self.fail(NSError(domain: "Sync", code: 4, userInfo: [
                        NSLocalizedDescriptionKey: "마스터가 연결을 닫았습니다"]))
                    return
                }
                self.receiveLoop()
            }
        }
    }

    private func deliver(_ line: Data, _ recvNs: Int64) {
        if let w = waiter {
            waiter = nil
            w.resume(returning: (line, recvNs))
        } else {
            inbox.append((line, recvNs))
        }
    }

    private func fail(_ error: Error) {
        closedError = error
        if let w = waiter {
            waiter = nil
            w.resume(throwing: error)
        }
    }

    private func nextLine() async throws -> (Data, Int64) {
        if !inbox.isEmpty { return inbox.removeFirst() }
        if let e = closedError { throw e }
        return try await withCheckedThrowingContinuation { cont in
            self.waiter = cont
        }
    }

    private func send<T: Encodable>(_ msg: T) throws {
        guard let c = conn else {
            throw NSError(domain: "Sync", code: 5, userInfo: [
                NSLocalizedDescriptionKey: "연결이 없습니다"])
        }
        let data = try WireCodec.encodeLine(msg)
        c.send(content: data, completion: .contentProcessed { err in
            if let err { AppLog.shared.e("Sync", "송신 실패: \(err)") }
        })
    }

    func close() {
        conn?.cancel()
        conn = nil
    }

    private func thermalName() -> String {
        switch ProcessInfo.processInfo.thermalState {
        case .nominal: return "nominal"
        case .fair: return "fair"
        case .serious: return "serious"
        case .critical: return "critical"
        @unknown default: return "unknown"
        }
    }
}
