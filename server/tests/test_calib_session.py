"""
캘리브레이션 촬영 → 렌즈 특성 → 리그, 처음부터 끝까지 (mocapsync.calib_session).

폰 대신 정답을 아는 가짜 영상(.mp4)과 사이드카를 uploads/<세션>/ 모양으로 만들어
도구가 실제로 쓰는 경로(파일 찾기 → 영상 읽기 → 시각 붙이기 → 계산 → 저장)를 그대로 탑니다.

★ 두 폰은 부팅 시각이 달라 폰 시계 값이 전혀 다르고(오프셋으로만 이어짐), 프레임을
  찍는 순간도 7 ms 어긋나게 만듭니다. 오프셋을 무시하거나 프레임 번호로 짝지으면
  같은 순간이 아니라서 결과가 틀어집니다.
"""
from __future__ import annotations

import json

import cv2
import numpy as np
import pytest

from mocapsync import calib as C
from mocapsync import calib_session as CS
from mocapsync import pipeline as PL

from calib_synth import SynthCam, board_in_world, compose, floor_board, look_at, true_corners
from test_calib import CAM_A, CAM_B, LOOK_A, LOOK_B, POS_A, POS_B
from test_sidecar import BASE_START_NS, valid_dict

#: 판이 너무 작게 찍히지 않게 한 칸 40 mm (도구의 --square-mm 로 지정하는 경우)
BOARD = C.Board(square_m=0.04)


def visible(cam, R, t, margin=10) -> bool:
    c = true_corners(cam, R, t, BOARD)
    w, h = cam.size
    return bool((R @ BOARD.center + t)[2] > 0 and c.min() >= margin
                and c[:, 0].max() <= w - margin and c[:, 1].max() <= h - margin)
DEV_A, DEV_B = "AAAA00000001", "BBBB00000002"
STEP = 16_666_666
OFFSET_A = 5_522_186_169_000
BASE_B = BASE_START_NS + 3_000_000_000_000        # 폰 B 는 다른 때 부팅
PHASE_B = 7_000_000                               # 폰 B 는 7 ms 늦게 찍음
OFFSET_B = OFFSET_A + BASE_START_NS - BASE_B      # 마스터 시각 = A 와 같은 축


def sidecar(dev: str, session: str, n: int, base: int, offset: int, phase: int = 0) -> dict:
    d = valid_dict(n)
    frames = [[k, base + phase + k * STEP] for k in range(n)]
    d.update(deviceId=dev, sessionId=session, width=1280, height=720, frames=frames,
             clockOffsetNs=offset, clockMeasuredAtNs=base - 1_000_000_000,
             firstFramePtsNs=frames[0][1], requestedStartAtSlaveNs=frames[0][1] - 1_000_000,
             requestedStartAtMasterNs=frames[0][1] - 1_000_000 + offset,
             cameraDeviceType="AVCaptureDeviceTypeBuiltInUltraWideCamera", fieldOfViewDeg=100.0)
    return d


def write_video(path, images_per_frame) -> None:
    wr = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 60, (1280, 720))
    assert wr.isOpened()
    for img in images_per_frame:
        wr.write(cv2.cvtColor(img, cv2.COLOR_GRAY2BGR))
    wr.release()


def held_poses_video(synth: SynthCam, look, holds, t_frames_s) -> list[np.ndarray]:
    """
    holds: [(시작초, 끝초, 판→월드 자세 또는 None)]. 판은 구간마다 멈춰 있고 사이에 순간이동
    합니다 (움직이는 프레임은 속도 검사가 버리므로 그릴 필요가 없음).
    """
    cache = {}
    out = []
    for ts in t_frames_s:
        idx = next((i for i, (a, b, _) in enumerate(holds) if a <= ts < b), None)
        if idx not in cache:
            pose = holds[idx][2] if idx is not None else None
            boards = [] if pose is None else [compose(*look, *pose)]
            cache[idx] = synth.render(boards, BOARD)
        out.append(cache[idx])
    return out


@pytest.fixture(scope="module")
def sessions(tmp_path_factory):
    root = tmp_path_factory.mktemp("calib")
    up = root / "uploads"
    syn = {DEV_A: SynthCam(CAM_A, seed=11), DEV_B: SynthCam(CAM_B, seed=12)}
    rng = np.random.default_rng(7)

    # ── 1) 렌즈 특성 촬영: 폰마다 판을 가까이 여러 자세로 (0.2초씩 멈춤) ──
    s1 = up / "S20261001-100000"
    s1.mkdir(parents=True)
    for dev, cam, base, off in ((DEV_A, CAM_A, BASE_START_NS, OFFSET_A),
                                (DEV_B, CAM_B, BASE_B, OFFSET_B)):
        Rw, tw = look_at([0, 0, 0], [0, 1, 0])
        holds, ts = [], 0.0
        while len(holds) < 30:
            u, v = rng.uniform(80, 1200), rng.uniform(60, 660)
            ray = np.append(C.undistort_normalized(np.array([[u, v]]), cam)[0], 1.0)
            pw = Rw.T @ (ray / np.linalg.norm(ray) * rng.uniform(0.45, 0.9) - tw)
            pose = board_in_world(pw, rng.normal(0, 0.3, 3), rng.uniform(0, 360), BOARD)
            if not visible(cam, *compose(Rw, tw, *pose), margin=6):
                continue
            holds.append((ts, ts + 0.2, pose))
            ts += 0.2
        n = int(ts * 60)
        write_video(s1 / f"{dev}.mp4", held_poses_video(syn[dev], (Rw, tw), holds,
                                                        np.arange(n) / 60.0))
        (s1 / f"{dev}.json").write_text(json.dumps(sidecar(dev, s1.name, n, base, off)),
                                        encoding="utf-8")

    # ── 2) 위치·방향 촬영: 두 폰 동시, 판을 둘 다 보이는 곳에서 → 마지막에 바닥 ──
    s2 = up / "S20261001-101500"
    s2.mkdir()
    mid = (POS_A + POS_B) / 2
    holds, ts = [], 0.0
    while len(holds) < 36:
        ctr = rng.uniform([0.0, 1.3, 0.6], [0.8, 2.2, 1.4])
        pose = board_in_world(ctr, mid + rng.normal(0, 0.3, 3), rng.normal(0, 30), BOARD)
        if all(visible(cam, *compose(*look, *pose))
               for cam, look in ((CAM_A, LOOK_A), (CAM_B, LOOK_B))):
            holds.append((ts, ts + 0.25, pose))
            ts += 0.25
    ts += 0.3                                             # 판을 내려놓는 동안 (안 보임)
    fb = floor_board((0.3, 1.3), 25, BOARD)
    assert visible(CAM_A, *compose(*LOOK_A, *fb)), "바닥 판이 cam01 화면에 들어와야 합니다"
    holds.append((ts, ts + 2.5, fb))
    n = int((ts + 2.5) * 60)
    for dev, look, base, off, phase in ((DEV_A, LOOK_A, BASE_START_NS, OFFSET_A, 0),
                                        (DEV_B, LOOK_B, BASE_B, OFFSET_B, PHASE_B)):
        t_s = (np.arange(n) * STEP + phase) / 1e9
        write_video(s2 / f"{dev}.mp4", held_poses_video(syn[dev], look, holds, t_s))
        (s2 / f"{dev}.json").write_text(json.dumps(sidecar(dev, s2.name, n, base, off, phase)),
                                        encoding="utf-8")
    return root, s1, s2


def test_intrinsics_then_extrinsics_end_to_end(sessions):
    root, s1, s2 = sessions
    work = root / "work"
    lines: list[str] = []

    outs = CS.run_intrinsics(s1, work, BOARD, log=lines.append)
    assert [o.device_id for o in outs] == [DEV_A, DEV_B]
    for o, cam in zip(outs, (CAM_A, CAM_B)):
        assert o.saved is not None, "\n".join(lines)
        K = np.array(o.record["K"])
        assert abs(K[0, 0] / cam.K[0, 0] - 1) < 0.01
        assert o.record["rmsPx"] < 0.5
        assert o.record["holdoutRmsPx"] is not None and o.record["holdoutRmsPx"] < 0.6
        assert (work / "intrinsics" / f"{o.device_id}_coverage.jpg").is_file()

    oc = CS.run_extrinsics(s2, work, BOARD, log=lines.append)
    assert oc.rig_dir is not None, "\n".join(lines)
    rep = oc.report
    assert rep["floor"] is not None and rep["floor"]["camera"] == "cam01"
    assert rep["cameras"]["cam01"]["heightM"] == pytest.approx(1.0, abs=0.02)
    assert rep["cameras"]["cam02"]["heightM"] == pytest.approx(1.2, abs=0.02)
    assert rep["distancesM"]["cam01-cam02"] == pytest.approx(
        float(np.linalg.norm(POS_A - POS_B)), abs=0.01)
    assert abs(rep["scaleCheck"]["errorPct"]) < 1.0
    assert rep["rmsPx"] < 1.0

    # ★ cameras.json 은 도구가 사이드카의 deviceId 로 씁니다
    assert json.loads((oc.rig_dir / "cameras.json").read_text()) == {"cam01": DEV_A, "cam02": DEV_B}
    assert (oc.rig_dir / "check_cam01.jpg").is_file() and (oc.rig_dir / "check_cam02.jpg").is_file()

    # 러너가 이 리그를 그대로 받아들이는지 (순서·해상도 대조까지)
    rig = PL.load_rig(oc.rig_dir)
    cams = PL.assign_cameras(PL.find_uploads(s2), rig)
    assert [c.name for c in cams] == ["cam01", "cam02"]
    assert [c.device_id for c in cams] == [DEV_A, DEV_B]
    assert not [i for i in PL.check_calibration(rig, cams) if i.severity == "fatal"]

    # 다시 돌리면 이전 리그는 rig_backup/ 으로 (rig/ 에 .toml 이 둘이 되면 러너가 멈춤)
    CS.run_extrinsics(s2, work, BOARD, log=lines.append, stamp="again")
    assert (work / "rig_backup" / "again" / "Calib.toml").is_file()
    assert len(list((work / "rig").glob("*.toml"))) == 1


def test_extrinsics_needs_intrinsics_first(sessions, tmp_path):
    _, _, s2 = sessions
    with pytest.raises(C.CalibError, match="렌즈 특성이 없습니다"):
        CS.run_extrinsics(s2, tmp_path, BOARD, log=lambda s: None)


def test_extrinsics_refuses_intrinsics_from_other_settings(sessions, tmp_path):
    _, _, s2 = sessions
    for dev in (DEV_A, DEV_B):
        rec = {"deviceId": dev, "cameraDeviceType": "AVCaptureDeviceTypeBuiltInUltraWideCamera",
               "width": 1280, "height": 720, "targetFps": 120, "isBinned": False,
               "K": CAM_A.K.tolist(), "dist": CAM_A.dist.tolist()}
        C.save_intrinsics(tmp_path / "intrinsics", rec, "x")
    with pytest.raises(C.CalibError, match="targetFps"):
        CS.run_extrinsics(s2, tmp_path, BOARD, log=lambda s: None)


def test_extrinsics_needs_two_phones(sessions, tmp_path):
    _, s1, _ = sessions
    one = tmp_path / "one"
    one.mkdir()
    for ext in (".mp4", ".json"):
        (one / f"{DEV_A}{ext}").write_bytes((s1 / f"{DEV_A}{ext}").read_bytes())
    with pytest.raises(C.CalibError, match="동시에"):
        CS.run_extrinsics(one, tmp_path, BOARD, log=lambda s: None)
