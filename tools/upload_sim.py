"""
업로드 경로를 PC 안에서 끝까지 시험합니다.

★ 왜 필요한가
개발자는 아이폰에서 코드를 돌릴 수 없습니다. 업로드가 안 되면 원인이
(가) 규약 (나) 마스터 수신 로직 (다) 아이폰 송신 코드 중 어디인지 알 수 없습니다.
이 스크립트가 (가)(나)를 PC 에서 미리 검증해 주므로, 실기기에서 실패하면
원인이 (다)로 좁혀집니다.

또한 마스터가 사이드카를 받아 Python 검증기로 판정하는 흐름도 여기서 확인됩니다.

사용:
    python tools/upload_sim.py --host 127.0.0.1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from mocapsync import protocol as P  # noqa: E402


def make_sidecar(frame_count: int = 600) -> dict:
    """실기기가 만들 것과 같은 모양의 사이드카."""
    base = 1_000_000_000_000
    step = 16_666_666
    return {
        "schemaVersion": 1,
        "deviceId": "SIMDEV000001",
        "deviceName": "sim-iPhone",
        "model": "iPhone12,1",
        "osVersion": "17.5.1",
        "appVersion": "sim-0.1.0",
        "sessionId": "S20260925-SIM",
        "role": "slave",
        "clock": "CLOCK_UPTIME_RAW",
        "clockOffsetNs": 61_951_927_450_000,
        "clockUncertaintyNs": 3_286_000,
        "clockMinRttNs": 6_571_000,
        "clockMeasuredAtNs": base - 2_000_000_000,
        "sleepAtSyncNs": 1_234_567,
        "sleepAtRecordStartNs": 1_234_609,
        "timestampSource": "CMSampleBufferPresentationTimeStamp",
        "timestampDomainDeltaNs": -458,
        "targetFps": 60,
        "width": 1920,
        "height": 1080,
        "cameraDeviceType": "AVCaptureDeviceTypeBuiltInWideAngleCamera",
        "fieldOfViewDeg": 69.7,
        "isBinned": False,
        "exposureDurationNs": 2_000_000,
        "iso": 640.0,
        "lensPosition": 0.42,
        "focusLocked": True,
        "whiteBalanceLocked": True,
        "exposureLocked": True,
        "stabilization": "off",
        "requestedStartAtMasterNs": base + 61_951_927_450_000,
        "requestedStartAtSlaveNs": base,
        "firstFramePtsNs": base,
        "droppedFrameCount": 0,
        "thermalAtStart": "nominal",
        "thermalAtEnd": "fair",
        "batteryAtStart": 0.8,
        "batteryAtEnd": 0.78,
        "frames": [[i, base + i * step] for i in range(frame_count)],
    }


async def send_one(reader, writer, session_id: str, name: str, payload: bytes) -> bool:
    print(f"  {name}: {len(payload) / 1_048_576:.3f} MB 전송")
    writer.write(P.encode(P.upload_begin(session_id, name, len(payload))))
    await writer.drain()

    resp = P.decode(await reader.readline())
    if resp.get("type") != P.T.UPLOAD_READY:
        print(f"  ★ ready 가 오지 않았습니다: {resp}")
        return False

    # 아이폰과 같게 256KB 씩 나눠 보냅니다
    step = 256 * 1024
    for off in range(0, len(payload), step):
        writer.write(payload[off:off + step])
        await writer.drain()

    done = P.decode(await reader.readline())
    if done.get("type") != P.T.UPLOAD_DONE or not done.get("ok"):
        print(f"  ★ 완료 응답 이상: {done}")
        return False
    print(f"  수신 확인 {done.get('size')} 바이트")
    return len(payload) == int(done.get("size", -1))


async def main_async(a: argparse.Namespace) -> int:
    reader, writer = await asyncio.open_connection(a.addr, a.tcp)
    print(f"연결됨 {a.addr}:{a.tcp}")

    writer.write(P.encode(P.hello(
        device_id="SIMDEV000001", name="upload-sim", platform="pysim",
        model=platform.machine() or "pc", os_version=platform.release(),
        app_version="sim-0.1.0", clock="time.monotonic_ns")))
    await writer.drain()
    ack = P.decode(await reader.readline())
    if ack.get("type") != P.T.HELLO_ACK:
        print(f"★ hello 실패: {ack}")
        return 2
    print("hello_ack 수신")

    sid = "S20260925-SIM"
    ok = True

    # ★ 사이드카를 먼저 (아이폰과 같은 순서)
    side = json.dumps(make_sidecar(a.frames), ensure_ascii=False,
                      indent=2, sort_keys=True).encode("utf-8")
    ok &= await send_one(reader, writer, sid, "SIMDEV000001.json", side)

    # 영상 대역 — 실제 영상 대신 같은 크기의 더미
    if a.video_mb > 0:
        dummy = bytes(a.video_mb * 1_048_576)
        ok &= await send_one(reader, writer, sid, "SIMDEV000001.mov", dummy)

    # 경로 탈출 방어 확인
    if a.check_escape:
        print("경로 탈출 방어 확인: ../../evil.txt")
        ok &= await send_one(reader, writer, sid, "../../evil.txt", b"x" * 16)
        escaped = (ROOT / "evil.txt").exists() or (ROOT.parent / "evil.txt").exists()
        if escaped:
            print("  ★★ 실패: 상위 경로에 파일이 생겼습니다")
            ok = False
        else:
            print("  OK: 상위 경로로 나가지 못했습니다")

    writer.close()
    with __import__("contextlib").suppress(Exception):
        await writer.wait_closed()

    print()
    print("전체 결과:", "성공" if ok else "★ 실패")
    return 0 if ok else 3


def main() -> int:
    ap = argparse.ArgumentParser(description="업로드 경로 시뮬레이터")
    # 'host'/'port' 라는 이름을 피합니다 (일부 셸 래퍼가 거부)
    ap.add_argument("--addr", default="127.0.0.1", help="마스터 주소")
    ap.add_argument("--tcp", type=int, default=P.DEFAULT_PORT)
    ap.add_argument("--frames", type=int, default=600)
    ap.add_argument("--video-mb", type=int, default=2,
                    help="영상 대역 더미 크기(MB). 0 이면 생략")
    ap.add_argument("--check-escape", action="store_true", default=True)
    a = ap.parse_args()
    return asyncio.run(main_async(a))


if __name__ == "__main__":
    raise SystemExit(main())
