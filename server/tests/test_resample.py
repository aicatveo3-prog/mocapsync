"""
키포인트 리샘플러를 **정답을 아는 합성 데이터**로 시험합니다.

방식
----
관절이 알려진 공식 x(t), y(t) 로 움직인다고 두고, 카메라 두 대가 서로 다른
프레임 위상·서로 다른 클럭 오프셋으로 그걸 찍었다고 가정합니다.
리샘플러가 공통 격자 시각에서 그 공식의 값을 얼마나 정확히 되찾는지 잽니다.

★ 이 테스트가 증명하는 것
"가장 가까운 프레임을 그냥 쓰는 것"(보간 없음)과 비교해 오차가 얼마나 줄어드는지.
이 차이가 이 프로젝트의 존재 이유입니다.

★ 사람이 두 명인 경우도 시험합니다. 처음 구현은 한 명만 골랐는데, 공식 데모
  (배경에 다른 사람이 있음)에서 엉뚱한 사람을 골라 Pose2Sim 이 카메라 하나를
  통째로 버렸습니다. 합성 데이터에 사람이 한 명뿐이라 못 잡았던 사각지대입니다.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pytest

from mocapsync import resample as R
from mocapsync import sidecar as sc

FPS = 60
FRAME_NS = 1_000_000_000 / FPS
K = 26
BASE = 1_000_000_000_000        # 공통 시계로 1000초 무렵


def truth(t_s: np.ndarray, k: int, who: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """사람 who 의 관절 k 진짜 위치 (px). 2 Hz 로 크게 흔드는 팔 정도의 움직임."""
    ph = k * 0.37 + who * 1.1
    cx = 960 if who == 0 else 400
    x = cx + 300 * np.sin(2 * math.pi * 2.0 * t_s + ph)
    y = 540 + 180 * np.cos(2 * math.pi * 1.3 * t_s + ph)
    return x, y


def write_camera(tmp, name, offset_ns, phase_ns, n_frames=300, device="DEV",
                 drop=(), hide=None, outlier=None, nan_slot=True,
                 second_person=False, swap_at=None):
    """
    카메라 한 대의 pose JSON 폴더와 사이드카를 만듭니다.

    offset_ns : 마스터 = 이 카메라 시계 + offset
    phase_ns  : 이 카메라의 프레임 위상 (마스터 시계 기준)
    drop      : 카메라가 버린 프레임 (영상에도 사이드카에도 없음 — 발열 드롭)
    hide      : (시작, 끝) 프레임 구간에서 관절 0 을 안 보이게 (가림)
    outlier   : (프레임, 관절, dx, 신뢰도) 한 점을 튀게
    second_person : 두 번째 사람을 다른 슬롯에 (데모처럼)
    swap_at   : 이 프레임부터 추적기가 두 사람의 슬롯을 바꿔 붙임 (ID 뒤바뀜)
    """
    d = tmp / "pose" / f"{name}_json"
    d.mkdir(parents=True)
    frames, idx = [], 0
    for n in range(n_frames):
        if n in drop:
            continue
        t_master = BASE + phase_ns + n * FRAME_NS
        t_slave = int(round(t_master - offset_ns))
        frames.append([idx, t_slave])
        ts = (t_master - BASE) / 1e9

        def person(who):
            kp = []
            for k in range(K):
                x, y = truth(np.array([ts]), k, who)
                c = 0.9
                xv, yv = float(x[0]), float(y[0])
                if who == 0 and hide and k == 0 and hide[0] <= n < hide[1]:
                    xv = yv = c = float("nan")
                if who == 0 and outlier and outlier[0] == n and outlier[1] == k:
                    xv += outlier[2]
                    c = outlier[3]
                kp += [xv, yv, c]
            return {"person_id": [-1], "pose_keypoints_2d": kp}

        people = []
        if nan_slot:   # 실측처럼 빈 NaN 슬롯을 앞에 끼워 넣습니다
            people.append({"person_id": [-1], "pose_keypoints_2d": [float("nan")] * (3 * K)})
        p0 = person(0)
        if second_person:
            p1 = person(1)
            if swap_at is not None and n >= swap_at:
                p0, p1 = p1, p0
            people += [p0, p1]
        else:
            people.append(p0)
        (d / f"{name}_{idx:06d}.json").write_text(
            json.dumps({"version": 1.3, "people": people}), encoding="utf-8")
        idx += 1

    side = {
        "schemaVersion": 1, "deviceId": device, "sessionId": "S-TEST",
        "clock": "test", "clockOffsetNs": int(offset_ns),
        "clockUncertaintyNs": 1_700_000, "clockMinRttNs": 3_400_000,
        "clockMeasuredAtNs": frames[0][1] - 1_000_000_000,
        "sleepAtSyncNs": 0, "sleepAtRecordStartNs": 42,
        "targetFps": FPS, "width": 1920, "height": 1080,
        "stabilization": "off", "frames": frames,
    }
    sp = tmp / f"{device}.json"
    sp.write_text(json.dumps(side), encoding="utf-8")
    return d, sp


def subject(g4: np.ndarray) -> np.ndarray:
    """(G, S, K, 3) 에서 주 피험자 슬롯 (G, K, 3)."""
    return g4[:, R.main_slot(g4)]


def err_vs_truth(grid, kp_grid, keypoints=range(K), who=0):
    ts = (grid - BASE) / 1e9
    errs = []
    for k in keypoints:
        x, y = truth(ts, k, who)
        ok = np.isfinite(kp_grid[:, k, 0])
        errs.append(np.hypot(kp_grid[ok, k, 0] - x[ok], kp_grid[ok, k, 1] - y[ok]))
    return np.concatenate(errs)


def two_cams(tmp, **kw_a):
    a = write_camera(tmp, "cam01", offset_ns=37_500_000, phase_ns=0, device="A", **kw_a)
    b = write_camera(tmp, "cam02", offset_ns=-812_250_000, phase_ns=7.3e6, device="B")
    return a, b


def run(pairs, method=R.DEFAULT_METHOD, **kw):
    tracks = []
    for jd, sp in pairs:
        n, kp = R.load_pose_dir(jd)
        tracks.append(R.attach_timestamps(jd.name, n, kp, sc.Sidecar.load(sp)))
    grid = R.common_grid(tracks)
    outs = [R.resample_track(t, grid, method=method, **kw) for t in tracks]
    return grid, tracks, outs


def all_err(grid, outs):
    return np.concatenate([err_vs_truth(grid, subject(g)) for g, _ in outs])


# ── 핵심: 정확도 ─────────────────────────────────────────────────────────────

def test_cubic_recovers_truth_far_better_than_nearest(tmp_path):
    """
    ★ 이 프로젝트의 핵심 주장.
    가장 가까운 프레임을 그냥 쓰면 수십 px 틀리는 빠른 움직임에서,
    곡선 보간은 1 px 아래로 맞춥니다.
    """
    pairs = two_cams(tmp_path)
    grid, _, cub = run(pairs, "cubic")
    _, _, near = run(pairs, "nearest")
    e_cub, e_near = all_err(grid, cub), all_err(grid, near)
    assert np.percentile(e_cub, 99) < 1.0, f"cubic 99% {np.percentile(e_cub, 99):.3f}px"
    assert np.max(e_near) > 10, "비교 기준(보간 없음)의 오차가 예상보다 작습니다"
    assert np.mean(e_near) / np.mean(e_cub) > 20


def test_linear_is_between(tmp_path):
    pairs = two_cams(tmp_path)
    grid, _, lin = run(pairs, "linear")
    _, _, cub = run(pairs, "cubic")
    assert np.mean(all_err(grid, cub)) < np.mean(all_err(grid, lin))


def test_default_makima_is_accurate_on_smooth_motion(tmp_path):
    """
    기본값 makima 는 실측 데이터(튀는 점)에 강해서 골랐습니다.
    매끈한 움직임에서도 1 px 아래로 맞추는지 확인합니다 — 한쪽만 잘하면 안 됩니다.
    """
    assert R.DEFAULT_METHOD == "makima"
    pairs = two_cams(tmp_path)
    grid, _, outs = run(pairs)
    _, _, lin = run(pairs, "linear")
    e, e_lin = all_err(grid, outs), all_err(grid, lin)
    assert np.percentile(e, 99) < 1.0, f"makima 99% {np.percentile(e, 99):.3f}px"
    assert np.mean(e) < np.mean(e_lin) / 3


def test_makima_resists_high_confidence_spike_better_than_cubic(tmp_path):
    """
    신뢰도가 높은데도 한 프레임 튀는 경우(임계값으로 못 거름).
    cubic 은 튄 점 주변 여러 프레임을 출렁이게 하고, makima 는 덜 번집니다.
    """
    pairs = two_cams(tmp_path, outlier=(150, 20, 80.0, 0.9))
    grid, tracks, cub = run(pairs, "cubic")
    _, _, mak = run(pairs, "makima")
    t_spike = tracks[0].t_ns[150]
    far = np.abs(grid - t_spike) > 1.6 * FRAME_NS
    e_c = err_vs_truth(grid[far], subject(cub[0][0])[far], keypoints=[20])
    e_m = err_vs_truth(grid[far], subject(mak[0][0])[far], keypoints=[20])
    assert np.max(e_m) <= np.max(e_c)


def test_both_cameras_land_on_same_instants(tmp_path):
    """두 카메라의 시계가 850 ms 어긋나 있어도, 격자 k 번째는 두 카메라 모두 같은 순간."""
    pairs = two_cams(tmp_path)
    grid, _, outs = run(pairs)
    ga, gb = subject(outs[0][0]), subject(outs[1][0])
    ok = np.isfinite(ga[:, 5, 0]) & np.isfinite(gb[:, 5, 0])
    # 합성 데이터는 두 카메라가 같은 평면을 보므로, 같은 순간이면 같은 좌표입니다
    assert np.max(np.abs(ga[ok, 5, :2] - gb[ok, 5, :2])) < 1.0


def test_shift_stats_reflect_subframe_offset(tmp_path):
    pairs = two_cams(tmp_path)
    _, _, outs = run(pairs)
    shifts = sorted(o[1].shift_ms_max for o in outs)
    # 격자는 늦게 시작한 카메라(B, 위상 7.3 ms)의 첫 프레임에서 시작합니다.
    # 그래서 B 는 격자와 위상이 같고, A 는 7.3 ms 어긋납니다.
    assert shifts[0] < 0.01
    assert 6.5 < shifts[1] < 8.34       # 7.3 ms 와 9.37 ms 중 가까운 쪽


def test_unknown_method_is_error(tmp_path):
    pairs = two_cams(tmp_path)
    with pytest.raises(ValueError, match="보간법"):
        run(pairs, "magic")


# ── 사람이 여럿 ──────────────────────────────────────────────────────────────

def test_two_people_are_kept_separate(tmp_path):
    """
    ★ 데모에서 드러난 버그의 재현.
    두 사람 모두 관절 26개가 다 잡히면 '관절이 가장 많은 한 명'이 무의미합니다.
    슬롯별로 따로 이어서 두 사람 모두 자기 궤적을 유지해야 합니다.
    """
    a = write_camera(tmp_path, "cam01", 37_500_000, 0, device="A", second_person=True)
    b = write_camera(tmp_path, "cam02", -812_250_000, 7.3e6, device="B")
    grid, _, outs = run([a, b])
    g = outs[0][0]                          # (G, S, K, 3)
    assert g.shape[1] == 3                  # NaN 슬롯 + 두 사람
    e0 = err_vs_truth(grid, g[:, 1], who=0)
    e1 = err_vs_truth(grid, g[:, 2], who=1)
    assert np.percentile(e0, 99) < 1.0
    assert np.percentile(e1, 99) < 1.0, "두 번째 사람이 망가졌습니다"


def test_both_people_are_written(tmp_path):
    """누가 피험자인지는 Pose2Sim 이 정합니다. 우리는 둘 다 넘겨야 합니다."""
    a = write_camera(tmp_path, "cam01", 0, 0, device="A", second_person=True)
    b = write_camera(tmp_path, "cam02", 0, 5e6, device="B")
    out = tmp_path / "pose-sync"
    R.resample_session([a, b], out)
    fr = json.loads(sorted((out / "cam01_json").glob("*.json"))[10].read_text())
    assert len(fr["people"]) == 2, "빈 슬롯은 빼고 두 사람은 모두 써야 합니다"


def test_track_id_swap_is_cut_not_bridged(tmp_path):
    """
    추적기가 중간에 두 사람의 슬롯을 바꿔 붙이면 좌표가 한 프레임에 수백 px 뜁니다.
    그걸 곡선으로 이으면 두 사람 사이를 날아다니는 가짜 관절이 됩니다. 끊어야 합니다.
    """
    a = write_camera(tmp_path, "cam01", 0, 0, device="A",
                     second_person=True, swap_at=150)
    b = write_camera(tmp_path, "cam02", 0, 7.3e6, device="B")
    grid, tracks, outs = run([a, b])
    g, st = outs[0]
    assert st.jump_cuts > 0
    t_swap_lo, t_swap_hi = tracks[0].t_ns[149], tracks[0].t_ns[150]
    between = (grid > t_swap_lo) & (grid < t_swap_hi)
    assert between.any()
    # 뒤바뀌는 순간 사이의 격자는 비워 둡니다 (두 사람 사이 가짜 좌표 금지)
    assert np.all(np.isnan(g[between, 1, :, 2]))
    # 뒤바뀌기 전/후는 각자 정확해야 합니다
    before = grid < t_swap_lo
    e = err_vs_truth(grid[before], g[before, 1], who=0)
    assert np.percentile(e, 99) < 1.0


def test_fast_real_motion_is_not_cut(tmp_path):
    """
    끊는 기준이 너무 작으면 빠른 실제 움직임까지 끊깁니다.
    한 프레임 44 px(손목 5 m/s 상당) 으로 움직여도 끊지 않아야 합니다.
    """
    pairs = two_cams(tmp_path)
    _, _, outs = run(pairs)
    assert outs[0][1].jump_cuts == 0


# ── 격자 ─────────────────────────────────────────────────────────────────────

def test_grid_is_inside_overlap_only(tmp_path):
    """외삽하지 않습니다 — 모든 카메라가 찍고 있던 구간만."""
    pairs = two_cams(tmp_path)
    grid, tracks, _ = run(pairs)
    assert grid[0] >= max(t.t_ns[0] for t in tracks)
    assert grid[-1] <= min(t.t_ns[-1] for t in tracks)
    assert np.all(np.abs(np.diff(grid) - FRAME_NS) <= 1)


def test_no_overlap_is_error(tmp_path):
    a = write_camera(tmp_path, "cam01", 0, 0, device="A")
    # 공통 시계로 1시간 뒤에 찍은 카메라 (위상을 1시간 밀어야 합니다 —
    # 오프셋만 바꾸면 공통 시각은 그대로라 여전히 겹칩니다)
    b = write_camera(tmp_path, "cam02", 0, 3600e9, device="B")
    tracks = []
    for jd, sp in (a, b):
        n, kp = R.load_pose_dir(jd)
        tracks.append(R.attach_timestamps(jd.name, n, kp, sc.Sidecar.load(sp)))
    with pytest.raises(R.ResampleError, match="동시에 찍은 구간"):
        R.common_grid(tracks)


# ── 공백·드롭·이상치 ─────────────────────────────────────────────────────────

def test_long_gap_is_not_bridged(tmp_path):
    """1초 가려진 관절을 곡선으로 지어내지 않습니다."""
    pairs = two_cams(tmp_path, hide=(100, 160))
    grid, tracks, outs = run(pairs)
    g, st = outs[0]
    ga = subject(g)
    ta = tracks[0].t_ns
    inside_gap = (grid > ta[99]) & (grid < ta[160])
    assert inside_gap.sum() > 50
    assert np.all(np.isnan(ga[inside_gap, 0, 2])), "긴 공백을 이어 붙였습니다"
    assert np.all(np.isfinite(ga[inside_gap, 1, 2]))   # 다른 관절은 멀쩡
    assert st.max_bridged_gap_ms <= R.DEFAULT_MAX_GAP_NS / 1e6


def test_short_gap_is_bridged_accurately(tmp_path):
    pairs = two_cams(tmp_path, hide=(100, 101))
    grid, _, outs = run(pairs)
    ga = subject(outs[0][0])
    assert np.all(np.isfinite(ga[:, 0, 2]))
    assert np.percentile(err_vs_truth(grid, ga, keypoints=[0]), 99) < 3.0


def test_dropped_frames_are_compensated_by_timestamps(tmp_path):
    """
    ★ 발열로 카메라가 프레임을 버리면 영상에도 사이드카에도 그 프레임이 없습니다.
      프레임 번호로 짝지으면 그 뒤가 전부 한 프레임씩 밀립니다.
      시각으로 이으면 영향이 없습니다.
    """
    pairs = two_cams(tmp_path, drop={50, 120, 121})
    grid, _, outs = run(pairs)
    assert np.percentile(err_vs_truth(grid, subject(outs[0][0])), 99) < 3.0


def test_low_confidence_outlier_does_not_bend_neighbours(tmp_path):
    """
    실측에서 발가락이 한 프레임 259 px 튄 적이 있습니다 (신뢰도 0.31).
    신뢰도가 임계값 아래인 점은 곡선에 넣지 않으므로 이웃 프레임이 멀쩡해야 합니다.
    """
    pairs = two_cams(tmp_path, outlier=(150, 20, 259.0, 0.2))
    grid, _, outs = run(pairs)
    e = err_vs_truth(grid, subject(outs[0][0]), keypoints=[20])
    assert np.max(e) < 3.0, f"이상치가 곡선을 휘었습니다 (최대 {np.max(e):.1f}px)"


# ── 읽기 ─────────────────────────────────────────────────────────────────────

def test_load_keeps_slots(tmp_path):
    jd, _ = write_camera(tmp_path, "cam01", 0, 0, device="A", second_person=True)
    n, kp = R.load_pose_dir(jd)
    assert kp.shape == (300, 3, K, 3)
    assert np.all(np.isnan(kp[:, 0]))          # 빈 NaN 슬롯
    assert np.all(np.isfinite(kp[:, 1, :, 2]))


def test_pick_person_skips_nan_slots():
    nan = {"pose_keypoints_2d": [float("nan")] * 9}
    real = {"pose_keypoints_2d": [1, 2, 0.9, 3, 4, 0.8, 5, 6, 0.7]}
    p = R.pick_person([nan, nan, real])
    assert p is not None and p[0, 0] == 1


def test_pick_person_none_when_empty():
    assert R.pick_person([]) is None
    assert R.pick_person([{"pose_keypoints_2d": [float("nan")] * 9}]) is None


def test_frame_number_parse(tmp_path):
    assert R.frame_number(tmp_path / "cam01_000123.json") == 123
    # Pose2Sim 과 같은 규칙: 마지막 숫자 = 프레임 번호
    assert R.frame_number(tmp_path / "cam01.json") == 1
    with pytest.raises(R.ResampleError):
        R.frame_number(tmp_path / "camera.json")


def test_json_more_frames_than_sidecar_is_error(tmp_path):
    """영상과 사이드카가 어긋나면 모든 시각이 밀립니다. 경고가 아니라 오류."""
    jd, sp = write_camera(tmp_path, "cam01", 0, 0, device="A")
    d = json.loads(sp.read_text())
    d["frames"] = d["frames"][:-10]
    sp.write_text(json.dumps(d))
    n, kp = R.load_pose_dir(jd)
    with pytest.raises(R.ResampleError, match="많습니다"):
        R.attach_timestamps(jd.name, n, kp, sc.Sidecar.load(sp))


def test_partial_json_range_is_warning_not_error(tmp_path):
    """Pose2Sim 을 영상 일부 구간만 돌린 경우 — 프레임 번호로 맞추면 됩니다."""
    jd, sp = write_camera(tmp_path, "cam01", 0, 0, device="A")
    for f in sorted(jd.glob("*.json"))[:20]:
        f.unlink()
    n, kp = R.load_pose_dir(jd)
    tr = R.attach_timestamps(jd.name, n, kp, sc.Sidecar.load(sp))
    assert tr.warnings
    assert tr.t_ns.size == 280


# ── 쓰기 ─────────────────────────────────────────────────────────────────────

def test_written_json_has_no_nan_and_zero_for_missing():
    kp = np.full((K, 3), np.nan)
    kp[3] = [10.0, 20.0, 0.8]
    fr = R.openpose_frame(kp)
    s = json.dumps(fr)
    json.loads(s, parse_constant=lambda c: pytest.fail(f"비표준 JSON 상수 {c}"))
    flat = fr["people"][0]["pose_keypoints_2d"]
    assert flat[9:12] == [10.0, 20.0, 0.8]
    assert flat[0:3] == [0.0, 0.0, 0.0]


def test_empty_frame_has_no_people():
    assert R.openpose_frame(np.full((2, K, 3), np.nan))["people"] == []


def test_session_writes_pose_sync_layout(tmp_path):
    """Pose2Sim 이 pose-sync/<camNN_json>/<camNN>_000000.json 을 번호 순으로 짝짓습니다."""
    pairs = two_cams(tmp_path)
    out = tmp_path / "pose-sync"
    res = R.resample_session(pairs, out)
    for name in ("cam01_json", "cam02_json"):
        files = sorted((out / name).glob("*.json"))
        assert len(files) == res.grid_ns.size
        assert files[0].name == f"{name[:-5]}_000000.json"
        assert R.frame_number(files[-1]) == res.grid_ns.size - 1
    rep = json.loads((out / "resample_report.json").read_text(encoding="utf-8"))
    assert rep["gridPoints"] == res.grid_ns.size
    assert len(rep["cameras"]) == 2


def test_duplicate_device_is_error(tmp_path):
    a = write_camera(tmp_path, "cam01", 0, 0, device="SAME")
    d2 = tmp_path / "b"
    d2.mkdir()
    b = write_camera(d2, "cam02", 0, 5e6, device="SAME")
    with pytest.raises(R.ResampleError, match="두 번"):
        R.resample_session([a, b], None)
