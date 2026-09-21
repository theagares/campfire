"""
app/adapters/proxy/addon.py
mitmproxy 애드온 — 업로드 요청을 붙들고, 검사하고, 마스킹본으로 바꿔 보낸다.

확장 프로그램 경로와 결정적으로 다른 점: **되돌려 넣지 않는다.** 확장은 사이트가
이미 받은 파일을 우리 것으로 갈아끼워야 해서 주입·증거 체인이 필요했다. 여기서는
아직 사이트에 닿지 않은 요청의 본문을 고치는 것이라 그 기계가 통째로 없다.

사이트별 코드가 사라지는 건 아니다 — DOM 대신 와이어 포맷을 알아야 한다.
지금 상태:
  - multipart 로 올리는 곳(Claude/Copilot/Perplexity)은 한 처리로 덮는다.
  - ChatGPT 는 등록→PUT 2단계라 전용 처리가 있다.
  - Grok 은 JSON+base64 라 아직 못 다룬다 → **차단**한다(UNSUPPORTED_UPLOADS).
  - Gemini 는 push.clients6.google.com 으로 raw 본문을 올린다 → 아직 못 다뤄 **차단**.

fail-closed 원칙: 이 파일의 모든 경로는 "판단이 안 서면 안 보낸다" 로 끝난다.
예외가 나면 원본이 흘러가는 게 아니라 403 으로 끊는다(_guard 참고).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from mitmproxy import http

from app import config
from app.adapters.proxy.decision import DecisionTimeout, broker
from app.adapters.proxy.multipart import MultipartError, decode, encode

logger = logging.getLogger("securedoc.proxy")

# 마스킹 결과는 항상 텍스트(MD)로 낸다.
#
# 엔진의 wrap_masked_file() 은 DOCX 를 내지만 여기서는 쓰지 않는다. ChatGPT 경로는
# 선언한 크기에 **정확히** 맞춰야 해서 뒤를 공백으로 채우는데, DOCX 는 ZIP 이라
# 꼬리에 바이트를 붙이는 게 안전하다고 검증되지 않았다. 텍스트는 자명하게 안전하다.
MASKED_MIME = "text/markdown"


def masked_name_for(original: str) -> str:
    stem = original.rsplit(".", 1)[0] if "." in original else original
    return f"{stem}_masked.md"


# ─── 사이트 표 ────────────────────────────────────────────────────────────────
#
# multipart 로 올리는 곳은 전부 같은 처리를 타고, 그렇지 않은 곳만 전용 처리가 붙는다.
MULTIPART_HOSTS = {
    "claude.ai",
    "copilot.microsoft.com",
    "www.perplexity.ai",
    "perplexity.ai",
    # Gemini 는 여기 없다. 실측해 보니 업로드가 같은 오리진이 아니라
    # push.clients6.google.com 으로 가고 multipart 도 아니다 — 아래
    # UNSUPPORTED_UPLOADS 참고. 여기 넣어 봐야 아무 효과가 없다.
}

CHATGPT_HOSTS = {"chatgpt.com", "chat.openai.com"}
CHATGPT_REGISTER_PATH = "/backend-api/files"

# 업로드인 것은 아는데 그 형식을 아직 다룰 수 없는 자리.
#
# 통과시키면 원문이 그대로 나간다. "지원하지 않는다" 와 "그냥 보낸다" 는 다르고,
# 이 제품에서 후자는 곧 사고다. 그래서 막고 이유를 알린다.
# Grok 은 multipart 가 아니라 JSON 본문에 base64 를 넣어 보낸다(확장 쪽 실측).
UNSUPPORTED_UPLOADS: dict[str, tuple[str, ...]] = {
    "grok.com": ("/rest/app-chat/upload-file",),
    # Gemini. 2026-09-21 프록시로 직접 재서 확정했다:
    #   POST push.clients6.google.com/upload/  ct=x-www-form-urlencoded  len=46
    #   POST push.clients6.google.com/upload/  ct=x-www-form-urlencoded  len=554273
    # 두 번째가 파일 바이트다. content-type 은 urlencoded 라고 적혀 있지만 실제로는
    # Google resumable upload 라 **본문 전체가 파일**이다. multipart 가 아니므로
    # MULTIPART_HOSTS 에 넣어도 아무 효과가 없다 — 그래서 막는다.
    #
    # 제대로 고치려면 ChatGPT 와 같은 모양이 된다: 첫 요청(세션 시작)의 헤더에
    # 선언된 길이를 N' 으로 고치고, 두 번째 본문을 N' 까지 패딩한다. 그 헤더 이름을
    # 아직 안 봤다(SECUREDOC_PROXY_LOG_REQUESTS 로 한 번 더 재면 된다).
    "push.clients6.google.com": ("/upload/",),
}

# 추적 중인 ChatGPT 업로드 수 상한. 재시도를 위해 약속값을 남겨 두기 때문에
# 그냥 두면 무한히 쌓인다. 동시에 올릴 수 있는 수보다 넉넉하면 충분하다.
_MAX_TRACKED_UPLOADS = 64


def _multipart_hosts() -> set[str]:
    """매번 읽는다 — 환경변수로 더한 호스트가 재시작 없이 먹어야 한다."""
    return MULTIPART_HOSTS | config.PROXY_EXTRA_HOSTS


@dataclass
class ChatGptUpload:
    """등록 단계에서 잡아 둔 값. PUT 이 올 때 쓴다."""

    declared: int          # 등록에 써 넣은 N' — PUT 본문을 여기에 정확히 맞춰야 한다
    masked_name: str
    original_name: str
    # 판단이 끝난 뒤의 최종 본문. 같은 PUT 이 다시 오면 이걸 그대로 쓴다.
    #
    # 예전엔 PUT 을 처리하면서 약속값을 pop 했는데, 그러면 **재시도가 통과한다** —
    # 두 번째 PUT 은 키를 못 찾아 분기를 안 타고 원본이 그대로 나간다. 업로드
    # 재시도는 흔한 일이라 그냥 두면 실제로 밟힌다.
    settled: bytes | None = None
    blocked: bool = False  # 한 번 막았으면 재시도도 막는다


def size_ceiling(declared: int) -> int:
    """등록 시점에 선언할 N'.

    마스킹은 텍스트를 늘린다(`redact_map.py`: 홍길동 3자 → [이름 마스킹] 8자).
    게다가 DOCX 처럼 압축된 입력은 풀린 텍스트가 파일보다 클 수 있어 N 의 배수로만
    잡으면 모자란다. 그래서 배수에 고정분을 더해 넉넉히 잡는다.

    과대 선언 자체는 안전하다 — 17바이트짜리를 4096 으로 등록해도 등록은 200 이다
    (실측). 문제가 되는 건 선언값과 **실제 PUT 크기**가 다를 때뿐이다.
    """
    return min(declared * 4 + 65536, config.MAX_UPLOAD_BYTES)


class CampfireAddon:
    def __init__(self) -> None:
        # 등록 응답에서 받은 upload_url 의 경로 → 그 업로드의 약속값.
        # PUT 은 완전히 다른 호스트(oaiusercontent.com)로 가기 때문에 경로로 잇는다.
        self._chatgpt: dict[str, ChatGptUpload] = {}

    # ── mitmproxy 훅 ─────────────────────────────────────────────────────────

    async def request(self, flow: http.HTTPFlow) -> None:
        try:
            await self._on_request(flow)
        except DecisionTimeout as exc:
            self._block(flow, f"검토 시간이 지나 전송을 취소했습니다 ({exc})")
        except Exception:
            # 여기서 통과시키면 원본이 그대로 나간다. 그게 이 제품이 막아야 하는
            # 단 하나의 사고라, 알 수 없는 예외는 전부 차단으로 떨어뜨린다.
            logger.exception("[proxy] 요청 처리 실패 — 차단 %s", flow.request.pretty_url)
            self._block(flow, "검사에 실패해 전송을 막았습니다")

    def response(self, flow: http.HTTPFlow) -> None:
        try:
            self._on_response(flow)
        except Exception:
            # 응답 훅은 기록만 한다. 실패해도 이미 나간 요청을 되돌릴 수 없으므로
            # 차단하지 않되, 다음 PUT 이 약속값을 못 찾아 차단되는 쪽으로 떨어진다.
            logger.exception("[proxy] 응답 처리 실패 %s", flow.request.pretty_url)

    # ── 분기 ─────────────────────────────────────────────────────────────────

    async def _on_request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host
        if config.PROXY_LOG_REQUESTS and flow.request.method in ("POST", "PUT"):
            logger.info(
                "[proxy] %s %s%s ct=%s len=%s",
                flow.request.method, host, flow.request.path.split("?")[0],
                flow.request.headers.get("content-type", "-"),
                len(flow.request.content or b""),
            )
            # 길이를 미리 선언하는 헤더를 찾으려고 남긴다(ChatGPT 의 file_size 에
            # 해당하는 자리). 값 자체는 크기·명령어라 민감정보가 아니다.
            up = {k: v for k, v in flow.request.headers.items()
                  if k.lower().startswith(("x-goog-upload", "x-guploader", "upload-"))}
            if up:
                logger.info("[proxy]   업로드 헤더 %s", up)
        if host in CHATGPT_HOSTS and flow.request.path.split("?")[0] == CHATGPT_REGISTER_PATH:
            self._chatgpt_register(flow)
            return
        key = flow.request.path.split("?")[0]
        if key in self._chatgpt and flow.request.method == "PUT":
            await self._chatgpt_put(flow, key)
            return
        if flow.request.method == "POST" and key in UNSUPPORTED_UPLOADS.get(host, ()):
            logger.warning("[proxy] 다루지 못하는 업로드 형식 — 차단 %s%s", host, key)
            self._block(flow, "아직 지원하지 않는 업로드 형식이라 전송을 막았습니다")
            return
        if host in _multipart_hosts() and flow.request.method == "POST":
            await self._multipart(flow, host)

    def _on_response(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host
        if host in CHATGPT_HOSTS and flow.request.path.split("?")[0] == CHATGPT_REGISTER_PATH:
            self._chatgpt_register_response(flow)

    # ── multipart 경로 (Claude / Copilot / Perplexity, Gemini 는 미확인) ────

    async def _multipart(self, flow: http.HTTPFlow, host: str) -> None:
        ctype = flow.request.headers.get("content-type", "")
        if "multipart/form-data" not in ctype.lower():
            return
        try:
            parts = decode(flow.request.content or b"", ctype)
        except MultipartError:
            # multipart 라고 적혀 있는데 못 읽으면 우리가 모르는 모양이다.
            # 통과시키면 검사 없이 나가므로 차단한다.
            logger.warning("[proxy] multipart 해석 실패 — 차단 %s", flow.request.pretty_url)
            self._block(flow, "업로드 형식을 해석하지 못해 전송을 막았습니다")
            return

        file_idx = [i for i, p in enumerate(parts) if p.is_file()]
        if not file_idx:
            return  # 파일이 없는 평범한 폼. 건드릴 이유가 없다.

        for i in file_idx:
            part = parts[i]
            name = part.filename or "upload.bin"
            masked = await self._scan_and_decide(
                data=part.body, file_name=name, mime=part.content_type, host=host
            )
            if masked is None:
                self._block(flow, "검토 결과 전송하지 않기로 했습니다")
                return
            if masked is _ORIGINAL:
                continue  # 사람이 원본 전송을 택했다 — 이 파트는 그대로 둔다
            parts[i] = part.replaced(
                body=masked, filename=masked_name_for(name), content_type=MASKED_MIME
            )

        flow.request.content = encode(parts, ctype)

    # ── ChatGPT 경로 (등록 → PUT → 확인) ────────────────────────────────────

    def _chatgpt_register(self, flow: http.HTTPFlow) -> None:
        """등록 요청을 고친다.

        이 시점엔 파일 바이트를 아직 못 봤다. 그래서 마스킹 결과 크기를 알 수 없고,
        넉넉한 N' 을 선언해 둔 뒤 PUT 때 그 크기에 **정확히** 맞춰 채운다.
        ChatGPT 는 선언값과 실제 크기가 다르면 `file_size_mismatch` 로 거부한다(실측).
        """
        try:
            body = json.loads(flow.request.get_text() or "{}")
        except json.JSONDecodeError:
            return
        declared = int(body.get("file_size") or 0)
        if declared <= 0:
            return
        original_name = str(body.get("file_name") or "upload.bin")
        nprime = size_ceiling(declared)
        masked_name = masked_name_for(original_name)

        body["file_size"] = nprime
        # 이름과 타입도 같이 바꾼다. `.docx` 로 등록해 놓고 MD 바이트를 올리면
        # 받는 쪽이 DOCX 로 열려다 처리 단계에서 실패한다.
        body["file_name"] = masked_name
        body["mime_type"] = MASKED_MIME
        if "client_resolved_mime_type" in body:
            body["client_resolved_mime_type"] = MASKED_MIME
        flow.request.set_text(json.dumps(body))
        # 응답에서 upload_url 을 받을 때 이어 붙이려고 잠시 들고 있는다.
        flow.metadata["campfire_upload"] = ChatGptUpload(
            declared=nprime, masked_name=masked_name, original_name=original_name
        )

    def _chatgpt_register_response(self, flow: http.HTTPFlow) -> None:
        pending = flow.metadata.get("campfire_upload")
        if pending is None or flow.response is None:
            return
        try:
            data = json.loads(flow.response.get_text() or "{}")
        except json.JSONDecodeError:
            return
        url = data.get("upload_url")
        if not url:
            return
        # PUT 은 chatgpt.com 이 아니라 oaiusercontent.com 으로 간다. 경로만 키로 쓴다.
        path = url.split("?", 1)[0]
        idx = path.find("/", path.find("://") + 3)
        self._chatgpt[path[idx:] if idx > 0 else path] = pending
        # 재시도를 위해 남겨 두는 항목이라 스스로는 안 줄어든다. 오래된 것부터 버린다.
        while len(self._chatgpt) > _MAX_TRACKED_UPLOADS:
            self._chatgpt.pop(next(iter(self._chatgpt)))

    async def _chatgpt_put(self, flow: http.HTTPFlow, key: str) -> None:
        # pop 하지 않는다 — 재시도된 PUT 이 분기를 못 타고 원본으로 나가는 것을 막는다.
        promise = self._chatgpt[key]
        if promise.blocked:
            self._block(flow, "검토 결과 전송하지 않기로 했습니다")
            return
        if promise.settled is not None:
            # 이미 판단이 끝난 업로드의 재시도. 사람에게 다시 묻지 않는다.
            flow.request.content = promise.settled
            return

        masked = await self._scan_and_decide(
            data=flow.request.content or b"",
            file_name=promise.original_name,
            mime=flow.request.headers.get("content-type", ""),
            host="chatgpt.com",
        )
        if masked is None:
            promise.blocked = True
            self._block(flow, "검토 결과 전송하지 않기로 했습니다")
            return
        if masked is _ORIGINAL:
            # 원본을 보내기로 했는데 등록은 이미 N' 으로 고쳐 버렸다. 원본 크기와
            # 다르므로 그대로 두면 사이트가 거부한다 — 원본도 같은 규칙으로 채운다.
            masked = flow.request.content or b""
        if len(masked) > promise.declared:
            # N' 이 모자랐다. 잘라서 보내면 사용자가 모르는 채로 내용이 사라지므로
            # 차단한다 — 조용한 손실보다 실패가 낫다.
            logger.warning(
                "[proxy] 마스킹 결과가 선언값을 넘었다 %d > %d", len(masked), promise.declared
            )
            promise.blocked = True
            self._block(flow, "마스킹 결과가 예상보다 커서 전송을 막았습니다")
            return
        # 선언값에 정확히 맞춘다. 공백 패딩은 텍스트에서 안전하다(실측으로 통과 확인).
        promise.settled = masked.ljust(promise.declared, b" ")
        flow.request.content = promise.settled

    # ── 공통: 검사 + 사람 판단 ───────────────────────────────────────────────

    async def _scan_and_decide(
        self, *, data: bytes, file_name: str, mime: str, host: str
    ) -> bytes | None | object:
        """검사하고 판단을 받는다.

        반환: 마스킹 바이트 / `_ORIGINAL`(원본 그대로) / None(보내지 않음).
        """
        from app.core.pipeline.orchestrator import run_pipeline

        result = await run_pipeline(
            file_bytes=data, mime_type=mime, file_name=file_name, wrap_file=False
        )
        if result.get("blocked"):
            logger.info("[proxy] 정책 차단 file=%s", file_name)
            return None

        action = await broker.wait(
            file_name=file_name,
            host=host,
            result=result,
            timeout_s=config.PROXY_DECISION_TIMEOUT_S,
        )
        if action == "cancel":
            return None
        if action == "send_original":
            return _ORIGINAL
        return (result.get("maskedText") or "").encode("utf-8")

    # ── 차단 ─────────────────────────────────────────────────────────────────

    def _block(self, flow: http.HTTPFlow, reason: str) -> None:
        """요청을 사이트에 보내지 않고 여기서 끝낸다.

        상태 코드는 403 이다. 사이트 JS 가 이걸 업로드 실패로 보여 주는 게, 조용히
        성공한 척하는 것보다 낫다.
        """
        flow.response = http.Response.make(
            403,
            json.dumps({"error": "campfire_blocked", "reason": reason}, ensure_ascii=False).encode(),
            {"Content-Type": "application/json; charset=utf-8"},
        )


# "원본을 그대로 보낸다" 를 나타내는 표식. None(보내지 않음)과 구분해야 해서
# bytes 도 None 도 아닌 고유 객체를 쓴다.
_ORIGINAL = object()
