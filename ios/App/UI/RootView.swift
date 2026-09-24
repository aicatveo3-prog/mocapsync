import SwiftUI

struct RootView: View {
    @StateObject private var log = AppLog.shared
    @State private var shareURL: URL?
    @State private var showShare = false
    @State private var toast: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    buildCard
                    firstStepCard
                    roleCard
                    recordCard
                    logCard
                    footer
                }
                .padding(16)
            }
            .background(Color(red: 0.063, green: 0.078, blue: 0.094))
            .navigationTitle("MocapSync")
            .navigationBarTitleDisplayMode(.inline)
        }
        .sheet(isPresented: $showShare) {
            if let u = shareURL { ShareSheet(items: [u]) }
        }
        .overlay(alignment: .bottom) {
            if let t = toast {
                Text(t)
                    .font(.footnote)
                    .padding(12)
                    .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
                    .padding(.bottom, 24)
                    .transition(.opacity)
                    .task {
                        try? await Task.sleep(nanoseconds: 2_500_000_000)
                        toast = nil
                    }
            }
        }
    }

    // MARK: - 카드

    private var buildCard: some View {
        Card(title: "이 빌드") {
            KV("버전", "\(BuildInfo.versionName) (build \(BuildInfo.buildNumber))")
            KV("커밋", BuildInfo.gitCommit)
            KV("bundleId", BuildInfo.bundleId)
            KV("기기", "\(BuildInfo.deviceFriendlyName)  (\(BuildInfo.deviceModelIdentifier))")
            KV("iOS", BuildInfo.osVersion)
            Text("""
                 이 단계의 목적은 CI 가 만든 앱이 실제로 실행되는지, \
                 그리고 이 아이폰이 60fps · 초점고정 · 시계 조건을 만족하는지 \
                 확인하는 것입니다. 녹화와 동기 기능은 아직 없습니다.
                 """)
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.top, 6)
        }
    }

    private var firstStepCard: some View {
        Card(title: "먼저 여기부터") {
            Text("""
                 아래 '기기 진단'을 열고, 그 화면에서 '리포트 공유'로 결과를 \
                 개발자에게 보내주세요. 이 리포트가 다음 단계 설계를 결정합니다.
                 """)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            NavigationLink {
                DiagnosticsView()
            } label: {
                Text("기기 진단 열기")
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .background(Color.green.opacity(0.85), in: RoundedRectangle(cornerRadius: 10))
                    .foregroundStyle(.black)
            }
            .padding(.top, 10)
        }
    }

    private var roleCard: some View {
        Card(title: "클럭 동기") {
            Text("""
                 PC 마스터(`python server/master.py`)에 붙어서 시각 차이를 측정합니다. \
                 폰 1대만으로 측정 가능합니다.
                 """)
                .font(.subheadline)
                .foregroundStyle(.secondary)

            NavigationLink {
                SyncView()
            } label: {
                Text("동기 측정 열기")
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .background(Color.blue.opacity(0.85), in: RoundedRectangle(cornerRadius: 10))
                    .foregroundStyle(.white)
            }
            .padding(.top, 10)

            Text("""
                 실측 결과: 이 경로(폰·PC 모두 공유기 WiFi)의 바닥은 최소 RTT \
                 4.076 ms = 오차 상한 2.038 ms 입니다. 설정을 바꿔도 4 ms 아래로 \
                 내려가지 않았습니다. 목표 2 ms 는 측정 정밀도 안에서 달성된 것으로 \
                 보고 이 단계를 닫았습니다. (자세한 기록은 DESIGN.md §3.12)
                 """)
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.top, 6)
        }
    }

    private var recordCard: some View {
        Card(title: "녹화 (3단계)") {
            Text("""
                 1080p 60fps · 노출/초점/화이트밸런스 고정 · 안정화 끄기로 촬영하고, \
                 프레임마다 시각을 사이드카 JSON 에 기록합니다. \
                 폰 1대로도 시험 촬영과 예약 시작 자체시험이 가능합니다.
                 """)
                .font(.subheadline)
                .foregroundStyle(.secondary)

            NavigationLink {
                RecordView()
            } label: {
                Text("녹화 화면 열기")
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .background(Color.red.opacity(0.8), in: RoundedRectangle(cornerRadius: 10))
                    .foregroundStyle(.white)
            }
            .padding(.top, 10)

            Text("""
                 ★ 촬영 전에 '클럭 동기'를 먼저 하세요. 폰이 한 번 자면 이전 \
                 오프셋이 무효가 됩니다 (CLOCK_UPTIME_RAW 가 절전 중 멈춥니다 — 실측 확인). \
                 사이드카가 그걸 탐지해서 '치명'으로 표시합니다.
                 """)
                .font(.caption2)
                .foregroundStyle(.orange)
                .padding(.top, 6)
        }
    }

    private var logCard: some View {
        Card(title: "로그") {
            KV("버퍼", "\(log.count) 줄  (revision \(log.revision))")
            NavigationLink {
                LogView()
            } label: {
                Text("로그 보기")
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 10)
                    .background(Color.white.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
            }
            .padding(.top, 8)

            HStack(spacing: 10) {
                Button {
                    if let u = LogExporter.writeToTemp() {
                        shareURL = u
                        showShare = true
                    } else {
                        toast = "로그 파일 생성 실패"
                    }
                } label: {
                    Text("로그 공유")
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                        .background(Color.green.opacity(0.8), in: RoundedRectangle(cornerRadius: 10))
                        .foregroundStyle(.black)
                }
                Button {
                    toast = LogExporter.copyToClipboard()
                } label: {
                    Text("복사")
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                        .background(Color.white.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
                }
            }
            .padding(.top, 8)

            Text("공유·복사에는 기기 진단 리포트가 항상 머리말로 붙습니다. 이것만 보내주시면 됩니다.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.top, 6)
        }
    }

    private var footer: some View {
        Text("MocapSync · Pose2Sim 기반 마커리스 3D 모션캡쳐")
            .font(.caption2)
            .monospaced()
            .foregroundStyle(.tertiary)
            .frame(maxWidth: .infinity)
            .padding(.top, 8)
    }
}

// MARK: - 공통 컴포넌트

struct Card<Content: View>: View {
    let title: String
    @ViewBuilder var content: Content

    init(title: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.headline)
                .foregroundStyle(Color.green)
            content
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 12))
    }
}

struct KV: View {
    let k: String
    let v: String
    init(_ k: String, _ v: String) { self.k = k; self.v = v }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Text(k)
                .font(.caption)
                .monospaced()
                .foregroundStyle(.secondary)
                .frame(width: 120, alignment: .leading)
            Text(v)
                .font(.caption)
                .monospaced()
                .frame(maxWidth: .infinity, alignment: .leading)
                .textSelection(.enabled)
        }
    }
}

struct RoleButton: View {
    let title: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
                .overlay(RoundedRectangle(cornerRadius: 10)
                    .stroke(Color.white.opacity(0.25)))
        }
    }
}
