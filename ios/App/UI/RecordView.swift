import AVFoundation
import SwiftUI
import UIKit

/// 녹화 화면.
///
/// ★ 화면 설계 원칙: **잠긴 설정을 숫자로 보여줍니다.**
///
/// "잠갔다"는 말만 띄우면 실제로 잠겼는지 알 수 없습니다. 셔터 µs, ISO,
/// 렌즈 위치, 안정화 상태를 실제 적용값으로 보여줘야, 촬영 전에 사람이
/// 이상한 값을 발견할 수 있습니다. 촬영 후에 알면 다시 찍어야 합니다.
struct RecordView: View {
    @StateObject private var cap = CaptureCoordinator()
    @ObservedObject private var log = AppLog.shared
    @State private var shareItems: [URL] = []
    @State private var showShare = false
    @State private var selectedSession: String?
    @State private var copied = false
    @StateObject private var uploader = Uploader(deviceId: CaptureCoordinator.stableDeviceId())
    /// 마지막으로 쓴 주소를 기억합니다. 매번 입력하게 하면 루프가 느려집니다.
    @AppStorage("mocapsync.uploadHost") private var uploadHost = ""
    @AppStorage("mocapsync.uploadPort") private var uploadPort = "9001"

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                previewCard
                syncCard
                statusCard
                controlCard
                if !cap.recorder.lastValidation.isEmpty { validationCard }
                settingsCard
                if !cap.applied.warnings.isEmpty { warningCard }
                diagnosticCard
                sessionsCard
                explainCard
            }
            .padding(16)
        }
        .background(Color(red: 0.063, green: 0.078, blue: 0.094))
        .navigationTitle("녹화")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            if CameraController.permissionStatus() == .authorized {
                await cap.start()
            }
        }
        .onDisappear { cap.stopSession() }
        .sheet(isPresented: $showShare) {
            if !shareItems.isEmpty { ShareSheet(items: shareItems) }
        }
    }

    // MARK: - 미리보기

    private var previewCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            CameraPreview(session: cap.camera.session)
                .aspectRatio(16.0 / 9.0, contentMode: .fit)
                .frame(maxWidth: .infinity)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(alignment: .topLeading) { recIndicator.padding(10) }
                .overlay(alignment: .bottomTrailing) {
                    Text(String(format: "%dx%d @%dfps",
                                cap.applied.width, cap.applied.height, cap.applied.fps))
                        .font(.caption2.monospacedDigit())
                        .padding(6)
                        .background(.black.opacity(0.55), in: RoundedRectangle(cornerRadius: 6))
                        .padding(8)
                }

            Text("삼각대에 고정하세요. 미리보기가 흔들리면 OIS 가 렌즈를 움직여 "
                 + "캘리브레이션이 틀어집니다.")
                .font(.caption2).foregroundStyle(.secondary)
        }
    }

    private var recIndicator: some View {
        Group {
            switch cap.phase {
            case .recording:
                HStack(spacing: 6) {
                    Circle().fill(.red).frame(width: 10, height: 10)
                    Text("REC \(cap.recorder.displayFrameCount)")
                        .font(.caption.bold().monospacedDigit())
                }
                .padding(.horizontal, 8).padding(.vertical, 5)
                .background(.black.opacity(0.6), in: Capsule())
            case .armed(let lead):
                HStack(spacing: 6) {
                    Circle().fill(.yellow).frame(width: 10, height: 10)
                    Text(String(format: "예약 %.0fms", lead))
                        .font(.caption.bold().monospacedDigit())
                }
                .padding(.horizontal, 8).padding(.vertical, 5)
                .background(.black.opacity(0.6), in: Capsule())
            default:
                EmptyView()
            }
        }
    }

    // MARK: - 상태

    private var statusCard: some View {
        Card(title: "상태") {
            switch cap.phase {
            case .needsPermission:
                Text("카메라 권한이 필요합니다").font(.subheadline)
                Button("권한 요청하고 시작") {
                    Task { await cap.requestPermissionAndStart() }
                }
                .padding(.top, 6)
            case .permissionDenied:
                Text("카메라 권한이 거부됐습니다").font(.subheadline).foregroundStyle(.red)
                Text("설정 → MocapSync → 카메라를 켜 주세요.")
                    .font(.caption).foregroundStyle(.secondary)
            case .configuring:
                HStack { ProgressView(); Text("카메라 구성 중...").font(.subheadline) }
            case .converging(let left):
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        ProgressView()
                        Text(String(format: "자동노출 수렴 대기 %.1f초", left))
                            .font(.subheadline).monospacedDigit()
                    }
                    Text("★ 이 시간이 필요한 이유: 세션을 켠 직후 바로 잠그면 "
                         + "초기값(대개 엉뚱한 값)으로 굳어버립니다. 자동이 한 번 "
                         + "맞춘 뒤에 그 값을 잠가야 합니다.")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            case .ready:
                Text("준비 완료 — 설정이 잠겼습니다")
                    .font(.subheadline).foregroundStyle(.green)
            case .armed(let lead):
                Text(String(format: "예약 대기 중 (%.0f ms 뒤 시작)", lead))
                    .font(.subheadline).foregroundStyle(.yellow)
            case .recording:
                VStack(alignment: .leading, spacing: 4) {
                    Text("녹화 중").font(.subheadline).foregroundStyle(.red)
                    KV("프레임", "\(cap.recorder.displayFrameCount)")
                    KV("실측 fps", String(format: "%.2f", cap.recorder.displayFps))
                    KV("버린 프레임", "\(cap.recorder.displayDroppedCount)")
                }
            case .finishing:
                HStack { ProgressView(); Text("파일 마무리 중...").font(.subheadline) }
            case .done:
                Text("저장 완료").font(.subheadline).foregroundStyle(.green)
            case .failed(let m):
                Text("실패: \(m)").font(.subheadline).foregroundStyle(.red)
            }

            Divider().padding(.vertical, 4)
            KV("남은 녹화 시간", String(format: "약 %.0f분",
                                   CaptureCoordinator.estimatedMinutesLeft()))
        }
    }

    // MARK: - 클럭 동기 상태 ★ 가장 먼저 보여야 하는 카드

    /// 3단계 첫 시험에서 사이드카가 "사용 불가"로 나온 원인이 여기였습니다.
    /// 동기 화면과 녹화 화면이 서로를 몰라서 오프셋이 0 이었습니다.
    /// 이제 연결됐고, 상태를 **촬영 전에** 크게 보여줍니다.
    private var syncCard: some View {
        let fresh = cap.syncFreshness
        let ok = fresh.canRecord
        let color: Color = ok ? .green : .orange

        return Card(title: "클럭 동기 상태") {
            HStack(spacing: 8) {
                Image(systemName: ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                    .foregroundStyle(color)
                Text(ok ? "촬영 가능" : "촬영 전에 동기가 필요합니다")
                    .font(.headline).foregroundStyle(color)
            }

            Text(fresh.summary)
                .font(.caption).foregroundStyle(.secondary)

            if let r = SyncStore.shared.latest {
                Divider().padding(.vertical, 4)
                KV("오프셋", String(format: "%+.3f ms", r.offsetNs.ms))
                KV("측정 당시 상한", String(format: "%.3f ms", r.uncertaintyNs.ms))
                KV("드리프트 포함 상한",
                   String(format: "%.3f ms",
                          SyncStore.shared.displayEffectiveUncertaintyNs.ms))
                if let ppm = SyncStore.shared.measuredDriftPpm,
                   let unc = SyncStore.shared.driftUncertaintyPpm {
                    KV("드리프트 실측",
                       abs(ppm) > unc
                       ? String(format: "%+.2f ppm (±%.2f)", ppm, unc)
                       : String(format: "측정 불가 (±%.2f ppm 잡음 안)", unc))
                } else {
                    KV("드리프트 실측", "두 번 측정하면 나옵니다")
                }
            }

            if !ok {
                NavigationLink {
                    SyncView()
                } label: {
                    Text("클럭 동기 하러 가기")
                        .font(.headline)
                        .frame(maxWidth: .infinity).padding(.vertical, 12)
                        .background(Color.blue.opacity(0.85),
                                    in: RoundedRectangle(cornerRadius: 10))
                        .foregroundStyle(.white)
                }
                .padding(.top, 8)

                Text("""
                     동기 없이도 녹화는 됩니다. 카메라 설정을 시험하는 데는 쓸 수 있습니다.
                     다만 사이드카 검증이 '사용 불가'로 나옵니다 — 여러 대의 영상을
                     합칠 수 없기 때문입니다. 3D 복원에 쓸 영상은 반드시 동기 후에 찍으세요.
                     """)
                    .font(.caption2).foregroundStyle(.secondary).padding(.top, 6)
            }
        }
        .padding(.vertical, 2)
        .background(color.opacity(0.08), in: RoundedRectangle(cornerRadius: 14))
    }

    // MARK: - 조작

    private var controlCard: some View {
        Card(title: "촬영") {
            let canRecord = cap.phase == .ready || cap.phase == .done

            Button {
                cap.recordNow()
            } label: {
                Text("바로 녹화 시작")
                    .frame(maxWidth: .infinity).padding(.vertical, 12)
                    .background(canRecord ? Color.red.opacity(0.85) : Color.gray.opacity(0.3),
                                in: RoundedRectangle(cornerRadius: 10))
                    .foregroundStyle(.white)
            }
            .disabled(!canRecord)

            Button {
                cap.recordScheduledSelfTest(leadMs: 800)
            } label: {
                Text("예약 시작 자체시험 (0.8초 뒤)")
                    .frame(maxWidth: .infinity).padding(.vertical, 12)
                    .background(canRecord ? Color.yellow.opacity(0.75) : Color.gray.opacity(0.3),
                                in: RoundedRectangle(cornerRadius: 10))
                    .foregroundStyle(.black)
            }
            .disabled(!canRecord)
            .padding(.top, 6)

            Text("★ 자체시험은 폰 1대로 예약 시작 경로를 확인합니다. "
                 + "마스터 없이 '0.8초 뒤'를 예약 시각으로 잡고, 프레임 시각을 비교해 "
                 + "그 순간의 첫 프레임부터 기록합니다. 아래 검증에서 "
                 + "started_early / started_late 가 안 나오면 성공입니다.")
                .font(.caption2).foregroundStyle(.secondary)

            Button {
                cap.stopRecording()
            } label: {
                Text("녹화 정지")
                    .frame(maxWidth: .infinity).padding(.vertical, 12)
                    .background(isBusy ? Color.white.opacity(0.18) : Color.gray.opacity(0.2),
                                in: RoundedRectangle(cornerRadius: 10))
            }
            .disabled(!isBusy)
            .padding(.top, 10)

            Button("설정 다시 잠그기") { cap.relock() }
                .font(.caption)
                .padding(.top, 8)
            Text("조명이 바뀌었으면 다시 잠그세요. 노출을 현재 밝기에 맞춰 재계산합니다.")
                .font(.caption2).foregroundStyle(.secondary)
        }
    }

    private var isBusy: Bool {
        if case .recording = cap.phase { return true }
        if case .armed = cap.phase { return true }
        return false
    }

    // MARK: - 자체검증

    private var validationCard: some View {
        let lines = cap.recorder.lastValidation
        let fatalLines = lines.filter { $0.hasPrefix("[치명]") }
        let usable = cap.recorder.lastUsable

        return Card(title: usable
             ? "사이드카 자체검증 — 사용 가능 ✔"
             : "사이드카 자체검증 — ★ 사용 불가") {

            // ★ 치명 사유를 가장 크게, 가장 먼저.
            //   이전 버전은 모든 줄을 같은 크기로 늘어놓아서 사용자가
            //   "사용 불가"만 보고 이유를 지나쳤습니다. 그러면 개발자는
            //   추측할 수밖에 없고 수정이 한 번에 안 끝납니다.
            if !usable {
                VStack(alignment: .leading, spacing: 6) {
                    Text("이 촬영은 3D 복원에 쓸 수 없습니다")
                        .font(.headline).foregroundStyle(.red)
                    ForEach(Array(fatalLines.enumerated()), id: \.offset) { _, l in
                        Text(l.replacingOccurrences(of: "[치명] ", with: ""))
                            .font(.footnote)
                            .foregroundStyle(.red)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.red.opacity(0.14), in: RoundedRectangle(cornerRadius: 10))

            }

            Divider().padding(.vertical, 6)

            ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                Text(line)
                    .font(.caption)
                    .foregroundStyle(line.hasPrefix("[치명]") ? .red
                                     : (line.hasPrefix("[경고]") ? .orange : .secondary))
                    .fixedSize(horizontal: false, vertical: true)
            }

            Text("★ 폰에서 바로 검증하는 이유: PC 까지 올리고 나서 문제를 발견하면 "
                 + "다시 찍을 기회를 놓칩니다. 여기서 '치명'이 나오면 그 촬영은 "
                 + "3D 복원에 쓸 수 없습니다.")
                .font(.caption2).foregroundStyle(.secondary).padding(.top, 6)
        }
    }

    // MARK: - 적용된 설정

    private var settingsCard: some View {
        Card(title: "잠긴 설정 (실제 적용값)") {
            KV("카메라", shortDeviceType(cap.applied.deviceType))
            KV("해상도", "\(cap.applied.width) x \(cap.applied.height)")
            KV("프레임레이트", "\(cap.applied.fps) fps (min=max 고정)")
            KV("화각", String(format: "%.1f도", cap.applied.fieldOfViewDeg))
            KV("binned", cap.applied.isBinned ? "예 (해상감 저하)" : "아니오")
            KV("셔터", cap.applied.exposureDurationNs > 0
                ? String(format: "%.0f µs = 1/%.0f초",
                         Double(cap.applied.exposureDurationNs) / 1000,
                         1e9 / Double(cap.applied.exposureDurationNs))
                : "—")
            KV("ISO", String(format: "%.0f", cap.applied.iso))
            KV("렌즈 위치", String(format: "%.3f", cap.applied.lensPosition))
            KV("노출 고정", cap.applied.exposureLocked ? "예" : "★ 아니오")
            KV("초점", cap.applied.isFixedFocusLens
                ? "고정초점 렌즈 (잠글 기구 없음 — 정상)"
                : (cap.applied.focusLocked ? "잠김" : "★ 안 잠김"))
            KV("화이트밸런스", cap.applied.whiteBalanceLocked ? "잠김" : "★ 안 잠김")
            KV("안정화(EIS)", cap.applied.stabilization)
        }
    }

    private func shortDeviceType(_ s: String) -> String {
        s.replacingOccurrences(of: "AVCaptureDeviceType", with: "")
    }

    private var warningCard: some View {
        Card(title: "잠그지 못한 것") {
            ForEach(Array(cap.applied.warnings.enumerated()), id: \.offset) { _, w in
                Text("· " + w).font(.caption).foregroundStyle(.orange)
            }
        }
    }

    // MARK: - 진단 복사 ★ 개발 루프용

    /// ★ 한 번 눌러 필요한 정보 전부를 클립보드에 담습니다.
    ///
    /// 개발자가 실기기를 만질 수 없으므로, 화면에서 값을 하나하나 찾아 옮겨 적게
    /// 하면 루프가 느려지고 빠뜨리기도 쉽습니다. 실제로 3단계 시험에서
    /// "사용 불가"라는 결과만 전달되고 사유가 빠져서 두 번 추측해야 했습니다.
    ///
    /// **성공했을 때도 필요합니다.** 검증을 통과했어도 fps 가 30 으로 잡혔거나
    /// 셔터가 느리면 경고만 뜨고 지나갑니다. 숫자를 봐야 알 수 있습니다.
    private var diagnosticCard: some View {
        Card(title: "진단 정보 보내기") {
            Button {
                UIPasteboard.general.string = cap.diagnosticText()
                copied = true
            } label: {
                HStack {
                    Image(systemName: copied ? "checkmark" : "doc.on.doc")
                    Text(copied ? "복사됐습니다 — 채팅에 붙여주세요" : "전체 진단 복사")
                }
                .font(.headline)
                .frame(maxWidth: .infinity).padding(.vertical, 12)
                .background(copied ? Color.green.opacity(0.8) : Color.blue.opacity(0.85),
                            in: RoundedRectangle(cornerRadius: 10))
                .foregroundStyle(.white)
            }

            Text("""
                 빌드·클럭동기·카메라 실제 적용값·프레임 통계·검증 결과가 한 번에 \
                 복사됩니다. 개발자가 실기기를 볼 수 없으므로 이 정보가 유일한 눈입니다.
                 """)
                .font(.caption2).foregroundStyle(.secondary).padding(.top, 6)

            ScrollView(.horizontal, showsIndicators: true) {
                Text(cap.diagnosticText())
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            .frame(maxHeight: 220)
            .padding(.top, 8)
        }
    }

    // MARK: - 저장된 세션

    private var sessionsCard: some View {
        Card(title: "저장된 촬영") {
            if cap.sessions.isEmpty {
                Text("없음").font(.subheadline).foregroundStyle(.secondary)
            } else {
                ForEach(cap.sessions, id: \.self) { sid in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(sid).font(.caption.bold())
                            Spacer()
                            Button("PC로 전송") {
                                uploader.upload(sessionId: sid,
                                                files: cap.files(in: sid),
                                                host: uploadHost,
                                                port: UInt16(uploadPort) ?? Wire.defaultPort)
                            }
                            .font(.caption.bold())
                            .disabled(uploader.phase.isBusy || uploadHost.isEmpty)
                            Button("공유") {
                                shareItems = cap.files(in: sid)
                                showShare = !shareItems.isEmpty
                            }
                            .font(.caption)
                            Button("삭제") { cap.deleteSession(sid) }
                                .font(.caption).foregroundStyle(.red)
                        }
                        ForEach(cap.files(in: sid), id: \.self) { f in
                            Text("  " + f.lastPathComponent + "  " + fileSize(f))
                                .font(.caption2.monospaced())
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(.vertical, 6)
                    Divider()
                }
            }
            Button("목록 새로고침") { cap.refreshSessions() }
                .font(.caption).padding(.top, 6)

            Divider().padding(.vertical, 6)

            // ── PC 전송 ─────────────────────────────────────────────────────
            Text("PC 마스터 주소").font(.caption).foregroundStyle(.secondary)
            HStack(spacing: 8) {
                TextField("10.89.215.89", text: $uploadHost)
                    .textFieldStyle(.roundedBorder)
                    .keyboardType(.numbersAndPunctuation)
                    .autocorrectionDisabled()
                    .textInputAutocapitalization(.never)
                TextField("9001", text: $uploadPort)
                    .textFieldStyle(.roundedBorder)
                    .keyboardType(.numberPad)
                    .frame(width: 80)
            }

            switch uploader.phase {
            case .sending(let name, let sent, let total):
                VStack(alignment: .leading, spacing: 4) {
                    Text(name).font(.caption2.monospaced())
                    ProgressView(value: Double(sent), total: Double(max(total, 1)))
                    Text(String(format: "%.2f / %.2f MB",
                                Double(sent) / 1_048_576, Double(total) / 1_048_576))
                        .font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                }
                .padding(.top, 6)
            case .connecting:
                HStack { ProgressView(); Text("연결 중...").font(.caption) }.padding(.top, 6)
            case .finished(let f, let b):
                Text(String(format: "전송 완료: %d개, %.2f MB", f, Double(b) / 1_048_576))
                    .font(.caption).foregroundStyle(.green).padding(.top, 6)
            case .failed(let m):
                Text("전송 실패: \(m)")
                    .font(.caption).foregroundStyle(.red).padding(.top, 6)
            default:
                EmptyView()
            }

            if !uploader.log.isEmpty {
                Text(uploader.log.suffix(6).joined(separator: "\n"))
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary).padding(.top, 4)
            }

            Text("""
                 ★ 사이드카(.json)를 영상보다 먼저 보냅니다. 영상은 수십 MB 라 \
                 느린 링크에서 오래 걸리는데, 전송이 중간에 끊겨도 사이드카가 \
                 도착해 있으면 PC 가 검증 결과를 보여줄 수 있습니다.
                 PC 마스터가 받은 사이드카를 자체 검증기로 다시 판정하므로, \
                 폰과 PC 의 판정이 같은지도 자동으로 대조됩니다.
                 """)
                .font(.caption2).foregroundStyle(.secondary).padding(.top, 6)
        }
    }

    private func fileSize(_ u: URL) -> String {
        let attrs = try? FileManager.default.attributesOfItem(atPath: u.path)
        let b = (attrs?[.size] as? NSNumber)?.int64Value ?? 0
        if b > 1_048_576 { return String(format: "%.1f MB", Double(b) / 1_048_576) }
        return String(format: "%.0f KB", Double(b) / 1024)
    }

    // MARK: - 설명

    private var explainCard: some View {
        Card(title: "예약 시작이 정밀 대기를 쓰지 않는 이유") {
            Text("""
                 카메라는 이미 60fps 로 돌고 있습니다. 우리가 정할 것은
                 "언제 세션을 켜느냐"가 아니라 "어느 프레임부터 파일에 쓰느냐"입니다.

                 그래서 각 프레임의 시각(PTS)을 보고, 예약 시각 이후의 첫 프레임부터
                 기록합니다. 이렇게 하면

                 · 정밀 대기가 필요 없습니다 (CPU 를 태울 이유도 없음)
                 · 명령이 예약 시각보다 먼저만 도착하면 지연이 전혀 무해합니다
                 · 시작 지점이 프레임 경계에 정확히 맞습니다

                 남는 오차는 프레임 양자화(16.67 ms)뿐이고, 그건 프레임 타임스탬프가
                 이미 정확히 알려주므로 PC 의 리샘플러가 처리합니다.
                 """)
                .font(.caption).foregroundStyle(.secondary)
        }
    }
}

// MARK: - 미리보기 레이어

/// `AVCaptureVideoPreviewLayer` 를 SwiftUI 에 얹습니다.
///
/// ★ 미리보기는 `AVCaptureVideoDataOutput` 과 **별개 경로**입니다.
///   미리보기를 켜도 우리가 기록하는 프레임에는 영향이 없습니다.
///   (다만 GPU 를 조금 쓰므로 발열이 걱정되면 화면을 덮어 두면 됩니다)
struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession

    func makeUIView(context: Context) -> PreviewView {
        let v = PreviewView()
        v.backgroundColor = .black
        v.videoPreviewLayer.session = session
        v.videoPreviewLayer.videoGravity = .resizeAspect
        return v
    }

    func updateUIView(_ uiView: PreviewView, context: Context) {
        if uiView.videoPreviewLayer.session !== session {
            uiView.videoPreviewLayer.session = session
        }
    }

    final class PreviewView: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var videoPreviewLayer: AVCaptureVideoPreviewLayer {
            layer as! AVCaptureVideoPreviewLayer
        }
    }
}
