import SwiftUI

struct DiagnosticsView: View {
    @State private var cams: [DeviceProbe.CameraInfo] = []
    @State private var clock: DeviceProbe.ClockFacts?
    @State private var sys: DeviceProbe.SystemFacts?
    @State private var loading = true
    @State private var shareURL: URL?
    @State private var showShare = false
    @State private var expanded: Set<String> = []

    var body: some View {
        ScrollView {
            if loading {
                ProgressView("카메라 포맷 읽는 중...")
                    .padding(40)
            } else {
                VStack(alignment: .leading, spacing: 14) {
                    clockCard
                    verdictCard
                    systemCard
                    ForEach(cams) { cam in cameraCard(cam) }
                    actions
                }
                .padding(16)
            }
        }
        .background(Color(red: 0.063, green: 0.078, blue: 0.094))
        .navigationTitle("기기 진단")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .sheet(isPresented: $showShare) {
            if let u = shareURL { ShareSheet(items: [u]) }
        }
    }

    private func load() async {
        // 카메라 포맷 열거는 수백 개가 될 수 있어 백그라운드에서 합니다.
        let c = await Task.detached(priority: .userInitiated) {
            (DeviceProbe.cameras(), DeviceProbe.clockFacts(), DeviceProbe.systemFacts())
        }.value
        cams = c.0
        clock = c.1
        sys = c.2
        loading = false
        AppLog.shared.i("Diag",
            "진단 완료: 카메라 \(c.0.count)대, 시계차이 \(c.1.deltaNs)ns, "
            + "같은도메인=\(c.1.sameDomain)")
    }

    // MARK: - ★★ 시계 도메인 (가장 중요)

    private var clockCard: some View {
        Group {
            if let c = clock {
                let ok = c.sameDomain
                VStack(alignment: .leading, spacing: 8) {
                    Text("★ 시계 도메인 검증")
                        .font(.headline)
                        .foregroundStyle(ok ? Color.green : Color.red)
                    Text(ok ? "같은 도메인 — 변환 불필요" : "다른 도메인 — 설계 재검토 필요")
                        .font(.title2.bold())
                        .foregroundStyle(ok ? Color.green : Color.red)

                    Text("""
                         이게 프로젝트 전체의 전제입니다. 우리가 시각 동기에 쓰는 시계와 \
                         카메라가 프레임에 도장 찍는 시계가 같아야, 프레임 타임스탬프를 \
                         그대로 공통 시간축으로 옮길 수 있습니다.
                         """)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.vertical, 4)

                    KV("CLOCK_UPTIME_RAW", "\(c.uptimeRawNs) ns")
                    KV("CMClock HostTime", "\(c.hostClockNs) ns")
                    KV("차이", String(format: "%d ns  (%.6f ms)",
                                      c.deltaNs, Double(c.deltaNs) / Double(NS.perMilli)))
                    KV("누적 절전시간", String(format: "%.3f 초",
                                          Double(c.sleptNs) / Double(NS.perSecond)))
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background((ok ? Color.green : Color.red).opacity(0.12),
                            in: RoundedRectangle(cornerRadius: 12))
            }
        }
    }

    // MARK: - 종합 판정

    private var verdictCard: some View {
        let target = cams.first { $0.deviceTypeRaw.contains("WideAngle") && $0.position == "후면" }
            ?? cams.first { $0.position == "후면" }
            ?? cams.first
        let checks = target?.checks ?? []
        let fails = checks.filter { $0.status == .fail }.count
        let warns = checks.filter { $0.status == .warn }.count

        let label: String
        let color: Color
        let msg: String
        if target == nil {
            label = "판정 불가"; color = .gray
            msg = "카메라를 하나도 못 찾았습니다. 시뮬레이터면 정상입니다."
        } else if fails > 0 {
            label = "제약 있음"; color = .red
            msg = "\(fails)개 항목이 '불가'입니다. 아래에서 확인하고 리포트를 보내주세요."
        } else if warns > 0 {
            label = "사용 가능 (주의 \(warns))"; color = .yellow
            msg = "치명적 문제는 없습니다. '주의' 항목은 촬영 조건이나 실측으로 보완합니다."
        } else {
            label = "양호"; color = .green
            msg = "주 후면 카메라가 모든 항목을 통과했습니다."
        }

        return VStack(alignment: .leading, spacing: 6) {
            Text("종합 판정").font(.caption).foregroundStyle(.secondary)
            Text(label).font(.title.bold()).foregroundStyle(color)
            Text(msg).font(.footnote)
            if let t = target {
                Text("기준 카메라: \(t.name) · 감지된 카메라 \(cams.count)대")
                    .font(.caption2).monospaced().foregroundStyle(.secondary)
                    .padding(.top, 4)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(color.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))
    }

    private var systemCard: some View {
        Group {
            if let s = sys {
                Card(title: "기기 상태") {
                    KV("모델", "\(BuildInfo.deviceFriendlyName) (\(BuildInfo.deviceModelIdentifier))")
                    KV("iOS", BuildInfo.osVersion)
                    KV("온도", s.thermalState)
                    KV("배터리", "\(Int(s.batteryLevel * 100))% (\(s.batteryState))")
                    KV("저전력모드", s.lowPowerMode ? "켜짐 ★ 끄세요" : "꺼짐")
                    KV("여유공간", "\(s.freeDiskBytes / 1_000_000_000) GB")
                    KV("카메라권한", s.cameraAuthorized)
                }
            }
        }
    }

    // MARK: - 카메라

    private func cameraCard(_ cam: DeviceProbe.CameraInfo) -> some View {
        Card(title: "\(cam.name) · \(cam.position)") {
            ForEach(cam.checks) { ch in
                HStack(alignment: .top, spacing: 10) {
                    Text(ch.status.rawValue)
                        .font(.caption.bold())
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(color(ch.status).opacity(0.18),
                                    in: RoundedRectangle(cornerRadius: 6))
                        .foregroundStyle(color(ch.status))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(ch.name).font(.subheadline.weight(.semibold))
                        Text(ch.detail).font(.caption).foregroundStyle(.secondary)
                    }
                }
                .padding(.vertical, 3)
            }

            Divider().padding(.vertical, 4)

            KV("60fps+ 포맷", "\(cam.formats.filter { $0.supports60 }.count)개")
            KV("1080p60", "\(cam.best1080p60.count)개")
            KV("4K60", "\(cam.best4K60.count)개")
            KV("전체 포맷", "\(cam.formats.count)개")
            KV("최대 프레임", String(format: "%.0f fps", cam.maxFrameRateAny))

            Button {
                if expanded.contains(cam.id) { expanded.remove(cam.id) }
                else { expanded.insert(cam.id) }
            } label: {
                Text(expanded.contains(cam.id) ? "60fps 포맷 접기" : "60fps 포맷 펼치기")
                    .font(.caption)
            }
            .padding(.top, 4)

            if expanded.contains(cam.id) {
                let fast = cam.formats.filter { $0.supports60 }
                    .sorted { ($0.width * $0.height) > ($1.width * $1.height) }
                ForEach(Array(fast.enumerated()), id: \.offset) { _, f in
                    Text("\(f.label)  \(Int(f.maxFrameRate))fps  "
                         + "노출 1/\(f.minExposureNs > 0 ? Int(Double(NS.perSecond) / Double(f.minExposureNs)) : 0)초~  "
                         + "ISO \(Int(f.minISO))-\(Int(f.maxISO))  "
                         + String(format: "FOV %.0f도", f.fovDegrees))
                        .font(.caption2).monospaced()
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func color(_ s: DeviceProbe.Status) -> Color {
        switch s {
        case .pass: return .green
        case .warn: return .yellow
        case .fail: return .red
        case .unknown: return .gray
        }
    }

    private var actions: some View {
        VStack(spacing: 10) {
            Button {
                if let u = LogExporter.writeToTemp() {
                    shareURL = u
                    showShare = true
                }
            } label: {
                Text("이 리포트 공유")
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .background(Color.green.opacity(0.85), in: RoundedRectangle(cornerRadius: 10))
                    .foregroundStyle(.black)
            }
            Button {
                _ = LogExporter.copyToClipboard()
            } label: {
                Text("클립보드 복사")
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 10)
                    .background(Color.white.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
            }
            Button {
                loading = true
                Task { await load() }
            } label: {
                Text("다시 측정")
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 10)
                    .overlay(RoundedRectangle(cornerRadius: 10)
                        .stroke(Color.white.opacity(0.25)))
            }
        }
        .padding(.top, 6)
    }
}
