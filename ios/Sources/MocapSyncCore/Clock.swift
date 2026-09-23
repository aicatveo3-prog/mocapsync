import Foundation

/// 단조 시계.
///
/// ★ 규약의 가장 중요한 규칙 (docs/PROTOCOL.md §1):
/// **동기에 쓰는 시계는 카메라 프레임에 도장 찍는 시계와 같아야 합니다.**
///
/// iOS 에서 `AVCaptureVideoDataOutput` 이 주는 `CMSampleBufferGetPresentationTimeStamp`
/// 는 캡처 세션의 동기화 클럭(`CMClockGetHostTimeClock()`) 기준이고, 그 클럭은
/// `mach_absolute_time` 도메인입니다. `CLOCK_UPTIME_RAW` 가 같은 도메인을
/// 나노초로 바로 주므로 이걸 씁니다.
///
/// 이렇게 하면 변환이 한 겹도 필요 없습니다. (안드로이드는 기기에 따라
/// elapsedRealtime <-> uptime 변환이 필요했습니다)
///
/// 주의: `CLOCK_UPTIME_RAW` 는 기기가 절전(sleep)하면 멈춥니다.
/// 세션 중에는 화면을 켜 두므로 문제없지만, 백그라운드/절전을 거친 뒤에는
/// 오프셋을 **반드시 다시 측정**해야 합니다.
public enum MonotonicClock {

    /// 현재 단조 시각 (나노초).
    @inlinable
    public static func nowNs() -> Int64 {
        Int64(bitPattern: clock_gettime_nsec_np(CLOCK_UPTIME_RAW))
    }

    /// 이 시계의 이름. hello 메시지와 사이드카에 기록해서 나중에 추적 가능하게 합니다.
    public static let name = "CLOCK_UPTIME_RAW"

    /// 절전 중에도 흐르는 시계. 두 시계의 차이를 보면 절전을 거쳤는지 알 수 있습니다.
    @inlinable
    public static func realtimeNs() -> Int64 {
        Int64(bitPattern: clock_gettime_nsec_np(CLOCK_MONOTONIC_RAW))
    }
}

/// 나노초 상수.
public enum NS {
    public static let perMicro: Int64 = 1_000
    public static let perMilli: Int64 = 1_000_000
    public static let perSecond: Int64 = 1_000_000_000
}

public extension Int64 {
    /// 나노초 -> 밀리초 (표시용)
    var ms: Double { Double(self) / Double(NS.perMilli) }
}
