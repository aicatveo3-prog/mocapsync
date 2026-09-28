"""
"3D 만들기" 작업 — 대시보드 버튼이 tools/run_session.py 를 별도 프로세스로 돌립니다.

★ 왜 별도 프로세스인가
 - Pose2Sim 은 리포 .venv 가 아니라 ~/.venv/pose2sim_gpu 에 있습니다 (GPU 패키지 조합).
 - 2D 추정은 수십 초~수 분 걸립니다. 마스터 안에서 돌리면 폰과의 시계 왕복 응답이
   늦어져 동기 품질이 떨어집니다. 마스터는 기다리기만 합니다.
 - GPU 를 두 작업이 나눠 쓰면 둘 다 느려지므로 한 번에 하나만 돌립니다.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from collections import deque
from pathlib import Path

from .sessions_index import is_safe_sid, read_run_result

STEP = re.compile(r"\[(\d)/(\d)\]\s*(.+)")

EXIT_MEANING = {
    0: "3D 완료",
    10: "3D 전까지 완료 (캘리브레이션 없음 또는 1대)",
    2: "입력 문제로 멈춤",
    3: "Pose2Sim 단계 실패",
}


def find_pose_python() -> Path | None:
    """Pose2Sim 이 설치된 파이썬. GPU 환경을 먼저 찾습니다."""
    home = Path.home()
    for c in (home / ".venv" / "pose2sim_gpu" / "Scripts" / "python.exe",
              home / ".venv" / "pose2sim" / "Scripts" / "python.exe",
              home / ".venv" / "pose2sim_gpu" / "bin" / "python",
              home / ".venv" / "pose2sim" / "bin" / "python"):
        if c.is_file():
            return c
    return None


class JobRunner:
    def __init__(self, python_exe: Path | None, script: Path, work_root: Path,
                 extra_args: list[str] | None = None):
        self.python_exe = Path(python_exe) if python_exe else None
        self.script = Path(script)
        self.work_root = Path(work_root)
        self.extra_args = list(extra_args or [])
        self.job: dict | None = None
        self.lines: deque[str] = deque(maxlen=200)
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return bool(self.job and self.job["state"] == "running")

    def start(self, sid: str) -> tuple[bool, str]:
        if not is_safe_sid(sid):
            return False, "세션 이름이 이상합니다"
        if self.python_exe is None or not self.python_exe.is_file():
            return False, ("Pose2Sim 파이썬을 찾지 못했습니다 (~/.venv/pose2sim_gpu). "
                           "tools/setup_pose2sim_gpu.ps1 로 만드세요.")
        if self.running:
            return False, f"이미 {self.job['sid']} 를 처리 중입니다. 끝나면 다시 누르세요."
        self.lines.clear()
        self.job = {"sid": sid, "state": "running", "step": 0, "steps": 7,
                    "stepText": "시작 중", "startedAt": time.time(), "endedAt": None,
                    "exitCode": None, "verdict": None}
        self._task = asyncio.get_running_loop().create_task(self._run(sid))
        return True, f"{sid} 3D 만들기를 시작했습니다"

    async def _run(self, sid: str) -> None:
        env = dict(os.environ, PYTHONUTF8="1", MPLBACKEND="Agg", PYTHONUNBUFFERED="1")
        try:
            proc = await asyncio.create_subprocess_exec(
                str(self.python_exe), str(self.script), sid,
                "--work-root", str(self.work_root), *self.extra_args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.script.parent.parent), env=env)
            assert proc.stdout is not None
            # ★ 줄 단위(readline)로 읽지 않습니다. Pose2Sim 의 진행 막대(tqdm)는 \r 만 쓰고
            #   줄을 바꾸지 않아서, 1000 프레임이면 한 "줄"이 64 KB 제한을 넘어 readline 이
            #   예외를 던집니다. 조각으로 읽어 \r, \n 둘 다에서 자릅니다.
            buf = b""
            while True:
                chunk = await proc.stdout.read(8192)
                if not chunk:
                    break
                buf += chunk
                *parts, buf = re.split(rb"[\r\n]", buf)
                for raw in parts:
                    self._take(raw)
                if len(buf) > 65536:           # 줄바꿈 없이 끝없이 오는 출력 방어
                    self._take(buf[-2000:])
                    buf = b""
            if buf:
                self._take(buf)
            code = await proc.wait()
        except Exception as e:  # noqa: BLE001
            self.lines.append(f"★ 실행 실패: {type(e).__name__}: {e}")
            code = -1
        res = read_run_result(self.work_root / "sessions" / sid)
        self.job.update(
            state="done" if code in (0, 10) else "failed",
            exitCode=code, endedAt=time.time(),
            verdict=(res or {}).get("verdict") or EXIT_MEANING.get(code, f"종료 코드 {code}"))

    def _take(self, raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace").rstrip()
        if not line or set(line) <= {"-", "="}:
            return
        if "it/s]" in line or "s/it]" in line:   # 진행 막대 한 칸은 로그에 넣지 않습니다
            return
        self.lines.append(line)
        m = STEP.search(line)
        if m:
            self.job["step"], self.job["steps"] = int(m.group(1)), int(m.group(2))
            self.job["stepText"] = m.group(3).strip()

    def snapshot(self) -> dict | None:
        if not self.job:
            return None
        j = dict(self.job)
        j["log"] = list(self.lines)[-40:]
        end = j["endedAt"] or time.time()
        j["elapsedS"] = round(end - j["startedAt"], 1)
        return j
