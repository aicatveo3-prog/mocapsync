import Foundation
import Network
import UIKit

// ─────────────────────────────────────────────────────────────────────────────
// 원격 촬영 — PC 마스터가 여러 폰을 한꺼번에 시작·정지시킵니다.
//
// ★ 왜 필요한가
//
// 3단계까지는 동기 측정이 끝나면 연결을 끊고, 녹화는 폰마다 버튼을 눌렀습니다.
// 그러면 여러 대를 같은 순간에 시작할 수 없습니다. 원격 모드에서는
//
//   1. 폰이 마스터에 연결해 클럭 동기를 재고 "remote_ready" 로 대기합니다
//   2. 마스터가 30초마다 sync_request 를 보내 오프셋을 신선하게 유지합니다
//      (수정발진자 드리프트 — 최악 40 ppm 이면 50초에 2 ms)
//   3. 마스터가 schedule_start 로 **공통 시각**을 알려주면, 각 폰이 그 시각을
//      자기 시계로 바꿔 그 이후의 첫 프레임부터 기록합니다 (Recorder 의 PTS 비교)
//   4. 마스터가 stop 을 보내면 파일을 마무리해 올리고 record_done 을 보냅니다
//
// ★ 설계 원칙: 폰은 **받은 명령에만 반응**합니다. 스스로 타이머를 돌지 않습니다.
//   개발자가 이 코드를 실기기에서 돌려볼 수 없으므로 동시성을 최소로 둡니다.
//   같은 상태기계를 server/slave_sim.py --remote 로 PC 에서 먼저 검증했습니다.
//
// ★ 명령이 엉뚱한 때 도착해도 버리지 않습니다 (awaitType + deferred).
//   동기 측정 도중 schedule_start 가 끼어들면, 측정 루프는 time_resp 만 기다리므로
//   그냥 버리면 "한 대만 안 찍힘"이 됩니다. 보관했다가 측정이 끝나면 처리합니다.
//
// 파일 업로드는 실기기에서 검증된 Uploader 를 그대로 씁니다 (별도 TCP 연결).
// ─────────────────────────────────────────────────────────────────────────────

@MainActor
final class RemoteLink: ObservableObject {

    enum Phase: Equatable {
        case idle
        case connecting
        case syncing(done: Int, total: Int)
        case ready
        case recording(String)
        case stopping
        case uploading
        case failed(String)

        var isConnected: Bool {
            switch self {
            case .idle, .failed: return false
            default: return true
            }
        }

        var label: String {
            switch self {
            case .idle: return "꺼짐"
            case .connecting: return "연결 중..."
            case .syncing(let d, let t): return "클럭 동기 중 \(d)/\(t)"
            case .ready: return "대기 중 — PC 에서 시작을 누르세요"
            case .recording(let s): return "녹화 중 (\(s))"
            case .stopping: return "파일 마무리 중..."
            case .uploading: return "PC 로 전송 중..."
            case .failed(let m): return "끊김: \(m)"
            }
        }
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var lastUncertaintyMs: Double?
    @Published private(set) var lastSyncAt: Date?
    @Published private(set) var events: [String] = []
    @Published private(set) var sessionsCompleted = 0

    /// 접속 직후 첫 동기: 권장 설정 (400회, 20x300ms 버스트)
    static let initialSync = SyncClient.Options.spread
    /// 주기 재측정: 가볍게 200회. 마스터가 30초마다 요청합니다.
    static let resync: SyncClient.Options = {
        var o = SyncClient.Options.spread
        o.probeCount = 200
        return o
    }()

    private weak var capture: CaptureCoordinator?
    private let uploader: Uploader
    private let deviceId: String
    private var host = ""
    private var port: UInt16 = Wire.defaultPort
    private var masterId = ""
    private var currentSession: String?

    private let netQueue = DispatchQueue(label: "mocapsync.remote", qos: .userInitiated)
    private var conn: NWConnection?
    private var framer = LineFramer()
    /// 수신 시각이 붙은 줄. 기다리는 쪽이 없을 때 쌓입니다.
    private var inbox: [(Data, Int64)] = []
    /// 기다리던 종류가 아니어서 미뤄 둔 줄. 메인 루프가 먼저 처리합니다.
    private var deferred: [(Data, Int64)] = []
    private var waiter: CheckedContinuation<(Data, Int64), Error>?
    private var closedError: Error?
    private var runTask: Task<Void, Never>?

    init(deviceId: String) {
        self.deviceId = deviceId
        self.uploader = Uploader(deviceId: deviceId)
    }

    // MARK: - 진입점

    func start(host: String, port: UInt16, capture: CaptureCoordinator) {
        guard !phase.isConnected else { return }
        self.host = host.trimmingCharacters(in: .whitespaces)
        self.port = port
        self.capture = capture
        events = []
        runTask = Task { await self.run() }
    }

    func stop() {
        guard phase.isConnected else { return }
        runTask?.cancel()
        // 수신 대기 중인 continuation 을 풀어 줍니다. 안 풀면 run() 이 영원히 멈춰 있습니다.
        fail(RemoteLink.err("사용자가 원격 대기를 껐습니다"))
        close()
        phase = .idle
        note("원격 대기 종료")
        if currentSession != nil {
            note("★ 녹화 중에 끊었습니다. 녹화는 이 폰에서 직접 멈추세요.")
        }
    }

    private func note(_ s: String) {
        events.append(s)
        if events.count > 40 { events.removeFirst(events.count - 40) }
        AppLog.shared.i("Remote", s)
    }

    // MARK: - 상태기계

    private func run() async {
        phase = .connecting
        note("연결 시도 \(host):\(port)")
        do {
            try await connect()

            try await sendLine(WireCodec.encodeLine(HelloMsg(
                deviceId: deviceId,
                name: UIDevice.current.name,
                platform: "ios",
                model: BuildInfo.deviceModelIdentifier,
                osVersion: BuildInfo.osVersion,
                appVersion: BuildInfo.versionFull,
                clock: MonotonicClock.name)))

            let (ack, _) = try await awaitType([Wire.MsgType.helloAck, Wire.MsgType.error])
            if try WireCodec.typeOf(ack) == Wire.MsgType.error.rawValue {
                let e = try WireCodec.decode(ErrorMsg.self, from: ack)
                throw RemoteLink.err("마스터가 거부: \(e.code ?? "?") \(e.message ?? "")")
            }
            let a = try WireCodec.decode(HelloAckMsg.self, from: ack)
            guard a.proto == Wire.protocolVersion else {
                throw WireCodec.WireError.protocolMismatch(ours: Wire.protocolVersion,
                                                           theirs: a.proto)
            }
            masterId = a.serverId ?? "\(host):\(port)"
            note("마스터 연결됨 (\(masterId))")

            try await doSync(RemoteLink.initialSync)
            try await sendStatus(.ready)
            phase = .ready
            note("원격 대기 중")

            while !Task.isCancelled {
                let (line, _) = try await nextAny()
                try await dispatch(line)
            }
        } catch {
            if !Task.isCancelled {
                phase = .failed(error.localizedDescription)
                note("연결 끊김: \(error.localizedDescription)")
            }
        }
        close()
    }

    private func dispatch(_ line: Data) async throws {
        let typeName = try WireCodec.typeOf(line)
        guard let t = Wire.MsgType(rawValue: typeName) else {
            note("(무시) 모르는 메시지 \(typeName)")
            return
        }
        switch t {
        case .syncRequest:
            // 녹화·업로드 중에는 재측정하지 않습니다. 마스터도 보내지 않지만 방어합니다.
            guard phase == .ready else { return }
            try await doSync(RemoteLink.resync)
            try await sendStatus(.ready)
            phase = .ready
        case .scheduleStart:
            try await onSchedule(try WireCodec.decode(ScheduleStartMsg.self, from: line))
        case .stop:
            try await onStop(try WireCodec.decode(StopMsg.self, from: line))
        case .timeResp:
            break   // 늦게 도착한 왕복 응답
        default:
            note("(무시) \(typeName)")
        }
    }

    private func onSchedule(_ m: ScheduleStartMsg) async throws {
        guard let cap = capture else {
            try await nack(m.sessionId, "no_camera: 녹화 화면이 닫혔습니다")
            return
        }
        if let cur = currentSession {
            try await nack(m.sessionId, "already_recording: \(cur)")
            return
        }
        let check = cap.remoteSchedule(sessionId: m.sessionId,
                                       startAtMasterNs: m.startAtMasterNs)
        guard check.ok else {
            try await nack(m.sessionId, check.reason, lead: check.leadNs)
            return
        }
        currentSession = m.sessionId
        try await sendLine(WireCodec.encodeLine(StartAckMsg(
            sessionId: m.sessionId, startAtSlaveNs: check.startAtSlaveNs,
            leadNs: check.leadNs)))
        try await sendStatus(.recording)
        phase = .recording(m.sessionId)
        note("▶ \(m.sessionId) 시작 예약 (\(Int(check.leadMs)) ms 뒤)")
    }

    private func nack(_ sid: String, _ reason: String, lead: Int64 = 0) async throws {
        try await sendLine(WireCodec.encodeLine(StartNackMsg(
            sessionId: sid, reason: reason, leadNs: lead)))
        note("★ 시작 거부: \(reason)")
    }

    private func onStop(_ m: StopMsg) async throws {
        guard m.sessionId == currentSession, let cap = capture else {
            note("stop 무시 (진행 중인 세션이 아님: \(m.sessionId))")
            return
        }
        phase = .stopping
        try await sendStatus(.stopping)
        note("■ \(m.sessionId) 정지")

        var files: [URL] = []
        var frames = 0
        var usable = false
        var fatal: [String] = []

        if let r = await cap.stopRecordingAndWait() {
            let chk = RemoteLink.selfCheck(r.sidecar)
            frames = chk.frames
            usable = chk.usable
            fatal = chk.fatal
            files = [r.sidecar, r.movie]
        } else {
            fatal = ["녹화 파일을 만들지 못했습니다. 이 폰의 로그를 확인하세요."]
        }

        phase = .uploading
        try await sendStatus(.uploading)
        var uploaded = false
        if !files.isEmpty {
            uploaded = await uploader.uploadAndWait(sessionId: m.sessionId, files: files,
                                                    host: host, port: port)
            if !uploaded {
                fatal.append("업로드 실패: \(uploader.log.last ?? "?")")
            }
        }

        try await sendLine(WireCodec.encodeLine(RecordDoneMsg(
            sessionId: m.sessionId,
            files: files.map(\.lastPathComponent),
            frames: frames,
            usable: usable && uploaded,
            fatal: fatal)))
        note("전송 \(uploaded ? "완료" : "★ 실패"): \(frames)프레임, "
             + (usable ? "자체검증 사용 가능" : "★ 자체검증 사용 불가"))

        currentSession = nil
        sessionsCompleted += 1
        try await sendStatus(.ready)
        phase = .ready
    }

    /// 방금 쓴 사이드카를 다시 읽어 검증합니다. 결과를 마스터에게 알려,
    /// 마스터의 Python 판정과 대조되게 합니다.
    static func selfCheck(_ url: URL) -> (frames: Int, usable: Bool, fatal: [String]) {
        guard let d = try? Data(contentsOf: url),
              let s = try? Sidecar.decoded(from: d) else {
            return (0, false, ["사이드카를 읽을 수 없습니다"])
        }
        let fatal = s.validate()
            .filter { $0.severity == .fatal }
            .map { "\($0.code): \($0.message)" }
        return (s.frameCount, fatal.isEmpty, fatal)
    }

    // MARK: - 클럭 동기
    //
    // SyncClient 의 측정 루프와 같은 절차입니다 (워밍업 -> 버스트 -> 최소 RTT).
    // 차이는 응답을 awaitType 으로 받는다는 것 하나입니다 — 측정 도중 끼어든
    // 명령을 버리지 않기 위해서입니다.

    private func doSync(_ opt: SyncClient.Options) async throws {
        let timeResp: Set<Wire.MsgType> = [.timeResp]

        for w in 0..<opt.warmupCount {
            let t1 = MonotonicClock.nowNs()
            try sendNoWait(WireCodec.encodeTimeReqLine(seq: -1 - w, t1: t1))
            _ = try await awaitType(timeResp)
        }

        var samples: [TimeSample] = []
        samples.reserveCapacity(opt.probeCount)
        // UI 갱신을 묶습니다. 매 왕복 @Published 를 건드리면 리렌더가 다음 t1 을
        // 늦춰 RTT 를 부풀립니다 (DESIGN.md 3.11 결함 2).
        let uiStride = max(1, opt.probeCount / 20)

        for seq in 0..<opt.probeCount {
            if seq % uiStride == 0, !isBusyRecording {
                phase = .syncing(done: seq, total: opt.probeCount)
            }
            if opt.burstSize > 0, seq > 0, seq % opt.burstSize == 0 {
                try await Task.sleep(nanoseconds: UInt64(opt.burstGapMs) * 1_000_000)
                for w in 0..<opt.burstWarmup {
                    let wt1 = MonotonicClock.nowNs()
                    try sendNoWait(WireCodec.encodeTimeReqLine(seq: -1000 - w, t1: wt1))
                    _ = try await awaitType(timeResp)
                }
            }
            // ★ t1 = 패킷 조립 직전, t4 = 소켓 콜백에서 찍은 값
            let t1 = MonotonicClock.nowNs()
            try sendNoWait(WireCodec.encodeTimeReqLine(seq: seq, t1: t1))
            let (line, t4) = try await awaitType(timeResp)
            let r = try WireCodec.decode(TimeRespMsg.self, from: line)
            guard r.seq >= 0 else { continue }
            samples.append(TimeSample(seq: r.seq, t1: r.t1, t2: r.t2, t3: r.t3, t4: t4))
        }

        let est = ClockSync.estimate(samples)
        let prof = ClockSync.rttProfile(samples)
        if est.samplesUsed > 0 {
            SyncStore.shared.record(est, masterId: masterId)
        }
        lastUncertaintyMs = est.uncertaintyMs
        lastSyncAt = Date()
        try await sendLine(WireCodec.encodeLine(
            TimeResultMsg(est, prof, config: opt.configString + "/remote")))
        note("동기 완료: 상한 \(String(format: "%.3f", est.uncertaintyMs)) ms "
             + "(최소RTT \(String(format: "%.3f", est.minRttMs)) ms)")
    }

    private var isBusyRecording: Bool {
        switch phase {
        case .recording, .stopping, .uploading: return true
        default: return false
        }
    }

    private func sendStatus(_ s: Wire.RemoteState) async throws {
        try await sendLine(WireCodec.encodeLine(StatusMsg(
            state: s.rawValue,
            framesCaptured: capture?.recorder.displayFrameCount ?? 0,
            battery: Double(UIDevice.current.batteryLevel),
            thermal: Recorder.thermalName())))
    }

    // MARK: - 수신 (명령을 버리지 않는 대기)

    /// 미뤄 둔 것부터 처리합니다.
    private func nextAny() async throws -> (Data, Int64) {
        if !deferred.isEmpty { return deferred.removeFirst() }
        return try await nextLine()
    }

    /// 원하는 종류가 올 때까지 읽고, 다른 것은 deferred 에 보관합니다.
    private func awaitType(_ types: Set<Wire.MsgType>) async throws -> (Data, Int64) {
        while true {
            let item = try await nextLine()
            if let t = Wire.MsgType(rawValue: try WireCodec.typeOf(item.0)), types.contains(t) {
                return item
            }
            deferred.append(item)
        }
    }

    private func nextLine() async throws -> (Data, Int64) {
        if !inbox.isEmpty { return inbox.removeFirst() }
        if let e = closedError { throw e }
        return try await withCheckedThrowingContinuation { cont in
            self.waiter = cont
        }
    }

    // MARK: - 소켓

    private func connect() async throws {
        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true                 // Nagle 끄기. 안 끄면 RTT 가 부풀어요.
        tcp.connectionTimeout = 10
        tcp.enableKeepalive = true
        tcp.keepaliveIdle = 10

        let params = NWParameters(tls: nil, tcp: tcp)
        params.includePeerToPeer = true
        // 시각 동기와 명령 전달용이라 음성 등급. 대용량 업로드는 Uploader 가
        // 별도 연결(.background)로 하므로 이 연결로는 작은 메시지만 오갑니다.
        params.serviceClass = .interactiveVoice

        let ep = NWEndpoint.hostPort(host: NWEndpoint.Host(host),
                                    port: NWEndpoint.Port(rawValue: port) ?? .any)
        let c = NWConnection(to: ep, using: params)
        conn = c
        framer = LineFramer()
        inbox = []
        deferred = []
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
                        cont.resume(throwing: RemoteLink.err("연결이 취소됐습니다"))
                    }
                case .waiting(let e):
                    AppLog.shared.w("Remote", "연결 대기: \(e)")
                default:
                    break
                }
            }
            c.start(queue: netQueue)
        }
    }

    private func receiveLoop() {
        guard let c = conn else { return }
        c.receive(minimumIncompleteLength: 1, maximumLength: 65536) {
            [weak self] data, _, isComplete, error in
            // ★ t4 를 찍는 자리. 다른 어떤 코드보다 먼저.
            let recvNs = MonotonicClock.nowNs()
            guard let self else { return }
            Task { @MainActor in
                if let data, !data.isEmpty {
                    for line in self.framer.feed(data) { self.deliver(line, recvNs) }
                }
                if let error { self.fail(error); return }
                if isComplete {
                    self.fail(RemoteLink.err("마스터가 연결을 닫았습니다"))
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

    /// 완료를 기다립니다. 명령·결과처럼 반드시 도착해야 하는 메시지용.
    private func sendLine(_ data: Data) async throws {
        guard let c = conn else { throw RemoteLink.err("연결이 없습니다") }
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
            c.send(content: data, completion: .contentProcessed { err in
                if let err { cont.resume(throwing: err) } else { cont.resume() }
            })
        }
    }

    /// 시각 왕복 전용. t1 이후에 async 홉을 넣지 않기 위해 기다리지 않습니다.
    private func sendNoWait(_ data: Data) throws {
        guard let c = conn else { throw RemoteLink.err("연결이 없습니다") }
        c.send(content: data, completion: .contentProcessed { err in
            if let err { AppLog.shared.e("Remote", "송신 실패: \(err)") }
        })
    }

    private func close() {
        conn?.cancel()
        conn = nil
    }

    /// 네트워크 콜백(메인 스레드 밖)에서도 부르므로 actor 에 묶지 않습니다.
    nonisolated private static func err(_ m: String) -> NSError {
        NSError(domain: "Remote", code: 1, userInfo: [NSLocalizedDescriptionKey: m])
    }
}
