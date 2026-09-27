"""
캘리브레이션용 체커보드 인쇄 파일(A4 PDF)을 만듭니다.

Pose2Sim 기본 보드(한 칸 60 mm, 5x8 칸 = 300x480 mm)는 A4(210x297 mm)에 들어가지 않습니다.
그래서 A4 에 맞춘 보드를 씁니다.

  7 x 10 칸, 한 칸 23 mm  → 보드 161 x 230 mm
  안쪽 꼭짓점 6 x 9  (Pose2Sim 설정 corners_nb 의 순서는 캘리브레이션 도구에서 실측으로 정합니다)

  - 꼭짓점 수를 짝수 x 홀수로 둡니다. 둘 다 같은 홀짝이면 보드를 180도 돌렸을 때
    OpenCV 가 방향을 구분하지 못합니다.
  - 보드 둘레에 흰 여백(15 mm 이상)을 둡니다. OpenCV 검출에 필요합니다.
  - 아래에 100 mm 눈금 막대를 넣습니다. 프린터가 크기를 줄였는지 자로 확인하는 용도입니다.

글자는 영어로 넣습니다 (matplotlib 기본 글꼴에 한글이 없어 깨질 수 있음).

Pose2Sim 환경의 파이썬으로 실행합니다 (matplotlib 필요):
    & "$env:USERPROFILE\\.venv\\pose2sim_gpu\\Scripts\\python.exe" tools\\make_checkerboard.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

A4_W, A4_H = 210.0, 297.0          # mm
MM_PER_INCH = 25.4


def draw(cols: int, rows: int, square: float, out: Path, preview_dpi: int = 0) -> None:
    bw, bh = cols * square, rows * square
    if bw > A4_W - 30 or bh > A4_H - 60:
        raise SystemExit(f"보드 {bw}x{bh} mm 가 A4 에 여백과 함께 들어가지 않습니다")

    fig = plt.figure(figsize=(A4_W / MM_PER_INCH, A4_H / MM_PER_INCH))
    ax = fig.add_axes((0, 0, 1, 1))          # 여백 없이 종이 전체 = mm 좌표
    ax.set_xlim(0, A4_W)
    ax.set_ylim(A4_H, 0)                     # 위에서 아래로
    ax.axis("off")

    x0 = (A4_W - bw) / 2
    y0 = 15.0
    for r in range(rows):
        for c in range(cols):
            if (r + c) % 2 == 0:
                ax.add_patch(Rectangle((x0 + c * square, y0 + r * square), square, square,
                                       facecolor="black", edgecolor="none", antialiased=False))

    # 100 mm 확인용 막대
    yr = y0 + bh + 12
    xr = (A4_W - 100) / 2
    ax.plot([xr, xr + 100], [yr, yr], color="black", linewidth=1.2)
    for k in range(11):
        h = 4 if k % 5 == 0 else 2
        ax.plot([xr + 10 * k, xr + 10 * k], [yr - h, yr], color="black", linewidth=0.8)
    ax.text(A4_W / 2, yr + 5, "This line must measure exactly 100 mm", ha="center",
            va="top", fontsize=9)

    info = (f"MocapSync calibration board  |  {cols} x {rows} squares, {square:g} mm  |  "
            f"inner corners {cols - 1} x {rows - 1}\n"
            "Print at 100% (\"Actual size\", NOT \"Fit to page\").  "
            "Glue flat on a rigid board. Matte paper.")
    ax.text(A4_W / 2, yr + 14, info, ha="center", va="top", fontsize=7.5, linespacing=1.6)

    fig.savefig(out, format="pdf")
    if preview_dpi:
        fig.savefig(out.with_suffix(".png"), dpi=preview_dpi)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description="A4 체커보드 PDF")
    ap.add_argument("--out", type=Path,
                    default=Path.home() / "Desktop" / "MocapSync_checkerboard_A4_7x10_23mm.pdf")
    ap.add_argument("--cols", type=int, default=7)
    ap.add_argument("--rows", type=int, default=10)
    ap.add_argument("--square", type=float, default=23.0, help="한 칸 크기 (mm)")
    ap.add_argument("--preview-dpi", type=int, default=0, help="PNG 미리보기도 저장 (검사용)")
    a = ap.parse_args()
    if (a.cols - 1) % 2 == (a.rows - 1) % 2:
        print("★ 안쪽 꼭짓점 수가 짝수 x 홀수여야 합니다 (칸 수로는 홀수 x 짝수)")
        return 2
    draw(a.cols, a.rows, a.square, a.out, a.preview_dpi)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(f"저장: {a.out}")
    print(f"안쪽 꼭짓점 {a.cols - 1} x {a.rows - 1}, 한 칸 {a.square:g} mm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
