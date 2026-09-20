#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
영상에서 특정 프레임들을 뽑아 PNG 로 저장합니다.

용도: 3D 복원 결과를 의심할 때, 원본 2D 영상을 근거로 삼기 위해서.
Pose2Sim 이 만든 pose/camNN_pose.mp4 (키포인트가 얹힌 영상) 에 쓰면
"3D 가 이상한 건가, 원래 동작이 그런 건가"를 바로 가릴 수 있습니다.

사용법
------
  python tools/grab_frames.py <영상> --frames 1,24,48,72,96 --out <출력폴더>
  python tools/grab_frames.py <영상> --grid 1,24,48,72 --out <출력폴더>   # 가로로 붙임
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2  # opencv-python (pose2sim 의존성에 포함)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description="영상에서 프레임 추출")
    ap.add_argument("video", type=Path)
    ap.add_argument("--frames", default="", help="쉼표로 구분한 1-based 프레임 번호")
    ap.add_argument("--grid", default="", help="지정 프레임들을 가로로 이어붙여 한 장으로 저장")
    ap.add_argument("--out", type=Path, required=True, help="출력 폴더")
    ap.add_argument("--scale", type=float, default=1.0, help="저장 시 배율")
    args = ap.parse_args()

    cap = cv2.VideoCapture(str(args.video))
    if not cap.isOpened():
        print(f"영상을 열 수 없습니다: {args.video}")
        return 1

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"{args.video.name}: {w}x{h}, {fps:.3f} fps, 총 {total} 프레임 "
          f"({total / fps if fps else 0:.2f} s)")

    args.out.mkdir(parents=True, exist_ok=True)
    want = args.grid or args.frames
    nums = [int(x) for x in want.split(",") if x.strip()]
    if not nums:
        print("--frames 또는 --grid 를 주세요.")
        return 2

    grabbed = []
    for n in nums:
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, n - 1))
        ok, img = cap.read()
        if not ok:
            print(f"  프레임 {n}: 읽기 실패")
            continue
        if args.scale != 1.0:
            img = cv2.resize(img, None, fx=args.scale, fy=args.scale,
                             interpolation=cv2.INTER_AREA)
        # 프레임 번호를 이미지에 새깁니다 (나중에 헷갈리지 않게)
        cv2.putText(img, f"f{n}  t={(n - 1) / fps:.3f}s", (12, 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(img, f"f{n}  t={(n - 1) / fps:.3f}s", (12, 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2, cv2.LINE_AA)
        grabbed.append((n, img))

    cap.release()

    if args.grid:
        if not grabbed:
            return 3
        hmin = min(im.shape[0] for _, im in grabbed)
        tiles = []
        for _, im in grabbed:
            if im.shape[0] != hmin:
                r = hmin / im.shape[0]
                im = cv2.resize(im, (int(im.shape[1] * r), hmin),
                                interpolation=cv2.INTER_AREA)
            tiles.append(im)
        strip = cv2.hconcat(tiles)
        p = args.out / f"{args.video.stem}_grid.png"
        cv2.imwrite(str(p), strip)
        print(f"  저장: {p}  ({strip.shape[1]}x{strip.shape[0]})")
    else:
        for n, im in grabbed:
            p = args.out / f"{args.video.stem}_f{n:04d}.png"
            cv2.imwrite(str(p), im)
            print(f"  저장: {p}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
