#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
마스터 조작 명령을 보냅니다 (같은 PC 에서만).

마스터 콘솔에서 s / x / l 을 눌러도 되지만, 마스터를 백그라운드로 돌리거나
스크립트로 자동 시험할 때는 이걸 씁니다.

사용:
    python server/ctl.py list
    python server/ctl.py start [--lead-ms 1000]
    python server/ctl.py stop  [--timeout 900]

보안: 마스터는 루프백(127.0.0.1)에서 온 명령만 받습니다. 같은 WiFi 의 다른
기기에서 녹화를 시작·정지시킬 수 없습니다.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mocapsync import protocol as P  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


async def main_async(a: argparse.Namespace) -> int:
    reader, writer = await asyncio.open_connection("127.0.0.1", a.tcp)
    extra = {}
    if a.cmd == "start":
        extra["leadMs"] = a.lead_ms
    if a.cmd == "stop":
        extra["timeoutS"] = a.timeout
    writer.write(P.encode(P.ctl(a.cmd, **extra)))
    await writer.drain()
    line = await reader.readline()
    writer.close()
    if not line:
        print("★ 마스터가 응답 없이 연결을 닫았습니다")
        return 2
    r = P.decode(line)
    print(r.get("message", ""))
    return 0 if r.get("ok") else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="MocapSync 마스터 조작")
    ap.add_argument("cmd", choices=["start", "stop", "list"])
    # 'port' 라는 이름을 피합니다 (일부 셸 래퍼가 거부)
    ap.add_argument("--tcp", type=int, default=P.DEFAULT_PORT)
    ap.add_argument("--lead-ms", type=int, default=1000)
    ap.add_argument("--timeout", type=float, default=900)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
