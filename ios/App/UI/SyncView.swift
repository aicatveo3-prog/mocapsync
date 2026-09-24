import SwiftUI

/// 클럭 동기 측정 화면.
///
/// 화면 설계 원칙: **가장 크게 띄우는 숫자는 '오차 상한'입니다.**
///
/// 오프셋 자체는 크게 보여줘도 의미가 없습니다. 맞는지 알 수 없으니까요.
/// 반면 오차 상한(= 최소RTT/2)은 "이 값보다 나쁠 수 없다"고 **증명된** 수치입니다.
/// 설계 문서의 "오차 2ms 미만" 목표를 판정할 수 있는 유일한 숫자입니다.
/// 자세한 근거는 docs/PROTOCOL.md §4.3.
struct SyncView: View {
    @StateObject private var client = SyncClient()
    @State private var manualHost = ""
    @State private var manualPort = "9001"
    @State private var shareURL: URL?
    @State private var showShare = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                verdictCard
                if let e = client.estimate { detailCard(e) }
                if let p = client.profile { profileCard(p) }
                phaseCard
                optionsCard
                mastersCard
                manualCard
                explainCard
            }
            .padding(16)
        }
        .background(Color(red: 0.063, green: 0.078, blue: 0.094))
        .navigationTitle("클럭 동기")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { if client.phase == .idle { client.startBrowsing() } }
        .onDisappear { client.stopBrowsing(); client.close() }
        .sheet(isPresented: $showShare) {
            if let u = shareURL { ShareSheet(items: [u]) }
        }
    }

    // MARK: - 큰 숫자

    private var verdictCard: some View {
        let e = client.estimate
        let ok = e?.meetsTarget() ?? false
        let color: Color = e == nil ? .gray : (ok ? .green : .orange)

        return VStack(alignment: .leading, spacing: 6) {
            Text("오차 상한 (증명된 값)")
                .font(.caption).foregroundStyle(.secondary)

            if let e {
                Text(String(format: "%.3f ms", e.uncertaintyMs))
                    .font(.system(size: 52, weight: .black, design: .rounded))
                    .foregroundStyle(color)
                    .monospacedDigit()
                Text(ok ? "통과 — 목표 2 ms 미만" : "미달 — 목표 2 ms 이상")
                    .font(.headline).foregroundStyle(color)
                Text("이 값보다 나쁠 수 없습니다. 최소 RTT의 절반이며 수학적으로 보장됩니다.")
                    .font(.caption).foregroundStyle(.secondary).padding(.top, 2)
            } else {
                Text("— ms")
                    .font(.system(size: 52, weight: .black, design: .rounded))
                    .foregroundStyle(.gray)
                Text("아직 측정하지 않았습니다")
                    .font(.headline).foregroundStyle(.secondary)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(color.opacity(0.12), in: RoundedRectangle(cornerRadius: 14))
    }

    private func detailCard(_ e: SyncEstimate) -> some View {
        Card(title: "측정값") {
            KV("오프셋", String(format: "%+.3f ms   (%+d ns)", e.offsetMs, e.offsetNs))
            KV("최소 RTT", String(format: "%.3f ms", e.minRttMs))
            KV("오차 상한", String(format: "%.3f ms   = 최소RTT / 2", e.uncertaintyMs))
            KV("실측 흔들림", String(format: "%.3f ms", e.spreadMs))
            KV("중앙값 RTT", String(format: "%.3f ms", Double(e.medianRttNs) / Double(NS.perMilli)))
            KV("샘플", "사용 \(e.samplesUsed) / 전체 \(e.samplesTotal) (폐기 \(e.samplesRejected))")

            Divider().padding(.vertical, 4)
            Text(spreadNote(e)).font(.caption).foregroundStyle(.secondary)

            Button {
                if let u = LogExporter.writeToTemp() {
                    shareURL = u
                    showShare = true
                }
            } label: {
                Text("결과 공유")
                    .frame(maxWidth: .infinity).padding(.vertical, 10)
                    .background(Color.green.opacity(0.85), in: RoundedRectangle(cornerRadius: 10))
                    .foregroundStyle(.black)
            }
            .padding(.top, 6)
        }
    }

    /// spread 가 상한보다 크면 뭔가 이상합니다 (시계 드리프트, 스로틀링, 백그라운드 전환)
    private func spreadNote(_ e: SyncEstimate) -> String {
        if e.samplesUsed == 0 { return "유효 샘플이 없습니다." }
        if e.spreadNs > e.uncertaintyNs * 3 {
            return "★ 실측 흔들림이 오차 상한보다 훨씬 큽니다. 시계 드리프트나 발열 스로틀링, "
                + "앱이 백그라운드로 내려갔을 가능성을 의심하세요. 화면을 켠 채 다시 측정해 보세요."
        }
        return "실측 흔들림이 오차 상한과 비슷하거나 작으면 정상입니다. "
            + "크면 시계가 불안정하다는 뜻입니다."
    }

    // MARK: - RTT 분포

    /// ★ 이 카드가 "다음에 무엇을 할지"를 알려줍니다.
    ///
    /// 최소 RTT 숫자 하나로는 두 상황을 구분할 수 없습니다.
    ///   · 이미 물리적 바닥 -> 왕복을 늘려도 소용없다. 경로를 바꿔야 한다.
    ///   · 표본 부족        -> 왕복을 늘리면 내려간다.
    /// p0 와 p50 의 간격이 그걸 알려줍니다.
    private func profileCard(_ p: RttProfile) -> some View {
        Card(title: "RTT 분포 — 다음에 할 일") {
            Text(p.diagnosis)
                .font(.footnote)
                .foregroundStyle(p.shape == .narrow ? .orange : .primary)

            Divider().padding(.vertical, 4)

            KV("최소 (p0)", String(format: "%.3f ms", p.p0Ms))
            KV("하위 10%", String(format: "%.3f ms", p.p10Ms))
            KV("중앙값 (p50)", String(format: "%.3f ms", p.p50Ms))
            KV("상위 10% (p90)", String(format: "%.3f ms", p.p90Ms))
            KV("최대", String(format: "%.3f ms", p.p100Ms))

            if !p.histogramLines.isEmpty {
                Text(p.histogramLines.joined(separator: "\n"))
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .padding(.top, 6)
            }

            Text("목표선: 최소 RTT 4 ms 미만이면 오차 상한 2 ms 미만입니다.")
                .font(.caption2).foregroundStyle(.secondary).padding(.top, 4)
        }
    }

    // MARK: - 측정 설정

    private var optionsCard: some View {
        Card(title: "측정 설정") {
            Picker("방식", selection: Binding(
                get: { preset(client.options) },
                set: { client.options = $0.options })) {
                    ForEach(Preset.allCases) { p in Text(p.label).tag(p) }
                }
                .pickerStyle(.segmented)

            Text(preset(client.options).help)
                .font(.caption2).foregroundStyle(.secondary).padding(.top, 2)

            KV("왕복", "\(client.options.probeCount)회")
            KV("버스트", client.options.burstSize > 0
                ? "\(client.options.burstSize)회씩, 사이 \(client.options.burstGapMs)ms 휴식"
                : "없음 (단일 연사)")
            KV("예상 소요", String(format: "%.1f초", client.options.estimatedSeconds))

            Divider().padding(.vertical, 6)

            Text("통신 우선순위").font(.caption).foregroundStyle(.secondary)
            Picker("우선순위", selection: Binding(
                get: { client.options.priority },
                set: { client.options.priority = $0 })) {
                    ForEach(SyncClient.NetPriority.allCases) { p in
                        Text(p.label).tag(p)
                    }
                }
                .pickerStyle(.segmented)

            Text(client.options.priority.help)
                .font(.caption2).foregroundStyle(.secondary).padding(.top, 2)

            Divider().padding(.vertical, 6)

            Text("""
                 ★ 왜 '분산'이 기본인가

                 2026-09-24 실측이 재현되지 않았습니다.
                   18:22  최소 RTT 4.435 ms
                   18:58  최소 RTT 5.989 ms   (같은 빌드·같은 설정)

                 120회 연사는 2초 만에 끝나므로 WiFi 상태의 2초 창 하나만
                 봅니다. 그 창이 나쁘면 결과도 나쁩니다. 같은 2초 안에서
                 왕복을 1000회로 늘려도 창이 안 바뀌니 소용없습니다.

                 '분산'은 20회씩 연사하고 300ms 쉬기를 20번 반복해
                 20개의 서로 다른 시간 창을 봅니다. 버스트 내부는 연사라
                 무선이 깨어 있고, 버스트 시작 시 3회를 버려 절전 기동시간을
                 표본에서 제외합니다.
                 """)
                .font(.caption2).foregroundStyle(.secondary)

            Divider().padding(.vertical, 6)

            Text("""
                 ★ 최소 RTT 를 더 줄이려면 (효과가 큰 순서)

                 1. PC 를 유선 랜에 연결하세요. PC 도 WiFi 면 왕복 한 번에
                    공중을 4번 건너갑니다(폰→AP→PC→AP→폰). 유선이면 2번입니다.
                    이게 가장 큰 차이를 만듭니다.
                 2. 폰을 5GHz WiFi 에 연결하세요. 2.4GHz 는 혼잡해서 느립니다.
                 3. 공유기에 가까이 가세요.
                 4. '분산' 또는 '넓게 분산' 으로 여러 시간 창을 보세요.
                 """)
                .font(.caption2).foregroundStyle(.secondary)
        }
    }

    /// 측정 방식 프리셋. 사용자가 조합을 직접 맞추지 않아도 되게 묶었습니다.
    private enum Preset: String, CaseIterable, Identifiable {
        case rapid, spread, wide, legacy
        var id: String { rawValue }

        var options: SyncClient.Options {
            switch self {
            case .rapid:  return .rapid
            case .spread: return .spread
            case .wide:   return .wide
            case .legacy: return .legacy
            }
        }
        var label: String {
            switch self {
            case .rapid:  return "연사"
            case .spread: return "분산"
            case .wide:   return "넓게"
            case .legacy: return "기준선"
            }
        }
        var help: String {
            switch self {
            case .rapid:
                return "120회를 2초에 몰아칩니다. 빠르지만 WiFi 상태의 시간 창 하나만 봅니다."
            case .spread:
                return "★ 권장. 20회씩 20버스트, 약 7초간 20개 시간 창을 봅니다."
            case .wide:
                return "20회씩 30버스트, 약 30초. 가장 낮은 최소 RTT 를 찾을 확률이 높습니다."
            case .legacy:
                return "2026-09-24 17:56 측정과 동일한 조건(40회·간격 5ms·워밍업 없음). 비교용."
            }
        }
    }

    /// 현재 옵션이 어떤 프리셋인지.
    ///
    /// ★ 우선순위는 프리셋과 독립된 축이므로 비교에서 제외합니다.
    ///   안 그러면 우선순위를 바꿀 때마다 프리셋 선택이 '분산'으로 튑니다.
    private func preset(_ o: SyncClient.Options) -> Preset {
        Preset.allCases.first { p in
            var q = p.options
            q.priority = o.priority
            return q == o
        } ?? .spread
    }

    // MARK: - 진행 상태

    private var phaseCard: some View {
        Card(title: "상태") {
            switch client.phase {
            case .idle:
                Text("대기 중").font(.subheadline)
            case .browsing:
                HStack { ProgressView(); Text("마스터 찾는 중...").font(.subheadline) }
            case .connecting(let l):
                HStack { ProgressView(); Text("연결 중: \(l)").font(.subheadline) }
            case .handshaking:
                HStack { ProgressView(); Text("규약 확인 중...").font(.subheadline) }
            case .warmingUp:
                HStack { ProgressView(); Text("WiFi 무선 깨우는 중 (워밍업)...").font(.subheadline) }
            case .syncing(let d, let t):
                VStack(alignment: .leading, spacing: 6) {
                    Text("시각 왕복 \(d) / \(t)").font(.subheadline).monospacedDigit()
                    ProgressView(value: Double(d), total: Double(t))
                }
            case .finished:
                Text("완료").font(.subheadline).foregroundStyle(.green)
            case .failed(let m):
                Text("실패: \(m)").font(.subheadline).foregroundStyle(.red)
            }

            if let sid = client.lastServerId {
                KV("마스터 ID", sid)
            }

            if client.phase == .finished || isFailed {
                Button("다시 측정") { client.startBrowsing() }
                    .padding(.top, 8)
            }
        }
    }

    private var isFailed: Bool {
        if case .failed = client.phase { return true }
        return false
    }

    // MARK: - 마스터 목록

    private var mastersCard: some View {
        Card(title: "발견된 마스터") {
            if client.masters.isEmpty {
                Text("없음").font(.subheadline).foregroundStyle(.secondary)
            } else {
                ForEach(client.masters) { m in
                    Button {
                        client.run(master: m)
                    } label: {
                        HStack {
                            Text(m.id).font(.subheadline)
                            Spacer()
                            Text("측정 시작").font(.caption).foregroundStyle(.green)
                        }
                        .padding(.vertical, 8)
                    }
                    .disabled(client.phase.isBusy && client.phase != .browsing)
                    Divider()
                }
            }

            if client.localNetworkHint {
                VStack(alignment: .leading, spacing: 4) {
                    Text("★ 마스터를 못 찾고 있습니다").font(.caption.bold()).foregroundStyle(.orange)
                    Text("""
                         확인할 것:
                         1. PC 에서 `python server/master.py` 가 돌고 있는지
                         2. 폰과 PC 가 **같은 WiFi** 인지 (셀룰러 데이터 끄면 확실합니다)
                         3. 설정 → 개인정보 보호 및 보안 → **로컬 네트워크** → MocapSync 허용
                         4. PC 방화벽이 9001 포트를 막고 있는지
                         """)
                        .font(.caption2).foregroundStyle(.secondary)
                }
                .padding(10)
                .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
                .padding(.top, 6)
            }
        }
    }

    // MARK: - 직접 입력

    private var manualCard: some View {
        Card(title: "IP 직접 입력 (Bonjour 실패 시)") {
            HStack(spacing: 8) {
                TextField("192.168.0.10", text: $manualHost)
                    .textFieldStyle(.roundedBorder)
                    .keyboardType(.numbersAndPunctuation)
                    .autocorrectionDisabled()
                    .textInputAutocapitalization(.never)
                TextField("9001", text: $manualPort)
                    .textFieldStyle(.roundedBorder)
                    .keyboardType(.numberPad)
                    .frame(width: 80)
            }
            Button {
                let p = UInt16(manualPort) ?? Wire.defaultPort
                client.run(host: manualHost.trimmingCharacters(in: .whitespaces), port: p)
            } label: {
                Text("이 주소로 측정")
                    .frame(maxWidth: .infinity).padding(.vertical, 10)
                    .background(Color.white.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
            }
            .disabled(manualHost.isEmpty || (client.phase.isBusy && client.phase != .browsing))
            .padding(.top, 6)

            Text("PC 마스터를 실행하면 콘솔에 IP 가 찍힙니다.")
                .font(.caption2).foregroundStyle(.secondary)
        }
    }

    // MARK: - 설명

    private var explainCard: some View {
        Card(title: "이 숫자가 왜 증명되는가") {
            Text("""
                 편도 지연을 d_up, d_dn 이라 하면 추정 오차는 정확히 (d_up − d_dn) / 2 입니다.
                 두 값은 음수가 될 수 없고 합이 RTT 이므로, 차이는 RTT 를 넘지 못합니다.

                 따라서 |오차| ≤ RTT / 2 입니다.

                 경로가 대칭이면 오차가 0 이고, 최악의 경우에도 RTT 의 절반을 넘지 않습니다.
                 그래서 최소 RTT 가 4 ms 미만이면 오차 2 ms 미만이 보장됩니다.

                 왕복을 40회 하고 그중 RTT 가 가장 작은 샘플만 쓰는 이유도 같습니다.
                 네트워크 큐잉은 지연을 늘리기만 하므로, RTT 가 최소인 샘플이
                 가장 덜 오염되었고 오차 상한도 가장 작습니다.
                 """)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }
}
