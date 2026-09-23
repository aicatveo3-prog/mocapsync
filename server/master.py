#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MocapSync PC 테스트 마스터.

역할
----
1. mDNS 로 `_mocapsync._tcp` 를 광고합니다 -> 폰이 IP 입력 없이 찾아옵니다
2. TCP 로 슬레이브 접속을 받습니다
3. `time_req` 에 즉시 `time_resp` 로 답합니다 (t2, t3 를 자기 시계로 기록)
4. 슬레이브가 보낸 `time_result` 를 받아 화면에 표시합니다
5. `schedule_start` 로 예약 녹화를 지시합니다

왜 PC 마스터인가
---------------
- 개발자가 Windows 에서 Swift 를 컴파일할 수 없으므로, **검증된 상대**를 먼저 만듭니다
- 폰 1대만 있어도 클럭 동기 측정이 가능합니다
- 로그가 PC 에 모입니다

야외 촬영에서는 폰 마스터를 씁니다. 규약이 같으므로 구현만 두 곳에 있는 것입니다.

사용법
------
  python server/master.py                       # 광고 + 대기
  python server/master.py --port 9001
  python server/master.py --no-mdns             # mDNS 없이 (IP 직접 입력용)
  python server/master.py --auto-start 5        # 접속 5초 후 자동으로 예약 시작 지시
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import socket
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mocapsync import clocksync as cs  # noqa: E402
from mocapsync import protocol as P  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def log(msg: str, *, fh=None) -> None:
    line = f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def local_ips() -> list[str]:
    """이 PC 의 IPv4 주소들. 폰이 어디로 붙어야 하는지 보여주기 위함."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    # 기본 라우트로 나가는 주소 (가장 신뢰할 수 있음)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


class Slave:
    """접속한 슬레이브 하나의 상태."""

    def __init__(self, peer: str):
        self.peer = peer
        self.device_id: str = "?"
        self.name: str = "?"
        self.platform: str = "?"
        self.model: str = "?"
        self.os_version: str = "?"
        self.clock: str = "?"
        self.probe_count = 0
        self.estimate: dict | None = None
        self.state = "connected"

    def label(self) -> str:
        return f"{self.name}({self.device_id[:8]}) {self.model} {self.platform} {self.os_version}"


class Master:
    def __init__(self, port: int, auto_start_after: float | None, fh):
        self.port = port
        self.auto_start_after = auto_start_after
        self.fh = fh
        self.server_id = uuid.uuid4().hex[:12]
        self.slaves: dict[str, Slave] = {}
        self.session_seq = 0

    # ── 연결 처리 ────────────────────────────────────────────────────────────
    async def handle(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        peer_t = writer.get_extra_info("peername")
        peer = f"{peer_t[0]}:{peer_t[1]}" if peer_t else "?"

        # ★ Nagle 끄기. 안 끄면 작은 패킷이 뭉쳐서 왕복 시간이 부풀어오릅니다.
        sock = writer.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        sl = Slave(peer)
        self.slaves[peer] = sl
        log(f"[+] 접속 {peer}", fh=self.fh)

        auto_task = None
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = P.decode(line)
                except ValueError as e:
                    log(f"[!] {peer} 잘못된 메시지: {e}", fh=self.fh)
                    continue

                t = msg.get("type")

                if t == P.T.TIME_REQ:
                    # ★ 여기는 최대한 빨라야 합니다. t2 를 받은 직후,
                    #    t3 를 보내기 직전에 찍습니다.
                    t2 = P.now_ns()
                    seq = int(msg.get("seq", -1))
                    t1 = int(msg.get("t1", 0))
                    t3 = P.now_ns()
                    writer.write(P.encode(P.time_resp(seq, t1, t2, t3)))
                    await writer.drain()
                    sl.probe_count += 1
                    continue

                if t == P.T.HELLO:
                    proto = int(msg.get("proto", 0))
                    if proto != P.PROTOCOL_VERSION:
                        log(f"[!] {peer} 규약 불일치: 슬레이브 {proto} "
                            f"!= 마스터 {P.PROTOCOL_VERSION}. 연결 종료", fh=self.fh)
                        writer.write(P.encode(P.error(
                            "proto_mismatch",
                            f"master proto={P.PROTOCOL_VERSION}")))
                        await writer.drain()
                        break
                    sl.device_id = str(msg.get("deviceId", "?"))
                    sl.name = str(msg.get("name", "?"))
                    sl.platform = str(msg.get("platform", "?"))
                    sl.model = str(msg.get("model", "?"))
                    sl.os_version = str(msg.get("osVersion", "?"))
                    sl.clock = str(msg.get("clock", "?"))
                    log(f"    hello: {sl.label()}  clock={sl.clock}", fh=self.fh)
                    writer.write(P.encode(P.hello_ack(self.server_id, "py")))
                    await writer.drain()

                    if self.auto_start_after is not None:
                        auto_task = asyncio.create_task(
                            self._auto_start(writer, sl, self.auto_start_after))
                    continue

                if t == P.T.TIME_RESULT:
                    sl.estimate = msg
                    off = msg.get("offsetNs", 0) / cs.NS_PER_MS
                    rtt = msg.get("minRttNs", 0) / cs.NS_PER_MS
                    unc = msg.get("uncertaintyNs", 0) / cs.NS_PER_MS
                    spr = msg.get("spreadNs", 0) / cs.NS_PER_MS
                    used = msg.get("samplesUsed", 0)
                    total = msg.get("samplesTotal", 0)
                    ok = (used > 0) and (msg.get("uncertaintyNs", 1 << 62)
                                         < cs.DEFAULT_TARGET_NS)
                    log("", fh=self.fh)
                    log(f"    ┌─ 동기 결과: {sl.label()}", fh=self.fh)
                    log(f"    │  오프셋      {off:+.3f} ms", fh=self.fh)
                    log(f"    │  최소 RTT    {rtt:.3f} ms", fh=self.fh)
                    log(f"    │  오차 상한   {unc:.3f} ms   (= 최소RTT/2, 보장값)",
                        fh=self.fh)
                    log(f"    │  실측 흔들림 {spr:.3f} ms", fh=self.fh)
                    log(f"    │  샘플        {used}/{total}", fh=self.fh)
                    log(f"    └─ 판정: {'통과 ✔  (2ms 목표 달성)' if ok else '미달 �’'}",
                        fh=self.fh)
                    log("", fh=self.fh)
                    continue

                if t == P.T.STATUS:
                    sl.state = str(msg.get("state", "?"))
                    extras = []
                    if "battery" in msg:
                        extras.append(f"배터리 {msg['battery'] * 100:.0f}%")
                    if "thermal" in msg:
                        extras.append(f"온도 {msg['thermal']}")
                    if msg.get("framesCaptured"):
                        extras.append(f"프레임 {msg['framesCaptured']}")
                    log(f"    status[{sl.name}] {sl.state}"
                        + (f"  {' · '.join(extras)}" if extras else ""), fh=self.fh)
                    continue

                if t in (P.T.START_ACK, P.T.START_NACK):
                    lead = msg.get("leadNs", 0) / cs.NS_PER_MS
                    if t == P.T.START_ACK:
                        log(f"    start_ack[{sl.name}] 여유 {lead:.1f} ms", fh=self.fh)
                    else:
                        log(f"    start_NACK[{sl.name}] {msg.get('reason')} "
                            f"여유 {lead:.1f} ms", fh=self.fh)
                    continue

                if t == P.T.ERROR:
                    log(f"    error[{sl.name}] {msg.get('code')}: "
                        f"{msg.get('message')}", fh=self.fh)
                    continue

                log(f"    (무시) 알 수 없는 type={t}", fh=self.fh)

        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            if auto_task:
                auto_task.cancel()
            self.slaves.pop(peer, None)
            log(f"[-] 종료 {peer} (왕복 {sl.probe_count}회 응답)", fh=self.fh)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _auto_start(self, writer: asyncio.StreamWriter,
                          sl: Slave, delay_s: float) -> None:
        """테스트 편의: 접속 후 일정 시간 뒤 예약 시작을 자동 지시."""
        await asyncio.sleep(delay_s)
        self.session_seq += 1
        sid = f"S{datetime.now():%Y%m%d}-{self.session_seq}"
        # 설계 문서: 현재 + 500ms
        start_at = P.now_ns() + 500 * cs.NS_PER_MS
        log(f"    -> schedule_start {sid} (현재+500ms) 를 {sl.name} 에게", fh=self.fh)
        writer.write(P.encode(P.schedule_start(sid, start_at)))
        await writer.drain()

    async def run(self) -> None:
        server = await asyncio.start_server(self.handle, "0.0.0.0", self.port)
        addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
        log(f"TCP 대기: {addrs}", fh=self.fh)
        async with server:
            await server.serve_forever()


async def advertise_mdns(port: int, server_id: str):
    """
    mDNS 광고.

    ★ 반드시 AsyncZeroconf 를 써야 합니다.
    동기 Zeroconf().register_service() 를 실행 중인 asyncio 루프 안에서 호출하면
    zeroconf 가 이벤트 루프 블로킹을 감지해 EventLoopBlocked 예외를 던집니다.
    (실제로 이 함정에 걸렸습니다)
    """
    try:
        from zeroconf import ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf
    except ImportError:
        log("zeroconf 미설치 — mDNS 광고 없이 진행합니다 (IP 직접 입력 필요)")
        return None, None

    ips = local_ips()
    if not ips:
        log("로컬 IP 를 찾지 못했습니다 — mDNS 광고 생략")
        return None, None

    aiozc = AsyncZeroconf()
    info = ServiceInfo(
        P.SERVICE_TYPE,
        f"MocapSyncPC-{server_id[:6]}.{P.SERVICE_TYPE}",
        addresses=[socket.inet_aton(ip) for ip in ips],
        port=port,
        properties={
            "proto": str(P.PROTOCOL_VERSION),
            "role": "master",
            "impl": "py",
            "sid": "",
        },
        server=f"mocapsync-pc-{server_id[:6]}.local.",
    )
    await aiozc.async_register_service(info)
    log(f"mDNS 광고: {P.SERVICE_TYPE}  port={port}  ip={', '.join(ips)}")
    return aiozc, info


async def main_async(args) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"master_{datetime.now():%Y%m%d-%H%M%S}.log"
    fh = open(log_path, "w", encoding="utf-8")

    log("=" * 70, fh=fh)
    log(" MocapSync PC 테스트 마스터", fh=fh)
    log(f" 규약 v{P.PROTOCOL_VERSION}   목표: 오차 2ms 미만 "
        f"(= 최소 RTT 4ms 미만)", fh=fh)
    log("=" * 70, fh=fh)

    m = Master(args.port, args.auto_start, fh)
    aiozc, info = (None, None)
    if not args.no_mdns:
        try:
            aiozc, info = await advertise_mdns(args.port, m.server_id)
        except Exception as e:
            log(f"mDNS 광고 실패 ({type(e).__name__}: {e}) — "
                f"TCP 는 계속 동작합니다. 폰에서 IP 를 직접 입력하세요.", fh=fh)

    ips = local_ips()
    if ips:
        log(f"폰이 mDNS 로 못 찾으면 직접 입력: {ips[0]}:{args.port}", fh=fh)
    log("Ctrl+C 로 종료", fh=fh)
    log("", fh=fh)

    try:
        await m.run()
    except asyncio.CancelledError:
        pass
    finally:
        if aiozc and info:
            with contextlib.suppress(Exception):
                await aiozc.async_unregister_service(info)
                await aiozc.async_close()
        log(f"로그: {log_path}", fh=fh)
        fh.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="MocapSync PC 테스트 마스터")
    ap.add_argument("--port", type=int, default=P.DEFAULT_PORT)
    ap.add_argument("--no-mdns", action="store_true", help="mDNS 광고 생략")
    ap.add_argument("--auto-start", type=float, default=None,
                    metavar="초", help="접속 N초 후 예약 시작을 자동 지시 (테스트용)")
    args = ap.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n종료")
        return 0


if __name__ == "__main__":
    sys.exit(main())
