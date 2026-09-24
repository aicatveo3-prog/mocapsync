import SwiftUI

struct LogView: View {
    @StateObject private var log = AppLog.shared
    @State private var shareURL: URL?
    @State private var showShare = false
    @State private var toast: String?

    var body: some View {
        let lines = log.snapshot()

        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Button("공유") {
                    if let u = LogExporter.writeToTemp() {
                        shareURL = u
                        showShare = true
                    }
                }
                .buttonStyle(.borderedProminent)

                Button("복사") { toast = LogExporter.copyToClipboard() }
                    .buttonStyle(.bordered)

                Button("지우기") {
                    log.clear()
                    log.i("Log", "로그 버퍼를 비웠습니다")
                    toast = "로그를 비웠습니다"
                }
                .buttonStyle(.bordered)
            }
            .font(.footnote)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)

            Text("\(lines.count) 줄 · 최대 4000줄 보관 (오래된 줄부터 버립니다)")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .padding(.bottom, 4)

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 1) {
                        ForEach(Array(lines.enumerated()), id: \.offset) { idx, line in
                            Text(line)
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(tint(line))
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .id(idx)
                        }
                    }
                    .padding(.horizontal, 10)
                }
                .onAppear {
                    if !lines.isEmpty { proxy.scrollTo(lines.count - 1, anchor: .bottom) }
                }
                .onChange(of: log.revision) { _, _ in
                    let n = log.count
                    if n > 0 { proxy.scrollTo(n - 1, anchor: .bottom) }
                }
            }
        }
        .background(Color(red: 0.09, green: 0.11, blue: 0.14))
        .navigationTitle("로그")
        .navigationBarTitleDisplayMode(.inline)
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
                    .task {
                        try? await Task.sleep(nanoseconds: 2_500_000_000)
                        toast = nil
                    }
            }
        }
    }

    private func tint(_ line: String) -> Color {
        if line.contains(" E/") { return .red }
        if line.contains(" W/") { return .yellow }
        if line.contains(" M/") { return .green }
        return .primary
    }
}
