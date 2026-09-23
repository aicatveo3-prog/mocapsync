#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
클럭 동기 계산 — 순수 함수만. 네트워크 I/O 없음.

여기에 I/O 를 섞지 않는 이유:
iOS 앱(Swift)이 이 로직을 그대로 옮겨 구현해야 하는데, 개발자는 Windows 에서
Swift 를 컴파일할 수 없습니다. 그래서 계산 부분을 순수 함수로 떼어내
**PC 에서 완전히 테스트**한 뒤, Swift 쪽은 이 테스트와 같은 입력/출력을
재현하는지만 확인하면 되게 만듭니다.

수학 (docs/PROTOCOL.md §4 와 동일)
--------------------------------
    t1 = 슬레이브 송신 시각 (슬레이브 시계)
    t2 = 마스터 수신 시각   (마스터 시계)
    t3 = 마스터 송신 시각   (마스터 시계)
    t4 = 슬레이브 수신 시각 (슬레이브 시계)

    오프셋  θ = ((t2 - t1) + (t3 - t4)) / 2      부호: 마스터시각 = 슬레이브시각 + θ
    왕복시간 δ = (t4 - t1) - (t3 - t2)

    편도 지연을 d_up, d_dn 이라 하면
        θ̂ = θ + (d_up - d_dn) / 2
    이고 d_up + d_dn = δ, d_up,d_dn >= 0 이므로

        |오차| <= δ / 2        <-- 보장된 상한

    즉 최소 RTT 가 4ms 미만이면 오프셋 오차는 2ms 미만입니다.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, asdict
from typing import Iterable, Sequence

NS_PER_MS = 1_000_000

#: 설계 문서의 목표: 오차 2ms 미만
DEFAULT_TARGET_NS = 2 * NS_PER_MS

#: 기본 왕복 횟수 (설계 문서: 20~50회)
DEFAULT_PROBE_COUNT = 40

#: 최종 오프셋을 뽑을 때 쓸 최소-RTT 샘플 개수
DEFAULT_BEST_K = 1

#: spread(실제 흔들림) 를 계산할 때 볼 샘플 개수
SPREAD_WINDOW = 10


@dataclass(frozen=True)
class TimeSample:
    """왕복 1회의 네 시각."""

    seq: int
    t1: int  # 슬레이브 송신
    t2: int  # 마스터 수신
    t3: int  # 마스터 송신
    t4: int  # 슬레이브 수신

    @property
    def offset_ns(self) -> int:
        """마스터시각 = 슬레이브시각 + offset_ns"""
        return ((self.t2 - self.t1) + (self.t3 - self.t4)) // 2

    @property
    def rtt_ns(self) -> int:
        """왕복 시간에서 마스터의 처리 시간을 뺀 값 = 순수 네트워크 왕복."""
        return (self.t4 - self.t1) - (self.t3 - self.t2)

    @property
    def server_processing_ns(self) -> int:
        """마스터가 요청을 받고 응답을 보낼 때까지 걸린 시간."""
        return self.t3 - self.t2

    def is_sane(self) -> bool:
        """
        물리적으로 불가능한 샘플을 걸러냅니다.

        걸러지는 경우:
          - RTT 가 음수      -> 시계 점프 또는 버그
          - 마스터 처리시간이 음수
          - 슬레이브 시각이 역행
          - RTT 가 10초 초과 -> 명백한 이상치
        """
        if self.rtt_ns < 0:
            return False
        if self.server_processing_ns < 0:
            return False
        if self.t4 < self.t1:
            return False
        if self.rtt_ns > 10_000_000_000:
            return False
        return True


@dataclass(frozen=True)
class SyncEstimate:
    """최종 동기 결과. 슬레이브가 마스터에게 보고하고, 화면에 표시합니다."""

    offset_ns: int
    min_rtt_ns: int
    #: 보장된 오차 상한 = min_rtt/2. "이 값 이하로 틀렸다"고 말할 수 있는 숫자.
    uncertainty_ns: int
    #: 최소 RTT 샘플들의 오프셋 최대-최소. 실제 관측된 흔들림.
    spread_ns: int
    samples_total: int
    samples_used: int
    samples_rejected: int
    median_rtt_ns: int

    @property
    def offset_ms(self) -> float:
        return self.offset_ns / NS_PER_MS

    @property
    def min_rtt_ms(self) -> float:
        return self.min_rtt_ns / NS_PER_MS

    @property
    def uncertainty_ms(self) -> float:
        return self.uncertainty_ns / NS_PER_MS

    @property
    def spread_ms(self) -> float:
        return self.spread_ns / NS_PER_MS

    def meets_target(self, target_ns: int = DEFAULT_TARGET_NS) -> bool:
        """
        성공 판정.

        uncertainty(=min_rtt/2) 로 판정합니다. 이건 '증명된 상한'이라
        '아마 맞을 것 같다'가 아니라 '이 값보다 나쁠 수 없다'입니다.

        ★ samples_used == 0 을 반드시 먼저 걸러야 합니다.
        측정이 하나도 없으면 min_rtt=0, uncertainty=0 이 되어
        "0 < 2ms 이므로 통과"라는 거짓 성공이 나옵니다.
        (테스트 test_estimate_empty 가 이 버그를 잡았습니다)
        """
        if self.samples_used == 0:
            return False
        return self.uncertainty_ns < target_ns

    def verdict(self, target_ns: int = DEFAULT_TARGET_NS) -> str:
        if self.samples_used == 0:
            return "측정 실패 (유효 샘플 없음)"
        if self.meets_target(target_ns):
            return (f"통과 — 오차 상한 {self.uncertainty_ms:.3f} ms "
                    f"< 목표 {target_ns / NS_PER_MS:.1f} ms")
        return (f"미달 — 오차 상한 {self.uncertainty_ms:.3f} ms "
                f">= 목표 {target_ns / NS_PER_MS:.1f} ms "
                f"(최소 RTT {self.min_rtt_ms:.3f} ms 를 줄여야 합니다)")

    def summary_lines(self) -> list[str]:
        """로그/화면용 여러 줄 요약."""
        return [
            f"오프셋       = {self.offset_ms:+.3f} ms  ({self.offset_ns:+d} ns)",
            f"최소 RTT     = {self.min_rtt_ms:.3f} ms",
            f"오차 상한    = {self.uncertainty_ms:.3f} ms   <- min_rtt/2, 보장값",
            f"실측 흔들림  = {self.spread_ms:.3f} ms   <- 최소RTT {SPREAD_WINDOW}개의 오프셋 폭",
            f"중앙값 RTT   = {self.median_rtt_ns / NS_PER_MS:.3f} ms",
            f"샘플         = 사용 {self.samples_used} / 전체 {self.samples_total} "
            f"(폐기 {self.samples_rejected})",
        ]

    def to_dict(self) -> dict:
        return asdict(self)


def estimate(samples: Iterable[TimeSample],
             best_k: int = DEFAULT_BEST_K) -> SyncEstimate:
    """
    샘플 묶음에서 최종 오프셋을 추정합니다.

    절차 (docs/PROTOCOL.md §4.4 와 동일해야 함):
      1. 비정상 샘플 폐기
      2. RTT 오름차순 정렬
      3. 상위 best_k 개의 오프셋 중앙값을 최종 오프셋으로
      4. 최소 RTT 로 오차 상한 계산

    왜 최소 RTT 샘플만 쓰는가:
      네트워크 큐잉은 지연을 '늘리기만' 합니다. 줄이지 못합니다.
      따라서 RTT 가 가장 작은 샘플이 큐잉에 가장 덜 오염된 샘플이고,
      그 샘플의 오차 상한(δ/2)도 가장 작습니다.
    """
    all_samples = list(samples)
    good = [s for s in all_samples if s.is_sane()]
    rejected = len(all_samples) - len(good)

    if not good:
        return SyncEstimate(
            offset_ns=0, min_rtt_ns=0, uncertainty_ns=0, spread_ns=0,
            samples_total=len(all_samples), samples_used=0,
            samples_rejected=rejected, median_rtt_ns=0,
        )

    by_rtt = sorted(good, key=lambda s: s.rtt_ns)
    k = max(1, min(best_k, len(by_rtt)))
    chosen = by_rtt[:k]

    offset = int(statistics.median([s.offset_ns for s in chosen]))
    min_rtt = by_rtt[0].rtt_ns

    window = by_rtt[:min(SPREAD_WINDOW, len(by_rtt))]
    offsets_w = [s.offset_ns for s in window]
    spread = max(offsets_w) - min(offsets_w)

    return SyncEstimate(
        offset_ns=offset,
        min_rtt_ns=min_rtt,
        uncertainty_ns=min_rtt // 2,
        spread_ns=spread,
        samples_total=len(all_samples),
        samples_used=k,
        samples_rejected=rejected,
        median_rtt_ns=int(statistics.median([s.rtt_ns for s in by_rtt])),
    )


# ── 시각 변환 ────────────────────────────────────────────────────────────────

def master_to_slave_ns(master_ns: int, offset_ns: int) -> int:
    """마스터 시각 -> 슬레이브 시각.  마스터 = 슬레이브 + offset  이므로 뺍니다."""
    return master_ns - offset_ns


def slave_to_master_ns(slave_ns: int, offset_ns: int) -> int:
    """슬레이브 시각 -> 마스터 시각."""
    return slave_ns + offset_ns


@dataclass(frozen=True)
class ScheduleCheck:
    """예약 시작 명령이 실행 가능한지 판정한 결과."""

    ok: bool
    start_at_slave_ns: int
    lead_ns: int
    reason: str = ""

    @property
    def lead_ms(self) -> float:
        return self.lead_ns / NS_PER_MS


#: 예약 시각까지 최소로 남아 있어야 하는 여유. 이보다 짧으면 거부합니다.
#: 카메라 세션은 미리 열어두지만, 스레드 깨우기/스케줄링 지연을 흡수할 여유가 필요합니다.
DEFAULT_MIN_LEAD_NS = 300 * NS_PER_MS

#: 정밀 대기에서 스핀으로 넘어가는 시점. 이 값보다 적게 남으면 바쁜대기.
#:
#: 20ms 로 잡은 근거 (실측):
#:   Windows 의 asyncio 타이머 해상도는 약 15.6ms 입니다. 3ms 마진으로 테스트했더니
#:   대략 대기 단계가 마진을 넘겨 버려 +4.2ms 늦었습니다. 마진이 타이머 해상도보다
#:   커야 스핀 구간에 확실히 진입합니다.
#:   iOS 는 mach_wait_until() 이 커널 수준에서 정밀하게 깨워주므로 더 작아도 됩니다.
DEFAULT_SPIN_MARGIN_NS = 20 * NS_PER_MS


def check_schedule(start_at_master_ns: int,
                   offset_ns: int,
                   now_slave_ns: int,
                   min_lead_ns: int = DEFAULT_MIN_LEAD_NS) -> ScheduleCheck:
    """
    예약 시작을 받아들일 수 있는지 판정.

    늦게 도착한 명령을 억지로 따라가면 오히려 동기가 틀어집니다.
    남은 시간이 부족하면 거부하고 마스터가 다시 예약하게 하는 것이 맞습니다.
    """
    start_at_slave = master_to_slave_ns(start_at_master_ns, offset_ns)
    lead = start_at_slave - now_slave_ns
    if lead < min_lead_ns:
        return ScheduleCheck(
            ok=False, start_at_slave_ns=start_at_slave, lead_ns=lead,
            reason=(f"lead_too_small: 남은 {lead / NS_PER_MS:.1f} ms "
                    f"< 최소 {min_lead_ns / NS_PER_MS:.1f} ms"),
        )
    return ScheduleCheck(ok=True, start_at_slave_ns=start_at_slave, lead_ns=lead)


# ── 프레임 타임스탬프 -> 공통 시간축 ──────────────────────────────────────────

def frames_to_master_timeline(frames: Sequence[Sequence[int]],
                              offset_ns: int) -> list[tuple[int, int]]:
    """
    사이드카의 frames([[프레임번호, 로컬ns], ...]) 를 마스터 시간축으로 옮깁니다.

    4단계 리샘플러의 첫 단계입니다. 이걸 거친 뒤 모든 카메라를
    공통 60Hz 격자에 보간합니다.
    """
    return [(int(idx), slave_to_master_ns(int(ts), offset_ns)) for idx, ts in frames]


def frame_interval_stats(frames: Sequence[Sequence[int]]) -> dict:
    """
    프레임 간격 통계. 프레임 드롭과 발열 스로틀링을 잡아내는 용도.

    반환: 평균/중앙값/최소/최대 간격(ms), 추정 fps, 드롭 의심 개수
    """
    ts = [int(t) for _, t in frames]
    if len(ts) < 2:
        return {"count": len(ts)}
    d = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    med = statistics.median(d)
    # 중앙값의 1.5배를 넘으면 프레임을 흘린 것으로 봅니다
    drops = sum(1 for x in d if x > med * 1.5)
    return {
        "count": len(ts),
        "mean_interval_ms": statistics.mean(d) / NS_PER_MS,
        "median_interval_ms": med / NS_PER_MS,
        "min_interval_ms": min(d) / NS_PER_MS,
        "max_interval_ms": max(d) / NS_PER_MS,
        "stdev_interval_ms": (statistics.stdev(d) / NS_PER_MS) if len(d) > 1 else 0.0,
        "estimated_fps": (1e9 / med) if med else 0.0,
        "suspected_drops": drops,
    }
