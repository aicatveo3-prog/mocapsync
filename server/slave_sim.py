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

원격 모드 (--remote)
-------------------
아이폰 녹화 화면의 'PC 원격 대기'와 같은 동작을 합니다. 연결을 유지한 채
마스터의 sync_request / schedule_start / stop 에 반응하고, 정지하면 사이드카와
더미 영상을 올린 뒤 record_done 을 보냅니다.
가짜 오프셋이 다른 시뮬레이터 두 개를 띄우면 **폰 두 대의 동시 촬영**을 PC 에서
끝까지 시험할 수 있습니다. 각 카메라의 프레임 위상도 무작위로 어긋나게 만듭니다
(실제 카메라들은 서로 프레임 경계가 맞지 않습니다).

사용법
------
  python server/slave_sim.py                               # mDNS 탐색
  python server/slave_sim.py --host 192.168.0.10 --port 9001
  python server/slave_sim.py --fake-offset-ms 37.5         # 추정기 채점
  python server/slave_sim.py --delay-ms 8 --jitter-ms 4 --asym-ms 6
  python server/slave_sim.py --remote --name camA --fake-offset-ms 12.5
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
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
FRAME_NS = 16_666_667   # 60 fps


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
        self.prof: cs.RttProfile = cs.EMPTY_RTT_PROFILE
        self.sync_at_slave_ns = 0
        # 원격 모드 상태
        self.pending: list[dict] = []
        self.rec_session: str | None = None
        self.rec_start_slave_ns = 0
        self.rec_requested_master_ns = 0
        # 카메라 프레임 위상. 실제 카메라들은 서로 프레임 경계가 맞지 않습니다.
        self.frame_phase_ns = self.rng.randrange(0, FRAME_NS)

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

    # ── 수신 ────────────────────────────────────────────────────────────────
    async def _read(self, reader) -> dict | None:
        if self.pending:
            return self.pending.pop(0)
        line = await reader.readline()
        if not line:
            return None
        return P.decode(line)

    async def _await_type(self, reader, types: set[str]) -> dict:
        """
        원하는 종류의 메시지가 올 때까지 읽고, 다른 것은 보관해 둡니다.

        ★ 아이폰 앱(LinkOps.awaitType)과 같은 규칙입니다.
          동기 측정이나 업로드 도중에 마스터 명령이 끼어들어도 버리지 않고
          나중에 처리합니다. 버리면 "시작했는데 한 대만 안 찍힘" 같은 일이 생깁니다.
        """
        while True:
            line = await reader.readline()
            if not line:
                raise ConnectionError("마스터 연결 종료")
            msg = P.decode(line)
            if msg.get("type") in types:
                return msg
            self.pending.append(msg)

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
            app_version="sim-0.2.0", clock="time.monotonic_ns")))
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

        # 2) 시각 동기
        await self.do_sync(reader, writer, verbose=True)
        writer.write(P.encode(P.status("synced", battery=1.0, thermal="nominal")))
        await writer.drain()

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

        try:
            if self.a.remote:
                await self.remote_loop(reader, writer)
            else:
                await self.legacy_loop(reader, writer)
        except (ConnectionResetError, asyncio.IncompleteReadError, ConnectionError):
            log("연결 끊김")
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
        return 0

    async def do_sync(self, reader, writer, verbose: bool = False) -> None:
        if verbose:
            log(f"시각 동기 시작: {self.a.probes}회 왕복")
            if self.fake_offset_ns:
                log(f"  (가짜 오프셋 주입: {self.fake_offset_ns / MS:+.3f} ms "
                    f"— 추정기가 이 값을 되찾아야 합니다)")
            if self.a.delay_ms or self.a.jitter_ms or self.a.asym_ms:
                log(f"  (인공 지연: 편도 {self.a.delay_ms}ms, 흔들림 {self.a.jitter_ms}ms, "
                    f"업링크 비대칭 +{self.a.asym_ms}ms)")

        # 워밍업 — 결과를 버리는 왕복.
        # PC 에서는 무선 절전이 없어 의미가 거의 없지만, 아이폰 앱과 **같은 절차**를
        # 돌려야 PC 에서 잡은 버그가 실기기에서도 같은 자리에서 잡힙니다.
        for w in range(self.a.warmup):
            t1 = self.clock_ns()
            await self._net_delay(uplink=True)
            writer.write(P.encode(P.time_req(-1 - w, t1)))
            await writer.drain()
            await self._await_type(reader, {P.T.TIME_RESP})
            await self._net_delay(uplink=False)

        samples: list[cs.TimeSample] = []
        bursts = 1
        for seq in range(self.a.probes):
            # 버스트 경계 — 연사만 하면 WiFi 상태의 짧은 시간 창 하나만 봅니다.
            if self.a.burst_size > 0 and seq > 0 and seq % self.a.burst_size == 0:
                bursts += 1
                await asyncio.sleep(self.a.burst_gap_ms / 1000.0)
                for w in range(self.a.burst_warmup):
                    wt1 = self.clock_ns()
                    await self._net_delay(uplink=True)
                    writer.write(P.encode(P.time_req(-1000 - w, wt1)))
                    await writer.drain()
                    await self._await_type(reader, {P.T.TIME_RESP})
                    await self._net_delay(uplink=False)

            t1 = self.clock_ns()
            await self._net_delay(uplink=True)
            writer.write(P.encode(P.time_req(seq, t1)))
            await writer.drain()

            resp = await self._await_type(reader, {P.T.TIME_RESP})
            await self._net_delay(uplink=False)
            t4 = self.clock_ns()
            if int(resp["seq"]) < 0:
                continue   # 뒤늦게 온 워밍업 응답 방어
            samples.append(cs.TimeSample(
                seq=int(resp["seq"]), t1=int(resp["t1"]),
                t2=int(resp["t2"]), t3=int(resp["t3"]), t4=t4))

            if self.a.gap_ms > 0:
                await asyncio.sleep(self.a.gap_ms / 1000.0)

        self.est = cs.estimate(samples, best_k=self.a.best_k)
        self.prof = cs.rtt_profile(samples)
        self.offset_ns = self.est.offset_ns
        self.sync_at_slave_ns = self.clock_ns()

        if verbose:
            print()
            log("── 동기 결과 ──")
            for ln in self.est.summary_lines():
                log("  " + ln)
            log(f"  판정: {self.est.verdict()}")
            log("")
            log(f"  버스트 {bursts}개로 분산 측정")
            log("  " + self.prof.summary_line())
            for ln in self.prof.histogram_lines():
                log("  " + ln)
            log(f"  분포 판정[{self.prof.shape}]: {self.prof.diagnosis}")
        if self.fake_offset_ns:
            err = abs(self.est.offset_ns - self.fake_offset_ns)
            within = err <= self.est.uncertainty_ns + 1
            log(f"  채점: 진짜 {self.fake_offset_ns / MS:+.3f} / 추정 "
                f"{self.est.offset_ns / MS:+.3f} / 오차 {err / MS:.3f} ms / "
                f"상한 {self.est.uncertainty_ms:.3f} ms  "
                f"{'✔' if within else '✘ <-- 버그!'}")

        cfg = (f"{self.a.probes}회/"
               + (f"{self.a.burst_size}x{self.a.burst_gap_ms:g}ms"
                  if self.a.burst_size > 0 else "단일연사")
               + f"/워밍업{self.a.warmup}+{self.a.burst_warmup}"
               + f"/간격{self.a.gap_ms:g}ms/pysim")
        writer.write(P.encode(P.time_result(self.est, self.prof, cfg)))
        await writer.drain()

    # ── 원격 모드 ────────────────────────────────────────────────────────────
    async def remote_loop(self, reader, writer) -> None:
        """
        ★ 아이폰 RemoteLink 와 같은 상태기계입니다.
        받은 명령에만 반응합니다. 스스로 타이머를 돌지 않습니다.
        """
        await self._status(writer, P.RemoteState.READY)
        log(f"원격 대기 ({self.name}) — 마스터의 명령을 기다립니다")
        while True:
            msg = await self._read(reader)
            if msg is None:
                log("마스터 연결 종료")
                return
            t = msg.get("type")
            if t == P.T.SYNC_REQUEST:
                if self.rec_session:
                    continue    # 녹화 중에는 재측정하지 않습니다
                await self.do_sync(reader, writer)
                await self._status(writer, P.RemoteState.READY)
            elif t == P.T.SCHEDULE_START:
                await self._on_remote_schedule(writer, msg)
            elif t == P.T.STOP:
                await self._on_remote_stop(reader, writer, msg)
            elif t == P.T.TIME_RESP:
                pass
            else:
                log(f"(무시) type={t}")

    async def _status(self, writer, state: str, frames: int = 0) -> None:
        writer.write(P.encode(P.status(state, battery=1.0, thermal="nominal",
                                       frames_captured=frames)))
        await writer.drain()

    async def _on_remote_schedule(self, writer, msg: dict) -> None:
        sid = str(msg.get("sessionId"))
        start_master = int(msg.get("startAtMasterNs", 0))
        if self.rec_session:
            writer.write(P.encode(P.start_nack(sid, "already_recording", 0)))
            await writer.drain()
            return
        chk = cs.check_schedule(start_master, self.offset_ns, self.clock_ns())
        if not chk.ok:
            log(f"schedule_start 거부: {chk.reason}")
            writer.write(P.encode(P.start_nack(sid, chk.reason, chk.lead_ns)))
            await writer.drain()
            return
        self.rec_session = sid
        self.rec_start_slave_ns = chk.start_at_slave_ns
        self.rec_requested_master_ns = start_master
        writer.write(P.encode(P.start_ack(sid, chk.start_at_slave_ns, chk.lead_ns)))
        await writer.drain()
        await self._status(writer, P.RemoteState.RECORDING)
        log(f"▶ {sid} 예약 수락: {chk.lead_ms:.1f} ms 뒤 첫 프레임부터 기록")

    def _frames(self, start_ns: int, stop_ns: int) -> list[list[int]]:
        """
        이 카메라가 찍었을 프레임 시각.

        아이폰 Recorder 와 같은 규칙: 카메라는 자기 위상으로 60fps 를 계속 내보내고,
        예약 시각 **이후의 첫 프레임**부터 기록합니다.
        """
        k0 = -(-(start_ns - self.frame_phase_ns) // FRAME_NS)   # ceil
        out = []
        t = self.frame_phase_ns + k0 * FRAME_NS
        while t <= stop_ns:
            out.append([len(out), t])
            t += FRAME_NS
        return out

    async def _on_remote_stop(self, reader, writer, msg: dict) -> None:
        sid = str(msg.get("sessionId"))
        if self.rec_session != sid:
            log(f"stop 무시: 녹화 중인 세션이 아닙니다 ({sid})")
            return
        await self._status(writer, P.RemoteState.STOPPING)
        stop_ns = self.clock_ns()
        frames = self._frames(self.rec_start_slave_ns, stop_ns)
        log(f"■ {sid} 정지: {len(frames)}프레임 ({len(frames) / 60:.2f}초)")

        side = self._sidecar(sid, frames)
        side_bytes = json.dumps(side, ensure_ascii=False, indent=2,
                                sort_keys=True).encode("utf-8")
        await self._status(writer, P.RemoteState.UPLOADING)
        # ★ 사이드카 먼저 (아이폰과 같은 순서)
        await self._upload(reader, writer, sid, f"{self.device_id}.json", side_bytes)
        await self._upload(reader, writer, sid, f"{self.device_id}.mov",
                           bytes(self.a.video_kb * 1024))

        writer.write(P.encode(P.record_done(
            sid, [f"{self.device_id}.json", f"{self.device_id}.mov"],
            len(frames), True, [])))
        await writer.drain()
        self.rec_session = None
        await self._status(writer, P.RemoteState.READY)

    async def _upload(self, reader, writer, sid: str, name: str, data: bytes) -> None:
        writer.write(P.encode(P.upload_begin(sid, name, len(data))))
        await writer.drain()
        r = await self._await_type(reader, {P.T.UPLOAD_READY, P.T.UPLOAD_DONE})
        if r.get("type") != P.T.UPLOAD_READY:
            raise ConnectionError(f"업로드 거부: {r}")
        step = 256 * 1024
        for off in range(0, len(data), step):
            writer.write(data[off:off + step])
            await writer.drain()
        done = await self._await_type(reader, {P.T.UPLOAD_DONE})
        if not done.get("ok"):
            raise ConnectionError(f"업로드 실패: {done}")

    def _sidecar(self, sid: str, frames: list[list[int]]) -> dict:
        """아이폰 Recorder 가 쓰는 것과 같은 모양의 사이드카."""
        return {
            "schemaVersion": 1,
            "deviceId": self.device_id,
            "deviceName": self.name,
            "model": "pysim",
            "osVersion": platform.release(),
            "appVersion": "sim-0.2.0",
            "sessionId": sid,
            "role": "slave",
            "clock": "time.monotonic_ns",
            "clockOffsetNs": self.offset_ns,
            "clockUncertaintyNs": self.est.uncertainty_ns if self.est else 0,
            "clockMinRttNs": self.est.min_rtt_ns if self.est else 0,
            "clockMeasuredAtNs": self.sync_at_slave_ns,
            # 읽기 잡음 수준의 차이 (실기기와 같게 — 같으면 정확비교 버그를 못 잡습니다)
            "sleepAtSyncNs": 1_000_000,
            "sleepAtRecordStartNs": 1_000_042,
            "timestampSource": "simulated",
            "timestampDomainDeltaNs": 0,
            "targetFps": 60,
            "width": 1920,
            "height": 1080,
            "cameraDeviceType": "AVCaptureDeviceTypeBuiltInWideAngleCamera",
            "fieldOfViewDeg": 69.7,
            "isBinned": False,
            "exposureDurationNs": 2_000_000,
            "iso": 640.0,
            "lensPosition": 0.5,
            "focusLocked": True,
            "whiteBalanceLocked": True,
            "exposureLocked": True,
            "stabilization": "off",
            "deviceOrientation": "landscapeLeft",
            "cameraWarnings": [],
            "requestedStartAtMasterNs": self.rec_requested_master_ns,
            "requestedStartAtSlaveNs": self.rec_start_slave_ns,
            "firstFramePtsNs": frames[0][1] if frames else None,
            "droppedFrameCount": 0,
            "thermalAtStart": "nominal",
            "thermalAtEnd": "nominal",
            "batteryAtStart": 1.0,
            "batteryAtEnd": 1.0,
            "frames": frames,
        }

    # ── 예전 모드: 예약 시작만 시험 ──────────────────────────────────────────
    async def legacy_loop(self, reader, writer) -> None:
        log("예약 시작 명령 대기 중... (Ctrl+C 로 종료)")
        while True:
            msg = await self._read(reader)
            if msg is None:
                log("마스터 연결 종료")
                return
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

    async def _precise_wait(self, target_ns: int) -> None:
        """
        정밀 대기 (대략 sleep + 스핀).

        아이폰 녹화에서는 이것이 필요 없습니다. 카메라가 이미 60fps 로 돌고 있어서
        "예약 시각 이후 첫 프레임부터 기록"하면 되기 때문입니다 (Recorder.swift).
        PC 쪽에서 정밀하게 무언가를 시작해야 할 때를 위해 남겨 둡니다.
        """
        margin = self.a.spin_margin_ns
        coarse_target = target_ns - margin
        while True:
            left = coarse_target - self.clock_ns()
            if left <= 0:
                break
            await asyncio.sleep(min(left / 1e9, 0.020))
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
                found.append((ip, info.port))
                log(f"mDNS 발견: {name} -> {ip}:{info.port}")
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
    ap.add_argument("--burst-size", type=int, default=cs.DEFAULT_BURST_SIZE,
                    help="버스트 하나에 넣을 측정 왕복 수. 0 이면 단일 연사.")
    ap.add_argument("--burst-gap-ms", type=float, default=cs.DEFAULT_BURST_GAP_MS)
    ap.add_argument("--burst-warmup", type=int, default=cs.DEFAULT_BURST_WARMUP)
    ap.add_argument("--warmup", type=int, default=cs.DEFAULT_WARMUP_COUNT)
    ap.add_argument("--gap-ms", type=float, default=float(cs.DEFAULT_PROBE_GAP_MS))
    ap.add_argument("--fake-offset-ms", type=float, default=0.0,
                    help="가짜 클럭 오프셋 주입 (추정기 채점용)")
    ap.add_argument("--delay-ms", type=float, default=0.0, help="인공 편도 지연")
    ap.add_argument("--jitter-ms", type=float, default=0.0, help="인공 지연 흔들림")
    ap.add_argument("--asym-ms", type=float, default=0.0,
                    help="업링크에만 더하는 비대칭 지연")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--precise-wait", action="store_true", default=True)
    ap.add_argument("--no-precise-wait", dest="precise_wait", action="store_false")
    ap.add_argument("--spin-margin-ns", type=int, default=cs.DEFAULT_SPIN_MARGIN_NS)
    ap.add_argument("--exit-after-sync", action="store_true",
                    help="동기 결과 보고 후 즉시 종료 (자동 테스트용)")
    ap.add_argument("--remote", action="store_true",
                    help="원격 모드: 연결을 유지하고 마스터의 시작/정지 명령을 따릅니다")
    ap.add_argument("--video-kb", type=int, default=256,
                    help="원격 모드에서 올릴 더미 영상 크기 (KB)")
    args = ap.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n종료")
        return 0


if __name__ == "__main__":
    sys.exit(main())
