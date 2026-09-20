#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
TRC 파일 점검 도구.

.trc 의 헤더, 마커 목록, 축별 범위, 마커별 평균 위치, 결측률, 뼈 길이 통계를 찍습니다.

왜 필요한가
----------
1. 좌표계를 추측하지 않기 위해서. Blender 는 Z-up 인데 OpenSim/.trc 는 보통 Y-up 입니다.
   어느 축이 '위'인지는 실제 값(예: 골반 높이 ~0.9m, 머리 ~1.6m)을 보면 확정됩니다.
2. 뼈 길이 표준편차는 설계 문서의 검증 항목 ③ 입니다 (5mm 이내 목표).
   나중에 우리 폰 데이터에 그대로 쓸 수 있도록 지금 만들어 둡니다.

사용법
------
  python tools/trc_inspect.py <trc파일>
  python tools/trc_inspect.py <trc파일> --bones
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# HALPE_26 기반 골격 연결 (Pose2Sim 이 삼각측량하는 22개 마커용)
BONES = [
    ("Hip", "RHip"), ("RHip", "RKnee"), ("RKnee", "RAnkle"),
    ("RAnkle", "RHeel"), ("RAnkle", "RBigToe"), ("RBigToe", "RSmallToe"),
    ("Hip", "LHip"), ("LHip", "LKnee"), ("LKnee", "LAnkle"),
    ("LAnkle", "LHeel"), ("LAnkle", "LBigToe"), ("LBigToe", "LSmallToe"),
    ("Hip", "Neck"), ("Neck", "Head"), ("Head", "Nose"),
    ("Neck", "RShoulder"), ("RShoulder", "RElbow"), ("RElbow", "RWrist"),
    ("Neck", "LShoulder"), ("LShoulder", "LElbow"), ("LElbow", "LWrist"),
]


def load_trc(path: Path) -> dict:
    """TRC 를 읽어 {header, markers, times, frames[[x,y,z]*n]} 로 돌려줍니다."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) < 6:
        raise ValueError(f"TRC 가 너무 짧습니다: {path}")

    keys = [k.strip() for k in lines[1].split("\t")]
    vals = [v.strip() for v in lines[2].split("\t")]
    header = dict(zip(keys, vals))

    # 4행: Frame#  Time  <마커명들>
    markers = [m.strip() for m in lines[3].split("\t")[2:] if m.strip()]

    times: list[float] = []
    frames: list[list[tuple[float, float, float]]] = []

    def num(tok: str) -> float:
        tok = tok.strip()
        if not tok:
            return math.nan
        try:
            return float(tok)
        except ValueError:
            return math.nan

    for ln in lines[5:]:
        if not ln.strip():
            continue
        p = ln.split("\t")
        if len(p) < 3:
            continue
        times.append(num(p[1]))
        coords = [num(v) for v in p[2:]]
        pts = []
        for k in range(len(markers)):
            i = 3 * k
            if i + 2 < len(coords):
                pts.append((coords[i], coords[i + 1], coords[i + 2]))
            else:
                pts.append((math.nan, math.nan, math.nan))
        frames.append(pts)

    return {"header": header, "markers": markers, "times": times, "frames": frames, "path": path}


def guess_up_axis(trc: dict) -> tuple[int, str]:
    """
    '위' 축을 데이터로 추정합니다.
    근거: 사람은 서 있으므로 발(Heel/BigToe)과 머리(Head)의 차이가 가장 큰 축이 수직입니다.
    """
    markers = trc["markers"]
    frames = trc["frames"]
    if "Head" not in markers:
        return 1, "Head 마커가 없어 추정 불가 (기본 Y 가정)"

    hi = markers.index("Head")
    foot_names = [n for n in ("RHeel", "LHeel", "RBigToe", "LBigToe", "RAnkle", "LAnkle") if n in markers]
    if not foot_names:
        return 1, "발 마커가 없어 추정 불가 (기본 Y 가정)"
    fis = [markers.index(n) for n in foot_names]

    diffs = [0.0, 0.0, 0.0]
    cnt = 0
    for pts in frames:
        head = pts[hi]
        if any(math.isnan(c) for c in head):
            continue
        feet = [pts[i] for i in fis if not any(math.isnan(c) for c in pts[i])]
        if not feet:
            continue
        cnt += 1
        for ax in range(3):
            foot_mean = sum(p[ax] for p in feet) / len(feet)
            diffs[ax] += head[ax] - foot_mean
    if cnt == 0:
        return 1, "유효 프레임 없음 (기본 Y 가정)"
    diffs = [d / cnt for d in diffs]
    ax = max(range(3), key=lambda a: abs(diffs[a]))
    sign = "+" if diffs[ax] > 0 else "-"
    label = "XYZ"[ax]
    detail = ("머리-발 평균차: " +
              ", ".join(f"{'XYZ'[a]}={diffs[a]:+.3f}m" for a in range(3)) +
              f"  -> 수직축 = {sign}{label}")
    return ax, detail


def main() -> int:
    ap = argparse.ArgumentParser(description="TRC 파일 점검")
    ap.add_argument("trc", type=Path)
    ap.add_argument("--bones", action="store_true", help="뼈 길이 통계도 출력")
    args = ap.parse_args()

    trc = load_trc(args.trc.resolve())
    markers, frames = trc["markers"], trc["frames"]

    print("=" * 70)
    print(f" {trc['path'].name}")
    print("=" * 70)
    for k, v in trc["header"].items():
        print(f"  {k:<20} = {v}")
    print(f"  마커 {len(markers)}개, 데이터 행 {len(frames)}개")
    print()

    # 축별 범위
    print(" 축별 범위 (전체 마커)")
    for ax in range(3):
        vs = [p[ax] for pts in frames for p in pts if not math.isnan(p[ax])]
        if vs:
            print(f"   {'XYZ'[ax]}: {min(vs):+.3f} ~ {max(vs):+.3f}   (폭 {max(vs)-min(vs):.3f} m)")
    print()

    up_ax, up_detail = guess_up_axis(trc)
    print(" 수직축 추정 (데이터 기반, 추측 아님)")
    print(f"   {up_detail}")
    print()

    # 마커별 평균 위치 + 결측률
    print(f" {'마커':<12} {'X':>8} {'Y':>8} {'Z':>8}   결측")
    for k, n in enumerate(markers):
        xs = [pts[k][0] for pts in frames]
        ys = [pts[k][1] for pts in frames]
        zs = [pts[k][2] for pts in frames]
        valid = [i for i in range(len(frames)) if not math.isnan(xs[i])]
        nan_pct = 100.0 * (len(frames) - len(valid)) / len(frames) if frames else 0
        if valid:
            mx = sum(xs[i] for i in valid) / len(valid)
            my = sum(ys[i] for i in valid) / len(valid)
            mz = sum(zs[i] for i in valid) / len(valid)
            print(f" {n:<12} {mx:+8.3f} {my:+8.3f} {mz:+8.3f}   {nan_pct:5.1f}%")
        else:
            print(f" {n:<12} {'-':>8} {'-':>8} {'-':>8}   {nan_pct:5.1f}%")

    if args.bones:
        print()
        print(" 뼈 길이 통계  (설계 검증항목 ③: 표준편차 5mm 이내가 목표)")
        print(f" {'뼈':<24} {'평균(mm)':>10} {'표준편차(mm)':>13} {'판정':>6}")
        idx = {n: i for i, n in enumerate(markers)}
        worst = 0.0
        for a, b in BONES:
            if a not in idx or b not in idx:
                continue
            ia, ib = idx[a], idx[b]
            lens = []
            for pts in frames:
                pa, pb = pts[ia], pts[ib]
                if any(math.isnan(c) for c in pa + pb):
                    continue
                lens.append(math.dist(pa, pb))
            if len(lens) < 2:
                continue
            mean = statistics.mean(lens) * 1000
            sd = statistics.stdev(lens) * 1000
            worst = max(worst, sd)
            verdict = "OK" if sd <= 5.0 else "높음"
            print(f" {a + '-' + b:<24} {mean:10.1f} {sd:13.2f} {verdict:>6}")
        print()
        print(f" 최대 표준편차 = {worst:.2f} mm  "
              f"({'5mm 기준 통과' if worst <= 5.0 else '5mm 기준 초과'})")
        print(" * 데모 데이터 기준입니다. 우리 폰 데이터에도 같은 잣대를 씁니다.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
