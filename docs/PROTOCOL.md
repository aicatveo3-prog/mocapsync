# MocapSync 통신 규약 v1

마스터와 슬레이브 사이의 유선 규약(wire protocol)입니다.

**이 문서가 계약서입니다.** iOS 앱(Swift)과 PC 테스트 마스터(Python)가 각각 이걸 구현하고,
서로 통신해야 합니다. 구현이 어긋나면 이 문서를 고치고 양쪽을 맞춥니다.

> 왜 PC 구현을 먼저 만드는가:
> 개발자가 Windows에서 Swift 를 컴파일할 수 없습니다. 그래서 규약과 시각 계산을
> **먼저 Python 으로 만들고 완전히 테스트**한 뒤, iOS 앱이 "이미 검증된 상대"와
> 통신하게 합니다. 문제가 생겼을 때 "규약이 틀렸나"를 의심할 필요가 없어집니다.
> 부수 효과로 폰 1대만으로도 클럭 동기 측정이 가능해집니다 (PC가 마스터 역할).

---

## 1. 시계 규칙 ★ 가장 중요

모든 시각은 **정수 나노초**이며, **각 기기 자신의 단조 시계(monotonic clock)** 값입니다.
기기 간에 공통 기준점(epoch)이 없어도 됩니다. 우리가 구하는 것은 두 시계의 **차이**입니다.

### 규칙: 동기에 쓰는 시계는 카메라 프레임에 도장 찍는 시계와 **같아야** 합니다

이걸 어기면 변환이 한 겹 더 생기고, 그 변환이 오차의 주범이 됩니다.

| 플랫폼 | 사용할 시계 | 카메라 타임스탬프와 같은 도메인인가 |
| --- | --- | --- |
| **iOS** | `clock_gettime_nsec_np(CLOCK_UPTIME_RAW)` | ✅ `mach_absolute_time` 도메인 = AVFoundation 의 host time clock = `CMSampleBufferGetPresentationTimeStamp` 기준 |
| Android | `SystemClock.elapsedRealtimeNanos()` | ⚠️ `SENSOR_INFO_TIMESTAMP_SOURCE` 가 REALTIME 이면 같음. UNKNOWN 이면 `(elapsedRealtime - uptime)` 델타 변환 필요 |
| PC (Python) | `time.monotonic_ns()` | (PC는 프레임을 찍지 않으므로 무관) |

iOS 단일 플랫폼으로 가면 이 표의 경고가 사라집니다. 아이폰은 하나의 시계만 씁니다.

> 주의: `CLOCK_UPTIME_RAW` 는 기기가 절전(sleep)에 들어가면 멈춥니다.
> 세션 중에는 화면을 켜 두므로 문제되지 않지만, **절전을 거친 뒤에는 오프셋을 반드시
> 다시 측정해야 합니다.** 앱은 `UIApplication.shared.isIdleTimerDisabled = true` 로
> 화면 꺼짐을 막고, 백그라운드 복귀 시 오프셋을 무효화합니다.

---

## 2. 전송 계층

- **TCP**, 한 연결당 한 슬레이브
- **`TCP_NODELAY` 필수** (Nagle 알고리즘이 왕복 시간을 부풀립니다)
- **줄바꿈으로 구분된 JSON** (newline-delimited JSON). 한 줄 = 한 메시지, UTF-8
- 기본 포트 **9001** (mDNS 로 광고되므로 하드코딩 금지)

### 왜 UDP 가 아닌가

고전적 NTP 는 UDP 를 씁니다. TCP 는 재전송·혼잡제어가 왕복 시간에 끼어들 수 있으니까요.
그럼에도 v1 은 TCP 를 택했습니다.

- 구현이 훨씬 단순합니다 (패킷 손실·순서 처리 불필요)
- 우리는 **왕복을 40회 하고 RTT 최소 샘플만 채택**합니다. 재전송이 낀 샘플은 RTT 가
  크게 튀므로 자동으로 탈락합니다
- 로컬 WiFi 1홉이라 손실이 드뭅니다

**만약 실측에서 최소 RTT 가 목표를 못 맞추면 UDP 로 바꿉니다.** 판단 기준은 §4 에 있습니다.

---

## 3. 기기 탐색 (mDNS / Bonjour)

- 서비스 타입: **`_mocapsync._tcp`**
- 마스터가 광고, 슬레이브가 탐색 → **IP 주소 입력 불필요**
- TXT 레코드:

| 키 | 예시 | 의미 |
| --- | --- | --- |
| `proto` | `1` | 규약 버전 |
| `role` | `master` | 광고자 역할 |
| `impl` | `py` / `ios` | 구현 종류 (디버깅용) |
| `sid` | `S20260917-1` | 현재 세션 ID (없으면 빈 값) |

구현:
- Python: `zeroconf`
- iOS: `NWListener` + `NWBrowser` (Network framework). Info.plist 에
  `NSLocalNetworkUsageDescription` 과 `NSBonjourServices` (`_mocapsync._tcp`) 필요.
  **이 두 항목이 없으면 iOS 14+ 에서 조용히 실패합니다.**

---

## 4. 시각 동기 절차

### 4.1 메시지 교환

슬레이브가 **자신의** 오프셋을 알아야 하므로 **슬레이브가 요청을 보냅니다.**

```
슬레이브                                    마스터
   |                                          |
   |  t1 = 내 시계                            |
   |--- time_req {seq, t1} ------------------->|
   |                                  t2 = 마스터 시계 (수신 시각)
   |                                  t3 = 마스터 시계 (송신 시각)
   |<-- time_resp {seq, t1, t2, t3} -----------|
   |  t4 = 내 시계                            |
```

이걸 기본 **40회** 반복합니다 (설계 문서: 20~50회).

### 4.2 계산

```
오프셋  θ = ((t2 - t1) + (t3 - t4)) / 2
왕복시간 δ = (t4 - t1) - (t3 - t2)
```

**θ 의 부호 정의**: `마스터시각 = 슬레이브시각 + θ`
따라서 슬레이브가 마스터 시각을 자기 시각으로 바꿀 때는 **빼야** 합니다.

```
슬레이브시각 = 마스터시각 - θ
```

### 4.3 왜 이 식인가, 그리고 오차의 한계 ★

진짜 편도 지연을 `d_up`(슬레이브→마스터), `d_dn`(마스터→슬레이브) 라 하면

```
t2 = t1 + d_up + θ
t4 = t3 + d_dn - θ
```

여기서

```
δ  = (t4 - t1) - (t3 - t2) = d_up + d_dn
θ̂  = ((t2 - t1) + (t3 - t4)) / 2 = θ + (d_up - d_dn) / 2
```

즉 **추정 오차 = (d_up − d_dn) / 2**. 경로가 대칭이면 오차가 **0** 입니다.

`d_up, d_dn ≥ 0` 이고 `d_up + d_dn = δ` 이므로 `|d_up − d_dn| ≤ δ` 입니다. 따라서

> ### **|오차| ≤ δ / 2**
>
> **최소 RTT 가 4ms 미만이면 오프셋 오차가 2ms 미만임이 보장됩니다.**

이게 설계 문서의 "오차 2ms 미만" 목표를 **측정 가능하고 증명 가능한 기준**으로 바꿔줍니다.
"2ms 인 것 같다"가 아니라 "최소 RTT 가 1.4ms 였으므로 오차는 0.7ms 이하"라고 말할 수 있습니다.

그래서 앱은 오프셋만 보여주지 않고 **항상 최소 RTT 와 그로부터 계산된 상한을 함께**
표시합니다.

### 4.4 추정기 (양쪽 구현이 동일해야 함)

1. N 개 샘플 수집 (기본 40)
2. RTT 가 음수이거나 비정상인 샘플 폐기 (시계 점프, 버그)
3. RTT 오름차순 정렬
4. 상위 `best_k` 개(기본 1) 채택 → 그 오프셋의 중앙값을 최종 오프셋으로
5. 보고 항목:
   - `offset_ns` 최종 오프셋
   - `min_rtt_ns` 최소 왕복시간
   - `uncertainty_ns = min_rtt_ns / 2` ← **보장된 오차 상한**
   - `spread_ns` 최소 RTT 10개 샘플의 오프셋 최대-최소 (실제 흔들림 크기)
   - `samples_total`, `samples_used`

`uncertainty_ns` 와 `spread_ns` 를 둘 다 보는 이유: 전자는 이론적 보장, 후자는
실제 관측된 안정성입니다. spread 가 uncertainty 보다 크면 뭔가 잘못된 것입니다
(시계 드리프트, 스로틀링, 백그라운드 전환).

### 4.6 ★ 오해하기 쉬운 점: 무엇이 정밀해야 하는가

PC 구현을 만들면서 실측으로 확인한 중요한 사실입니다.

**예약 시작 시각의 정밀도는 임계 경로가 아닙니다.**

처음에는 "예약 시각에 정확히 시작해야 동기가 맞는다"고 생각해서 시작 지연을 줄이는 데
공을 들였습니다 (단순 sleep +11.7ms → 스핀 대기 +4.2ms). 그런데 다시 생각해 보면
**시작 순간이 조금 어긋나도 결과에 영향이 없습니다.**

이유: 우리는 **프레임마다 타임스탬프를 사이드카에 기록**하고, PC 리샘플러가 그
타임스탬프를 기준으로 공통 60Hz 격자에 보간합니다. 카메라 A 가 마스터시각
`[100.0, 116.7, 133.3, ...]` 에, B 가 `[104.2, 120.9, 137.6, ...]` 에 프레임을
찍었다면, 둘 다 같은 격자로 보간되므로 **시작이 4.2ms 어긋난 사실 자체는 사라집니다.**

그래서 정밀도 요구사항은 이렇게 갈립니다.

| 항목 | 정밀도 요구 | 근거 |
| --- | --- | --- |
| **클럭 오프셋 θ** | **2ms 미만 (핵심)** | 프레임 타임스탬프를 공통 시간축으로 옮기는 데 직접 쓰입니다. 여기가 틀리면 전부 틀립니다 |
| **프레임 타임스탬프** | **하드웨어 정확도 (핵심)** | AVFoundation 이 센서 노출 시점을 줍니다. 우리가 손댈 수 없고, 손댈 필요도 없습니다 |
| 예약 시작 시각 | **±수십 ms 로 충분** | 두 폰이 동작 구간 동안 돌고 있기만 하면 됩니다 |

**그럼 예약 시작은 왜 하는가?** "지금 찍어" 방식과 비교하면 답이 나옵니다.

- **"지금 찍어"**: 시작 시각 차이 = 패킷 지연 차이. **크기를 알 수 없고 한계도 없습니다.**
  한 폰이 300ms 늦게 시작하면 동작의 앞부분을 놓칩니다
- **예약 시작**: 시작 시각 차이 = 타이머 오차뿐. **수 ms 로 한계가 있습니다.**
  게다가 얼마나 늦었는지 사이드카에 기록되므로 확인 가능합니다

즉 예약 시작의 목적은 **"패킷 지연을 무해하게 만드는 것"**이고, 그건 ±5ms 로도 완전히
달성됩니다. 마이크로초를 다툴 필요가 없습니다.

> 다만 사이드카의 `actual_first_frame_local_ns - scheduled_start_local_ns` 는 계속
> 기록합니다. 이 값이 갑자기 수백 ms 로 튀면 카메라 세션 준비가 안 된 상태에서
> 시작했다는 뜻이므로 **버그 탐지 지표**로 유용합니다.

### 4.7 정밀 대기 패턴 (구현 참고)

그래도 시작을 깔끔하게 맞추는 건 공짜에 가까우니 아래 패턴을 씁니다.

**단순 sleep 은 쓰지 마세요.** OS 타이머 해상도 때문에 수 ms~수십 ms 늦습니다.
(실측: Windows asyncio 기본 해상도 약 15.6ms)

```
1) 목표시각 - 마진  까지는 OS 에 양보하며 대기 (CPU 절약)
2) 남은 마진 구간은 단조 시계를 바쁜대기(spin)
```

| 플랫폼 | 마진 | 권장 API |
| --- | --- | --- |
| Windows (PC 마스터) | 20ms | `asyncio.sleep` + spin |
| **iOS** | 2~3ms | **`mach_wait_until()`** 이 커널 수준에서 정밀하게 깨워줍니다. 그 뒤 짧은 spin |

### 4.5 결과 보고

슬레이브가 계산을 마치면 마스터에게 알립니다. 마스터 화면에 큰 글씨로 띄우기 위함입니다.

```json
{"type":"time_result","offsetNs":-1234567,"minRttNs":1420000,
 "uncertaintyNs":710000,"spreadNs":180000,"samplesTotal":40,"samplesUsed":1}
```

---

## 5. 예약 시작 ★ 핵심

**"지금 찍어" 방식은 절대 쓰지 않습니다.** 패킷 지연이 그대로 동기 오차가 되기 때문입니다.

마스터가 **미래의 절대 시각**을 지정해 보냅니다.

```json
{"type":"schedule_start","sessionId":"S20260917-1",
 "startAtMasterNs":123456789000000,
 "targetFps":60,"width":1920,"height":1080,
 "lockAe":true,"lockAwb":true,"lockFocus":true,
 "stabilization":"off","maxExposureNs":2000000}
```

슬레이브의 처리:

```
startAtSlaveNs = startAtMasterNs - offsetNs
남은시간 = startAtSlaveNs - 지금(내 시계)
```

- 남은시간이 **최소 여유(기본 300ms)보다 작으면 거부**하고 `nack` 을 보냅니다.
  늦게 도착한 명령을 억지로 따라가면 오히려 틀어집니다
- 카메라 세션은 **미리** 열어두고(`armed` 상태), 그 시각에 기록만 시작합니다.
  세션 시작에 수백 ms 가 걸리므로 예약 시각에 열기 시작하면 늦습니다
- WiFi 패킷 지연이 300ms 안이면 **지연이 결과에 전혀 영향을 주지 않습니다.** 이게 이 방식의 요점입니다

응답:

```json
{"type":"start_ack","sessionId":"S20260917-1","startAtSlaveNs":...,"leadMs":487}
{"type":"start_nack","sessionId":"S20260917-1","reason":"lead_too_small","leadMs":42}
```

---

## 6. 메시지 목록

모든 메시지에 `type` 필드가 있습니다. 모르는 `type` 은 **무시**합니다(전방 호환).

| type | 방향 | 용도 |
| --- | --- | --- |
| `hello` | 슬레이브 → 마스터 | 접속 인사. 기기 정보 |
| `hello_ack` | 마스터 → 슬레이브 | 수락. 규약 버전 합의 |
| `time_req` | 슬레이브 → 마스터 | 시각 요청 |
| `time_resp` | 마스터 → 슬레이브 | 시각 응답 |
| `time_result` | 슬레이브 → 마스터 | 오프셋 계산 결과 보고 |
| `schedule_start` | 마스터 → 슬레이브 | 예약 녹화 시작 |
| `start_ack` / `start_nack` | 슬레이브 → 마스터 | 예약 수락/거부 |
| `stop` | 마스터 → 슬레이브 | 녹화 종료 |
| `status` | 슬레이브 → 마스터 | 주기 상태 (배터리, 온도, 프레임 수) |
| `error` | 양방향 | 오류 통지 |

### hello

```json
{"type":"hello","proto":1,"deviceId":"A1B2C3","name":"iPhone-1",
 "platform":"ios","model":"iPhone12,1","osVersion":"17.5.1",
 "appVersion":"0.2.0","clock":"CLOCK_UPTIME_RAW"}
```

`deviceId` 는 기기마다 고정이어야 합니다 (PC가 캘리브레이션 파일을 자동 매칭하는 키).
iOS 는 `identifierForVendor` 를 쓰되, 앱 재설치로 바뀌므로 **첫 실행 시 UUID 를 만들어
Keychain 에 저장**하는 방식을 권합니다.

### status

```json
{"type":"status","state":"idle","battery":0.87,"thermal":"nominal",
 "framesCaptured":0,"freeBytes":12345678901}
```

`state`: `idle` | `synced` | `armed` | `recording` | `uploading` | `error`
`thermal` (iOS `ProcessInfo.thermalState`): `nominal` | `fair` | `serious` | `critical`

> 발열은 실제 위험입니다. 60fps 촬영은 열을 많이 내고, `serious` 이상이면 iOS 가
> 프레임을 떨어뜨립니다. 마스터가 이걸 보고 경고해야 합니다.

---

## 7. 사이드카 JSON (녹화 산출물)

영상과 **함께** 저장되어 PC로 업로드됩니다. 설계 문서의 필드명을 유지합니다.

```json
{
  "schema": 1,
  "device_id": "A1B2C3",
  "session_id": "S20260917-1",
  "platform": "ios",
  "model": "iPhone12,1",
  "clock": "CLOCK_UPTIME_RAW",
  "timestamp_source": "HOST_TIME",
  "clock_offset_ns": -1234567,
  "clock_uncertainty_ns": 710000,
  "min_rtt_ns": 1420000,
  "scheduled_start_master_ns": 123456789000000,
  "scheduled_start_local_ns": 123456790234567,
  "actual_first_frame_local_ns": 123456790241000,
  "target_fps": 60,
  "width": 1920,
  "height": 1080,
  "frames": [[0, 123456790241000], [1, 123456790257667]]
}
```

`frames` 는 `[프레임번호, 그 프레임의 로컬 단조시계 나노초]` 입니다.

**PC 가 하는 일**: `frames` 의 각 시각에 `clock_offset_ns` 를 더해 마스터 시간축으로
옮기고, 모든 카메라를 공통 60Hz 격자에 cubic spline 보간합니다 (설계 4-③).
영상은 절대 리인코딩하지 않습니다.

`actual_first_frame_local_ns` 와 `scheduled_start_local_ns` 의 차이가 **실제 시작 지연**입니다.
이 값이 프레임 간격(60fps = 16.67ms)보다 크면 예약 시작이 제대로 동작하지 않은 것입니다.

---

## 8. 버전 협상

- `hello.proto` 와 `hello_ack.proto` 가 다르면 **연결을 끊습니다**
- 규약을 바꿀 때마다 `proto` 를 올리고 이 문서를 갱신합니다
- 알 수 없는 필드와 알 수 없는 `type` 은 무시 (전방 호환)

---

## 9. 구현 현황

| 구성 | 위치 | 상태 |
| --- | --- | --- |
| 오프셋 계산 (순수 함수) | `server/mocapsync/clocksync.py` | ✅ |
| 메시지 인코딩/디코딩 | `server/mocapsync/protocol.py` | ✅ |
| PC 테스트 마스터 | `server/master.py` | ✅ |
| 슬레이브 시뮬레이터 | `server/slave_sim.py` | ✅ |
| 테스트 | `server/tests/` | ✅ |
| iOS 앱 | `ios/` | 미착수 |

시뮬레이터는 인공 지연·흔들림·**경로 비대칭**·가짜 클럭 오프셋을 주입할 수 있습니다.
같은 PC 에서 돌리면 진짜 오프셋을 알고 있으므로 **추정기의 정확도를 직접 채점**할 수 있습니다.
