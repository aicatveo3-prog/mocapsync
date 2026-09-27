"""
4단계 파이프라인 — 업로드된 촬영 한 건을 Pose2Sim 3D 결과까지 가져가는 준비물들.

이 모듈은 **Pose2Sim 을 import 하지 않습니다.** 파일을 찾고, 검사하고, 프로젝트
폴더를 만들고, 결과 로그를 읽는 일만 합니다. 그래서 리포 .venv 에서 단위 테스트가
됩니다. 실제로 Pose2Sim 을 부르는 것은 tools/run_session.py 입니다.

흐름
----
  uploads/<세션>/<기기ID>.mov + <기기ID>.json      (마스터가 받은 그대로)
     │  find_uploads()       영상·사이드카 짝 찾기
     │  load_rig()           캘리브레이션 + "어느 폰이 cam01 인가" 표
     │  assign_cameras()     기기 → cam01, cam02 ...
     │  check_inputs()       사이드카 검증 + 세션 검증 + 캘리브레이션 대조
     ▼
  <작업폴더>/sessions/<세션>/                         (ASCII 경로 — OpenSim 제약)
     videos/cam01.mov  sidecars/cam01.json  calibration/Calib.toml  Config.toml
     │  (run_session.py) poseEstimation → 리샘플러 → personAssociation
     │                   → triangulation → filtering
     ▼
     pose-3d/*.trc     summarize_logs() 로 품질 요약

★ 조용히 틀리는 곳 두 군데를 여기서 막습니다
 1. **카메라 순서.** Pose2Sim 은 캘리브레이션 파일의 카메라와 pose 폴더를 이름이
    아니라 **순서로** 짝짓습니다 (triangulation.py: computeP(calib) 의 순서 ↔
    pose 폴더를 끝 숫자로 정렬한 순서). 폰 두 대가 뒤바뀌어도 오류 없이
    엉뚱한 3D 가 나옵니다. 그래서 캘리브레이션 옆에 cameras.json(기기 ↔ camNN)을
    두고, 캘리브레이션 파일 안의 카메라 순서가 cam01, cam02 ... 인지 확인합니다.
 2. **남은 옛 결과.** Pose2Sim 삼각측량은 pose-associated → pose-sync → pose 순으로
    있는 것을 읽습니다. 앞 단계가 실패했는데 옛 폴더가 남아 있으면 옛 결과로
    3D 를 만듭니다. 그래서 다시 돌릴 때 뒤 단계 폴더를 먼저 지웁니다.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .sidecar import Issue, Sidecar, check_session

VIDEO_EXTS = (".mov", ".mp4", ".m4v")

#: 작업 폴더 기본값. 리포 경로에 한글이 있어 홈 아래 ASCII 경로를 씁니다 (DESIGN §3.9).
DEFAULT_WORK_ROOT = Path.home() / "Pose2SimWork"

#: 캘리브레이션 해상도와 영상 해상도의 허용 차이 (각 변, 비율).
#  공식 데모도 영상 1080 폭에 캘리브레이션 1088 로 0.7% 다릅니다 (16의 배수 정렬).
SIZE_TOLERANCE = 0.02

#: 다시 돌릴 때 지우는 폴더 (뒤 단계 산출물). pose/ 는 오래 걸려서 남깁니다.
DOWNSTREAM_DIRS = ("pose-sync", "pose-associated", "pose-3d", "kinematics")

CAM_NAME = re.compile(r"^cam(\d+)$")


class PipelineError(Exception):
    """사람이 고쳐야 진행할 수 있는 입력 문제."""


def cam_name(i: int) -> str:
    """0 -> cam01"""
    return f"cam{i + 1:02d}"


def is_ascii_path(p: Path) -> bool:
    return str(p).isascii()


# ── 업로드 찾기 ───────────────────────────────────────────────────────────────

@dataclass
class Upload:
    device_id: str
    video: Path
    sidecar_path: Path
    sidecar: Sidecar


def find_uploads(session_dir: Path) -> list[Upload]:
    """
    uploads/<세션>/ 에서 <기기ID>.<영상> 과 <기기ID>.json 짝을 찾습니다.

    짝이 없는 파일은 오류입니다. 영상만 있으면 시각을 모르고, 사이드카만 있으면
    찍힌 게 없습니다. 올리다 끊긴 .part 파일도 오류로 알립니다.
    """
    d = Path(session_dir)
    if not d.is_dir():
        raise PipelineError(f"촬영 폴더가 없습니다: {d}")

    parts = sorted(p.name for p in d.glob("*.part"))
    if parts:
        raise PipelineError(
            f"업로드가 끝나지 않은 파일이 있습니다: {', '.join(parts)}. "
            "폰에서 다시 올리거나 업로드가 끝날 때까지 기다리세요.")

    videos = {p.stem: p for p in d.iterdir() if p.suffix.lower() in VIDEO_EXTS}
    sides = {p.stem: p for p in d.glob("*.json")}
    only_v = sorted(set(videos) - set(sides))
    only_s = sorted(set(sides) - set(videos))
    problems = []
    if only_v:
        problems.append(f"사이드카 없는 영상: {', '.join(only_v)}")
    if only_s:
        problems.append(f"영상 없는 사이드카: {', '.join(only_s)}")
    if problems:
        raise PipelineError("짝이 맞지 않습니다. " + " / ".join(problems))
    if not videos:
        raise PipelineError(f"영상이 없습니다: {d}")

    out = []
    for stem in sorted(videos):
        sc = Sidecar.load(sides[stem])
        # 파일 이름이 아니라 사이드카 안의 deviceId 를 믿습니다 (캘리브레이션 매칭 기준).
        out.append(Upload(device_id=sc.device_id or stem, video=videos[stem],
                          sidecar_path=sides[stem], sidecar=sc))
    return out


# ── 캘리브레이션 (리그) ───────────────────────────────────────────────────────

@dataclass
class CalibCamera:
    key: str                 # toml 섹션 이름
    name: str                # name 필드 (없으면 key)
    size: tuple[float, float]  # (폭, 높이) px


@dataclass
class Rig:
    """
    폰 배치 한 벌의 캘리브레이션.

    <rig>/ 안에 Pose2Sim 형식 캘리브레이션 .toml 한 개와 cameras.json
    ({"cam01": "<기기ID>", "cam02": "<기기ID>"}) 을 둡니다.
    폰을 옮기면 외부 파라미터가 바뀌므로 새 리그입니다.
    """

    dir: Path
    calib_path: Path
    cameras: list[CalibCamera]
    device_of: dict[str, str]           # camNN -> deviceId


def read_calib(path: Path) -> list[CalibCamera]:
    """Pose2Sim 과 같은 규칙으로 카메라 섹션을 **파일 순서대로** 읽습니다."""
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    cams = []
    for key, v in data.items():
        # Pose2Sim common.computeP 와 같은 제외 목록
        if key in ("metadata", "capture_volume", "charuco", "checkerboard"):
            continue
        if not isinstance(v, dict):
            continue
        for need in ("size", "matrix", "distortions", "rotation", "translation"):
            if need not in v:
                raise PipelineError(f"캘리브레이션 [{key}] 에 '{need}' 가 없습니다: {path}")
        w, h = (float(x) for x in v["size"][:2])
        cams.append(CalibCamera(key=key, name=str(v.get("name") or key), size=(w, h)))
    if not cams:
        raise PipelineError(f"캘리브레이션에 카메라가 없습니다: {path}")
    return cams


def load_rig(rig_dir: Path | None) -> Rig | None:
    """리그 폴더를 읽습니다. 폴더나 .toml 이 없으면 None (캘리브레이션 없음)."""
    if rig_dir is None:
        return None
    d = Path(rig_dir)
    tomls = sorted(d.glob("*.toml")) if d.is_dir() else []
    if not tomls:
        return None
    if len(tomls) > 1:
        # Pose2Sim 은 가장 최근에 만든 파일을 고릅니다 (st_ctime). 헷갈리니 하나만 허용합니다.
        raise PipelineError(
            f"리그 폴더에 캘리브레이션 파일이 {len(tomls)}개입니다: "
            f"{', '.join(t.name for t in tomls)}. 하나만 남기세요.")
    mp = d / "cameras.json"
    if not mp.is_file():
        raise PipelineError(
            f"{mp} 가 없습니다. 캘리브레이션의 cam01, cam02 가 각각 어느 폰인지 "
            "알아야 합니다. 예: {\"cam01\": \"6DC32E3A1F59\", \"cam02\": \"...\"}")
    device_of = {str(k): str(v) for k, v in json.loads(mp.read_text(encoding="utf-8")).items()}
    bad = [k for k in device_of if not CAM_NAME.match(k)]
    if bad:
        raise PipelineError(f"cameras.json 의 키는 cam01 형식이어야 합니다: {bad}")
    if len(set(device_of.values())) != len(device_of):
        raise PipelineError(f"cameras.json 에 같은 기기가 두 번 있습니다: {device_of}")
    return Rig(dir=d, calib_path=tomls[0], cameras=read_calib(tomls[0]), device_of=device_of)


# ── 카메라 번호 붙이기 ────────────────────────────────────────────────────────

@dataclass
class Camera:
    name: str                # cam01
    upload: Upload

    @property
    def device_id(self) -> str:
        return self.upload.device_id


def assign_cameras(uploads: list[Upload], rig: Rig | None) -> list[Camera]:
    """
    기기 → camNN.

    리그가 있으면 cameras.json 을 그대로 따릅니다 (캘리브레이션과 같은 번호여야 함).
    없으면 기기 ID 순서로 붙입니다 — 이때는 3D 를 만들지 않으므로 번호는 표시용입니다.
    """
    ids = [u.device_id for u in uploads]
    if len(set(ids)) != len(ids):
        raise PipelineError(f"같은 기기의 파일이 두 번 있습니다: {ids}")

    if rig is None:
        ordered = sorted(uploads, key=lambda u: u.device_id)
        return [Camera(cam_name(i), u) for i, u in enumerate(ordered)]

    cam_of = {dev: cam for cam, dev in rig.device_of.items()}
    unknown = [u.device_id for u in uploads if u.device_id not in cam_of]
    if unknown:
        raise PipelineError(
            f"캘리브레이션에 없는 폰입니다: {unknown}. 이 폰이 포함된 배치로 "
            "캘리브레이션을 다시 하거나 cameras.json 을 확인하세요.")
    missing = sorted(set(rig.device_of) - {cam_of[u.device_id] for u in uploads})
    if missing:
        raise PipelineError(
            f"캘리브레이션에는 있는데 이번 촬영에 없는 카메라: {missing}. "
            "Pose2Sim 은 캘리브레이션 카메라 수와 영상 수가 같아야 합니다.")
    cams = [Camera(cam_of[u.device_id], u) for u in uploads]
    return sorted(cams, key=lambda c: int(CAM_NAME.match(c.name).group(1)))


# ── 검사 ─────────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    where: str               # cam01 / 세션 / 캘리브레이션
    issue: Issue

    def __str__(self) -> str:
        return f"[{self.where}] {self.issue}"


def _size_close(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return all(abs(x - y) <= SIZE_TOLERANCE * max(x, y) for x, y in zip(a, b))


def check_calibration(rig: Rig, cams: list[Camera]) -> list[Issue]:
    """캘리브레이션 파일과 이번 촬영이 맞물리는지."""
    out: list[Issue] = []
    names = [c.name for c in rig.cameras]
    want = [c.name for c in cams]
    if len(names) != len(want):
        out.append(Issue(
            "fatal", "calib_count",
            f"캘리브레이션 카메라 {len(names)}대, 촬영 {len(want)}대입니다."))
        return out
    if names != want:
        # ★ Pose2Sim 은 순서로 짝짓습니다. 이름이 순서와 다르면 조용히 뒤바뀝니다.
        out.append(Issue(
            "fatal", "calib_order",
            f"캘리브레이션 파일 안의 카메라 순서가 {names} 입니다. {want} 순서여야 "
            "합니다. Pose2Sim 은 이름이 아니라 순서로 영상과 짝짓습니다."))
        return out
    for cc, cam in zip(rig.cameras, cams):
        sc = cam.upload.sidecar
        vid = (float(sc.width), float(sc.height))
        if vid[0] <= 0 or vid[1] <= 0:
            out.append(Issue("warning", "calib_size_unknown",
                             f"{cam.name}: 사이드카에 해상도가 없어 캘리브레이션과 대조하지 못했습니다."))
        elif _size_close(cc.size, vid):
            continue
        elif _size_close(cc.size, vid[::-1]):
            out.append(Issue(
                "warning", "calib_size_rotated",
                f"{cam.name}: 캘리브레이션 {cc.size[0]:.0f}x{cc.size[1]:.0f}, 영상 "
                f"{vid[0]:.0f}x{vid[1]:.0f} — 가로/세로가 바뀌었습니다. 영상이 회전 "
                "정보로 돌아가 읽히는지 확인하세요."))
        else:
            out.append(Issue(
                "fatal", "calib_size",
                f"{cam.name}: 캘리브레이션 해상도 {cc.size[0]:.0f}x{cc.size[1]:.0f} 가 영상 "
                f"{vid[0]:.0f}x{vid[1]:.0f} 와 다릅니다. 다른 설정으로 찍은 캘리브레이션입니다."))
    return out


def check_inputs(cams: list[Camera], rig: Rig | None) -> list[Finding]:
    """사이드카 개별 검증 + 세션 검증 + 캘리브레이션 대조. 치명이 있으면 멈춰야 합니다."""
    out: list[Finding] = []
    for c in cams:
        out += [Finding(c.name, i) for i in c.upload.sidecar.validate()]
    out += [Finding("세션", i) for i in check_session([c.upload.sidecar for c in cams])]
    if rig is not None:
        out += [Finding("캘리브레이션", i) for i in check_calibration(rig, cams)]
    return out


def has_fatal(findings: list[Finding]) -> bool:
    return any(f.issue.severity == "fatal" for f in findings)


# ── 프로젝트 폴더 ─────────────────────────────────────────────────────────────

def _same_file(src: Path, dst: Path) -> bool:
    try:
        if os.path.samefile(src, dst):          # 하드링크
            return True
    except OSError:
        pass
    a, b = src.stat(), dst.stat()               # 복사본 (copy2 는 수정 시각을 보존)
    return a.st_size == b.st_size and a.st_mtime_ns == b.st_mtime_ns


def _place(src: Path, dst: Path) -> str:
    """영상을 프로젝트에 둡니다. 같은 드라이브면 하드링크(용량 0), 아니면 복사."""
    if dst.exists():
        if _same_file(src, dst):
            return "있음"
        dst.unlink()
    try:
        os.link(src, dst)
        return "링크"
    except OSError:
        shutil.copy2(src, dst)
        return "복사"


POSE_SOURCES = "pose_sources.json"


def _pose_sources(cams: list[Camera]) -> dict:
    """camNN 마다 어느 기기의 어느 영상인지. 이게 바뀌면 2D 결과를 다시 만들어야 합니다."""
    out = {}
    for c in cams:
        st = c.upload.video.stat()
        out[c.name] = [c.device_id, str(c.upload.video.resolve()), st.st_size, st.st_mtime_ns]
    return out


def build_project(project: Path, cams: list[Camera], rig: Rig | None,
                  template_config: Path) -> dict[str, str]:
    """
    Pose2Sim 프로젝트 폴더를 만듭니다. 다시 부르면 뒤 단계 산출물을 지우고 새로 씁니다.

    ★ 2D 결과(pose/)는 오래 걸려서 남기지만, 어느 camNN 에든 **다른 영상**이 들어오면
      통째로 지웁니다. 실측(2026-09-27): 리그의 기기 ↔ camNN 표를 바꿔 다시 돌렸더니
      cam01 폴더에 옛 cam01 영상의 2D 결과가 남아, 새 cam01 사이드카 시각과 섞인 채
      오류 없이 3D 까지 나왔습니다. 일부만 지우면 안 됩니다 — Pose2Sim 은 pose/ 의
      첫 폴더만 보고 "이미 했다"며 추정 전체를 건너뜁니다.

    반환: 영상별 처리 방식 {"cam01": "링크"|"복사"|"있음"}, 2D 를 지웠으면 "pose/": "지움"
    """
    project = Path(project)
    if not is_ascii_path(project):
        raise PipelineError(
            f"작업 폴더 경로에 한글 등 ASCII 가 아닌 문자가 있습니다: {project}. "
            "OpenSim 이 이런 경로의 파일을 열지 못합니다 (DESIGN §3.9).")
    project.mkdir(parents=True, exist_ok=True)
    # Pose2Sim 은 부모 폴더에 Config.toml 이 있으면 배치 모드로 봅니다.
    if (project.parent / "Config.toml").exists():
        raise PipelineError(
            f"{project.parent} 에 Config.toml 이 있습니다. Pose2Sim 이 배치 모드로 "
            "해석해 설정이 섞입니다. 작업 폴더를 바꾸거나 그 파일을 치우세요.")

    for name in DOWNSTREAM_DIRS:
        shutil.rmtree(project / name, ignore_errors=True)

    how: dict[str, str] = {}
    src_file = project / POSE_SOURCES
    sources = _pose_sources(cams)
    old = json.loads(src_file.read_text(encoding="utf-8")) if src_file.is_file() else None
    if old != sources and (project / "pose").exists():
        shutil.rmtree(project / "pose")
        how["pose/"] = "지움"
    src_file.write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")

    vids = project / "videos"
    vids.mkdir(exist_ok=True)
    wanted = {f"{c.name}{c.upload.video.suffix.lower()}" for c in cams}
    for old in vids.iterdir():
        if old.name not in wanted:
            old.unlink()        # 이전에 다른 번호로 만든 영상이 섞이지 않게

    sides = project / "sidecars"
    shutil.rmtree(sides, ignore_errors=True)
    sides.mkdir()

    for c in cams:
        how[c.name] = _place(c.upload.video, vids / f"{c.name}{c.upload.video.suffix.lower()}")
        shutil.copy2(c.upload.sidecar_path, sides / f"{c.name}.json")

    calib = project / "calibration"
    shutil.rmtree(calib, ignore_errors=True)
    if rig is not None:
        calib.mkdir()
        shutil.copy2(rig.calib_path, calib / rig.calib_path.name)

    shutil.copy2(template_config, project / "Config.toml")
    (project / "cameras.json").write_text(json.dumps(
        {c.name: {"deviceId": c.device_id, "deviceName": c.upload.sidecar.device_name,
                  "video": str(c.upload.video), "sidecar": str(c.upload.sidecar_path)}
         for c in cams}, ensure_ascii=False, indent=2), encoding="utf-8")
    return how


def pose2sim_overrides(project: Path, fps: float, device: str, backend: str,
                       pose_mode: str = "performance", save_video: bool = False,
                       redo_pose: bool = False) -> dict:
    """
    Config.toml 위에 덮어쓸 설정. Pose2Sim 은 dict 를 받으면 프로젝트의
    Config.toml 과 재귀 병합합니다 (Pose2Sim.read_config_files).
    """
    return {
        "project": {
            "project_dir": str(project),
            # 리샘플러 격자가 정확히 이 fps 입니다. 필터 차단주파수 계산에 쓰입니다.
            "frame_rate": int(round(fps)),
            "multi_person": False,
        },
        "pose": {
            "mode": pose_mode,
            # ★ backend 와 device 를 함께 줘야 GPU 가 선택됩니다 (DESIGN §4.1 함정 2).
            "backend": backend,
            "device": device,
            "display_detection": False,
            "save_video": "to_video" if save_video else "none",
            "overwrite_pose": bool(redo_pose),
            "parallel_workers_pose": "auto",
        },
        # synchronization 은 부르지 않지만, 혹시 불려도 창을 띄워 멈추지 않게 둡니다.
        "synchronization": {"synchronization_gui": False, "display_sync_plots": False},
        # 데모 Config 는 필터 그래프 창을 띄워 닫을 때까지 멈춥니다.
        "filtering": {"display_figures": False, "save_filt_plots": True},
    }


# ── 결과 읽기 ─────────────────────────────────────────────────────────────────

_NUM = r"([\d.]+)"


def summarize_logs(logs_txt: str) -> dict:
    """
    Pose2Sim logs.txt 에서 **마지막 실행**의 품질 지표를 뽑습니다.
    logs.txt 는 실행할 때마다 뒤에 덧붙으므로 마지막 구간만 봅니다.
    """
    out: dict = {}
    tri = logs_txt.rfind("Triangulation of 2D points")
    assoc = logs_txt.rfind("Associating persons")
    if assoc >= 0:
        seg = logs_txt[assoc: tri if tri > assoc else None]
        m = re.search(rf"Mean reprojection error for \S+ point on all frames is {_NUM} px", seg)
        if m:
            out["association_reproj_px"] = float(m.group(1))
    if tri >= 0:
        seg = logs_txt[tri:]
        m = re.search(rf"Mean reprojection error for all points on [^\n]*? is {_NUM} px"
                      rf"(?:, which roughly corresponds to {_NUM} mm)?", seg)
        if m:
            out["reproj_px"] = float(m.group(1))
            if m.group(2):
                out["reproj_mm"] = float(m.group(2))
        m = re.search(rf"In average, {_NUM} cameras had to be excluded", seg)
        if m:
            out["avg_excluded_cams"] = float(m.group(1))
        m = re.search(r"Camera (\S+) was excluded (\d+)% of the time([^\n]*)", seg)
        if m:
            ex = {m.group(1): int(m.group(2))}
            for cam, pct in re.findall(r"Camera (\S+?): (\d+)%", m.group(3)):
                ex[cam] = int(pct)
            out["excluded_pct"] = ex
    return out


def trc_summary(path: Path) -> dict:
    """TRC 헤더(프레임·마커 수)와 비어 있는 좌표 비율."""
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    info: dict = {"file": Path(path).name}
    if len(lines) < 6:
        info["error"] = "데이터가 없습니다"
        return info
    head = dict(zip((k.strip() for k in lines[1].split("\t")),
                    (v.strip() for v in lines[2].split("\t"))))
    info["frames"] = int(float(head.get("NumFrames", 0) or 0))
    info["markers"] = int(float(head.get("NumMarkers", 0) or 0))
    info["rate"] = float(head.get("DataRate", 0) or 0)
    total = empty = 0
    for ln in lines[5:]:
        if not ln.strip():
            continue
        for tok in ln.split("\t")[2:]:
            total += 1
            t = tok.strip().lower()
            if not t or t in ("nan", "-nan", "inf", "-inf"):
                empty += 1
                continue
            try:
                if not math.isfinite(float(t)):
                    empty += 1
            except ValueError:
                empty += 1
    info["empty_pct"] = 100.0 * empty / total if total else 100.0
    return info


def quality_notes(summary: dict) -> list[str]:
    """숫자를 사람 말로. 판정 기준은 공식 데모 실측(재투영 10.4 px)에 기댑니다."""
    notes = []
    px = summary.get("reproj_px")
    excluded = summary.get("excluded_pct") or {}
    worst_ex = max(excluded.values(), default=0)
    if px is not None and worst_ex >= 50:
        # ★ Pose2Sim 은 오차가 허용치 밑으로 내려갈 때까지 카메라를 뺍니다. 그래서
        #   캘리브레이션이 틀려도 재투영 오차는 작게 나올 수 있습니다.
        #   실측(2026-09-27, 데모 4대에서 cam01↔cam03 을 일부러 뒤바꿈):
        #   재투영 6.0 px 로 오히려 좋아 보였고, 배제 비율 cam01 86% / cam02 51% 만 이상했습니다.
        notes.append(f"[경고] 재투영 오차 {px:.1f} px 는 카메라를 많이 버린 뒤의 값이라 "
                     "믿을 수 없습니다. 아래 배제 비율을 보세요 — 카메라가 뒤바뀌었거나 "
                     "캘리브레이션이 틀렸을 가능성이 큽니다.")
    elif px is not None:
        if px <= 15:
            notes.append(f"재투영 오차 {px:.1f} px — 좋음 (공식 데모 10.4 px, Pose2Sim 허용 15 px)")
        elif px <= 25:
            notes.append(f"[주의] 재투영 오차 {px:.1f} px — 데모(10.4 px)보다 큽니다. "
                         "캘리브레이션이나 동기를 의심해 볼 만합니다.")
        else:
            notes.append(f"[경고] 재투영 오차 {px:.1f} px — 큽니다. 캘리브레이션이 틀렸거나 "
                         "카메라가 뒤바뀌었을 수 있습니다.")
    if len(excluded) == 2 and px is not None:
        # 두 대면 카메라를 뺄 수 없고, 두 광선은 거의 늘 가까이 지나가서 오차가 작게 나옵니다.
        # 실측(데모 cam01+cam03): 8.6 px, 같은 영상 4대로는 10.6 px.
        notes.append("[참고] 카메라 2대의 재투영 오차는 4대보다 작게 나오는 편입니다. "
                     "작다고 더 정확한 것은 아닙니다 (검산할 세 번째 시점이 없음).")
    for cam, pct in excluded.items():
        if pct >= 50:
            notes.append(f"[경고] {cam} 이 {pct}% 버려졌습니다. 그 카메라에 사람이 잘 안 "
                         "보였거나 캘리브레이션이 어긋났습니다.")
    for t in summary.get("trc", []):
        if t.get("empty_pct", 0) > 20:
            notes.append(f"[주의] {t['file']}: 비어 있는 좌표 {t['empty_pct']:.0f}% "
                         "(사람이 화면 밖이거나 가려진 구간).")
    return notes
