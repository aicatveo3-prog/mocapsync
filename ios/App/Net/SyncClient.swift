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
        case warmingUp
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

    /// 한 번의 측정에 쓰는 설정. UI 에서 바꿔 실험할 수 있게 밖으로 뺐습니다.
    ///
    /// 2026-09-24 실측에서 최소 RTT 4.810 ms 로 목표(4 ms)를 못 맞췄기 때문에,
    /// 어떤 요인이 지배적인지 사용자가 직접 조합을 돌려볼 수 있어야 합니다.
    struct Options: Equatable {
        var probeCount: Int = 400
        var warmupCount: Int = SyncConfig.warmupCount
        var gapMs: Int = SyncConfig.probeGapMs

        // ── 버스트 (시간 분산 표본화) ────────────────────────────────────────
        //
        // ★ 왜 필요한가 — 2026-09-24 실측이 재현되지 않았습니다.
        //
        //   18:22  최소 RTT 4.435 ms
        //   18:58  최소 RTT 5.989 ms   (같은 빌드, 같은 설정)
        //
        // 35% 차이입니다. 원인: 120회 연사는 2초 만에 끝나므로 WiFi 상태의
        // **2초 창 하나**만 표본화합니다. 그 창이 나쁘면 최소 RTT 도 나쁩니다.
        // 같은 2초 안에서 왕복을 1000회로 늘려도 창이 바뀌지 않으니 소용없습니다.
        //
        // 필요한 것은 "여러 시간 창을 보는 것"인데, 그냥 간격을 주면 무선이
        // 매번 절전에 들어가 손해입니다. 둘을 동시에 만족시키는 방법이 버스트입니다.
        //   · 버스트 내부는 연사     -> 무선이 깨어 있음
        //   · 버스트 사이는 휴식     -> 다른 시간 창을 봄
        //   · 버스트마다 짧은 워밍업 -> 휴식 뒤 절전 기동시간을 본 표본에서 제외
        /// 버스트 하나에 넣을 측정 왕복 수. 0 이면 단일 버스트(연사).
        var burstSize: Int = 20
        /// 버스트 사이 휴식(ms)
        var burstGapMs: Int = 300
        /// 각 버스트 시작 시 버리는 왕복 수
        var burstWarmup: Int = 3

        /// 측정에 걸릴 대략적인 시간(초). UI 안내용.
        var estimatedSeconds: Double {
            guard burstSize > 0 else { return Double(probeCount) * 0.016 }
            let bursts = max(1, Int(ceil(Double(probeCount) / Double(burstSize))))
            let perProbe = 0.016 + Double(gapMs) / 1000.0
            return Double(bursts - 1) * Double(burstGapMs) / 1000.0
                 + Double(probeCount + bursts * burstWarmup) * perProbe
        }

        /// 단일 버스트 연사. 빠르지만 시간 창 하나만 봅니다.
        static let rapid = Options(probeCount: 120, warmupCount: 10, gapMs: 0,
                                   burstSize: 0, burstGapMs: 0, burstWarmup: 0)
        /// ★ 기본값. 20회씩 20버스트, 사이 300ms → 약 7초간 20개 시간 창을 봅니다.
        static let spread = Options(probeCount: 400, warmupCount: 10, gapMs: 0,
                                    burstSize: 20, burstGapMs: 300, burstWarmup: 3)
        /// 더 오래 흩뿌립니다. 약 30초.
        static let wide   = Options(probeCount: 600, warmupCount: 10, gapMs: 0,
                                    burstSize: 20, burstGapMs: 900, burstWarmup: 3)
        /// 2026-09-24 17:56 측정과 같은 조건. 비교 기준선.
        static let legacy = Options(probeCount: 40, warmupCount: 0, gapMs: 5,
                                    burstSize: 0, burstGapMs: 0, burstWarmup: 0)
    }

    @Published var phase: Phase = .idle
    @Published var masters: [Master] = []
    @Published var estimate: SyncEstimate?
    @Published var profile: RttProfile?
    @Published var lastServerId: String?
    @Published var options: Options = .spread
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

    init() {
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
        profile = nil
        Task { await self.session(endpoint: endpoint, label: label) }
    }

    private func session(endpoint: NWEndpoint, label: String) async {
        phase = .connecting(label)
        AppLog.shared.i("Sync", "연결 시도: \(label)")

        do {
            try await connect(endpoint)
            phase = .handshaking

            // 1) hello
            try await send(HelloMsg(
                deviceId: deviceId,
                name: UIDevice.current.name,
                platform: "ios",
                model: BuildInfo.deviceModelIdentifier,
                osVersion: BuildInfo.osVersion,
                // ★ 커밋 해시까지 보냅니다. PC 마스터 콘솔에서 어떤 빌드가
                //   접속했는지 바로 보이므로, 폰 화면을 안 봐도 판정됩니다.
                appVersion: BuildInfo.versionFull,
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

            try await send(StatusMsg(state: "idle",
                                     battery: Double(UIDevice.current.batteryLevel),
                                     thermal: thermalName()))

            let opt = options
            AppLog.shared.i("Sync", String(
                format: "측정 설정: 왕복 %d회, 워밍업 %d회, 간격 %dms, 버스트 %d회씩 휴식 %dms (예상 %.1f초)",
                opt.probeCount, opt.warmupCount, opt.gapMs,
                opt.burstSize, opt.burstGapMs, opt.estimatedSeconds))

            // ── 2) 워밍업 ────────────────────────────────────────────────────
            //
            // ★ 왜 버리는 왕복이 필요한가
            //
            // iOS 는 WiFi 무선을 공격적으로 절전시킵니다. 유휴 뒤 첫 패킷은
            // 무선을 깨우는 시간이 붙어 몇 ms 느립니다. 우리는 **최소** RTT 를
            // 쓰므로 느린 표본이 섞여도 결과가 나빠지진 않지만, 분포 진단이
            // 오염되고 무엇보다 "쉬었다 보내면 매번 다시 잠든다"는 문제가 있습니다.
            // 그래서 워밍업으로 무선을 깨우고, 본 측정은 간격 0 으로 연사합니다.
            if opt.warmupCount > 0 {
                phase = .warmingUp
                for seq in 0..<opt.warmupCount {
                    let t1 = MonotonicClock.nowNs()
                    try sendNoWait(WireCodec.encodeTimeReqLine(seq: -1 - seq, t1: t1))
                    _ = try await nextLine()   // 결과는 버립니다
                }
                AppLog.shared.i("Sync", "워밍업 \(opt.warmupCount)회 완료 (표본에서 제외)")
            }

            // ── 3) 본 측정 ───────────────────────────────────────────────────
            var samples: [TimeSample] = []
            samples.reserveCapacity(opt.probeCount)
            let t0 = MonotonicClock.nowNs()

            // ★ UI 갱신을 묶습니다.
            //
            // phase 는 @Published 이므로 대입할 때마다 SwiftUI 가 화면을 다시 그립니다.
            // 매 왕복마다 갱신하면 400회 측정에서 400번 리렌더가 MainActor 를 점유하고,
            // 그 지연이 "다음 왕복의 t1 을 찍기 전"에 들어가 RTT 를 부풀립니다.
            // 측정기가 스스로 측정값을 망치는 전형적인 형태입니다.
            let uiStride = max(1, opt.probeCount / 20)
            var burstCount = 1

            for seq in 0..<opt.probeCount {
                if seq % uiStride == 0 {
                    phase = .syncing(done: seq, total: opt.probeCount)
                }

                // ── 버스트 경계 ─────────────────────────────────────────────
                //
                // 휴식으로 다른 시간 창을 보고, 재개 직전 짧은 워밍업으로
                // 절전 기동시간을 본 표본에서 제외합니다.
                // 워밍업을 안 하면 각 버스트의 첫 왕복이 몇 ms 느려지는데,
                // 우리는 최소값만 쓰므로 결과는 안 나빠지지만 분포가 오염됩니다.
                if opt.burstSize > 0, seq > 0, seq % opt.burstSize == 0 {
                    burstCount += 1
                    try? await Task.sleep(nanoseconds: UInt64(opt.burstGapMs) * 1_000_000)
                    for w in 0..<opt.burstWarmup {
                        let wt1 = MonotonicClock.nowNs()
                        // 음수 seq = 버리는 왕복. 초기 워밍업(-1-n)과 겹치지 않게 -1000 부터.
                        try sendNoWait(WireCodec.encodeTimeReqLine(seq: -1000 - w, t1: wt1))
                        _ = try await nextLine()
                    }
                }

                // ★ t1 = 패킷 조립 직전. 고속 인코더라 여기서 µs 단위만 씁니다.
                //   JSONEncoder 를 쓰면 이 사이에 수십~수백 µs 가 끼어듭니다.
                let t1 = MonotonicClock.nowNs()
                try sendNoWait(WireCodec.encodeTimeReqLine(seq: seq, t1: t1))

                let (line, t4) = try await nextLine()   // ★ t4 = 소켓 콜백에서 찍힌 값
                let type = try WireCodec.typeOf(line)
                guard type == Wire.MsgType.timeResp.rawValue else {
                    AppLog.shared.w("Sync", "seq=\(seq) 예상과 다른 응답: \(type)")
                    continue
                }
                let r = try WireCodec.decode(TimeRespMsg.self, from: line)
                guard r.seq >= 0 else { continue }   // 뒤늦게 온 워밍업 응답 방어
                samples.append(TimeSample(seq: r.seq, t1: r.t1, t2: r.t2, t3: r.t3, t4: t4))

                if opt.gapMs > 0 {
                    try? await Task.sleep(nanoseconds: UInt64(opt.gapMs) * 1_000_000)
                }
            }

            let elapsed = MonotonicClock.nowNs() - t0
            let est = ClockSync.estimate(samples)
            let prof = ClockSync.rttProfile(samples)
            estimate = est
            profile = prof

            AppLog.shared.i("Sync", String(
                format: "동기 완료 %d회 %.2f초 (버스트 %d개) | 오프셋 %+.3fms | 최소RTT %.3fms | 상한 %.3fms | 흔들림 %.3fms",
                samples.count, Double(elapsed) / 1e9, burstCount,
                est.offsetMs, est.minRttMs, est.uncertaintyMs, est.spreadMs))
            AppLog.shared.i("Sync", "판정: \(est.verdict())")

            // ★ 분포를 로그에 남깁니다. 이게 다음 행동을 결정합니다.
            AppLog.shared.i("Sync", prof.summaryLine)
            for l in prof.histogramLines { AppLog.shared.i("Sync", l) }
            AppLog.shared.i("Sync", "분포 판정[\(prof.shape.rawValue)]: \(prof.diagnosis)")

            // ── 4) 결과 보고 ────────────────────────────────────────────────
            //
            // ★ await 로 송신 완료를 기다립니다.
            //   기다리지 않고 close() 를 부르면 연결이 취소되면서
            //   "송신 실패: POSIXErrorCode(rawValue: 89): Operation canceled" 가 납니다.
            //   (2026-09-24 실측 로그에서 실제로 발생했습니다)
            try await send(TimeResultMsg(est, prof))
            try await send(StatusMsg(state: "synced",
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

    /// 송신 완료를 **기다리는** 버전. hello / status / time_result 처럼
    /// "반드시 도착해야 하는" 메시지에 씁니다.
    ///
    /// 기다리지 않으면 바로 뒤의 close() 가 연결을 취소하면서 전송이 잘립니다.
    /// (POSIXErrorCode 89: Operation canceled)
    private func send<T: Encodable>(_ msg: T) async throws {
        guard let c = conn else {
            throw NSError(domain: "Sync", code: 5, userInfo: [
                NSLocalizedDescriptionKey: "연결이 없습니다"])
        }
        let data = try WireCodec.encodeLine(msg)
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
            // contentProcessed 는 정확히 한 번 호출되므로 continuation 규칙을 지킵니다.
            c.send(content: data, completion: .contentProcessed { err in
                if let err {
                    AppLog.shared.e("Sync", "송신 실패: \(err)")
                    cont.resume(throwing: err)
                } else {
                    cont.resume()
                }
            })
        }
    }

    /// 완료를 기다리지 않는 버전. **시각 왕복 전용**입니다.
    ///
    /// 여기서 기다리면 t1 이후에 async 홉이 하나 더 끼어들 여지가 생깁니다.
    /// time_req 는 응답(time_resp)이 곧바로 오므로 도착 여부는 그걸로 확인됩니다.
    /// 응답이 안 오면 nextLine() 이 영원히 기다리는 대신 연결 오류로 깨집니다.
    private func sendNoWait(_ data: Data) throws {
        guard let c = conn else {
            throw NSError(domain: "Sync", code: 5, userInfo: [
                NSLocalizedDescriptionKey: "연결이 없습니다"])
        }
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
