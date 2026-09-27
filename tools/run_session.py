"""
촬영 한 건을 3D 결과까지 한 번에 돌립니다 (4단계 파이프라인 러너).

  uploads/<세션>/ (폰이 올린 영상 + 사이드카)
    1. 입력 찾기        영상·사이드카 짝
    2. 검사            사이드카 검증, 세션 검증, 캘리브레이션 대조 — 치명이면 멈춤
    3. 프로젝트 만들기   <작업폴더>/sessions/<세션>/ (ASCII 경로)
    4. 2D 자세 추정     Pose2Sim.poseEstimation (GPU 가능하면 GPU)
    5. 시각 맞추기      키포인트 리샘플러 → pose-sync/ (Pose2Sim synchronization 대신)
    6. 3D             personAssociation → triangulation → filtering (+ kinematics)
    7. 요약            재투영 오차, 카메라 배제 비율, 빈 좌표 비율

캘리브레이션이 없거나 카메라가 1대면 5번까지 하고 멈춥니다 (종료 코드 10).

Pose2Sim 이 설치된 환경의 파이썬으로 실행합니다:
    & "$env:USERPROFILE\\.venv\\pose2sim_gpu\\Scripts\\python.exe" tools\\run_session.py S20260925-231039

    인자는 uploads/ 아래 세션 이름이나 폴더 경로입니다.
    캘리브레이션(리그) 기본 위치: ~/Pose2SimWork/rig/  (Calib.toml + cameras.json)

종료 코드: 0 = 3D 완료, 10 = 3D 전까지 완료(캘리브레이션 없음/1대),
          2 = 입력 문제(고쳐야 진행 가능), 3 = Pose2Sim 단계 실패
"""
from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))

from mocapsync import pipeline as PL  # noqa: E402
from mocapsync import resample as R  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BAR = "=" * 72
SUB = "-" * 72

EXIT_DONE, EXIT_INPUT, EXIT_STAGE, EXIT_PARTIAL = 0, 2, 3, 10


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
    raise PL.PipelineError(f"촬영 폴더를 찾을 수 없습니다: {arg}  (uploads/ 아래 이름 또는 경로)")


def pick_device(req: str) -> tuple[str, str, str]:
    """(device, backend, 설명). CUDA 는 onnxruntime-gpu 가 있고 CUDA 제공자가 보일 때만."""
    try:
        import onnxruntime as ort
        provs = ort.get_available_providers()
    except Exception:
        ort, provs = None, []
    try:
        md.version("onnxruntime-gpu")
        gpu_pkg = True
    except md.PackageNotFoundError:
        gpu_pkg = False

    if req != "CPU" and gpu_pkg and "CUDAExecutionProvider" in provs:
        try:
            # ★ pip 로 받은 CUDA DLL 을 찾게 합니다 (DESIGN §4.1 함정 3)
            ort.preload_dlls()
        except Exception as e:
            return "CUDA", "onnxruntime", f"GPU (preload_dlls 실패: {e} — CPU 로 후퇴할 수 있음)"
        return "CUDA", "onnxruntime", "GPU (onnxruntime CUDA)"
    if req == "CUDA":
        raise PL.PipelineError(
            "GPU 를 요청했지만 이 파이썬에 onnxruntime-gpu 가 없거나 CUDA 가 보이지 않습니다. "
            "~/.venv/pose2sim_gpu 의 파이썬으로 실행하세요.")
    return "CPU", "auto", "CPU"


def count_json(d: Path) -> dict[str, int]:
    if not d.is_dir():
        return {}
    return {s.name: len(list(s.glob("*.json"))) for s in sorted(d.iterdir()) if s.is_dir()}


def main() -> int:
    ap = argparse.ArgumentParser(description="촬영 한 건 → 3D (4단계 파이프라인)")
    ap.add_argument("session", help="uploads/ 아래 세션 이름 또는 촬영 폴더 경로")
    ap.add_argument("--work-root", type=Path, default=PL.DEFAULT_WORK_ROOT,
                    help=f"작업 폴더 (기본 {PL.DEFAULT_WORK_ROOT})")
    ap.add_argument("--rig", type=Path, default=None,
                    help="캘리브레이션 폴더 (기본 <작업폴더>/rig). Calib.toml + cameras.json")
    ap.add_argument("--no-rig", action="store_true", help="캘리브레이션을 쓰지 않음 (3D 전까지만)")
    ap.add_argument("--device", default="auto", choices=["auto", "CUDA", "CPU"])
    ap.add_argument("--pose-mode", default="performance",
                    choices=["lightweight", "balanced", "performance"])
    ap.add_argument("--save-video", action="store_true",
                    help="관절을 그린 확인용 영상도 저장 (GPU 에서 약 1.5배 느려짐)")
    ap.add_argument("--redo-pose", action="store_true", help="이미 있는 2D 결과를 버리고 다시 추정")
    ap.add_argument("--method", default=R.DEFAULT_METHOD, choices=R.METHODS, help="리샘플 보간법")
    ap.add_argument("--kinematics", action="store_true", help="OpenSim 관절각까지 (느림)")
    a = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        sess_dir = resolve_session(a.session)
    except PL.PipelineError as e:
        print(f"★ {e}")
        return EXIT_INPUT
    sid = sess_dir.name
    log = Tee(REPO / "logs" / f"run_session_{sid}_{stamp}.log")
    run: dict = {"session": sid, "uploads": str(sess_dir), "started": stamp, "stages": {}}
    project = (a.work_root / "sessions" / sid)

    def finish(code: int, verdict: str) -> int:
        run["exit_code"], run["verdict"] = code, verdict
        if project.is_dir():
            (project / "mocapsync_run.json").write_text(
                json.dumps(run, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        log()
        log(BAR)
        log(f" 결과: {verdict}")
        log(f" 이 실행 로그: {log.path}")
        if project.is_dir():
            log(f" 프로젝트: {project}")
        log(BAR)
        log.close()
        return code

    log(BAR)
    log(f" 촬영 → 3D   세션 {sid}   {datetime.now():%Y-%m-%d %H:%M:%S}")
    log(BAR)

    # ── 1. 입력 ───────────────────────────────────────────────────────────
    log()
    log(" [1/7] 입력 찾기")
    try:
        ups = PL.find_uploads(sess_dir)
        rig = None if a.no_rig else PL.load_rig(a.rig or (a.work_root / "rig"))
        cams = PL.assign_cameras(ups, rig)
    except PL.PipelineError as e:
        log(f"   ★ {e}")
        return finish(EXIT_INPUT, "입력 문제로 멈춤")
    for c in cams:
        sc = c.upload.sidecar
        log(f"   {c.name} = {c.device_id} ({sc.device_name or '?'})  "
            f"{c.upload.video.name}  {sc.frame_count}프레임  {sc.width}x{sc.height}")
    log(f"   캘리브레이션: {rig.calib_path if rig else '없음 — 3D 전까지만 합니다'}")
    run["cameras"] = {c.name: c.device_id for c in cams}

    # ── 2. 검사 ───────────────────────────────────────────────────────────
    log()
    log(" [2/7] 검사")
    findings = PL.check_inputs(cams, rig)
    for f in findings:
        log(f"   {f}")
    run["findings"] = [str(f) for f in findings]
    if PL.has_fatal(findings):
        log()
        log("   ★ [치명] 항목이 있어 멈춥니다. 이대로 돌리면 결과가 조용히 틀립니다.")
        return finish(EXIT_INPUT, "검사에서 치명 문제 — 멈춤")
    log("   치명 문제 없음")

    # ── 3. 프로젝트 ───────────────────────────────────────────────────────
    log()
    log(" [3/7] 프로젝트 만들기")
    try:
        import Pose2Sim as _p2s
        from Pose2Sim import Pose2Sim as P2S
    except ImportError:
        log("   ★ Pose2Sim 이 이 파이썬에 없습니다. 이렇게 실행하세요:")
        log('     & "$env:USERPROFILE\\.venv\\pose2sim_gpu\\Scripts\\python.exe" '
            f"tools\\run_session.py {sid}")
        return finish(EXIT_INPUT, "Pose2Sim 환경이 아님")
    template = Path(_p2s.__file__).resolve().parent / "Demo_SinglePerson" / "Config.toml"
    try:
        how = PL.build_project(project, cams, rig, template)
    except PL.PipelineError as e:
        log(f"   ★ {e}")
        return finish(EXIT_INPUT, "프로젝트를 만들 수 없음")
    log(f"   {project}")
    log("   영상: " + ", ".join(f"{k} {v}" for k, v in how.items()))

    try:
        device, backend, dev_desc = pick_device(a.device)
    except PL.PipelineError as e:
        log(f"   ★ {e}")
        return finish(EXIT_INPUT, "장치 선택 실패")
    fps = float(max(c.upload.sidecar.target_fps for c in cams) or R.DEFAULT_FPS)
    cfg = PL.pose2sim_overrides(project, fps, device, backend, a.pose_mode,
                                a.save_video, a.redo_pose)
    (project / "mocapsync_overrides.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    def stage(name: str, fn) -> bool:
        log()
        log(SUB)
        log(f" >> {name}")
        log(SUB)
        t0 = time.time()
        try:
            fn(cfg)
        except Exception as e:
            dt = time.time() - t0
            run["stages"][name] = {"ok": False, "s": round(dt, 1), "error": f"{type(e).__name__}: {e}"}
            log(f"   -> 실패 ({dt:.1f}초): {type(e).__name__}: {e}")
            for ln in traceback.format_exc().splitlines():
                log("      " + ln)
            return False
        dt = time.time() - t0
        run["stages"][name] = {"ok": True, "s": round(dt, 1)}
        log(f"   -> 완료 ({dt:.1f}초)")
        return True

    cwd0 = Path.cwd()
    os.chdir(project)        # Pose2Sim 일부 경로가 현재 폴더 기준입니다
    try:
        # ── 4. 2D ─────────────────────────────────────────────────────────
        log()
        log(f" [4/7] 2D 자세 추정  ({dev_desc}, mode={a.pose_mode})")
        before = count_json(project / "pose")
        reused = bool(before) and all(before.values()) and not a.redo_pose
        if reused:
            log("   이전 2D 결과가 있어 그대로 씁니다 (--redo-pose 로 다시 추정)")
        if not stage("poseEstimation", P2S.poseEstimation):
            return finish(EXIT_STAGE, "2D 자세 추정 실패")
        got = count_json(project / "pose")
        want = {f"{c.name}_json" for c in cams}
        if set(got) != want or not all(got.values()):
            log(f"   ★ 2D 결과 폴더가 예상과 다릅니다: {got} (기대 {sorted(want)})")
            return finish(EXIT_STAGE, "2D 결과 이상")
        n_total = sum(got.values())
        log("   " + ", ".join(f"{k} {v}개" for k, v in got.items()))
        if not reused:
            dt = run["stages"]["poseEstimation"]["s"]
            speed = n_total / dt if dt else 0
            run["pose_fps"] = round(speed, 1)
            log(f"   처리 속도 {speed:.1f} 프레임/초")
            if device == "CUDA" and speed < 10:
                log("   [주의] GPU 치고 느립니다. 다른 GPU 작업(게임 등)이 돌고 있거나 "
                    "CPU 로 후퇴했을 수 있습니다 (nvidia-smi 로 확인).")

        # ── 5. 시각 맞추기 ────────────────────────────────────────────────
        log()
        log(f" [5/7] 시각 맞추기 (리샘플러, {a.method})")
        pairs = [(project / "pose" / f"{c.name}_json", project / "sidecars" / f"{c.name}.json")
                 for c in cams]
        try:
            res = R.resample_session(pairs, project / "pose-sync", fps=fps, method=a.method)
        except R.ResampleError as e:
            log(f"   ★ {e}")
            return finish(EXIT_INPUT, "리샘플 중단")
        rep = res.report()
        run["resample"] = rep
        log(f"   공통 구간 {rep['durationS']:.2f}초, 격자 {rep['gridPoints']}점 @{fps:g}Hz")
        for c in rep["cameras"]:
            log(f"   {c['name']:<11} 사람 있는 격자 {c['person_ratio'] * 100:5.1f}%  "
                f"주 피험자 관절 {c['main_filled_ratio'] * 100:5.1f}%  "
                f"시각 이동 평균 {c['shift_ms_mean']:.2f} ms  튀어서 끊음 {c['jump_cuts']}회")
            for w in c["warnings"]:
                log(f"      [경고] {w}")

        # ── 6. 3D ─────────────────────────────────────────────────────────
        log()
        log(" [6/7] 3D")
        if rig is None or len(cams) < 2:
            why = "캘리브레이션이 없습니다" if rig is None else "카메라가 1대입니다"
            log(f"   {why}. 3D 는 건너뜁니다.")
            log("   여기까지 결과: pose/ (2D), pose-sync/ (시각 맞춘 2D)")
            return finish(EXIT_PARTIAL, f"3D 전까지 완료 ({why})")
        steps = [("personAssociation", P2S.personAssociation),
                 ("triangulation", P2S.triangulation),
                 ("filtering", P2S.filtering)]
        if a.kinematics:
            steps.append(("kinematics", P2S.kinematics))
        for name, fn in steps:
            if not stage(name, fn):
                return finish(EXIT_STAGE, f"{name} 실패")
    finally:
        os.chdir(cwd0)

    # ── 7. 요약 ───────────────────────────────────────────────────────────
    log()
    log(" [7/7] 요약")
    logs = project / "logs.txt"
    summary = PL.summarize_logs(logs.read_text(encoding="utf-8", errors="replace")) \
        if logs.is_file() else {}
    trcs = sorted((project / "pose-3d").glob("*.trc"))
    summary["trc"] = [PL.trc_summary(t) for t in trcs]
    run["summary"] = summary
    if "reproj_px" in summary:
        log(f"   재투영 오차 {summary['reproj_px']:.1f} px"
            + (f" (약 {summary['reproj_mm']:.1f} mm)" if "reproj_mm" in summary else ""))
    if summary.get("excluded_pct"):
        log("   카메라 배제 비율: " + ", ".join(f"{k} {v}%" for k, v in summary["excluded_pct"].items()))
    for t in summary["trc"]:
        log(f"   {t['file']}: {t.get('frames')}프레임, 마커 {t.get('markers')}개, "
            f"빈 좌표 {t.get('empty_pct', 0):.1f}%")
    notes = PL.quality_notes(summary)
    run["notes"] = notes
    for n in notes:
        log(f"   {n}")
    filt = [t for t in trcs if "_filt" in t.name]
    if not filt:
        log("   ★ 필터된 TRC 가 없습니다.")
        return finish(EXIT_STAGE, "3D 파일 없음")
    log(f"   3D 결과: {filt[0]}")
    return finish(EXIT_DONE, "3D 완료")


if __name__ == "__main__":
    raise SystemExit(main())
