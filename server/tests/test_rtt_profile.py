"""
RTT 분포 진단(rtt_profile)을 검증합니다.

ios/Tests/MocapSyncCoreTests/RttProfileTests.swift 와 **같은 성질**을 검증합니다.
한쪽만 고치면 다른 쪽 테스트가 깨져서 드러나게 하는 것이 목적입니다.

이 기능은 2026-09-24 아이폰 11 실측 때문에 추가됐습니다.
실측값: 최소 RTT 4.810 ms -> 오차 상한 2.405 ms (목표 2 ms 미달).
숫자 하나로는 "물리적 바닥"인지 "표본 부족"인지 알 수 없어서 분포를 보게 만들었습니다.
"""
import math

import pytest

from mocapsync import clocksync as cs

MS = cs.NS_PER_MS


def sample(seq: int, rtt_ns: int, offset_ns: int = 0,
           proc_ns: int = 50_000) -> cs.TimeSample:
    """원하는 RTT 를 갖는 샘플. 편도는 대칭(오차 0)."""
    t1 = 1_000_000_000
    d_up = rtt_ns // 2
    d_dn = rtt_ns - d_up
    t2 = t1 + d_up + offset_ns
    t3 = t2 + proc_ns
    t4 = t3 + d_dn - offset_ns
    return cs.TimeSample(seq=seq, t1=t1, t2=t2, t3=t3, t4=t4)


# ── 백분위 ───────────────────────────────────────────────────────────────────

def test_percentile_nearest_rank():
    xs = [i * MS for i in range(1, 11)]  # 1..10 ms
    assert cs.percentile_nearest(xs, 0) == 1 * MS
    assert cs.percentile_nearest(xs, 100) == 10 * MS
    # idx = floor(0.5 * 9 + 0.5) = floor(5.0) = 5  -> 6ms
    assert cs.percentile_nearest(xs, 50) == 6 * MS


def test_percentile_uses_swift_rounding_not_bankers():
    """
    ★ 이 테스트가 잡는 버그

    Swift 의 .rounded() 는 0에서 먼 쪽 반올림이라 round(4.5) == 5 입니다.
    Python 내장 round() 는 짝수 반올림이라 round(4.5) == 4 입니다.

    percentile_nearest 가 내장 round() 를 쓰면 두 구현이 **다른 표본**을
    p50 으로 고르게 되고, 같은 측정에서 다른 진단이 나옵니다.
    그래서 floor(x + 0.5) 로 구현해야 합니다.
    """
    assert round(4.5) == 4          # Python 의 함정을 문서화
    assert math.floor(4.5 + 0.5) == 5

    # n=10 에서 p50 -> 0.5*9 = 4.5 -> Swift 는 5번 인덱스를 고릅니다
    xs = list(range(10))
    assert cs.percentile_nearest(xs, 50) == 5, \
        "내장 round() 를 쓰면 4 가 나옵니다. Swift 와 갈라집니다"


def test_percentile_always_returns_observed_value():
    """보간하면 관측되지 않은 RTT 가 나와서 해석이 애매해집니다."""
    xs = [3 * MS, 7 * MS]
    for p in range(0, 101, 5):
        assert cs.percentile_nearest(xs, p) in xs


def test_percentile_empty():
    assert cs.percentile_nearest([], 50) == 0


# ── 분포 모양 판정 ───────────────────────────────────────────────────────────

def test_narrow_distribution_means_floor_reached():
    """실측 재현: 최소 4.81ms, 중앙값도 5ms 대 -> 바닥 도달."""
    s = [sample(i, 4_810_000 + (i % 4) * 100_000) for i in range(40)]
    p = cs.rtt_profile(s)
    assert p.count == 40
    assert p.shape == "narrow"
    assert p.headroom < 0.25
    assert "유선" in p.diagnosis, \
        f"바닥에 도달했으면 경로를 바꾸라고 안내해야 합니다: {p.diagnosis}"


def test_heavy_tail_means_more_probes_help():
    s = [sample(0, 3 * MS)]
    s += [sample(i, (10 + i % 20) * MS) for i in range(1, 40)]
    p = cs.rtt_profile(s)
    assert p.shape == "heavyTail"
    assert "왕복" in p.diagnosis


def test_moderate_distribution():
    # p0=4ms, p50 은 4ms 의 1.25~2배 사이가 되게
    s = [sample(i, (4 + i % 3) * MS) for i in range(30)]
    p = cs.rtt_profile(s)
    assert p.shape == "moderate"


def test_too_few_samples():
    assert cs.rtt_profile([sample(0, 4 * MS)]).shape == "tooFewSamples"


def test_empty_profile():
    p = cs.rtt_profile([])
    assert p == cs.EMPTY_RTT_PROFILE
    assert p.count == 0
    assert p.headroom == 0.0
    assert p.histogram_lines() == []


# ── 버킷 ─────────────────────────────────────────────────────────────────────

def test_buckets_sum_to_count():
    s = [sample(i, i * 500_000) for i in range(100)]
    p = cs.rtt_profile(s)
    assert sum(p.buckets) == p.count, \
        "버킷 합이 표본 수와 다릅니다. 경계 조건에서 샘플을 흘리고 있습니다"


def test_buckets_length_is_edges_plus_one():
    p = cs.rtt_profile([sample(0, 4 * MS)])
    assert len(p.buckets) == len(cs.RTT_BUCKET_EDGES_MS) + 1


def test_overflow_goes_to_last_bucket():
    p = cs.rtt_profile([sample(0, 5_000_000_000)])
    assert p.buckets[-1] == 1


# ── 비정상 샘플 배제 ─────────────────────────────────────────────────────────

def test_insane_samples_excluded():
    """
    is_sane() 이 거른 샘플은 분포에도 들어가면 안 됩니다.
    안 그러면 히스토그램이 거짓말을 하고 진단 문장도 틀립니다.
    """
    good = sample(0, 4 * MS)
    bad = cs.TimeSample(seq=1, t1=1000, t2=500, t3=400, t4=900)  # rtt 음수
    assert not bad.is_sane()
    p = cs.rtt_profile([good, bad])
    assert p.count == 1


# ── 설정 기본값 ──────────────────────────────────────────────────────────────

def test_probe_gap_defaults_to_zero_for_radio_wakefulness():
    """
    기본 간격을 0 으로 바꾼 결정을 못박아 둡니다.
    5ms 였을 때 iOS WiFi 가 매 왕복마다 절전에 들어가 최소 RTT 가 부풀었습니다.
    """
    assert cs.DEFAULT_PROBE_GAP_MS == 0
    assert cs.DEFAULT_WARMUP_COUNT > 0


def test_summary_line_contains_all_percentiles():
    s = [sample(i, (4 + i) * MS) for i in range(20)]
    line = cs.rtt_profile(s).summary_line()
    for k in ("p0", "p10", "p50", "p90", "max"):
        assert k in line


# ── Swift 구현과의 수치 일치 (골든 벡터) ─────────────────────────────────────

def test_golden_vector_matches_swift():
    """
    ★ 두 구현이 같은 입력에 같은 출력을 내는지 못박습니다.

    Swift 쪽 RttProfileTests.testNarrowDistributionMeansFloorReached 와
    **같은 입력**을 씁니다. 여기 기대값이 바뀌면 Swift 쪽도 바뀌어야 합니다.
    """
    s = [sample(i, 4_810_000 + (i % 4) * 100_000) for i in range(40)]
    p = cs.rtt_profile(s)
    assert p.count == 40
    assert p.p0_ns == 4_810_000
    assert p.p100_ns == 5_110_000
    # 40개 중 p50: idx = floor(0.5*39 + 0.5) = floor(20.0) = 20
    rtts = sorted(4_810_000 + (i % 4) * 100_000 for i in range(40))
    assert p.p50_ns == rtts[20]
    # edges = [0.5, 1, 2, 3, 4, 5, 6, 8, 12, 20, 40]
    #   4.81, 4.91 -> 5 보다 작음  -> index 5 ("4~5")
    #   5.01, 5.11 -> 6 보다 작음  -> index 6 ("5~6")
    assert cs.RTT_BUCKET_EDGES_MS[5] == 5
    assert p.buckets[5] == 20
    assert p.buckets[6] == 20
    assert sum(p.buckets) == 40


def test_sub_millisecond_resolution_exists():
    """
    유선 랜으로 바꾸면 RTT 가 1ms 아래로 갈 수 있습니다.
    그때 전부 한 버킷에 뭉치면 개선 여부를 볼 수 없습니다.
    """
    s = [sample(i, 200_000 + i * 10_000) for i in range(20)]  # 0.20~0.39 ms
    p = cs.rtt_profile(s)
    assert p.buckets[0] == 20, "1ms 미만 구간에 해상도가 없습니다"
    assert len([b for b in p.buckets if b > 0]) >= 1
