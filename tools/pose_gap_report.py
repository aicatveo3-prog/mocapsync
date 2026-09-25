"""
사람이 검출되지 않은 프레임이 **어디에** 있는지 봅니다.

★ 왜 이 구분이 중요한가

미검출 비율만 보면 원인을 알 수 없습니다.
  · 앞뒤에 연속으로 뭉쳐 있으면  -> 피험자가 아직 안 들어왔거나 이미 나간 것.
    정상이고, frame_range 로 잘라내면 됩니다.
  · 중간에 흩어져 있으면        -> 검출 실패. 조명·모션블러·가림을 봐야 합니다.
    이건 촬영을 다시 해야 하는 신호입니다.

같은 31% 라도 앞의 경우는 문제가 아니고 뒤의 경우는 심각합니다.

사용:
    python tools/pose_gap_report.py <pose/camNN_json> --fps 60
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def has_person(path: Path) -> bool:
    """NaN 슬롯을 걸러내고 실제 사람이 있는지 봅니다."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    for p in obj.get("people") or []:
        kp = p.get("pose_keypoints_2d") or []
        for i in range(len(kp) // 3):
            try:
                c = float(kp[i * 3 + 2])
            except (TypeError, ValueError):
                continue
            if not math.isnan(c) and c > 0:
                return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("json_dir")
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--min-run", type=int, default=5,
                    help="이보다 짧은 검출 구간은 요약에서 생략")
    a = ap.parse_args()

    files = sorted(Path(a.json_dir).glob("*.json"))
    if not files:
        print(f"JSON 이 없습니다: {a.json_dir}")
        return 1

    flags = [has_person(f) for f in files]
    n = len(flags)

    runs = []
    cur = flags[0]
    start = 0
    for i in range(1, n):
        if flags[i] != cur:
            runs.append((cur, start, i - 1))
            cur = flags[i]
            start = i
    runs.append((cur, start, n - 1))

    print(f"전체 {n}프레임 ({n / a.fps:.2f}초)")
    print()
    print("구간:")
    for v, s, e in runs:
        length = e - s + 1
        if v and length < a.min_run:
            continue
        label = "검출  " if v else "미검출"
        print(f"  {label} {s:5d}~{e:5d}  {length:5d}프레임  "
              f"{length / a.fps:6.2f}초   (영상 {s / a.fps:5.1f}s 부터)")

    miss_runs = [(s, e) for v, s, e in runs if not v]
    n_miss = sum(1 for f in flags if not f)
    print()
    print(f"총 미검출 {n_miss}프레임 ({n_miss / n * 100:.1f}%), 구간 {len(miss_runs)}개")

    if not miss_runs:
        print("판정: 모든 프레임에서 사람이 검출됐습니다.")
        return 0

    # ★ 판정 기준: "가장 긴 연속 검출 구간"을 쓸 수 있는 구간으로 봅니다.
    #
    #   처음에는 미검출 구간이 0번이나 마지막에 **닿는지**로 앞뒤/중간을 나눴습니다.
    #   그런데 피험자가 들어오는 동안에는 검출이 깜빡깜빡합니다 —
    #   미검출/검출/미검출이 번갈아 나오죠. 그 구간들은 0번에 닿지 않으므로
    #   전부 "중간"으로 분류되어, 실제로는 정상인 촬영이 "검출 실패"로 판정됐습니다.
    #   (2026-09-25 실측: 앞 2.5초가 8개 구간으로 쪼개져 중간 10.6% 로 오판)
    #
    #   피험자가 실제로 찍힌 구간은 "가장 긴 연속 검출 구간" 하나입니다.
    #   그 안의 공백만이 진짜 검출 실패입니다.
    detected_runs = [(s, e) for v, s, e in runs if v]
    if not detected_runs:
        print("판정: ★ 사람이 한 프레임도 검출되지 않았습니다.")
        return 3

    best_s, best_e = max(detected_runs, key=lambda t: t[1] - t[0])
    best_len = best_e - best_s + 1

    before = best_s
    after = n - 1 - best_e
    print(f"  가장 긴 연속 검출   프레임 {best_s}~{best_e}  "
          f"{best_len}프레임 ({best_len / a.fps:.2f}초)")
    print(f"  그 앞              {before}프레임 ({before / a.fps:.2f}초)  "
          "<- 피험자가 들어오는 중")
    print(f"  그 뒤              {after}프레임 ({after / a.fps:.2f}초)  "
          "<- 피험자가 나간 뒤")
    print()

    # 쓸 구간 안의 공백만 진짜 실패입니다
    inner_gaps = [(s, e) for s, e in miss_runs if s >= best_s and e <= best_e]
    inner = sum(e - s + 1 for s, e in inner_gaps)

    if best_len / a.fps < 5:
        print(f"판정: ★ 연속 검출 구간이 {best_len / a.fps:.2f}초뿐입니다. "
              "촬영 규칙은 5초 이상입니다.")
    elif inner == 0:
        print(f"판정: 쓸 수 있는 구간 안에 공백이 **하나도 없습니다**. "
              f"{best_len / a.fps:.2f}초 연속 검출.")
        print(f"      Config.toml 에서  frame_range = [{best_s}, {best_e}]")
    elif inner / best_len < 0.02:
        print(f"판정: 쓸 구간 안 공백이 {inner / best_len * 100:.1f}% 로 적습니다. "
              "보간으로 메울 수 있습니다.")
        print(f"      frame_range = [{best_s}, {best_e}]")
    else:
        print(f"판정: ★ 쓸 구간 안 공백이 {inner / best_len * 100:.1f}% 입니다. 검출 실패입니다.")
        print("      조명 부족, 모션블러, 가림을 확인하세요.")
        longest = max(inner_gaps, key=lambda t: t[1] - t[0])
        print(f"      가장 긴 공백: 프레임 {longest[0]}~{longest[1]} "
              f"({(longest[1] - longest[0] + 1) / a.fps:.2f}초, 영상 {longest[0] / a.fps:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
