"""
캘리브레이션 촬영 한 건을 처리합니다 (tools/calibrate.py 가 부릅니다).

  intrinsics  uploads/<세션>/ 의 영상마다 렌즈 특성
              → <작업폴더>/intrinsics/<기기ID>.json  (+ <기기ID>_coverage.jpg)
  extrinsics  uploads/<세션>/ 의 영상 전부 (동시 녹화)
              → <작업폴더>/rig/  Calib.toml, cameras.json, rig_report.json, check_camNN.jpg

계산은 mocapsync.calib 에 있고, 여기서는 파일을 찾고, 순서를 정하고, 사람에게 보여 줄
말을 만들고, 결과를 씁니다. 판정이 나쁘면 쓰지 않습니다 (--force 로만). 조용히 틀린
캘리브레이션은 조용히 틀린 3D 가 되기 때문입니다.
"""
from __future__ import annotations

import json
import math
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from . import calib as C
from . import pipeline as PL
from .sidecar import check_session

Log = Callable[[str], None]

INTRINSICS_DIR = "intrinsics"
RIG_DIR = "rig"
RIG_BACKUP_DIR = "rig_backup"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _fmt_level(level: str) -> str:
    return {"good": "좋음", "ok": "쓸 만함 (아래 주의)", "bad": "부족"}[level]


# ── 내부 ──────────────────────────────────────────────────────────────────────

@dataclass
class IntrinsicsOutcome:
    device_id: str
    level: str                           # good / ok / bad
    messages: list[str] = field(default_factory=list)
    record: dict | None = None
    saved: Path | None = None


def run_intrinsics(session_dir: Path, work_root: Path, board: C.Board = C.DEFAULT_BOARD, *,
                   log: Log = print, only: list[str] | None = None, force: bool = False,
                   rational: bool = False, max_speed: float = C.INTR_MAX_SPEED,
                   max_views: int = C.INTR_MAX_VIEWS, rate_hz: float = 10.0,
                   stamp: str | None = None) -> list[IntrinsicsOutcome]:
    """세션의 영상마다 렌즈 특성을 구합니다. 폰 한 대가 찍었든 여러 대가 찍었든 됩니다."""
    stamp = stamp or _stamp()
    ups = PL.find_uploads(Path(session_dir))
    if only:
        ups = [u for u in ups if u.device_id in only]
        if not ups:
            raise C.CalibError(f"이 촬영에 {only} 기기의 영상이 없습니다.")
    out_dir = Path(work_root) / INTRINSICS_DIR
    outcomes = []
    for u in ups:
        sc = u.sidecar
        log("")
        log(f" ● {u.device_id} ({sc.device_name or '?'})  {u.video.name}")
        log(f"   렌즈 {sc.camera_device_type or '?'}  {sc.width}x{sc.height} @{sc.target_fps}fps  "
            f"폰이 알려준 화각 {sc.field_of_view_deg:.1f}도")
        w, h, n, vfps = C.video_info(u.video)
        fps = float(sc.target_fps or vfps or 60)
        every = max(1, int(round(fps / rate_hz)))
        ks = list(range(0, max(0, n - 1), every))
        want = set(ks) | {k + 1 for k in ks}

        def prog(done, total):
            log(f"   판 찾는 중 {done}/{total}")

        det, size = C.detect_in_video(u.video, want, board, progress=prog)
        obs = C.paired_observations(det, ks, fps)
        slow = [o for o in obs if o.speed <= max_speed]
        seen = sum(1 for k in ks if k in det)
        log(f"   살펴본 장면 {len(ks)}개 → 판이 보임 {seen}개 → 천천히 움직인 장면 {len(slow)}개"
            f" (빠르다고 뺀 장면 {len(obs) - len(slow)}개, 기준 {max_speed:.0f} px/초)")
        if (w, h) != (sc.width, sc.height) and sc.width > 0:
            log(f"   [주의] 영상 {w}x{h} 가 사이드카 {sc.width}x{sc.height} 와 다릅니다.")
        if seen == 0:
            msg = ("판이 한 번도 보이지 않았습니다. 판 전체(안쪽 꼭짓점 "
                   f"{board.cols}x{board.rows}개)가 화면 안에 들어왔는지, 조명이 충분한지, "
                   "--board 설정이 인쇄한 판과 같은지 확인하세요.")
            log(f"   ★ {msg}")
            outcomes.append(IntrinsicsOutcome(u.device_id, "bad", [msg]))
            continue
        try:
            res = C.calibrate_intrinsics(slow, size, board, max_views, rational)
        except C.CalibError as e:
            log(f"   ★ {e}")
            if len(obs) > len(slow) * 2:
                log("     판을 더 천천히 움직이세요. 자세를 바꿀 때마다 1초씩 멈추면 됩니다.")
            outcomes.append(IntrinsicsOutcome(u.device_id, "bad", [str(e)]))
            continue

        level, msgs = res.verdict()
        K, d = res.cam.K, res.cam.dist
        hold = "못 함 (장면 부족)" if math.isnan(res.holdout_rms) else f"{res.holdout_rms:.3f} px"
        log(f"   재투영 오차 {res.rms:.3f} px  (목표 {C.INTR_GOOD_RMS} px 이하)   "
            f"안 쓴 장면으로 검산 {hold}")
        log(f"   쓴 장면 {res.views}개 (오차 커서 뺀 장면 {res.dropped_views}개)   "
            f"화면 덮은 비율 {res.coverage * 100:.0f}% / 가장자리 {res.edge_coverage * 100:.0f}%")
        log(f"   fx {K[0, 0]:.1f}  fy {K[1, 1]:.1f}  cx {K[0, 2]:.1f}  cy {K[1, 2]:.1f}  "
            f"(추정 오차 ±{res.std_intrinsics['fx']:.2f} / ±{res.std_intrinsics['cx']:.2f} px)")
        log("   왜곡 " + " ".join(f"{v:+.4f}" for v in d))
        fov = C.horizontal_fov_deg(res.cam)
        log(f"   계산한 가로 화각 {fov:.1f}도 (폰이 알려준 값 {sc.field_of_view_deg:.1f}도)")
        if sc.field_of_view_deg > 0 and abs(fov - sc.field_of_view_deg) > 8:
            msgs.append(f"계산한 화각 {fov:.1f}도가 폰이 알려준 {sc.field_of_view_deg:.1f}도와 "
                        "8도 넘게 다릅니다. 다른 렌즈로 찍었거나 판 크기 설정이 틀렸을 수 있습니다.")
        p2s = C.pose2sim_undistort_error(res.cam)
        if p2s > 1.0:
            msgs.append(f"왜곡이 커서 Pose2Sim 기본 왜곡 펴기가 화면 구석에서 최대 {p2s:.1f} px "
                        "덜 폅니다 (OpenCV 기본 반복 횟수). 가장자리 관절 정확도가 떨어질 수 있습니다.")
        for m in msgs:
            log(f"   [주의] {m}")
        log(f"   판정: {_fmt_level(level)}")

        rec = C.intrinsics_record(res, sc, Path(session_dir).name, board, stamp)
        rec["computedFovDeg"] = round(fov, 2)
        rec["pose2simUndistortErrorPx"] = round(p2s, 3)
        oc = IntrinsicsOutcome(u.device_id, level, msgs, rec)
        if level == "bad" and not force:
            log("   ★ 판정이 부족해서 저장하지 않았습니다. 다시 찍거나 --force 로 저장하세요.")
        else:
            oc.saved = C.save_intrinsics(out_dir, rec, stamp)
            frame = C.read_frame(u.video, res.used_keys[len(res.used_keys) // 2])
            img = C.draw_coverage(frame, size, (o.corners for o in slow
                                                if o.key in set(res.used_keys)), res.coverage_map)
            cv2.imwrite(str(out_dir / f"{u.device_id}_coverage.jpg"), img)
            log(f"   저장: {oc.saved}")
            log(f"   확인 그림: {out_dir / (u.device_id + '_coverage.jpg')}  "
                "(초록 점 = 쓴 꼭짓점, 빨간 칸 = 판이 한 번도 안 닿은 곳)")
        outcomes.append(oc)
    return outcomes


# ── 외부 ──────────────────────────────────────────────────────────────────────

@dataclass
class ExtrinsicsOutcome:
    level: str
    messages: list[str]
    report: dict
    rig_dir: Path | None = None


def _cam_order(ups: list[PL.Upload], order: list[str] | None) -> list[PL.Upload]:
    """cam01, cam02 ... 순서. 기본은 기기 ID 순서 (pipeline.assign_cameras 와 같은 규칙)."""
    by_id = {u.device_id: u for u in ups}
    if not order:
        return [by_id[i] for i in sorted(by_id)]
    if sorted(order) != sorted(by_id):
        raise C.CalibError(f"--order {order} 가 이 촬영의 기기 {sorted(by_id)} 와 다릅니다.")
    return [by_id[i] for i in order]


def run_extrinsics(session_dir: Path, work_root: Path, board: C.Board = C.DEFAULT_BOARD, *,
                   log: Log = print, force: bool = False, order: list[str] | None = None,
                   rate_hz: float = 8.0, max_speed: float = C.EXTR_MAX_SPEED,
                   use_floor: bool = True, stamp: str | None = None) -> ExtrinsicsOutcome:
    stamp = stamp or _stamp()
    work_root = Path(work_root)
    ups = PL.find_uploads(Path(session_dir))
    if len(ups) < 2:
        raise C.CalibError(f"영상이 {len(ups)}개입니다. 위치·방향은 폰 두 대 이상이 "
                           "**동시에** 찍은 촬영으로 구합니다.")
    ups = _cam_order(ups, order)
    names = [PL.cam_name(i) for i in range(len(ups))]

    # 시계가 믿을 만해야 같은 순간을 짝지을 수 있습니다
    log("")
    log(" [검사] 사이드카 (시계 동기)")
    fatal = []
    for name, u in zip(names, ups):
        for i in u.sidecar.validate():
            if i.severity == "fatal":
                fatal.append(f"{name}: {i}")
            elif i.code in ("clock_uncertainty_degraded", "sync_aging", "frame_drops", "too_short"):
                log(f"   [{name}] {i}")
    for i in check_session([u.sidecar for u in ups]):
        if i.severity == "fatal":
            fatal.append(f"세션: {i}")
        elif i.code in ("overlap", "worst_uncertainty", "overlap_too_short", "lens_mismatch"):
            log(f"   [세션] {i}")
    if fatal:
        raise C.CalibError("사이드카에 치명 문제가 있어 같은 순간을 짝지을 수 없습니다:\n     "
                           + "\n     ".join(fatal))

    # 렌즈 특성
    log("")
    log(" [렌즈 특성] 폰마다 저장된 값을 씁니다")
    idir = work_root / INTRINSICS_DIR
    models, recs = [], []
    for name, u in zip(names, ups):
        rec = C.load_intrinsics(idir, u.device_id)
        if rec is None:
            raise C.CalibError(
                f"{name} = {u.device_id} ({u.sidecar.device_name or '?'}) 의 렌즈 특성이 없습니다 "
                f"({idir / (u.device_id + '.json')}). 먼저 렌즈 특성 촬영을 하고 "
                "'intrinsics' 를 돌리세요.")
        bad = C.intrinsics_mismatch(rec, u.sidecar)
        if bad:
            raise C.CalibError(
                f"{name} = {u.device_id} 의 렌즈 특성은 다른 촬영 설정에서 잰 것입니다: "
                + "; ".join(bad) + ". 지금 설정으로 렌즈 특성을 다시 찍으세요.")
        w, h, nframes, _ = C.video_info(u.video)
        if (w, h) != (int(rec["width"]), int(rec["height"])):
            raise C.CalibError(f"{name}: 영상 {w}x{h} 가 렌즈 특성 {rec['width']}x{rec['height']} "
                               "와 다릅니다.")
        if nframes > u.sidecar.frame_count:
            raise C.CalibError(
                f"{name}: 영상 프레임 {nframes}개가 사이드카 시각 {u.sidecar.frame_count}개보다 "
                "많습니다. 시각을 붙일 수 없습니다.")
        models.append(C.cam_from_record(rec))
        recs.append(rec)
        log(f"   {name} = {u.device_id} ({u.sidecar.device_name or '?'})  렌즈 특성 "
            f"{rec.get('created', '?')} 오차 {rec.get('rmsPx', '?')} px")

    # 같은 순간 격자
    t_list, idx_list = [], []
    for u in ups:
        fr = np.array(u.sidecar.frames, dtype=np.int64)
        t_list.append(fr[:, 1] + np.int64(u.sidecar.clock_offset_ns))
        idx_list.append(fr[:, 0])
    grid = C.sample_grid(t_list, rate_hz)
    log("")
    log(f" [판 찾기] 공통 구간 {(grid[-1] - grid[0]) / 1e9:.1f}초, {rate_hz:g}번/초 → 격자 {len(grid)}점")
    obs_all = []
    for name, u, t, fi in zip(names, ups, t_list, idx_list):
        want = C.frames_for_grid(t, grid, fi)

        def prog(done, total, name=name):
            log(f"   {name} 판 찾는 중 {done}/{total}")

        det, _ = C.detect_in_video(u.video, want, board, progress=prog)
        ob = C.timed_observations(det, t, grid, fi)
        obs_all.append(ob)
        fast = sum(1 for o in ob.values() if o.speed > max_speed)
        log(f"   {name}: 판이 보인 순간 {len(ob)}개 (빠르다고 뺄 순간 {fast}개)")
    obs_ba = [{m: o for m, o in ob.items() if o.speed <= max_speed} for ob in obs_all]

    # 번들 조정
    log("")
    log(" [계산] 카메라 위치·방향 (번들 조정)")
    sol = C.bundle_adjust(models, obs_ba, board, ref=0)
    pairs = C.shared_counts(sol, len(ups))
    scale = C.scale_check(models, sol, obs_ba, board)

    # 바닥 → 월드
    floor = C.find_floor(models, sol, obs_all, grid, board) if use_floor else None
    if floor is not None:
        R_W, o = C.world_frame(floor.up, floor.point)
    else:
        up = np.mean([R.T @ np.array([0.0, -1.0, 0.0]) for R in sol.cam_R], axis=0)
        R_W, o = C.world_frame(up, np.zeros(3))
    poses = C.to_world(sol, R_W, o)
    centers = [C.camera_center(R, t) for R, t in poses]

    # 판정
    msgs: list[str] = []
    level = "good"

    def worse(to: str) -> None:
        nonlocal level
        order_ = ["good", "ok", "bad"]
        if order_.index(to) > order_.index(level):
            level = to

    for (a, b), cnt in pairs.items():
        if cnt < C.EXTR_MIN_SHARED:
            worse("bad")
            msgs.append(f"{names[a]}–{names[b]} 가 판을 함께 본 장면이 {cnt}개뿐입니다.")
        elif cnt < 3 * C.EXTR_MIN_SHARED:
            worse("ok")
            msgs.append(f"{names[a]}–{names[b]} 가 판을 함께 본 장면이 {cnt}개로 적은 편입니다.")
    if sol.rms > C.EXTR_OK_RMS:
        worse("bad")
        msgs.append(f"재투영 오차 {sol.rms:.2f} px 가 큽니다 (좋음 {C.EXTR_GOOD_RMS} / 한계 "
                    f"{C.EXTR_OK_RMS} px). 렌즈 특성이 틀렸거나 시계 동기가 어긋났을 수 있습니다.")
    elif sol.rms > C.EXTR_GOOD_RMS:
        worse("ok")
        msgs.append(f"재투영 오차 {sol.rms:.2f} px 가 조금 큽니다 (좋음 {C.EXTR_GOOD_RMS} px 이하).")
    err = abs(scale.get("errorPct", math.inf))
    if err > C.SCALE_OK_PCT:
        worse("bad")
        msgs.append(f"카메라로 잰 판 한 칸이 {scale.get('squareMm', float('nan')):.2f} mm 입니다 "
                    f"(실제 {board.square_m * 1000:g} mm). 축척이 틀렸습니다 — 판 인쇄 크기나 "
                    "--square-mm 설정을 확인하세요.")
    elif err > C.SCALE_GOOD_PCT:
        worse("ok")
        msgs.append(f"카메라로 잰 판 한 칸이 {scale['squareMm']:.2f} mm 로 실제와 {err:.1f}% 다릅니다.")
    if floor is None:
        worse("ok")
        msgs.append("바닥에 눕힌 판을 찾지 못했습니다. 위쪽 방향을 카메라 기울기로 어림했고 "
                    "높이 0 이 바닥이 아닙니다 (3D 는 되지만 바닥·수직이 조금 틀어질 수 있음).")
    else:
        for name, c in zip(names, centers):
            if not 0.1 <= c[2] <= 3.0:
                worse("bad")
                msgs.append(f"{name} 높이가 {c[2]:.2f} m 로 나왔습니다. 바닥 판을 잘못 찾았을 수 있습니다.")

    # 보고서
    cams_rep = {}
    for i, (name, u, rec) in enumerate(zip(names, ups, recs)):
        R, t = poses[i]
        fwd = R.T @ np.array([0.0, 0.0, 1.0])
        cams_rep[name] = {
            "deviceId": u.device_id, "deviceName": u.sidecar.device_name,
            "rmsPx": round(sol.cam_rms(i), 3),
            "views": sum(1 for (c, _) in sol.obs_rms if c == i),
            "positionM": [round(float(v), 4) for v in centers[i]],
            "heightM": round(float(centers[i][2]), 3),
            "lookDownDeg": round(math.degrees(math.asin(float(np.clip(-fwd[2], -1, 1)))), 1),
            "intrinsicsCreated": rec.get("created"), "intrinsicsRmsPx": rec.get("rmsPx"),
        }
    dist = {f"{names[a]}-{names[b]}": round(float(np.linalg.norm(centers[a] - centers[b])), 3)
            for a in range(len(ups)) for b in range(a + 1, len(ups))}
    report = {
        "created": stamp, "session": Path(session_dir).name, "board": board.to_dict(),
        "cameras": cams_rep, "distancesM": dist,
        "sharedViews": {f"{names[a]}-{names[b]}": c for (a, b), c in pairs.items()},
        "rmsPx": round(sol.rms, 3), "droppedObservations": sol.dropped,
        "scaleCheck": scale,
        "floor": None if floor is None else {
            "camera": names[floor.cam], "durationS": round(floor.duration_s, 2),
            "views": floor.views, "rmsPx": round(floor.rms, 3)},
        "verdict": level, "messages": msgs,
    }

    log(f"   재투영 오차 {sol.rms:.3f} px (좋음 {C.EXTR_GOOD_RMS} px 이하), 튀어서 뺀 관측 {sol.dropped}개")
    for (a, b), cnt in pairs.items():
        log(f"   {names[a]}–{names[b]} 함께 본 장면 {cnt}개")
    if scale.get("views"):
        log(f"   검산: 카메라로 잰 판 한 칸 {scale['squareMm']:.2f} mm (실제 {board.square_m * 1000:g} mm, "
            f"{scale['errorPct']:+.2f}%)")
    log("")
    log(" [결과] 줄자로 확인할 수 있는 값")
    for name in names:
        c = cams_rep[name]
        log(f"   {name} ({c['deviceName'] or c['deviceId']}): 높이 {c['heightM']:.2f} m, "
            f"아래로 {c['lookDownDeg']:.0f}도 숙임, 오차 {c['rmsPx']:.2f} px")
    for k, v in dist.items():
        log(f"   {k} 사이 거리 {v:.2f} m")
    if floor is not None:
        log(f"   바닥 판: {names[floor.cam]} 이 {floor.duration_s:.1f}초 동안 봄 → 원점 = 바닥 판 가운데")
    for m in msgs:
        log(f"   [주의] {m}")
    log(f"   판정: {_fmt_level(level)}")

    oc = ExtrinsicsOutcome(level, msgs, report)
    if level == "bad" and not force:
        log("   ★ 판정이 부족해서 리그를 쓰지 않았습니다. 다시 찍거나 --force 로 쓰세요.")
        return oc
    oc.rig_dir = write_rig(work_root, names, ups, models, poses, sol.rms, report, stamp)
    log("")
    log(f"   리그 저장: {oc.rig_dir}")
    log("     Calib.toml, cameras.json, rig_report.json, "
        + ", ".join(f"check_{n}.jpg" for n in names))

    # 확인 그림: 바닥 판이 보이던 순간 (없으면 끝 무렵)
    t_pick = int(grid[len(grid) - 1])
    if floor is not None and floor.keys:
        t_pick = int(grid[floor.keys[len(floor.keys) // 2]])
    for i, (name, u, t, fi) in enumerate(zip(names, ups, t_list, idx_list)):
        k = int(np.clip(np.searchsorted(t, t_pick), 0, len(t) - 1))
        frame = C.read_frame(u.video, int(fi[k]))
        if frame is None:
            continue
        R, tt = poses[i]
        others = [(names[j], centers[j]) for j in range(len(names)) if j != i]
        img = C.draw_check(frame, models[i], R, tt, others, floor is not None)
        cv2.imwrite(str(oc.rig_dir / f"check_{name}.jpg"), img)
    log("   확인 그림: 노란 격자(0.5 m)가 바닥에 붙어 보이고, 다른 폰이 화면에 보이면 그 자리에")
    log("   보라색 동그라미가 오면 맞습니다.")
    return oc


def write_rig(work_root: Path, names: list[str], ups: list[PL.Upload], models: list[C.CamModel],
              poses: list[tuple[np.ndarray, np.ndarray]], rms: float, report: dict,
              stamp: str) -> Path:
    """
    리그 폴더를 새로 씁니다. 이전 리그는 rig_backup/<시각>/ 으로 옮깁니다
    (pipeline.load_rig 는 폴더에 .toml 이 둘이면 멈추므로 섞이면 안 됩니다).
    """
    rig = Path(work_root) / RIG_DIR
    if rig.exists() and any(rig.iterdir()):
        dst = Path(work_root) / RIG_BACKUP_DIR / stamp
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(rig), str(dst))
    rig.mkdir(parents=True, exist_ok=True)
    (rig / "Calib.toml").write_text(C.calib_toml(names, models, poses, rms), encoding="utf-8")
    # ★ 사람이 쓰지 않습니다. 영상마다 사이드카의 deviceId 로 정한 순서 그대로입니다.
    (rig / "cameras.json").write_text(
        json.dumps({n: u.device_id for n, u in zip(names, ups)}, indent=2), encoding="utf-8")
    (rig / "rig_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    return rig
