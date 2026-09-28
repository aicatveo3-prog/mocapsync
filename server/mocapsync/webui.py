"""
마스터 대시보드용 아주 작은 웹 서버 (표준 라이브러리만).

★ 이 PC 에서만 열립니다
 - 127.0.0.1 에만 붙습니다. 같은 WiFi 의 다른 기기는 접속 자체가 안 됩니다.
 - Host 헤더가 127.0.0.1 / localhost 가 아니면 거부합니다. (DNS rebinding: 악성 웹페이지가
   자기 도메인을 127.0.0.1 로 돌려 이 서버를 부르는 공격을 막습니다)
 - 조작(POST)은 `X-MocapSync: 1` 헤더와 JSON 본문을 요구합니다. 다른 웹사이트가 브라우저를
   시켜 몰래 "녹화 시작"을 보내려면 이 헤더를 붙여야 하는데, 그러면 브라우저가 CORS 사전
   확인(OPTIONS)을 먼저 보내고 이 서버는 허락하지 않으므로 막힙니다.
 - 인증(비밀번호)은 없습니다. 이 PC 를 쓰는 사람은 조작할 수 있습니다.

폰과 마스터 사이의 연결(TCP 9001)은 이것과 별개입니다.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

MAX_HEADER = 16 * 1024
MAX_BODY = 64 * 1024

STATIC_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}

# 인라인 스크립트·외부 자원을 막습니다. 폰 이름처럼 네트워크에서 온 글자가 화면에
# 들어가므로, 혹시 이스케이프를 빠뜨려도 스크립트가 실행되지 않게 하는 두 번째 방어선입니다.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

Action = Callable[[dict], Awaitable[tuple[int, dict]]]


@dataclass
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


def _response(status: int, body: bytes, ctype: str, extra: dict | None = None) -> bytes:
    reason = {200: "OK", 202: "Accepted", 400: "Bad Request", 403: "Forbidden",
              404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
              413: "Payload Too Large", 500: "Internal Server Error"}.get(status, "OK")
    head = {
        "Content-Type": ctype,
        "Content-Length": str(len(body)),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Connection": "close",
    }
    head.update(extra or {})
    lines = [f"HTTP/1.1 {status} {reason}"] + [f"{k}: {v}" for k, v in head.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def json_response(status: int, obj) -> bytes:
    return _response(status, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                     "application/json; charset=utf-8")


class WebUI:
    """
    get_state : 1초마다 불리는 화면 상태 (dict)
    getters   : 추가 GET /api/<이름> (예: sessions)
    actions   : POST /api/<이름>  본문 dict → (상태코드, dict)
    """

    def __init__(self, static_dir: Path, get_state: Callable[[], dict],
                 getters: dict[str, Callable[[], object]] | None = None,
                 actions: dict[str, Action] | None = None, host: str = "127.0.0.1",
                 port: int = 8765):
        self.static_dir = Path(static_dir)
        self.get_state = get_state
        self.getters = getters or {}
        self.actions = actions or {}
        self.host = host
        self.port = port
        self.server: asyncio.base_events.Server | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self.server:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()

    async def _read(self, reader: asyncio.StreamReader) -> Request | int:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        except asyncio.LimitOverrunError:
            return 413
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            return 400
        if len(head) > MAX_HEADER:
            return 413
        try:
            text = head.decode("latin-1")
            first, *rest = text.split("\r\n")
            method, target, _ = first.split(" ", 2)
        except ValueError:
            return 400
        headers = {}
        for ln in rest:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        n = int(headers.get("content-length", "0") or 0)
        if n < 0 or n > MAX_BODY:
            return 413
        body = await reader.readexactly(n) if n else b""
        return Request(method.upper(), target.split("?", 1)[0], headers, body)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req = await self._read(reader)
            if isinstance(req, int):
                out = json_response(req, {"ok": False, "message": "요청을 읽을 수 없습니다"})
            else:
                out = await self._route(req)
            writer.write(out)
            await writer.drain()
        except Exception as e:  # noqa: BLE001 — 화면 요청 하나가 마스터를 죽이면 안 됩니다
            with contextlib.suppress(Exception):
                writer.write(json_response(500, {"ok": False, "message": f"{type(e).__name__}: {e}"}))
                await writer.drain()
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _route(self, req: Request) -> bytes:
        if req.headers.get("host", "") not in self.allowed_hosts():
            return json_response(403, {"ok": False, "message": "이 PC 의 주소로만 열 수 있습니다"})

        if req.method == "GET":
            if req.path in ("/", "/index.html"):
                return self._static("index.html")
            if req.path.lstrip("/") in STATIC_TYPES:
                return self._static(req.path.lstrip("/"))
            if req.path == "/api/state":
                return json_response(200, self.get_state())
            name = req.path.removeprefix("/api/")
            if req.path.startswith("/api/") and name in self.getters:
                return json_response(200, self.getters[name]())
            return json_response(404, {"ok": False, "message": "없는 주소입니다"})

        if req.method == "POST":
            if req.headers.get("x-mocapsync") != "1":
                return json_response(403, {"ok": False, "message": "대시보드에서만 조작할 수 있습니다"})
            origin = req.headers.get("origin")
            if origin and origin.removeprefix("http://") not in self.allowed_hosts():
                return json_response(403, {"ok": False, "message": "다른 사이트에서 온 요청입니다"})
            if not req.headers.get("content-type", "").startswith("application/json"):
                return json_response(400, {"ok": False, "message": "JSON 이 필요합니다"})
            name = req.path.removeprefix("/api/")
            fn = self.actions.get(name) if req.path.startswith("/api/") else None
            if fn is None:
                return json_response(404, {"ok": False, "message": "없는 조작입니다"})
            try:
                body = json.loads(req.body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return json_response(400, {"ok": False, "message": "JSON 을 읽을 수 없습니다"})
            if not isinstance(body, dict):
                return json_response(400, {"ok": False, "message": "JSON 객체가 필요합니다"})
            status, obj = await fn(body)
            return json_response(status, obj)

        return json_response(405, {"ok": False, "message": "GET / POST 만 됩니다"})

    def _static(self, name: str) -> bytes:
        p = self.static_dir / name
        if name not in STATIC_TYPES or not p.is_file():
            return json_response(404, {"ok": False, "message": "화면 파일이 없습니다"})
        extra = {"Content-Security-Policy": CSP} if name.endswith(".html") else None
        return _response(200, p.read_bytes(), STATIC_TYPES[name], extra)
