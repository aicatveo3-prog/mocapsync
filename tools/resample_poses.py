"""
Pose2Sim 프로젝트의 2D 관절을 공통 시간축으로 리샘플해 pose-sync/ 에 씁니다.

이걸 돌린 뒤에는 Pose2Sim.synchronization() 을 **건너뛰고**
personAssociation() -> triangulation() -> filtering() -> kinematics() 로 갑니다.
(personAssociation / triangulation 은 pose-sync/ 가 있으면 그걸 읽습니다)

사용:
    python tools/resample_poses.py <프로젝트 폴더> --map cam01=<사이드카.json> --map cam02=<...>

    --map 의 왼쪽은 pose/ 안의 폴더 이름에서 _json 을 뺀 것입니다 (cam01_json -> cam01).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from mocapsync import resample as R  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description="키포인트 리샘플러")
    ap.add_argument("project", help="Pose2Sim 프로젝트 폴더 (pose/ 가 있는 곳)")
    ap.add_argument("--map", action="append", required=True, metavar="camNN=사이드카.json")
    ap.add_argument("--fps", type=float, default=R.DEFAULT_FPS)
    ap.add_argument("--method", choices=R.METHODS, default=R.DEFAULT_METHOD)
    ap.add_argument("--max-gap-ms", type=float, default=R.DEFAULT_MAX_GAP_NS / 1e6)
    ap.add_argument("--min-conf", type=float, default=R.DEFAULT_MIN_CONF)
    a = ap.parse_args()

    proj = Path(a.project)
    pairs = []
    for m in a.map:
        cam, _, side = m.partition("=")
        jd = proj / "pose" / f"{cam}_json"
        if not jd.is_dir():
            print(f"★ 폴더가 없습니다: {jd}")
            return 2
        pairs.append((jd, Path(side)))

    out = proj / "pose-sync"
    try:
        res = R.resample_session(pairs, out, fps=a.fps, method=a.method,
                                 max_gap_ns=int(a.max_gap_ms * 1e6), min_conf=a.min_conf)
    except R.ResampleError as e:
        print(f"★ 리샘플 중단: {e}")
        return 3

    rep = res.report()
    print(f"공통 격자 {rep['gridPoints']}점  {rep['durationS']:.2f}초  @{a.fps:g}Hz  ({a.method})")
    for c in rep["cameras"]:
        print(f"  {c['name']:<12} 사람 슬롯 {c['slots']}개  "
              f"사람 있는 격자 {c['person_ratio'] * 100:5.1f}%  "
              f"주 피험자 관절 채움 {c['main_filled_ratio'] * 100:5.1f}%")
        print(f"  {'':<12} 가장 가까운 프레임까지 평균 {c['shift_ms_mean']:.2f} / "
              f"최대 {c['shift_ms_max']:.2f} ms  "
              f"이은 최대 공백 {c['max_bridged_gap_ms']:.1f} ms  "
              f"튀어서 끊음 {c['jump_cuts']}회")
        for w in c["warnings"]:
            print(f"    [경고] {w}")
    print(f"저장: {out}")
    print("다음: Pose2Sim.personAssociation() -> triangulation() -> filtering()  "
          "(synchronization 은 건너뜁니다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
