"""
4단계 파이프라인 준비물(mocapsync.pipeline) 시험. Pose2Sim 없이 돕니다.

핵심은 **조용히 틀리는 경우를 멈추는지**입니다.
 - 영상/사이드카 짝이 안 맞음, 업로드가 덜 끝남
 - 캘리브레이션의 카메라 순서가 cam01, cam02 순이 아님 (Pose2Sim 은 순서로 짝지음)
 - 이번 촬영의 폰이 캘리브레이션에 없음 / 해상도가 다름
 - 다시 돌릴 때 옛 3D 결과가 남음
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mocapsync import pipeline as PL

from test_sidecar import valid_dict

DEV_A, DEV_B = "AAAA00000001", "BBBB00000002"


def calib_text(names, size=(1920, 1080), keys=None):
    keys = keys or names
    out = ["[metadata]", 'adjusted = false', ""]
    for k, n in zip(keys, names):
        out += [f"[{k}]", f'name = "{n}"', f"size = [ {size[0]}.0, {size[1]}.0]",
                "matrix = [ [ 1500.0, 0.0, 960.0], [ 0.0, 1500.0, 540.0], [ 0.0, 0.0, 1.0]]",
                "distortions = [ 0.0, 0.0, 0.0, 0.0]",
                "rotation = [ 0.0, 0.0, 0.0]", "translation = [ 0.0, 0.0, 3.0]",
                "fisheye = false", ""]
    return "\n".join(out)


def make_upload(root: Path, devices=(DEV_A, DEV_B), session="S20260930-120000",
                ext=".mov", size=(1920, 1080)) -> Path:
    d = root / "uploads" / session
    d.mkdir(parents=True)
    for dev in devices:
        sd = valid_dict(600)
        sd["deviceId"], sd["sessionId"] = dev, session
        sd["width"], sd["height"] = size
        (d / f"{dev}.json").write_text(json.dumps(sd), encoding="utf-8")
        (d / f"{dev}{ext}").write_bytes(b"fake video " + dev.encode())
    return d


def make_rig(root: Path, mapping: dict, names=None, size=(1920, 1080)) -> Path:
    r = root / "rig"
    r.mkdir()
    (r / "Calib.toml").write_text(calib_text(names or list(mapping), size), encoding="utf-8")
    (r / "cameras.json").write_text(json.dumps(mapping), encoding="utf-8")
    return r


# ── 업로드 찾기 ───────────────────────────────────────────────────────────────

def test_find_uploads_pairs_by_stem(tmp_path):
    d = make_upload(tmp_path)
    ups = PL.find_uploads(d)
    assert [u.device_id for u in ups] == [DEV_A, DEV_B]
    assert all(u.video.suffix == ".mov" and u.sidecar_path.suffix == ".json" for u in ups)


def test_video_without_sidecar_is_error(tmp_path):
    d = make_upload(tmp_path)
    (d / f"{DEV_B}.json").unlink()
    with pytest.raises(PL.PipelineError, match="사이드카 없는 영상"):
        PL.find_uploads(d)


def test_sidecar_without_video_is_error(tmp_path):
    d = make_upload(tmp_path)
    (d / f"{DEV_A}.mov").unlink()
    with pytest.raises(PL.PipelineError, match="영상 없는 사이드카"):
        PL.find_uploads(d)


def test_partial_upload_is_error(tmp_path):
    d = make_upload(tmp_path)
    (d / f"{DEV_A}.mov.part").write_bytes(b"x")
    with pytest.raises(PL.PipelineError, match="끝나지 않은"):
        PL.find_uploads(d)


def test_device_id_comes_from_sidecar_not_filename(tmp_path):
    d = make_upload(tmp_path, devices=(DEV_A,))
    (d / f"{DEV_A}.json").rename(d / "renamed.json")
    (d / f"{DEV_A}.mov").rename(d / "renamed.mov")
    assert PL.find_uploads(d)[0].device_id == DEV_A


# ── 카메라 번호 ───────────────────────────────────────────────────────────────

def test_without_rig_cameras_follow_device_order(tmp_path):
    ups = PL.find_uploads(make_upload(tmp_path, devices=(DEV_B, DEV_A)))
    cams = PL.assign_cameras(ups, None)
    assert [(c.name, c.device_id) for c in cams] == [("cam01", DEV_A), ("cam02", DEV_B)]


def test_rig_mapping_decides_camera_numbers(tmp_path):
    """캘리브레이션에서 B 가 cam01 이었으면 이번에도 B 가 cam01 이어야 합니다."""
    ups = PL.find_uploads(make_upload(tmp_path))
    rig = PL.load_rig(make_rig(tmp_path, {"cam01": DEV_B, "cam02": DEV_A}))
    cams = PL.assign_cameras(ups, rig)
    assert [(c.name, c.device_id) for c in cams] == [("cam01", DEV_B), ("cam02", DEV_A)]


def test_phone_not_in_rig_is_error(tmp_path):
    ups = PL.find_uploads(make_upload(tmp_path, devices=(DEV_A, "CCCC00000003")))
    rig = PL.load_rig(make_rig(tmp_path, {"cam01": DEV_A, "cam02": DEV_B}))
    with pytest.raises(PL.PipelineError, match="캘리브레이션에 없는 폰"):
        PL.assign_cameras(ups, rig)


def test_rig_camera_missing_from_session_is_error(tmp_path):
    ups = PL.find_uploads(make_upload(tmp_path, devices=(DEV_A,)))
    rig = PL.load_rig(make_rig(tmp_path, {"cam01": DEV_A, "cam02": DEV_B}))
    with pytest.raises(PL.PipelineError, match="이번 촬영에 없는"):
        PL.assign_cameras(ups, rig)


# ── 리그 읽기 ─────────────────────────────────────────────────────────────────

def test_no_rig_folder_means_no_calibration(tmp_path):
    assert PL.load_rig(tmp_path / "nothing") is None
    assert PL.load_rig(None) is None


def test_rig_without_camera_map_is_error(tmp_path):
    r = make_rig(tmp_path, {"cam01": DEV_A, "cam02": DEV_B})
    (r / "cameras.json").unlink()
    with pytest.raises(PL.PipelineError, match="cameras.json"):
        PL.load_rig(r)


def test_rig_with_two_calib_files_is_error(tmp_path):
    """Pose2Sim 은 가장 최근 파일을 고릅니다. 어느 게 쓰일지 헷갈리므로 거부합니다."""
    r = make_rig(tmp_path, {"cam01": DEV_A, "cam02": DEV_B})
    (r / "Calib_old.toml").write_text(calib_text(["cam01", "cam02"]), encoding="utf-8")
    with pytest.raises(PL.PipelineError, match="2개"):
        PL.load_rig(r)


def test_read_calib_keeps_file_order_and_skips_metadata(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(calib_text(["cam02", "cam01"]), encoding="utf-8")
    assert [c.name for c in PL.read_calib(p)] == ["cam02", "cam01"]


def test_calib_section_missing_field_is_error(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(calib_text(["cam01"]).replace("translation", "trans"), encoding="utf-8")
    with pytest.raises(PL.PipelineError, match="translation"):
        PL.read_calib(p)


# ── 캘리브레이션 대조 ─────────────────────────────────────────────────────────

def _checked(tmp_path, names, size=(1920, 1080), video_size=(1920, 1080)):
    ups = PL.find_uploads(make_upload(tmp_path, size=video_size))
    rig = PL.load_rig(make_rig(tmp_path, {"cam01": DEV_A, "cam02": DEV_B}, names, size))
    cams = PL.assign_cameras(ups, rig)
    return {i.code: i.severity for i in PL.check_calibration(rig, cams)}


def test_calibration_matching_session_is_clean(tmp_path):
    assert _checked(tmp_path, ["cam01", "cam02"]) == {}


def test_calibration_order_swapped_is_fatal(tmp_path):
    """★ Pose2Sim 은 순서로 짝짓습니다. 이름이 뒤바뀐 순서면 3D 가 조용히 틀립니다."""
    assert _checked(tmp_path, ["cam02", "cam01"]) == {"calib_order": "fatal"}


def test_calibration_size_mismatch_is_fatal(tmp_path):
    assert _checked(tmp_path, ["cam01", "cam02"], size=(1280, 720)) == {"calib_size": "fatal"}


def test_calibration_small_size_difference_is_ok(tmp_path):
    """공식 데모도 영상 1080 폭에 캘리브레이션 1088 입니다."""
    assert _checked(tmp_path, ["cam01", "cam02"], size=(1088, 1920),
                    video_size=(1080, 1920)) == {}


def test_calibration_rotated_size_warns(tmp_path):
    assert _checked(tmp_path, ["cam01", "cam02"], size=(1080, 1920)) == \
        {"calib_size_rotated": "warning"}


def test_check_inputs_collects_sidecar_session_and_calibration(tmp_path):
    ups = PL.find_uploads(make_upload(tmp_path))
    rig = PL.load_rig(make_rig(tmp_path, {"cam01": DEV_A, "cam02": DEV_B}, ["cam02", "cam01"]))
    cams = PL.assign_cameras(ups, rig)
    f = PL.check_inputs(cams, rig)
    wheres = {x.where for x in f}
    assert {"세션", "캘리브레이션"} <= wheres
    assert PL.has_fatal(f)


def test_check_inputs_flags_bad_sidecar(tmp_path):
    d = make_upload(tmp_path)
    sd = json.loads((d / f"{DEV_A}.json").read_text(encoding="utf-8"))
    sd["stabilization"] = "standard"
    (d / f"{DEV_A}.json").write_text(json.dumps(sd), encoding="utf-8")
    cams = PL.assign_cameras(PL.find_uploads(d), None)
    f = PL.check_inputs(cams, None)
    assert any(x.where == "cam01" and x.issue.code == "stabilization_on" for x in f)
    assert PL.has_fatal(f)


# ── 프로젝트 폴더 ─────────────────────────────────────────────────────────────

def _template(tmp_path) -> Path:
    t = tmp_path / "template_Config.toml"
    t.write_text("[project]\nframe_rate = 'auto'\n", encoding="utf-8")
    return t


def test_build_project_layout(tmp_path):
    ups = PL.find_uploads(make_upload(tmp_path))
    rig = PL.load_rig(make_rig(tmp_path, {"cam01": DEV_B, "cam02": DEV_A}))
    cams = PL.assign_cameras(ups, rig)
    proj = tmp_path / "work" / "sessions" / "S1"
    how = PL.build_project(proj, cams, rig, _template(tmp_path))

    assert set(how) == {"cam01", "cam02"}
    assert sorted(p.name for p in (proj / "videos").iterdir()) == ["cam01.mov", "cam02.mov"]
    # cam01 은 리그 표대로 B 폰의 영상이어야 합니다
    assert (proj / "videos" / "cam01.mov").read_bytes().endswith(DEV_B.encode())
    s1 = json.loads((proj / "sidecars" / "cam01.json").read_text(encoding="utf-8"))
    assert s1["deviceId"] == DEV_B
    assert [p.name for p in (proj / "calibration").iterdir()] == ["Calib.toml"]
    assert (proj / "Config.toml").is_file()
    m = json.loads((proj / "cameras.json").read_text(encoding="utf-8"))
    assert m["cam01"]["deviceId"] == DEV_B


def test_rebuild_removes_stale_downstream_results(tmp_path):
    """★ 옛 pose-associated 가 남으면 삼각측량이 그걸 먼저 읽습니다."""
    cams = PL.assign_cameras(PL.find_uploads(make_upload(tmp_path)), None)
    proj = tmp_path / "work" / "sessions" / "S1"
    PL.build_project(proj, cams, None, _template(tmp_path))
    for name in PL.DOWNSTREAM_DIRS:
        (proj / name / "x").mkdir(parents=True)
    (proj / "pose" / "cam01_json").mkdir(parents=True)
    (proj / "videos" / "cam09.mov").write_bytes(b"old")

    PL.build_project(proj, cams, None, _template(tmp_path))
    for name in PL.DOWNSTREAM_DIRS:
        assert not (proj / name).exists(), name
    assert (proj / "pose" / "cam01_json").is_dir()      # 2D 는 비싸서 남깁니다
    assert not (proj / "videos" / "cam09.mov").exists()
    assert not (proj / "calibration").exists()          # 리그 없이 다시 돌림


def test_rebuild_with_same_inputs_keeps_2d_results(tmp_path):
    cams = PL.assign_cameras(PL.find_uploads(make_upload(tmp_path)), None)
    proj = tmp_path / "work" / "sessions" / "S1"
    PL.build_project(proj, cams, None, _template(tmp_path))
    (proj / "pose" / "cam01_json").mkdir(parents=True)
    how = PL.build_project(proj, cams, None, _template(tmp_path))
    assert (proj / "pose" / "cam01_json").is_dir()
    assert how == {"cam01": "있음", "cam02": "있음"}


def test_changed_camera_mapping_discards_2d_results(tmp_path):
    """
    ★ 실측으로 찾은 버그 (2026-09-27, 데모 4대).
    리그 표를 바꾸면 cam01 에 다른 폰의 영상이 들어옵니다. 옛 pose/cam01_json 이 남으면
    옛 영상의 2D 가 새 사이드카 시각과 섞여 오류 없이 3D 까지 나옵니다.
    """
    ups = PL.find_uploads(make_upload(tmp_path))
    proj = tmp_path / "work" / "sessions" / "S1"
    r1 = PL.load_rig(make_rig(tmp_path, {"cam01": DEV_A, "cam02": DEV_B}))
    PL.build_project(proj, PL.assign_cameras(ups, r1), r1, _template(tmp_path))
    (proj / "pose" / "cam01_json").mkdir(parents=True)

    (tmp_path / "rig" / "cameras.json").write_text(
        json.dumps({"cam01": DEV_B, "cam02": DEV_A}), encoding="utf-8")
    r2 = PL.load_rig(tmp_path / "rig")
    how = PL.build_project(proj, PL.assign_cameras(ups, r2), r2, _template(tmp_path))
    assert how["pose/"] == "지움"
    assert not (proj / "pose").exists()
    assert (proj / "videos" / "cam01.mov").read_bytes().endswith(DEV_B.encode())


def test_replaced_video_with_same_size_discards_2d_results(tmp_path):
    """크기만 같은 다른 영상(다시 올린 촬영)도 다른 영상으로 봐야 합니다."""
    d = make_upload(tmp_path)
    proj = tmp_path / "work" / "sessions" / "S1"
    PL.build_project(proj, PL.assign_cameras(PL.find_uploads(d), None), None, _template(tmp_path))
    (proj / "pose" / "cam01_json").mkdir(parents=True)

    v = d / f"{DEV_A}.mov"
    data = v.read_bytes()
    v.unlink()                                   # 하드링크를 끊고 새 파일로
    v.write_bytes(data[::-1])
    st = v.stat()
    os.utime(v, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    how = PL.build_project(proj, PL.assign_cameras(PL.find_uploads(d), None), None,
                           _template(tmp_path))
    assert how["pose/"] == "지움" and how["cam01"] in ("링크", "복사")
    assert (proj / "videos" / "cam01.mov").read_bytes() == data[::-1]


def test_non_ascii_project_path_is_refused(tmp_path):
    cams = PL.assign_cameras(PL.find_uploads(make_upload(tmp_path)), None)
    with pytest.raises(PL.PipelineError, match="ASCII"):
        PL.build_project(tmp_path / "작업" / "S1", cams, None, _template(tmp_path))


def test_config_in_parent_folder_is_refused(tmp_path):
    """Pose2Sim 은 부모에 Config.toml 이 있으면 배치 모드로 설정을 섞습니다."""
    cams = PL.assign_cameras(PL.find_uploads(make_upload(tmp_path)), None)
    parent = tmp_path / "work" / "sessions"
    parent.mkdir(parents=True)
    (parent / "Config.toml").write_text("", encoding="utf-8")
    with pytest.raises(PL.PipelineError, match="배치"):
        PL.build_project(parent / "S1", cams, None, _template(tmp_path))


def test_overrides_force_gpu_pair_and_headless():
    cfg = PL.pose2sim_overrides(Path("C:/w/S1"), 60.003, "CUDA", "onnxruntime")
    assert cfg["pose"]["device"] == "CUDA" and cfg["pose"]["backend"] == "onnxruntime"
    assert cfg["pose"]["display_detection"] is False
    assert cfg["pose"]["save_video"] == "none"
    assert cfg["filtering"]["display_figures"] is False
    assert cfg["synchronization"]["synchronization_gui"] is False
    assert cfg["project"]["frame_rate"] == 60


# ── 결과 읽기 ─────────────────────────────────────────────────────────────────

# 실제 Pose2Sim 0.10.49 로그에서 가져온 줄들 (Demo_Resampled, 2026-09-27)
LOG_OLD_RUN = """
Associating persons for , for all frames.
--> Mean reprojection error for Neck point on all frames is 30.0 px, which roughly corresponds to 50.0 mm.
Triangulation of 2D points for , for all frames.
--> Mean reprojection error for all points on frames 0 to 98 is 99.9 px, which roughly corresponds to 99.9 mm.
Camera cam02 was excluded 100% of the time, Camera cam01: 1%.
"""
LOG_NEW_RUN = """
Associating persons for , for all frames.
--> Mean reprojection error for Neck point on all frames is 11.8 px, which roughly corresponds to 21.5 mm.
--> In average, 0.0 cameras had to be excluded to reach the demanded 20 px error threshold after excluding points with likelihood below 0.3.
Triangulation of 2D points for , for all frames.
Mean reprojection error for Hip is 10.2 px (~ 0.019 m), reached with 0.81 excluded cameras.
--> Mean reprojection error for all points on frames 0 to 98 is 10.4 px, which roughly corresponds to 18.9 mm.
In average, 0.44 cameras had to be excluded to reach these thresholds.
Camera cam03 was excluded 31% of the time, Camera cam01: 6%, Camera cam04: 5%, and Camera cam02: 2%.
"""


def test_summarize_logs_reads_last_run_only():
    s = PL.summarize_logs(LOG_OLD_RUN + LOG_NEW_RUN)
    assert s["association_reproj_px"] == 11.8
    assert s["reproj_px"] == 10.4 and s["reproj_mm"] == 18.9
    assert s["avg_excluded_cams"] == 0.44
    assert s["excluded_pct"] == {"cam03": 31, "cam01": 6, "cam04": 5, "cam02": 2}


def test_summarize_logs_empty():
    assert PL.summarize_logs("") == {}


def test_trc_summary_counts_empty_values(tmp_path):
    p = tmp_path / "t.trc"
    p.write_text(
        "PathFileType\t4\t(X/Y/Z)\tt.trc\n"
        "DataRate\tCameraRate\tNumFrames\tNumMarkers\tUnits\tOrigDataRate\tOrigDataStartFrame\tOrigNumFrames\n"
        "60\t60\t2\t2\tm\t60\t0\t2\n"
        "Frame#\tTime\tHip\t\t\tNeck\n"
        "\t\tX1\tY1\tZ1\tX2\tY2\tZ2\n"
        "0\t0.0\t1\t2\t3\tnan\tnan\tnan\n"
        "1\t0.0167\t1\t2\t3\t4\t5\t6\n", encoding="utf-8")
    t = PL.trc_summary(p)
    assert (t["frames"], t["markers"], t["rate"]) == (2, 2, 60.0)
    assert t["empty_pct"] == pytest.approx(25.0)


def test_quality_notes_flag_bad_camera_and_error():
    notes = PL.quality_notes({"reproj_px": 40.0, "excluded_pct": {"cam02": 100, "cam01": 3}})
    assert any("[경고] 재투영" in n for n in notes)
    assert any("cam02" in n and "100%" in n for n in notes)
    assert not any("cam01" in n for n in notes)


def test_low_error_with_heavy_exclusion_is_not_called_good():
    """
    ★ 실측 (2026-09-27): 데모 4대에서 cam01↔cam03 을 뒤바꾸자 재투영 6.0 px 로 오히려
      좋아 보였습니다. Pose2Sim 이 맞지 않는 카메라를 빼고 계산하기 때문입니다.
    """
    notes = PL.quality_notes({"reproj_px": 6.0,
                              "excluded_pct": {"cam01": 86, "cam02": 51, "cam03": 48, "cam04": 14}})
    assert not any("좋음" in n for n in notes)
    assert any("믿을 수 없습니다" in n for n in notes)
    assert sum("버려졌습니다" in n for n in notes) == 2


def test_quality_notes_good_run():
    notes = PL.quality_notes({"reproj_px": 10.4, "excluded_pct": {"cam01": 5, "cam02": 1, "cam03": 0}})
    assert len(notes) == 1 and "좋음" in notes[0]


def test_two_camera_error_gets_caveat():
    """실측: 같은 데모 영상 2대 8.6 px / 4대 10.6 px. 작다고 더 정확한 게 아닙니다."""
    notes = PL.quality_notes({"reproj_px": 8.6, "excluded_pct": {"cam01": 0, "cam02": 0}})
    assert any("2대" in n for n in notes)
