# -*- coding: utf-8 -*-
"""
클럭 동기 계산 테스트.

핵심 아이디어: **진짜 오프셋을 우리가 정해놓고** 가상의 왕복 샘플을 만들어서,
추정기가 그 값을 되찾아오는지 채점합니다. 실기기에서는 진짜 오프셋을 알 수 없으니
이런 채점이 불가능합니다. 그래서 여기서 확실히 해둬야 합니다.

특히 검증하는 것:
  1. 대칭 경로에서는 오차가 0 이어야 한다 (수학적으로 그래야 함)
  2. 비대칭 경로에서 오차가 (d_up - d_dn)/2 와 정확히 일치해야 한다
  3. 어떤 경우에도 |오차| <= min_rtt/2 를 넘지 않아야 한다  <- 보장 상한
  4. 큐잉 지연(한쪽으로만 늘어나는 지연)이 있어도 최소 RTT 샘플이 살아남아야 한다
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mocapsync.clocksync import (  # noqa: E402
    NS_PER_MS,
    DEFAULT_MIN_LEAD_NS,
    TimeSample,
    check_schedule,
    estimate,
    frame_interval_stats,
    frames_to_master_timeline,
    master_to_slave_ns,
    slave_to_master_ns,
)

MS = NS_PER_MS


def make_sample(seq: int, true_offset_ns: int, d_up_ns: int, d_dn_ns: int,
                t1: int = 1_000_000_000, server_proc_ns: int = 50_000) -> TimeSample:
    """
    진짜 오프셋과 편도 지연을 지정해서 샘플 하나를 합성합니다.

    규약 정의 그대로:
        t2 = t1 + d_up + θ
        t4 = t3 + d_dn - θ
    (θ = 마스터 - 슬레이브)
    """
    t2 = t1 + d_up_ns + true_offset_ns
    t3 = t2 + server_proc_ns
    t4 = t3 + d_dn_ns - true_offset_ns
    return TimeSample(seq=seq, t1=t1, t2=t2, t3=t3, t4=t4)


# ── 1. 기본 수학 ─────────────────────────────────────────────────────────────

def test_symmetric_path_is_exact():
    """경로가 대칭이면 오차가 정확히 0 이어야 합니다."""
    true_offset = 12_345_678  # 12.3ms
    s = make_sample(0, true_offset, d_up_ns=1 * MS, d_dn_ns=1 * MS)
    assert s.offset_ns == true_offset
    assert s.rtt_ns == 2 * MS


def test_rtt_excludes_server_processing():
    """RTT 는 마스터의 처리 시간을 빼야 합니다."""
    s = make_sample(0, 0, d_up_ns=3 * MS, d_dn_ns=2 * MS, server_proc_ns=7 * MS)
    assert s.rtt_ns == 5 * MS
    assert s.server_processing_ns == 7 * MS


@pytest.mark.parametrize("d_up_ms,d_dn_ms", [
    (1, 1), (1, 3), (3, 1), (0, 4), (4, 0), (0.5, 2.5),
])
def test_asymmetry_error_matches_formula(d_up_ms, d_dn_ms):
    """오차가 정확히 (d_up - d_dn)/2 여야 합니다."""
    true_offset = -7_777_777
    d_up = int(d_up_ms * MS)
    d_dn = int(d_dn_ms * MS)
    s = make_sample(0, true_offset, d_up, d_dn)
    expected_err = (d_up - d_dn) // 2
    assert s.offset_ns - true_offset == pytest.approx(expected_err, abs=1)


@pytest.mark.parametrize("d_up_ms,d_dn_ms", [
    (1, 1), (1, 3), (3, 1), (0, 4), (4, 0), (0.1, 9.9), (9.9, 0.1),
])
def test_error_never_exceeds_half_rtt(d_up_ms, d_dn_ms):
    """
    ★ 이게 제일 중요한 성질입니다.
    어떤 비대칭이어도 |오차| <= RTT/2 를 넘지 않아야 합니다.
    이 성질 덕분에 '최소 RTT 4ms 미만 -> 오차 2ms 미만'을 보장할 수 있습니다.
    """
    true_offset = 3_141_592
    d_up = int(d_up_ms * MS)
    d_dn = int(d_dn_ms * MS)
    s = make_sample(0, true_offset, d_up, d_dn)
    err = abs(s.offset_ns - true_offset)
    assert err <= s.rtt_ns / 2 + 1, f"오차 {err} > RTT/2 {s.rtt_ns / 2}"


# ── 2. 추정기 ────────────────────────────────────────────────────────────────

def test_estimate_picks_min_rtt_sample():
    """
    큐잉이 낀 나쁜 샘플이 많아도, 깨끗한 최소 RTT 샘플을 골라내야 합니다.
    실제 WiFi 가 정확히 이렇게 동작합니다 (대부분 느리고 간헐적으로 빠름).
    """
    true_offset = 5_000_000
    samples = []
    # 나쁜 샘플 39개: 큐잉으로 업링크가 랜덤하게 늘어남 (지연은 늘기만 함)
    rng = random.Random(42)
    for i in range(39):
        extra = int(rng.uniform(5, 50) * MS)
        samples.append(make_sample(i, true_offset, d_up_ns=1 * MS + extra, d_dn_ns=1 * MS))
    # 좋은 샘플 1개: 대칭 0.4ms
    samples.append(make_sample(99, true_offset, d_up_ns=400_000, d_dn_ns=400_000))

    est = estimate(samples)
    assert est.samples_total == 40
    assert est.samples_rejected == 0
    assert est.min_rtt_ns == 800_000
    assert est.uncertainty_ns == 400_000
    # 깨끗한 샘플을 골랐으므로 진짜 오프셋을 거의 정확히 복원
    assert abs(est.offset_ns - true_offset) < 1000


def test_estimate_rejects_insane_samples():
    """시계 점프 같은 비정상 샘플은 폐기되어야 합니다."""
    good = make_sample(0, 0, 1 * MS, 1 * MS)
    bad_negative_rtt = TimeSample(seq=1, t1=1000, t2=2000, t3=9_000_000, t4=2000)
    bad_backwards = TimeSample(seq=2, t1=5000, t2=1000, t3=2000, t4=3000)
    est = estimate([good, bad_negative_rtt, bad_backwards])
    assert est.samples_total == 3
    assert est.samples_rejected == 2
    assert est.samples_used == 1


def test_estimate_empty():
    est = estimate([])
    assert est.samples_used == 0
    assert not est.meets_target()
    assert "실패" in est.verdict()


def test_meets_target_uses_guaranteed_bound():
    """
    판정은 '증명된 상한'으로 해야 합니다.
    RTT 3.9ms -> 상한 1.95ms -> 통과
    RTT 4.1ms -> 상한 2.05ms -> 미달
    """
    s_pass = make_sample(0, 0, d_up_ns=int(1.95 * MS), d_dn_ns=int(1.95 * MS))
    assert estimate([s_pass]).meets_target()

    s_fail = make_sample(0, 0, d_up_ns=int(2.05 * MS), d_dn_ns=int(2.05 * MS))
    assert not estimate([s_fail]).meets_target()


def test_spread_detects_instability():
    """
    오프셋이 샘플마다 흔들리면 spread 가 커져야 합니다.
    (시계 드리프트나 스로틀링을 잡아내는 지표)
    """
    rng = random.Random(7)
    samples = [
        make_sample(i, 1_000_000 + int(rng.uniform(-500_000, 500_000)),
                    d_up_ns=1 * MS, d_dn_ns=1 * MS)
        for i in range(20)
    ]
    est = estimate(samples)
    assert est.spread_ns > 500_000


# ── 3. 시각 변환 ─────────────────────────────────────────────────────────────

def test_conversion_roundtrip():
    offset = -123_456_789
    slave = 999_000_000_000
    assert master_to_slave_ns(slave_to_master_ns(slave, offset), offset) == slave


def test_conversion_sign_convention():
    """
    마스터 = 슬레이브 + offset.
    마스터 시계가 5ms 앞서 있으면(offset=+5ms),
    마스터의 100ms 지점은 슬레이브의 95ms 지점입니다.
    """
    offset = 5 * MS
    assert master_to_slave_ns(100 * MS, offset) == 95 * MS
    assert slave_to_master_ns(95 * MS, offset) == 100 * MS


# ── 4. 예약 시작 ─────────────────────────────────────────────────────────────

def test_schedule_accepts_with_enough_lead():
    offset = 2 * MS
    now_slave = 1_000 * MS
    # 마스터 기준 500ms 뒤 -> 슬레이브 기준으로도 약 500ms 뒤
    start_master = slave_to_master_ns(now_slave, offset) + 500 * MS
    chk = check_schedule(start_master, offset, now_slave)
    assert chk.ok
    assert chk.lead_ms == pytest.approx(500.0, abs=0.001)


def test_schedule_rejects_late_command():
    """
    ★ 늦게 도착한 명령은 거부해야 합니다.
    억지로 따라가면 동기가 틀어지는데, 그게 조용히 일어나면 최악입니다.
    """
    offset = 0
    now_slave = 1_000 * MS
    start_master = now_slave + 50 * MS  # 50ms 밖에 안 남음
    chk = check_schedule(start_master, offset, now_slave)
    assert not chk.ok
    assert "lead_too_small" in chk.reason


def test_schedule_rejects_past_command():
    """이미 지나간 시각도 당연히 거부."""
    chk = check_schedule(500 * MS, 0, 1_000 * MS)
    assert not chk.ok
    assert chk.lead_ns < 0


def test_schedule_default_min_lead_is_300ms():
    """설계 문서는 (현재 + 500ms) 예약. 최소 여유 300ms 는 그 안에 들어갑니다."""
    assert DEFAULT_MIN_LEAD_NS == 300 * MS


# ── 5. 프레임 타임스탬프 ─────────────────────────────────────────────────────

def test_frames_to_master_timeline():
    offset = 1_000_000
    frames = [[0, 100], [1, 200], [2, 300]]
    out = frames_to_master_timeline(frames, offset)
    assert out == [(0, 1_000_100), (1, 1_000_200), (2, 1_000_300)]


def test_frame_interval_stats_detects_60fps():
    """60fps = 16.667ms 간격."""
    step = 16_666_667
    frames = [[i, i * step] for i in range(60)]
    st = frame_interval_stats(frames)
    assert st["estimated_fps"] == pytest.approx(60.0, abs=0.01)
    assert st["suspected_drops"] == 0


def test_frame_interval_stats_detects_drops():
    """
    발열 스로틀링으로 프레임을 흘리면 간격이 2배로 벌어집니다.
    이걸 자동으로 세어야 합니다 (설계: '보간 프레임 수'를 품질 대시보드에 표시).
    """
    step = 16_666_667
    ts = []
    t = 0
    for i in range(60):
        ts.append(t)
        t += step * (2 if i in (10, 20, 30) else 1)
    frames = [[i, v] for i, v in enumerate(ts)]
    st = frame_interval_stats(frames)
    assert st["suspected_drops"] == 3
    assert st["max_interval_ms"] == pytest.approx(33.333, abs=0.01)


# ── 6. 시나리오: 실제 WiFi 를 흉내낸 통합 시나리오 ────────────────────────────

@pytest.mark.parametrize("seed", range(10))
def test_realistic_wifi_scenario(seed):
    """
    실제 로컬 WiFi 를 흉내냅니다:
      - 기본 편도 지연 0.5~2ms
      - 간헐적 큐잉 스파이크
      - 업/다운 비대칭
    40회 왕복 후 추정 오차가 보장 상한 안에 있는지 확인합니다.
    """
    rng = random.Random(seed)
    true_offset = rng.randint(-50 * MS, 50 * MS)

    samples = []
    for i in range(40):
        base_up = rng.uniform(0.4, 2.0) * MS
        base_dn = rng.uniform(0.4, 2.0) * MS
        if rng.random() < 0.3:  # 30% 확률로 큐잉 스파이크
            base_up += rng.uniform(3, 40) * MS
        if rng.random() < 0.3:
            base_dn += rng.uniform(3, 40) * MS
        samples.append(make_sample(i, true_offset, int(base_up), int(base_dn),
                                   t1=1_000_000_000 + i * 20_000_000))

    est = estimate(samples)
    err = abs(est.offset_ns - true_offset)

    # 보장 상한을 절대 넘지 않아야 합니다
    assert err <= est.uncertainty_ns + 1, (
        f"seed={seed}: 오차 {err / MS:.3f}ms > 상한 {est.uncertainty_ms:.3f}ms")
    # 그리고 실용적으로도 쓸 만해야 합니다
    assert err < 2 * MS, f"seed={seed}: 오차 {err / MS:.3f}ms"
