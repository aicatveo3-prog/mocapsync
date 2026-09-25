"""
Pose2Sim 이 만든 2D 키포인트 JSON 을 검사합니다.

★ 왜 필요한가
"자세 추정이 돌았다"와 "쓸 만한 키포인트가 나왔다"는 다릅니다.
JSON 이 프레임마다 생겼어도 사람을 못 찾은 프레임이 섞여 있을 수 있고,
특정 관절만 신뢰도가 낮을 수도 있습니다. 오버레이 영상을 눈으로 훑어도
놓치기 쉬우므로 숫자로 봐야 합니다.

★ JSON 구조의 함정 (실측으로 알게 됨)
`people` 배열에는 추적기가 비워 둔 슬롯이 섞여 있고, 그 슬롯의 값은 전부
`NaN` 입니다. 앞에서부터 people[0] 을 집으면 빈 슬롯을 읽어 통계가 전부
NaN 이 됩니다. 유효 키포인트가 가장 많은 사람을 골라야 합니다.

★ 화면 밖 키포인트
모델은 가려지거나 프레임을 벗어난 관절의 위치를 **추정해서** 내보냅니다.
그래서 좌표가 음수이거나 해상도를 넘을 수 있습니다. 신뢰도는 높게 나올 수도
있습니다. 이건 "머리가 프레임에서 잘렸다" 같은 촬영 문제의 신호이므로
따로 세어서 보고합니다.

사용:
    python tools/pose_json_report.py <pose/camNN_json> --width 1920 --height 1080
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

#: HALPE_26 키포인트 이름 (Pose2Sim 'Body_with_feet')
HALPE_26 = [
    "Nose", "LEye", "REye", "LEar", "REar",
    "LShoulder", "RShoulder", "LElbow", "RElbow", "LWrist", "RWrist",
    "LHip", "RHip", "LKnee", "RKnee", "LAnkle", "RAnkle",
    "Head", "Neck", "Hip",
    "LBigToe", "RBigToe", "LSmallToe", "RSmallToe", "LHeel", "RHeel",
]

#: 3D 복원에서 가장 중요한 관절. 여기가 약하면 결과를 믿을 수 없습니다.
CORE = ["LShoulder", "RShoulder", "LElbow", "RElbow", "LWrist", "RWrist",
        "LHip", "RHip", "LKnee", "RKnee", "LAnkle", "RAnkle"]


def triples(kp: list) -> list[tuple[float, float, float]]:
    out = []
    for i in range(len(kp) // 3):
        try:
            out.append((float(kp[i * 3]), float(kp[i * 3 + 1]), float(kp[i * 3 + 2])))
        except (TypeError, ValueError):
            out.append((math.nan, math.nan, math.nan))
    return out


def valid_count(kp: list) -> int:
    return sum(1 for _, _, c in triples(kp) if not math.isnan(c) and c > 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("json_dir")
    ap.add_argument("--low", type=float, default=0.3,
                    help="이 신뢰도 미만을 '검출 실패'로 봅니다")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    a = ap.parse_args()

    d = Path(a.json_dir)
    files = sorted(d.glob("*.json"))
    if not files:
        print(f"JSON 이 없습니다: {d}")
        return 1

    n_frames = len(files)
    n_no_person = 0
    n_real_multi = 0
    slot_counts: list[int] = []

    per_kp: list[list[float]] = [[] for _ in range(len(HALPE_26))]
    per_kp_outside: list[int] = [0] * len(HALPE_26)
    n_kp_seen = 0

    for f in files:
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"★ 읽기 실패 {f.name}: {e}")
            continue

        people = obj.get("people") or []
        slot_counts.append(len(people))

        # ★ NaN 슬롯을 걸러내고, 유효 키포인트가 가장 많은 사람을 고릅니다.
        scored = [(valid_count(p.get("pose_keypoints_2d") or []), p) for p in people]
        scored = [(n, p) for n, p in scored if n > 0]
        if not scored:
            n_no_person += 1
            continue
        if len(scored) > 1:
            n_real_multi += 1
        scored.sort(key=lambda t: -t[0])
        kp = scored[0][1].get("pose_keypoints_2d") or []

        ts = triples(kp)
        n_kp_seen = max(n_kp_seen, len(ts))
        for i, (x, y, c) in enumerate(ts):
            if i >= len(per_kp):
                break
            if math.isnan(c):
                continue
            per_kp[i].append(c)
            if x < 0 or y < 0 or x > a.width or y > a.height:
                per_kp_outside[i] += 1

    print(f"프레임 수              {n_frames}")
    print(f"사람 미검출 프레임     {n_no_person}  ({n_no_person / n_frames * 100:.2f}%)")
    print(f"실제 2명 이상 검출     {n_real_multi}  "
          f"({n_real_multi / n_frames * 100:.2f}%)")
    if slot_counts:
        print(f"people 슬롯 수         평균 {statistics.mean(slot_counts):.1f} "
              f"(빈 NaN 슬롯 포함)")
    print(f"키포인트 개수          {n_kp_seen}"
          + ("  (HALPE_26)" if n_kp_seen == 26 else "  ★ 26이 아닙니다"))
    print()

    all_conf = [c for lst in per_kp for c in lst]
    if not all_conf:
        print("★ 유효 키포인트가 하나도 없습니다.")
        return 2

    print(f"전체 평균 신뢰도       {statistics.mean(all_conf):.3f}")
    print(f"전체 중앙값            {statistics.median(all_conf):.3f}")
    print(f"신뢰도 {a.low} 미만 비율    "
          f"{sum(1 for c in all_conf if c < a.low) / len(all_conf) * 100:.2f}%")
    print()
    print(f"{'관절':<12} {'평균':>6} {'중앙':>6} {'약함%':>7} {'화면밖%':>8}")
    print("-" * 46)

    rows = []
    for i, name in enumerate(HALPE_26):
        lst = per_kp[i]
        if not lst:
            rows.append((name, 0.0, 0.0, 100.0, 0.0))
            continue
        low = sum(1 for c in lst if c < a.low) / len(lst) * 100
        out = per_kp_outside[i] / len(lst) * 100
        rows.append((name, statistics.mean(lst), statistics.median(lst), low, out))

    for name, mean, med, low, out in rows:
        flags = ""
        if low > 10:
            flags += " ★약함"
        if out > 20:
            flags += " ★화면밖"
        print(f"{name:<12} {mean:6.3f} {med:6.3f} {low:6.1f}% {out:7.1f}%{flags}")

    print()
    weak = [r[0] for r in rows if r[3] > 10]
    outside = [r[0] for r in rows if r[4] > 20]
    if weak:
        print(f"★ 신뢰도 낮은 관절: {', '.join(weak)}")
    if outside:
        print(f"★ 프레임을 벗어난 관절: {', '.join(outside)}")
        print("   -> 피험자가 화면에 다 들어오지 않았습니다. 뒤로 물러나거나 "
              "카메라를 조정해 머리부터 발끝까지 담으세요.")

    print()
    core_weak = [r[0] for r in rows if r[0] in CORE and r[3] > 10]
    if n_no_person / n_frames > 0.05:
        print(f"판정: ★ 사람 미검출이 {n_no_person / n_frames * 100:.1f}% 입니다. "
              "촬영을 다시 하는 것이 좋습니다.")
    elif core_weak:
        print(f"판정: ★ 몸통·팔다리 주요 관절이 약합니다: {', '.join(core_weak)}")
    else:
        print("판정: 몸통·팔다리 주요 관절이 모두 안정적입니다. "
              "이 영상은 3D 복원 입력으로 쓸 수 있습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
