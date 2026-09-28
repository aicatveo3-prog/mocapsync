"""
대시보드 시험 — 웹 서버의 보안 규칙, 촬영 목록 요약, 3D 작업 실행, 녹화 중 화면 상태.

★ 보안 규칙이 핵심입니다. 대시보드는 녹화를 시작·정지하고 폴더를 여는 조작을 받습니다.
  같은 WiFi 의 다른 기기나, 브라우저에 열린 다른 웹사이트가 이걸 부르면 안 됩니다.
"""
from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest

import master as M
from mocapsync import jobs as J
from mocapsync import sessions_index as SI
from mocapsync import webui as W

from test_remote_session import run_session
from test_sidecar import make_frames, valid_dict

WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"


# ── 웹 서버 ───────────────────────────────────────────────────────────────────

async def raw_request(port: int, text: str) -> tuple[int, dict, bytes]:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(text.encode("utf-8"))
    await w.drain()
    data = await r.read()
    w.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    return status, headers, body


def req(method, path, port, host=None, headers=None, body=b""):
    host = host or f"127.0.0.1:{port}"
    hs = {"Host": host, **(headers or {})}
    if body:
        hs["Content-Length"] = str(len(body))
    head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hs.items())
    return head + "\r\n" + body.decode("utf-8")


POST_OK = {"X-MocapSync": "1", "Content-Type": "application/json"}


async def with_ui(fn):
    calls = []

    async def start(body):
        calls.append(body)
        return 202, {"ok": True, "message": "보냈습니다"}

    ui = W.WebUI(WEBUI_DIR, lambda: {"phase": "idle", "name": "<script>x</script>"},
                 getters={"sessions": lambda: {"sessions": []}},
                 actions={"start": start}, port=0)
    await ui.start()
    try:
        return await fn(ui, calls)
    finally:
        await ui.close()


def run(fn):
    return asyncio.run(with_ui(fn))


def test_ui_binds_loopback_only():
    async def t(ui, _):
        return ui.server.sockets[0].getsockname()[0]
    assert run(t) == "127.0.0.1"


def test_page_and_state_are_served_with_csp():
    async def t(ui, _):
        page = await raw_request(ui.port, req("GET", "/", ui.port))
        state = await raw_request(ui.port, req("GET", "/api/state", ui.port))
        js = await raw_request(ui.port, req("GET", "/app.js", ui.port))
        return page, state, js
    (s1, h1, b1), (s2, _, b2), (s3, h3, _) = run(t)
    assert s1 == 200 and b"MocapSync" in b1
    assert "script-src 'self'" in h1["content-security-policy"]
    assert h1["x-content-type-options"] == "nosniff"
    assert s2 == 200 and json.loads(b2)["phase"] == "idle"
    assert s3 == 200 and h3["content-type"].startswith("text/javascript")


def test_foreign_host_header_is_refused():
    """DNS rebinding 방어: 다른 이름으로 들어온 요청은 읽기도 거부합니다."""
    async def t(ui, _):
        return await raw_request(ui.port, req("GET", "/api/state", ui.port, host="evil.example"))
    assert run(t)[0] == 403


def test_post_without_dashboard_header_is_refused():
    """다른 사이트가 폼이나 단순 fetch 로 몰래 '녹화 시작'을 보내는 경우."""
    async def t(ui, calls):
        s = await raw_request(ui.port, req("POST", "/api/start", ui.port,
                                           headers={"Content-Type": "application/json"}, body=b"{}"))
        return s[0], calls
    status, calls = run(t)
    assert status == 403 and calls == []


def test_post_from_other_origin_is_refused():
    async def t(ui, calls):
        s = await raw_request(ui.port, req("POST", "/api/start", ui.port,
                                           headers={**POST_OK, "Origin": "http://evil.example"}, body=b"{}"))
        return s[0], calls
    status, calls = run(t)
    assert status == 403 and calls == []


def test_post_needs_json():
    async def t(ui, calls):
        s = await raw_request(ui.port, req("POST", "/api/start", ui.port,
                                           headers={"X-MocapSync": "1", "Content-Type": "text/plain"},
                                           body=b"{}"))
        return s[0], calls
    status, calls = run(t)
    assert status == 400 and calls == []


def test_valid_post_reaches_action():
    async def t(ui, calls):
        s = await raw_request(ui.port, req("POST", "/api/start", ui.port,
                                           headers={**POST_OK, "Origin": f"http://127.0.0.1:{ui.port}"},
                                           body=b'{"x": 1}'))
        return s[0], calls
    status, calls = run(t)
    assert status == 202 and calls == [{"x": 1}]


def test_unknown_paths_and_traversal_are_404():
    async def t(ui, _):
        return [
            (await raw_request(ui.port, req("GET", p, ui.port)))[0]
            for p in ("/../master.py", "/webui/app.js", "/api/nothing", "/%2e%2e/master.py")
        ]
    assert run(t) == [404, 404, 404, 404]


def test_oversized_body_is_refused():
    async def t(ui, calls):
        head = (f"POST /api/start HTTP/1.1\r\nHost: 127.0.0.1:{ui.port}\r\n"
                f"X-MocapSync: 1\r\nContent-Type: application/json\r\n"
                f"Content-Length: {W.MAX_BODY + 1}\r\n\r\n")
        s = await raw_request(ui.port, head)
        return s[0], calls
    status, calls = run(t)
    assert status == 413 and calls == []


def test_js_never_uses_innerhtml():
    """폰 이름 같은 네트워크 글자가 HTML 로 해석되지 않게 (XSS)."""
    js = (WEBUI_DIR / "app.js").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in js.splitlines() if not ln.strip().startswith("//"))
    assert "innerHTML" not in code and "outerHTML" not in code and "insertAdjacentHTML" not in code
    assert "<script>" not in (WEBUI_DIR / "index.html").read_text(encoding="utf-8").replace(
        '<script src="app.js"></script>', "")


# ── 촬영 목록 요약 ────────────────────────────────────────────────────────────

def make_upload(root: Path, sid="S20260927-221904", offsets_ms=(0.0, 7.083)):
    d = root / sid
    d.mkdir(parents=True)
    for i, off in enumerate(offsets_ms):
        sd = valid_dict(600)
        sd["deviceId"] = f"DEV{i:09d}"
        sd["sessionId"] = sid
        sd["frames"] = make_frames(600, start_ns=1_000_000_000_000 + int(off * 1e6))
        sd["firstFramePtsNs"] = sd["frames"][0][1]
        sd["requestedStartAtSlaveNs"] = 1_000_000_000_000
        sd["cameraDeviceType"] = "AVCaptureDeviceTypeBuiltInUltraWideCamera"
        (d / f"DEV{i:09d}.json").write_text(json.dumps(sd), encoding="utf-8")
        (d / f"DEV{i:09d}.mov").write_bytes(b"x" * 1000)
    return d


def test_summary_reports_first_frame_spread(tmp_path):
    s = SI.summarize_session(make_upload(tmp_path))
    assert s["spreadMs"] == pytest.approx(7.083, abs=0.001)
    assert s["sameFrame"] is True
    assert s["usable"] is True
    assert [c["lens"] for c in s["cameras"]] == ["초광각 0.5x", "초광각 0.5x"]
    assert s["videos"] == 2


def test_summary_flags_spread_over_one_frame(tmp_path):
    s = SI.summarize_session(make_upload(tmp_path, offsets_ms=(0.0, 20.0)))
    assert s["sameFrame"] is False


def test_broken_sidecar_does_not_crash_summary(tmp_path):
    d = make_upload(tmp_path)
    (d / "BROKEN.json").write_text("{ not json", encoding="utf-8")
    s = SI.summarize_session(d)
    assert any(c.get("error") for c in s["cameras"])
    assert s["usable"] is False


def test_unsafe_session_names_are_rejected(tmp_path):
    idx = SI.SessionIndex(tmp_path, tmp_path / "work")
    for bad in ("..", "../x", "a/b", "a\\b", "", "C:x"):
        assert idx.summary(bad) is None


def test_index_lists_newest_first_and_reads_3d_result(tmp_path):
    make_upload(tmp_path / "up", "S20260927-100000")
    make_upload(tmp_path / "up", "S20260927-110000")
    proj = tmp_path / "work" / "sessions" / "S20260927-100000"
    (proj / "pose-3d").mkdir(parents=True)
    (proj / "pose-3d" / "x_filt_butterworth.trc").write_text("", encoding="utf-8")
    (proj / "mocapsync_run.json").write_text(json.dumps(
        {"exit_code": 0, "verdict": "3D 완료", "summary": {"reproj_px": 8.6}}), encoding="utf-8")
    items = SI.SessionIndex(tmp_path / "up", tmp_path / "work").list()
    assert [i["sid"] for i in items] == ["S20260927-110000", "S20260927-100000"]
    assert items[0]["result"] is None
    assert items[1]["result"]["reprojPx"] == 8.6
    assert items[1]["result"]["trc"] == "x_filt_butterworth.trc"


def test_index_notices_partial_upload(tmp_path):
    d = make_upload(tmp_path)
    idx = SI.SessionIndex(tmp_path, tmp_path / "work")
    assert idx.summary(d.name)["partial"] is False
    (d / "DEV000000001.mov.part").write_bytes(b"x")
    assert idx.summary(d.name)["partial"] is True       # 캐시가 바뀐 폴더를 알아챕니다


# ── 3D 작업 실행 ──────────────────────────────────────────────────────────────

FAKE_RUNNER = textwrap.dedent('''
    import sys, time
    print(" [1/7] 입력 찾기", flush=True)
    # tqdm 처럼 줄바꿈 없이 \\r 만 쓰는 긴 진행 막대 (64 KB 넘게)
    sys.stdout.write("".join(f"\\r {i}%|###| {i}/1000 [00:01<00:00, 30.0it/s]" for i in range(3000)))
    sys.stdout.flush()
    print("", flush=True)
    print(" [4/7] 2D 자세 추정", flush=True)
    print(" [7/7] 요약", flush=True)
    sys.exit(int(sys.argv[sys.argv.index("--code") + 1]) if "--code" in sys.argv else 0)
''')


def _run_job(tmp_path, code: int):
    script = tmp_path / "tools" / "fake_run.py"
    script.parent.mkdir()
    script.write_text(FAKE_RUNNER, encoding="utf-8")

    async def go():
        jr = J.JobRunner(Path(sys.executable), script, tmp_path / "work", ["--code", str(code)])
        ok, _ = jr.start("S20260927-221904")
        assert ok
        ok2, msg2 = jr.start("S20260927-221904")
        assert not ok2 and "처리 중" in msg2           # 한 번에 하나만
        while jr.running:
            await asyncio.sleep(0.05)
        return jr.snapshot()
    return asyncio.run(go())


def test_job_parses_steps_and_survives_progress_bar(tmp_path):
    snap = _run_job(tmp_path, 10)
    assert snap["state"] == "done" and snap["exitCode"] == 10
    assert (snap["step"], snap["steps"]) == (7, 7)
    assert not any("it/s]" in ln for ln in snap["log"])
    assert "3D 전까지" in snap["verdict"]


def test_job_failure_is_reported(tmp_path):
    snap = _run_job(tmp_path, 3)
    assert snap["state"] == "failed" and "실패" in snap["verdict"]


def test_job_refuses_unsafe_sid_and_missing_python(tmp_path):
    async def go():
        jr = J.JobRunner(Path(sys.executable), tmp_path / "x.py", tmp_path)
        a = jr.start("../etc")
        jr2 = J.JobRunner(None, tmp_path / "x.py", tmp_path)
        b = jr2.start("S1")
        return a, b
    a, b = asyncio.run(go())
    assert a[0] is False and b[0] is False and "Pose2Sim" in b[1]


# ── 마스터 화면 상태 (가짜 폰 2대로 실제 녹화) ────────────────────────────────

def test_dashboard_state_after_real_session(tmp_path):
    m, sid, _, _, _ = asyncio.run(run_session(tmp_path))
    st = m.dashboard_state()
    assert st["phase"] == "idle" and st["session"] is None
    assert st["lastMessage"]["ok"] is True and st["lastMessage"]["cmd"] == "stop"
    r = st["lastResult"]
    assert r["sid"] == sid and len(r["cameras"]) == 2 and r["missing"] == []
    assert r["spreadMs"] is not None and r["spreadMs"] < 1000 / 60
    assert st["uploads"] == []
    json.dumps(st, ensure_ascii=False, default=str)       # 화면으로 보낼 수 있어야 함


def test_ui_actions_refuse_when_not_possible(tmp_path):
    async def go():
        m = M.Master(port=0, auto_start_after=None, fh=None)
        m.upload_root = tmp_path
        a = await m.ui_start({})
        b = await m.ui_stop({})
        c = await m.ui_make3d({"sid": "../x"})
        d = await m.ui_open({"sid": "S1", "what": "C:/Windows"})
        return a, b, c, d
    a, b, c, d = asyncio.run(go())
    assert a[0] == 409 and "대기 중인 폰" in a[1]["message"]
    assert b[0] == 409
    assert c[0] == 404
    assert d[0] == 400
