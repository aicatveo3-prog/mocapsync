#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MocapSync 유선 규약 v1 — 메시지 정의와 인코딩/디코딩.

docs/PROTOCOL.md 가 명세이고 이 파일이 그 파이썬 구현입니다.
iOS(Swift) 쪽은 같은 JSON 을 주고받아야 합니다.

형식: 줄바꿈으로 구분된 JSON (newline-delimited JSON), UTF-8.
한 줄 = 한 메시지. 알 수 없는 type 과 알 수 없는 필드는 무시합니다(전방 호환).
"""

from __future__ import annotations

import json
import time
from typing import Any

PROTOCOL_VERSION = 1

SERVICE_TYPE = "_mocapsync._tcp.local."
DEFAULT_PORT = 9001


# ── 시계 ─────────────────────────────────────────────────────────────────────

def now_ns() -> int:
    """
    PC 측 단조 시계. 나노초.

    time.monotonic_ns() 를 쓰는 이유:
      - 단조(monotonic). 시스템 시각 변경(NTP 보정, 사용자 조작)에 영향받지 않음
      - Windows 에서는 QueryPerformanceCounter 기반이라 해상도가 충분
    절대 time.time() 을 쓰지 마세요. 그건 벽시계라서 뒤로 갈 수 있습니다.
    """
    return time.monotonic_ns()


# ── 메시지 종류 ───────────────────────────────────────────────────────────────

class T:
    HELLO = "hello"
    HELLO_ACK = "hello_ack"
    TIME_REQ = "time_req"
    TIME_RESP = "time_resp"
    TIME_RESULT = "time_result"
    SCHEDULE_START = "schedule_start"
    START_ACK = "start_ack"
    START_NACK = "start_nack"
    STOP = "stop"
    STATUS = "status"
    ERROR = "error"
    # ── 파일 업로드 ─────────────────────────────────────────────────────────
    #
    # ★ 왜 규약에 넣는가
    #
    # 3단계까지 만든 영상과 사이드카를 PC 로 옮길 방법이 없었습니다.
    # USB 경로(iTunes 파일 공유 / pymobiledevice3)를 검토했지만
    #   · iTunes 는 사람이 매번 GUI 를 조작해야 하고
    #   · pymobiledevice3 는 Windows 에서 C 컴파일러를 요구해 설치가 막혔습니다
    # 어차피 4단계가 WiFi 업로드이므로 그걸 먼저 만듭니다.
    #
    # 무엇보다 마스터가 받은 사이드카를 Python 검증기에 바로 넣어 판정을 찍으면,
    # Swift/Python 두 구현이 실제로 같은 판정을 내는지 자동으로 확인됩니다.
    UPLOAD_BEGIN = "upload_begin"
    UPLOAD_READY = "upload_ready"
    UPLOAD_DONE = "upload_done"


# ── 인코딩 / 디코딩 ───────────────────────────────────────────────────────────

def encode(msg: dict[str, Any]) -> bytes:
    """메시지 하나를 한 줄 바이트로. separators 로 공백을 없애 크기를 줄입니다."""
    if "type" not in msg:
        raise ValueError(f"'type' 없는 메시지: {msg!r}")
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes | str) -> dict[str, Any]:
    """한 줄을 메시지로. 실패 시 ValueError."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    line = line.strip()
    if not line:
        raise ValueError("빈 줄")
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError(f"객체가 아님: {type(obj).__name__}")
    if "type" not in obj:
        raise ValueError("'type' 필드 없음")
    return obj


# ── 생성 헬퍼 (오타 방지용) ───────────────────────────────────────────────────

def hello(device_id: str, name: str, platform: str, model: str,
          os_version: str, app_version: str, clock: str) -> dict:
    return {
        "type": T.HELLO, "proto": PROTOCOL_VERSION,
        "deviceId": device_id, "name": name, "platform": platform,
        "model": model, "osVersion": os_version, "appVersion": app_version,
        "clock": clock,
    }


def hello_ack(server_id: str, impl: str, session_id: str | None = None) -> dict:
    return {
        "type": T.HELLO_ACK, "proto": PROTOCOL_VERSION,
        "serverId": server_id, "impl": impl, "sessionId": session_id,
    }


def time_req(seq: int, t1: int) -> dict:
    return {"type": T.TIME_REQ, "seq": seq, "t1": t1}


def time_resp(seq: int, t1: int, t2: int, t3: int) -> dict:
    return {"type": T.TIME_RESP, "seq": seq, "t1": t1, "t2": t2, "t3": t3}


def time_result(est, prof=None, config: str = "") -> dict:
    """
    est:  clocksync.SyncEstimate
    prof: clocksync.RttProfile | None

    ★ RTT 분포를 함께 실어 보냅니다.
    최소 RTT 하나로는 "물리적 바닥"과 "표본 부족"을 구분할 수 없고,
    분포가 슬레이브(폰) 안에만 있으면 사람이 매번 로그를 내보내 붙여야 해서
    디버깅 루프가 느려집니다. 마스터 콘솔에서 바로 보이게 합니다.

    ios/Sources/MocapSyncCore/WireProtocol.swift 의 TimeResultMsg 와
    키가 정확히 같아야 합니다.
    """
    import mocapsync.clocksync as _cs
    p = prof if prof is not None else _cs.EMPTY_RTT_PROFILE
    return {
        "type": T.TIME_RESULT,
        "offsetNs": est.offset_ns,
        "minRttNs": est.min_rtt_ns,
        "uncertaintyNs": est.uncertainty_ns,
        "spreadNs": est.spread_ns,
        "samplesTotal": est.samples_total,
        "samplesUsed": est.samples_used,
        "samplesRejected": est.samples_rejected,
        "rttP0Ns": p.p0_ns,
        "rttP10Ns": p.p10_ns,
        "rttP50Ns": p.p50_ns,
        "rttP90Ns": p.p90_ns,
        "rttP100Ns": p.p100_ns,
        "rttShape": p.shape,
        # ★ 이 숫자를 만든 측정 설정. 사람이 읽는 기록용.
        #   사용자가 "무슨 설정으로 쟀는지" 말해주지 않아도 로그에 남습니다.
        "config": config,
    }


def upload_begin(session_id: str, name: str, size: int, sha256: str = "") -> dict:
    """
    파일 하나를 보내겠다는 예고.

    이 줄 다음에 **정확히 size 바이트의 원본 데이터**가 옵니다.
    줄 단위 JSON 규약 안에 이진 데이터를 섞는 방법입니다 — 수신측이
    readexactly(size) 로 정확히 그만큼만 읽으면 다시 줄 모드로 돌아옵니다.

    base64 로 감싸지 않는 이유: 영상이 수십 MB 라 33% 증가가 그대로 전송 시간과
    메모리에 반영됩니다. 핫스팟처럼 느린 링크에서는 체감이 큽니다.
    """
    return {"type": T.UPLOAD_BEGIN, "sessionId": session_id,
            "name": name, "size": size, "sha256": sha256}


def upload_ready(name: str) -> dict:
    """받을 준비가 됐다. 이제 원본 바이트를 보내라."""
    return {"type": T.UPLOAD_READY, "name": name}


def upload_done(name: str, size: int, ok: bool, message: str = "") -> dict:
    return {"type": T.UPLOAD_DONE, "name": name, "size": size,
            "ok": ok, "message": message}


def schedule_start(session_id: str, start_at_master_ns: int, *,
                   target_fps: int = 60, width: int = 1920, height: int = 1080,
                   lock_ae: bool = True, lock_awb: bool = True,
                   lock_focus: bool = True, stabilization: str = "off",
                   max_exposure_ns: int = 2_000_000) -> dict:
    return {
        "type": T.SCHEDULE_START, "sessionId": session_id,
        "startAtMasterNs": start_at_master_ns,
        "targetFps": target_fps, "width": width, "height": height,
        "lockAe": lock_ae, "lockAwb": lock_awb, "lockFocus": lock_focus,
        "stabilization": stabilization, "maxExposureNs": max_exposure_ns,
    }


def start_ack(session_id: str, start_at_slave_ns: int, lead_ns: int) -> dict:
    return {"type": T.START_ACK, "sessionId": session_id,
            "startAtSlaveNs": start_at_slave_ns, "leadNs": lead_ns}


def start_nack(session_id: str, reason: str, lead_ns: int) -> dict:
    return {"type": T.START_NACK, "sessionId": session_id,
            "reason": reason, "leadNs": lead_ns}


def stop(session_id: str) -> dict:
    return {"type": T.STOP, "sessionId": session_id}


def status(state: str, *, battery: float | None = None,
           thermal: str | None = None, frames_captured: int = 0,
           free_bytes: int | None = None) -> dict:
    m = {"type": T.STATUS, "state": state, "framesCaptured": frames_captured}
    if battery is not None:
        m["battery"] = battery
    if thermal is not None:
        m["thermal"] = thermal
    if free_bytes is not None:
        m["freeBytes"] = free_bytes
    return m


def error(code: str, message: str) -> dict:
    return {"type": T.ERROR, "code": code, "message": message}
