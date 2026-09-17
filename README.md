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
| 0 | PC에서 Pose2Sim 데모 실행 → `.trc` 생성 확인 | 진행 중 |
| **1** | **GitHub 리포 + Actions APK 빌드 + 기기 진단 화면** | **현재** |
| 2 | 클럭 동기 전용 최소 앱 (성공 판정: 오차 2ms 미만) | 대기 |
| 3 | 녹화 + 예약 시작 + 타임스탬프 사이드카 | 대기 |
| 4 | PC 업로드 수신 + 키포인트 리샘플러 + Pose2Sim 자동 실행 | 대기 |
| 5 | 웹 3D 뷰어 + 품질 대시보드 | 대기 |
| 6 | 마스터 미리보기 그리드, 배터리/온도 모니터 | 대기 |
| 7 | (선택) 실시간 모드 — 영상 대신 키포인트만 전송 | 대기 |

설계 근거와 확정 사항은 [docs/DESIGN.md](docs/DESIGN.md) 에 있습니다.

## 폴더 구조

```
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
