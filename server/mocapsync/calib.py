"""
캘리브레이션 — 폰마다 렌즈 특성(내부), 배치마다 위치·방향(외부), 그리고 Pose2Sim 리그.

왜 Pose2Sim calibration() 을 쓰지 않나
-------------------------------------
Pose2Sim 의 외부 캘리브레이션은 'board'(바닥에 놓은 판을 모든 카메라가 봄)와
'scene'(줄자로 잰 점을 화면에서 손으로 클릭) 두 가지입니다. 'keypoints' 는 설정
목록에만 있고 0.10.49 코드에서 NotImplementedError 입니다 (calibration.py:963).
A4 판(한 칸 23 mm)은 초광각(107.8도)에서 3 m 떨어지면 한 칸이 약 5 px 라 바닥 판
방식이 불안하고, 클릭 방식은 사람이 틀립니다.

우리에겐 Pose2Sim 에 없는 것이 있습니다 — **프레임마다 공통 시계 시각**(사이드카).
그래서 판을 들고 돌아다니는 영상을 폰 여러 대가 동시에 찍으면, 같은 순간 두
카메라가 본 판을 짝지어 스테레오로 풀 수 있습니다. 판을 카메라 가까이 들어 크게
보여도 됩니다. 짝짓는 시각은 리샘플러와 같은 생각입니다 — 앞뒤 프레임 사이를
시각대로 보간해서 **정확히 같은 순간**의 꼭짓점을 만듭니다.

 1. 내부 (폰마다 한 번)   판을 손에 들고 여러 각도·화면 구석까지
                           → cv2.calibrateCamera
                           결과: <작업폴더>/intrinsics/<기기ID>.json
 2. 외부 (배치마다)       폰 전부 동시 녹화, 판을 두 카메라가 함께 보게 돌아다님
                           → 판 자세 + 카메라 자세 번들 조정 (재투영 오차 최소화)
                           마지막에 판을 바닥에 5초 → 위쪽 방향과 바닥 높이
                           결과: <작업폴더>/rig/Calib.toml + cameras.json

★ cameras.json 을 사람이 쓰지 않습니다
  폰 2대에서는 카메라가 뒤바뀌어도 재투영 오차·배제율에 전혀 안 나타납니다
  (DESIGN §4.3 실측: 뒤바꾼 3D 가 중앙값 54 cm 틀렸는데 지표는 정상). 이 모듈은
  영상 파일마다 사이드카의 deviceId 로 카메라를 알아보므로 뒤바뀔 틈이 없습니다.

★ 판의 방향을 모든 카메라에서 같게 맞춥니다
  findChessboardCorners 는 꼭짓점을 어느 모서리부터 세는지 보장하지 않습니다.
  카메라 A 는 왼쪽 위부터, B 는 오른쪽 아래부터 세면 스테레오가 조용히 틀립니다.
  그래서 (1) 격자의 좌우 손잡이 방향과 (2) 칸 색으로 순서를 하나로 정합니다.
  이 판(7x10 칸)은 180도 돌리면 칸 색이 뒤집혀서 색으로 구분됩니다
  (tools/make_checkerboard.py: 왼쪽 위 칸이 검정).

좌표 약속
---------
 판 좌표: X = 종이 가로(오른쪽), Y = 종이 세로(아래), Z = X×Y = 종이 **뒤쪽**.
          안쪽 꼭짓점 (i, j) = (i·s, j·s, 0). 판을 바닥에 눕히면 Z 가 바닥 속을 가리키므로
          위쪽 = -Z.
 카메라:  OpenCV (x 오른쪽, y 아래, z 앞). 자세 (R, t): X_cam = R·X + t.
 월드:    Z 위, 바닥 = 0, 원점 = 바닥 판 가운데, Y = cam01 이 보는 방향(수평).
          Pose2Sim 이 TRC 를 쓸 때 Z-up → Y-up 으로 돌립니다 (common.zup2yup).
 Calib.toml: rotation = Rodrigues(월드→카메라), translation = 미터.
             Pose2Sim common.computeP 와 같은 규칙입니다.
"""
from __future__ import annotations

import json
import math
import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix
from scipy.spatial.transform import Rotation

NS_PER_S = 1_000_000_000


class CalibError(Exception):
    """사람이 다시 찍거나 입력을 고쳐야 하는 문제."""


# ── 판 ────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Board:
    """
    체커보드. cols = 한 줄의 안쪽 꼭짓점 수(종이 가로), rows = 줄 수(종이 세로).
    기본값은 tools/make_checkerboard.py 의 A4 판 (7x10 칸, 23 mm → 안쪽 6x9).
    """

    cols: int = 6
    rows: int = 9
    square_m: float = 0.023

    @property
    def pattern(self) -> tuple[int, int]:
        """OpenCV patternSize = (한 줄의 꼭짓점 수, 줄 수)."""
        return (self.cols, self.rows)

    @property
    def n(self) -> int:
        return self.cols * self.rows

    def object_points(self) -> np.ndarray:
        """(n, 3) 판 좌표 (미터). 줄 단위로 왼쪽→오른쪽, OpenCV 출력 순서와 같음."""
        j, i = np.mgrid[0:self.rows, 0:self.cols]
        s = self.square_m
        return np.stack([i.ravel() * s, j.ravel() * s, np.zeros(self.n)], axis=1)

    @property
    def center(self) -> np.ndarray:
        s = self.square_m
        return np.array([(self.cols - 1) * s / 2, (self.rows - 1) * s / 2, 0.0])

    def to_dict(self) -> dict:
        return {"innerCols": self.cols, "innerRows": self.rows,
                "squareMm": round(self.square_m * 1000, 4)}


DEFAULT_BOARD = Board()


# ── 검출 ──────────────────────────────────────────────────────────────────────

#: 한 칸이 이보다 작게 찍히면 꼭짓점 위치를 믿기 어렵습니다 (px).
MIN_SQUARE_PX = 4.0

_FIND_FLAGS = (cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
               | cv2.CALIB_CB_FAST_CHECK)
_SUBPIX = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 40, 0.001)


def _square_px(g: np.ndarray) -> float:
    """격자 (rows, cols, 2) 에서 한 칸의 대략적인 크기 (짧은 쪽 10% 분위)."""
    a = np.linalg.norm(np.diff(g, axis=1), axis=2).ravel()
    b = np.linalg.norm(np.diff(g, axis=0), axis=2).ravel()
    return float(np.percentile(np.concatenate([a, b]), 10))


def _patch_mean(gray: np.ndarray, center: np.ndarray, size: int) -> float:
    p = cv2.getRectSubPix(gray, (size, size), (float(center[0]), float(center[1])))
    return float(p.mean())


def canonical_order(corners: np.ndarray, gray: np.ndarray, board: Board) -> np.ndarray:
    """
    검출된 꼭짓점을 판에 대해 **하나뿐인 순서**로 정렬합니다.

     1. 손잡이: 판을 앞에서 보면 (첫 줄 방향) × (첫 열 방향) 이 화면에서 양수입니다
        (x 오른쪽, y 아래 좌표). 음수면 줄 안의 순서를 뒤집습니다.
     2. 색: 꼭짓점 (0,0)-(1,0)-(0,1)-(1,1) 사이 칸이 검정이어야 합니다.
        한 칸만 보면 조명에 속을 수 있어 안쪽 칸 전부의 색 차이로 판정합니다.
        틀리면 180도 돌립니다 (배열 전체를 뒤집음 — 손잡이는 그대로).
    """
    g = corners.reshape(board.rows, board.cols, 2).copy()
    a = g[0, 1] - g[0, 0]
    b = g[1, 0] - g[0, 0]
    if a[0] * b[1] - a[1] * b[0] < 0:
        g = g[:, ::-1]

    size = max(3, int(round(_square_px(g) * 0.3)))
    dark, light = [], []
    for j in range(board.rows - 1):
        for i in range(board.cols - 1):
            c = (g[j, i] + g[j, i + 1] + g[j + 1, i] + g[j + 1, i + 1]) / 4
            (dark if (i + j) % 2 == 0 else light).append(_patch_mean(gray, c, size))
    if np.mean(dark) > np.mean(light):
        g = g[::-1, ::-1]
    return g.reshape(-1, 2)


def detect_board(gray: np.ndarray, board: Board = DEFAULT_BOARD) -> np.ndarray | None:
    """
    흑백 영상 한 장에서 판의 안쪽 꼭짓점 (n, 2) 를 찾습니다. 없으면 None.

    원래 크기에서 바로 찾습니다. 절반 크기에서 먼저 찾는 흔한 요령은 실측(1280x720,
    한 칸 10~20 px)에서 오히려 느렸습니다 — 작은 영상에서 판을 자주 놓치고, 놓칠 때
    탐색이 길어서 (절반 60 ms 실패 + 원래 10 ms 성공 vs 원래 크기만 10 ms).
    """
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    found, c = cv2.findChessboardCorners(gray, board.pattern, flags=_FIND_FLAGS)
    if not found or c is None:
        return None
    c = c.reshape(-1, 2).astype(np.float32)
    sq = _square_px(c.reshape(board.rows, board.cols, 2))
    if sq < MIN_SQUARE_PX:
        return None
    win = int(np.clip(round(sq * 0.3), 2, 11))
    c = cv2.cornerSubPix(gray, c.reshape(-1, 1, 2), (win, win), (-1, -1), _SUBPIX)
    return canonical_order(c.reshape(-1, 2).astype(np.float64), gray, board)


def video_info(video: Path) -> tuple[int, int, int, float]:
    """(폭, 높이, 프레임 수, fps) — 컨테이너에 적힌 값."""
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise CalibError(f"영상을 열 수 없습니다: {video}")
    try:
        return (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), float(cap.get(cv2.CAP_PROP_FPS)))
    finally:
        cap.release()


def read_frame(video: Path, index: int) -> np.ndarray | None:
    """프레임 한 장 (BGR). 확인 그림용이라 앞에서부터 읽어도 됩니다."""
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return None
    try:
        for _ in range(max(0, index)):
            if not cap.grab():
                return None
        ok, fr = cap.read()
        return fr if ok else None
    finally:
        cap.release()


def detect_in_video(video: Path, indices: Iterable[int], board: Board = DEFAULT_BOARD,
                    workers: int = 0,
                    progress: Callable[[int, int], None] | None = None,
                    ) -> tuple[dict[int, np.ndarray], tuple[int, int]]:
    """
    영상의 지정한 프레임들에서 판을 찾습니다. 반환: ({프레임번호: 꼭짓점}, (폭, 높이)).

    영상은 앞에서부터 한 번만 읽고(건너뛸 프레임은 grab 만), 검출은 여러 스레드로
    돌립니다 (OpenCV 가 GIL 을 풀어서 실제로 병렬이 됩니다).
    """
    want = sorted({int(i) for i in indices if int(i) >= 0})
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise CalibError(f"영상을 열 수 없습니다: {video}")
    size = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    out: dict[int, np.ndarray] = {}
    if not want:
        cap.release()
        return out, size
    wset, last = set(want), want[-1]
    workers = workers or max(1, min(8, (os.cpu_count() or 2) - 1))
    done = 0

    def collect(futs) -> None:
        nonlocal done
        for f in futs:
            idx = pending.pop(f)
            c = f.result()
            if c is not None:
                out[idx] = c
            done += 1
            if progress and (done % 50 == 0 or done == len(want)):
                progress(done, len(want))

    pending: dict = {}
    try:
        with ThreadPoolExecutor(workers) as ex:
            i = 0
            while i <= last:
                if i in wset:
                    ok, fr = cap.read()
                    if not ok:
                        break
                    size = (fr.shape[1], fr.shape[0])
                    gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
                    pending[ex.submit(detect_board, gray, board)] = i
                    if len(pending) >= workers * 3:
                        fin, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                        collect(fin)
                elif not cap.grab():
                    break
                i += 1
            fin, _ = wait(list(pending))
            collect(fin)
    finally:
        cap.release()
    return out, size


# ── 관측 ──────────────────────────────────────────────────────────────────────

@dataclass
class Obs:
    """한 순간 카메라 한 대가 본 판."""

    key: int                  # 내부: 프레임 번호 / 외부: 공통 격자 번호
    corners: np.ndarray       # (n, 2) px
    speed: float              # 꼭짓점 최대 이동 속도 (px/초). 흔들림·롤링셔터 판정용


def paired_observations(det: dict[int, np.ndarray], frames: Iterable[int],
                        fps: float) -> list[Obs]:
    """
    프레임 k 와 k+1 이 둘 다 검출된 곳만 씁니다 (k+1 은 속도를 재는 용도).

    ★ 움직이는 판은 두 가지로 망가집니다. 모션블러(셔터 1/500초라 작음)와
      **롤링셔터** — 화면 위아래 줄을 다른 순간에 읽어서 움직이는 판이 기울어져 찍힙니다.
      속도를 재서 빠른 장면을 버립니다.
    """
    out = []
    for k in frames:
        a, b = det.get(k), det.get(k + 1)
        if a is None or b is None:
            continue
        v = float(np.max(np.linalg.norm(b - a, axis=1))) * fps
        out.append(Obs(k, a, v))
    return out


def sample_grid(t_ns_list: Sequence[np.ndarray], rate_hz: float) -> np.ndarray:
    """모든 카메라가 동시에 찍고 있던 구간 위의 등간격 시각 (공통 시계, ns)."""
    lo = max(int(t[0]) for t in t_ns_list)
    hi = min(int(t[-1]) for t in t_ns_list)
    if hi <= lo:
        raise CalibError("카메라들이 동시에 찍은 구간이 없습니다. 같은 촬영의 파일인지, "
                         "시계 동기가 됐는지 확인하세요.")
    step = NS_PER_S / rate_hz
    n = int(math.floor((hi - lo) / step)) + 1
    return lo + np.round(np.arange(n) * step).astype(np.int64)


def bracket_frames(t_ns: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """격자 시각마다 그 앞 프레임 번호 k (t[k] <= T < t[k+1]). 범위 밖은 -1."""
    k = np.searchsorted(t_ns, grid, side="right") - 1
    k[(k < 0) | (k >= len(t_ns) - 1)] = -1
    return k


def frames_for_grid(t_ns: np.ndarray, grid: np.ndarray,
                    frame_idx: np.ndarray | None = None) -> set[int]:
    """격자 시각마다 앞뒤 두 프레임의 영상 프레임 번호."""
    fi = np.arange(len(t_ns)) if frame_idx is None else np.asarray(frame_idx)
    k = bracket_frames(t_ns, grid)
    k = k[k >= 0]
    return set(fi[k].tolist()) | set(fi[k + 1].tolist())


def timed_observations(det: dict[int, np.ndarray], t_ns: np.ndarray, grid: np.ndarray,
                       frame_idx: np.ndarray | None = None,
                       max_gap_ratio: float = 1.6) -> dict[int, Obs]:
    """
    격자 시각 T 마다 앞뒤 프레임의 꼭짓점을 시각대로 선형 보간합니다.

    두 폰은 같은 60fps 라도 찍는 순간이 최대 반 프레임(8.3 ms) 다릅니다. 가장 가까운
    프레임끼리 짝지으면 판이 0.3 m/s 로 움직일 때 2.5 mm 어긋난 채로 스테레오를 풉니다.
    프레임이 빠진 곳(간격이 평소의 1.6배 넘음)은 잇지 않습니다.

    t_ns[p] 는 사이드카 p 번째 항목의 공통 시각, frame_idx[p] 는 그 항목의 영상 프레임 번호.
    """
    out: dict[int, Obs] = {}
    if len(t_ns) < 2:
        return out
    fi = np.arange(len(t_ns)) if frame_idx is None else np.asarray(frame_idx)
    dt_med = float(np.median(np.diff(t_ns)))
    for m, k in enumerate(bracket_frames(t_ns, grid)):
        if k < 0:
            continue
        t0, t1 = int(t_ns[k]), int(t_ns[k + 1])
        if t1 - t0 > max_gap_ratio * dt_med:
            continue
        a, b = det.get(int(fi[k])), det.get(int(fi[k + 1]))
        if a is None or b is None:
            continue
        al = (int(grid[m]) - t0) / (t1 - t0)
        v = float(np.max(np.linalg.norm(b - a, axis=1))) * NS_PER_S / (t1 - t0)
        out[m] = Obs(m, (1 - al) * a + al * b, v)
    return out


# ── 카메라 모델 ───────────────────────────────────────────────────────────────

@dataclass
class CamModel:
    K: np.ndarray             # 3x3
    dist: np.ndarray          # OpenCV 순서 (k1, k2, p1, p2[, k3[, k4, k5, k6]])
    size: tuple[int, int]     # (폭, 높이)


def project(Xc: np.ndarray, cam: CamModel) -> np.ndarray:
    """카메라 좌표 (N, 3) → 픽셀 (N, 2). cv2.projectPoints 와 같은 식 (벡터화)."""
    d = np.zeros(8)
    dd = np.asarray(cam.dist, dtype=float).ravel()
    if dd.size > 8:
        raise CalibError("왜곡 계수는 8개까지만 지원합니다")
    d[:dd.size] = dd
    k1, k2, p1, p2, k3, k4, k5, k6 = d
    z = Xc[:, 2]
    x, y = Xc[:, 0] / z, Xc[:, 1] / z
    r2 = x * x + y * y
    r4 = r2 * r2
    r6 = r4 * r2
    rad = (1 + k1 * r2 + k2 * r4 + k3 * r6) / (1 + k4 * r2 + k5 * r4 + k6 * r6)
    xd = x * rad + 2 * p1 * x * y + p2 * (r2 + 2 * x * x)
    yd = y * rad + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y
    K = cam.K
    return np.stack([K[0, 0] * xd + K[0, 1] * yd + K[0, 2], K[1, 1] * yd + K[1, 2]], axis=1)


#: 왜곡 펴기(반복 계산)를 충분히 돌립니다. OpenCV 기본은 5회라 왜곡이 큰 초광각
#  가장자리에서 덜 펴집니다 (pose2sim_undistort_error 참고).
_UNDIST_CRIT = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 100, 1e-12)


def undistort_normalized(px: np.ndarray, cam: CamModel) -> np.ndarray:
    """픽셀 (N, 2) → 왜곡을 편 정규 좌표 (N, 2)."""
    return cv2.undistortPoints(np.asarray(px, dtype=np.float64).reshape(-1, 1, 2), cam.K, cam.dist,
                               criteria=_UNDIST_CRIT).reshape(-1, 2)


def pose2sim_undistort_error(cam: CamModel, n: int = 25) -> float:
    """
    Pose2Sim 이 쓰는 방식 그대로(cv2.undistortPoints 기본값, P=optim_K) 화면 전체의
    점을 폈을 때, 정확히 편 값과의 최대 차이 (optim_K 픽셀).

    Pose2Sim(undistort_points=true)은 반복 횟수를 지정하지 않습니다. 왜곡이 크면 가장자리
    관절이 덜 펴진 채 삼각측량됩니다. 이 값이 크면(1 px 이상) 알려야 합니다.
    """
    w, h = cam.size
    xs, ys = np.meshgrid(np.linspace(0, w - 1, n), np.linspace(0, h - 1, n))
    px = np.stack([xs.ravel(), ys.ravel()], axis=1).reshape(-1, 1, 2).astype(np.float64)
    optim = cv2.getOptimalNewCameraMatrix(cam.K, cam.dist, (int(w), int(h)), 1, (int(w), int(h)))[0]
    fast = cv2.undistortPoints(px, cam.K, cam.dist, None, optim).reshape(-1, 2)
    exact = cv2.undistortPoints(px, cam.K, cam.dist, None, optim, criteria=_UNDIST_CRIT).reshape(-1, 2)
    return float(np.max(np.linalg.norm(fast - exact, axis=1)))


def horizontal_fov_deg(cam: CamModel) -> float:
    """왜곡을 편 뒤의 실제 가로 화각 (화면 가운데 줄의 양 끝)."""
    w, h = cam.size
    u = undistort_normalized(np.array([[0.0, (h - 1) / 2], [w - 1.0, (h - 1) / 2]]), cam)
    return math.degrees(math.atan(-u[0, 0]) + math.atan(u[1, 0]))


def board_pose(corners: np.ndarray, cam: CamModel,
               board: Board = DEFAULT_BOARD) -> tuple[np.ndarray, np.ndarray] | None:
    """판 → 카메라 자세 (R, t). 평면 전용 IPPE 후 LM 으로 다듬습니다."""
    obj = board.object_points()
    img = corners.reshape(-1, 1, 2).astype(np.float64)
    ok, rvec, tvec = cv2.solvePnP(obj, img, cam.K, cam.dist, flags=cv2.SOLVEPNP_IPPE)
    if not ok:
        return None
    rvec, tvec = cv2.solvePnPRefineLM(obj, img, cam.K, cam.dist, rvec, tvec)
    t = tvec.ravel()
    if t[2] <= 0:
        return None
    return cv2.Rodrigues(rvec)[0], t


def view_rms(corners: np.ndarray, cam: CamModel, board: Board = DEFAULT_BOARD) -> float:
    """이 모델로 판 자세만 맞췄을 때의 재투영 오차 (px). 모델 검산용."""
    p = board_pose(corners, cam, board)
    if p is None:
        return math.inf
    R, t = p
    uv = project(board.object_points() @ R.T + t, cam)
    return float(np.sqrt(np.mean(np.sum((uv - corners) ** 2, axis=1))))


# ── 내부 캘리브레이션 ─────────────────────────────────────────────────────────

#: 내부 캘리브레이션에 쓸 장면의 최대 속도 (px/초). 60fps 에서 프레임당 1 px.
INTR_MAX_SPEED = 60.0
#: 계산에 넣을 장면 수 상한. 더 넣어도 정확도는 거의 그대로이고 느려집니다.
INTR_MAX_VIEWS = 60
INTR_MIN_VIEWS = 12
#: 판정 기준 (px). 인수인계 목표 0.5 px.
INTR_GOOD_RMS = 0.5
INTR_OK_RMS = 1.0
#: 화면 칸 나누기 (가로 x 세로) — 판이 화면 어디까지 닿았는지.
COVER_GRID = (8, 6)


def view_features(c: np.ndarray, board: Board, size: tuple[int, int]) -> np.ndarray:
    """장면을 고르기 위한 특징: 화면 위치, 크기, 기울기 (원근으로 줄어든 변 길이 비)."""
    g = c.reshape(board.rows, board.cols, 2)
    w, h = size
    quad = np.array([g[0, 0], g[0, -1], g[-1, -1], g[-1, 0]], dtype=np.float32)
    area = abs(cv2.contourArea(quad))
    top = np.linalg.norm(g[0, -1] - g[0, 0])
    bot = np.linalg.norm(g[-1, -1] - g[-1, 0])
    left = np.linalg.norm(g[-1, 0] - g[0, 0])
    right = np.linalg.norm(g[-1, -1] - g[0, -1])
    m = c.mean(axis=0)
    return np.array([m[0] / w, m[1] / h, 2.0 * math.sqrt(area) / w,
                     3.0 * math.log(max(top, 1e-6) / max(bot, 1e-6)),
                     3.0 * math.log(max(left, 1e-6) / max(right, 1e-6))])


def select_views(obs: Sequence[Obs], board: Board, size: tuple[int, int],
                 max_views: int = INTR_MAX_VIEWS) -> list[int]:
    """
    서로 가장 다른 장면들을 고릅니다 (farthest point sampling).
    같은 자세로 오래 들고 있던 장면이 수백 장 있어도 한 장만 뽑힙니다.
    """
    if len(obs) <= max_views:
        return list(range(len(obs)))
    F = np.stack([view_features(o.corners, board, size) for o in obs])
    center = np.array([0.5, 0.5])
    first = int(np.argmin(np.linalg.norm(F[:, :2] - center, axis=1)))
    chosen = [first]
    dmin = np.linalg.norm(F - F[first], axis=1)
    while len(chosen) < max_views:
        k = int(np.argmax(dmin))
        if dmin[k] <= 0:
            break
        chosen.append(k)
        dmin = np.minimum(dmin, np.linalg.norm(F - F[k], axis=1))
    return sorted(chosen)


def coverage(corner_sets: Iterable[np.ndarray], size: tuple[int, int],
             grid: tuple[int, int] = COVER_GRID) -> tuple[float, float, np.ndarray]:
    """(전체 칸 중 판이 닿은 비율, 가장자리 칸 중 닿은 비율, 칸별 여부 (gy, gx))."""
    gx, gy = grid
    w, h = size
    hit = np.zeros((gy, gx), dtype=bool)
    for c in corner_sets:
        ix = np.clip((c[:, 0] / w * gx).astype(int), 0, gx - 1)
        iy = np.clip((c[:, 1] / h * gy).astype(int), 0, gy - 1)
        hit[iy, ix] = True
    edge = np.zeros_like(hit)
    edge[0, :] = edge[-1, :] = edge[:, 0] = edge[:, -1] = True
    return float(hit.mean()), float(hit[edge].mean()), hit


@dataclass
class IntrinsicsResult:
    cam: CamModel
    rms: float                   # 계산에 쓴 장면의 재투영 오차 (px)
    holdout_rms: float           # 계산에 안 쓴 장면으로 검산한 오차 (중앙값, px)
    views: int
    dropped_views: int           # 오차가 커서 뺀 장면
    coverage: float
    edge_coverage: float
    coverage_map: np.ndarray
    std_intrinsics: dict         # fx, fy, cx, cy 의 추정 표준편차 (px)
    used_keys: list[int]
    rational: bool = False

    def verdict(self) -> tuple[str, list[str]]:
        """('good'|'ok'|'bad', 사람 말 목록)."""
        msgs = []
        level = "good"
        if self.rms > INTR_OK_RMS or self.views < INTR_MIN_VIEWS:
            level = "bad"
        elif self.rms > INTR_GOOD_RMS:
            level = "ok"
        if self.holdout_rms > 2 * INTR_OK_RMS:
            level = "bad"
            msgs.append(f"계산에 안 쓴 장면으로 검산한 오차가 {self.holdout_rms:.2f} px 로 큽니다. "
                        "렌즈 모델이 화면 일부에만 맞춰졌을 수 있습니다.")
        if self.coverage < 0.5:
            level = "bad" if level == "bad" else "ok"
            msgs.append(f"판이 화면의 {self.coverage * 100:.0f}% 에만 닿았습니다. 가장자리 "
                        "왜곡을 추정하려면 판을 화면 구석구석까지 가져가야 합니다.")
        elif self.edge_coverage < 0.6:
            if level == "good":
                level = "ok"
            msgs.append(f"화면 가장자리 칸의 {self.edge_coverage * 100:.0f}% 에만 판이 닿았습니다. "
                        "초광각은 가장자리 왜곡이 크니 판을 네 모서리와 변 가까이까지 가져가세요.")
        return level, msgs


def calibrate_intrinsics(obs: Sequence[Obs], size: tuple[int, int],
                         board: Board = DEFAULT_BOARD, max_views: int = INTR_MAX_VIEWS,
                         rational: bool = False) -> IntrinsicsResult:
    """
    장면 고르기 → calibrateCamera → 오차 큰 장면 빼고 한 번 더 → 안 쓴 장면으로 검산.

    왜곡 모델: 기본 k1 k2 p1 p2 k3 (5개). 초광각이라 k3 까지 씁니다. Pose2Sim 은
    distortions 배열을 그대로 cv2.undistortPoints 에 넘기므로 5개도 읽습니다
    (common.retrieve_calib_params). rational=True 면 8개 모델.
    """
    if len(obs) < INTR_MIN_VIEWS:
        raise CalibError(f"판이 또렷하게 잡힌 장면이 {len(obs)}개뿐입니다 "
                         f"(최소 {INTR_MIN_VIEWS}개). 판을 더 천천히, 더 오래 보여 주세요.")
    pick = select_views(obs, board, size, max_views)
    objp = board.object_points().astype(np.float32)
    flags = cv2.CALIB_RATIONAL_MODEL if rational else 0
    crit = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 200, 1e-10)

    def run(idx: list[int]):
        img = [obs[i].corners.astype(np.float32).reshape(-1, 1, 2) for i in idx]
        return cv2.calibrateCameraExtended([objp] * len(idx), img, size, None, None,
                                           flags=flags, criteria=crit)

    rms, K, dist, _, _, sdi, _, pve = run(pick)
    pve = np.asarray(pve).ravel()
    cut = max(3.0 * float(np.median(pve)), INTR_OK_RMS)
    keep = [i for i, e in zip(pick, pve) if e <= cut]
    dropped = len(pick) - len(keep)
    if dropped and len(keep) >= INTR_MIN_VIEWS:
        rms, K, dist, _, _, sdi, _, pve = run(keep)
        pick = keep
    else:
        dropped = 0
    cam = CamModel(K=K, dist=np.asarray(dist).ravel(), size=tuple(size))

    used = set(pick)
    rest = [o for i, o in enumerate(obs) if i not in used]
    if len(rest) > 200:
        rest = [rest[i] for i in np.linspace(0, len(rest) - 1, 200).astype(int)]
    hold = [view_rms(o.corners, cam, board) for o in rest]
    if len(rest) < 5:
        # 남는 장면이 없으면(짧은 촬영) 4등분해서 3/4 로 계산하고 나머지 1/4 로 검산합니다.
        hold = []
        folds = [pick[k::4] for k in range(4)]
        for k, test in enumerate(folds):
            train = [i for j, f in enumerate(folds) if j != k for i in f]
            if len(train) < INTR_MIN_VIEWS or not test:
                continue
            _, K2, d2, *_ = run(train)
            m2 = CamModel(K=K2, dist=np.asarray(d2).ravel(), size=tuple(size))
            hold += [view_rms(obs[i].corners, m2, board) for i in test]
    hold = [h for h in hold if math.isfinite(h)]
    cov, edge, hit = coverage((obs[i].corners for i in pick), size)
    sdi = np.asarray(sdi).ravel()
    return IntrinsicsResult(
        cam=cam, rms=float(rms), holdout_rms=float(np.median(hold)) if hold else math.nan,
        views=len(pick), dropped_views=dropped, coverage=cov, edge_coverage=edge,
        coverage_map=hit,
        std_intrinsics={"fx": float(sdi[0]), "fy": float(sdi[1]),
                        "cx": float(sdi[2]), "cy": float(sdi[3])},
        used_keys=[obs[i].key for i in pick], rational=rational)


# ── 내부 캘리브레이션 저장 ────────────────────────────────────────────────────

#: 렌즈 특성이 같은 조건에서 잰 것인지 대조할 사이드카 항목.
#  fps 가 다르면 화각이 잘릴 수 있습니다 (1080p120 은 38.4도, DESIGN §3.10).
MATCH_KEYS = ("cameraDeviceType", "width", "height", "targetFps", "isBinned")


def intrinsics_record(res: IntrinsicsResult, sidecar, session: str, board: Board,
                      created: str) -> dict:
    return {
        "deviceId": sidecar.device_id,
        "deviceName": sidecar.device_name,
        "model": sidecar.model,
        "cameraDeviceType": sidecar.camera_device_type,
        "fieldOfViewDeg": sidecar.field_of_view_deg,
        "width": int(res.cam.size[0]),
        "height": int(res.cam.size[1]),
        "targetFps": sidecar.target_fps,
        "isBinned": sidecar.is_binned,
        "K": res.cam.K.tolist(),
        "dist": res.cam.dist.tolist(),
        "distModel": "rational8" if res.rational else "k1k2p1p2k3",
        "rmsPx": round(res.rms, 4),
        "holdoutRmsPx": None if math.isnan(res.holdout_rms) else round(res.holdout_rms, 4),
        "views": res.views,
        "droppedViews": res.dropped_views,
        "coverage": round(res.coverage, 3),
        "edgeCoverage": round(res.edge_coverage, 3),
        "stdIntrinsicsPx": {k: round(v, 3) for k, v in res.std_intrinsics.items()},
        "verdict": res.verdict()[0],
        "session": session,
        "created": created,
        "board": board.to_dict(),
    }


def cam_from_record(rec: dict) -> CamModel:
    return CamModel(K=np.array(rec["K"], dtype=float), dist=np.array(rec["dist"], dtype=float),
                    size=(int(rec["width"]), int(rec["height"])))


def intrinsics_mismatch(rec: dict, sidecar) -> list[str]:
    """저장된 렌즈 특성과 이번 촬영 설정이 다른 항목들."""
    now = {"cameraDeviceType": sidecar.camera_device_type, "width": sidecar.width,
           "height": sidecar.height, "targetFps": sidecar.target_fps,
           "isBinned": sidecar.is_binned}
    return [f"{k}: 렌즈 특성 {rec.get(k)!r} / 이번 {now[k]!r}"
            for k in MATCH_KEYS if rec.get(k) != now[k]]


def save_intrinsics(root: Path, rec: dict, stamp: str) -> Path:
    """<root>/<기기ID>.json. 이전 것은 old/ 로 옮겨 둡니다."""
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"{rec['deviceId']}.json"
    if p.exists():
        old = root / "old"
        old.mkdir(exist_ok=True)
        p.replace(old / f"{rec['deviceId']}_{stamp}.json")
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_intrinsics(root: Path, device_id: str) -> dict | None:
    p = Path(root) / f"{device_id}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


# ── 외부 캘리브레이션: 번들 조정 ─────────────────────────────────────────────

#: 외부 캘리브레이션에 쓸 장면의 최대 속도 (px/초). 시각 보간을 하므로 내부보다 느슨합니다.
EXTR_MAX_SPEED = 120.0
#: 카메라 한 쌍이 판을 함께 본 장면이 이보다 적으면 풀지 않습니다.
EXTR_MIN_SHARED = 15
EXTR_GOOD_RMS = 1.0
EXTR_OK_RMS = 2.0
#: 삼각측량한 판 한 칸 길이의 허용 오차 (%)
SCALE_GOOD_PCT = 1.0
SCALE_OK_PCT = 2.0


def _mean_pose(Rs: list[np.ndarray], ts: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray, int]:
    """회전은 평균(바깥값 제외), 위치는 중앙값."""
    rot = Rotation.from_matrix(np.stack(Rs))
    mean = rot.mean()
    keep = np.ones(len(Rs), dtype=bool)
    for _ in range(2):
        ang = (rot * mean.inv()).magnitude()
        keep = ang <= max(math.radians(2.0), 3.0 * float(np.median(ang)))
        mean = rot[keep].mean()
    return mean.as_matrix(), np.median(np.stack(ts)[keep], axis=0), int(keep.sum())


def initial_cam_poses(pnp: list[dict[int, tuple[np.ndarray, np.ndarray]]], ref: int = 0,
                      min_shared: int = EXTR_MIN_SHARED) -> list[tuple[np.ndarray, np.ndarray]]:
    """
    카메라마다 기준 카메라(ref) 좌표 → 그 카메라 좌표 변환의 초기값.
    판을 함께 본 장면이 가장 많은 짝부터 이어 붙입니다 (여러 대일 때 사슬).
    """
    C = len(pnp)
    known: dict[int, tuple[np.ndarray, np.ndarray]] = {ref: (np.eye(3), np.zeros(3))}
    while len(known) < C:
        best = None
        for a in known:
            for b in range(C):
                if b in known:
                    continue
                shared = sorted(set(pnp[a]) & set(pnp[b]))
                if len(shared) >= min_shared and (best is None or len(shared) > len(best[2])):
                    best = (a, b, shared)
        if best is None:
            missing = [c for c in range(C) if c not in known]
            raise CalibError(
                f"카메라 {', '.join(str(c + 1) for c in missing)} 번이 다른 카메라와 판을 함께 본 "
                f"장면이 {min_shared}개보다 적습니다. 판을 두 카메라 사이, 둘 다 보이는 곳에서 "
                "천천히 움직여 주세요.")
        a, b, shared = best
        Rs, ts = [], []
        for m in shared:
            Ra, ta = pnp[a][m]
            Rb, tb = pnp[b][m]
            R_ab = Rb @ Ra.T
            Rs.append(R_ab)
            ts.append(tb - R_ab @ ta)
        R_ab, t_ab, _ = _mean_pose(Rs, ts)
        R_a, t_a = known[a]
        known[b] = (R_ab @ R_a, R_ab @ t_a + t_ab)
    return [known[c] for c in range(C)]


@dataclass
class RigSolution:
    cam_R: list[np.ndarray]           # 기준 카메라 좌표 → 카메라 c
    cam_t: list[np.ndarray]
    board: dict[int, tuple[np.ndarray, np.ndarray]]   # 장면 m: 판 → 기준 카메라 좌표
    obs_rms: dict[tuple[int, int], float]              # (카메라, 장면) → 재투영 오차 px
    dropped: int = 0

    def cam_rms(self, c: int) -> float:
        e = [v for (cc, _), v in self.obs_rms.items() if cc == c]
        return float(np.sqrt(np.mean(np.square(e)))) if e else math.nan

    @property
    def rms(self) -> float:
        e = list(self.obs_rms.values())
        return float(np.sqrt(np.mean(np.square(e)))) if e else math.nan


def bundle_adjust(cams: Sequence[CamModel], obs: Sequence[dict[int, Obs]],
                  board: Board = DEFAULT_BOARD, ref: int = 0,
                  init: list[tuple[np.ndarray, np.ndarray]] | None = None,
                  min_shared: int = EXTR_MIN_SHARED) -> RigSolution:
    """
    카메라 자세와 장면마다의 판 자세를 함께 맞춥니다 (재투영 오차 최소화).

    렌즈 특성은 고정합니다 (내부 캘리브레이션이 더 정확함). 두 대 이상이 함께 본
    장면만 씁니다 — 한 대만 본 장면은 카메라 사이 관계에 아무것도 보태지 않습니다.
    오차가 튀는 관측은 한 번 빼고 다시 맞춥니다.
    """
    C = len(cams)
    pnp: list[dict[int, tuple[np.ndarray, np.ndarray]]] = []
    for c in range(C):
        d = {}
        for m, o in obs[c].items():
            p = board_pose(o.corners, cams[c], board)
            if p is not None:
                d[m] = p
        pnp.append(d)
    init = init or initial_cam_poses(pnp, ref, min_shared)

    active = {c: set(pnp[c]) for c in range(C)}
    dropped = 0
    x_cam = None
    sol = None
    for rnd in range(2):
        counts: dict[int, int] = {}
        for c in range(C):
            for m in active[c]:
                counts[m] = counts.get(m, 0) + 1
        samples = sorted(m for m, n in counts.items() if n >= 2)
        if not samples:
            raise CalibError("두 카메라 이상이 함께 본 장면이 없습니다.")
        sol = _ba_once(cams, obs, pnp, active, samples, board, ref,
                       init if x_cam is None else x_cam)
        x_cam = list(zip(sol.cam_R, sol.cam_t))
        if rnd == 0:
            errs = np.array(list(sol.obs_rms.values()))
            cut = max(4.0 * float(np.median(errs)), 2.0)
            bad = [k for k, v in sol.obs_rms.items() if v > cut]
            if not bad:
                break
            for c, m in bad:
                active[c].discard(m)
            dropped = len(bad)
    sol.dropped = dropped
    return sol


def _ba_once(cams, obs, pnp, active, samples, board, ref, init) -> RigSolution:
    C = len(cams)
    free = [c for c in range(C) if c != ref]
    ci = {c: i for i, c in enumerate(free)}
    si = {m: i for i, m in enumerate(samples)}
    objp = board.object_points()
    n = board.n

    # 판 자세 초기값: 기준 카메라가 봤으면 그 PnP, 아니면 다른 카메라 PnP 를 옮김
    b0 = []
    for m in samples:
        src = ref if m in pnp[ref] and m in active[ref] else \
            next(c for c in range(C) if m in active[c])
        Rb, tb = pnp[src][m]
        Rc, tc = init[src]
        b0.append((Rc.T @ Rb, Rc.T @ (tb - tc)))

    x0 = np.concatenate(
        [np.concatenate([Rotation.from_matrix(init[c][0]).as_rotvec(), init[c][1]]) for c in free]
        + [np.concatenate([Rotation.from_matrix(R).as_rotvec(), t]) for R, t in b0])

    groups = []     # 카메라별 (장면 번호 배열, 꼭짓점 (O, n, 2), 장면 키 목록)
    for c in range(C):
        ms = [m for m in samples if m in active[c]]
        if ms:
            groups.append((c, np.array([si[m] for m in ms]),
                           np.stack([obs[c][m].corners for m in ms]), ms))

    nf = 6 * len(free)

    def unpack(x):
        cr = [np.eye(3)] * C
        ct = [np.zeros(3)] * C
        if free:
            p = x[:nf].reshape(-1, 6)
            Rm = Rotation.from_rotvec(p[:, :3]).as_matrix()
            for c in free:
                cr[c], ct[c] = Rm[ci[c]], p[ci[c], 3:]
        q = x[nf:].reshape(-1, 6)
        return cr, ct, Rotation.from_rotvec(q[:, :3]).as_matrix(), q[:, 3:]

    def fun(x):
        cr, ct, bR, bt = unpack(x)
        out = []
        for c, s, uv, _ in groups:
            Xr = np.einsum("mij,nj->mni", bR[s], objp) + bt[s][:, None, :]
            Xc = Xr @ cr[c].T + ct[c]
            out.append((project(Xc.reshape(-1, 3), cams[c]).reshape(len(s), n, 2) - uv).ravel())
        return np.concatenate(out)

    rows = sum(len(s) for _, s, _, _ in groups) * 2 * n
    J = lil_matrix((rows, x0.size), dtype=np.uint8)
    r = 0
    for c, s, _, _ in groups:
        for k in s:
            if c != ref:
                J[r:r + 2 * n, 6 * ci[c]:6 * ci[c] + 6] = 1
            J[r:r + 2 * n, nf + 6 * k:nf + 6 * k + 6] = 1
            r += 2 * n

    res = least_squares(fun, x0, jac_sparsity=J, method="trf", loss="soft_l1",
                        f_scale=1.0, x_scale="jac", max_nfev=200)
    cr, ct, bR, bt = unpack(res.x)
    resid = fun(res.x)
    obs_rms: dict[tuple[int, int], float] = {}
    r = 0
    for c, s, _, ms in groups:
        for m in ms:
            e = resid[r:r + 2 * n].reshape(n, 2)
            obs_rms[(c, m)] = float(np.sqrt(np.mean(np.sum(e * e, axis=1))))
            r += 2 * n
    return RigSolution(cam_R=list(cr), cam_t=list(ct),
                       board={m: (bR[si[m]], bt[si[m]]) for m in samples}, obs_rms=obs_rms)


def shared_counts(sol: RigSolution, C: int) -> dict[tuple[int, int], int]:
    """카메라 짝마다 판을 함께 본 (번들 조정에 쓰인) 장면 수."""
    by_m: dict[int, set[int]] = {}
    for (c, m) in sol.obs_rms:
        by_m.setdefault(m, set()).add(c)
    out = {(a, b): 0 for a in range(C) for b in range(a + 1, C)}
    for cs in by_m.values():
        for a in cs:
            for b in cs:
                if a < b:
                    out[(a, b)] += 1
    return out


def triangulate(cams: Sequence[CamModel], Rs: Sequence[np.ndarray], ts: Sequence[np.ndarray],
                pts: Sequence[np.ndarray]) -> np.ndarray:
    """
    카메라 여러 대의 같은 점들 (각 (N, 2) px) → 3D (N, 3). 왜곡을 편 뒤 선형(DLT).
    Pose2Sim 과 같은 방식이라, 이 결과가 맞으면 Pose2Sim 삼각측량도 맞습니다.
    """
    A = []
    for cam, R, t, p in zip(cams, Rs, ts, pts):
        u = undistort_normalized(p, cam)
        P = np.hstack([R, t.reshape(3, 1)])
        A.append(u[:, :1] * P[2] - P[0])
        A.append(u[:, 1:] * P[2] - P[1])
    A = np.stack(A, axis=1)                        # (N, 2C, 4)
    _, _, vt = np.linalg.svd(A)
    X = vt[:, -1, :]
    return X[:, :3] / X[:, 3:]


def scale_check(cams: Sequence[CamModel], sol: RigSolution, obs: Sequence[dict[int, Obs]],
                board: Board = DEFAULT_BOARD) -> dict:
    """
    ★ 판 자세와 무관한 검산: 카메라 자세만으로 꼭짓점을 삼각측량해서 한 칸 길이를 잽니다.
    23 mm 로 나오면 카메라 사이 거리(축척)와 방향이 맞는 것입니다.
    """
    by_m: dict[int, list[int]] = {}
    for (c, m) in sol.obs_rms:
        by_m.setdefault(m, []).append(c)
    lengths = []
    for m, cs in by_m.items():
        if len(cs) < 2:
            continue
        X = triangulate([cams[c] for c in cs], [sol.cam_R[c] for c in cs],
                        [sol.cam_t[c] for c in cs], [obs[c][m].corners for c in cs])
        g = X.reshape(board.rows, board.cols, 3)
        d = np.concatenate([np.linalg.norm(np.diff(g, axis=1), axis=2).ravel(),
                            np.linalg.norm(np.diff(g, axis=0), axis=2).ravel()])
        lengths.append(float(np.mean(d)))
    if not lengths:
        return {"views": 0}
    med = float(np.median(lengths))
    return {"views": len(lengths), "squareMm": round(med * 1000, 3),
            "errorPct": round((med / board.square_m - 1) * 100, 3),
            "spreadMm": round(float(np.std(lengths)) * 1000, 3)}


# ── 바닥 (위쪽 방향) ─────────────────────────────────────────────────────────

#: 바닥 판: 이 속도(px/초) 아래로 이 시간 이상 멈춰 있어야 합니다.
STILL_SPEED = 20.0
STILL_MIN_S = 1.5
#: 판의 위쪽(-Z)이 카메라의 위쪽(-y)과 이 각도 안이어야 "바닥에 누운 판" 입니다.
#  카메라를 수평에 가깝게 세우면 바닥 판은 거의 0~60도, 손에 세워 든 판은 90도 근처입니다.
FLOOR_MAX_TILT_DEG = 60.0


@dataclass
class Floor:
    up: np.ndarray            # 기준 카메라 좌표의 위쪽 단위벡터
    point: np.ndarray         # 기준 카메라 좌표의 바닥 판 가운데
    cam: int                  # 어느 카메라로 찾았나
    duration_s: float
    views: int
    rms: float                # 평균 꼭짓점으로 맞춘 판 자세의 재투영 오차 (px)
    keys: list[int] = field(default_factory=list)   # 멈춰 있던 격자 번호들


def find_floor(cams: Sequence[CamModel], sol: RigSolution, obs: Sequence[dict[int, Obs]],
               grid: np.ndarray, board: Board = DEFAULT_BOARD,
               still_speed: float = STILL_SPEED, min_still_s: float = STILL_MIN_S,
               max_tilt_deg: float = FLOOR_MAX_TILT_DEG) -> Floor | None:
    """
    바닥에 눕혀 둔 판을 찾습니다: 멈춰 있고(min_still_s 이상) 판의 위쪽이 카메라의
    위쪽과 비슷한 구간 중 가장 긴 것. 멈춘 동안의 꼭짓점을 평균해 자세를 한 번에 풉니다
    (잡음이 줄어듦). 한 카메라만 봐도 됩니다.
    """
    best: Floor | None = None
    cam_up = np.array([0.0, -1.0, 0.0])
    for c, cam in enumerate(cams):
        cand = []
        for m in sorted(obs[c]):
            o = obs[c][m]
            if o.speed > still_speed:
                continue
            p = board_pose(o.corners, cam, board)
            if p is None:
                continue
            up_c = -p[0][:, 2]
            if math.degrees(math.acos(float(np.clip(up_c @ cam_up, -1, 1)))) <= max_tilt_deg:
                cand.append(m)
        runs, cur = [], []
        for m in cand:
            if cur and m - cur[-1] > 2:
                runs.append(cur)
                cur = []
            cur.append(m)
        if cur:
            runs.append(cur)
        for run in runs:
            dur = (int(grid[run[-1]]) - int(grid[run[0]])) / NS_PER_S
            if dur < min_still_s:
                continue
            if best is not None and (dur, len(run)) <= (best.duration_s, best.views):
                continue
            mean_c = np.mean(np.stack([obs[c][m].corners for m in run]), axis=0)
            p = board_pose(mean_c, cam, board)
            if p is None:
                continue
            Rb, tb = p
            Rc, tc = sol.cam_R[c], sol.cam_t[c]
            R_ref = Rc.T @ Rb
            t_ref = Rc.T @ (tb - tc)
            up = -R_ref[:, 2]
            best = Floor(up=up / np.linalg.norm(up), point=R_ref @ board.center + t_ref,
                         cam=c, duration_s=dur, views=len(run),
                         rms=view_rms(mean_c, cam, board), keys=list(run))
    return best


def world_frame(up: np.ndarray, origin: np.ndarray,
                forward: np.ndarray = np.array([0.0, 0.0, 1.0])) -> tuple[np.ndarray, np.ndarray]:
    """
    기준 카메라 좌표에서 월드 축 (열 = X, Y, Z) 과 원점.
    Z = 위, Y = forward(기준 카메라가 보는 방향)를 수평으로 누인 것, X = Y × Z.
    """
    Z = up / np.linalg.norm(up)
    f = forward - (forward @ Z) * Z
    if np.linalg.norm(f) < 1e-6:          # 카메라가 바로 아래/위를 봄
        f = np.array([1.0, 0.0, 0.0]) - Z[0] * Z
    Y = f / np.linalg.norm(f)
    X = np.cross(Y, Z)
    return np.column_stack([X, Y, Z]), np.asarray(origin, dtype=float)


def to_world(sol: RigSolution, R_W: np.ndarray, o: np.ndarray
             ) -> list[tuple[np.ndarray, np.ndarray]]:
    """
    카메라마다 월드 → 카메라 (R, t).
    X_ref = R_W·X_w + o  이고  X_c = R_c·X_ref + t_c  이므로
    X_c = (R_c·R_W)·X_w + (R_c·o + t_c).
    """
    return [(Rc @ R_W, Rc @ o + tc) for Rc, tc in zip(sol.cam_R, sol.cam_t)]


def camera_center(R: np.ndarray, t: np.ndarray) -> np.ndarray:
    return -R.T @ t


# ── Pose2Sim 형식 ─────────────────────────────────────────────────────────────

def _f(x) -> str:
    v = float(x)
    if not math.isfinite(v):
        raise CalibError(f"캘리브레이션 값이 유한하지 않습니다: {v}")
    return repr(v)


def calib_toml(names: Sequence[str], cams: Sequence[CamModel],
               poses: Sequence[tuple[np.ndarray, np.ndarray]], error_px: float) -> str:
    """
    Pose2Sim Calib.toml. 카메라 순서 = names 순서 (cam01, cam02 ...).
    ★ Pose2Sim 은 이 **순서**로 영상과 짝짓습니다 (pipeline.py 모듈 설명).
    """
    out = []
    for name, cam, (R, t) in zip(names, cams, poses):
        K = cam.K
        rvec = cv2.Rodrigues(np.asarray(R, dtype=float))[0].ravel()
        out += [
            f"[{name}]",
            f'name = "{name}"',
            f"size = [ {_f(cam.size[0])}, {_f(cam.size[1])}]",
            f"matrix = [ [ {_f(K[0, 0])}, {_f(K[0, 1])}, {_f(K[0, 2])}], "
            f"[ 0.0, {_f(K[1, 1])}, {_f(K[1, 2])}], [ 0.0, 0.0, 1.0]]",
            "distortions = [ " + ", ".join(_f(v) for v in np.ravel(cam.dist)) + "]",
            "rotation = [ " + ", ".join(_f(v) for v in rvec) + "]",
            "translation = [ " + ", ".join(_f(v) for v in np.ravel(t)) + "]",
            "fisheye = false",
            "",
        ]
    out += ["[metadata]", "adjusted = false", f"error = {_f(error_px)}", ""]
    return "\n".join(out)


# ── 확인 그림 ─────────────────────────────────────────────────────────────────

def _max_radius(cam: CamModel) -> float:
    """화면 가장자리 점들의 (왜곡 편) 정규 좌표 반지름 최댓값. 이 밖은 그리지 않습니다."""
    w, h = cam.size
    edge = np.array([[0, 0], [w / 2, 0], [w - 1, 0], [w - 1, h / 2], [w - 1, h - 1],
                     [w / 2, h - 1], [0, h - 1], [0, h / 2]], dtype=np.float64)
    return float(np.max(np.linalg.norm(undistort_normalized(edge, cam), axis=1)))


def _draw_polyline(img, pts_w, R, t, cam, rmax, color, thick) -> None:
    Xc = pts_w @ R.T + t
    ok = Xc[:, 2] > 0.05
    r = np.full(len(Xc), np.inf)
    r[ok] = np.hypot(Xc[ok, 0] / Xc[ok, 2], Xc[ok, 1] / Xc[ok, 2])
    ok &= r <= rmax * 1.02
    uv = np.full((len(Xc), 2), np.nan)
    if ok.any():
        uv[ok] = project(Xc[ok], cam)
    for a, b in zip(range(len(uv) - 1), range(1, len(uv))):
        if ok[a] and ok[b]:
            cv2.line(img, tuple(np.round(uv[a]).astype(int)), tuple(np.round(uv[b]).astype(int)),
                     color, thick, cv2.LINE_AA)


def draw_check(frame: np.ndarray, cam: CamModel, R: np.ndarray, t: np.ndarray,
               others: Sequence[tuple[str, np.ndarray]], floor: bool,
               grid_half_m: float = 3.0, step_m: float = 0.5) -> np.ndarray:
    """
    카메라 영상 위에 월드 바닥 격자(0.5 m), 원점 축, 다른 카메라 위치를 그립니다.
    바닥 격자가 실제 바닥에 붙어 보이고, 다른 폰이 화면에 보이면 그 자리에 동그라미가
    오면 캘리브레이션이 맞습니다.
    """
    img = frame.copy()
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    rmax = _max_radius(cam)
    thick = max(1, img.shape[1] // 900)
    vals = np.arange(-grid_half_m, grid_half_m + 1e-9, step_m)
    s = np.linspace(-grid_half_m, grid_half_m, 121)
    col = (0, 220, 255) if floor else (160, 160, 160)
    for v in vals:
        _draw_polyline(img, np.column_stack([s, np.full_like(s, v), np.zeros_like(s)]),
                       R, t, cam, rmax, col, thick)
        _draw_polyline(img, np.column_stack([np.full_like(s, v), s, np.zeros_like(s)]),
                       R, t, cam, rmax, col, thick)
    ax = np.linspace(0, 0.5, 20)
    z = np.zeros_like(ax)
    for vec, colr in (((ax, z, z), (0, 0, 255)), ((z, ax, z), (0, 200, 0)), ((z, z, ax), (255, 80, 0))):
        _draw_polyline(img, np.column_stack(vec), R, t, cam, rmax, colr, thick * 3)
    for label, C in others:
        Xc = R @ C + t
        if Xc[2] <= 0.05 or math.hypot(Xc[0] / Xc[2], Xc[1] / Xc[2]) > rmax:
            continue
        uv = np.round(project(Xc.reshape(1, 3), cam)[0]).astype(int)
        cv2.circle(img, tuple(uv), 28 * thick, (255, 0, 255), 2 * thick, cv2.LINE_AA)
        cv2.putText(img, label, (int(uv[0]) + 30 * thick, int(uv[1])), cv2.FONT_HERSHEY_SIMPLEX,
                    0.9 * thick, (255, 0, 255), 2 * thick, cv2.LINE_AA)
    note = "floor grid 0.5 m" if floor else "NO FLOOR BOARD - grid height is a guess"
    font, scale = cv2.FONT_HERSHEY_SIMPLEX, 0.9 * thick
    (tw, th), base = cv2.getTextSize(note, font, scale, 2 * thick)
    cv2.rectangle(img, (10, 10), (30 + tw, 30 + th + base), (0, 0, 0), -1)
    cv2.putText(img, note, (20, 20 + th), font, scale, (255, 255, 255), 2 * thick, cv2.LINE_AA)
    return img


def draw_coverage(frame: np.ndarray | None, size: tuple[int, int],
                  corner_sets: Iterable[np.ndarray], hit: np.ndarray) -> np.ndarray:
    """계산에 쓴 꼭짓점을 점으로, 판이 한 번도 안 닿은 칸을 빨갛게."""
    w, h = size
    if frame is None:
        img = np.full((h, w, 3), 40, np.uint8)
    else:
        img = (frame.astype(np.float32) * 0.5).astype(np.uint8)
    gy, gx = hit.shape
    over = img.copy()
    for iy in range(gy):
        for ix in range(gx):
            if not hit[iy, ix]:
                cv2.rectangle(over, (int(ix * w / gx), int(iy * h / gy)),
                              (int((ix + 1) * w / gx) - 1, int((iy + 1) * h / gy) - 1),
                              (0, 0, 200), -1)
    img = cv2.addWeighted(over, 0.4, img, 0.6, 0)
    r = max(2, w // 640)
    for c in corner_sets:
        for p in c:
            cv2.circle(img, (int(round(p[0])), int(round(p[1]))), r, (0, 255, 120), -1)
    return img
