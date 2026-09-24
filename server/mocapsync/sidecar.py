"""
사이드카 — 영상 파일 옆에 붙는 타임스탬프 기록. PC 쪽 구현.

★ 이 파일이 프로젝트에서 가장 중요한 데이터 구조입니다.

영상은 "무엇이 찍혔는지"만 담습니다. "언제 찍혔는지"는 담지 못합니다.
(컨테이너의 PTS 는 파일 시작 기준 상대시간이라 다른 기기와 비교할 수 없습니다)

3D 복원은 "같은 순간에 두 각도에서 본 점"을 삼각측량합니다. 그래서 프레임
하나하나가 공통 시간축에서 몇 시였는지를 알아야 합니다. 그걸 담는 게 이 파일입니다.
우리 파이프라인의 차별점(키포인트 리샘플러)이 이 파일만 보고 동작합니다.

ios/Sources/MocapSyncCore/Sidecar.swift 와 **키와 판정이 정확히 일치해야** 합니다.
양쪽 테스트가 같은 성질을 검증하므로 한쪽만 고치면 다른 쪽이 깨집니다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

from . import clocksync as cs

NS_PER_MS = cs.NS_PER_MS
NS_PER_S = 1_000_000_000

CURRENT_SCHEMA_VERSION = 1

#: 60fps 에서 반 프레임. 이걸 넘으면 프레임 짝짓기가 모호해집니다.
HALF_FRAME_NS_60 = 8_333_333

# ── 오차 상한 판정 기준 3단계 ────────────────────────────────────────────────
#
# ★ 왜 3단계인가
#
# 2026-09-24 실측에서 이 경로의 바닥은 최소 RTT 4.076 ms = 상한 2.038 ms 였고,
# 설정을 어떻게 바꿔도 4 ms 아래로 내려가지 않았습니다 (DESIGN.md 3.12).
# 즉 설계 목표 2 ms 는 구조적으로 거의 항상 초과합니다.
#
# 이걸 경고로 두면 모든 정상 촬영에 경고가 붙고, 그러면 아무도 경고를 보지
# 않게 됩니다. 그래서 "목표 초과"는 info, "기준선보다 나쁨"은 warning 으로 나눕니다.

#: 설계 목표. 넘으면 info.
TARGET_UNCERTAINTY_NS = 2 * NS_PER_MS

#: 실측 기준선(최악 2.995 ms)보다 나쁘면 warning. 링크 이상 신호.
DEGRADED_UNCERTAINTY_NS = 3 * NS_PER_MS

#: 드리프트 판정에 쓰는 상대 주파수 오차 (ppm).
#
#  휴대기기 수정발진자 규격이 보통 ±20 ppm 이라 두 기기 상대로 40 ppm 을
#  최악값으로 잡습니다.
#  ios 쪽 Sidecar.assumedDriftPpm / ClockOffsetRecord.worstCaseDriftPpm 과
#  같은 값이어야 합니다. 다르면 폰에서는 통과했는데 PC 에서 거부되는 일이 생깁니다.
ASSUMED_DRIFT_PPM = 40.0

#: 누적 절전시간 두 값의 차이가 이 값 이하면 "안 잤다"고 봅니다.
#
#  이 값은 폰에서 시계 두 개를 연달아 읽어 빼서 만들므로, 잔 적이 없어도
#  읽기 간격만큼 수십 ns 씩 달라집니다 (아이폰 11 실측 42 ns).
#  ios 쪽 MonotonicClock.sleepNoiseToleranceNs 와 같은 값이어야 합니다.
#
#  1 ms 근거: 읽기 잡음은 수십 ns ~ 수백 µs, 의미 있는 절전은 수백 ms 이상.
#  두 영역 사이에 넉넉히 들어갑니다. 1 ms 절전을 놓쳐도 피해가 1 ms 라서
#  오차 예산(2 ms) 안입니다.
SLEEP_NOISE_TOLERANCE_NS = 1 * NS_PER_MS

#: 1/500초. 이보다 느리면 모션블러
MAX_EXPOSURE_NS = 2_000_000

#: Swift 쪽 Sidecar 가 내보내는 키 전체.
#: 테스트가 이 집합을 못박습니다. 한쪽만 바뀌면 드러나게 하는 장치입니다.
SIDECAR_KEYS: frozenset[str] = frozenset({
    "schemaVersion",
    "deviceId", "deviceName", "model", "osVersion", "appVersion",
    "sessionId", "role",
    "clock", "clockOffsetNs", "clockUncertaintyNs", "clockMinRttNs",
    "clockMeasuredAtNs", "sleepAtSyncNs", "sleepAtRecordStartNs",
    "timestampSource", "timestampDomainDeltaNs",
    "targetFps", "width", "height", "cameraDeviceType",
    "fieldOfViewDeg", "isBinned", "exposureDurationNs", "iso",
    "lensPosition", "focusLocked", "whiteBalanceLocked",
    "exposureLocked", "stabilization",
    "requestedStartAtMasterNs", "requestedStartAtSlaveNs", "firstFramePtsNs",
    "droppedFrameCount", "thermalAtStart", "thermalAtEnd",
    "batteryAtStart", "batteryAtEnd",
    "frames",
})


@dataclass(frozen=True)
class Issue:
    """검증 결과 한 건."""

    severity: str   # "fatal" | "warning" | "info"
    code: str
    message: str

    def __str__(self) -> str:
        mark = {"fatal": "[치명]", "warning": "[경고]", "info": "[참고]"}[self.severity]
        return f"{mark} {self.code}: {self.message}"


@dataclass
class Sidecar:
    """
    사이드카 한 개 = 카메라 한 대의 촬영 기록.

    ★ 설계 원칙: 변환하지 않고 원본을 저장합니다.
    프레임 시각은 그 폰의 시계 그대로, 오프셋은 별도 필드로 둡니다.
    미리 더해서 저장하면 나중에 오프셋이 틀렸다는 걸 알아도 되돌릴 수 없습니다.
    """

    schema_version: int = CURRENT_SCHEMA_VERSION

    device_id: str = ""
    device_name: str = ""
    model: str = ""
    os_version: str = ""
    app_version: str = ""

    session_id: str = ""
    role: str = "slave"

    clock: str = ""
    clock_offset_ns: int = 0
    clock_uncertainty_ns: int = 0
    clock_min_rtt_ns: int = 0
    clock_measured_at_ns: int = 0
    sleep_at_sync_ns: int = 0
    sleep_at_record_start_ns: int = 0

    timestamp_source: str = ""
    timestamp_domain_delta_ns: int = 0

    target_fps: int = 0
    width: int = 0
    height: int = 0
    camera_device_type: str = ""
    field_of_view_deg: float = 0.0
    is_binned: bool = False
    exposure_duration_ns: int = 0
    iso: float = 0.0
    lens_position: float = 0.0
    focus_locked: bool = False
    white_balance_locked: bool = False
    exposure_locked: bool = False
    stabilization: str = ""

    requested_start_at_master_ns: int | None = None
    requested_start_at_slave_ns: int | None = None
    first_frame_pts_ns: int | None = None

    dropped_frame_count: int = 0
    thermal_at_start: str = ""
    thermal_at_end: str = ""
    battery_at_start: float = -1.0
    battery_at_end: float = -1.0

    frames: list[list[int]] = field(default_factory=list)

    #: 읽는 중 발견한 구조적 문제 (키 누락 등)
    load_issues: list[Issue] = field(default_factory=list)

    # ── 읽기 ────────────────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Sidecar":
        """
        폰이 쓴 JSON 을 읽습니다.

        ★ 키가 없어도 예외를 던지지 않습니다.
        4단계 업로드 서버가 잘못된 파일 하나 때문에 죽으면 안 되고,
        무엇이 왜 잘못됐는지 보고해야 합니다. 그래서 load_issues 에 모읍니다.
        """
        issues: list[Issue] = []

        missing = SIDECAR_KEYS - set(d.keys())
        if missing:
            issues.append(Issue(
                "warning", "missing_keys",
                f"사이드카에 없는 키: {', '.join(sorted(missing))}"))
        extra = set(d.keys()) - SIDECAR_KEYS
        if extra:
            issues.append(Issue(
                "info", "extra_keys",
                f"모르는 키(무시함): {', '.join(sorted(extra))}"))

        def i(k: str, default: int = 0) -> int:
            v = d.get(k, default)
            return default if v is None else int(v)

        def f(k: str, default: float = 0.0) -> float:
            v = d.get(k, default)
            return default if v is None else float(v)

        def s(k: str, default: str = "") -> str:
            v = d.get(k, default)
            return default if v is None else str(v)

        def b(k: str, default: bool = False) -> bool:
            v = d.get(k, default)
            return default if v is None else bool(v)

        def opt_i(k: str) -> int | None:
            v = d.get(k)
            return None if v is None else int(v)

        raw_frames = d.get("frames") or []
        frames: list[list[int]] = []
        for fr in raw_frames:
            try:
                frames.append([int(x) for x in fr])
            except (TypeError, ValueError):
                issues.append(Issue(
                    "fatal", "frame_parse",
                    f"프레임 항목을 읽을 수 없습니다: {fr!r}"))
                break

        return cls(
            schema_version=i("schemaVersion", CURRENT_SCHEMA_VERSION),
            device_id=s("deviceId"),
            device_name=s("deviceName"),
            model=s("model"),
            os_version=s("osVersion"),
            app_version=s("appVersion"),
            session_id=s("sessionId"),
            role=s("role", "slave"),
            clock=s("clock"),
            clock_offset_ns=i("clockOffsetNs"),
            clock_uncertainty_ns=i("clockUncertaintyNs"),
            clock_min_rtt_ns=i("clockMinRttNs"),
            clock_measured_at_ns=i("clockMeasuredAtNs"),
            sleep_at_sync_ns=i("sleepAtSyncNs"),
            sleep_at_record_start_ns=i("sleepAtRecordStartNs"),
            timestamp_source=s("timestampSource"),
            timestamp_domain_delta_ns=i("timestampDomainDeltaNs"),
            target_fps=i("targetFps"),
            width=i("width"),
            height=i("height"),
            camera_device_type=s("cameraDeviceType"),
            field_of_view_deg=f("fieldOfViewDeg"),
            is_binned=b("isBinned"),
            exposure_duration_ns=i("exposureDurationNs"),
            iso=f("iso"),
            lens_position=f("lensPosition"),
            focus_locked=b("focusLocked"),
            white_balance_locked=b("whiteBalanceLocked"),
            exposure_locked=b("exposureLocked"),
            stabilization=s("stabilization"),
            requested_start_at_master_ns=opt_i("requestedStartAtMasterNs"),
            requested_start_at_slave_ns=opt_i("requestedStartAtSlaveNs"),
            first_frame_pts_ns=opt_i("firstFramePtsNs"),
            dropped_frame_count=i("droppedFrameCount"),
            thermal_at_start=s("thermalAtStart"),
            thermal_at_end=s("thermalAtEnd"),
            battery_at_start=f("batteryAtStart", -1.0),
            battery_at_end=f("batteryAtEnd", -1.0),
            frames=frames,
            load_issues=issues,
        )

    @classmethod
    def load(cls, path: str | Path) -> "Sidecar":
        p = Path(path)
        return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def to_dict(self) -> dict[str, Any]:
        """Swift 쪽과 같은 카멜케이스 키로 내보냅니다 (왕복 시험용)."""
        return {
            "schemaVersion": self.schema_version,
            "deviceId": self.device_id,
            "deviceName": self.device_name,
            "model": self.model,
            "osVersion": self.os_version,
            "appVersion": self.app_version,
            "sessionId": self.session_id,
            "role": self.role,
            "clock": self.clock,
            "clockOffsetNs": self.clock_offset_ns,
            "clockUncertaintyNs": self.clock_uncertainty_ns,
            "clockMinRttNs": self.clock_min_rtt_ns,
            "clockMeasuredAtNs": self.clock_measured_at_ns,
            "sleepAtSyncNs": self.sleep_at_sync_ns,
            "sleepAtRecordStartNs": self.sleep_at_record_start_ns,
            "timestampSource": self.timestamp_source,
            "timestampDomainDeltaNs": self.timestamp_domain_delta_ns,
            "targetFps": self.target_fps,
            "width": self.width,
            "height": self.height,
            "cameraDeviceType": self.camera_device_type,
            "fieldOfViewDeg": self.field_of_view_deg,
            "isBinned": self.is_binned,
            "exposureDurationNs": self.exposure_duration_ns,
            "iso": self.iso,
            "lensPosition": self.lens_position,
            "focusLocked": self.focus_locked,
            "whiteBalanceLocked": self.white_balance_locked,
            "exposureLocked": self.exposure_locked,
            "stabilization": self.stabilization,
            "requestedStartAtMasterNs": self.requested_start_at_master_ns,
            "requestedStartAtSlaveNs": self.requested_start_at_slave_ns,
            "firstFramePtsNs": self.first_frame_pts_ns,
            "droppedFrameCount": self.dropped_frame_count,
            "thermalAtStart": self.thermal_at_start,
            "thermalAtEnd": self.thermal_at_end,
            "batteryAtStart": self.battery_at_start,
            "batteryAtEnd": self.battery_at_end,
            "frames": self.frames,
        }

    # ── 파생 정보 ───────────────────────────────────────────────────────────

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def timestamps_ns(self) -> list[int]:
        return [int(fr[1]) for fr in self.frames if len(fr) >= 2]

    def to_master_ns(self, slave_ns: int) -> int:
        """이 기기 시각 -> 마스터(공통) 시각."""
        return cs.slave_to_master_ns(slave_ns, self.clock_offset_ns)

    @property
    def timestamps_master_ns(self) -> list[int]:
        return [self.to_master_ns(t) for t in self.timestamps_ns]

    def master_timeline(self) -> list[tuple[int, int]]:
        """4단계 리샘플러 입력: [(프레임번호, 마스터시각), ...]"""
        return cs.frames_to_master_timeline(self.frames, self.clock_offset_ns)

    @property
    def duration_ns(self) -> int:
        t = self.timestamps_ns
        return (t[-1] - t[0]) if len(t) >= 2 else 0

    @property
    def slept_since_sync(self) -> bool:
        """
        ★ 동기 이후 폰이 잤는가. 잤다면 clock_offset_ns 는 무효입니다.

        ★★ 반드시 허용 오차를 써야 합니다.

        누적 절전시간은 폰에서 시계 두 개(CLOCK_MONOTONIC_RAW, CLOCK_UPTIME_RAW)를
        연달아 읽어 빼서 만듭니다. 그래서 잔 적이 없어도 읽기 간격만큼
        수십 ns 씩 값이 달라집니다 (아이폰 11 실측 42 ns).

        처음에 != 로 비교했더니 모든 촬영이 '잤다'로 판정되어 치명 거부됐습니다.
        (2026-09-24 3단계 시험에서 두 번 연속 "사용 불가"가 난 원인)
        """
        return self.sleep_since_sync_ns > SLEEP_NOISE_TOLERANCE_NS

    @property
    def sleep_since_sync_ns(self) -> int:
        return self.sleep_at_record_start_ns - self.sleep_at_sync_ns

    def interval_stats(self) -> dict:
        return cs.frame_interval_stats(self.frames)

    # ── 검증 ────────────────────────────────────────────────────────────────

    def validate(self, target_fps_tolerance: float = 0.05) -> list[Issue]:
        """
        ★ 사이드카는 조용히 망가집니다.

        프레임 0개, 시각 역행, fps 불일치, 낡은 오프셋 — 전부 예외 없이
        그냥 "이상한 3D"로 끝납니다. Pose2Sim 을 돌리기 **전에** 걸러야 합니다.

        ios 쪽 Sidecar.validate() 와 같은 코드·같은 심각도를 내야 합니다.
        """
        out: list[Issue] = list(self.load_issues)

        if self.schema_version != CURRENT_SCHEMA_VERSION:
            out.append(Issue(
                "warning", "schema_version",
                f"사이드카 형식 버전이 {self.schema_version} 입니다 "
                f"(현재 {CURRENT_SCHEMA_VERSION})."))

        if not self.device_id:
            out.append(Issue(
                "fatal", "no_device_id",
                "deviceId 가 비었습니다. PC 가 캘리브레이션을 매칭할 수 없습니다."))

        # ── 프레임 ──────────────────────────────────────────────────────────
        if not self.frames:
            out.append(Issue(
                "fatal", "no_frames",
                "프레임이 0개입니다. 녹화가 실제로 되지 않았습니다."))
            return out

        bad = next((fr for fr in self.frames if len(fr) != 2), None)
        if bad is not None:
            out.append(Issue(
                "fatal", "frame_shape",
                f"프레임 항목은 [번호, 시각] 2개여야 하는데 {len(bad)}개인 항목이 있습니다."))
            return out

        ts = self.timestamps_ns
        for k in range(1, len(ts)):
            if ts[k] <= ts[k - 1]:
                out.append(Issue(
                    "fatal", "non_monotonic",
                    f"프레임 시각이 증가하지 않습니다: {k - 1}번 {ts[k - 1]} → "
                    f"{k}번 {ts[k]}. 리샘플러의 보간이 불가능합니다."))
                break

        idx = [int(fr[0]) for fr in self.frames]
        if idx[0] != 0:
            out.append(Issue(
                "warning", "index_start",
                f"프레임 번호가 0 이 아니라 {idx[0]} 부터 시작합니다."))
        gaps = sum(1 for k in range(1, len(idx)) if idx[k] != idx[k - 1] + 1)
        if gaps:
            out.append(Issue(
                "warning", "index_gap",
                f"프레임 번호에 {gaps}곳의 건너뜀이 있습니다."))

        # ── fps ─────────────────────────────────────────────────────────────
        st = self.interval_stats()
        est = st.get("estimated_fps", 0.0)
        if self.target_fps > 0 and est > 0:
            rel = abs(est - self.target_fps) / self.target_fps
            if rel > target_fps_tolerance:
                out.append(Issue(
                    "warning", "fps_mismatch",
                    f"실측 {est:.2f} fps 가 목표 {self.target_fps} fps 와 "
                    f"{rel * 100:.1f}% 차이납니다."))
        if self.target_fps < 60:
            out.append(Issue(
                "warning", "fps_below_60",
                f"목표 fps 가 {self.target_fps} 입니다. Pose2Sim 공식 문서는 "
                "60Hz 미만에서 정확도가 떨어진다고 밝힙니다."))
        drops = st.get("suspected_drops", 0)
        if drops:
            sev = "warning" if drops / max(1, st.get("count", 1)) > 0.02 else "info"
            out.append(Issue(
                sev, "frame_drops",
                f"간격이 중앙값의 1.5배를 넘는 곳이 {drops}곳입니다 "
                f"(최대 {st.get('max_interval_ms', 0):.1f} ms). 발열 스로틀링 의심."))
        if self.dropped_frame_count:
            out.append(Issue(
                "warning", "reported_drops",
                f"카메라가 {self.dropped_frame_count}개 프레임을 버렸다고 보고했습니다."))

        # ── 시계 ────────────────────────────────────────────────────────────
        #
        # ★ 가장 놓치기 쉬운 실패: 오프셋이 낡은 것.
        #   폰이 자면 CLOCK_UPTIME_RAW 가 멈추므로 이전 오프셋이 즉시 무효가 됩니다.
        #   (2026-09-24 실측: 52분 중 34분을 자면서 오프셋이 34분 어긋남)
        if self.slept_since_sync:
            out.append(Issue(
                "fatal", "slept_since_sync",
                f"동기 측정 후 녹화 시작까지 폰이 "
                f"{self.sleep_since_sync_ns / NS_PER_S:.1f}초 잠들었습니다. "
                "CLOCK_UPTIME_RAW 는 절전 중 멈추므로 clockOffsetNs 가 무효입니다. "
                "촬영 직전에 동기를 다시 해야 합니다."))

        if self.clock_uncertainty_ns <= 0:
            out.append(Issue(
                "fatal", "no_clock_sync",
                "clockUncertaintyNs 가 0 이하입니다. 클럭 동기를 하지 않았거나 "
                "측정에 실패했습니다."))
        elif self.clock_uncertainty_ns > HALF_FRAME_NS_60:
            out.append(Issue(
                "fatal", "clock_uncertainty_over_half_frame",
                f"오차 상한 {self.clock_uncertainty_ns / NS_PER_MS:.3f} ms 가 "
                "반 프레임(8.333 ms)을 넘습니다. 프레임 짝짓기가 모호해집니다."))
        elif self.clock_uncertainty_ns > DEGRADED_UNCERTAINTY_NS:
            # 실측 기준선(4.076~5.989 ms RTT -> 2.038~2.995 ms)보다 나쁩니다.
            out.append(Issue(
                "warning", "clock_uncertainty_degraded",
                f"오차 상한 {self.clock_uncertainty_ns / NS_PER_MS:.3f} ms 가 "
                f"실측 기준선 {DEGRADED_UNCERTAINTY_NS / NS_PER_MS:.1f} ms 보다 "
                "나쁩니다. WiFi 상태가 평소보다 안 좋습니다. "
                "촬영 전에 동기를 다시 해 보세요."))
        elif self.clock_uncertainty_ns > TARGET_UNCERTAINTY_NS:
            # ★ 정보로만 남깁니다. 경고로 올리면 모든 정상 촬영에 붙습니다.
            #   실측된 이 경로의 바닥이 4.076 ms RTT = 2.038 ms 상한이므로,
            #   2 ms 목표는 구조적으로 거의 항상 초과합니다 (DESIGN.md 3.12).
            out.append(Issue(
                "info", "clock_uncertainty_over_target",
                f"오차 상한 {self.clock_uncertainty_ns / NS_PER_MS:.3f} ms 가 "
                "설계 목표 2 ms 를 넘습니다. 공유기 WiFi 에서는 정상 범위입니다 "
                "(실측 바닥 2.038 ms)."))

        if abs(self.timestamp_domain_delta_ns) > NS_PER_MS:
            out.append(Issue(
                "fatal", "clock_domain_mismatch",
                f"동기 시계와 프레임 시계의 도메인 차이가 "
                f"{self.timestamp_domain_delta_ns / NS_PER_MS:.3f} ms 입니다. "
                "같은 도메인이어야 합니다 (규약 §1)."))

        if ts and ts[0] < self.clock_measured_at_ns:
            out.append(Issue(
                "warning", "frames_before_sync",
                "첫 프레임이 오프셋 측정보다 먼저 찍혔습니다. "
                "동기 → 녹화 순서를 확인하세요."))

        # ── 동기 나이와 드리프트 ────────────────────────────────────────────
        #
        # ★ 가장 안 보이는 실패 모드입니다.
        #
        # 두 기기의 수정발진자 주파수가 미세하게 다릅니다. 규격은 보통 ±20 ppm 이고
        # 상대 드리프트는 최악 40 ppm 입니다. 1초에 40 µs 씩 조용히 어긋납니다.
        #   2 ms 예산 소진 = 50초 / 반 프레임(8.33ms) 소진 = 약 3분 30초
        # 오프셋을 재고 한참 뒤에 찍으면 측정 상한은 좋은데 실제로는 어긋납니다.
        # 측정값만으로는 알 수 없으므로 나이로 판정합니다.
        if (self.clock_uncertainty_ns > 0 and ts
                and self.clock_measured_at_ns > 0
                and ts[0] >= self.clock_measured_at_ns):
            age = ts[0] - self.clock_measured_at_ns
            drift = int(age * ASSUMED_DRIFT_PPM / 1e6)
            effective = self.clock_uncertainty_ns + drift

            if effective > HALF_FRAME_NS_60:
                out.append(Issue(
                    "fatal", "sync_too_old",
                    f"동기를 {age / NS_PER_S:.0f}초 전에 측정했습니다. "
                    f"수정발진자 차이({ASSUMED_DRIFT_PPM:.0f} ppm 가정)로 최악 "
                    f"{drift / NS_PER_MS:.2f} ms 드리프트가 쌓여 실효 상한이 "
                    f"{effective / NS_PER_MS:.2f} ms 입니다. "
                    "반 프레임(8.333 ms)을 넘으므로 프레임을 잘못 짝지을 수 "
                    "있습니다. 촬영 직전에 동기를 다시 하세요."))
            elif age > 60 * NS_PER_S:
                out.append(Issue(
                    "warning", "sync_aging",
                    f"동기를 {age / NS_PER_S:.0f}초 전에 측정했습니다. 최악 "
                    f"{drift / NS_PER_MS:.2f} ms 드리프트가 쌓였을 수 있어 "
                    f"실효 상한은 {effective / NS_PER_MS:.2f} ms 입니다. "
                    "다음에는 촬영 직전에 동기하세요."))

        # ── 예약 시작 ───────────────────────────────────────────────────────
        want = self.requested_start_at_slave_ns
        got = self.first_frame_pts_ns
        if want is not None and got is not None:
            late = got - want
            if late < 0:
                out.append(Issue(
                    "fatal", "started_early",
                    f"첫 프레임이 예약 시각보다 {-late / NS_PER_MS:.3f} ms 이릅니다. "
                    "논리 오류입니다."))
            elif late > 2.0 * NS_PER_S / max(self.target_fps, 1):
                out.append(Issue(
                    "warning", "started_late",
                    f"첫 프레임이 예약 시각보다 {late / NS_PER_MS:.3f} ms 늦습니다 "
                    f"(프레임 간격 {1000.0 / max(self.target_fps, 1):.3f} ms)."))

        # ── 촬영 설정 ───────────────────────────────────────────────────────
        if self.stabilization != "off":
            out.append(Issue(
                "fatal", "stabilization_on",
                f"안정화가 '{self.stabilization}' 입니다. 프레임마다 화면이 변형되어 "
                "캘리브레이션이 무의미해집니다."))
        if not self.exposure_locked:
            out.append(Issue(
                "warning", "exposure_not_locked",
                "노출이 고정되지 않았습니다. 밝기가 변하면 2D 검출이 흔들립니다."))
        if not self.white_balance_locked:
            out.append(Issue(
                "warning", "wb_not_locked", "화이트밸런스가 고정되지 않았습니다."))
        # 고정초점 렌즈는 focus_locked=False 가 정상이므로 경고하지 않습니다
        # (초광각·전면은 초점 기구가 없습니다 — DESIGN.md §3.10)
        if self.exposure_duration_ns > MAX_EXPOSURE_NS:
            out.append(Issue(
                "warning", "shutter_too_slow",
                f"셔터 {self.exposure_duration_ns / 1000:.0f} µs "
                f"(1/{NS_PER_S / max(self.exposure_duration_ns, 1):.0f}초) 가 "
                "1/500초보다 느립니다. 모션블러가 생깁니다."))
        if "Dual" in self.camera_device_type or "Triple" in self.camera_device_type:
            out.append(Issue(
                "fatal", "virtual_camera",
                f"합성(가상) 카메라 '{self.camera_device_type}' 로 촬영했습니다. "
                "촬영 중 렌즈가 바뀌면 초점거리·왜곡이 통째로 달라집니다."))

        if self.duration_ns < 5 * NS_PER_S:
            out.append(Issue(
                "warning", "too_short",
                f"길이 {self.duration_ns / NS_PER_S:.2f}초. 촬영 규칙은 5초 이상입니다."))

        return out

    @property
    def is_usable(self) -> bool:
        return not any(i.severity == "fatal" for i in self.validate())

    def validation_report(self) -> list[str]:
        issues = self.validate()
        if not issues:
            return ["문제 없음 ✔"]
        return [str(i) for i in issues]


# ── 여러 대 한꺼번에 ─────────────────────────────────────────────────────────

def check_session(sidecars: Iterable[Sidecar]) -> list[Issue]:
    """
    한 세션의 카메라 여러 대를 함께 검사합니다.

    ★ 개별 사이드카는 다 정상인데 **같이 놓으면 틀린** 경우가 있습니다.
    그게 여기서 잡힙니다. 4단계 업로드 서버가 Pose2Sim 을 돌리기 전에 호출합니다.
    """
    cams = list(sidecars)
    out: list[Issue] = []

    if len(cams) < 2:
        out.append(Issue(
            "warning", "single_camera",
            f"카메라가 {len(cams)}대입니다. 삼각측량에는 최소 2대가 필요합니다."))
        return out

    ids = [c.device_id for c in cams]
    if len(set(ids)) != len(ids):
        out.append(Issue(
            "fatal", "duplicate_device_id",
            f"deviceId 가 중복됩니다: {ids}. 캘리브레이션이 엉뚱하게 매칭됩니다."))

    sessions = {c.session_id for c in cams}
    if len(sessions) > 1:
        out.append(Issue(
            "fatal", "session_mismatch",
            f"서로 다른 세션의 파일이 섞였습니다: {sorted(sessions)}"))

    fps = {c.target_fps for c in cams}
    if len(fps) > 1:
        out.append(Issue(
            "warning", "fps_mismatch_across_cameras",
            f"카메라마다 목표 fps 가 다릅니다: {sorted(fps)}. "
            "리샘플러가 공통 격자로 맞추긴 하지만 의도한 설정인지 확인하세요."))

    res = {(c.width, c.height) for c in cams}
    if len(res) > 1:
        out.append(Issue(
            "info", "resolution_mismatch",
            f"해상도가 다릅니다: {sorted(res)}. 캘리브레이션이 카메라별로 "
            "따로 되어 있으면 문제없습니다."))

    # ★ 공통 시간축에서 실제로 겹치는 구간이 있는지.
    #   없으면 삼각측량할 프레임이 한 장도 없습니다.
    spans = []
    for c in cams:
        t = c.timestamps_master_ns
        if len(t) >= 2:
            spans.append((t[0], t[-1], c.device_id))
    if len(spans) >= 2:
        lo = max(s[0] for s in spans)
        hi = min(s[1] for s in spans)
        if hi <= lo:
            out.append(Issue(
                "fatal", "no_overlap",
                "공통 시간축에서 겹치는 구간이 없습니다. "
                "클럭 오프셋이 틀렸거나 촬영 시각이 완전히 다릅니다."))
        else:
            overlap_s = (hi - lo) / NS_PER_S
            out.append(Issue(
                "info", "overlap",
                f"공통 구간 {overlap_s:.2f}초."))
            if overlap_s < 5:
                out.append(Issue(
                    "warning", "overlap_too_short",
                    f"공통 구간이 {overlap_s:.2f}초뿐입니다. 5초 이상을 권합니다."))

    worst = max((c.clock_uncertainty_ns for c in cams), default=0)
    if worst > 0:
        out.append(Issue(
            "info", "worst_uncertainty",
            f"가장 나쁜 오차 상한 {worst / NS_PER_MS:.3f} ms "
            "(두 카메라 사이의 상대 오차는 최악의 경우 이 값의 2배입니다)."))

    return out
