#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
슬레이브 시뮬레이터 — iOS 앱이 해야 할 일을 Python 으로 미리 구현한 것.

목적
----
1. 규약과 추정기를 **실제 TCP 소켓** 위에서 검증합니다 (단위테스트는 합성 데이터)
2. iOS 앱 개발 시 "이 순서로, 이 필드로 보내면 된다"는 살아있는 참조 구현
3. 인공 지연/비대칭/가짜 클럭 오프셋을 주입해 최악 조건을 시험

가짜 오프셋 주입이 강력한 이유
-----------------------------
같은 PC 에서 마스터와 이 시뮬레이터를 돌리면 진짜 오프셋은 0 입니다.
`--fake-offset-ms` 로 슬레이브 시계를 인위적으로 옮기면 **정답을 알고 있는 상태**가 되어,
추정기가 그 값을 되찾아오는지 채점할 수 있습니다. 실기기에서는 불가능한 검증입니다.

사용법
------
  # mDNS 로 마스터 자동 탐색
  python server/slave_sim.py

  # 직접 지정
  python server/slave_sim.py --host 192.168.0.10 --port 9001

  # 가짜 오프셋 +37.5ms 를 넣고 추정기가 되찾는지 확인
  python server/slave_sim.py --fake-offset-ms 37.5

  # 지연 주입 (편도 8ms, 흔들림 4ms, 업링크만 +6ms 비대칭)
  python server/slave_sim.py --delay-ms 8 --jitter-ms 4 --asym-ms 6
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import platform
import random
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

MS = cs.NS_PER_MS


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {msg}", flush=True)


class SlaveSim:
    def __init__(self, args):
        self.a = args
        self.fake_offset_ns = int(args.fake_offset_ms * MS)
        self.device_id = args.device_id or uuid.uuid4().hex[:12]
        self.name = args.name
        self.rng = random.Random(args.seed)
        self.offset_ns = 0
        self.est: cs.SyncEstimate | None = None

    # ── 슬레이브의 시계 ──────────────────────────────────────────────────────
    def clock_ns(self) -> int:
        """
        이 '기기'의 단조 시계.

        fake_offset 의 부호: 규약은 마스터 = 슬레이브 + θ 이므로,
        θ = +X 를 만들려면 슬레이브 시계를 X 만큼 **뒤로** 밀어야 합니다.
        """
        return P.now_ns() - self.fake_offset_ns

    async def _net_delay(self, uplink: bool) -> None:
        """인공 네트워크 지연. 업링크에만 비대칭을 더합니다."""
        d = self.a.delay_ms
        if d <= 0 and self.a.jitter_ms <= 0 and self.a.asym_ms <= 0:
            return
        extra = self.rng.uniform(0, self.a.jitter_ms)
        if uplink:
            extra += self.a.asym_ms
        await asyncio.sleep(max(0.0, (d + extra) / 1000.0))

    # ── 실행 ────────────────────────────────────────────────────────────────
    async def run(self, host: str, port: int) -> int:
        log(f"접속 시도 {host}:{port}")
        reader, writer = await asyncio.open_connection(host, port)

        sock = writer.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # 1) hello
        writer.write(P.encode(P.hello(
            device_id=self.device_id, name=self.name, platform="pysim",
            model=platform.machine() or "pc", os_version=platform.release(),
            app_version="sim-0.1.0", clock="time.monotonic_ns")))
        await writer.drain()

        ack = P.decode(await reader.readline())
        if ack.get("type") == P.T.ERROR:
            log(f"마스터가 거부: {ack}")
            return 2
        if ack.get("type") != P.T.HELLO_ACK:
            log(f"예상과 다른 응답: {ack}")
            return 2
        log(f"hello_ack 수신 (serverId={ack.get('serverId')}, impl={ack.get('impl')})")

        writer.write(P.encode(P.status("idle", battery=1.0, thermal="nominal")))
        await writer.drain()

        # 2) 시각 동기 왕복
        log(f"시각 동기 시작: {self.a.probes}회 왕복")
        if self.fake_offset_ns:
            log(f"  (가짜 오프셋 주입: {self.fake_offset_ns / MS:+.3f} ms "
                f"— 추정기가 이 값을 되찾아야 합니다)")
        if self.a.delay_ms or self.a.jitter_ms or self.a.asym_ms:
            log(f"  (인공 지연: 편도 {self.a.delay_ms}ms, 흔들림 {self.a.jitter_ms}ms, "
                f"업링크 비대칭 +{self.a.asym_ms}ms)")

        samples: list[cs.TimeSample] = []
        for seq in range(self.a.probes):
            t1 = self.clock_ns()
            await self._net_delay(uplink=True)
            writer.write(P.encode(P.time_req(seq, t1)))
            await writer.drain()

            resp = P.decode(await reader.readline())
            await self._net_delay(uplink=False)
            t4 = self.clock_ns()

            if resp.get("type") != P.T.TIME_RESP:
                log(f"  seq={seq} 예상과 다른 응답: {resp.get('type')}")
                continue
            samples.append(cs.TimeSample(
                seq=int(resp["seq"]), t1=int(resp["t1"]),
                t2=int(resp["t2"]), t3=int(resp["t3"]), t4=t4))

            if self.a.gap_ms > 0:
                await asyncio.sleep(self.a.gap_ms / 1000.0)

        self.est = cs.estimate(samples, best_k=self.a.best_k)
        self.offset_ns = self.est.offset_ns

        print()
        log("── 동기 결과 ──")
        for ln in self.est.summary_lines():
            log("  " + ln)
        log(f"  판정: {self.est.verdict()}")

        # 정답을 알고 있으면 채점
        if self.fake_offset_ns:
            err = abs(self.est.offset_ns - self.fake_offset_ns)
            within = err <= self.est.uncertainty_ns + 1
            log("")
            log(f"  ★ 채점 (정답을 알고 있으므로 가능)")
            log(f"     진짜 오프셋 = {self.fake_offset_ns / MS:+.3f} ms")
            log(f"     추정 오프셋 = {self.est.offset_ns / MS:+.3f} ms")
            log(f"     실제 오차   = {err / MS:.3f} ms")
            log(f"     보장 상한   = {self.est.uncertainty_ms:.3f} ms")
            log(f"     상한 준수   = {'예 ✔' if within else '아니오 ✘  <-- 버그!'}")
        print()

        # 3) 결과 보고
        writer.write(P.encode(P.time_result(self.est)))
        await writer.drain()
        writer.write(P.encode(P.status("synced", battery=1.0, thermal="nominal")))
        await writer.drain()

        # 4) 예약 시작 대기
        if self.a.exit_after_sync:
            log("--exit-after-sync 이므로 동기 결과만 보고하고 종료합니다.")
            await asyncio.sleep(0.3)  # 마스터가 결과를 받아 출력할 시간
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            if self.fake_offset_ns:
                err = abs(self.est.offset_ns - self.fake_offset_ns)
                return 0 if err <= self.est.uncertainty_ns + 1 else 3
            return 0 if self.est.meets_target() else 3

        log("예약 시작 명령 대기 중... (Ctrl+C 로 종료)")
        try:
            while True:
                line = await reader.readline()
                if not line:
                    log("마스터 연결 종료")
                    break
                msg = P.decode(line)
                t = msg.get("type")

                if t == P.T.SCHEDULE_START:
                    await self._on_schedule(writer, msg)
                elif t == P.T.STOP:
                    log(f"stop 수신: {msg.get('sessionId')}")
                    writer.write(P.encode(P.status("idle")))
                    await writer.drain()
                elif t == P.T.TIME_RESP:
                    pass
                else:
                    log(f"(무시) type={t}")
        except (ConnectionResetError, asyncio.IncompleteReadError):
            log("연결 끊김")
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
        return 0

    async def _precise_wait(self, target_ns: int) -> None:
        """
        정밀 대기. ★ iOS 앱이 이 패턴을 그대로 써야 합니다.

        왜 단순 sleep 이 안 되는가:
          OS 타이머 해상도가 수 ms 입니다. Windows 는 약 15.6ms,
          Darwin(iOS/macOS)도 기본 타이머는 수 ms 오차가 있습니다.
          클럭 동기를 0.2ms 로 맞춰놓고 시작 트리거가 12ms 늦으면
          그 정밀도가 전부 날아갑니다. (실측으로 확인했습니다: +11.7ms)

        해법: 2단 대기
          1) 목표보다 spin_margin 만큼 앞까지는 OS 에게 양보하며 대기 (CPU 절약)
          2) 남은 구간은 단조 시계를 바쁜대기(spin) — 마이크로초 정확도

        iOS 대응:
          1) Thread.sleep(forTimeInterval:) 또는 DispatchQueue.asyncAfter 로 대략 대기
          2) while clock_gettime_nsec_np(CLOCK_UPTIME_RAW) < target { } 로 마무리
             (실제로는 mach_wait_until() 이 더 좋습니다. 커널이 정밀하게 깨워줍니다)
        """
        margin = self.a.spin_margin_ns
        # 1단계: 대략 대기
        coarse_target = target_ns - margin
        while True:
            left = coarse_target - self.clock_ns()
            if left <= 0:
                break
            # 한 번에 다 자지 않고 조금씩 — 오버슈트를 줄입니다
            await asyncio.sleep(min(left / 1e9, 0.020))
        # 2단계: 스핀
        while self.clock_ns() < target_ns:
            pass

    async def _on_schedule(self, writer, msg: dict) -> None:
        sid = str(msg.get("sessionId"))
        start_master = int(msg.get("startAtMasterNs", 0))
        now_slave = self.clock_ns()

        chk = cs.check_schedule(start_master, self.offset_ns, now_slave)
        log(f"schedule_start {sid}: 마스터시각 {start_master} "
            f"-> 내 시각 {chk.start_at_slave_ns}, 여유 {chk.lead_ms:.1f} ms")

        if not chk.ok:
            log(f"  거부: {chk.reason}")
            writer.write(P.encode(P.start_nack(sid, "lead_too_small", chk.lead_ns)))
            await writer.drain()
            return

        writer.write(P.encode(P.start_ack(sid, chk.start_at_slave_ns, chk.lead_ns)))
        await writer.drain()
        writer.write(P.encode(P.status("armed")))
        await writer.drain()

        # 실제 앱은 여기서 카메라 세션을 이미 열어둔 상태로 그 순간을 기다립니다.
        remain_ms = (chk.start_at_slave_ns - self.clock_ns()) / MS
        log(f"  {remain_ms:.1f} ms 대기 후 '녹화' 시작 "
            f"(모드: {'정밀(스핀)' if self.a.precise_wait else '단순 sleep'})")

        if self.a.precise_wait:
            await self._precise_wait(chk.start_at_slave_ns)
        else:
            wait_s = (chk.start_at_slave_ns - self.clock_ns()) / 1e9
            if wait_s > 0:
                await asyncio.sleep(wait_s)

        actual = self.clock_ns()
        late_ms = (actual - chk.start_at_slave_ns) / MS
        log(f"  ▶ 녹화 시작. 예정 대비 {late_ms:+.3f} ms "
            f"({'프레임 간격 16.7ms 안' if abs(late_ms) < 16.7 else '★ 프레임 간격 초과!'})")
        writer.write(P.encode(P.status("recording", frames_captured=0)))
        await writer.drain()


async def discover(timeout: float) -> tuple[str, int] | None:
    """
    mDNS 로 마스터 찾기.

    ★ AsyncZeroconf / AsyncServiceBrowser 를 써야 합니다.
    동기 API 를 실행 중인 asyncio 루프에서 쓰면 EventLoopBlocked 가 납니다.
    """
    try:
        from zeroconf import ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
    except ImportError:
        log("zeroconf 미설치 — --host 로 직접 지정하세요")
        return None

    found: list[tuple[str, int]] = []
    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()

    aiozc = AsyncZeroconf()

    async def resolve(service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        if await info.async_request(aiozc.zeroconf, 3000):
            addrs = info.addresses or []
            if addrs:
                ip = socket.inet_ntoa(addrs[0])
                props = {
                    (k.decode() if isinstance(k, bytes) else k):
                    (v.decode() if isinstance(v, bytes) else v)
                    for k, v in (info.properties or {}).items()
                }
                found.append((ip, info.port))
                log(f"mDNS 발견: {name} -> {ip}:{info.port}  {props}")
                if not done.done():
                    done.set_result(True)

    def on_change(zeroconf, service_type, name, state_change) -> None:
        if state_change is ServiceStateChange.Added:
            asyncio.ensure_future(resolve(service_type, name))

    browser = AsyncServiceBrowser(aiozc.zeroconf, P.SERVICE_TYPE,
                                  handlers=[on_change])
    log(f"mDNS 탐색 중 ({P.SERVICE_TYPE}) ... 최대 {timeout}초")
    try:
        await asyncio.wait_for(done, timeout=timeout)
    except asyncio.TimeoutError:
        log("mDNS 탐색 시간 초과")
    finally:
        with contextlib.suppress(Exception):
            await browser.async_cancel()
            await aiozc.async_close()
    return found[0] if found else None


async def main_async(args) -> int:
    host, port = args.host, args.port
    if not host:
        got = await discover(args.discover_timeout)
        if not got:
            log("마스터를 못 찾았습니다. --host 로 직접 지정하세요.")
            return 1
        host, port = got
    sim = SlaveSim(args)
    return await sim.run(host, port)


def main() -> int:
    ap = argparse.ArgumentParser(description="MocapSync 슬레이브 시뮬레이터")
    ap.add_argument("--host", default=None, help="마스터 IP (비우면 mDNS 탐색)")
    ap.add_argument("--port", type=int, default=P.DEFAULT_PORT)
    ap.add_argument("--discover-timeout", type=float, default=8.0)
    ap.add_argument("--name", default="pysim-1")
    ap.add_argument("--device-id", default=None)
    ap.add_argument("--probes", type=int, default=cs.DEFAULT_PROBE_COUNT)
    ap.add_argument("--best-k", type=int, default=cs.DEFAULT_BEST_K)
    ap.add_argument("--gap-ms", type=float, default=5.0,
                    help="왕복 사이 간격")
    ap.add_argument("--fake-offset-ms", type=float, default=0.0,
                    help="가짜 클럭 오프셋 주입 (추정기 채점용)")
    ap.add_argument("--delay-ms", type=float, default=0.0, help="인공 편도 지연")
    ap.add_argument("--jitter-ms", type=float, default=0.0, help="인공 지연 흔들림")
    ap.add_argument("--asym-ms", type=float, default=0.0,
                    help="업링크에만 더하는 비대칭 지연")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--precise-wait", action="store_true", default=True,
                    help="정밀 대기(대략 sleep + 스핀). 기본 켜짐")
    ap.add_argument("--no-precise-wait", dest="precise_wait",
                    action="store_false",
                    help="단순 sleep 사용 (타이머 해상도 문제를 보여주는 비교용)")
    ap.add_argument("--spin-margin-ns", type=int,
                    default=cs.DEFAULT_SPIN_MARGIN_NS,
                    help="스핀으로 전환하는 남은시간 (기본 3ms)")
    ap.add_argument("--exit-after-sync", action="store_true",
                    help="동기 결과 보고 후 즉시 종료 (자동 테스트용). "
                         "가짜 오프셋을 줬으면 채점 결과를 종료코드로 반환")
    args = ap.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n종료")
        return 0


if __name__ == "__main__":
    sys.exit(main())
