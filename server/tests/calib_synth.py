"""
캘리브레이션 시험용 가짜 카메라 — 정답(렌즈 특성·자세)을 아는 체커보드 영상을 그립니다.

실제 폰으로 시험할 수 없으므로(AI 는 폰을 못 만짐) 정답을 아는 영상을 만들어
도구가 정답을 되찾는지 봅니다. 그림은 "픽셀마다 광선을 쏴서 판과 만나는 점의 색"
으로 그리므로 왜곡·원근이 실제 카메라와 같은 식을 따릅니다.
  픽셀 → (왜곡 폄) 정규 좌표 → 광선 → 판 평면과 교점 → 판 좌표 → 흑/백
"""
from __future__ import annotations

import cv2
import numpy as np

from mocapsync.calib import Board, CamModel

_CRIT = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 60, 1e-14)


def look_at(pos, target, up=(0.0, 0.0, 1.0)) -> tuple[np.ndarray, np.ndarray]:
    """월드(Z 위) → 카메라(OpenCV: x 오른쪽, y 아래, z 앞) 자세 (R, t)."""
    pos, target, up = (np.asarray(v, dtype=float) for v in (pos, target, up))
    z = target - pos
    z /= np.linalg.norm(z)
    x = np.cross(z, up)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.stack([x, y, z])
    return R, -R @ pos


def board_in_world(center, normal_toward, spin_deg=0.0, board: Board = Board()
                   ) -> tuple[np.ndarray, np.ndarray]:
    """
    판 → 월드 자세. 판 앞면(-Z)이 normal_toward 를 향하고, 판 가운데가 center 에 옵니다.
    spin_deg 만큼 판을 제 면 안에서 돌립니다.
    """
    center = np.asarray(center, dtype=float)
    front = np.asarray(normal_toward, dtype=float) - center
    front /= np.linalg.norm(front)
    Zb = -front                                   # 판 Z = 종이 뒤쪽
    ref = np.array([0.0, 0.0, -1.0]) if abs(Zb[2]) < 0.9 else np.array([0.0, 1.0, 0.0])
    Yb = ref - (ref @ Zb) * Zb                    # 종이 세로(아래)를 되도록 월드 아래로
    Yb /= np.linalg.norm(Yb)
    Xb = np.cross(Yb, Zb)
    R = np.column_stack([Xb, Yb, Zb])
    a = np.radians(spin_deg)
    spin = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    R = R @ spin
    return R, center - R @ board.center


def floor_board(center_xy, yaw_deg=0.0, board: Board = Board()) -> tuple[np.ndarray, np.ndarray]:
    """바닥에 앞면이 위로 가게 눕힌 판 (판 Z = 월드 -Z)."""
    a = np.radians(yaw_deg)
    Xb = np.array([np.cos(a), np.sin(a), 0.0])
    Zb = np.array([0.0, 0.0, -1.0])
    Yb = np.cross(Zb, Xb)
    R = np.column_stack([Xb, Yb, Zb])
    c = np.array([center_xy[0], center_xy[1], 0.0])
    return R, c - R @ board.center


class SynthCam:
    def __init__(self, cam: CamModel, ss: int = 1, seed: int = 0):
        self.cam = cam
        self.ss = ss
        w, h = cam.size
        W, H = w * ss, h * ss
        us = (np.arange(W) + 0.5) / ss - 0.5
        vs = (np.arange(H) + 0.5) / ss - 0.5
        uu, vv = np.meshgrid(us, vs)
        px = np.stack([uu.ravel(), vv.ravel()], axis=1).reshape(-1, 1, 2)
        n = cv2.undistortPoints(px, cam.K, cam.dist, criteria=_CRIT).reshape(H, W, 2)
        self.rays = np.concatenate([n, np.ones((H, W, 1))], axis=2)
        rng = np.random.default_rng(seed)
        low = rng.uniform(70, 170, size=(max(2, h // 24), max(2, w // 24))).astype(np.float32)
        self.bg = cv2.resize(low, (W, H), interpolation=cv2.INTER_CUBIC)
        self.rng = rng

    def render(self, boards: list[tuple[np.ndarray, np.ndarray]], board: Board,
               noise: float = 1.5, blur: float = 0.7) -> np.ndarray:
        """
        boards: 판 → 카메라 자세 (R, t) 목록. 흑백 uint8 (h, w).
        판이 차지하는 사각형 영역만 계산합니다 (빠르게).
        """
        img = self.bg.copy()
        H, W = img.shape
        s = board.square_m
        m = 0.6 * s
        lo_u, hi_u = -s - m, board.cols * s + m
        lo_v, hi_v = -s - m, board.rows * s + m
        for R, t in boards:
            # 종이 윤곽을 투영해 계산할 영역을 정함 (가장자리 여유 포함)
            e = np.linspace(0, 1, 9)
            outline = np.concatenate([
                np.stack([lo_u + (hi_u - lo_u) * e, np.full(9, lo_v)], 1),
                np.stack([lo_u + (hi_u - lo_u) * e, np.full(9, hi_v)], 1),
                np.stack([np.full(9, lo_u), lo_v + (hi_v - lo_v) * e], 1),
                np.stack([np.full(9, hi_u), lo_v + (hi_v - lo_v) * e], 1)])
            Xc = np.column_stack([outline, np.zeros(len(outline))]) @ R.T + t
            if np.any(Xc[:, 2] <= 0.01):
                continue
            uv, _ = cv2.projectPoints(Xc, np.zeros(3), np.zeros(3), self.cam.K, self.cam.dist)
            uv = uv.reshape(-1, 2) * self.ss
            x0, y0 = np.floor(uv.min(0)).astype(int) - 4 * self.ss
            x1, y1 = np.ceil(uv.max(0)).astype(int) + 4 * self.ss
            x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, W), min(y1, H)
            if x1 <= x0 or y1 <= y0:
                continue
            rays = self.rays[y0:y1, x0:x1]
            n = R[:, 2]
            den = rays @ n
            with np.errstate(divide="ignore", invalid="ignore"):
                lam = (n @ t) / den
            P = lam[..., None] * rays - t
            ub = P @ R[:, 0]
            vb = P @ R[:, 1]
            ok = np.isfinite(lam) & (lam > 0)
            c = np.floor(ub / s).astype(int) + 1
            r = np.floor(vb / s).astype(int) + 1
            in_sq = (c >= 0) & (c <= board.cols) & (r >= 0) & (r <= board.rows)
            black = in_sq & ((r + c) % 2 == 0)
            paper = ok & (ub >= lo_u) & (ub <= hi_u) & (vb >= lo_v) & (vb <= hi_v)
            patch = img[y0:y1, x0:x1]
            patch[paper] = np.where(black[paper], 25.0, 230.0)
        if self.ss > 1:
            img = cv2.resize(img, self.cam.size, interpolation=cv2.INTER_AREA)
        if blur > 0:
            img = cv2.GaussianBlur(img, (0, 0), blur)
        if noise > 0:
            img = img + self.rng.normal(0, noise, img.shape)
        return np.clip(img, 0, 255).astype(np.uint8)


def true_corners(cam: CamModel, R: np.ndarray, t: np.ndarray, board: Board) -> np.ndarray:
    uv, _ = cv2.projectPoints(board.object_points(), cv2.Rodrigues(R)[0], t, cam.K, cam.dist)
    return uv.reshape(-1, 2)


def compose(R_wc, t_wc, R_bw, t_bw) -> tuple[np.ndarray, np.ndarray]:
    """판→월드 와 월드→카메라 를 이어 판→카메라."""
    return R_wc @ R_bw, R_wc @ t_bw + t_wc
