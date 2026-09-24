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

    /// ★ 부팅 이후 누적 절전 시간 (나노초).
    ///
    /// `CLOCK_MONOTONIC_RAW` 는 절전 중에도 흐르고 `CLOCK_UPTIME_RAW` 는 멈추므로,
    /// 둘의 차이가 "지금까지 잠들어 있던 시간"입니다.
    ///
    /// ★ 이 값이 왜 중요한가 (2026-09-24 실측으로 밝혀짐)
    ///
    ///   19:11 측정 오프셋  +5,522,186 ms
    ///   20:04 측정 오프셋  +7,560,244 ms
    ///   실제 경과 52분 38초인데 폰 시계는 18분 40초만 진행 → **34분을 잤음**
    ///
    /// 즉 폰이 한 번 자면 이전에 측정한 클럭 오프셋은 **그 즉시 무효**입니다.
    /// 그래서 동기 시점과 녹화 시점의 이 값을 각각 기록해 두고, 값이 달라졌으면
    /// "오프셋이 낡았다"고 판정합니다. 사이드카에 두 값을 모두 넣는 이유입니다.
    /// ★★ 주의: 이 값은 **매번 조금씩 다릅니다.**
    ///
    /// 두 시계를 연달아 읽어서 빼기 때문에, 그 사이에 흐른 시간(수십~수백 ns)이
    /// 그대로 결과에 섞입니다. 아이폰 11 실측에서 잔 적이 없는데도 42 ns 가
    /// 나왔습니다. 즉 **같은 값이 두 번 나오는 일은 사실상 없습니다.**
    ///
    /// 그래서 두 시점의 이 값을 비교할 때는 반드시 `sleepNoiseToleranceNs` 를
    /// 허용 오차로 써야 합니다. 정확히 같은지(`!=`) 를 보면 "항상 잤다"가 됩니다.
    ///
    /// 실제로 그 버그를 냈습니다: 사이드카 검증이 `sleepAtRecordStartNs !=
    /// sleepAtSyncNs` 로 판정해서 모든 촬영이 '치명'으로 거부됐습니다.
    /// 테스트는 두 값을 똑같이 넣어서 통과했고 — 현실에서는 같을 수 없는 값인데요.
    @inlinable
    public static func cumulativeSleepNs() -> Int64 {
        realtimeNs() - nowNs()
    }

    /// `cumulativeSleepNs()` 두 번 호출의 차이가 이 값 이하면 "안 잤다"고 봅니다.
    ///
    /// 1 ms 로 잡은 근거
    ///  · 읽기 잡음은 실측 수십 ns 수준. 스케줄링에 밀려도 수백 µs 를 넘기 어렵습니다.
    ///  · 반대로 의미 있는 절전은 최소 수백 ms 단위입니다 (화면이 꺼지고 잠드는 데
    ///    그 이상 걸립니다). 1 ms 는 두 영역 사이에 넉넉히 들어갑니다.
    ///  · 혹시 1 ms 짜리 절전을 놓쳐도 피해가 1 ms 라서 오차 예산(2 ms) 안입니다.
    public static let sleepNoiseToleranceNs: Int64 = 1 * NS.perMilli

    /// 두 시점의 누적 절전시간을 비교해 "그 사이에 잤는지" 판정합니다.
    /// 읽기 잡음을 허용 오차로 흡수합니다.
    @inlinable
    public static func didSleep(from a: Int64, to b: Int64) -> Bool {
        (b - a) > sleepNoiseToleranceNs
    }

    /// ★ 부팅 시각 (epoch 나노초). 재부팅을 감지하는 데 씁니다.
    ///
    /// 왜 필요한가: `CLOCK_UPTIME_RAW` 는 부팅 때 0 으로 초기화됩니다.
    /// 그래서 저장해 둔 클럭 오프셋은 재부팅 후 **완전히 무의미**해집니다.
    /// 그런데 오프셋 값만 봐서는 재부팅했는지 알 수 없습니다.
    ///
    /// `kern.boottime` 은 부팅한 벽시계 시각이라 재부팅하면 값이 바뀝니다.
    /// 저장 시점과 사용 시점의 이 값을 비교하면 재부팅을 확실히 잡습니다.
    ///
    /// (절전만으로는 이 값이 바뀌지 않습니다. 절전은 cumulativeSleepNs 로 잡습니다)
    public static func bootTimeEpochNs() -> Int64 {
        var tv = timeval()
        var size = MemoryLayout<timeval>.stride
        if sysctlbyname("kern.boottime", &tv, &size, nil, 0) != 0 {
            return 0
        }
        return Int64(tv.tv_sec) * NS.perSecond + Int64(tv.tv_usec) * NS.perMicro
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
