#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Pose2Sim 데모 러너 (0단계).

목적
----
"Pose2Sim이 이 PC에서 실제로 돌아가서 .trc 가 나오는가"를 한 번에 확인합니다.
성공/실패를 눈으로 판정할 수 있게, 마지막에 산출물 검증 리포트를 찍습니다.

왜 이 스크립트가 필요한가
------------------------
공식 데모는 ipython 에서 8줄을 직접 치는 방식입니다. 그런데 기본 Config.toml 은
  display_detection = true    (실시간 OpenCV 창)
  synchronization_gui = true  (사람이 클릭해야 넘어가는 GUI)
  display_figures = true      (matplotlib 창)
이라서 중간에 멈춥니다. 처음 한 번은 사람 개입 없이 끝까지 돌려보고
"되는구나"를 확인하는 게 낫습니다. 그래서 headless 모드를 기본으로 둡니다.

Config.toml 은 건드리지 않습니다.
Pose2Sim 은 config 인자로 부분 dict 를 받으면 <project_dir>/Config.toml 을 베이스로
재귀 병합합니다(Pose2Sim.read_config_files 소스에서 확인). 그래서 원본 설정 파일은
사람이 읽고 배우는 용도로 그대로 남겨둡니다.

사용법
------
  # 1) 데모 폴더만 복사 (아무것도 실행 안 함)
  python tools/pose2sim_demo.py --setup-only

  # 2) 전체 파이프라인 자동 실행 (권장 첫 실행)
  python tools/pose2sim_demo.py

  # 3) 공식 문서와 똑같이, 창/GUI 다 띄우면서 실행
  python tools/pose2sim_demo.py --mode interactive

  # 4) 정확도 우선 + GPU
  python tools/pose2sim_demo.py --pose-mode performance --device CUDA

  # 5) 특정 단계만
  python tools/pose2sim_demo.py --stages triangulation,filtering,kinematics
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import inspect
import os
import platform
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# 한국어 출력이 cp949 콘솔에서 깨지지 않도록
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent


def is_ascii_path(p: Path) -> bool:
    return str(p).isascii()


# ─────────────────────────────────────────────────────────────────────────────
# ★ 실측으로 확인한 제약 (2026-09-17)
#
# OpenSim 4.6 의 C++ 코어는 Windows 에서 경로에 non-ASCII 문자가 있으면 파일을
# 열지 못합니다. std::string 기반이라 UTF-8/와이드 API 를 쓰지 않기 때문입니다.
#
#   RuntimeError: std::exception in 'OpenSim::ScaleTool::ScaleTool(...)':
#   Object: Cannot open file ...\모션캡쳐_Pose2Sim\...\..._scaling_setup.xml.
#   It may not exist or you do not have permission to read it.
#
# 확인 방법: 그 XML 파일은 43.6KB 로 실제로 존재했습니다. Python 이 쓴 파일을
# OpenSim 이 못 읽은 것입니다. 같은 프로젝트를 ASCII 경로(C:\p2s_test)로 복사해서
# kinematics 만 다시 돌리면 .mot / .osim 이 정상 생성됩니다.
#
# 주의: 디렉터리 정션(mklink /J)으로는 우회되지 않습니다. Pose2Sim 이 경로를
# resolve() 해서 원래의 non-ASCII 경로로 되돌립니다.
#
# 그래서 이 리포지토리 경로가 non-ASCII 면 작업 폴더를 홈 디렉터리 아래
# ASCII 경로로 자동 이동시킵니다. 코드는 리포에, 데이터는 ASCII 경로에.
# (데이터 폴더는 어차피 .gitignore 대상입니다)
# ─────────────────────────────────────────────────────────────────────────────
if is_ascii_path(REPO_ROOT):
    DEFAULT_WORKDIR = REPO_ROOT / "pose2sim_projects"
else:
    DEFAULT_WORKDIR = Path.home() / "Pose2SimWork"

DEFAULT_PROJECT = DEFAULT_WORKDIR / "Demo_SinglePerson"

STAGE_ORDER = [
    "calibration",
    "poseEstimation",
    "synchronization",
    "personAssociation",
    "triangulation",
    "filtering",
    "markerAugmentation",
    "kinematics",
]

# 각 단계가 무엇을 하는지 (실행 중에 화면에 같이 찍습니다)
STAGE_DESC = {
    "calibration": "카메라 캘리브레이션. 데모는 Qualisys .qca.txt 를 Calib.toml 로 변환합니다.",
    "poseEstimation": "RTMPose 로 영상마다 2D 키포인트 검출 -> pose/ 에 OpenPose 형식 json.",
    "synchronization": "키포인트 수직 속도의 상호상관으로 카메라 간 시간차 보정 -> pose-sync/.",
    "personAssociation": "여러 시점에서 같은 사람을 짝지음 (단일 인물이면 재투영오차 최소 인물 선택).",
    "triangulation": "2D -> 3D 삼각측량. 신뢰도 가중 + 재투영오차 기반 카메라 배제 -> pose-3d/*.trc.",
    "filtering": "3D 좌표 필터링 (기본 Butterworth 6Hz).",
    "markerAugmentation": "Stanford LSTM 으로 가상 마커 47개 추정. 카메라 4대 미만일 때 특히 도움.",
    "kinematics": "OpenSim 모델 스케일링 + 역운동학 -> kinematics/*.mot (관절각).",
}

BAR = "=" * 74
SUB = "-" * 74


# ─────────────────────────────────────────────────────────────────────────────
# 로그: 화면과 파일에 동시 출력
# ─────────────────────────────────────────────────────────────────────────────
class Tee:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(path, "w", encoding="utf-8")

    def __call__(self, msg: str = ""):
        print(msg, flush=True)
        self.fh.write(msg + "\n")
        self.fh.flush()

    def close(self):
        self.fh.close()


# ─────────────────────────────────────────────────────────────────────────────
# 데모 폴더 준비
# ─────────────────────────────────────────────────────────────────────────────
def find_package_dir() -> Path:
    import Pose2Sim

    return Path(inspect.getfile(Pose2Sim)).resolve().parent


def setup_project(project: Path, demo_name: str, log) -> Path:
    pkg = find_package_dir()
    src = pkg / demo_name
    if not src.is_dir():
        raise FileNotFoundError(
            f"설치된 Pose2Sim 안에 {demo_name} 가 없습니다: {src}\n"
            f"  있는 폴더: {[d.name for d in pkg.iterdir() if d.name.startswith('Demo')]}"
        )

    if project.exists():
        log(f"  프로젝트 폴더가 이미 있습니다 (재사용): {project}")
    else:
        log(f"  복사 중: {src}")
        log(f"       -> {project}")
        project.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, project)
        log("  복사 완료.")

    cfg = project / "Config.toml"
    if not cfg.is_file():
        raise FileNotFoundError(f"Config.toml 이 없습니다: {cfg}")

    vids = sorted((project / "videos").glob("*.*"))
    log(f"  영상 {len(vids)}개: " + ", ".join(v.name for v in vids))
    return project


# ─────────────────────────────────────────────────────────────────────────────
# config 오버라이드
# ─────────────────────────────────────────────────────────────────────────────
def build_config(project: Path, args) -> dict:
    """
    Config.toml 을 베이스로 병합될 '부분 dict' 를 만듭니다.
    여기 없는 키는 모두 Config.toml 값이 그대로 쓰입니다.
    """
    cfg: dict = {
        "project": {"project_dir": str(project)},
        "pose": {
            "mode": args.pose_mode,
            "device": args.device,
        },
    }

    if args.mode == "headless":
        # 사람 개입 없이 끝까지 돌리기 위한 설정
        cfg["pose"].update(
            {
                "display_detection": False,       # 실시간 창 끔 (병렬 처리의 전제조건)
                "parallel_workers_pose": "auto",  # 영상당 워커 1개
            }
        )
        cfg["synchronization"] = {
            "synchronization_gui": False,  # ★ 이게 true면 클릭을 기다리며 멈춥니다
            "display_sync_plots": False,
            "save_sync_plots": True,       # 파일로는 저장 (나중에 확인 가능)
        }
        cfg["filtering"] = {
            "display_figures": False,
            "save_filt_plots": True,
        }
        cfg["calibration"] = {
            "calculate": {
                "intrinsics": {"show_detection_intrinsics": False},
                "extrinsics": {"show_reprojection_error": False},
            }
        }

    if args.overwrite_pose:
        cfg["pose"]["overwrite_pose"] = True

    if args.simple_model:
        cfg["kinematics"] = {"use_simple_model": True}

    if args.frame_range:
        lo, hi = (int(x) for x in args.frame_range.split(","))
        cfg["project"]["frame_range"] = [lo, hi]

    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# 산출물 검증
# ─────────────────────────────────────────────────────────────────────────────
def parse_trc(path: Path) -> dict:
    """TRC 헤더와 데이터 통계를 읽습니다. OpenSim TRC 는 5줄 헤더 + 데이터."""
    info: dict = {"path": path}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()

    if len(lines) < 6:
        info["error"] = "파일이 너무 짧습니다 (헤더만 있고 데이터가 없음)"
        return info

    # 3번째 줄에 DataRate, CameraRate, NumFrames, NumMarkers, Units ...
    keys = lines[1].split("\t")
    vals = lines[2].split("\t")
    header = dict(zip([k.strip() for k in keys], [v.strip() for v in vals]))
    info["DataRate"] = header.get("DataRate")
    info["NumFrames"] = header.get("NumFrames")
    info["NumMarkers"] = header.get("NumMarkers")
    info["Units"] = header.get("Units")

    markers = [m for m in lines[3].split("\t")[2:] if m.strip()]
    info["markers"] = markers

    data = [ln for ln in lines[5:] if ln.strip()]
    info["data_rows"] = len(data)

    # NaN 비율 = 삼각측량이 실패한 좌표 비율
    total = 0
    nan = 0
    for ln in data:
        for tok in ln.split("\t")[2:]:
            tok = tok.strip()
            if not tok:
                continue
            total += 1
            if tok.lower() in ("nan", "-nan", "1.#qnan", "inf", "-inf"):
                nan += 1
    info["values"] = total
    info["nan"] = nan
    info["nan_pct"] = (100.0 * nan / total) if total else 0.0
    return info


def verify(project: Path, log) -> bool:
    log()
    log(BAR)
    log(" 산출물 검증")
    log(BAR)

    ok = True

    def check(label: str, cond: bool, detail: str = "") -> bool:
        nonlocal ok
        mark = "[ OK ]" if cond else "[FAIL]"
        log(f" {mark} {label}" + (f"   {detail}" if detail else ""))
        if not cond:
            ok = False
        return cond

    # 1) 캘리브레이션
    calib = sorted((project / "calibration").glob("Calib*.toml"))
    check("캘리브레이션 파일 (calibration/Calib*.toml)", bool(calib),
          calib[0].name if calib else "없음")

    # 2) 2D 키포인트
    pose_dir = project / "pose"
    cam_dirs = sorted([d for d in pose_dir.glob("*") if d.is_dir()]) if pose_dir.is_dir() else []
    json_counts = {d.name: len(list(d.glob("*.json"))) for d in cam_dirs}
    check("2D 키포인트 (pose/*/*.json)", bool(json_counts) and all(v > 0 for v in json_counts.values()),
          ", ".join(f"{k}={v}개" for k, v in json_counts.items()) if json_counts else "없음")

    # 3) 동기화 결과 (건너뛰었으면 없을 수 있음)
    sync_dir = project / "pose-sync"
    if sync_dir.is_dir():
        sync_counts = {d.name: len(list(d.glob("*.json"))) for d in sorted(sync_dir.glob("*")) if d.is_dir()}
        log(f" [info] 동기화 결과 (pose-sync/): " +
            (", ".join(f"{k}={v}개" for k, v in sync_counts.items()) if sync_counts else "비어 있음"))

    # 4) ★ 3D 좌표 — 0단계의 핵심 성공 조건
    trc_dir = project / "pose-3d"
    trcs = sorted(trc_dir.glob("*.trc")) if trc_dir.is_dir() else []
    got_trc = check("★ 3D 마커 궤적 (pose-3d/*.trc)", bool(trcs), f"{len(trcs)}개")

    if got_trc:
        for t in trcs:
            i = parse_trc(t)
            if "error" in i:
                log(f"        - {t.name}: {i['error']}")
                ok = False
                continue
            log(f"        - {t.name}")
            log(f"            프레임 {i['NumFrames']}  마커 {i['NumMarkers']}개  "
                f"{i['DataRate']} Hz  단위 {i['Units']}")
            log(f"            데이터 행 {i['data_rows']}  결측(NaN) {i['nan']}/{i['values']} "
                f"= {i['nan_pct']:.2f}%")
            if i["markers"]:
                log(f"            마커 예: {', '.join(i['markers'][:8])} ...")

    # 5) c3d
    c3ds = sorted(trc_dir.glob("*.c3d")) if trc_dir.is_dir() else []
    log(f" [info] c3d 파일: {len(c3ds)}개")

    # 6) OpenSim 결과
    kin = project / "kinematics"
    osims = sorted(kin.glob("*.osim")) if kin.is_dir() else []
    mots = sorted(kin.glob("*.mot")) if kin.is_dir() else []
    check("스케일된 OpenSim 모델 (kinematics/*.osim)", bool(osims), f"{len(osims)}개")
    check("관절각 (kinematics/*.mot)", bool(mots),
          ", ".join(m.name for m in mots) if mots else "없음")

    # 7) 품질 지표 — 나중에 5단계 품질 대시보드가 파싱할 대상
    logs = project / "logs.txt"
    if logs.is_file():
        txt = logs.read_text(encoding="utf-8", errors="replace")
        log()
        log(" 품질 지표 (logs.txt 에서 추출)")
        hits = 0
        for line in txt.splitlines():
            low = line.lower()
            if any(k in low for k in ("mean reprojection error", "reprojection error",
                                      "excluded", "interpolated", "residual")):
                s = line.strip()
                if s:
                    log(f"   | {s[:150]}")
                    hits += 1
            if hits >= 25:
                log("   | ... (이하 생략, 전체는 logs.txt 참고)")
                break
        if hits == 0:
            log("   | (해당 키워드를 못 찾았습니다. logs.txt 를 직접 보세요)")
    else:
        log(" [info] logs.txt 가 없습니다.")

    log()
    log(BAR)
    if ok:
        log(" 결과: 성공.  0단계 통과.")
        log(f" .trc 위치: {trc_dir}")
        log(" 이 파일이 나중에 웹 3D 뷰어(5단계)가 재생할 바로 그 형식입니다.")
    else:
        log(" 결과: 실패한 항목이 있습니다. 위의 [FAIL] 을 확인하세요.")
        log(f" 상세 로그: {project / 'logs.txt'}")
    log(BAR)
    return ok


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Pose2Sim 데모 러너 (0단계)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--project", type=Path, default=DEFAULT_PROJECT,
                    help=f"작업 폴더 (기본: {DEFAULT_PROJECT})")
    ap.add_argument("--demo", default="Demo_SinglePerson",
                    choices=["Demo_SinglePerson", "Demo_MultiPerson", "Demo_Batch"],
                    help="복사해올 데모 (기본: Demo_SinglePerson)")
    ap.add_argument("--mode", default="headless", choices=["headless", "interactive"],
                    help="headless=창/GUI 없이 끝까지 자동 실행(기본), interactive=공식 문서와 동일")
    ap.add_argument("--pose-mode", default="balanced",
                    choices=["lightweight", "balanced", "performance"],
                    help="RTMPose 정확도/속도 (기본: balanced)")
    ap.add_argument("--device", default="auto", choices=["auto", "CPU", "CUDA", "MPS", "ROCM"],
                    help="추론 장치 (기본: auto)")
    ap.add_argument("--stages", default="",
                    help="쉼표로 구분한 실행 단계. 비우면 전체. 예: triangulation,filtering")
    ap.add_argument("--skip", default="",
                    help="건너뛸 단계. 예: synchronization,markerAugmentation")
    ap.add_argument("--frame-range", default="",
                    help="프레임 범위 제한. 예: 0,100 (빠른 테스트용)")
    ap.add_argument("--overwrite-pose", action="store_true",
                    help="이미 있는 2D 검출 결과를 무시하고 다시 검출")
    ap.add_argument("--simple-model", action="store_true",
                    help="OpenSim 단순 모델 사용 (역운동학 10배 이상 빠름)")
    ap.add_argument("--setup-only", action="store_true",
                    help="데모 폴더만 복사하고 종료")
    args = ap.parse_args()

    project = args.project.resolve()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # 로그는 리포 안에 둡니다 (경로에 한글이 있어도 Python 은 문제없습니다)
    log_path = REPO_ROOT / "logs" / f"pose2sim_demo_{stamp}.log"
    log = Tee(log_path)

    try:
        log(BAR)
        log(" Pose2Sim 데모 러너 (0단계)")
        log(f" 시작 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log(BAR)

        # 환경 정보 (문제 생겼을 때 이 블록만 봐도 원인 추적이 됩니다)
        log()
        log(" [환경]")
        log(f"   python           = {sys.version.split()[0]}  ({sys.executable})")
        log(f"   platform         = {platform.platform()}")
        try:
            log(f"   pose2sim         = {md.version('pose2sim')}")
        except Exception as e:
            log(f"   pose2sim         = 버전 조회 실패: {e}")
        for pkg in ("onnxruntime", "onnxruntime-gpu", "opensim", "rtmlib", "openvino",
                    "opencv-python", "numpy", "scipy"):
            try:
                log(f"   {pkg:<16} = {md.version(pkg)}")
            except Exception:
                pass
        try:
            import onnxruntime as ort
            log(f"   ORT providers    = {ort.get_available_providers()}")
        except Exception as e:
            log(f"   ORT providers    = 조회 실패: {e}")
        log(f"   cpu_count        = {os.cpu_count()}")

        # ── 경로 가드 ──────────────────────────────────────────────────────
        # OpenSim(C++) 은 non-ASCII 경로를 못 읽습니다. 여기서 미리 잡아야
        # 파이프라인 7단계를 다 돌린 뒤 마지막에 실패하는 낭비를 막습니다.
        log()
        log(" [경로 점검]")
        log(f"   리포지토리   = {REPO_ROOT}   (ASCII: {is_ascii_path(REPO_ROOT)})")
        log(f"   작업 폴더    = {project}   (ASCII: {is_ascii_path(project)})")
        if not is_ascii_path(project):
            log()
            log("   [!] 작업 폴더 경로에 ASCII 가 아닌 문자가 있습니다.")
            log("       OpenSim 4.6 의 C++ 코어는 이런 경로의 파일을 열지 못합니다.")
            log("       calibration ~ filtering 까지는 되지만 kinematics 에서")
            log("       'Cannot open file ..._scaling_setup.xml' 로 실패합니다.")
            log("       (디렉터리 정션으로도 우회되지 않습니다. Pose2Sim 이 resolve() 합니다)")
            log()
            log("       해결: ASCII 경로를 --project 로 지정하세요. 예:")
            log(f"         --project {Path.home() / 'Pose2SimWork' / args.demo}")
            if "kinematics" not in {s.strip() for s in args.skip.split(",")}:
                log()
                log("   kinematics 를 건너뛰고 계속하려면 --skip kinematics 를 붙이세요.")
                log("   여기서 중단합니다.")
                return 4

        # 프로젝트 준비
        log()
        log(" [프로젝트 준비]")
        project = setup_project(project, args.demo, log)
        if args.setup_only:
            log()
            log(f" --setup-only 이므로 여기서 종료합니다. 폴더: {project}")
            return 0

        # 실행할 단계 결정
        stages = [s.strip() for s in args.stages.split(",") if s.strip()] or list(STAGE_ORDER)
        skip = {s.strip() for s in args.skip.split(",") if s.strip()}
        stages = [s for s in stages if s not in skip]
        unknown = [s for s in stages if s not in STAGE_ORDER]
        if unknown:
            log(f" 알 수 없는 단계: {unknown}")
            log(f" 가능한 값: {STAGE_ORDER}")
            return 2

        config = build_config(project, args)
        log()
        log(" [설정 오버라이드] (아래 키만 덮어쓰고, 나머지는 Config.toml 그대로)")
        for section, vals in config.items():
            log(f"   [{section}]")
            for k, v in vals.items():
                log(f"     {k} = {v!r}")
        if args.mode == "headless":
            log("   * headless 모드: 실시간 창과 동기화 GUI를 껐습니다.")
            log("     공식 문서와 똑같이 창을 보려면 --mode interactive 로 실행하세요.")

        # 실행
        from Pose2Sim import Pose2Sim as P

        cwd0 = Path.cwd()
        os.chdir(project)  # Pose2Sim 일부 경로가 cwd 기준이라 안전하게 이동
        results = []
        try:
            for name in stages:
                fn = getattr(P, name)
                log()
                log(SUB)
                log(f" >> {name}")
                log(f"    {STAGE_DESC.get(name, '')}")
                log(SUB)
                t0 = time.time()
                try:
                    fn(config)
                    dt = time.time() - t0
                    results.append((name, True, dt, ""))
                    log(f"    -> 완료 ({dt:.1f}초)")
                except Exception as e:
                    dt = time.time() - t0
                    tb = traceback.format_exc()
                    results.append((name, False, dt, f"{type(e).__name__}: {e}"))
                    log(f"    -> 실패 ({dt:.1f}초): {type(e).__name__}: {e}")
                    log("    --- 트레이스백 ---")
                    for ln in tb.splitlines():
                        log("    " + ln)
                    log("    ------------------")
                    if name in ("calibration", "poseEstimation", "triangulation"):
                        log("    이 단계는 뒤 단계의 전제조건이라 여기서 중단합니다.")
                        break
        finally:
            os.chdir(cwd0)

        # 단계 요약
        log()
        log(BAR)
        log(" 단계별 결과")
        log(BAR)
        total = 0.0
        for name, good, dt, err in results:
            total += dt
            log(f" {'OK  ' if good else 'FAIL'}  {name:<20} {dt:7.1f}초  {err}")
        log(f" 합계 {total:.1f}초")

        ok = verify(project, log)

        log()
        log(f" 이 실행의 전체 로그: {log_path}")
        log(" 문제가 있으면 이 파일을 그대로 붙여주세요.")
        return 0 if ok else 1

    except Exception:
        log()
        log(" 예상치 못한 오류:")
        for ln in traceback.format_exc().splitlines():
            log("   " + ln)
        log()
        log(f" 로그: {log_path}")
        return 3
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
