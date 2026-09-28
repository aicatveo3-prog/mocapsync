# 인수인계 — 다음 AI 에게 (2026-09-28 기준)

이 파일을 처음부터 끝까지 읽고 시작하세요. 설계 결정의 근거는 `docs/DESIGN.md`,
폰↔PC 규약은 `docs/PROTOCOL.md` 에 있습니다. 여기는 "지금 어디까지 왔고 무엇을 조심하나"입니다.

## 1. 프로젝트 한 줄

아이폰 11 두 대(iOS 17.4.1 / 17.5.1)로 **마커 없는 3D 모션캡처**. 폰 앱이 시계를 PC 에 맞추고
같은 순간에 녹화 → PC 로 업로드 → 2D 자세 추정(GPU) → **키포인트 리샘플러**(서브프레임 시각 정렬,
이 프로젝트의 핵심) → Pose2Sim 삼각측량 → 3D(TRC). 저장소: https://github.com/aicatveo3-prog/mocapsync (main).

## 2. 사용자와 일하는 방법 (꼭 지킬 것)

- **한국어**로, 쉽고 자세하게. 명령은 복사해서 붙일 수 있게. 단계마다 **"이렇게 보이면 성공"** 기준을 줍니다.
- **추측을 사실처럼 말하지 않습니다.** 확인한 것 / 못 한 것을 구분해서 말합니다.
- AI 는 폰에서 빌드·실행·시험을 **못 합니다.** 사용자가 손과 눈입니다. 폰 쪽 코드는 CI 컴파일까지만 검증됩니다.
- 사용자가 거절한 것: 유료 Apple 계정($99), 안드로이드, 웹캠, **LAN 케이블**(항상 WiFi), 폰 핫스팟(공기계라 불가).
- 사용자가 실험하지 않아도 되는 작업이면 그렇다고 먼저 알려 줍니다.

## 3. 지금 상태

| 단계 | 상태 |
| --- | --- |
| 0. Pose2Sim 데모 | 완료 |
| 1. 원격 촬영 (PC 가 여러 폰을 같은 순간 시작·정지) | **실기기 2대 성공** (2026-09-27, 첫 프레임 차이 7.1 ms) |
| 2. 클럭 동기 | 5GHz 공유기에서 오차 상한 약 1.7~2.2 ms |
| 3. 녹화 + 사이드카(프레임 시각) | 실기기 검증 완료 |
| 2D 자세 추정 GPU | CPU 대비 7.5~11.4배 |
| 키포인트 리샘플러 | 완료, 기본 makima (DESIGN §4.2) |
| 4. 파이프라인 러너 `tools/run_session.py` | 완료, 데모로 3D 까지 검증 (DESIGN §4.3) |
| PC 마스터 대시보드 (브라우저) | 완료, 가짜 폰으로 검증 (DESIGN §4.4). **실폰으로는 아직** |
| 기본 렌즈 초광각 0.5x | 빌드 24/25 에 들어감. **실폰 설치 확인 전** |
| **캘리브레이션** | **다음 할 일. 아직 없음** → 그래서 실폰 영상은 아직 3D 가 안 나옵니다 |
| 5. 3D 뷰어 | 아직 (대시보드에 붙일 예정, three.js) |

테스트: Python 220개 통과 (`server/tests`). Swift 테스트는 CI(`iOS Core Tests`).

## 4. 사용자에게 걸려 있는 일

1. **빌드 25 를 두 폰에 설치** (Sideloadly, 케이블 1개라 한 대씩):
   `C:\Users\USER\Desktop\MocapSync_IPA\MocapSync-unsigned-ipa-25\MocapSync-unsigned.ipa`
   - 성공 기준: 녹화 화면 "잠긴 설정" 카메라 = `BuiltInUltraWideCamera`, 화각 약 107.8도,
     대시보드 카메라 카드 배터리가 실제 % (예전엔 -100%).
   - 무료 Apple ID → **설치 후 7일이면 앱이 안 열립니다.** 재설치하면 다시 7일.
   - 설치 직후 카메라가 "구성 중..."에서 멈춘 적 있음 → 앱을 완전히 닫고 다시 열면 됐음 (원인 미조사).
2. 체커보드 인쇄: 바탕화면 `MocapSync_checkerboard_A4_7x10_23mm.pdf` (7×10칸, 23 mm, 안쪽 꼭짓점 6×9).
   "실제 크기"로 인쇄, 100 mm 막대를 자로 확인, 딱딱한 판에 평평하게. **아직 찍지 말라고** 안내했음.
3. 대시보드 실폰 시험: 바탕화면 "MocapSync 마스터" 아이콘 → 두 폰 "PC 원격 대기 켜기" → 녹화 시작/정지.

## 5. 다음 작업: 캘리브레이션 도구

필요한 것 (사용자에게 약속한 내용):
- **렌즈 특성(intrinsics)**: 폰마다 한 번. 체커보드를 손에 들고 여러 각도·화면 구석까지 천천히, **영상 모드로**
  (사진 모드 금지 — 형식이 달라짐). 목표 오차 0.5 px 이하. 초광각은 고정초점이라 초점 문제는 없음.
  왜곡이 크므로 러너는 이미 `triangulation.undistort_points = true`.
- **위치·방향(extrinsics)**: 폰을 옮길 때마다. A4 판이 초광각에서 작게 찍혀 바닥 판 방식이 충분한지
  **확인 필요**. 대안은 줄자로 잰 점 10개 이상(scene) 방식.
- 결과를 **리그 폴더** `~/Pose2SimWork/rig/` 에 `Calib.toml`(Pose2Sim 형식, 카메라 순서 cam01, cam02…)
  + `cameras.json` (`{"cam01": "<기기ID>", ...}`) 로 씁니다. 러너가 이걸 읽습니다.
- ★ **cameras.json 은 도구가 자동으로 써야 합니다.** 폰 2대에서는 카메라가 뒤바뀌어도 재투영 오차·배제율에
  전혀 안 나타납니다(실측: 뒤바꾼 3D 가 중앙값 54 cm 틀렸는데 지표는 정상). 사람이 손으로 쓰면 안 됩니다.
- 사용자에게 줄 한국어 촬영 가이드 (몇 m, 몇 초, 어떻게 움직이는지).
- 기기 ID: 폰 1 = `6DC32E3A1F59` (iOS 17.5.1), 폰 2 = `CFA431374C8C` (iOS 17.4.1).

그 다음: 사람을 넣은 실촬영 → 첫 3D → 3D 뷰어(대시보드) → 품질 확인.
보류 중인 사용자 결정: 뼈 길이 SD 5 mm 기준 완화 여부.

## 6. 코드 지도

| 위치 | 내용 |
| --- | --- |
| `ios/App/` | 폰 앱 (SwiftUI). `Capture/CameraController.swift` 카메라 잠금, `Net/RemoteLink.swift` 원격 촬영 |
| `ios/Sources/MocapSyncCore/` | 폰·PC 공통 규칙의 Swift 쪽 (사이드카 검증 등). **Python 쪽과 판정이 같아야 함** |
| `server/master.py` | PC 마스터 (폰 연결, 시계 동기, 녹화 지시, 업로드 수신, 대시보드) |
| `server/webui/`, `server/mocapsync/webui.py` | 대시보드 화면 / 127.0.0.1 전용 웹 서버 |
| `server/mocapsync/sidecar.py` | 사이드카 읽기·검증 (Swift `Sidecar.swift` 와 짝) |
| `server/mocapsync/resample.py` | 키포인트 리샘플러 |
| `server/mocapsync/pipeline.py` | 러너 준비물 (업로드 짝, 리그, 검사, 프로젝트 폴더, 로그 요약) |
| `server/mocapsync/jobs.py`, `sessions_index.py` | 대시보드의 "3D 만들기", 촬영 목록 |
| `server/slave_sim.py` | 가짜 폰 (폰 없이 시험) |
| `tools/run_session.py` | 촬영 한 건 → 3D (종료 코드 0 완료 / 10 캘리브레이션 없음 / 2 입력 문제 / 3 단계 실패) |
| `tools/make_demo_session.py` | 공식 데모를 폰 업로드처럼 꾸밈 (폰 없이 러너 시험) |
| `uploads/<세션>/` | 폰이 올린 영상+사이드카 (git 제외) |
| `~/Pose2SimWork/sessions/<세션>/` | 러너 작업 폴더 (ASCII 경로 필수) |

## 7. 환경과 명령

- 리포 `.venv` (Python 3.13): 마스터·테스트. Pose2Sim 은 **없음**.
- `~/.venv/pose2sim_gpu`: Pose2Sim 0.10.49 + onnxruntime-gpu 1.22 (CUDA 12). 러너는 이걸로.
  재생성 `tools/setup_pose2sim_gpu.ps1`. GPU = RTX 4060 Laptop.
- iOS 빌드: `ios/**` 를 main 에 푸시하면 GitHub Actions 가 서명 없는 IPA 를 만듦. 받기:
  `gh run download <id> -n MocapSync-unsigned-ipa-<번호> -D C:\Users\USER\Desktop\MocapSync_IPA\MocapSync-unsigned-ipa-<번호>`
  (gh: `C:\Program Files\GitHub CLI\gh.exe`)

```powershell
& .venv\Scripts\python.exe -m pytest server\tests -q -p no:cacheprovider
& "$env:USERPROFILE\.venv\pose2sim_gpu\Scripts\python.exe" tools\run_session.py <세션>
.\.venv\Scripts\python.exe server\master.py --no-browser      # 대시보드 http://127.0.0.1:8765
.\.venv\Scripts\python.exe server\slave_sim.py --host 127.0.0.1 --remote --name camA --device-id CAMA00000001 --fake-offset-ms 37.5 --probes 120 --seed 1
```

## 8. 함정 (실제로 물린 것)

- **경로에 한글이 있으면 OpenSim(C++)이 파일을 못 엽니다.** 리포 경로가 한글이라 Pose2Sim 작업은 `~/Pose2SimWork` 에서.
- Pose2Sim 은 캘리브레이션 카메라와 영상을 **이름이 아니라 순서로** 짝짓습니다.
- Pose2Sim 은 `pose-associated` → `pose-sync` → `pose` 순으로 있는 걸 읽습니다. 옛 폴더가 남으면 옛 결과로 3D 를 만듭니다 (러너가 지움).
- GPU: Config 에 `backend='onnxruntime'` **와** `device='CUDA'` 둘 다, 그리고 `onnxruntime.preload_dlls()` 먼저.
- 셸: PowerShell 리다이렉트(`*>`)는 한글이 깨집니다 → 파이썬에서 UTF-8 로 파일에 쓰고 `read_file` 로 읽기.
  명령에 "port"/"host" 글자가 있으면 거부되는 도구가 있어 인자 이름을 `--tcp` 등으로 피했습니다.
  git 커밋 메시지 파일(`-F`)은 방금 만든 파일을 못 찾는 일이 잦음 → `-m` 여러 개로.
- 방금 만든 파일을 도구가 바로 못 보는 경우가 있음 → 몇 초 기다렸다 다시.
- 마스터는 하나만: 포트 9001 이 사용 중이면 새로 안 켜고 대시보드만 엽니다.
- 조명: 실내 ISO 가 이미 상한(3072) 근처. 초광각은 빛이 약 1.8배 더 필요 → 사람 찍을 때 밝게.
- 공유기: `KT_GiGA_5G_7678`(5GHz, 집)이 가장 좋음. 카페 WiFi 는 기기 간 통신이 막힐 수 있음.
