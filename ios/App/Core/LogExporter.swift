import Foundation
import UIKit

/// 로그 내보내기.
///
/// 이 파일은 장식이 아니라 핵심 인프라입니다.
/// 개발자가 실기기를 만질 수 없으므로, 사용자가 폰에서 로그를 꺼내 대화창에
/// 붙여넣는 경로가 유일한 디버깅 채널입니다.
///
/// 내보내는 내용 = 기기 진단 리포트 + 앱 로그 전체.
/// 진단 리포트를 항상 머리말로 붙이는 이유: 로그만 받으면 "어떤 기기? 어떤 빌드?"를
/// 되묻는 왕복이 한 번 더 생기기 때문입니다.
enum LogExporter {

    static func buildText() -> String {
        var s = DeviceProbe.reportText()
        s += "\n"
        let lines = AppLog.shared.snapshot()
        s += "========================================================\n"
        s += " 앱 로그 (\(lines.count) 줄)\n"
        s += "========================================================\n"
        if lines.isEmpty {
            s += "(비어 있음)\n"
        } else {
            s += lines.joined(separator: "\n")
            s += "\n"
        }
        return s
    }

    /// 임시 폴더에 txt 파일을 만들고 URL 을 돌려줍니다.
    static func writeToTemp() -> URL? {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("logs", isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: dir,
                                                    withIntermediateDirectories: true)
            // 오래된 파일 정리
            if let olds = try? FileManager.default.contentsOfDirectory(at: dir,
                                                                      includingPropertiesForKeys: nil) {
                if olds.count > 8 {
                    for u in olds.prefix(olds.count - 8) {
                        try? FileManager.default.removeItem(at: u)
                    }
                }
            }
            let fmt = DateFormatter()
            fmt.dateFormat = "yyyyMMdd-HHmmss"
            fmt.locale = Locale(identifier: "en_US_POSIX")
            let model = BuildInfo.deviceModelIdentifier
                .replacingOccurrences(of: ",", with: "-")
            let name = "mocapsync_\(model)_\(fmt.string(from: Date())).txt"
            let url = dir.appendingPathComponent(name)
            try buildText().write(to: url, atomically: true, encoding: .utf8)
            AppLog.shared.i("LogExporter", "로그 파일 생성: \(name)")
            return url
        } catch {
            AppLog.shared.e("LogExporter", "파일 생성 실패: \(error)")
            return nil
        }
    }

    static func copyToClipboard() -> String {
        let full = buildText()
        let limit = 200_000
        let text: String
        if full.count <= limit {
            text = full
        } else {
            let tail = String(full.suffix(limit))
            text = "(앞부분 \(full.count - limit) 글자 생략 — 전체는 '공유'로 파일을 받으세요)\n" + tail
        }
        UIPasteboard.general.string = text
        AppLog.shared.i("LogExporter", "클립보드 복사 (\(text.count) 글자)")
        return "클립보드에 복사했습니다 (\(text.count) 글자)"
    }
}

/// SwiftUI 에서 iOS 공유 시트를 띄우기 위한 래퍼.
struct ShareSheet: UIViewControllerRepresentable {
    let items: [Any]

    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }

    func updateUIViewController(_ vc: UIActivityViewController, context: Context) {}
}
