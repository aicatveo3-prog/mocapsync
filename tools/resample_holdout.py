"""
리샘플러를 **실제 영상의 관절 데이터**로 검증합니다 (hold-out).

★ 방법
짝수 프레임만 입력으로 주고, 리샘플러가 **홀수 프레임의 시각**에서 값을 만들게 한 뒤,
실제로 검출된 홀수 프레임 값과 비교합니다. 정답(홀수 프레임)은 입력에 없으므로
보간이 실제 움직임을 얼마나 잘 따라가는지 잴 수 있습니다.

입력 간격이 33 ms(30fps 상당)가 되므로 실제 사용(16.7 ms)보다 **어려운 조건**입니다.
여기서 나온 오차는 실사용 오차의 위쪽 한계로 볼 수 있습니다.

주의: 정답인 홀수 프레임 자체에도 검출 잡음이 있습니다. 그래서 오차가 0 이 될 수는
없습니다. 보간 방법끼리의 **상대 비교**가 핵심입니다.

사용:
    python tools/resample_holdout.py <pose/camNN_json> <사이드카.json>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from mocapsync import resample as R  # noqa: E402
from mocapsync import sidecar as sc  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HALPE = ["Nose", "LEye", "REye", "LEar", "REar", "LShoulder", "RShoulder",
         "LElbow", "RElbow", "LWrist", "RWrist", "LHip", "RHip", "LKnee", "RKnee",
         "LAnkle", "RAnkle", "Head", "Neck", "Hip", "LBigToe", "RBigToe",
         "LSmallToe", "RSmallToe", "LHeel", "RHeel"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("json_dir")
    ap.add_argument("sidecar")
    ap.add_argument("--min-conf", type=float, default=R.DEFAULT_MIN_CONF)
    a = ap.parse_args()

    nums, kp = R.load_pose_dir(Path(a.json_dir))
    tr = R.attach_timestamps(Path(a.json_dir).name, nums, kp, sc.Sidecar.load(a.sidecar))

    even = np.arange(tr.t_ns.size) % 2 == 0
    odd = ~even
    inp = R.CameraTrack(name="even", t_ns=tr.t_ns[even], kp=tr.kp[even])
    grid = tr.t_ns[odd]
    truth = tr.kp[odd]

    # 격자는 입력 구간 안쪽만 (외삽 없음)
    inside = (grid > inp.t_ns[0]) & (grid < inp.t_ns[-1])
    grid, truth = grid[inside], truth[inside]

    print(f"프레임 {tr.t_ns.size}개 -> 입력(짝수) {inp.t_ns.size}개, 정답(홀수) {grid.size}개")
    print(f"입력 간격 {np.median(np.diff(inp.t_ns)) / 1e6:.2f} ms  (실사용 16.67 ms 의 2배 — 더 어려운 조건)")
    print()

    # kp 는 (N, S, K, 3) — 사람 슬롯별. 모든 슬롯을 슬롯끼리 비교합니다
    # (리샘플러도 슬롯별로 잇습니다). 정답과 결과가 둘 다 있는 점만 셉니다.
    n_slots = tr.kp.shape[1]
    per_slot = np.sum(np.isfinite(tr.kp[..., 2]) & (tr.kp[..., 2] >= a.min_conf), axis=(0, 2))
    print(f"사람 슬롯 {n_slots}개, 슬롯별 유효 관절 수: {per_slot.tolist()}")
    print()

    results = {}
    per_kp = {}
    for method in R.METHODS:
        out, _ = R.resample_track(inp, grid, min_conf=a.min_conf, method=method)
        ok = (np.isfinite(out[..., 0]) & np.isfinite(truth[..., 0])
              & (truth[..., 2] >= a.min_conf))                  # (G, S, K)
        d = np.hypot(out[..., 0] - truth[..., 0], out[..., 1] - truth[..., 1])
        results[method] = d[ok]
        per_kp[method] = [d[..., k][ok[..., k]] for k in range(d.shape[2])]

    print(f"{'방법':<10}{'비교 수':>8}{'중앙값':>9}{'평균':>9}{'90%':>9}{'99%':>9}   (px)")
    for m, e in results.items():
        print(f"{m:<10}{e.size:>8}{np.median(e):>9.2f}{np.mean(e):>9.2f}"
              f"{np.percentile(e, 90):>9.2f}{np.percentile(e, 99):>9.2f}")
    print()

    # 움직임이 큰 관절에서 차이가 커집니다. 손목/발목을 따로 봅니다.
    print("움직임이 큰 관절 (평균 px)")
    print(f"  {'':<8}" + "".join(f"{m:>9}" for m in R.METHODS))
    for name in ("LWrist", "RWrist", "LElbow", "RElbow", "LAnkle", "RAnkle"):
        k = HALPE.index(name)
        vals = [np.mean(per_kp[m][k]) if per_kp[m][k].size else float("nan")
                for m in R.METHODS]
        print(f"  {name:<8}" + "".join(f"{v:>9.2f}" for v in vals))

    best = min(R.METHODS, key=lambda m: np.mean(results[m]))
    n, b = np.mean(results["nearest"]), np.mean(results[best])
    print()
    print(f"판정: 평균 오차가 가장 작은 방법은 '{best}' ({b:.2f} px). "
          f"보간 없음({n:.2f} px) 대비 {n / b:.1f}배.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
