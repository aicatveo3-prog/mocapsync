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
from mocapsync import sidecar  # noqa: E402

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
        self.app_version: str = "?"
        self.state = "connected"
        # ── 원격 촬영용 ──────────────────────────────────────────────────────
        self.writer: asyncio.StreamWriter | None = None
        self.is_ctl = False
        #: 마지막 time_result 를 받은 마스터 시각. 동기가 얼마나 낡았는지 판단합니다.
        self.last_sync_at_ns: int = 0
        #: sync_request 를 보냈고 아직 결과가 안 온 상태
        self.sync_pending = False
        self.sync_event = asyncio.Event()
        #: 세션별 start_ack/nack 대기
        self.ack_futures: dict[str, asyncio.Future] = {}
        #: 세션별 record_done 대기
        self.done_futures: dict[str, asyncio.Future] = {}

    def label(self) -> str:
        return f"{self.name}({self.device_id[:8]}) {self.model} {self.platform} {self.os_version}"

    @property
    def is_remote(self) -> bool:
        """원격 모드로 대기 중인 카메라인가 (녹화 화면에서 'PC 원격 대기'를 켠 폰)."""
        return self.state in P.RemoteState.ALL

    def sync_age_s(self) -> float:
        if not self.last_sync_at_ns:
            return float("inf")
        return (P.now_ns() - self.last_sync_at_ns) / 1e9

    async def send(self, msg: dict) -> None:
        if self.writer is None:
            raise ConnectionError("연결이 없습니다")
        self.writer.write(P.encode(msg))
        await self.writer.drain()


class Master:
    def __init__(self, port: int, auto_start_after: float | None, fh):
        self.port = port
        self.auto_start_after = auto_start_after
        self.fh = fh
        self.server_id = uuid.uuid4().hex[:12]
        self.slaves: dict[str, Slave] = {}
        self.session_seq = 0
        self.current_session: str | None = None
        self.session_cameras: list[Slave] = []
        # 업로드 받을 곳. 4단계 파이프라인이 여기를 입력으로 씁니다.
        self.upload_root = Path(__file__).resolve().parent.parent / "uploads"
        self.upload_root.mkdir(parents=True, exist_ok=True)

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
        sl.writer = writer
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
                    sl.app_version = str(msg.get("appVersion", "?"))
                    # ★ 앱 버전을 반드시 찍습니다.
                    #   실기기 테스트를 사람이 대신하므로, 폰에 깔린 빌드가
                    #   방금 만든 것인지 여기서 확인할 수 있어야 합니다.
                    #   (앱이 appVersion 에 "0.3.0 (7) 1668d37" 형태로 보냅니다)
                    log(f"    hello: {sl.label()}  clock={sl.clock}", fh=self.fh)
                    log(f"           앱 {sl.app_version}", fh=self.fh)
                    writer.write(P.encode(P.hello_ack(self.server_id, "py")))
                    await writer.drain()

                    if self.auto_start_after is not None:
                        auto_task = asyncio.create_task(
                            self._auto_start(writer, sl, self.auto_start_after))
                    continue

                if t == P.T.CTL:
                    # ★ 조작 명령은 이 PC 안에서만 받습니다.
                    #   같은 WiFi 의 아무 기기나 녹화를 시작·정지시키면 곤란하므로
                    #   루프백(127.0.0.1) 에서 온 것만 처리합니다.
                    sl.is_ctl = True
                    host = peer_t[0] if peer_t else ""
                    if host not in ("127.0.0.1", "::1"):
                        log(f"[!] {peer} 에서 온 조작 명령 거부 (루프백만 허용)", fh=self.fh)
                        await sl.send(P.ctl_result(False, "루프백에서만 조작할 수 있습니다"))
                        continue
                    ok, text, sid = await self.run_command(
                        str(msg.get("cmd", "")), msg)
                    await sl.send(P.ctl_result(ok, text, sid))
                    continue

                if t == P.T.RECORD_DONE:
                    sid = str(msg.get("sessionId", ""))
                    usable = bool(msg.get("usable"))
                    log(f"    record_done[{sl.name}] {sid}  프레임 {msg.get('frames')}  "
                        f"폰 자체검증 {'사용 가능' if usable else '★ 사용 불가'}", fh=self.fh)
                    for f in msg.get("fatal") or []:
                        log(f"      [치명] {f}", fh=self.fh)
                    fut = sl.done_futures.get(sid)
                    if fut and not fut.done():
                        fut.set_result(msg)
                    continue

                if t == P.T.TIME_RESULT:
                    sl.estimate = msg
                    sl.last_sync_at_ns = P.now_ns()
                    sl.sync_pending = False
                    sl.sync_event.set()
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
                    cfg = str(msg.get("config", "") or "")
                    if cfg:
                        log(f"    │  측정 설정   {cfg}", fh=self.fh)
                    self._log_rtt_profile(msg, total)
                    log(f"    └─ 판정: {'통과 ✔  (2ms 목표 달성)' if ok else '미달 �’'}",
                        fh=self.fh)
                    log("", fh=self.fh)
                    continue

                if t == P.T.UPLOAD_BEGIN:
                    await self._receive_file(reader, writer, sl, msg)
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
                    fut = sl.ack_futures.get(str(msg.get("sessionId", "")))
                    if fut and not fut.done():
                        fut.set_result(msg)
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
            # 이 폰을 기다리던 명령이 끝없이 기다리지 않게 풀어줍니다.
            for fut in list(sl.ack_futures.values()) + list(sl.done_futures.values()):
                if not fut.done():
                    fut.set_result(None)
            sl.sync_event.set()
            sl.writer = None
            self.slaves.pop(peer, None)
            log(f"[-] 종료 {peer} (왕복 {sl.probe_count}회 응답)", fh=self.fh)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _receive_file(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter,
                            sl: "Slave", msg: dict) -> None:
        """
        업로드 한 건을 받습니다.

        규약: `upload_begin` 줄 다음에 **정확히 size 바이트의 원본 데이터**가 옵니다.
        readexactly 로 그만큼만 읽으면 다시 줄 모드로 돌아옵니다.

        ★ 사이드카(.json)를 받으면 그 자리에서 Python 검증기에 넣어 판정을 찍습니다.
          왜: 폰(Swift)과 PC(Python)의 검증 구현이 같은 판정을 내야 하는데,
          지금까지는 그걸 실제 데이터로 확인한 적이 없었습니다. 업로드마다
          자동으로 대조되면 두 구현이 갈라지는 순간 드러납니다.
        """
        name = str(msg.get("name", "")).strip()
        size = int(msg.get("size", 0))
        session_id = str(msg.get("sessionId", "")) or "unknown"

        # ★ 경로 탈출 방어. 이름은 파일명으로만 씁니다.
        #   "../../.." 같은 이름이 오면 저장 위치를 벗어납니다.
        safe = Path(name).name
        if not safe or safe in (".", ".."):
            writer.write(P.encode(P.upload_done(name, 0, False, "이름이 비었습니다")))
            await writer.drain()
            return
        safe_session = Path(session_id).name or "unknown"

        if size < 0 or size > 4 * 1024 * 1024 * 1024:
            writer.write(P.encode(P.upload_done(safe, 0, False, f"크기가 이상합니다: {size}")))
            await writer.drain()
            return

        out_dir = self.upload_root / safe_session
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / safe
        tmp = dest.with_suffix(dest.suffix + ".part")

        log(f"    업로드 시작: {safe}  {size / 1_048_576:.2f} MB", fh=self.fh)
        writer.write(P.encode(P.upload_ready(safe)))
        await writer.drain()

        t0 = P.now_ns()
        got = 0
        try:
            with open(tmp, "wb") as f:
                while got < size:
                    chunk = await reader.readexactly(min(256 * 1024, size - got))
                    f.write(chunk)
                    got += len(chunk)
        except asyncio.IncompleteReadError as e:
            got += len(e.partial)
            log(f"    ★ 업로드 중단: {got}/{size} 바이트", fh=self.fh)
            tmp.unlink(missing_ok=True)
            return
        except Exception as e:  # noqa: BLE001
            log(f"    ★ 업로드 실패: {e}", fh=self.fh)
            tmp.unlink(missing_ok=True)
            return

        tmp.replace(dest)
        dt = (P.now_ns() - t0) / 1e9
        mbps = (size * 8 / 1e6 / dt) if dt > 0 else 0
        log(f"    업로드 완료: {dest}  {dt:.1f}초  {mbps:.1f} Mbps", fh=self.fh)

        writer.write(P.encode(P.upload_done(safe, got, True)))
        await writer.drain()

        if safe.lower().endswith(".json"):
            self._validate_uploaded_sidecar(dest)

    def _validate_uploaded_sidecar(self, path: Path) -> None:
        """
        받은 사이드카를 Python 검증기로 판정합니다.

        ★ 이게 Swift/Python 구현 일치의 실전 검증입니다.
          폰 화면에 찍힌 판정과 여기 판정이 같아야 합니다. 다르면 두 구현이
          갈라진 것이고, 그건 테스트가 잡지 못한 차이라는 뜻입니다.
        """
        try:
            sc = sidecar.Sidecar.load(path)
        except Exception as e:  # noqa: BLE001
            log(f"    ★ 사이드카를 읽을 수 없습니다: {e}", fh=self.fh)
            return

        st = sc.interval_stats()
        log("", fh=self.fh)
        log(f"    ┌─ 사이드카 검증 (PC/Python): {path.name}", fh=self.fh)
        log(f"    │  기기        {sc.device_id}  {sc.model}  앱 {sc.app_version}", fh=self.fh)
        log(f"    │  세션        {sc.session_id}", fh=self.fh)
        log(f"    │  프레임      {sc.frame_count}개  길이 {sc.duration_ns / 1e9:.2f}초", fh=self.fh)
        log(f"    │  실측 fps    {st.get('estimated_fps', 0):.3f}  "
            f"(목표 {sc.target_fps})", fh=self.fh)
        log(f"    │  간격        중앙 {st.get('median_interval_ms', 0):.3f} ms  "
            f"최소 {st.get('min_interval_ms', 0):.3f}  "
            f"최대 {st.get('max_interval_ms', 0):.3f}  "
            f"표준편차 {st.get('stdev_interval_ms', 0):.3f}", fh=self.fh)
        log(f"    │  드롭 의심   {st.get('suspected_drops', 0)}곳  "
            f"(카메라 보고 {sc.dropped_frame_count}개)", fh=self.fh)
        log(f"    │  해상도      {sc.width}x{sc.height}  화각 {sc.field_of_view_deg:.1f}도  "
            f"binned={sc.is_binned}", fh=self.fh)
        log(f"    │  셔터        {sc.exposure_duration_ns / 1000:.0f} µs  "
            f"ISO {sc.iso:.0f}  렌즈 {sc.lens_position:.3f}", fh=self.fh)
        log(f"    │  잠금        노출={sc.exposure_locked} 초점={sc.focus_locked} "
            f"WB={sc.white_balance_locked} 안정화={sc.stabilization}", fh=self.fh)
        log(f"    │  오프셋      {sc.clock_offset_ns / cs.NS_PER_MS:+.3f} ms  "
            f"상한 {sc.clock_uncertainty_ns / cs.NS_PER_MS:.3f} ms", fh=self.fh)
        log(f"    │  발열        {sc.thermal_at_start} → {sc.thermal_at_end}", fh=self.fh)
        for line_ in sc.validation_report():
            log(f"    │  {line_}", fh=self.fh)
        log(f"    └─ 판정: {'사용 가능 ✔' if sc.is_usable else '★ 사용 불가'}", fh=self.fh)
        log("", fh=self.fh)

    def _log_rtt_profile(self, msg: dict, total: int) -> None:
        """
        슬레이브가 보낸 RTT 분포를 찍습니다.

        ★ 왜 마스터가 이걸 보여줘야 하는가

        최소 RTT 숫자 하나로는 다음 두 상황을 구분할 수 없습니다.
          (가) 이미 이 경로의 물리적 바닥   -> 왕복을 늘려도 안 내려감. 경로를 바꿔야 함
          (나) 표본 부족 / 꼬리가 두꺼움     -> 왕복을 늘리면 내려감
        p0 와 p50 의 간격이 그걸 알려줍니다.

        분포가 폰 안에만 있으면 사람이 매번 로그를 내보내 붙여야 하고,
        그만큼 디버깅 루프가 느려집니다. 그래서 규약에 실어 여기서 찍습니다.

        ★ 그리고 여기서 **독립 재계산**을 합니다.
        받은 백분위로 마스터가 직접 판정을 계산해 슬레이브가 보낸 rttShape 와
        비교합니다. 다르면 Swift/Python 두 구현이 갈라진 것이므로 경고합니다.
        (두 구현이 같은 입력에 같은 출력을 내야 한다는 규칙의 런타임 검사)
        """
        p0 = int(msg.get("rttP0Ns", 0))
        if p0 <= 0:
            log("    │  (RTT 분포 없음 — 구버전 슬레이브. 앱을 업데이트하면 보입니다)",
                fh=self.fh)
            return

        prof = cs.RttProfile(
            count=max(0, total - int(msg.get("samplesRejected", 0))),
            p0_ns=p0,
            p10_ns=int(msg.get("rttP10Ns", 0)),
            p50_ns=int(msg.get("rttP50Ns", 0)),
            p90_ns=int(msg.get("rttP90Ns", 0)),
            p100_ns=int(msg.get("rttP100Ns", 0)),
            buckets=cs.EMPTY_RTT_PROFILE.buckets,
        )
        log(f"    │  {prof.summary_line()}", fh=self.fh)
        log(f"    │  분포[{prof.shape}] {prof.diagnosis}", fh=self.fh)

        theirs = str(msg.get("rttShape", "?"))
        if theirs != prof.shape:
            log(f"    │  ★ 경고: 슬레이브 판정 '{theirs}' != 마스터 판정 "
                f"'{prof.shape}'. Swift/Python 구현이 갈라졌습니다.", fh=self.fh)

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

    # ── 원격 촬영 ────────────────────────────────────────────────────────────
    #
    # ★ 왜 마스터가 주기적으로 동기를 다시 시키는가
    #
    # 두 폰의 수정발진자는 주파수가 미세하게 달라 오프셋이 시간에 따라 흐릅니다.
    # 규격 최악 40 ppm 이면 50초에 2 ms 입니다 (DESIGN.md, ClockOffsetRecord).
    # 폰을 세워두고 사람이 준비하는 동안 몇 분이 지나기 쉬우므로, 대기 중에는
    # 마스터가 RESYNC_INTERVAL_S 마다 다시 재게 합니다. 그래서 "시작"을 누르는
    # 순간 오프셋이 항상 30초 이내로 신선합니다.
    #
    # 폰이 스스로 타이머를 도는 대신 마스터가 시키는 이유: 폰 쪽을 "받은 명령에만
    # 반응하는" 단순한 구조로 둘 수 있습니다. 개발자가 폰 코드를 실기기에서
    # 돌려볼 수 없으므로, 폰 쪽 동시성은 적을수록 안전합니다.

    RESYNC_INTERVAL_S = 30.0
    #: 시작 직전 이보다 오래된 동기는 다시 잽니다.
    MAX_SYNC_AGE_AT_START_S = 45.0
    #: 예약 시작 여유. 폰은 최소 300 ms 를 요구합니다 (SyncConfig.minLeadNs).
    #: 핫스팟에서 최대 RTT 278 ms 를 본 적이 있어 넉넉히 1초로 둡니다.
    #: 카메라는 이미 돌고 있으므로 여유가 길어도 손해는 시작이 1초 늦는 것뿐입니다.
    DEFAULT_LEAD_MS = 1000

    def remote_cameras(self) -> list[Slave]:
        return [s for s in self.slaves.values() if s.is_remote and not s.is_ctl]

    async def resync_loop(self) -> None:
        """대기 중인 원격 카메라에게 주기적으로 동기를 다시 시킵니다."""
        while True:
            await asyncio.sleep(5.0)
            for sl in self.remote_cameras():
                if sl.state != P.RemoteState.READY or sl.sync_pending:
                    continue
                if sl.sync_age_s() < self.RESYNC_INTERVAL_S:
                    continue
                await self._request_sync(sl, "주기 재측정")

    async def _request_sync(self, sl: Slave, reason: str) -> None:
        sl.sync_pending = True
        sl.sync_event.clear()
        try:
            await sl.send(P.sync_request(reason))
        except Exception as e:  # noqa: BLE001
            sl.sync_pending = False
            sl.sync_event.set()
            log(f"    sync_request 실패[{sl.name}]: {e}", fh=self.fh)

    def list_text(self) -> str:
        cams = self.remote_cameras()
        others = [s for s in self.slaves.values()
                  if not s.is_remote and not s.is_ctl and s.device_id != "?"]
        lines = [f"원격 카메라 {len(cams)}대"
                 + (f"  (진행 중 세션 {self.current_session})" if self.current_session else "")]
        for s in cams:
            unc = (s.estimate or {}).get("uncertaintyNs", 0) / cs.NS_PER_MS
            age = s.sync_age_s()
            lines.append(
                f"  - {s.name} [{s.device_id[:8]}] {s.state:<13} "
                f"동기 {'측정 중' if s.sync_pending else f'{age:5.0f}초 전'}  "
                f"상한 {unc:.3f} ms  앱 {s.app_version}")
        if others:
            lines.append(f"원격 대기가 아닌 연결 {len(others)}개 (동기 화면/업로드 등)")
        if not cams:
            lines.append("  폰의 녹화 화면에서 'PC 원격 대기'를 켜세요.")
        return "\n".join(lines)

    async def run_command(self, cmd: str, msg: dict | None = None) -> tuple[bool, str, str | None]:
        msg = msg or {}
        if cmd == "start":
            return await self.start_recording(int(msg.get("leadMs", self.DEFAULT_LEAD_MS)))
        if cmd == "stop":
            return await self.stop_recording(float(msg.get("timeoutS", 900)))
        if cmd == "list":
            return True, self.list_text(), self.current_session
        return False, f"모르는 명령: {cmd!r} (start / stop / list)", None

    async def start_recording(self, lead_ms: int) -> tuple[bool, str, str | None]:
        """
        모든 원격 카메라에게 같은 순간부터 녹화하라고 지시합니다.

        순서
          1) 동기가 오래됐거나 측정 중인 폰은 새 결과를 기다립니다
          2) 공통 시각(마스터 시계) = 지금 + lead 를 정해 모두에게 보냅니다
          3) 각 폰의 ack/nack 을 모읍니다
        폰은 그 시각을 자기 시계로 바꿔서, 그 시각 이후의 첫 프레임부터 기록합니다.
        """
        if self.current_session:
            return False, f"이미 녹화 중입니다: {self.current_session}", self.current_session
        cams = [s for s in self.remote_cameras() if s.state == P.RemoteState.READY]
        if not cams:
            return False, "대기 중인 원격 카메라가 없습니다.\n" + self.list_text(), None

        # 1) 동기 신선도
        stale = [s for s in cams if s.sync_pending or s.sync_age_s() > self.MAX_SYNC_AGE_AT_START_S]
        for s in stale:
            if not s.sync_pending:
                await self._request_sync(s, "시작 직전 재측정")
        if stale:
            log(f"    시작 전 동기 재측정 대기: {', '.join(s.name for s in stale)}", fh=self.fh)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(s.sync_event.wait() for s in stale)), timeout=30)
            except asyncio.TimeoutError:
                return False, "동기 재측정이 30초 안에 끝나지 않았습니다.", None
            cams = [s for s in self.remote_cameras() if s.state == P.RemoteState.READY]

        # 2) 예약
        sid = f"S{datetime.now():%Y%m%d-%H%M%S}"
        start_at = P.now_ns() + lead_ms * cs.NS_PER_MS
        loop = asyncio.get_running_loop()
        for s in cams:
            s.ack_futures[sid] = loop.create_future()
            s.done_futures[sid] = loop.create_future()
        self.current_session = sid
        self.session_cameras = list(cams)
        log("", fh=self.fh)
        log(f"▶ 녹화 시작 지시 {sid}  카메라 {len(cams)}대  여유 {lead_ms} ms", fh=self.fh)
        for s in cams:
            await s.send(P.schedule_start(sid, start_at))

        # 3) 응답 모으기
        results = []
        for s in cams:
            try:
                r = await asyncio.wait_for(s.ack_futures[sid], timeout=lead_ms / 1000 + 5)
            except asyncio.TimeoutError:
                r = None
            results.append((s, r))

        acked = [s for s, r in results if r and r.get("type") == P.T.START_ACK]
        failed = [(s, r) for s, r in results if not (r and r.get("type") == P.T.START_ACK)]
        lines = [f"{sid}: {len(acked)}/{len(cams)}대 시작"]
        for s, r in failed:
            why = (r or {}).get("reason", "응답 없음")
            lines.append(f"  ★ {s.name}: {why}")
        if not acked:
            self.current_session = None
            self.session_cameras = []
            return False, "\n".join(lines), sid
        self.session_cameras = acked
        log("  " + "\n  ".join(lines), fh=self.fh)
        return True, "\n".join(lines), sid

    async def stop_recording(self, timeout_s: float) -> tuple[bool, str, str | None]:
        """모든 카메라를 멈추고, 파일이 다 올라오면 세션 단위로 검사합니다."""
        sid = self.current_session
        if not sid:
            return False, "진행 중인 녹화가 없습니다.", None
        cams = list(self.session_cameras)
        log("", fh=self.fh)
        log(f"■ 녹화 정지 지시 {sid}  카메라 {len(cams)}대", fh=self.fh)
        for s in cams:
            with contextlib.suppress(Exception):
                await s.send(P.stop(sid))

        done = []
        for s in cams:
            fut = s.done_futures.get(sid)
            try:
                r = await asyncio.wait_for(fut, timeout=timeout_s) if fut else None
            except asyncio.TimeoutError:
                r = None
            done.append((s, r))

        self.current_session = None
        self.session_cameras = []
        report = self._session_report(sid)
        missing = [s.name for s, r in done if r is None]
        head = f"{sid}: {len(done) - len(missing)}/{len(cams)}대 업로드 완료"
        if missing:
            head += f"  (★ 못 받음: {', '.join(missing)})"
        return not missing, head + "\n" + report, sid

    def _session_report(self, sid: str) -> str:
        """
        ★ 세션 단위 검사.

        사이드카 하나하나는 업로드될 때 이미 검증했습니다. 여기서는 여러 대를
        **같이** 놓았을 때만 드러나는 문제를 봅니다 — 공통 시간축에서 겹치는
        구간이 있는지, deviceId 가 겹치지 않는지, 두 카메라의 시작 시각이
        얼마나 맞았는지.
        """
        d = self.upload_root / sid
        files = sorted(d.glob("*.json"))
        if not files:
            return "  (사이드카가 없습니다)"
        cams = []
        for f in files:
            try:
                cams.append(sidecar.Sidecar.load(f))
            except Exception as e:  # noqa: BLE001
                log(f"    ★ {f.name} 읽기 실패: {e}", fh=self.fh)

        lines = []
        log("", fh=self.fh)
        log(f"    ┌─ 세션 검사: {sid}  ({len(cams)}대)", fh=self.fh)
        # 첫 프레임 시각을 공통 시간축(마스터 시계)으로 옮겨 비교합니다.
        firsts = []
        for c in cams:
            t = c.timestamps_master_ns
            if t:
                firsts.append((c, t[0], t[-1]))
                line = (f"{c.device_id[:8]}  {c.frame_count}프레임  "
                        f"첫 프레임 {t[0] / 1e9:.6f}s (공통시각)  "
                        f"상한 {c.clock_uncertainty_ns / cs.NS_PER_MS:.3f} ms  "
                        f"{'사용 가능' if c.is_usable else '★ 사용 불가'}")
                lines.append("  " + line)
                log(f"    │  {line}", fh=self.fh)
        if len(firsts) >= 2:
            t0s = [f[1] for f in firsts]
            spread_ms = (max(t0s) - min(t0s)) / cs.NS_PER_MS
            line = (f"첫 프레임 시각 차이 {spread_ms:.3f} ms "
                    f"(60fps 한 프레임 16.667 ms. 이 안이면 예약 시작이 같은 프레임을 잡은 것)")
            lines.append("  " + line)
            log(f"    │  {line}", fh=self.fh)
        for i in sidecar.check_session(cams):
            lines.append(f"  {i}")
            log(f"    │  {i}", fh=self.fh)
        log(f"    └─ 저장 위치: {d}", fh=self.fh)
        log("", fh=self.fh)
        return "\n".join(lines)

    async def run(self) -> None:
        server = await asyncio.start_server(self.handle, "0.0.0.0", self.port)
        addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
        log(f"TCP 대기: {addrs}", fh=self.fh)
        resync = asyncio.create_task(self.resync_loop())
        try:
            async with server:
                await server.serve_forever()
        finally:
            resync.cancel()


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


async def watch_ip_changes(aiozc, info, port: int, fh, interval_s: float = 5.0):
    """
    ★ IP 가 바뀌면 mDNS 광고를 다시 등록합니다.

    왜 필요한가 (실제로 세 번 겪은 문제)

    광고는 마스터를 켤 때 읽은 IP 로 한 번 등록됩니다. 그런데 노트북은
    장소를 옮기거나 WiFi 를 바꾸면 IP 가 달라집니다.
      192.168.219.50  ->  10.89.215.89  ->  172.30.1.28
    그러면 폰은 **낡은 주소**를 받아 연결하지 못하고, 사람은 "마스터를 못 찾는다"
    라는 증상만 보게 됩니다. 원인이 IP 변경이라는 걸 알기 어렵습니다.

    TCP 는 0.0.0.0 으로 열려 있어 새 IP 로도 들어옵니다. 문제는 광고뿐이므로
    광고만 갱신하면 됩니다.

    5초 간격으로 확인합니다. 폴링이지만 비용이 거의 없고(로컬 인터페이스 조회),
    네트워크 변경 이벤트를 크로스플랫폼으로 잡는 것보다 단순하고 확실합니다.
    """
    if aiozc is None or info is None:
        return
    known = set(local_ips())
    while True:
        await asyncio.sleep(interval_s)
        now = set(local_ips())
        if now == known or not now:
            continue

        log("", fh=fh)
        log(f"★ IP 가 바뀌었습니다: {', '.join(sorted(known))} → "
            f"{', '.join(sorted(now))}", fh=fh)
        known = now
        try:
            info.addresses = [socket.inet_aton(ip) for ip in sorted(now)]
            # 주소만 바뀌었으므로 갱신으로 충분합니다. 해제 후 재등록보다
            # 빠르고, 폰이 탐색 중일 때 서비스가 사라지는 순간이 없습니다.
            await aiozc.async_update_service(info)
            log(f"mDNS 광고 갱신 완료  ip={', '.join(sorted(now))}", fh=fh)
        except Exception as e:  # noqa: BLE001
            log(f"광고 갱신 실패 ({type(e).__name__}: {e}) — 해제 후 재등록 시도", fh=fh)
            try:
                await aiozc.async_unregister_service(info)
                await aiozc.async_register_service(info)
                log(f"재등록 완료  ip={', '.join(sorted(now))}", fh=fh)
            except Exception as e2:  # noqa: BLE001
                log(f"★ 재등록도 실패 ({type(e2).__name__}: {e2}). "
                    f"폰에서 IP 를 직접 입력하세요: {sorted(now)[0]}:{port}", fh=fh)
        log(f"폰에 직접 입력할 주소: {sorted(now)[0]}:{port}", fh=fh)
        with contextlib.suppress(OSError):
            (LOG_DIR.parent / "MASTER_ADDR.txt").write_text(
                f"{sorted(now)[0]}:{port}\n", encoding="utf-8")
        log("", fh=fh)


CONSOLE_HELP = """\
── 원격 촬영 조작 ──────────────────────────────
  s + Enter   녹화 시작 (대기 중인 모든 카메라)
  x + Enter   녹화 정지 (파일을 받아 세션 검사까지)
  l + Enter   카메라 목록
  h + Enter   이 도움말
───────────────────────────────────────────────"""


def start_console(m: "Master", loop: asyncio.AbstractEventLoop, fh) -> None:
    """
    콘솔 입력을 별도 스레드에서 읽습니다.

    asyncio 는 Windows 에서 표준입력을 비동기로 읽지 못하므로 스레드로 받아
    run_coroutine_threadsafe 로 이벤트 루프에 넘깁니다.
    """
    import threading

    keys = {"s": "start", "x": "stop", "l": "list"}

    def worker() -> None:
        log(CONSOLE_HELP, fh=fh)
        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                return
            k = line.strip().lower()
            if not k:
                continue
            if k == "h":
                log(CONSOLE_HELP, fh=fh)
                continue
            cmd = keys.get(k)
            if not cmd:
                log(f"모르는 키: {k!r}  (h 로 도움말)", fh=fh)
                continue
            fut = asyncio.run_coroutine_threadsafe(m.run_command(cmd), loop)
            try:
                ok, text, _ = fut.result()
            except Exception as e:  # noqa: BLE001
                log(f"★ 명령 실패: {e}", fh=fh)
                continue
            log(("" if ok else "★ ") + text, fh=fh)

    threading.Thread(target=worker, daemon=True, name="console").start()


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
        # ★ 현재 주소를 파일로도 남깁니다.
        #   장소를 옮길 때마다 "지금 주소가 뭐냐"를 로그에서 찾아야 했습니다.
        #   고정된 경로에 한 줄로 써 두면 바로 확인할 수 있습니다.
        with contextlib.suppress(OSError):
            (LOG_DIR.parent / "MASTER_ADDR.txt").write_text(
                f"{ips[0]}:{args.port}\n", encoding="utf-8")
    log("Ctrl+C 로 종료", fh=fh)
    log("", fh=fh)

    # IP 변경 감시. 장소를 옮기거나 WiFi 를 바꿔도 광고가 따라갑니다.
    watcher = asyncio.create_task(watch_ip_changes(aiozc, info, args.port, fh))

    # ★ 콘솔 키 입력으로 원격 촬영을 조작합니다.
    #   터미널에서 직접 켰을 때만 동작합니다. 백그라운드로 돌리면 입력이 없으므로
    #   그때는 `python server/ctl.py start` 로 조작합니다.
    if sys.stdin is not None and sys.stdin.isatty():
        start_console(m, asyncio.get_running_loop(), fh)
    else:
        log("콘솔 입력 없음 — 조작은  python server/ctl.py start | stop | list", fh=fh)

    try:
        await m.run()
    except asyncio.CancelledError:
        pass
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await watcher
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
