"""
리샘플러 출력이 **실제 Pose2Sim 3D 파이프라인**에서 동작하는지 공식 데모로 확인합니다.

★ 왜 데모인가
카메라 4대짜리 공식 데모(Demo_SinglePerson)는 이미 한 번 끝까지 돌려 결과가 있습니다.
폰 2대가 없어도 "우리 pose-sync/ 가 Pose2Sim 삼각측량에 그대로 먹히는가"를 볼 수 있습니다.

방법
  1. 데모를 복사합니다 (원본은 건드리지 않음)
  2. 데모 영상에는 타임스탬프가 없으므로, Pose2Sim synchronization 이 찾은
     정수 프레임 오프셋(cam01/02 = 0, cam03 = -2, cam04 = -1)으로 사이드카를 만듭니다
  3. 리샘플러로 pose-sync/ 를 만들고, synchronization 을 건너뛴 채
     personAssociation -> triangulation -> filtering 을 돌립니다
  4. 원래 결과와 재투영 오차를 비교합니다

  ★ 오프셋이 정수 프레임이므로 이 시험은 "보간"이 아니라 "배관"을 봅니다.
    결과가 원본과 거의 같아야 정상입니다. 서브프레임 이득은 실기기 두 대가
    있어야 잴 수 있습니다 (데모 영상에는 프레임 시각이 없음).

Pose2Sim 환경의 파이썬으로 실행합니다:
    ~/.venv/pose2sim/Scripts/python.exe tools/demo_resample_check.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from mocapsync import resample as R  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WORK = Path.home() / "Pose2SimWork"
SRC = WORK / "Demo_SinglePerson"
DST = WORK / "Demo_Resampled"
FPS = 60
FRAME_NS = 1_000_000_000 / FPS

#: Pose2Sim synchronization 이 데모에서 찾은 값 (logs.txt).
#: Pose2Sim 규칙: 새 번호 = 옛 번호 - offset. cam03 = -2 는 cam03 의 0번이
#: cam01 의 2번과 같은 순간이라는 뜻 = cam03 이 2프레임 늦게 시작.
FRAME_OFFSETS = {"cam01": 0, "cam02": 0, "cam03": -2, "cam04": -1}


def make_sidecar(cam: str, n_frames: int) -> Path:
    start = -FRAME_OFFSETS[cam]            # 공통 시간축에서 몇 프레임 늦게 시작했나
    frames = [[i, int(round((start + i) * FRAME_NS))] for i in range(n_frames)]
    side = {
        "schemaVersion": 1, "deviceId": f"DEMO_{cam}", "sessionId": "DEMO",
        "clock": "demo", "clockOffsetNs": 0, "clockUncertaintyNs": 1,
        "targetFps": FPS, "stabilization": "off", "frames": frames,
    }
    p = DST / "sidecars" / f"{cam}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(side), encoding="utf-8")
    return p


def reproj_summary(logs: Path) -> list[str]:
    if not logs.exists():
        return []
    txt = logs.read_text(encoding="utf-8", errors="replace")
    out = []
    for pat in (r"Mean reprojection error for all points on [^\n]*",
                r"Mean reprojection error for Neck point on all frames is [^\n]*",
                r"In average, [\d.]+ cameras had to be excluded[^\n]*"):
        m = re.findall(pat, txt)
        if m:
            out.append(m[-1])
    # 카메라별 제외 비율 (엉뚱한 사람을 고르면 여기서 100% 가 나옵니다)
    out += list(dict.fromkeys(re.findall(r"Camera \S+ was excluded [^\n]*", txt)))
    return out


def main() -> int:
    if not SRC.exists():
        print(f"★ 데모가 없습니다: {SRC}  (tools/pose2sim_demo.py 를 먼저 돌리세요)")
        return 1

    if DST.exists():
        shutil.rmtree(DST)
    shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(
        "pose-sync", "pose-associated", "pose-3d", "kinematics", "logs.txt", "opensim.log"))
    print(f"복사: {DST}")

    # 데모 Config 는 filtering 그래프를 창으로 띄워(display_figures = true) 창을 닫을 때까지
    # 멈춥니다. 복사본에서만 끕니다 (그래프 파일 저장은 그대로).
    cfg = DST / "Config.toml"
    cfg.write_text(re.sub(r"(?m)^(\s*display_figures\s*=\s*)true", r"\1false",
                          cfg.read_text(encoding="utf-8")), encoding="utf-8")

    pairs = []
    for cam in FRAME_OFFSETS:
        jd = DST / "pose" / f"{cam}_json"
        n = len(list(jd.glob("*.json")))
        pairs.append((jd, make_sidecar(cam, n)))

    res = R.resample_session(pairs, DST / "pose-sync")
    rep = res.report()
    print(f"리샘플: 격자 {rep['gridPoints']}점")
    for c in rep["cameras"]:
        print(f"  {c['name']}  슬롯 {c['slots']}  사람 {c['person_ratio'] * 100:.0f}%  "
              f"주 피험자 채움 {c['main_filled_ratio'] * 100:.0f}%  "
              f"튀어서 끊음 {c['jump_cuts']}회  "
              f"가장 가까운 프레임까지 최대 {c['shift_ms_max']:.3f} ms")

    # Pose2Sim 은 현재 폴더의 Config.toml 을 읽습니다.
    os.chdir(DST)
    from Pose2Sim import Pose2Sim
    Pose2Sim.personAssociation()
    Pose2Sim.triangulation()
    Pose2Sim.filtering()

    print()
    print("── 재투영 오차 비교 ──")
    print("원본 (Pose2Sim synchronization):")
    for line in reproj_summary(SRC / "logs.txt"):
        print("  " + line)
    print("리샘플러 (synchronization 건너뜀):")
    for line in reproj_summary(DST / "logs.txt"):
        print("  " + line)
    trcs = sorted((DST / "pose-3d").glob("*.trc"))
    print("생성된 TRC:", ", ".join(t.name for t in trcs) or "없음")
    return 0 if trcs else 4


if __name__ == "__main__":
    raise SystemExit(main())
