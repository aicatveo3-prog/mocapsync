import Foundation
import os

/// 앱 내 로그 버퍼.
///
/// 왜 필요한가:
/// 개발자는 실기기를 만질 수 없습니다. Xcode 콘솔도 볼 수 없습니다.
/// (코드 작성 -> CI 빌드 -> 사용자가 설치/실행 -> 로그 첨부 -> 수정)
/// 그래서 Console.app 이 아니라 **앱 안에서 읽고 파일로 뽑을 수 있는 로그**가
/// 1일차부터 필요합니다. 안드로이드 버전의 AppLog 와 같은 역할입니다.
final class AppLog: ObservableObject {

    static let shared = AppLog()

    private static let maxLines = 4000
    private let logger = Logger(subsystem: "com.mocapsync.ios", category: "app")
    private let lock = NSLock()
    private var buffer: [String] = []

    /// UI 가 관찰합니다. 메인 스레드에서만 갱신합니다.
    @Published private(set) var revision: Int = 0

    private lazy var timeFmt: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss.SSS"
        f.locale = Locale(identifier: "en_US_POSIX")
        return f
    }()

    private init() {}

    func d(_ tag: String, _ msg: String) { write("D", tag, msg) }
    func i(_ tag: String, _ msg: String) { write("I", tag, msg) }
    func w(_ tag: String, _ msg: String) { write("W", tag, msg) }
    func e(_ tag: String, _ msg: String) { write("E", tag, msg) }

    /// 사용자가 "지금 이 순간"을 표시하고 싶을 때 (예: 스톱워치 촬영 직전)
    func mark(_ msg: String) { write("M", "MARK", "──────── \(msg) ────────") }

    private func write(_ level: String, _ tag: String, _ msg: String) {
        let line = "\(timeFmt.string(from: Date())) \(level)/\(tag): \(msg)"

        lock.lock()
        buffer.append(line)
        if buffer.count > Self.maxLines {
            buffer.removeFirst(buffer.count - Self.maxLines)
        }
        lock.unlock()

        // os_log 에도 남깁니다 (USB 로 Console.app 을 볼 수 있는 상황에서는 이게 편함)
        switch level {
        case "E": logger.error("\(tag, privacy: .public): \(msg, privacy: .public)")
        case "W": logger.warning("\(tag, privacy: .public): \(msg, privacy: .public)")
        default: logger.info("\(tag, privacy: .public): \(msg, privacy: .public)")
        }

        // @Published 는 메인 스레드에서만 건드려야 합니다.
        // 카메라 콜백이나 네트워크 스레드에서 호출될 수 있으므로 반드시 디스패치합니다.
        if Thread.isMainThread {
            revision &+= 1
        } else {
            DispatchQueue.main.async { [weak self] in
                self?.revision &+= 1
            }
        }
    }

    func snapshot() -> [String] {
        lock.lock()
        defer { lock.unlock() }
        return buffer
    }

    var count: Int {
        lock.lock()
        defer { lock.unlock() }
        return buffer.count
    }

    func clear() {
        lock.lock()
        buffer.removeAll()
        lock.unlock()
        if Thread.isMainThread {
            revision &+= 1
        } else {
            DispatchQueue.main.async { [weak self] in self?.revision &+= 1 }
        }
    }
}
