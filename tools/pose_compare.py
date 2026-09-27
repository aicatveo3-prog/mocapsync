"""
두 번의 2D 자세 추정 결과가 같은지 비교합니다.

★ 왜 필요한가
CPU(OpenVINO)와 GPU(ONNXRuntime CUDA)는 같은 모델이라도 계산 경로가 다릅니다.
부동소수 연산 순서, 커널 구현, 정밀도가 달라서 결과가 미세하게 다를 수 있습니다.
"GPU 로 바꿨더니 빨라졌다"만 보고 넘어가면, 관절 위치가 조용히 달라졌는지 모릅니다.
속도를 올린 대가로 정확도를 잃었는지 **숫자로** 확인해야 합니다.

비교 방법
  프레임마다 "유효 키포인트가 가장 많은 사람"을 양쪽에서 골라
  같은 관절끼리 픽셀 거리를 잽니다. 신뢰도 차이도 봅니다.

사용:
    python tools/pose_compare.py <A/pose/camNN_json> <B/pose/camNN_json>
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

from pose_json_report import HALPE_26, triples, valid_count


def best_person(path: Path):
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    scored = [(valid_count(p.get("pose_keypoints_2d") or []), p)
              for p in obj.get("people") or []]
    scored = [t for t in scored if t[0] > 0]
    if not scored:
        return None
    scored.sort(key=lambda t: -t[0])
    return triples(scored[0][1].get("pose_keypoints_2d") or [])


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--min-conf", type=float, default=0.3,
                    help="양쪽 모두 이 신뢰도 이상인 관절만 거리 비교")
    a = ap.parse_args()

    fa = {p.name: p for p in Path(a.a).glob("*.json")}
    fb = {p.name: p for p in Path(a.b).glob("*.json")}
    common = sorted(set(fa) & set(fb))
    print(f"A {len(fa)}개 / B {len(fb)}개 / 공통 {len(common)}개")
    if not common:
        return 1

    both = only_a = only_b = neither = 0
    dists: list[float] = []
    per_kp: list[list[float]] = [[] for _ in HALPE_26]
    conf_diff: list[float] = []

    for name in common:
        pa, pb = best_person(fa[name]), best_person(fb[name])
        if pa and pb:
            both += 1
        elif pa:
            only_a += 1
            continue
        elif pb:
            only_b += 1
            continue
        else:
            neither += 1
            continue
        for i, ((xa, ya, ca), (xb, yb, cb)) in enumerate(zip(pa, pb)):
            if any(math.isnan(v) for v in (xa, ya, ca, xb, yb, cb)):
                continue
            conf_diff.append(abs(ca - cb))
            if ca < a.min_conf or cb < a.min_conf:
                continue
            d = math.hypot(xa - xb, ya - yb)
            dists.append(d)
            if i < len(per_kp):
                per_kp[i].append(d)

    n = len(common)
    print()
    print("검출 일치")
    print(f"  둘 다 검출   {both}  ({both / n * 100:.1f}%)")
    print(f"  A 만 검출    {only_a}")
    print(f"  B 만 검출    {only_b}")
    print(f"  둘 다 없음   {neither}")

    if not dists:
        print("비교할 관절이 없습니다.")
        return 2

    print()
    print(f"관절 위치 차이 (픽셀, 1920x1080 기준, 비교 {len(dists)}개)")
    print(f"  중앙값   {statistics.median(dists):.3f}")
    print(f"  평균     {statistics.mean(dists):.3f}")
    print(f"  95%      {pct(dists, 95):.3f}")
    print(f"  99%      {pct(dists, 99):.3f}")
    print(f"  최대     {max(dists):.3f}")
    print(f"신뢰도 차이  중앙값 {statistics.median(conf_diff):.4f}  "
          f"최대 {max(conf_diff):.4f}")

    print()
    print("관절별 차이 중앙값 / 95% (픽셀)")
    for name, lst in zip(HALPE_26, per_kp):
        if lst:
            print(f"  {name:<12} {statistics.median(lst):7.3f}  {pct(lst, 95):7.3f}")

    # 판정 기준
    # 1920x1080 에서 사람 키가 약 900px 이면 1px ≈ 2mm. 데모 파이프라인 잡음이
    # 약 20mm(≈10px) 였으므로 중앙값 1px 이하면 결과에 영향이 없다고 봅니다.
    med = statistics.median(dists)
    p95 = pct(dists, 95)
    agree = both / n
    print()
    if med <= 1.0 and p95 <= 3.0 and agree >= 0.99:
        print(f"판정: 두 결과가 사실상 같습니다 (중앙값 {med:.2f}px, 95% {p95:.2f}px). "
              "GPU 로 바꿔도 정확도 손실이 없습니다.")
    elif med <= 3.0:
        print(f"판정: 차이가 작지만 있습니다 (중앙값 {med:.2f}px, 95% {p95:.2f}px). "
              "파이프라인 잡음(약 10px)보다 작아 실용상 문제없습니다.")
    else:
        print(f"판정: ★ 차이가 큽니다 (중앙값 {med:.2f}px). 원인을 찾아야 합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
