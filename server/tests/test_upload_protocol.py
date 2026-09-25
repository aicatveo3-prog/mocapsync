"""
업로드 규약의 키를 못박습니다.

ios/Sources/MocapSyncCore/WireProtocol.swift 의 UploadBeginMsg 와
정확히 같은 키여야 합니다. size 키가 어긋나면 마스터가 0 으로 읽고
파일이 빈 채로 저장됩니다 — 예외도 안 나고 조용히 실패합니다.
"""
from mocapsync import protocol as P


def test_upload_begin_keys():
    d = P.upload_begin("S20260925-1", "ABC.json", 30_517)
    assert d == {
        "type": "upload_begin",
        "sessionId": "S20260925-1",
        "name": "ABC.json",
        "size": 30_517,
        "sha256": "",
    }


def test_upload_ready_keys():
    assert P.upload_ready("A.json") == {"type": "upload_ready", "name": "A.json"}


def test_upload_done_keys():
    d = P.upload_done("A.json", 30_517, True)
    assert set(d.keys()) == {"type", "name", "size", "ok", "message"}
    assert d["ok"] is True


def test_upload_types_are_registered():
    assert P.T.UPLOAD_BEGIN == "upload_begin"
    assert P.T.UPLOAD_READY == "upload_ready"
    assert P.T.UPLOAD_DONE == "upload_done"


def test_encode_decode_round_trip():
    d = P.upload_begin("S1", "v.mov", 26_214_400)
    assert P.decode(P.encode(d)) == d
