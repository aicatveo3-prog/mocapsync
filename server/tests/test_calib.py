"""
캘리브레이션(mocapsync.calib) 시험 — 정답을 아는 가짜 카메라로.

핵심은 **조용히 틀리는 경우**를 막는지입니다.
 - 판을 180도 돌려 들어도 꼭짓점 번호가 같은 모서리에서 시작하는가
   (카메라마다 다르게 세면 스테레오가 틀린 채로 "성공" 합니다)
 - 렌즈 특성을 되찾는가 (초광각 수준의 강한 왜곡)
 - 두 폰의 프레임 시각이 어긋나도 같은 순간으로 보간하는가
 - 카메라 사이 거리(축척)·방향·바닥이 맞는가
 - Pose2Sim 이 읽는 규칙(월드→카메라, Rodrigues, 미터, 카메라 순서)과 같은가
"""
from __future__ import annotations

import math
import tomllib

import cv2
import numpy as np
import pytest

from mocapsync import calib as C
from mocapsync import pipeline as PL

from calib_synth import (SynthCam, board_in_world, compose, floor_board, look_at,
                         true_corners)

BOARD = C.Board()
#: 초광각 정도의 강한 통 왜곡 (가로 화각 약 104도)
WIDE = C.CamModel(K=np.array([[330.0, 0, 323.0], [0, 331.0, 178.0], [0, 0, 1]]),
                  dist=np.array([-0.25, 0.08, 0.0005, -0.0003, -0.012]), size=(640, 360))


@pytest.fixture(scope="module")
def wide_synth():
    return SynthCam(WIDE, ss=2, seed=1)


def rot_deg(Ra, Rb) -> float:
    return math.degrees(C.Rotation.from_matrix(Ra @ Rb.T).magnitude())


# ── 판 ────────────────────────────────────────────────────────────────────────

def test_object_points_row_major():
    p = BOARD.object_points()
    assert p.shape == (54, 3)
    assert np.allclose(p[1], [0.023, 0, 0])          # 줄 안에서 X 로 증가
    assert np.allclose(p[6], [0, 0.023, 0])          # 다음 줄 = Y
    assert np.allclose(BOARD.center, [0.0575, 0.092, 0])


# ── 검출과 방향 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spin", [0, 90, 180, 270, 33, 212])
def test_detect_same_corner_order_whatever_rotation(wide_synth, spin):
    """판을 제 면 안에서 어떻게 돌려도 0번 꼭짓점이 판의 같은 모서리입니다."""
    Rw, tw = look_at([0, 0, 0], [0, 1, 0])
    Rb, tb = board_in_world([0.04, 0.35, 0.01], [0, 0, 0], spin)
    R, t = compose(Rw, tw, Rb, tb)
    c = C.detect_board(wide_synth.render([(R, t)], BOARD), BOARD)
    assert c is not None
    # 렌더링 앨리어싱 때문에 0.5 px 까지는 봐 줍니다. 순서가 틀리면 수십 px 입니다.
    assert np.abs(c - true_corners(WIDE, R, t, BOARD)).max() < 0.5


def test_detect_tilted_board(wide_synth):
    Rw, tw = look_at([0, 0, 0], [0, 1, 0])
    Rb, tb = board_in_world([-0.08, 0.4, -0.03], [0.25, 0, 0.15], 20)
    R, t = compose(Rw, tw, Rb, tb)
    c = C.detect_board(wide_synth.render([(R, t)], BOARD), BOARD)
    assert c is not None
    assert np.abs(c - true_corners(WIDE, R, t, BOARD)).max() < 0.5


def test_canonical_order_undoes_reversal_and_mirror(wide_synth):
    Rw, tw = look_at([0, 0, 0], [0, 1, 0])
    Rb, tb = board_in_world([0.0, 0.35, 0.0], [0, 0, 0], 15)
    R, t = compose(Rw, tw, Rb, tb)
    gray = wide_synth.render([(R, t)], BOARD)
    truth = true_corners(WIDE, R, t, BOARD)
    g = truth.reshape(BOARD.rows, BOARD.cols, 2)
    for bad in (truth[::-1], g[:, ::-1].reshape(-1, 2), g[::-1].reshape(-1, 2)):
        assert np.allclose(C.canonical_order(bad, gray, BOARD), truth)


def test_no_board_returns_none(wide_synth):
    assert C.detect_board(wide_synth.render([], BOARD), BOARD) is None


# ── 내부 캘리브레이션 ─────────────────────────────────────────────────────────

def random_views(synth: SynthCam, cam: C.CamModel, n: int, seed: int,
                 dist_range=(0.22, 0.45)) -> list[C.Obs]:
    """화면 곳곳(가장자리 포함)에 여러 기울기로 놓은 판."""
    rng = np.random.default_rng(seed)
    Rw, tw = look_at([0, 0, 0], [0, 1, 0])
    w, h = cam.size
    out = []
    for _ in range(600):
        if len(out) >= n:
            break
        u, v = rng.uniform(0.06 * w, 0.94 * w), rng.uniform(0.08 * h, 0.92 * h)
        ray = np.append(C.undistort_normalized(np.array([[u, v]]), cam)[0], 1.0)
        pc = ray / np.linalg.norm(ray) * rng.uniform(*dist_range)
        pw = Rw.T @ (pc - tw)
        Rb, tb = board_in_world(pw, rng.normal(0, 0.25, 3), rng.uniform(0, 360))
        R, t = compose(Rw, tw, Rb, tb)
        tr = true_corners(cam, R, t, BOARD)
        if tr.min() < 4 or tr[:, 0].max() > w - 5 or tr[:, 1].max() > h - 5:
            continue
        c = C.detect_board(synth.render([(R, t)], BOARD), BOARD)
        if c is not None:
            out.append(C.Obs(len(out), c, 0.0))
    return out


def test_intrinsics_recovers_wide_lens(wide_synth):
    obs = random_views(wide_synth, WIDE, 45, seed=3)
    res = C.calibrate_intrinsics(obs, WIDE.size, BOARD, max_views=40)
    K = res.cam.K
    assert abs(K[0, 0] / 330.0 - 1) < 0.005
    assert abs(K[1, 1] / 331.0 - 1) < 0.005
    assert abs(K[0, 2] - 323.0) < 1.5 and abs(K[1, 2] - 178.0) < 1.5
    assert res.rms < 0.4 and res.holdout_rms < 0.6
    assert res.verdict()[0] == "good"
    # 계수 하나하나보다 "같은 3D 점을 같은 픽셀에 찍는가" 가 중요합니다.
    rng = np.random.default_rng(0)
    px = np.column_stack([rng.uniform(20, 620, 400), rng.uniform(15, 345, 400)])
    X = np.column_stack([C.undistort_normalized(px, WIDE), np.ones(400)])
    assert np.abs(C.project(X, res.cam) - px).max() < 0.5


def test_intrinsics_too_few_views():
    with pytest.raises(C.CalibError, match="장면이"):
        C.calibrate_intrinsics([C.Obs(0, np.zeros((54, 2)), 0.0)] * 5, (640, 360), BOARD)


def test_select_views_skips_duplicates():
    base = true_corners(WIDE, *compose(*look_at([0, 0, 0], [0, 1, 0]),
                                       *board_in_world([0, 0.35, 0], [0, 0, 0])), BOARD)
    other = base + [60, 0]
    obs = [C.Obs(i, base, 0.0) for i in range(50)] + [C.Obs(50, other, 0.0)]
    pick = C.select_views(obs, BOARD, WIDE.size, max_views=5)
    assert 50 in pick          # 하나뿐인 다른 장면은 반드시 뽑힘
    assert len(pick) <= 5


def test_verdict_flags_poor_coverage():
    r = C.IntrinsicsResult(cam=WIDE, rms=0.3, holdout_rms=0.4, views=30, dropped_views=0,
                           coverage=0.3, edge_coverage=0.1, coverage_map=np.zeros((6, 8), bool),
                           std_intrinsics={}, used_keys=[])
    level, msgs = r.verdict()
    assert level == "ok" and any("화면" in m for m in msgs)


def test_pose2sim_default_undistort_is_inaccurate_for_strong_distortion():
    """
    ★ 실측으로 찾은 함정: OpenCV undistortPoints 기본값(반복 5회)은 강한 통 왜곡의
      화면 구석에서 수 px 덜 폅니다. Pose2Sim 은 반복 횟수를 지정하지 않습니다.
      도구가 이 값을 재서 알려 주는지 확인합니다.
    """
    assert C.pose2sim_undistort_error(WIDE) > 1.0
    mild = C.CamModel(K=WIDE.K, dist=np.array([-0.05, 0.01, 0, 0, 0]), size=WIDE.size)
    assert C.pose2sim_undistort_error(mild) < 0.01


# ── 같은 순간 짝짓기 ──────────────────────────────────────────────────────────

def test_timed_observations_interpolate_to_grid_time():
    """판이 일정하게 움직일 때, 격자 시각의 꼭짓점 = 그 시각의 실제 위치."""
    step = 16_666_667
    t = 5_000_000_000 + np.arange(40, dtype=np.int64) * step + 7_000_000   # 7 ms 어긋남
    base = np.zeros((54, 2))
    vel = np.array([120.0, -30.0])                                        # px/초
    det = {k: base + vel * (t[k] / 1e9) for k in range(40)}
    grid = 5_000_000_000 + np.arange(1, 12, dtype=np.int64) * 50_000_000     # 영상 0.67초 안
    ob = C.timed_observations(det, t, grid)
    assert len(ob) == len(grid)
    for m, o in ob.items():
        assert np.allclose(o.corners, base + vel * (grid[m] / 1e9), atol=1e-6)
        assert o.speed == pytest.approx(math.hypot(*vel), rel=1e-6)


def test_timed_observations_skip_dropped_frame_and_use_frame_numbers():
    step = 16_666_667
    idx = np.array([0, 1, 2, 3, 5, 6, 7])              # 4번 프레임이 빠짐
    t = idx * step
    det = {int(i): np.full((54, 2), float(i)) for i in idx}
    grid = np.array([int(3.5 * step), int(5.5 * step)])
    ob = C.timed_observations(det, t.astype(np.int64), grid, idx)
    assert 0 not in ob                                 # 3→5 는 두 칸 간격이라 잇지 않음
    assert np.allclose(ob[1].corners, 5.5)             # 5, 6 번 프레임 사이


def test_frames_for_grid_maps_to_video_frame_numbers():
    idx = np.array([10, 11, 12, 13])
    t = np.array([0, 100, 200, 300], dtype=np.int64)
    assert C.frames_for_grid(t, np.array([150]), idx) == {11, 12}


# ── 외부: 번들 조정, 축척, 바닥 ───────────────────────────────────────────────

CAM_A = C.CamModel(K=np.array([[620.0, 0, 645], [0, 620.0, 356], [0, 0, 1]]),
                   dist=np.array([-0.18, 0.05, 0, 0, -0.005]), size=(1280, 720))
CAM_B = C.CamModel(K=np.array([[615.0, 0, 638], [0, 616.0, 362], [0, 0, 1]]),
                   dist=np.array([-0.17, 0.045, 0.0003, 0, -0.004]), size=(1280, 720))
POS_A, POS_B = np.array([0.0, 0.0, 1.0]), np.array([1.8, 0.4, 1.2])
LOOK_A, LOOK_B = look_at(POS_A, [0.3, 2.0, 0.55]), look_at(POS_B, [0.3, 2.0, 0.7])


def visible(cam, R, t, margin=10) -> np.ndarray | None:
    c = true_corners(cam, R, t, BOARD)
    w, h = cam.size
    if (R @ BOARD.center + t)[2] <= 0 or c.min() < margin or c[:, 0].max() > w - margin \
            or c[:, 1].max() > h - margin:
        return None
    return c


def synthetic_rig(n=60, seed=0, noise=0.15, cams=((CAM_A, LOOK_A), (CAM_B, LOOK_B)),
                  only=None):
    """
    판을 두 카메라 사이 공간 여기저기에 든 장면들 → 카메라별 {장면: Obs}.
    only[c] 가 주어지면 그 카메라는 해당 장면 번호만 봅니다.
    """
    rng = np.random.default_rng(seed)
    mid = (POS_A + POS_B) / 2
    obs = [dict() for _ in cams]
    m = 0
    while m < n:
        ctr = rng.uniform([0.0, 1.3, 0.6], [0.8, 2.2, 1.4])
        Rb, tb = board_in_world(ctr, mid + rng.normal(0, 0.3, 3), rng.normal(0, 30))
        seen = []
        for ci, (cam, (Rw, tw)) in enumerate(cams):
            R, t = compose(Rw, tw, Rb, tb)
            c = visible(cam, R, t)
            if c is not None and (only is None or m in only[ci]):
                seen.append((ci, c))
        if len(seen) >= 2 or (only is not None and seen):
            for ci, c in seen:
                obs[ci][m] = C.Obs(m, c + rng.normal(0, noise, c.shape), 0.0)
            m += 1
    return obs


def truth_rel(i: int):
    """기준 카메라(A) 좌표 → 카메라 i 좌표 정답."""
    (Ra, ta), (Ri, ti) = LOOK_A, [LOOK_A, LOOK_B][i]
    R = Ri @ Ra.T
    return R, ti - R @ ta


def test_bundle_adjust_recovers_relative_pose():
    obs = synthetic_rig()
    sol = C.bundle_adjust([CAM_A, CAM_B], obs, BOARD)
    R, t = truth_rel(1)
    assert rot_deg(sol.cam_R[1], R) < 0.05
    assert np.linalg.norm(sol.cam_t[1] - t) < 0.003          # 3 mm
    assert sol.rms < 0.3
    sc = C.scale_check([CAM_A, CAM_B], sol, obs, BOARD)
    assert abs(sc["errorPct"]) < 0.3


def test_bundle_adjust_drops_bad_observations():
    obs = synthetic_rig(seed=1)
    for m in list(obs[1])[:3]:                                # 꼭짓점이 엉뚱하게 찍힌 관측
        obs[1][m] = C.Obs(m, obs[1][m].corners + np.random.default_rng(m).normal(0, 15, (54, 2)), 0)
    sol = C.bundle_adjust([CAM_A, CAM_B], obs, BOARD)
    assert sol.dropped >= 3
    R, t = truth_rel(1)
    assert np.linalg.norm(sol.cam_t[1] - t) < 0.004


def test_too_few_shared_views_is_error():
    obs = synthetic_rig(n=10)
    with pytest.raises(C.CalibError, match="함께 본"):
        C.bundle_adjust([CAM_A, CAM_B], obs, BOARD)


def test_three_cameras_chain_through_middle_camera():
    """세 번째 카메라가 cam01 과는 판을 함께 못 보고 cam02 와만 봐도 이어 붙입니다."""
    pos_c = np.array([1.2, 3.6, 1.1])
    look_c = look_at(pos_c, [0.4, 1.8, 0.9])
    cams = ((CAM_A, LOOK_A), (CAM_B, LOOK_B), (CAM_B, look_c))
    first, second = set(range(0, 40)), set(range(40, 80))
    obs = synthetic_rig(n=80, seed=2, cams=cams,
                        only=[first, first | second, second])
    sol = C.bundle_adjust([CAM_A, CAM_B, CAM_B], obs, BOARD)
    Ra, ta = LOOK_A
    Rc, tc = look_c
    R = Rc @ Ra.T
    assert rot_deg(sol.cam_R[2], R) < 0.1
    assert np.linalg.norm(sol.cam_t[2] - (tc - R @ ta)) < 0.006


def floor_obs(obs, cam_i, cam, look, start_m, n, xy=(0.3, 1.3), seed=5):
    """바닥 판을 n 장면 동안 멈춰 둔 관측을 덧붙입니다."""
    rng = np.random.default_rng(seed)
    Rb, tb = floor_board(xy, 25)
    R, t = compose(*look, Rb, tb)
    c = visible(cam, R, t)
    assert c is not None, "바닥 판이 화면에 들어와야 합니다"
    for m in range(start_m, start_m + n):
        obs[cam_i][m] = C.Obs(m, c + rng.normal(0, 0.1, c.shape), 2.0)


def test_floor_gives_world_with_z_up_and_camera_heights():
    obs = synthetic_rig()
    grid = np.arange(200, dtype=np.int64) * (C.NS_PER_S // 8)
    sol = C.bundle_adjust([CAM_A, CAM_B], obs, BOARD)
    floor_obs(obs, 0, CAM_A, LOOK_A, 100, 20)
    fl = C.find_floor([CAM_A, CAM_B], sol, obs, grid, BOARD)
    assert fl is not None and fl.cam == 0 and fl.duration_s >= 2.0
    R_W, o = C.world_frame(fl.up, fl.point)
    poses = C.to_world(sol, R_W, o)
    ca, cb = (C.camera_center(R, t) for R, t in poses)
    assert ca[2] == pytest.approx(1.0, abs=0.01)
    assert cb[2] == pytest.approx(1.2, abs=0.01)
    assert np.linalg.norm(ca - cb) == pytest.approx(np.linalg.norm(POS_A - POS_B), abs=0.004)
    # 원점 = 바닥 판 가운데: 카메라 A 의 수평 거리가 정답과 같아야 함
    assert np.hypot(*ca[:2]) == pytest.approx(np.hypot(0.3, 1.3), abs=0.01)
    # 카메라 A 가 보는 방향이 월드 +Y 쪽 (수평 성분)
    fwd = poses[0][0].T @ np.array([0, 0, 1.0])
    assert fwd[1] > 0.9 and abs(fwd[0]) < 1e-6


def test_held_board_is_not_mistaken_for_floor():
    """손에 세워 들고 잠깐 멈춘 판은 바닥이 아닙니다 (위쪽 방향이 90도 가까이 다름)."""
    obs = synthetic_rig()
    sol = C.bundle_adjust([CAM_A, CAM_B], obs, BOARD)
    first = min(obs[0])
    for m in range(100, 120):
        obs[0][m] = C.Obs(m, obs[0][first].corners, 1.0)
    grid = np.arange(200, dtype=np.int64) * (C.NS_PER_S // 8)
    assert C.find_floor([CAM_A, CAM_B], sol, obs, grid, BOARD) is None


# ── Pose2Sim 형식 ─────────────────────────────────────────────────────────────

def test_calib_toml_follows_pose2sim_rules(tmp_path):
    """
    Pose2Sim 과 같은 식으로 읽어 투영했을 때 월드 점이 우리 모델과 같은 픽셀에 떨어져야
    합니다 (common.computeP: P = K·[Rodrigues(rotation) | translation],
    undistort_points=true 면 K 대신 getOptimalNewCameraMatrix).
    """
    poses = [LOOK_A, LOOK_B]
    text = C.calib_toml(["cam01", "cam02"], [CAM_A, CAM_B], poses, 0.42)
    p = tmp_path / "Calib.toml"
    p.write_text(text, encoding="utf-8")
    data = tomllib.loads(text)
    assert [c.name for c in PL.read_calib(p)] == ["cam01", "cam02"]
    X = np.array([[0.4, 1.8, 0.9], [0.1, 2.1, 0.2], [0.7, 1.5, 1.3]])
    for key, cam, (Rt, tt) in zip(["cam01", "cam02"], [CAM_A, CAM_B], poses):
        d = data[key]
        K = np.array(d["matrix"])
        dist = np.array(d["distortions"])
        R = cv2.Rodrigues(np.array(d["rotation"]))[0]
        T = np.array(d["translation"])
        assert d["size"] == [1280.0, 720.0]
        ours = C.project(X @ Rt.T + tt, cam)
        cvp, _ = cv2.projectPoints(X, np.array(d["rotation"]), T, K, dist)
        assert np.allclose(cvp.reshape(-1, 2), ours, atol=1e-6)
        S = [int(s) for s in d["size"]]
        optim = cv2.getOptimalNewCameraMatrix(K, dist, S, 1, S)[0]
        P = np.hstack([optim, np.zeros((3, 1))]) @ np.vstack([np.hstack([R, T[:, None]]), [0, 0, 0, 1]])
        xh = (P @ np.hstack([X, np.ones((3, 1))]).T).T
        und = cv2.undistortPoints(ours.reshape(-1, 1, 2), K, dist, None, optim,
                                  criteria=(3, 100, 1e-12)).reshape(-1, 2)
        assert np.allclose(xh[:, :2] / xh[:, 2:], und, atol=1e-4)
    assert data["metadata"]["error"] == pytest.approx(0.42)


def test_calib_toml_rejects_nan():
    bad = C.CamModel(K=CAM_A.K, dist=np.array([np.nan, 0, 0, 0, 0]), size=CAM_A.size)
    with pytest.raises(C.CalibError):
        C.calib_toml(["cam01"], [bad], [LOOK_A], 0.1)


def test_triangulate_matches_truth():
    X = np.array([[0.4, 1.8, 0.9], [0.1, 2.1, 0.2]])
    pts = [C.project(X @ R.T + t, cam) for cam, (R, t) in ((CAM_A, LOOK_A), (CAM_B, LOOK_B))]
    got = C.triangulate([CAM_A, CAM_B], [LOOK_A[0], LOOK_B[0]], [LOOK_A[1], LOOK_B[1]], pts)
    assert np.allclose(got, X, atol=1e-6)


def test_project_matches_opencv():
    rng = np.random.default_rng(0)
    Xc = np.column_stack([rng.uniform(-1, 1, 50), rng.uniform(-0.6, 0.6, 50), rng.uniform(1, 3, 50)])
    for cam in (CAM_A, WIDE, C.CamModel(K=CAM_A.K, dist=np.array(
            [0.1, -0.02, 0.001, 0.002, 0.003, 0.05, -0.01, 0.002]), size=CAM_A.size)):
        cvp, _ = cv2.projectPoints(Xc, np.zeros(3), np.zeros(3), cam.K, cam.dist)
        assert np.allclose(C.project(Xc, cam), cvp.reshape(-1, 2), atol=1e-8)
