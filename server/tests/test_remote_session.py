"""
원격 촬영 전 과정을 실제 TCP 소켓 위에서 시험합니다.

마스터 1개 + 가짜 폰 2대(시계를 일부러 다르게 틀어 둠)를 같은 프로세스에 띄우고
  원격 대기 -> 녹화 시작 -> 녹화 -> 정지 -> 업로드 -> 세션 검사
까지 돌립니다.

★ 왜 이 테스트가 중요한가
개발자는 아이폰 두 대를 직접 돌려볼 수 없습니다. 실기기에서 "한 대만 안 찍혔다"가
나오면 원인이 규약인지, 마스터인지, 앱인지 알 수 없습니다. 이 테스트가 통과하면
규약과 마스터는 맞다는 것이 보장되므로, 실기기 실패는 앱 쪽으로 좁혀집니다.
"""
from __future__ import annotations

import argparse
import asyncio
import json

import pytest

import master as M
import slave_sim as S
from mocapsync import clocksync as cs
from mocapsync import protocol as P
from mocapsync import sidecar as sc

MS = cs.NS_PER_MS


def sim_args(name: str, dev: str, offset_ms: float, seed: int) -> argparse.Namespace:
    return argparse.Namespace(
        fake_offset_ms=offset_ms, device_id=dev, name=name, seed=seed,
        probes=30, best_k=1, burst_size=0, burst_gap_ms=0, burst_warmup=0,
        warmup=2, gap_ms=0.0, delay_ms=0.0, jitter_ms=0.0, asym_ms=0.0,
        precise_wait=True, spin_margin_ns=cs.DEFAULT_SPIN_MARGIN_NS,
        exit_after_sync=False, remote=True, video_kb=4)


async def wait_for(cond, timeout=10.0):
    t = asyncio.get_running_loop().time()
    while not cond():
        if asyncio.get_running_loop().time() - t > timeout:
            raise TimeoutError("조건이 만족되지 않았습니다")
        await asyncio.sleep(0.02)


async def run_session(tmp_path, record_s: float = 1.0):
    m = M.Master(port=0, auto_start_after=None, fh=None)
    m.upload_root = tmp_path
    server = await asyncio.start_server(m.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    a = S.SlaveSim(sim_args("camA", "CAMA00000001", 37.5, 1))
    b = S.SlaveSim(sim_args("camB", "CAMB00000002", -812.25, 2))
    ta = asyncio.create_task(a.run("127.0.0.1", port))
    tb = asyncio.create_task(b.run("127.0.0.1", port))
    try:
        await wait_for(lambda: len(m.remote_cameras()) == 2
                       and all(s.state == P.RemoteState.READY for s in m.remote_cameras()))
        ok, text, sid = await m.run_command("start", {"leadMs": 400})
        assert ok, text
        await asyncio.sleep(record_s)
        ok2, text2, sid2 = await m.run_command("stop", {"timeoutS": 20})
        assert ok2, text2
        assert sid2 == sid
        return m, sid, (a, b), text, text2
    finally:
        ta.cancel()
        tb.cancel()
        server.close()
        for t in (ta, tb):
            with pytest.raises(BaseException):
                await t


def test_two_cameras_record_and_upload(tmp_path):
    m, sid, (a, b), start_text, stop_text = asyncio.run(run_session(tmp_path))

    assert "2/2" in start_text
    assert "2/2" in stop_text

    d = tmp_path / sid
    names = sorted(p.name for p in d.iterdir())
    assert names == ["CAMA00000001.json", "CAMA00000001.mov",
                     "CAMB00000002.json", "CAMB00000002.mov"]

    cams = [sc.Sidecar.load(d / f"{x}.json") for x in ("CAMA00000001", "CAMB00000002")]
    for c in cams:
        assert c.session_id == sid
        assert c.is_usable, c.validation_report()
        assert c.frame_count > 30


def test_offsets_recovered_per_camera(tmp_path):
    """각 폰이 자기 시계의 가짜 오프셋을 되찾았는지 (추정 오차 <= 보장 상한)."""
    m, sid, (a, b), *_ = asyncio.run(run_session(tmp_path))
    for sim in (a, b):
        err = abs(sim.offset_ns - sim.fake_offset_ns)
        assert err <= sim.est.uncertainty_ns + 1


def test_first_frames_within_one_frame_on_common_timeline(tmp_path):
    """
    ★ 핵심 성질.
    두 카메라의 시계는 850 ms 가까이 어긋나 있지만, 공통 시간축(마스터 시계)으로
    옮기면 첫 프레임이 한 프레임(16.667 ms) 안에 모여야 합니다.
    예약 시작 + 클럭 오프셋이 제대로 맞물렸다는 증거입니다.
    """
    m, sid, *_ = asyncio.run(run_session(tmp_path))
    d = tmp_path / sid
    cams = [sc.Sidecar.load(p) for p in sorted(d.glob("*.json"))]
    firsts = [c.timestamps_master_ns[0] for c in cams]
    spread = max(firsts) - min(firsts)
    assert spread < S.FRAME_NS + 2 * max(c.clock_uncertainty_ns for c in cams)

    # 그리고 모두 예약 시각 **이후**여야 합니다 (먼저 찍으면 논리 오류)
    for c in cams:
        assert c.timestamps_master_ns[0] >= c.requested_start_at_master_ns - c.clock_uncertainty_ns


def test_session_check_passes(tmp_path):
    m, sid, *_ = asyncio.run(run_session(tmp_path))
    cams = [sc.Sidecar.load(p) for p in sorted((tmp_path / sid).glob("*.json"))]
    issues = sc.check_session(cams)
    assert not [i for i in issues if i.severity == "fatal"], issues
    assert any(i.code == "overlap" for i in issues)


def test_start_without_cameras_is_refused(tmp_path):
    async def go():
        m = M.Master(port=0, auto_start_after=None, fh=None)
        m.upload_root = tmp_path
        return await m.run_command("start")
    ok, text, _ = asyncio.run(go())
    assert not ok
    assert "없습니다" in text


def test_stop_without_session_is_refused(tmp_path):
    async def go():
        m = M.Master(port=0, auto_start_after=None, fh=None)
        return await m.run_command("stop")
    ok, text, _ = asyncio.run(go())
    assert not ok


def test_unknown_command(tmp_path):
    async def go():
        m = M.Master(port=0, auto_start_after=None, fh=None)
        return await m.run_command("dance")
    ok, text, _ = asyncio.run(go())
    assert not ok


def test_ctl_from_loopback_is_accepted(tmp_path):
    """ctl.py 와 같은 방식으로 루프백에서 list 를 보냅니다."""
    async def go():
        m = M.Master(port=0, auto_start_after=None, fh=None)
        m.upload_root = tmp_path
        server = await asyncio.start_server(m.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(P.encode(P.ctl("list")))
        await w.drain()
        resp = P.decode(await r.readline())
        w.close()
        server.close()
        return resp
    resp = asyncio.run(go())
    assert resp["type"] == P.T.CTL_RESULT
    assert resp["ok"] is True
    # 조작 연결은 카메라 목록에 끼면 안 됩니다
    assert "원격 카메라 0대" in resp["message"]


def test_frames_start_after_scheduled_instant():
    """시뮬레이터 프레임 규칙: 예약 시각 이후의 첫 프레임부터 (아이폰 Recorder 와 같음)."""
    sim = S.SlaveSim(sim_args("x", "X", 0, 3))
    start = 10_000_000_000
    fr = sim._frames(start, start + 1_000_000_000)
    assert fr[0][1] >= start
    assert fr[0][1] - start < S.FRAME_NS
    assert [f[0] for f in fr] == list(range(len(fr)))


def test_remote_protocol_keys():
    assert P.sync_request("x") == {"type": "sync_request", "reason": "x"}
    d = P.record_done("S1", ["a.json", "a.mov"], 120, True, [])
    assert set(d) == {"type", "sessionId", "files", "frames", "usable", "fatal"}
    assert P.RemoteState.READY == "remote_ready"
    assert json.loads(P.encode(d))["frames"] == 120
