"""
촬영 목록과 촬영 한 건의 요약 — 대시보드가 보여줄 숫자를 만듭니다.

마스터 콘솔은 같은 내용을 글자로 찍습니다(master._session_report). 여기서는 화면에
그리기 좋게 **구조화된 dict** 로 만듭니다. Pose2Sim 을 import 하지 않습니다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .sidecar import NS_PER_MS, NS_PER_S, Sidecar, check_session

SAFE_SID = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")

#: 사이드카 cameraDeviceType → 사람이 읽는 이름
LENS_NAMES = {
    "AVCaptureDeviceTypeBuiltInUltraWideCamera": "초광각 0.5x",
    "AVCaptureDeviceTypeBuiltInWideAngleCamera": "광각 1x",
}


def is_safe_sid(sid: str) -> bool:
    """경로 탈출을 막습니다. 세션 이름은 폴더 이름으로만 씁니다."""
    return bool(SAFE_SID.match(sid or "")) and sid not in (".", "..")


def _issue(i, where: str) -> dict:
    return {"severity": i.severity, "code": i.code, "message": i.message, "where": where}


def summarize_session(upload_dir: Path) -> dict:
    """
    uploads/<세션>/ 한 건의 요약.

    cameras  : 폰마다 프레임 수, 길이, 시계 오차 상한, ISO, 렌즈, 사용 가능 여부, 문제들
    spreadMs : 두 폰의 첫 프레임 시각 차이 (공통 시계). 16.667 ms 안이면 같은 프레임을 잡은 것
    issues   : 여러 대를 같이 놓았을 때만 보이는 문제 (check_session)
    """
    d = Path(upload_dir)
    cams, loaded = [], []
    for f in sorted(d.glob("*.json")):
        try:
            sc = Sidecar.load(f)
        except Exception as e:  # noqa: BLE001 — 깨진 파일 하나로 화면이 죽으면 안 됩니다
            cams.append({"file": f.name, "error": f"읽을 수 없습니다: {e}", "usable": False,
                         "issues": []})
            continue
        loaded.append(sc)
        t = sc.timestamps_master_ns
        st = sc.interval_stats()
        cams.append({
            "file": f.name,
            "deviceId": sc.device_id,
            "name": sc.device_name,
            "frames": sc.frame_count,
            "durationS": round(sc.duration_ns / NS_PER_S, 2),
            "fps": round(float(st.get("estimated_fps", 0.0)), 2),
            "uncertaintyMs": round(sc.clock_uncertainty_ns / NS_PER_MS, 3),
            "iso": round(sc.iso),
            "lens": LENS_NAMES.get(sc.camera_device_type, sc.camera_device_type or "?"),
            "fovDeg": round(sc.field_of_view_deg, 1),
            "usable": sc.is_usable,
            "firstMasterNs": t[0] if t else None,
            "issues": [_issue(i, sc.device_id[:8]) for i in sc.validate()],
        })

    firsts = [c["firstMasterNs"] for c in cams if c.get("firstMasterNs") is not None]
    spread = (max(firsts) - min(firsts)) / NS_PER_MS if len(firsts) >= 2 else None
    issues = [_issue(i, "세션") for i in check_session(loaded)] if loaded else []
    videos = [p for p in d.iterdir() if p.suffix.lower() in (".mov", ".mp4", ".m4v")] \
        if d.is_dir() else []
    return {
        "sid": d.name,
        "cameras": cams,
        "videos": len(videos),
        "spreadMs": round(spread, 3) if spread is not None else None,
        "sameFrame": spread is not None and spread < 1000 / 60,
        "usable": bool(cams) and all(c.get("usable") for c in cams),
        "issues": issues,
    }


def read_run_result(project: Path) -> dict | None:
    """tools/run_session.py 가 남긴 mocapsync_run.json 에서 화면에 필요한 것만."""
    p = Path(project) / "mocapsync_run.json"
    if not p.is_file():
        return None
    try:
        r = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"verdict": "결과 파일을 읽을 수 없습니다", "exitCode": None}
    s = r.get("summary") or {}
    trcs = sorted((Path(project) / "pose-3d").glob("*_filt*.trc"))
    return {
        "verdict": r.get("verdict"),
        "exitCode": r.get("exit_code"),
        "reprojPx": s.get("reproj_px"),
        "reprojMm": s.get("reproj_mm"),
        "excludedPct": s.get("excluded_pct"),
        "notes": r.get("notes") or [],
        "trc": trcs[0].name if trcs else None,
        "started": r.get("started"),
    }


class SessionIndex:
    """
    uploads/ 아래 촬영 목록. 폴더 내용이 그대로면 요약을 다시 계산하지 않습니다
    (대시보드가 몇 초마다 부르므로).
    """

    def __init__(self, upload_root: Path, work_root: Path):
        self.upload_root = Path(upload_root)
        self.work_root = Path(work_root)
        self._cache: dict[str, tuple[tuple, dict]] = {}

    def _key(self, d: Path) -> tuple:
        return tuple(sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns)
                            for p in d.iterdir() if p.is_file()))

    def summary(self, sid: str) -> dict | None:
        if not is_safe_sid(sid):
            return None
        d = self.upload_root / sid
        if not d.is_dir():
            return None
        key = self._key(d)
        hit = self._cache.get(sid)
        if hit and hit[0] == key:
            return hit[1]
        s = summarize_session(d)
        s["partial"] = any(n.endswith(".part") for n, _, _ in key)
        s["sizeMB"] = round(sum(sz for _, sz, _ in key) / 1_048_576, 1)
        self._cache[sid] = (key, s)
        return s

    def project_dir(self, sid: str) -> Path:
        return self.work_root / "sessions" / sid

    def list(self, limit: int = 30) -> list[dict]:
        if not self.upload_root.is_dir():
            return []
        dirs = [d for d in self.upload_root.iterdir() if d.is_dir() and is_safe_sid(d.name)]
        dirs.sort(key=lambda d: d.name, reverse=True)
        out = []
        for d in dirs[:limit]:
            s = self.summary(d.name)
            if s is None or not s["cameras"]:
                continue
            item = dict(s)
            item["result"] = read_run_result(self.project_dir(d.name))
            out.append(item)
        return out
