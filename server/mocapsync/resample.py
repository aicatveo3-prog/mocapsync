"""
키포인트 리샘플러 — 여러 카메라의 2D 관절을 공통 시간축으로 맞춥니다.

★ 이 프로젝트의 핵심 차별점입니다.

문제
----
카메라 두 대가 같은 60fps 로 찍어도 **프레임을 찍는 순간이 서로 다릅니다.**
A 는 0.0 / 16.7 / 33.3 ms 에, B 는 5.2 / 21.9 / 38.5 ms 에 찍는 식입니다.
3D 복원은 "같은 순간 두 각도에서 본 점"을 삼각측량하므로, A 의 N 번째와 B 의
N 번째를 그대로 짝지으면 최대 반 프레임(8.3 ms) 어긋난 점끼리 계산하게 됩니다.
손목이 5 m/s 로 움직이면 8.3 ms 에 4 cm 입니다.

Pose2Sim 의 synchronization() 은 움직임 상관으로 **정수 프레임** 오프셋을 찾습니다.
프레임보다 작은 어긋남은 남습니다.

해법
----
각 카메라의 프레임 시각(사이드카, 공통 시계로 변환)을 알고 있으므로,
관절 좌표를 시간의 함수로 보고 **공통 60Hz 격자**에서 다시 읽습니다.
    x_cam(t) 를 곡선으로 이어, 격자 시각 t_k 에서 값을 구함
    (기본 makima — 설계 문서는 cubic spline 이었으나 실측으로 바꿨습니다. DEFAULT_METHOD 참고)
그러면 모든 카메라가 정확히 같은 순간 t_k 의 좌표를 갖게 됩니다.

 - 영상을 다시 인코딩하지 않습니다 (화질 손실 없음, 시간 절약)
 - 프레임 드롭(발열)도 자동 보상됩니다 — 빠진 프레임 대신 앞뒤를 시각대로 잇습니다
 - 결과를 pose-sync/ 에 쓰면 Pose2Sim 의 synchronization() 을 건너뜁니다
   (personAssociation / triangulation 은 pose-sync/ 가 있으면 그걸 읽습니다)

지키는 것
--------
 - **사람을 고르지 않습니다.** 추적기가 붙인 슬롯(사람 번호)별로 따로 잇고,
   누가 피험자인지는 Pose2Sim personAssociation 이 여러 카메라를 보고 정합니다.
   ★ 처음에는 프레임마다 "관절이 가장 많은 한 명"을 골랐습니다. 공식 데모에 사람이
     두 명 찍혀 있어서 cam02 에서 엉뚱한 사람을 골랐고, Pose2Sim 이 cam02 를
     100% 버렸습니다. 게다가 프레임마다 고른 사람이 바뀌면 두 사람의 궤적을
     섞어서 잇게 됩니다.
 - **외삽하지 않습니다.** 관측 구간 밖의 값은 만들지 않습니다.
 - **긴 공백을 잇지 않습니다.** max_gap 보다 긴 공백은 비워 둡니다 (신뢰도 0).
   사람이 가려진 1초를 곡선으로 지어내면 그럴듯한 가짜가 됩니다.
 - **비현실적으로 튀는 곳에서 끊습니다.** 추적기가 슬롯을 다른 사람에게 넘기면
   좌표가 한 프레임에 수백 px 뜁니다. 그걸 곡선으로 이으면 두 사람 사이를
   날아다니는 가짜 관절이 됩니다.
 - **신뢰도가 낮은 점은 곡선에 넣지 않습니다.** Pose2Sim 삼각측량도 버리는 점
   (기본 0.3 미만)이 곡선을 휘게 하면 멀쩡한 이웃 프레임까지 오염됩니다.
 - **프레임 수가 맞지 않으면 멈춥니다.** 영상 프레임과 사이드카 시각의 1:1 대응이
   전제입니다 (그래서 녹화 때 B프레임을 껐습니다). 어긋나면 조용히 틀린 결과가
   나오므로 경고가 아니라 오류로 처리합니다.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
from scipy.interpolate import Akima1DInterpolator, CubicSpline, PchipInterpolator

from .sidecar import Sidecar

NS_PER_S = 1_000_000_000

#: 이보다 긴 공백은 잇지 않습니다. 50 ms = 60fps 에서 프레임 2개가 빠진 정도.
DEFAULT_MAX_GAP_NS = 50_000_000

#: 인접한 두 관측 사이 이동이 이보다 크면 다른 사람으로 보고 끊습니다 (px).
#
#  근거: 1080p 에서 사람 키가 약 900 px(1.7 m) 이면 1 px ≈ 1.9 mm.
#  손목 5 m/s 는 약 2600 px/s = 60fps 한 프레임에 44 px, 공백 50 ms 를 넘겨도
#  130 px 정도입니다. 200 px 은 실제 움직임으로는 나오지 않는 크기입니다.
DEFAULT_MAX_JUMP_PX = 200.0

#: 곡선에 넣을 최소 신뢰도. Pose2Sim 의 likelihood_threshold_triangulation 기본값.
DEFAULT_MIN_CONF = 0.3

DEFAULT_FPS = 60.0


class ResampleError(Exception):
    """조용히 틀린 결과를 내느니 멈춰야 하는 상황."""


# ── 읽기 ──────────────────────────────────────────────────────────────────────

_TRAILING_NUM = re.compile(r"(\d+)(?=\.json$)", re.IGNORECASE)


def frame_number(path: Path) -> int:
    """cam01_000123.json -> 123  (Pose2Sim 과 같은 규칙: 마지막 숫자)"""
    m = _TRAILING_NUM.search(path.name)
    if not m:
        raise ResampleError(f"파일 이름에서 프레임 번호를 찾을 수 없습니다: {path.name}")
    return int(m.group(1))


def _as_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return math.nan


def _person_array(p: dict) -> np.ndarray | None:
    kp = p.get("pose_keypoints_2d") or []
    a = np.array([_as_float(v) for v in kp], dtype=float)
    if a.size == 0 or a.size % 3:
        return None
    return a.reshape(-1, 3)


def pick_person(people: list[dict], n_kp: int | None = None) -> np.ndarray | None:
    """
    한 프레임의 people 중 유효 관절이 가장 많은 사람. (n_kp, 3) 또는 None.

    ★ 리샘플러는 이걸 쓰지 않습니다 (슬롯별로 다 잇습니다 — 모듈 설명 참고).
      한 사람만 찍힌 영상의 품질 점검 도구들이 씁니다.
    """
    best, best_n = None, 0
    for p in people or []:
        a = _person_array(p)
        if a is None:
            continue
        n = int(np.sum(np.isfinite(a[:, 2]) & (a[:, 2] > 0)))
        if n > best_n:
            best, best_n = a, n
    if best is None:
        return None
    if n_kp is not None and best.shape[0] != n_kp:
        out = np.full((n_kp, 3), np.nan)
        m = min(n_kp, best.shape[0])
        out[:m] = best[:m]
        return out
    return best


def load_pose_dir(json_dir: Path) -> tuple[np.ndarray, np.ndarray]:
    """
    Pose2Sim 의 pose/camNN_json 폴더를 읽습니다.

    반환: (프레임번호 (N,), 관절 (N, S, K, 3))
      S = 사람 슬롯 수. 슬롯 번호 = 추적기가 붙인 사람 번호입니다.
      Pose2Sim(sports2d 추적) 은 같은 사람을 같은 슬롯에 두고 빈 슬롯은 NaN 으로
      채웁니다. 실측(아이폰 1086프레임)에서 피험자는 741프레임 동안 2번 슬롯이었습니다.
    """
    json_dir = Path(json_dir)
    files = sorted(json_dir.glob("*.json"), key=frame_number)
    if not files:
        raise ResampleError(f"JSON 이 없습니다: {json_dir}")

    per_frame: list[list[np.ndarray | None]] = []
    n_slots, n_kp = 0, 0
    for f in files:
        obj = json.loads(f.read_text(encoding="utf-8"))
        slots = [_person_array(p) for p in (obj.get("people") or [])]
        per_frame.append(slots)
        n_slots = max(n_slots, len(slots))
        for a in slots:
            if a is not None:
                n_kp = max(n_kp, a.shape[0])
    if n_kp == 0:
        raise ResampleError(f"사람이 한 프레임도 검출되지 않았습니다: {json_dir}")

    kp = np.full((len(files), n_slots, n_kp, 3), np.nan)
    for i, slots in enumerate(per_frame):
        for s, a in enumerate(slots):
            if a is not None:
                m = min(n_kp, a.shape[0])
                kp[i, s, :m] = a[:m]
    nums = np.array([frame_number(f) for f in files], dtype=np.int64)
    return nums, kp


# ── 시각 붙이기 ───────────────────────────────────────────────────────────────

@dataclass
class CameraTrack:
    """카메라 한 대의 관절 시계열 (공통 시간축)."""

    name: str                 # 출력 폴더 이름. 예: "cam01_json"
    t_ns: np.ndarray          # (N,) int64  공통(마스터) 시각
    kp: np.ndarray            # (N, S, K, 3) x, y, 신뢰도. 없으면 NaN
    device_id: str = ""
    uncertainty_ns: int = 0
    warnings: list[str] = field(default_factory=list)


def attach_timestamps(name: str, frame_nums: np.ndarray, kp: np.ndarray,
                      sc: Sidecar) -> CameraTrack:
    """
    JSON 프레임 번호에 사이드카의 시각을 붙입니다.

    ★ 전제: 영상의 N 번째 프레임 = 사이드카의 N 번째 프레임.
      녹화에서 B프레임을 꺼서 보장했고, 실기기 영상으로 디코딩 프레임 수와
      사이드카 프레임 수가 같은 것을 확인했습니다 (696 = 696).
      그래도 어긋나면 조용히 틀리므로 여기서 다시 확인합니다.
    """
    side_idx = np.array([f[0] for f in sc.frames], dtype=np.int64)
    side_ts = np.array([f[1] for f in sc.frames], dtype=np.int64)
    if side_idx.size == 0:
        raise ResampleError(f"{name}: 사이드카에 프레임이 없습니다")
    n_side = int(side_idx.size)

    warnings: list[str] = []
    if int(frame_nums.max()) >= n_side:
        raise ResampleError(
            f"{name}: 영상 프레임({int(frame_nums.max()) + 1}개 이상)이 사이드카 "
            f"타임스탬프({n_side}개)보다 많습니다. 영상과 사이드카가 다른 촬영이거나 "
            "디코딩 프레임 수가 다릅니다. 이 상태로 이으면 모든 시각이 밀립니다.")
    if frame_nums.size != n_side:
        warnings.append(
            f"JSON {frame_nums.size}개 / 사이드카 {n_side}개. 영상 일부만 추정한 것으로 보고 "
            "프레임 번호로 맞춥니다.")

    lookup = dict(zip(side_idx.tolist(), side_ts.tolist()))
    try:
        t_slave = np.array([lookup[int(n)] for n in frame_nums], dtype=np.int64)
    except KeyError as e:
        raise ResampleError(f"{name}: 사이드카에 없는 프레임 번호 {e}") from None

    if np.any(np.diff(t_slave) <= 0):
        raise ResampleError(f"{name}: 프레임 시각이 증가하지 않습니다. 보간할 수 없습니다.")

    t_master = t_slave + np.int64(sc.clock_offset_ns)
    return CameraTrack(name=name, t_ns=t_master, kp=kp, device_id=sc.device_id,
                       uncertainty_ns=sc.clock_uncertainty_ns, warnings=warnings)


# ── 격자 ──────────────────────────────────────────────────────────────────────

def common_grid(tracks: Sequence[CameraTrack], fps: float = DEFAULT_FPS,
                start_ns: int | None = None) -> np.ndarray:
    """
    모든 카메라가 **동시에** 찍고 있던 구간 위의 등간격 시각들.

    구간 밖에서는 적어도 한 카메라가 외삽해야 하므로 격자를 만들지 않습니다.
    """
    lo = max(int(t.t_ns[0]) for t in tracks)
    hi = min(int(t.t_ns[-1]) for t in tracks)
    if start_ns is not None:
        lo = max(lo, int(start_ns))
    if hi <= lo:
        raise ResampleError(
            "카메라들이 동시에 찍은 구간이 없습니다. 클럭 오프셋이 틀렸거나 "
            "서로 다른 촬영의 파일입니다.")
    step = NS_PER_S / fps
    n = int(math.floor((hi - lo) / step)) + 1
    return lo + np.round(np.arange(n) * step).astype(np.int64)


# ── 보간 ──────────────────────────────────────────────────────────────────────

#: 곡선 보간 방법들. "linear" 과 "nearest" 는 resample_track 안에서 따로 처리합니다.
_INTERPOLATORS = {
    "cubic": lambda t, v: CubicSpline(t, v),
    "pchip": lambda t, v: PchipInterpolator(t, v),
    "makima": lambda t, v: Akima1DInterpolator(t, v, method="makima"),
}

METHODS = ("nearest", "linear", *_INTERPOLATORS)

#: ★ 기본 보간법 = makima (설계 문서의 "cubic spline" 에서 바꿨습니다).
#
#  근거 — 실제 아이폰 영상 관절로 hold-out 측정 (tools/resample_holdout.py,
#  짝수 프레임만 주고 홀수 프레임을 맞추게 함, 슬롯별 리샘플, 2026-09-27):
#
#               평균 오차   손목(L/R)     발목(L/R)
#    nearest     9.30 px   14.38/14.63   6.97/6.88
#    linear      4.71      3.43/3.74     3.35/3.10
#    cubic       4.97      3.07/3.41     3.56/3.32
#    pchip       4.76      3.05/3.37     3.40/3.10
#    makima      4.72      3.01/3.31     3.39/3.10
#
#  cubic spline 은 검출이 튀는 곳에서 곡선이 출렁여(overshoot) 곡선 중 가장 나빴습니다.
#  makima 는 튀는 점 주변에서 곡선을 누그러뜨려, 전체는 linear 와 같고(0.01 px 차)
#  빠르게 움직이는 관절(손목·팔꿈치)에서는 가장 좋습니다.
#  합성 데이터(매끈한 움직임)에서는 곡선 계열이 linear 보다 훨씬 정확합니다.
#  두 조건 모두에서 좋은 것이 makima 입니다.
DEFAULT_METHOD = "makima"


def _segments(t: np.ndarray, xy: np.ndarray, max_gap_ns: int,
              max_jump_px: float) -> list[slice]:
    """시각 공백이 max_gap 을 넘거나, 좌표가 max_jump 넘게 튀는 곳에서 자릅니다."""
    if t.size == 0:
        return []
    cut = np.diff(t) > max_gap_ns
    if xy.shape[0] >= 2:
        cut |= np.hypot(*np.diff(xy, axis=0).T) > max_jump_px
    idx = np.nonzero(cut)[0] + 1
    bounds = [0, *idx.tolist(), t.size]
    return [slice(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


@dataclass
class TrackStats:
    name: str
    grid_points: int
    #: 격자 시각과 가장 가까운 원래 프레임의 시간차 (ms). 보간이 얼마나 "움직였는지".
    shift_ms_mean: float
    shift_ms_max: float
    #: 사람 슬롯 수
    slots: int
    #: 사람이 한 명이라도 있는 격자 비율
    person_ratio: float
    #: 가장 오래 있던 사람(주 피험자로 추정)의 관절 채움 비율
    main_filled_ratio: float
    #: 이어 붙인 가장 긴 공백 (ms). max_gap 이하여야 합니다.
    max_bridged_gap_ms: float
    #: 튀어서 끊은 횟수 (추적기가 슬롯을 다른 사람에게 넘긴 흔적)
    jump_cuts: int
    warnings: list[str]

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _resample_series(t_ns: np.ndarray, kp: np.ndarray, grid: np.ndarray,
                     base: int, tg: np.ndarray, out: np.ndarray,
                     max_gap_ns: int, max_jump_px: float, min_conf: float,
                     method: str) -> tuple[int, int]:
    """한 사람(슬롯)의 (N, K, 3) 을 격자 위 out (G, K, 3) 에 채웁니다."""
    bridged, jumps = 0, 0
    for k in range(kp.shape[1]):
        x, y, c = kp[:, k, 0], kp[:, k, 1], kp[:, k, 2]
        ok = np.isfinite(x) & np.isfinite(y) & np.isfinite(c) & (c >= min_conf)
        if not np.any(ok):
            continue
        t_ok = t_ns[ok]
        x_ok, y_ok, c_ok = x[ok], y[ok], c[ok]
        segs = _segments(t_ok, np.column_stack([x_ok, y_ok]), max_gap_ns, max_jump_px)
        if len(segs) > 1:
            gaps = np.diff(t_ok) > max_gap_ns
            jumps += int(len(segs) - 1 - np.count_nonzero(gaps))
        for seg in segs:
            ts = t_ok[seg]
            if ts.size >= 2:
                bridged = max(bridged, int(np.max(np.diff(ts))))
            # 이 구간 안에 들어오는 격자만. 밖은 외삽이므로 만들지 않습니다.
            inside = (grid >= ts[0]) & (grid <= ts[-1])
            if not np.any(inside):
                continue
            tq = tg[inside]
            tsr = (ts - base).astype(np.float64) / NS_PER_S
            if ts.size == 1 or method == "nearest":
                j = np.clip(np.searchsorted(tsr, tq), 0, ts.size - 1)
                jm = np.clip(j - 1, 0, ts.size - 1)
                pick = np.where(np.abs(tsr[jm] - tq) <= np.abs(tsr[j] - tq), jm, j)
                out[inside, k, 0] = x_ok[seg][pick]
                out[inside, k, 1] = y_ok[seg][pick]
                out[inside, k, 2] = c_ok[seg][pick]
                continue
            if method == "linear" or ts.size < 4:
                out[inside, k, 0] = np.interp(tq, tsr, x_ok[seg])
                out[inside, k, 1] = np.interp(tq, tsr, y_ok[seg])
            else:
                interp = _INTERPOLATORS[method]
                out[inside, k, 0] = interp(tsr, x_ok[seg])(tq)
                out[inside, k, 1] = interp(tsr, y_ok[seg])(tq)
            # 신뢰도는 곡선으로 올리면 1을 넘거나 음수가 될 수 있어 직선으로 잇습니다.
            out[inside, k, 2] = np.interp(tq, tsr, c_ok[seg])
    return bridged, jumps


def resample_track(tr: CameraTrack, grid: np.ndarray,
                   max_gap_ns: int = DEFAULT_MAX_GAP_NS,
                   min_conf: float = DEFAULT_MIN_CONF,
                   method: str = DEFAULT_METHOD,
                   max_jump_px: float = DEFAULT_MAX_JUMP_PX) -> tuple[np.ndarray, TrackStats]:
    """
    카메라 한 대를 격자 위에서 다시 읽습니다. 반환 (G, S, K, 3), 없으면 NaN.

    method: METHODS 중 하나. 기본 makima (DEFAULT_METHOD 설명 참고).
            "nearest" 는 비교용 — 보간 없이 가장 가까운 프레임을 씁니다.
    """
    if method not in METHODS:
        raise ValueError(f"모르는 보간법: {method!r} (가능: {', '.join(METHODS)})")
    G = grid.size
    _, S, K, _ = tr.kp.shape
    out = np.full((G, S, K, 3), np.nan)
    # 부동소수 정밀도를 위해 격자 첫 시각을 기준으로 초 단위 상대시각을 씁니다.
    base = int(grid[0])
    tg = (grid - base).astype(np.float64) / NS_PER_S

    bridged, jumps = 0, 0
    for s in range(S):
        b, j = _resample_series(tr.t_ns, tr.kp[:, s], grid, base, tg, out[:, s],
                                max_gap_ns, max_jump_px, min_conf, method)
        bridged, jumps = max(bridged, b), jumps + j

    j = np.clip(np.searchsorted(tr.t_ns, grid), 1, tr.t_ns.size - 1)
    near = np.minimum(np.abs(tr.t_ns[j] - grid), np.abs(tr.t_ns[j - 1] - grid))
    filled = np.isfinite(out[..., 2])                      # (G, S, K)
    main = main_slot(out)
    stats = TrackStats(
        name=tr.name,
        grid_points=int(G),
        shift_ms_mean=float(np.mean(near) / 1e6),
        shift_ms_max=float(np.max(near) / 1e6),
        slots=int(S),
        person_ratio=float(np.mean(np.any(filled, axis=(1, 2)))),
        main_filled_ratio=float(np.mean(filled[:, main])) if S else 0.0,
        max_bridged_gap_ms=bridged / 1e6,
        jump_cuts=int(jumps),
        warnings=list(tr.warnings),
    )
    return out, stats


def main_slot(kp_grid: np.ndarray) -> int:
    """관절이 가장 많이 채워진 슬롯 (주 피험자로 추정). 통계·점검용."""
    filled = np.isfinite(kp_grid[..., 2])
    return int(np.argmax(filled.sum(axis=(0, 2)))) if kp_grid.shape[1] else 0


# ── 쓰기 ──────────────────────────────────────────────────────────────────────

def _person_entry(kp: np.ndarray) -> dict:
    valid = np.isfinite(kp[:, 2])
    a = np.where(valid[:, None], kp, 0.0)
    return {
        "person_id": [-1],
        "pose_keypoints_2d": [round(float(v), 4) for v in a.reshape(-1)],
        "face_keypoints_2d": [],
        "hand_left_keypoints_2d": [],
        "hand_right_keypoints_2d": [],
        "pose_keypoints_3d": [],
        "face_keypoints_3d": [],
        "hand_left_keypoints_3d": [],
        "hand_right_keypoints_3d": [],
    }


def openpose_frame(kp: np.ndarray) -> dict:
    """
    OpenPose JSON 한 프레임. kp 는 (S, K, 3) 또는 한 사람 (K, 3).

    없는 관절은 (0, 0, 0) 으로 씁니다 — OpenPose 관례이고, 신뢰도 0 이라
    Pose2Sim 이 임계값에서 버립니다. JSON 에 NaN 을 쓰지 않습니다
    (NaN 은 표준 JSON 이 아니어서 다른 도구가 못 읽을 수 있습니다).
    관절이 하나도 없는 사람 슬롯은 빼고 씁니다. 슬롯 번호는 다음 단계에서
    쓰이지 않습니다 (Pose2Sim personAssociation 이 카메라들을 보고 다시 정합니다).
    """
    if kp.ndim == 2:
        kp = kp[None]
    people = [_person_entry(p) for p in kp if np.any(np.isfinite(p[:, 2]))]
    return {"version": 1.3, "people": people}


def write_track(out_root: Path, name: str, kp_grid: np.ndarray) -> Path:
    """
    pose-sync/<name>/<cam>_000000.json ... 로 씁니다.

    파일 번호 = 격자 번호입니다. 모든 카메라가 같은 격자를 쓰므로, Pose2Sim 이
    파일을 번호 순으로 짝지으면 정확히 같은 순간끼리 짝지어집니다.
    """
    d = Path(out_root) / name
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("*.json"):
        old.unlink()
    stem = name[:-5] if name.endswith("_json") else name
    for i in range(kp_grid.shape[0]):
        (d / f"{stem}_{i:06d}.json").write_text(
            json.dumps(openpose_frame(kp_grid[i]), separators=(",", ":")),
            encoding="utf-8")
    return d


# ── 한 세션 전체 ──────────────────────────────────────────────────────────────

@dataclass
class SessionResult:
    grid_ns: np.ndarray
    fps: float
    stats: list[TrackStats]
    out_root: Path | None

    def report(self) -> dict:
        return {
            "fps": self.fps,
            "gridPoints": int(self.grid_ns.size),
            "gridStartNs": int(self.grid_ns[0]),
            "gridEndNs": int(self.grid_ns[-1]),
            "durationS": float((self.grid_ns[-1] - self.grid_ns[0]) / NS_PER_S),
            "cameras": [s.to_dict() for s in self.stats],
        }


def resample_session(pairs: Iterable[tuple[Path, Path]],
                     out_root: Path | None,
                     fps: float = DEFAULT_FPS,
                     max_gap_ns: int = DEFAULT_MAX_GAP_NS,
                     min_conf: float = DEFAULT_MIN_CONF,
                     method: str = DEFAULT_METHOD,
                     max_jump_px: float = DEFAULT_MAX_JUMP_PX) -> SessionResult:
    """
    pairs: [(pose/camNN_json 폴더, 그 카메라의 사이드카 .json), ...]
    out_root: 보통 <Pose2Sim 프로젝트>/pose-sync. None 이면 쓰지 않습니다.
    """
    tracks = []
    for json_dir, side_path in pairs:
        json_dir = Path(json_dir)
        sc = Sidecar.load(side_path)
        nums, kp = load_pose_dir(json_dir)
        tracks.append(attach_timestamps(json_dir.name, nums, kp, sc))

    ids = [t.device_id for t in tracks if t.device_id]
    if len(set(ids)) != len(ids):
        raise ResampleError(f"같은 기기의 사이드카가 두 번 들어왔습니다: {ids}")

    grid = common_grid(tracks, fps)
    stats = []
    for tr in tracks:
        g, st = resample_track(tr, grid, max_gap_ns, min_conf, method, max_jump_px)
        stats.append(st)
        if out_root is not None:
            write_track(Path(out_root), tr.name, g)

    res = SessionResult(grid_ns=grid, fps=fps, stats=stats,
                        out_root=Path(out_root) if out_root is not None else None)
    if out_root is not None:
        (Path(out_root) / "resample_report.json").write_text(
            json.dumps(res.report(), ensure_ascii=False, indent=2), encoding="utf-8")
    return res
