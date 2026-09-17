# MocapSync

안드로이드 폰 여러 대로 마커리스 3D 모션캡쳐를 하는 시스템.

3D 복원은 [Pose2Sim](https://github.com/perfanalytics/pose2sim)(BSD-3-Clause)에 위임합니다.
이 리포지토리가 만드는 것은 그 앞뒤, 즉 **촬영 오케스트레이션 + 데이터 시간 정렬 + 뷰어**입니다.

```
[안드로이드 폰 N대]  ──업로드──▶  [PC: FastAPI]  ──▶  [Pose2Sim]  ──▶  [웹 3D 뷰어]
  마스터/슬레이브                  키포인트 리샘플러         삼각측량/필터        .trc 재생
  예약 시작 + 클럭 동기            (60Hz 공통 타임그리드)
```

## 현재 진행 단계

| 단계 | 내용 | 상태 |
| --- | --- | --- |
| **0** | **PC에서 Pose2Sim 데모 실행 → `.trc` + `.mot` 생성 확인** | **완료** |
| 1 | GitHub 리포 + Actions APK 빌드 + 기기 진단 화면 | 코드 완료, 푸시 대기 |
| 2 | 클럭 동기 전용 최소 앱 (성공 판정: 오차 2ms 미만) | 대기 |
| 3 | 녹화 + 예약 시작 + 타임스탬프 사이드카 | 대기 |
| 4 | PC 업로드 수신 + 키포인트 리샘플러 + Pose2Sim 자동 실행 | 대기 |
| 5 | 웹 3D 뷰어 + 품질 대시보드 | 대기 |
| 6 | 마스터 미리보기 그리드, 배터리/온도 모니터 | 대기 |
| 7 | (선택) 실시간 모드 — 영상 대신 키포인트만 전송 | 대기 |

설계 근거와 확정 사항은 [docs/DESIGN.md](docs/DESIGN.md) 에 있습니다.

## 0단계: Pose2Sim 데모 실행 (완료)

### 설치 (한 번만)

```powershell
# uv 설치
winget install --id astral-sh.uv

# 격리 환경 (공식 문서와 동일)
uv venv "$env:USERPROFILE\.venv\pose2sim" --python 3.13
uv pip install --python "$env:USERPROFILE\.venv\pose2sim\Scripts\python.exe" pose2sim
```

`opensim` 이 pip 의존성에 포함되어 있어 **OpenSim 을 따로 설치할 필요가 없습니다.**

### 실행

```powershell
$env:PYTHONUTF8 = '1'
& "$env:USERPROFILE\.venv\pose2sim\Scripts\python.exe" tools\pose2sim_demo.py
```

이 스크립트는 데모 폴더를 복사하고, 8단계를 순서대로 돌린 뒤,
**산출물을 검증해서 성공/실패를 판정**합니다. 옵션은 `--help` 참고.

기본은 `--mode headless` 입니다. Pose2Sim 기본 설정은 `synchronization_gui = true`
라서 중간에 사람이 클릭할 때까지 멈추는데, 첫 실행은 무인으로 끝까지 돌려보는 게
낫기 때문입니다. 공식 문서처럼 창을 다 보려면 `--mode interactive`.

### 실측 결과 (i7-14700HX / CPU만 사용)

| 단계 | 소요 |
| --- | --- |
| calibration | 1.6초 |
| poseEstimation | 26.5초 |
| synchronization | 6.3초 |
| personAssociation | 1.7초 |
| triangulation | 3.6초 |
| filtering | 5.1초 |
| markerAugmentation | 0.3초 |
| kinematics | 7.9초 |
| **합계** | **53.1초** |

산출물 (카메라 4대 / 97프레임 / 60Hz):

- `pose-3d/*.trc` 3개 — 22마커(원본), 22마커(필터), **65마커(LSTM 증강)**, 결측 0.00%
- `kinematics/*.osim` 스케일된 전신 OpenSim 모델 (2.3MB)
- `kinematics/*.mot` 관절각 **63개 좌표 × 96프레임**, degree 단위
  (골반, 양쪽 고관절·무릎·발목·subtalar·mtp, 요추 L5-S1~L1-T12, 목, 양팔·팔꿈치·손목)

품질 지표:

| 항목 | 값 | 기준 |
| --- | --- | --- |
| 캘리브레이션 잔차 (RMS, 카메라별) | 0.221 / 0.235 / 0.171 / 0.191 px | 0.5 px 미만 권장 → 통과 |
| 평균 재투영 오차 | 약 10 px ≈ 18~21 mm | — |
| 배제된 카메라 수 (평균) | 0.05대 | 낮을수록 좋음 |
| 보간된 프레임 | 0개 | — |
| IK 마커 오차 (RMS) | 평균 22.7 mm, 최대 28.9 mm | 마커리스 통상 범위 |

> **GPU는 아직 안 씁니다.** 설치된 `onnxruntime 1.30.0` 은 CPU 전용이고
> providers 가 `['AzureExecutionProvider', 'CPUExecutionProvider']` 입니다.
> RTX 4060 을 쓰려면 `onnxruntime-gpu` 로 교체해야 합니다. 데모는 CPU로 53초라
> 급하지 않지만, 실제 촬영(수천 프레임)에서는 큰 차이가 납니다.

## 폴더 구조

```
tools/pose2sim_demo.py    0단계 데모 러너 (8단계 실행 + 산출물 검증)
android/                  Kotlin + Jetpack Compose 앱 (단일 APK, MASTER/SLAVE 역할 선택)
  app/src/main/java/com/mocapsync/app/
    MainActivity.kt
    core/AppLog.kt          앱 내 로그 버퍼 (실기기 디버깅의 유일한 채널)
    core/LogExporter.kt     로그 + 진단 리포트를 파일/클립보드로 내보내기
    core/DeviceProbe.kt     카메라 권한 없이 기기 능력 진단
    ui/                     Compose 화면
.github/workflows/
  android-apk.yml         APK 빌드 → 아티팩트 + "latest" 릴리스 갱신
docs/DESIGN.md            확정된 설계 (재논의 대상 아님)
```

`server/` (PC 파이프라인)와 `viewer/` (three.js)는 4·5단계에서 추가합니다.

## APK 받기 / 설치

Android Studio 없이 폰에 설치할 수 있게 만들어 두었습니다.

**방법 A — 폰에서 직접 (권장)**
1. 폰 브라우저로 이 리포지토리의 **Releases** 를 엽니다.
2. `latest` 릴리스에서 `mocapsync-debug.apk` 를 누릅니다.
3. "알 수 없는 앱 설치"를 허용하고 설치합니다.

**방법 B — PC에서**
1. **Actions** 탭 → 최신 워크플로 실행 → 아래 **Artifacts** 에서 zip 다운로드.
2. 압축을 풀고 `.apk` 를 폰으로 옮겨 설치합니다.

> 서명 관련: CI는 debug 키스토어를 캐시해서 서명을 고정합니다. 그래서 보통은 덮어쓰기
> 설치가 됩니다. 캐시가 만료되면 서명이 한 번 바뀌는데, 그때는 "앱이 설치되지 않았습니다"
> 오류가 나므로 기존 앱을 삭제하고 다시 설치하세요.

## 로컬 빌드 (선택 — CI만 써도 됩니다)

Android Studio 없이 커맨드라인으로 빌드할 수 있습니다.

```
JDK 17          C:\Program Files\Eclipse Adoptium\jdk-17.0.20.101-hotspot
Android SDK     C:\Users\USER\Android\sdk   (platform-35, build-tools 35.0.0, platform-tools)
Gradle          android/gradlew (wrapper 8.13)
```

> **함정 하나.** AGP는 프로젝트 경로에 **non-ASCII 문자가 있으면 빌드를 거부합니다.**
> 이 프로젝트 폴더 이름(`모션캡쳐_Pose2Sim`)이 여기에 걸립니다.
> 그래서 ASCII 경로로 디렉터리 정션을 만들어 그 안에서 빌드합니다 (파일 복사 아님, 관리자 권한 불필요).
>
> ```powershell
> cmd /c mklink /J "C:\mocapsync-build" "C:\Users\USER\Desktop\모션캡쳐_Pose2Sim\android"
> ```
>
> CI는 리눅스의 ASCII 경로에서 돌기 때문에 영향받지 않습니다.

```powershell
$env:JAVA_HOME   = 'C:\Program Files\Eclipse Adoptium\jdk-17.0.20.101-hotspot'
$env:ANDROID_HOME = 'C:\Users\USER\Android\sdk'
cd C:\mocapsync-build
.\gradlew.bat assembleDebug
# 결과: android/app/build/outputs/apk/debug/app-debug.apk
```

USB 디버깅으로 바로 설치하려면:

```powershell
C:\Users\USER\Android\sdk\platform-tools\adb.exe install -r `
  "C:\Users\USER\Desktop\모션캡쳐_Pose2Sim\android\app\build\outputs\apk\debug\app-debug.apk"
```

## 1단계 성공 판정

아래 3개가 모두 되면 1단계는 끝입니다.

1. GitHub Actions 워크플로가 **초록색**으로 끝난다.
2. 폰에 APK가 설치되고 앱이 실행된다. 첫 화면에 `git commit` 과 `빌드시각`이 보인다.
3. **기기 진단** 화면이 열리고, `SENSOR_TIMESTAMP 소스` 값이 `REALTIME` 또는 `UNKNOWN`
   중 하나로 표시된다. 그 화면에서 **로그 공유** 가 동작한다.

3번의 리포트가 2·3단계 구현 방향을 결정합니다.

## 개발 루프

개발자는 실기기에서 빌드/실행/측정을 할 수 없습니다. 그래서 이 루프로 진행합니다.

```
코드 작성  →  CI가 APK 빌드  →  사람이 설치·실행  →  로그 공유로 결과 전달  →  수정
```

이 때문에 앱에는 처음부터 다음이 들어 있습니다.
- 앱 내 로그 버퍼 (`AppLog`) — logcat 접근 없이도 로그를 볼 수 있음
- **로그 공유 / 클립보드 복사** — 진단 리포트가 항상 머리말로 붙음
- 화면에 `git commit` / `빌드시각` / `CI run` 노출 — 어떤 빌드를 테스트했는지 되묻지 않기 위해

## 라이선스 관점의 기술 선택

상업 이용 가능한 것만 씁니다.

| 채택 | 이유 |
| --- | --- |
| Pose2Sim (BSD-3-Clause) | 무료 + 상업 이용 가능, 논문 검증 관절각 오차 2~6° |
| RTMPose `Body_with_feet` (HALPE_26) | 몸 정확도 기준 최적. `Whole_body`(133점)는 몸 정확도가 더 낮음 |

| 탈락 | 이유 |
| --- | --- |
| Kineo / NLF / Sapiens | 비상업 라이선스 |
| OpenCap | iOS 전용 + 클라우드 종속 |
| FreeMoCap | MediaPipe 기반, 정확도 하위 |
| OpenPose | 상업 이용 유료 |
| SMPL / SMPL-X | 등록 필요 + 비상업 |
