"""
사이드카 구조와 검증을 시험합니다.

ios/Tests/MocapSyncCoreTests/SidecarTests.swift 와 **같은 성질**을 검증합니다.
한쪽만 고치면 다른 쪽 테스트가 깨져서 드러나게 하는 것이 목적입니다.

★ 왜 이 테스트가 중요한가
사이드카는 조용히 망가집니다. 프레임 0개, 시각 역행, 낡은 오프셋 —
전부 예외 없이 그냥 "이상한 3D"로 끝납니다. 각 실패 모드를 일부러 만들어
검증이 잡는지 확인합니다.
"""
from __future__ import annotations

import copy
import json

import pytest

from mocapsync import sidecar as sc

MS = sc.NS_PER_MS
S = sc.NS_PER_S


#: 프레임 시작 시각 = 부팅 후 1000초.
#  실제 폰이 그 정도 켜져 있고, "동기를 5분 전에 했다" 같은 상황을 만들려면
#  앞쪽에 여유가 있어야 합니다 (10초로 두면 음수가 됩니다).
BASE_START_NS = 1_000_000_000_000


def make_frames(count: int, start_ns: int = BASE_START_NS,
                fps: float = 60) -> list[list[int]]:
    step = int(1e9 / fps)
    return [[k, start_ns + k * step] for k in range(count)]


def valid_dict(frame_count: int = 600) -> dict:
    """모든 검사를 통과해야 하는 사이드카 (Swift 쪽 makeValid 와 동일한 값)."""
    frames = make_frames(frame_count)
    return {
        "schemaVersion": 1,
        "deviceId": "6DC32E3A1234",
        "deviceName": "iPhone",
        "model": "iPhone12,1",
        "osVersion": "17.5.1",
        "appVersion": "0.3.0 (11) abc1234",
        "sessionId": "S20260924-1",
        "role": "slave",
        "clock": "CLOCK_UPTIME_RAW",
        "clockOffsetNs": 5_522_186_169_000,
        "clockUncertaintyNs": 2_038_000,
        "clockMinRttNs": 4_076_000,
        "clockMeasuredAtNs": BASE_START_NS - 1_000_000_000,   # 첫 프레임 1초 전
        # ★ 두 절전값을 일부러 다르게 둡니다 (실측 읽기 잡음 42 ns 수준).
        #   이전 픽스처는 두 값을 똑같이 넣어서 정확 비교(!=) 버그를 못 잡았습니다.
        #   현실에서는 시계 두 개를 연달아 읽어 빼는 값이라 절대 같을 수 없고,
        #   실기기에서 모든 촬영이 치명 거부됐습니다.
        "sleepAtSyncNs": 1_234_567,
        "sleepAtRecordStartNs": 1_234_609,
        "timestampSource": "CMSampleBufferPresentationTimeStamp",
        "timestampDomainDeltaNs": -458,
        "targetFps": 60,
        "width": 1920,
        "height": 1080,
        "cameraDeviceType": "AVCaptureDeviceTypeBuiltInWideAngleCamera",
        "fieldOfViewDeg": 69.7,
        "isBinned": False,
        "exposureDurationNs": 2_000_000,
        "iso": 400.0,
        "lensPosition": 0.42,
        "focusLocked": True,
        "whiteBalanceLocked": True,
        "exposureLocked": True,
        "stabilization": "off",
        "deviceOrientation": "landscapeLeft",
        "cameraWarnings": [],
        # 마스터시각 = 슬레이브시각 + 오프셋
        "requestedStartAtMasterNs": BASE_START_NS + 5_522_186_169_000,
        "requestedStartAtSlaveNs": BASE_START_NS,
        "firstFramePtsNs": frames[0][1],
        "droppedFrameCount": 0,
        "thermalAtStart": "nominal",
        "thermalAtEnd": "fair",
        "batteryAtStart": 0.8,
        "batteryAtEnd": 0.78,
        "frames": frames,
    }


def load(d: dict) -> sc.Sidecar:
    return sc.Sidecar.from_dict(d)


def codes(s: sc.Sidecar) -> set[str]:
    return {i.code for i in s.validate()}


def fatals(s: sc.Sidecar) -> set[str]:
    return {i.code for i in s.validate() if i.severity == "fatal"}


# ── 정상본 ───────────────────────────────────────────────────────────────────

def test_valid_sidecar_has_no_warnings_or_fatals():
    """
    ★ 이게 먼저 통과해야 나머지 테스트가 의미를 가집니다.

    기준: 경고와 치명이 하나도 없어야 합니다. info 는 허용합니다 —
    실측 오차 상한 2.038 ms 가 설계 목표 2 ms 를 구조적으로 넘기 때문에
    (DESIGN.md 3.12) 정상 촬영에도 info 가 하나 붙습니다.
    그걸 경고로 올리면 모든 촬영에 경고가 붙어 경고가 무의미해집니다.
    """
    s = load(valid_dict())
    bad = [i for i in s.validate() if i.severity != "info"]
    assert bad == [], s.validation_report()
    assert s.is_usable


def test_measured_best_uncertainty_is_info_only():
    """실측 최고 기록(2.038 ms)은 info 까지만. 경고면 안 됩니다."""
    s = load(valid_dict())
    assert s.clock_uncertainty_ns == 2_038_000
    i = next(i for i in s.validate() if i.code == "clock_uncertainty_over_target")
    assert i.severity == "info"


def test_degraded_uncertainty_warns():
    """실측 기준선(3 ms)보다 나쁘면 경고."""
    d = valid_dict()
    d["clockUncertaintyNs"] = 3_500_000
    s = load(d)
    c = codes(s)
    assert "clock_uncertainty_degraded" in c
    assert "clock_uncertainty_over_target" not in c, "중복 보고"
    assert s.is_usable


def test_measured_worst_uncertainty_is_not_fatal():
    """실측 최악값 2.995ms (18:58 측정) 도 치명은 아니어야 합니다."""
    d = valid_dict()
    d["clockUncertaintyNs"] = 2_995_000
    s = load(d)
    assert s.is_usable
    assert "clock_uncertainty_degraded" not in codes(s)


def test_key_set_matches_swift():
    """
    Swift 쪽 testJSONKeysArePinned 와 같은 키 집합이어야 합니다.
    한쪽만 키를 바꾸면 여기서 드러납니다.
    """
    assert set(valid_dict(2).keys()) == set(sc.SIDECAR_KEYS)


def test_derived_values():
    s = load(valid_dict(600))
    assert s.frame_count == 600
    assert s.duration_ns / S == pytest.approx(599 / 60, abs=1e-6)
    assert s.interval_stats()["estimated_fps"] == pytest.approx(60, abs=0.5)
    assert not s.slept_since_sync
    # 읽기 잡음만큼은 차이가 납니다 (0 이 되는 일은 현실에 없습니다)
    assert s.sleep_since_sync_ns == 42
    assert s.sleep_since_sync_ns < sc.SLEEP_NOISE_TOLERANCE_NS


def test_to_master_conversion():
    s = load(valid_dict())
    slave = s.timestamps_ns[0]
    assert s.to_master_ns(slave) == slave + s.clock_offset_ns
    assert s.timestamps_master_ns[0] == slave + s.clock_offset_ns


def test_master_timeline_shape():
    s = load(valid_dict(5))
    tl = s.master_timeline()
    assert len(tl) == 5
    assert tl[0] == (0, s.timestamps_ns[0] + s.clock_offset_ns)


def test_dict_round_trip():
    a = load(valid_dict(50))
    b = load(a.to_dict())
    assert a.to_dict() == b.to_dict()


def test_json_round_trip():
    d = valid_dict(600)
    s = load(json.loads(json.dumps(d)))
    assert [i for i in s.validate() if i.severity != "info"] == [], s.validation_report()


# ── 읽기 견고성 ──────────────────────────────────────────────────────────────

def test_missing_keys_reported_not_raised():
    """
    업로드 서버가 잘못된 파일 하나 때문에 죽으면 안 됩니다.
    예외 대신 보고해야 합니다.
    """
    d = valid_dict(10)
    del d["clockOffsetNs"]
    del d["stabilization"]
    s = load(d)                       # 예외가 나면 실패
    assert "missing_keys" in codes(s)
    msg = next(i.message for i in s.validate() if i.code == "missing_keys")
    assert "clockOffsetNs" in msg and "stabilization" in msg


def test_immediate_recording_omits_scheduled_keys_without_warning():
    """
    ★ 2026-09-25 첫 실기기 업로드에서 발견한 오탐.

    Swift 의 JSONEncoder 는 nil Optional 을 키째로 생략합니다(null 을 쓰지 않음).
    그래서 예약 없이 "바로 녹화"하면 requestedStartAt* 가 아예 안 들어옵니다.
    이건 정상인데, 모든 키를 필수로 취급해서 정상 촬영에도 경고가 붙었습니다.

    테스트 픽스처는 세 키를 모두 채워 넣어서 이 경우를 시험하지 않았습니다.
    """
    d = valid_dict(600)
    del d["requestedStartAtMasterNs"]
    del d["requestedStartAtSlaveNs"]
    s = load(d)
    assert "missing_keys" not in codes(s), s.validation_report()
    # 선택 키가 없으면 예약 관련 판정도 건너뜁니다
    assert "started_early" not in codes(s)
    assert "started_late" not in codes(s)
    assert [i for i in s.validate() if i.severity != "info"] == [], s.validation_report()


def test_missing_first_frame_pts_is_not_warned():
    d = valid_dict(600)
    del d["firstFramePtsNs"]
    assert "missing_keys" not in codes(load(d))


def test_optional_keys_are_subset_of_all_keys():
    assert sc.SIDECAR_OPTIONAL_KEYS <= sc.SIDECAR_KEYS


def test_extra_keys_are_info_only():
    d = valid_dict(10)
    d["futureField"] = 123
    s = load(d)
    assert "extra_keys" in codes(s)
    assert "extra_keys" not in fatals(s)


def test_none_values_do_not_crash():
    d = valid_dict(10)
    d["clockOffsetNs"] = None
    d["iso"] = None
    s = load(d)
    assert s.clock_offset_ns == 0
    assert s.iso == 0.0


# ── 치명 실패들 ──────────────────────────────────────────────────────────────

def test_no_frames_is_fatal():
    d = valid_dict()
    d["frames"] = []
    s = load(d)
    assert "no_frames" in fatals(s)
    assert not s.is_usable


def test_non_monotonic_is_fatal():
    d = valid_dict(10)
    d["frames"][5][1] = d["frames"][4][1] - 1000
    assert "non_monotonic" in fatals(load(d))


def test_duplicate_timestamp_is_fatal():
    d = valid_dict(10)
    d["frames"][5][1] = d["frames"][4][1]
    assert "non_monotonic" in fatals(load(d))


def test_slept_since_sync_is_fatal():
    """★ 2026-09-24 실측으로 알게 된 실패 모드."""
    d = valid_dict()
    d["sleepAtRecordStartNs"] = d["sleepAtSyncNs"] + 34 * 60 * S
    s = load(d)
    assert s.slept_since_sync
    assert "slept_since_sync" in fatals(s)
    assert any("2040" in r or "34" in r for r in s.validation_report())


@pytest.mark.parametrize("noise", [1, 42, 500, 10_000, 999_999])
def test_sleep_read_noise_is_not_treated_as_sleep(noise):
    """
    ★★ 이 테스트가 없어서 실기기에서 두 번 연속 실패했습니다.

    누적 절전시간은 시계 두 개를 연달아 읽어 빼는 값이라 잔 적이 없어도
    수십 ns 씩 다릅니다. 정확 비교(!=)로 판정하면 항상 '잤다'가 됩니다.
    """
    d = valid_dict()
    d["sleepAtSyncNs"] = 1_000_000
    d["sleepAtRecordStartNs"] = 1_000_000 + noise
    s = load(d)
    assert not s.slept_since_sync, f"읽기 잡음 {noise} ns 를 절전으로 오판"
    assert "slept_since_sync" not in fatals(s)
    assert s.is_usable, s.validation_report()


def test_just_over_tolerance_is_sleep():
    d = valid_dict()
    d["sleepAtSyncNs"] = 1_000_000
    d["sleepAtRecordStartNs"] = 1_000_000 + sc.SLEEP_NOISE_TOLERANCE_NS + 1
    s = load(d)
    assert s.slept_since_sync
    assert "slept_since_sync" in fatals(s)


def test_negative_sleep_delta_is_not_sleep():
    d = valid_dict()
    d["sleepAtSyncNs"] = 5_000_000
    d["sleepAtRecordStartNs"] = 1_000_000
    assert not load(d).slept_since_sync


def test_sleep_tolerance_matches_swift():
    """ios 쪽 MonotonicClock.sleepNoiseToleranceNs 와 같아야 합니다."""
    assert sc.SLEEP_NOISE_TOLERANCE_NS == 1_000_000


def test_missing_clock_sync_is_fatal():
    d = valid_dict()
    d["clockUncertaintyNs"] = 0
    assert "no_clock_sync" in fatals(load(d))


def test_uncertainty_over_half_frame_is_fatal():
    d = valid_dict()
    d["clockUncertaintyNs"] = 9 * MS
    assert "clock_uncertainty_over_half_frame" in fatals(load(d))


def test_measured_voice_priority_uncertainty_is_usable():
    """실측 음성우선 값 2.331ms 도 경고 없이 쓸 수 있어야 합니다."""
    d = valid_dict()
    d["clockUncertaintyNs"] = 2_331_000
    s = load(d)
    assert "clock_uncertainty_over_target" in codes(s)
    assert s.is_usable
    assert [i for i in s.validate() if i.severity == "warning"] == []


def test_clock_domain_mismatch_is_fatal():
    d = valid_dict()
    d["timestampDomainDeltaNs"] = 5 * MS
    assert "clock_domain_mismatch" in fatals(load(d))


def test_stabilization_on_is_fatal():
    d = valid_dict()
    d["stabilization"] = "standard"
    assert "stabilization_on" in fatals(load(d))


def test_virtual_camera_is_fatal():
    d = valid_dict()
    d["cameraDeviceType"] = "AVCaptureDeviceTypeBuiltInDualWideCamera"
    assert "virtual_camera" in fatals(load(d))


def test_started_early_is_fatal():
    d = valid_dict()
    d["requestedStartAtSlaveNs"] = d["frames"][0][1] + 5 * MS
    assert "started_early" in fatals(load(d))


def test_empty_device_id_is_fatal():
    d = valid_dict()
    d["deviceId"] = ""
    assert "no_device_id" in fatals(load(d))


# ── 경고들 ───────────────────────────────────────────────────────────────────

def test_fps_mismatch_warns():
    d = valid_dict()
    d["frames"] = make_frames(600, fps=30)
    d["firstFramePtsNs"] = d["frames"][0][1]
    s = load(d)
    assert "fps_mismatch" in codes(s)
    assert s.is_usable


def test_target_fps_below_60_warns():
    d = valid_dict()
    d["targetFps"] = 30
    d["frames"] = make_frames(600, fps=30)
    d["firstFramePtsNs"] = d["frames"][0][1]
    assert "fps_below_60" in codes(load(d))


def test_frame_drops_detected():
    d = valid_dict(100)
    for k in range(50, 100):
        d["frames"][k][1] += 16_666_666
    assert "frame_drops" in codes(load(d))


def test_reported_drops_warn():
    d = valid_dict()
    d["droppedFrameCount"] = 7
    assert "reported_drops" in codes(load(d))


def test_slow_shutter_warns():
    d = valid_dict()
    d["exposureDurationNs"] = 8_000_000
    assert "shutter_too_slow" in codes(load(d))


def test_too_short_warns():
    d = valid_dict(60)
    s = load(d)
    assert "too_short" in codes(s)
    assert s.is_usable


def test_fixed_focus_lens_does_not_warn():
    """
    ★ 고정초점 렌즈는 focusLocked=False 가 정상입니다.
    초광각·전면은 초점 기구가 없어 잠글 대상이 없습니다 (DESIGN.md §3.10).
    """
    d = valid_dict()
    d["focusLocked"] = False
    d["cameraDeviceType"] = "AVCaptureDeviceTypeBuiltInUltraWideCamera"
    s = load(d)
    assert "focus_not_locked" not in codes(s)
    assert s.is_usable


def test_sync_too_old_is_fatal():
    """
    ★ 가장 안 보이는 실패 모드.
    측정 상한은 2.038ms 로 좋은데, 5분 전 측정이면 드리프트가 12ms 쌓여
    실효 상한이 반 프레임을 넘습니다. 측정값만 보면 알 수 없습니다.
    """
    d = valid_dict()
    d["clockMeasuredAtNs"] = d["frames"][0][1] - 300 * S
    s = load(d)
    assert "sync_too_old" in fatals(s), s.validation_report()
    assert not s.is_usable


def test_sync_aging_warns():
    d = valid_dict()
    d["clockMeasuredAtNs"] = d["frames"][0][1] - 120 * S
    s = load(d)
    assert "sync_aging" in codes(s)
    assert s.is_usable, "120초는 실효 6.84ms 로 반 프레임 안입니다"


def test_recent_sync_does_not_warn():
    d = valid_dict()
    d["clockMeasuredAtNs"] = d["frames"][0][1] - 50 * S
    c = codes(load(d))
    assert "sync_aging" not in c
    assert "sync_too_old" not in c


def test_drift_constant_matches_swift():
    """
    ios 쪽 Sidecar.assumedDriftPpm / ClockOffsetRecord.worstCaseDriftPpm 과
    같아야 합니다. 다르면 폰에서는 통과했는데 PC 에서 거부됩니다.
    """
    assert sc.ASSUMED_DRIFT_PPM == 40.0


def test_drift_budget_rationale():
    """40 ppm 에서 2ms 를 소진하는 시간이 50초 -> 신선 기준 60초의 근거."""
    seconds_to_burn_2ms = 2e-3 / (sc.ASSUMED_DRIFT_PPM / 1e6)
    assert seconds_to_burn_2ms == pytest.approx(50, abs=0.01)
    # 반 프레임을 소진하는 시간
    seconds_to_burn_half_frame = (sc.HALF_FRAME_NS_60 / 1e9) / (sc.ASSUMED_DRIFT_PPM / 1e6)
    assert seconds_to_burn_half_frame == pytest.approx(208.3, abs=0.5)


def test_report_on_fully_clean_sidecar():
    """
    오차 상한이 목표(2ms) 안이면 info 조차 없어야 합니다.
    실측 기본값 2.038ms 는 목표를 살짝 넘어 info 가 붙으므로,
    이 테스트는 "목표를 만족했을 때"의 이상적 상태를 확인합니다.
    """
    d = valid_dict()
    d["clockUncertaintyNs"] = 1_900_000
    d["clockMinRttNs"] = 3_800_000
    s = load(d)
    assert s.validation_report() == ["문제 없음 ✔"], s.validation_report()


def test_report_on_measured_sidecar_has_single_info():
    r = load(valid_dict()).validation_report()
    assert len(r) == 1, r
    assert r[0].startswith("[참고]"), r[0]


def test_portrait_orientation_warns():
    """
    ★ 2026-09-25 첫 실기기 촬영이 세로로 찍혔습니다.
    후면 카메라 기준 방향은 가로이므로 사람이 90도 누워 저장되고,
    2D 자세 추정 모델은 똑바로 선 사람으로 학습됐으므로 정확도가 떨어집니다.
    미리보기가 자동 회전해 보여줘서 촬영자는 알아채기 어렵습니다.
    """
    d = valid_dict()
    d["deviceOrientation"] = "portrait"
    s = load(d)
    assert "portrait_orientation" in codes(s)
    assert s.is_usable, "누워 있어도 데이터 자체는 쓸 수 있습니다 (정확도만 떨어짐)"


def test_landscape_orientation_does_not_warn():
    for o in ("landscapeLeft", "landscapeRight"):
        d = valid_dict()
        d["deviceOrientation"] = o
        assert "portrait_orientation" not in codes(load(d)), o


def test_high_iso_warns():
    """
    1/500초 셔터를 유지하려면 빛이 많이 필요합니다. 어두우면 ISO 가 치솟고
    노이즈가 2D 검출을 방해합니다. 첫 실기기 촬영이 ISO 3072 였습니다.
    """
    d = valid_dict()
    d["iso"] = 3072.0
    s = load(d)
    assert "iso_too_high" in codes(s)
    assert s.is_usable


def test_normal_iso_does_not_warn():
    d = valid_dict()
    d["iso"] = 640.0
    assert "iso_too_high" not in codes(load(d))


def test_camera_warnings_surface_as_info():
    """
    카메라가 잠그지 못한 것(OIS 등)은 지금까지 앱 화면에만 있었습니다.
    업로드된 파일만 봐서는 원인을 알 수 없었으므로 사이드카에 담습니다.
    """
    d = valid_dict()
    d["cameraWarnings"] = ["후면 광각에는 OIS 가 있고 끄는 API 가 없습니다."]
    s = load(d)
    issues = [i for i in s.validate() if i.code == "camera_warning"]
    assert len(issues) == 1
    assert issues[0].severity == "info"
    assert "OIS" in issues[0].message


def test_schema_version_mismatch_warns():
    d = valid_dict()
    d["schemaVersion"] = 99
    assert "schema_version" in codes(load(d))


# ── 세션 단위 검사 ───────────────────────────────────────────────────────────

def two_cams(offset_b_ns: int = 0, start_b_ns: int = BASE_START_NS):
    a = valid_dict(600)
    a["deviceId"] = "CAM_A"
    b = valid_dict(600)
    b["deviceId"] = "CAM_B"
    b["frames"] = make_frames(600, start_ns=start_b_ns)
    b["firstFramePtsNs"] = b["frames"][0][1]
    b["clockOffsetNs"] = a["clockOffsetNs"] + offset_b_ns
    b["requestedStartAtSlaveNs"] = start_b_ns
    return load(a), load(b)


def test_session_single_camera_warns():
    a, _ = two_cams()
    out = sc.check_session([a])
    assert any(i.code == "single_camera" for i in out)


def test_session_happy_path_reports_overlap():
    a, b = two_cams()
    out = sc.check_session([a, b])
    assert not [i for i in out if i.severity == "fatal"]
    assert any(i.code == "overlap" for i in out)


def test_session_duplicate_device_id_is_fatal():
    a, b = two_cams()
    b.device_id = a.device_id
    out = sc.check_session([a, b])
    assert any(i.code == "duplicate_device_id" and i.severity == "fatal" for i in out)


def test_session_mismatch_is_fatal():
    a, b = two_cams()
    b.session_id = "S-OTHER"
    out = sc.check_session([a, b])
    assert any(i.code == "session_mismatch" and i.severity == "fatal" for i in out)


def test_session_no_overlap_is_fatal():
    """
    ★ 개별 사이드카는 둘 다 정상인데 같이 놓으면 겹치는 구간이 없는 경우.
    클럭 오프셋이 틀렸을 때 이렇게 됩니다. 이게 잡혀야 합니다.
    """
    # B 를 1시간 뒤에 찍은 것으로 만듭니다
    a, b = two_cams(start_b_ns=BASE_START_NS + 3600 * S)
    out = sc.check_session([a, b])
    assert any(i.code == "no_overlap" and i.severity == "fatal" for i in out)


def test_session_short_overlap_warns():
    # B 를 9초 뒤에 시작 -> 10초 길이끼리 1초만 겹칩니다
    a, b = two_cams(start_b_ns=BASE_START_NS + 9 * S)
    out = sc.check_session([a, b])
    assert any(i.code == "overlap_too_short" for i in out)


def test_session_reports_worst_uncertainty():
    a, b = two_cams()
    b.clock_uncertainty_ns = 3 * MS
    out = sc.check_session([a, b])
    msg = next(i.message for i in out if i.code == "worst_uncertainty")
    assert "3.000" in msg
