"""애드온 분기 — "조용히 통과" 가 없는지만 본다.

이 파일이 지키는 것은 하나다: **판단하지 않은 바이트가 사이트로 나가지 않는다.**
마스킹 품질은 파이프라인 테스트가 본다. 여기서는 흐름만 본다.

run_pipeline 은 부르지 않는다(모델 로드가 붙는다) — `_scan_and_decide` 를
갈아끼워 사람 판단 결과만 흉내 낸다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

pytest.importorskip("mitmproxy")

from mitmproxy.test import tflow, tutils  # noqa: E402

from app.adapters.proxy import addon as addon_mod  # noqa: E402
from app.adapters.proxy.addon import CampfireAddon, ChatGptUpload, _ORIGINAL  # noqa: E402


def _req(host: str, path: str, method: str = "POST", content: bytes = b"x"):
    f = tflow.tflow(req=tutils.treq(method=method, host=host, path=path, content=content))
    f.request.headers["content-type"] = "application/json"
    return f


def _run(coro):
    return asyncio.run(coro)


def test_정상검사_탐지0건은_사람판단없이_즉시_원본통과(monkeypatch):
    import app.core.pipeline.orchestrator as orch

    async def clean(**_kwargs):
        return {
            "originalText": "hello", "maskedText": "hello",
            "piiItems": [], "injectionItems": [], "forcedMaskItems": [], "blocked": False,
            "scanStatus": "ok", "truncated": False,
            "stats": {"piiCount": 0, "injectionCount": 0, "forcedMaskCount": 0},
        }

    async def must_not_wait(**_kwargs):
        raise AssertionError("탐지 0건인데 사람 판단을 기다렸다")

    monkeypatch.setattr(orch, "run_pipeline", clean)
    monkeypatch.setattr(addon_mod.broker, "wait", must_not_wait)
    got = _run(CampfireAddon()._scan_and_decide(
        data=b"hello", file_name="clean.txt", mime="text/plain", host="claude.ai"
    ))
    assert got is _ORIGINAL


@pytest.mark.parametrize("can_replace, want", [(True, b"hello"), (False, "unreplaceable")])
def test_이미지_검사가_꺼져_그림을_뺀_문서는_깨끗해도_원본_대신_글만(monkeypatch, can_replace, want):
    import app.core.pipeline.orchestrator as orch

    async def clean_with_images(**_kwargs):
        return {
            "originalText": "hello", "maskedText": "hello", "imagesRemoved": 2,
            "piiItems": [], "injectionItems": [], "forcedMaskItems": [], "blocked": False,
            "scanStatus": "ok", "truncated": False,
            "stats": {"piiCount": 0, "injectionCount": 0, "forcedMaskCount": 0},
        }

    async def must_not_wait(**_kwargs):
        raise AssertionError("그림만 뺀 깨끗한 문서인데 사람 판단을 기다렸다")

    monkeypatch.setattr(orch, "run_pipeline", clean_with_images)
    monkeypatch.setattr(addon_mod.broker, "wait", must_not_wait)
    got = _run(CampfireAddon()._scan_and_decide(
        data=b"PK\x03\x04", file_name="pic.docx", mime="application/octet-stream", host="claude.ai",
        can_replace=can_replace,
    ))
    assert got == want if isinstance(want, bytes) else got is addon_mod._UNREPLACEABLE


def test_미검사0건은_자동통과하지_않는다(monkeypatch):
    """파싱 실패/모델 미준비의 0건은 clean이 아니라 판단 불가다."""
    import app.core.pipeline.orchestrator as orch

    async def unscanned(**_kwargs):
        return {
            "originalText": "hello", "maskedText": "hello",
            "piiItems": [], "injectionItems": [], "blocked": False,
            "scanStatus": "models_not_ready", "reason": "모델 준비 중",
            "stats": {"piiCount": 0, "injectionCount": 0},
        }

    waited = []

    async def decide(**kwargs):
        waited.append(kwargs)
        return "cancel"

    monkeypatch.setattr(orch, "run_pipeline", unscanned)
    monkeypatch.setattr(addon_mod.broker, "wait", decide)
    got = _run(CampfireAddon()._scan_and_decide(
        data=b"hello", file_name="clean.txt", mime="text/plain", host="claude.ai"
    ))
    assert got is None
    assert len(waited) == 1


def test_사용자지정_마스킹은_원본판단으로_우회할수없다(monkeypatch):
    import app.core.pipeline.orchestrator as orch

    async def forced(**_kwargs):
        return {
            "originalText": "secret", "maskedText": "[사용자 지정 마스킹]",
            "piiItems": [], "injectionItems": [],
            "forcedMaskItems": [{"start": 0, "end": 6, "type": "USER_DEFINED_TERM", "mandatory": True}],
            "blocked": False, "scanStatus": "ok", "truncated": False,
            "stats": {"piiCount": 0, "injectionCount": 0, "forcedMaskCount": 1},
        }

    async def choose_original(**_kwargs):
        return "send_original"

    monkeypatch.setattr(orch, "run_pipeline", forced)
    monkeypatch.setattr(addon_mod.broker, "wait", choose_original)
    got = _run(CampfireAddon()._scan_and_decide(
        data=b"secret", file_name="secret.txt", mime="text/plain", host="claude.ai"
    ))
    assert got == "[사용자 지정 마스킹]".encode()


# ── Grok: 다룰 수 없는 형식은 통과가 아니라 차단 ────────────────────────────


def test_grok_업로드는_차단된다():
    """Grok 은 JSON+base64 라 아직 못 고친다.

    예전엔 호스트 목록에 없어서 **그냥 통과**했다 — 원문이 그대로 나갔다는 뜻이다.
    못 다루는 것과 흘려보내는 것은 다르다.
    """
    a = CampfireAddon()
    f = _req("grok.com", "/rest/app-chat/upload-file", content=b'{"content":"BASE64"}')
    _run(a.request(f))

    assert f.response is not None, "차단되지 않았다 — 원문이 그대로 나간다"
    assert f.response.status_code == 403
    assert b"campfire_blocked" in f.response.content
    # 본문은 건드리지 않는다. 어차피 안 보낸다.
    assert f.request.content == b'{"content":"BASE64"}'


def test_grok_지금_업로드는_가린_사본으로_바꿔_보낸다():
    """2026-10-22 실측: 지금 Grok 은 /http/upload-file-v2/direct 로 파일 하나를 multipart 로 올린다.

    예전 경로만 막고 있어서 이 업로드는 검사 없이 그대로 나갔다.
    """
    boundary = "----WebKitFormBoundaryAYopRwrjB7QY9mjb"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"상담.txt\"\r\n"
            f"Content-Type: text/plain\r\n\r\n주민번호 900101-1234567\n\r\n--{boundary}--\r\n").encode()
    f = _req("grok.com", "/http/upload-file-v2/direct", content=body)
    f.request.headers["content-type"] = f"multipart/form-data; boundary={boundary}"
    a = CampfireAddon()

    async def fake(**kw):
        assert kw["data"] == "주민번호 900101-1234567\n".encode()
        return "주민번호 [주민등록번호 마스킹]\n".encode()

    a._scan_and_decide = fake  # type: ignore[assignment]
    _run(a.request(f))
    assert f.response is None
    sent = f.request.content.decode()
    assert 'filename="상담_masked.md"' in sent and "Content-Type: text/markdown" in sent
    assert "[주민등록번호 마스킹]" in sent and "900101-1234567" not in sent


def test_perplexity_S3_업로드는_서명_칸을_그대로_두고_파일만_가린_사본으로_바꾼다():
    """2026-10-26 실측: Perplexity 는 파일을 S3 presigned POST 로 바로 올려 원본이 검사 없이 나갔다.

    key·policy·서명 칸이 한 바이트라도 바뀌면 S3 가 거부하므로 그대로 둔다.
    """
    boundary = "----WebKitFormBoundaryg2rxZcXAX3A6dEgl"
    fields = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"key\"\r\n\r\nuploads/abc/메모.docx\r\n"
              f"--{boundary}\r\nContent-Disposition: form-data; name=\"policy\"\r\n\r\neyJleHAiOiIyMDI2In0=\r\n"
              f"--{boundary}\r\nContent-Disposition: form-data; name=\"x-amz-signature\"\r\n\r\n0a1b2c\r\n")
    body = (fields + f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"메모.docx\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\nSECRET 010-1234-5678\r\n--{boundary}--\r\n").encode()
    f = _req("ppl-ai-file-upload.s3.amazonaws.com", "/", content=body)
    f.request.headers["content-type"] = f"multipart/form-data; boundary={boundary}"
    a = CampfireAddon()

    async def fake(**kw):
        assert kw["data"] == b"SECRET 010-1234-5678"
        return b"SECRET [phone]"

    a._scan_and_decide = fake  # type: ignore[assignment]
    _run(a.request(f))
    assert f.response is None
    sent = f.request.content.decode()
    assert sent.startswith(fields), "서명 칸이 바뀌면 S3 가 거부한다"
    assert 'filename="메모_masked.md"' in sent and "SECRET [phone]" in sent and "010-1234-5678" not in sent


def test_perplexity_S3_업로드_호스트가_검사_대상이다():
    exact, _ = addon_mod.intercept_hosts()
    assert "ppl-ai-file-upload.s3.amazonaws.com" in exact  # PAC·TLS 풀기·CA 이름 제한이 모두 이 목록을 쓴다


def test_gemini_메인호스트도_프록시_경로_확인_대상이다():
    """확장은 현재 탭과 같은 호스트의 표식만 믿는다.

    업로드 호스트만 PAC 에 넣으면 Gemini 탭의 same-origin probe 는 DIRECT 로 가서
    프록시가 실제로 켜져 있어도 확장 화면에는 계속 확장 방식으로 보인다.
    """
    exact, _ = addon_mod.intercept_hosts()
    assert "gemini.google.com" in exact
    assert "push.clients6.google.com" in exact


def test_gemini_메인호스트는_경로표식용이고_요청본문은_건드리지_않는다():
    """Gemini 프롬프트는 확장이 맡는다. 메인 호스트를 PAC 에 넣었다고 프록시가
    모르는 와이어 형식을 추측해 고치면 안 된다."""
    a = CampfireAddon()
    f = _req("gemini.google.com", "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
             content=b"opaque-gemini-request")
    _run(a.request(f))
    assert f.response is None
    assert f.request.content == b"opaque-gemini-request"


def _scan_result(*, status="ok", pii=0):
    items = [{"type": "PHONE", "start": 0, "end": 1}] * pii
    return {"originalText": "t", "maskedText": "[가림]" if pii else "t", "piiItems": items,
            "injectionItems": [], "forcedMaskItems": [], "blocked": False, "scanStatus": status,
            "truncated": False, "stats": {"piiCount": pii, "injectionCount": 0, "forcedMaskCount": 0}}


@pytest.mark.parametrize("status, pii, asks, want", [
    ("ok", 2, False, addon_mod._UNREPLACEABLE),  # 끝까지 검사했고 가릴 것이 있다 → 묻지 않고 막는다
    ("ok", 0, False, _ORIGINAL),                 # 깨끗하면 원본
    # 검사를 못 끝냈어도(이미지 검사 꺼짐 등) 원본 그림을 보내지 않고 막는다 — 묻지 않는다(사용자 결정 1안)
    ("unsupported", 0, False, addon_mod._UNREPLACEABLE),
])
def test_가린_글을_넣을_수_없는_업로드는_깨끗할_때만_원본_아니면_묻지_않고_막는다(monkeypatch, status, pii, asks, want):
    import app.core.pipeline.orchestrator as orch
    asked = []

    async def run(**_kw):
        return _scan_result(status=status, pii=pii)

    async def wait(**_kw):
        asked.append(1)
        return "send_original"

    monkeypatch.setattr(orch, "run_pipeline", run)
    monkeypatch.setattr(addon_mod.broker, "wait", wait)
    got = _run(CampfireAddon()._scan_and_decide(
        data=b"\x89PNG\r\n\x1a\n" + b"0" * 32, file_name="upload.bin", mime="", host="gemini.google.com",
        can_replace=False,
    ))
    assert got is want and bool(asked) is asks


def test_gemini_는_검사를_끝내지_못한_파일을_이유를_붙여_막고_재시도도_막는다():
    # 시작 요청에서 이미 _masked.md 로 등록했으니 원본 바이트를 보낼 수 없다(always_text).
    a = CampfireAddon()
    _gem_start(a, declared=1000)
    seen = []

    async def fake(*, data, file_name, mime, host, flow=None, can_replace=True, always_text=False):
        seen.append(always_text)
        return addon_mod._UNREPLACEABLE

    a._scan_and_decide = fake  # type: ignore[assignment]
    f = _gem_bytes(b"SECRET")
    _run(a.request(f))
    assert seen == [True]
    assert f.response is not None and f.response.status_code == 403 and "검사를 끝내지 못한".encode() in f.response.content
    f2 = _gem_bytes(b"SECRET")
    _run(a.request(f2))
    assert f2.response is not None and f2.response.status_code == 403 and seen == [True]


@pytest.mark.parametrize("content, ctype, replaceable", [
    (b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png", False),
    (b"\x89PNG\r\n\x1a\n" + b"0" * 32, "text/markdown", False),  # 등록을 MD 로 바꿔도 바이트가 그림이면 그림
    (b"%PDF-1.7 ...", "application/pdf", True),
])
def test_ChatGPT_이미지는_가린_글로_못_바꾸니_가릴_것이_있으면_이유를_붙여_막는다(content, ctype, replaceable):
    a = CampfireAddon()
    _armed(a, "/files/img/raw")
    seen = []

    async def fake(*, data, file_name, mime, host, flow=None, can_replace=True, **_kw):
        seen.append(can_replace)
        return b"# masked" if can_replace else addon_mod._UNREPLACEABLE

    a._scan_and_decide = fake  # type: ignore[assignment]
    f = _put_flow("/files/img/raw", content)
    f.request.headers["content-type"] = ctype
    _run(a.request(f))
    assert seen == [replaceable]
    if replaceable:
        assert f.response is None and f.request.content.startswith(b"# masked")
    else:
        assert (f.response is not None and f.response.status_code == 403
                and addon_mod._IMAGE_HAS_PII.encode() in f.response.content)


def test_grok_의_다른_요청은_건드리지_않는다():
    """업로드 엔드포인트만 막는다. 전부 막으면 사이트가 죽는다."""
    a = CampfireAddon()
    f = _req("grok.com", "/rest/app-chat/conversations")
    _run(a.request(f))
    assert f.response is None


# ── Gemini: 크기 선언 재작성 + 패딩 (2026-09-21 실측 헤더) ─────────────────
#
# 실측한 모양:
#   POST /upload/  x-goog-upload-command: start
#                  x-goog-upload-header-content-length: 554273
#   POST /upload/  x-goog-upload-command: upload, finalize   (본문 전체가 파일)
# 두 요청이 **같은 경로**로 오기 때문에 시작 응답의 x-goog-upload-url 로 가른다.

GEM_URL = "https://push.clients6.google.com/upload/?upload_id=SESSION1"


def _gem_start(a: CampfireAddon, declared: int = 554273):
    f = _req("push.clients6.google.com", "/upload/", content=b"File name: report.docx")  # 2026-10-29 캡처 모양
    f.request.headers["x-goog-upload-protocol"] = "resumable"
    f.request.headers["x-goog-upload-command"] = "start"
    f.request.headers["x-goog-upload-header-content-length"] = str(declared)
    _run(a.request(f))
    f.response = tutils.tresp()
    f.response.headers["x-goog-upload-url"] = GEM_URL
    a.response(f)
    return f


def _gem_bytes(content: bytes):
    f = tflow.tflow(req=tutils.treq(method="POST", host="push.clients6.google.com",
                                    path="/upload/?upload_id=SESSION1", content=content))
    f.request.headers["x-goog-upload-command"] = "upload, finalize"
    return f


def test_gemini_시작요청의_선언길이를_키운다():
    """ChatGPT 의 file_size 와 같은 자리. 안 키우면 패딩할 여유가 없다."""
    a = CampfireAddon()
    f = _gem_start(a, declared=1000)
    assert f.response is None or f.response.status_code != 403
    assert int(f.request.headers["x-goog-upload-header-content-length"]) > 1000


def test_gemini_본문이_마스킹되고_선언값에_맞춰진다():
    a = CampfireAddon()
    start = _gem_start(a, declared=1000)
    nprime = int(start.request.headers["x-goog-upload-header-content-length"])

    async def fake(*, data, file_name, mime, host, flow=None, can_replace=True, **_kw):
        assert b"SECRET" in data
        return b"# masked"

    a._scan_and_decide = fake  # type: ignore[assignment]

    f = _gem_bytes(b"SECRET" * 100)
    _run(a.request(f))
    assert f.response is None
    assert f.request.content.startswith(b"# masked")
    assert b"SECRET" not in f.request.content
    assert len(f.request.content) == nprime, "선언 크기와 본문 크기가 다르면 거부당한다"


def test_gemini_시작요청을_못_봤으면_차단한다():
    """약속한 크기를 모르면 맞출 수가 없다. 통과는 곧 원문 유출이다."""
    a = CampfireAddon()
    f = _gem_bytes(b"SECRET")
    _run(a.request(f))
    assert f.response is not None and f.response.status_code == 403


def test_gemini_길이선언이_없으면_차단한다():
    a = CampfireAddon()
    f = _req("push.clients6.google.com", "/upload/", content=b"x" * 46)
    f.request.headers["x-goog-upload-command"] = "start"
    _run(a.request(f))
    assert f.response is not None and f.response.status_code == 403


def test_gemini_재시도도_같은_결과를_쓴다():
    a = CampfireAddon()
    _gem_start(a, declared=1000)
    calls = []

    async def fake(*, data, file_name, mime, host, flow=None, can_replace=True, **_kw):
        calls.append(1)
        return b"# masked"

    a._scan_and_decide = fake  # type: ignore[assignment]

    f1 = _gem_bytes(b"SECRET")
    _run(a.request(f1))
    f2 = _gem_bytes(b"SECRET")
    _run(a.request(f2))
    assert f2.request.content == f1.request.content
    assert b"SECRET" not in f2.request.content
    assert len(calls) == 1, "재시도인데 사람에게 또 물었다"


def test_모르는_호스트는_통과한다():
    """게이트웨이는 허용 목록만 본다 — 인터넷 전체를 검사하지 않는다."""
    a = CampfireAddon()
    f = _req("example.com", "/upload")
    _run(a.request(f))
    assert f.response is None


# ── 프록시 표식 헤더 (확장이 "내 트래픽이 프록시를 지난다"를 아는 근거) ──────

def _resp_flow(host="claude.ai"):
    f = tflow.tflow(req=tutils.treq(host=host, path="/api/x"), resp=tutils.tresp(content=b"{}"))
    return f


def test_응답에_프록시_표식_헤더를_찍는다():
    a = CampfireAddon()
    f = _resp_flow()
    a.response(f)
    assert f.response.headers.get("X-Campfire-Proxy") == "1", "표식이 없으면 확장이 손을 못 뗀다"


def test_표식을_cross_origin_에서_읽게_노출한다():
    a = CampfireAddon()
    f = _resp_flow()
    a.response(f)
    expose = f.response.headers.get("access-control-expose-headers", "")
    assert "x-campfire-proxy" in expose.lower()


def test_표식_헤더_이름은_소문자다():
    # HTTP/2 는 대문자 헤더 이름을 허용하지 않는다 — 대문자로 넣으면 mitmproxy 가
    # 응답마다 경고를 남겨 로그가 불어났다(맥 실측).
    a = CampfireAddon()
    f = _resp_flow()
    a.response(f)
    assert (b"x-campfire-proxy", b"1") in f.response.headers.fields


def test_기존_expose_헤더를_보존한다():
    a = CampfireAddon()
    f = _resp_flow()
    f.response.headers["access-control-expose-headers"] = "X-Request-Id"
    a.response(f)
    expose = f.response.headers.get("access-control-expose-headers", "")
    assert "X-Request-Id" in expose and "x-campfire-proxy" in expose.lower()


# ── ChatGPT: PUT 재시도가 통과하지 않는지 ───────────────────────────────────


def _put_flow(path: str, content: bytes):
    return tflow.tflow(
        req=tutils.treq(method="PUT", host="files.oaiusercontent.com", path=path, content=content)
    )


def _armed(a: CampfireAddon, key: str, declared: int = 1024) -> ChatGptUpload:
    promise = ChatGptUpload(declared=declared, masked_name="a_masked.md", original_name="a.docx")
    a._chatgpt[key] = promise
    return promise


def test_준비되지_않은_blob_PUT_은_차단된다():
    """2026-09-21 실사이트에서 실제로 밟은 구멍.

    등록 경로가 /backend-api/files 에서 /backend-api/files/upload_reservations 로
    바뀌어 있었고, 우리는 정확히 일치하는 경로 하나만 보고 있었다. 그래서 등록을
    못 봤고, 준비되지 않은 PUT 이 **그대로 통과해 파일이 ChatGPT 에 올라갔다.**

    경로 목록을 따라다니는 것으로는 부족하다 — 바이트가 나가는 호스트에서 막는다.
    """
    a = CampfireAddon()
    f = _put_flow("/files/unknown/raw", b"ORIGINAL-SECRET")
    _run(a.request(f))
    assert f.response is not None, "차단되지 않았다 — 원문이 그대로 나간다"
    assert f.response.status_code == 403
    assert f.request.content == b"ORIGINAL-SECRET"  # 안 보낼 것이므로 손대지 않는다


def test_두_등록경로_모두_인식한다():
    """UI 가 쓰는 경로와 예전 경로 둘 다 본다."""
    from app.adapters.proxy.addon import CHATGPT_REGISTER_PATHS

    assert "/backend-api/files" in CHATGPT_REGISTER_PATHS
    assert "/backend-api/files/upload_reservations" in CHATGPT_REGISTER_PATHS


def test_PUT_재시도는_같은_결과를_쓰고_다시_묻지_않는다():
    """예전엔 첫 PUT 에서 약속값을 pop 해서, 재시도가 분기를 못 타고 **원본으로
    나갔다.** 업로드 재시도는 흔한 일이라 그냥 두면 실제로 밟힌다."""
    a = CampfireAddon()
    _armed(a, "/files/abc/raw")
    asked = []

    async def fake(*, data, file_name, mime, host, flow=None, can_replace=True, **_kw):
        asked.append(file_name)
        return b"# masked"

    a._scan_and_decide = fake  # type: ignore[assignment]

    f1 = _put_flow("/files/abc/raw", b"ORIGINAL-SECRET")
    _run(a.request(f1))
    assert f1.response is None
    assert f1.request.content.startswith(b"# masked")
    assert len(f1.request.content) == 1024      # 선언값에 정확히 맞춘다

    # 재시도: 사이트가 같은 PUT 을 다시 보낸다.
    f2 = _put_flow("/files/abc/raw", b"ORIGINAL-SECRET")
    _run(a.request(f2))
    assert f2.response is None
    assert f2.request.content == f1.request.content, "재시도에서 원본이 나갔다"
    assert b"ORIGINAL-SECRET" not in f2.request.content
    assert asked == ["a.docx"], "재시도인데 사람에게 또 물었다"


def test_크기선언이_없는_흐름은_패딩하지_않는다():
    """upload_reservations 는 크기를 약속하지 않는다(실측).

    약속이 없으면 맞출 것도 없다. 예전 코드는 무조건 패딩해서 70바이트 파일을
    65KB 로 부풀렸다 — 약속이 없는데 부풀릴 이유가 없다.
    """
    a = CampfireAddon()
    promise = ChatGptUpload(declared=None, masked_name="m.md", original_name="a.txt")
    a._chatgpt["/files/nopad/raw"] = promise

    async def fake(*, data, file_name, mime, host, flow=None, can_replace=True, **_kw):
        return b"# masked"

    a._scan_and_decide = fake  # type: ignore[assignment]

    f = _put_flow("/files/nopad/raw", b"ORIGINAL-SECRET")
    _run(a.request(f))
    assert f.response is None
    assert f.request.content == b"# masked", "패딩 없이 그대로 나가야 한다"


def test_본문을_바꾸면_타입도_바꾼다():
    """PDF 를 올릴 때 PUT 은 Content-Type: application/pdf 로 온다(실측).

    마스킹 결과는 MD 텍스트라, 헤더를 그대로 두면 받는 쪽이 마크다운을 PDF 로
    열려고 한다. 업로드는 통과해도 처리 단계에서 깨진다.
    """
    a = CampfireAddon()
    promise = ChatGptUpload(declared=None, masked_name="m.md", original_name="a.pdf")
    a._chatgpt["/files/pdf/raw"] = promise

    async def fake(*, data, file_name, mime, host, flow=None, can_replace=True, **_kw):
        return b"# masked"

    a._scan_and_decide = fake  # type: ignore[assignment]

    f = _put_flow("/files/pdf/raw", b"%PDF-1.7 ...")
    f.request.headers["content-type"] = "application/pdf"
    _run(a.request(f))
    assert f.response is None
    assert f.request.content == b"# masked"
    assert f.request.headers["content-type"] == "text/markdown"


def test_upload_reservations_확정은_실제_PUT한_마스킹본_메타데이터로_바꾼다():
    """Windows v0.2.60 실측 흐름: 등록에는 크기가 없고 claim_and_finish 에서 다시 확정한다."""
    a = CampfireAddon()
    file_id = "file_00000000000000000000000000000001"
    raw_path = "/files/claim-flow/raw"
    original_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    register = _req(
        "chatgpt.com",
        "/backend-api/files/upload_reservations",
        content=json.dumps({"file_name": "pii.docx", "mime_type": original_mime, "use_case": "my_files"}).encode(),
    )
    _run(a.request(register))
    register.response = tutils.tresp(content=json.dumps({
        "upload_url": f"https://files.oaiusercontent.com{raw_path}?sig=fixture",
        "file_id": file_id,
    }).encode())
    a.response(register)

    async def fake(*, data, file_name, mime, host, flow=None, can_replace=True, **_kw):
        assert data == b"ORIGINAL-DOCX" and file_name == "pii.docx"
        return b"# masked\n"

    a._scan_and_decide = fake  # type: ignore[assignment]
    put = _put_flow(raw_path, b"ORIGINAL-DOCX")
    put.request.headers["content-type"] = original_mime
    _run(a.request(put))
    assert put.request.content == b"# masked\n"

    claim_body = {
        "entry_surface": "chat_composer",
        "file_name": "pii.docx",
        "file_size": 8074,
        "mime_type": original_mime,
        "client_resolved_mime_type": original_mime,
        "content_type": original_mime,
        "use_case": "my_files",
    }
    claim = _req(
        "chatgpt.com",
        f"/backend-api/files/upload_reservations/{file_id}/claim_and_finish",
        content=json.dumps(claim_body).encode(),
    )
    _run(a.request(claim))

    sent = json.loads(claim.request.get_text())
    assert claim.response is None
    assert sent["file_name"] == "pii_masked.md"
    assert sent["file_size"] == len(put.request.content)
    assert sent["mime_type"] == sent["client_resolved_mime_type"] == sent["content_type"] == "text/markdown"
    assert sent["entry_surface"] == "chat_composer" and sent["use_case"] == "my_files"


def test_upload_reservations_확정이_PUT보다_먼저_오면_막는다():
    a = CampfireAddon()
    file_id = "file_before_put"
    promise = ChatGptUpload(declared=None, masked_name="a_masked.md", original_name="a.docx", file_id=file_id)
    a._chatgpt_by_id[file_id] = promise
    claim = _req(
        "chatgpt.com",
        f"/backend-api/files/upload_reservations/{file_id}/claim_and_finish",
        content=b'{"file_name":"a.docx","file_size":10,"mime_type":"application/octet-stream"}',
    )
    _run(a.request(claim))
    assert claim.response is not None and claim.response.status_code == 403


def test_한번_막은_업로드는_재시도도_막는다():
    a = CampfireAddon()
    _armed(a, "/files/xyz/raw")

    async def refuse(*, data, file_name, mime, host, **_kw):
        return None  # 사람이 '보내지 않음' 을 골랐다

    a._scan_and_decide = refuse  # type: ignore[assignment]

    f1 = _put_flow("/files/xyz/raw", b"ORIGINAL-SECRET")
    _run(a.request(f1))
    assert f1.response.status_code == 403

    f2 = _put_flow("/files/xyz/raw", b"ORIGINAL-SECRET")
    _run(a.request(f2))
    assert f2.response is not None and f2.response.status_code == 403


def test_마스킹본이_선언값보다_크면_차단하고_재시도도_막는다():
    """자르면 사용자가 모르는 채로 내용이 사라진다. 조용한 손실보다 실패가 낫다."""
    a = CampfireAddon()
    _armed(a, "/files/big/raw", declared=8)

    async def big(*, data, file_name, mime, host, **_kw):
        return b"x" * 100

    a._scan_and_decide = big  # type: ignore[assignment]

    f1 = _put_flow("/files/big/raw", b"ORIGINAL")
    _run(a.request(f1))
    assert f1.response.status_code == 403

    f2 = _put_flow("/files/big/raw", b"ORIGINAL")
    _run(a.request(f2))
    assert f2.response.status_code == 403


def test_원본_전송을_골라도_선언값에_맞춰_채운다():
    """등록은 이미 N' 으로 고쳐 버렸다. 원본 크기 그대로 보내면 사이트가 거부한다."""
    a = CampfireAddon()
    _armed(a, "/files/orig/raw", declared=64)

    async def original(*, data, file_name, mime, host, flow=None, can_replace=True, **_kw):
        return _ORIGINAL

    a._scan_and_decide = original  # type: ignore[assignment]

    f = _put_flow("/files/orig/raw", b"ORIGINAL")
    _run(a.request(f))
    assert f.response is None
    assert f.request.content.startswith(b"ORIGINAL")
    assert len(f.request.content) == 64


def test_추적_목록은_무한히_자라지_않는다():
    """재시도용으로 남겨 두는 항목이라 스스로 줄지 않는다."""
    a = CampfireAddon()
    for i in range(addon_mod._MAX_TRACKED_UPLOADS + 20):
        f = tflow.tflow(
            req=tutils.treq(method="POST", host="chatgpt.com", path="/backend-api/files")
        )
        f.metadata["campfire_upload"] = ChatGptUpload(
            declared=10, masked_name="m.md", original_name="o.docx"
        )
        f.response = tutils.tresp(
            content=(
                '{"upload_url":"https://x.oaiusercontent.com/files/%d/raw?sig=1","file_id":"file_%d"}'
                % (i, i)
            ).encode()
        )
        a.response(f)
    assert len(a._chatgpt) == addon_mod._MAX_TRACKED_UPLOADS
    assert len(a._chatgpt_by_id) == addon_mod._MAX_TRACKED_UPLOADS


def test_Claude_메시지도_사용자지정_마스킹은_원본판단으로_우회할수없다(monkeypatch):
    from types import SimpleNamespace
    import app.core.pipeline.orchestrator as orch

    async def forced(**_kwargs):
        return {
            "originalText": "secret", "maskedText": "[사용자 지정 마스킹]",
            "piiItems": [], "injectionItems": [],
            "forcedMaskItems": [{"start": 0, "end": 6, "type": "USER_DEFINED_TERM", "mandatory": True}],
            "blocked": False, "scanStatus": "ok", "truncated": False,
        }

    async def choose_original(**_kwargs):
        return "send_original"

    monkeypatch.setattr(orch, "run_pipeline", forced)
    monkeypatch.setattr(addon_mod.broker, "wait", choose_original)
    fld = SimpleNamespace(value=b"secret")
    got = _run(CampfireAddon()._scan_texts_and_decide([fld], host="claude.ai"))
    assert got == [(fld, "[사용자 지정 마스킹]".encode())]


def test_파일명에_등록어가_있으면_깨끗해도_원본도_원래이름도_안나간다(monkeypatch):
    import app.core.pipeline.orchestrator as orch
    from app.core import forced_mask

    forced_mask.registry.configure(["Aurora"])

    async def clean(**_kwargs):
        return {
            "originalText": "hello", "maskedText": "hello",
            "piiItems": [], "injectionItems": [], "forcedMaskItems": [], "blocked": False,
            "scanStatus": "ok", "truncated": False,
        }

    asked = []

    async def choose_original(**_kwargs):
        asked.append(True)
        return "send_original"

    monkeypatch.setattr(orch, "run_pipeline", clean)
    monkeypatch.setattr(addon_mod.broker, "wait", choose_original)
    got = _run(CampfireAddon()._scan_and_decide(
        data=b"hello", file_name="Aurora_plan.txt", mime="text/plain", host="claude.ai"
    ))
    assert asked, "이름에 등록어가 있는데 깨끗하다고 바로 원본을 보냈다"
    assert got == b"hello"
    assert addon_mod.masked_name_for("Aurora_plan.pdf") == "document_masked.md"
    assert addon_mod.masked_name_for("plain.pdf") == "plain_masked.md"


def test_확장이_검토한_마스킹본은_다시_검사하지_않는다(monkeypatch):
    """맥 v0.2.58: 프록시가 확장의 마스킹본을 프롬프트 없이 다시 검사해 마지막 줄을 인젝션으로 바꿔 보냈고(ChatGPT),
    Gemini 는 바꿔 넣을 수 없어 막혀 첨부가 오류로 떴다. 확장이 알린 해시와 같은 바이트는 그대로 보낸다."""
    import app.core.pipeline.orchestrator as orch

    async def must_not_scan(**_kwargs):
        raise AssertionError("확장이 검토한 마스킹본을 다시 검사했다")

    monkeypatch.setattr(orch, "run_pipeline", must_not_scan)
    monkeypatch.setattr(addon_mod, "_approved", {})
    body = "담당자: [이름 마스킹]\n이번 분기 일정을 정리해 주세요.".encode()
    addon_mod.approve_uploads([(hashlib.sha256(body).hexdigest(), "pii_masked.md", len(body))])
    for host, can_replace in (("chatgpt.com", True), ("gemini.google.com", False)):
        got = _run(CampfireAddon()._scan_and_decide(
            data=body, file_name="upload.bin", mime="text/markdown", host=host, can_replace=can_replace,
        ))
        assert got is _ORIGINAL, host
    with pytest.raises(AssertionError):  # 한 바이트라도 다르면 지금처럼 검사한다
        _run(CampfireAddon()._scan_and_decide(data=body + b" ", file_name="x.md", mime="text/markdown", host="chatgpt.com"))


def test_확장이_검토한_마스킹본은_ChatGPT_등록의_이름과_크기를_고치지_않는다(monkeypatch):
    """맥 v0.2.58: 등록에 "image_masked_masked.md", 크기 67160(=406×4+64K)이 실렸다."""
    monkeypatch.setattr(addon_mod, "_approved", {})
    addon_mod.approve_uploads([("0" * 64, "image_masked.md", 406)])

    def register(name: str, size: int, use_case: str = "multimodal") -> tuple[dict, object]:
        flow = _req("chatgpt.com", "/backend-api/files",
                    content=json.dumps({"file_name": name, "file_size": size, "use_case": use_case}).encode())
        CampfireAddon()._chatgpt_register(flow)
        return json.loads(flow.request.get_text()), flow.metadata["campfire_upload"]

    body, promise = register("image_masked.md", 406)
    assert (body["file_name"], body["file_size"], promise.declared) == ("image_masked.md", 406, None)
    body, promise = register("report.docx", 406, "my_files")  # 알리지 않은 문서는 지금처럼 고친다(그림은 그대로 둔다)
    assert body["file_name"] == "report_masked.md" and body["file_size"] == promise.declared > 406


def test_chatgpt_document_forces_single_put_but_image_keeps_multipart():
    def register(name: str, mime: str) -> dict:
        flow = _req(
            "chatgpt.com",
            "/backend-api/files",
            content=json.dumps({
                "file_name": name,
                "file_size": 4096,
                "mime_type": mime,
                "supports_direct_azure_multipart": True,
            }).encode(),
        )
        CampfireAddon()._chatgpt_register(flow)
        return json.loads(flow.request.get_text())

    document = register(
        "report.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    image = register("photo.png", "image/png")

    assert document["supports_direct_azure_multipart"] is False
    assert image["supports_direct_azure_multipart"] is True


def test_확장의_경로_확인은_사이트까지_보내지_않고_프록시가_표식을_달아_답한다():
    # 예전엔 AI 사이트가 404 로 답해 콘솔에 빨간 줄이 남았고 확인에 인터넷 왕복이 들어 그동안 전송을 붙들었다(맥 v0.2.59).
    f = tflow.tflow(req=tutils.treq(method="HEAD", host="gemini.google.com",
                                    path="/.well-known/campfire-proxy-probe?ts=123", content=b""))
    _run(CampfireAddon().request(f))
    assert f.response is not None and f.response.status_code == 204
    assert f.response.headers.get(addon_mod.PROXY_MARK_HEADER) == "1"
