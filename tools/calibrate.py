"""
캘리브레이션 — 렌즈 특성(폰마다 한 번)과 위치·방향(폰을 놓을 때마다) → 리그.

  1) 렌즈 특성: 체커보드를 손에 들고 각 폰 앞에서 천천히 (docs/CALIBRATION.md)
       python tools\\calibrate.py intrinsics <세션>
     → ~/Pose2SimWork/intrinsics/<기기ID>.json

  2) 위치·방향: 폰을 삼각대에 고정하고 두 폰 동시 녹화, 판을 두 폰이 함께 보게
     돌아다니다가 마지막에 바닥에 5초
       python tools\\calibrate.py extrinsics <세션>
     → ~/Pose2SimWork/rig/  Calib.toml + cameras.json (+ check_camNN.jpg, rig_report.json)
     이후 tools\\run_session.py 가 이 리그로 3D 를 만듭니다.

  지금 상태 보기:
       python tools\\calibrate.py status

<세션> 은 uploads/ 아래 이름(예: S20261001-101500) 이나 폴더 경로입니다.
파이썬은 리포 .venv 나 Pose2Sim 환경 어느 쪽이든 됩니다 (OpenCV·SciPy 필요):
    & "$env:USERPROFILE\\.venv\\pose2sim_gpu\\Scripts\\python.exe" tools\\calibrate.py status

종료 코드: 0 = 저장함, 2 = 입력 문제(고쳐야 진행 가능), 4 = 품질 부족으로 저장 안 함
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))

from mocapsync import calib as C  # noqa: E402
from mocapsync import calib_session as CS  # noqa: E402
from mocapsync import pipeline as PL  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BAR = "=" * 72
EXIT_OK, EXIT_INPUT, EXIT_QUALITY = 0, 2, 4


class Tee:
    """화면과 파일에 같이 씁니다."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.fh = open(path, "w", encoding="utf-8")

    def __call__(self, msg: str = "") -> None:
        print(msg, flush=True)
        self.fh.write(msg + "\n")
        self.fh.flush()

    def close(self) -> None:
        self.fh.close()


def resolve_session(arg: str) -> Path:
    p = Path(arg)
    if p.is_dir():
        return p.resolve()
    q = REPO / "uploads" / arg
    if q.is_dir():
        return q
    raise C.CalibError(f"촬영 폴더를 찾을 수 없습니다: {arg}  (uploads/ 아래 이름 또는 경로)")


def board_from(a) -> C.Board:
    try:
        cols, rows = (int(x) for x in a.board.lower().split("x"))
    except ValueError:
        raise C.CalibError(f"--board 는 '6x9' 처럼 안쪽 꼭짓점 수로 적습니다: {a.board}") from None
    return C.Board(cols=cols, rows=rows, square_m=a.square_mm / 1000.0)


def cmd_status(a) -> int:
    root = a.work_root
    idir = root / CS.INTRINSICS_DIR
    print(BAR)
    print(" 캘리브레이션 상태")
    print(BAR)
    print(f"\n 렌즈 특성 ({idir})")
    recs = sorted(idir.glob("*.json")) if idir.is_dir() else []
    if not recs:
        print("   없음 — 'intrinsics' 를 먼저 하세요.")
    for p in recs:
        r = json.loads(p.read_text(encoding="utf-8"))
        print(f"   {r['deviceId']} ({r.get('deviceName') or '?'})  {r.get('created')}  "
              f"오차 {r.get('rmsPx')} px  판정 {r.get('verdict')}  "
              f"{str(r.get('cameraDeviceType', '')).replace('AVCaptureDeviceType', '')} "
              f"{r.get('width')}x{r.get('height')}@{r.get('targetFps')}")
    rig_dir = root / CS.RIG_DIR
    print(f"\n 리그 ({rig_dir})")
    try:
        rig = PL.load_rig(rig_dir)
    except PL.PipelineError as e:
        print(f"   ★ {e}")
        return EXIT_INPUT
    if rig is None:
        print("   없음 — 'extrinsics' 를 하면 생깁니다.")
        return EXIT_OK
    rep_p = rig_dir / "rig_report.json"
    rep = json.loads(rep_p.read_text(encoding="utf-8")) if rep_p.is_file() else {}
    print(f"   {rig.calib_path.name}  만든 시각 {rep.get('created', '?')}  "
          f"촬영 {rep.get('session', '?')}  판정 {rep.get('verdict', '?')}  오차 {rep.get('rmsPx', '?')} px")
    for cam, dev in rig.device_of.items():
        c = (rep.get("cameras") or {}).get(cam, {})
        print(f"   {cam} = {dev} ({c.get('deviceName') or '?'})  높이 {c.get('heightM', '?')} m")
    for k, v in (rep.get("distancesM") or {}).items():
        print(f"   {k} 사이 거리 {v} m")
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description="캘리브레이션 (렌즈 특성 / 위치·방향 → 리그)")
    ap.add_argument("--work-root", type=Path, default=PL.DEFAULT_WORK_ROOT,
                    help=f"작업 폴더 (기본 {PL.DEFAULT_WORK_ROOT})")
    ap.add_argument("--board", default="6x9", help="안쪽 꼭짓점 수 가로x세로 (기본 6x9 = A4 판)")
    ap.add_argument("--square-mm", type=float, default=23.0,
                    help="한 칸 크기 mm (기본 23 — 인쇄한 판을 자로 재서 다르면 고치세요)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("intrinsics", help="렌즈 특성 (폰마다 한 번)")
    pi.add_argument("session")
    pi.add_argument("--only", nargs="+", metavar="기기ID", help="이 기기만")
    pi.add_argument("--force", action="store_true", help="판정이 부족해도 저장")
    pi.add_argument("--rational", action="store_true", help="왜곡 계수 8개 모델")
    pi.add_argument("--max-speed", type=float, default=C.INTR_MAX_SPEED,
                    help=f"쓸 장면의 최대 판 속도 px/초 (기본 {C.INTR_MAX_SPEED:g})")

    pe = sub.add_parser("extrinsics", help="위치·방향 (폰을 놓을 때마다) → 리그")
    pe.add_argument("session")
    pe.add_argument("--force", action="store_true", help="판정이 부족해도 리그를 씀")
    pe.add_argument("--order", nargs="+", metavar="기기ID",
                    help="cam01, cam02 ... 순서 (기본: 기기 ID 순서)")
    pe.add_argument("--rate", type=float, default=8.0, help="1초에 몇 순간을 볼지 (기본 8)")
    pe.add_argument("--max-speed", type=float, default=C.EXTR_MAX_SPEED,
                    help=f"쓸 장면의 최대 판 속도 px/초 (기본 {C.EXTR_MAX_SPEED:g})")
    pe.add_argument("--no-floor", action="store_true", help="바닥 판을 찾지 않음")

    sub.add_parser("status", help="저장된 렌즈 특성과 현재 리그")
    a = ap.parse_args()

    if a.cmd == "status":
        return cmd_status(a)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        board = board_from(a)
        sess = resolve_session(a.session)
    except C.CalibError as e:
        print(f"★ {e}")
        return EXIT_INPUT
    log = Tee(REPO / "logs" / f"calibrate_{a.cmd}_{sess.name}_{stamp}.log")
    title = {"intrinsics": "렌즈 특성", "extrinsics": "위치·방향 → 리그"}[a.cmd]
    log(BAR)
    log(f" 캘리브레이션: {title}   촬영 {sess.name}   {datetime.now():%Y-%m-%d %H:%M:%S}")
    log(f" 판: 안쪽 꼭짓점 {board.cols}x{board.rows}, 한 칸 {board.square_m * 1000:g} mm")
    log(BAR)
    code = EXIT_OK
    try:
        if a.cmd == "intrinsics":
            outs = CS.run_intrinsics(sess, a.work_root, board, log=log, only=a.only,
                                     force=a.force, rational=a.rational,
                                     max_speed=a.max_speed, stamp=stamp)
            log("")
            log(BAR)
            for o in outs:
                log(f" {o.device_id}: {CS._fmt_level(o.level)}  "
                    + (f"→ 저장 {o.saved}" if o.saved else "→ 저장 안 함"))
            if any(o.saved is None for o in outs):
                code = EXIT_QUALITY
            if outs and all(o.saved for o in outs):
                log(" 다음: 폰을 삼각대에 놓고 위치·방향 촬영 → "
                    "python tools\\calibrate.py extrinsics <세션>")
        else:
            oc = CS.run_extrinsics(sess, a.work_root, board, log=log, force=a.force,
                                   order=a.order, rate_hz=a.rate, max_speed=a.max_speed,
                                   use_floor=not a.no_floor, stamp=stamp)
            log("")
            log(BAR)
            if oc.rig_dir is None:
                code = EXIT_QUALITY
                log(" 리그를 쓰지 않았습니다.")
            else:
                log(f" 리그: {oc.rig_dir}  ({CS._fmt_level(oc.level)})")
                log(" 다음: 사람을 넣고 촬영 → run_session.py 가 이 리그로 3D 를 만듭니다.")
    except (C.CalibError, PL.PipelineError) as e:
        log(f" ★ {e}")
        code = EXIT_INPUT
    log(f" 이 실행 로그: {log.path}")
    log(BAR)
    log.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
