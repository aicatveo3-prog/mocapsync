"""
Pose2Sim 공식 데모(카메라 4대)를 **폰이 올린 촬영처럼** 꾸밉니다.

폰 두 대로 실제 촬영하기 전에 tools/run_session.py 를 처음부터 끝까지 시험하기 위함입니다.

만드는 것
  <작업폴더>/demo_uploads/DEMO-4CAM/<기기ID>.mp4 + <기기ID>.json   (업로드 폴더와 같은 모양)
  <작업폴더>/demo_rig/Calib.toml + cameras.json                   (리그)

★ 기기 ID 를 일부러 cam 순서와 **반대**로 붙입니다 (cam01 = DEMO-CAM-D).
  cameras.json 을 무시하고 ID 순서로 번호를 붙이는 버그가 있으면 카메라가 뒤바뀌고
  재투영 오차가 크게 뛰어서 드러납니다.

인자로 카메라를 고르면 그 카메라만으로 꾸밉니다 (폰 2대 흉내):
    ... make_demo_session.py cam01,cam02     → DEMO-2CAM, demo_rig_2cam

데모 영상에는 프레임 시각이 없으므로, Pose2Sim synchronization 이 찾은 정수 프레임
오프셋(cam03 = -2, cam04 = -1)으로 사이드카 시각을 만듭니다 (demo_resample_check.py 와 같음).

Pose2Sim 환경의 파이썬으로 실행합니다 (영상 프레임 수를 cv2 로 셉니다):
    & "$env:USERPROFILE\\.venv\\pose2sim_gpu\\Scripts\\python.exe" tools\\make_demo_session.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import cv2

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WORK = Path.home() / "Pose2SimWork"
DEMO = WORK / "Demo_SinglePerson"
SID = "DEMO-4CAM"
UP = WORK / "demo_uploads" / SID
RIG = WORK / "demo_rig"

FRAME_OFFSETS = {"cam01": 0, "cam02": 0, "cam03": -2, "cam04": -1}
DEVICE = {"cam01": "DEMO-CAM-D", "cam02": "DEMO-CAM-C", "cam03": "DEMO-CAM-B", "cam04": "DEMO-CAM-A"}

BASE_NS = 1_000_000_000_000          # 부팅 후 1000초 무렵 (가짜)


def count_frames(video: Path) -> tuple[int, int, int, float]:
    cap = cv2.VideoCapture(str(video))
    w, h, fps = int(cap.get(3)), int(cap.get(4)), float(cap.get(5))
    n = 0
    while cap.grab():
        n += 1
    cap.release()
    return n, w, h, fps


def sidecar(cam: str, n: int, w: int, h: int, fps: float) -> dict:
    step = 1e9 / fps
    start = -FRAME_OFFSETS[cam]
    frames = [[i, BASE_NS + int(round((start + i) * step))] for i in range(n)]
    return {
        "schemaVersion": 1, "deviceId": DEVICE[cam], "deviceName": f"demo {cam}",
        "model": "demo", "osVersion": "-", "appVersion": "demo", "sessionId": SID,
        "role": "slave", "clock": "demo", "clockOffsetNs": 0,
        "clockUncertaintyNs": 1_000_000, "clockMinRttNs": 2_000_000,
        "clockMeasuredAtNs": BASE_NS - 1_000_000_000,
        "sleepAtSyncNs": 0, "sleepAtRecordStartNs": 0,
        "timestampSource": "demo", "timestampDomainDeltaNs": 0,
        "targetFps": int(round(fps)), "width": w, "height": h,
        "cameraDeviceType": "demo", "fieldOfViewDeg": 0.0, "isBinned": False,
        "exposureDurationNs": 2_000_000, "iso": 400.0, "lensPosition": 0.0,
        "focusLocked": True, "whiteBalanceLocked": True, "exposureLocked": True,
        "stabilization": "off", "deviceOrientation": "landscapeLeft", "cameraWarnings": [],
        "droppedFrameCount": 0, "thermalAtStart": "nominal", "thermalAtEnd": "nominal",
        "batteryAtStart": 1.0, "batteryAtEnd": 1.0, "frames": frames,
    }


def calib_subset(text: str, keep: list[str]) -> str:
    """
    캘리브레이션에서 keep 카메라만 남기고 cam01, cam02 ... 로 다시 번호를 붙입니다.
    (폰 2대 구성을 흉내 내기 위함. 데모 파일은 [camNN] 섹션이 이어진 단순한 형식입니다)
    """
    sections, cur = [], None
    for line in text.splitlines():
        if line.startswith("["):
            cur = [line.strip("[] \t"), [line]]
            sections.append(cur)
        elif cur is not None:
            cur[1].append(line)
    out = []
    for new_i, old in enumerate(keep):
        body = next(lines for name, lines in sections if name == old)
        new = f"cam{new_i + 1:02d}"
        out += [ln.replace(f"[{old}]", f"[{new}]").replace(f'"{old}"', f'"{new}"') for ln in body]
    out += [ln for name, lines in sections if not name.startswith("cam") for ln in lines]
    return "\n".join(out) + "\n"


def main() -> int:
    global SID, UP, RIG
    cams = [c for c in (sys.argv[1].split(",") if len(sys.argv) > 1 else FRAME_OFFSETS)]
    if any(c not in FRAME_OFFSETS for c in cams):
        print(f"★ 카메라 이름은 {list(FRAME_OFFSETS)} 중에서 고르세요")
        return 2
    if len(cams) != len(FRAME_OFFSETS):
        SID = f"DEMO-{len(cams)}CAM"
        UP = WORK / "demo_uploads" / SID
        RIG = WORK / f"demo_rig_{len(cams)}cam"
    if not DEMO.is_dir():
        print(f"★ 데모가 없습니다: {DEMO}  (tools/pose2sim_demo.py 를 먼저 돌리세요)")
        return 1
    calibs = sorted((DEMO / "calibration").glob("*.toml"))
    if len(calibs) != 1:
        print(f"★ 데모 캘리브레이션 .toml 이 1개가 아닙니다: {calibs}")
        return 1

    shutil.rmtree(UP, ignore_errors=True)
    UP.mkdir(parents=True)
    mapping = {}
    for i, cam in enumerate(cams):
        dev = DEVICE[cam]
        v = DEMO / "videos" / f"{cam}.mp4"
        n, w, h, fps = count_frames(v)
        shutil.copy2(v, UP / f"{dev}.mp4")
        (UP / f"{dev}.json").write_text(json.dumps(sidecar(cam, n, w, h, fps)), encoding="utf-8")
        mapping[f"cam{i + 1:02d}"] = dev
        print(f"  데모 {cam} -> cam{i + 1:02d} = {dev}   {n}프레임  {w}x{h}  {fps:g}fps  "
              f"오프셋 {FRAME_OFFSETS[cam]}")

    shutil.rmtree(RIG, ignore_errors=True)
    RIG.mkdir(parents=True)
    text = calibs[0].read_text(encoding="utf-8")
    (RIG / "Calib.toml").write_text(
        text if cams == list(FRAME_OFFSETS) else calib_subset(text, cams), encoding="utf-8")
    (RIG / "cameras.json").write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    print(f"업로드: {UP}")
    print(f"리그:   {RIG}")
    print("다음:")
    print(f'  & "$env:USERPROFILE\\.venv\\pose2sim_gpu\\Scripts\\python.exe" tools\\run_session.py '
          f'"{UP}" --rig "{RIG}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
