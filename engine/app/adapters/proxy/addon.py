"""
app/adapters/proxy/addon.py
mitmproxy 애드온 — 업로드 요청을 붙들고, 검사하고, 마스킹본으로 바꿔 보낸다.

확장 프로그램 경로와 결정적으로 다른 점: **되돌려 넣지 않는다.** 확장은 사이트가
이미 받은 파일을 우리 것으로 갈아끼워야 해서 주입·증거 체인이 필요했다. 여기서는
아직 사이트에 닿지 않은 요청의 본문을 고치는 것이라 그 기계가 통째로 없다.

사이트별 코드가 사라지는 건 아니다 — DOM 대신 와이어 포맷을 알아야 한다.
지금 상태:
  - multipart 로 올리는 곳(Claude/Copilot/Perplexity/Grok)은 한 처리로 덮는다.
  - ChatGPT 는 등록→PUT 2단계라 전용 처리가 있다. 흐름이 둘이고 크기 선언 여부가
    다르다 — upload_reservations 쪽은 선언이 없어 패딩도 필요 없다.
  - Gemini 는 push.clients6.google.com 으로 resumable 업로드를 한다. 크기를 먼저
    선언한다는 점에서 ChatGPT 와 같은 모양이라 같은 방법(선언값 재작성 + 패딩)을 쓴다.
  - Grok 의 예전 JSON+base64 업로드는 못 다뤄 **차단**한다(UNSUPPORTED_UPLOADS). 지금 쓰는
    경로는 multipart 라 위 처리로 덮는다.

등록에 파일 이름이 없는 흐름이 둘이라(ChatGPT upload_reservations, Gemini)
검사 직전에 바이트를 보고 종류를 채운다 — sniff.py 참고.

fail-closed 원칙: 이 파일의 모든 경로는 "판단이 안 서면 안 보낸다" 로 끝난다.
예외가 나면 원본이 흘러가는 게 아니라 403 으로 끊는다(_guard 참고).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass

from mitmproxy import http

from app import config
from app.adapters.proxy.decision import DecisionTimeout, broker
from app.adapters.proxy.multipart import MultipartError, decode, encode
from app.adapters.proxy.sniff import is_image, refine
from app.core import activity as activity_bus
from app.core import forced_mask
from app.store import db

logger = logging.getLogger("securedoc.proxy")

# 마스킹 결과는 항상 텍스트(MD)로 낸다.
#
# 엔진의 wrap_masked_file() 은 DOCX 를 내지만 여기서는 쓰지 않는다. ChatGPT 경로는
# 선언한 크기에 **정확히** 맞춰야 해서 뒤를 공백으로 채우는데, DOCX 는 ZIP 이라
# 꼬리에 바이트를 붙이는 게 안전하다고 검증되지 않았다. 텍스트는 자명하게 안전하다.
MASKED_MIME = "text/markdown"

# 프록시가 실제로 경로에 있음을 확장에게 알리는 표식. 확장은 이 헤더가 보이는
# 동안에만 자기 검사를 끈다(프록시·확장 병행 계획). 값은 뭐든 상관없고 존재만 본다.
# 소문자로 둔다 — HTTP/2 는 헤더 이름에 대문자를 허용하지 않아, mitmproxy 가 응답마다
# "Lowercased 'X-Campfire-Proxy' header ..." 를 로그에 남겼다(맥 실측). 헤더 이름은
# 대소문자를 가리지 않으므로 확장(interceptor.js 의 'x-campfire-proxy')은 그대로 읽는다.
PROXY_MARK_HEADER = "x-campfire-proxy"
# 확장이 "이 탭이 프록시를 지나는가" 를 묻는 경로(interceptor.js _probeProxyPath). 프록시가 직접 답한다.
PROXY_PROBE_PATH = "/.well-known/campfire-proxy-probe"


def masked_name_for(original: str) -> str:
    # 맥은 한글 파일 이름을 자모로 나눈 NFD 로 보낸다(맥 v0.2.59: Gemini 시작 요청 "File name: 보고서.docx" 가 NFD 날것
    # UTF-8). 바꾼 이름은 NFC 로 낸다 — 서버·화면이 보통 합친 글자로 다루고, 확장이 알린 이름(NFC)과도 같아진다.
    original = unicodedata.normalize("NFC", original)
    stem = original.rsplit(".", 1)[0] if "." in original else original
    # 이름에 사용자 지정 단어가 있으면 이름을 쓰지 않는다 — 내용을 가려도 이름으로 샌다.
    if forced_mask.registry.snapshot().find(stem):
        stem = "document"
    return f"{stem}_masked.md"


async def _run_pipeline(*, record_name: str, **kwargs) -> dict:
    """run_pipeline 을 돌리며 앱 처리현황에 방송하고 탐지 통계에 남긴다 — 확장(jobs.py)·MCP 와 같게.

    예전엔 프록시 검사만 둘 다 안 했다. 맥처럼 프록시가 프롬프트를 맡으면(확장 PROXY_PROMPT_HOSTS) 앱
    처리현황이 꿈쩍도 안 하고 통계(사용자 지정 건수 포함)도 오르지 않아, 검사가 안 되는 것처럼 보였다.
    확장이 이미 가린 파일을 프록시가 다시 검사하면 그 건도 한 번 더 센다(가린 뒤라 탐지는 0건).
    """
    from app.core.pipeline.orchestrator import run_pipeline

    job_id = f"proxy-{uuid.uuid4().hex}"
    activity_bus.job_started(job_id, source="proxy")

    async def emit(event: dict) -> None:
        activity_bus.job_event(job_id, event)

    ok = False
    try:
        result = await run_pipeline(emit=emit, **kwargs)
        ok = True
    finally:
        # run_pipeline 은 done 을 내지 않는다 — 여기서 끝내야 처리현황이 "탐지중" 으로 굳지 않는다.
        activity_bus.job_finished(job_id, ok=ok)
    # 이름에 등록어가 있으면 기록에도 남기지 않는다(masked_name_for 와 같은 이유).
    name = "document" if forced_mask.registry.snapshot().find(record_name) else record_name
    try:
        db.record_job(job_id, file_name=name, source="proxy", result=result)
    except Exception:  # noqa: BLE001 - 통계 기록 실패로 검사 결과를 버리지 않는다
        logger.exception("[proxy] 탐지 통계 기록 실패 — 검사 결과는 그대로 쓴다")
    return result


# ─── 사이트 표 ────────────────────────────────────────────────────────────────
#
# multipart 로 올리는 곳은 전부 같은 처리를 타고, 그렇지 않은 곳만 전용 처리가 붙는다.
MULTIPART_HOSTS = {
    "claude.ai",
    "copilot.microsoft.com",
    "www.perplexity.ai",
    "perplexity.ai",
    # 2026-10-26 실측: Perplexity 는 이제 파일을 자기 오리진이 아니라 S3 presigned POST(multipart, 파일 파트가 마지막,
    # key·policy·x-amz-* 칸이 앞)로 바로 올린다 — 이 호스트가 없어 원본이 검사 없이 나갔다. multipart 는 파일이 아닌
    # 칸을 바이트 그대로 다시 싸므로 서명 칸이 깨지지 않는다. 등록(batch_create_upload_urls)의 이름·형식은 아직
    # 못 바꿔서, 가린 글은 원래 형식 이름으로 저장된다 — 그래서 확장이 이 사이트 파일을 먼저 가린다(content.js
    # PROXY_FILE_HOSTS 에 없음). 이 호스트는 확장이 없는 경로의 안전망이다. 허용 이름이 늘어 CA 가 새로 만들어진다.
    "ppl-ai-file-upload.s3.amazonaws.com",
    # 2026-10-22 실측: Grok 은 이제 POST /http/upload-file-v2/direct 로 파일 하나(name="file")를
    # multipart 로 올린다. 예전 JSON+base64 경로(UNSUPPORTED_UPLOADS)만 막고 있어 그대로 나갔다.
    "grok.com",
    # Gemini 는 여기 없다. 업로드가 같은 오리진이 아니라
    # push.clients6.google.com 으로 가고 multipart 도 아니다(실측) — 전용 처리를
    # 따로 둔다. 여기 넣어 봐야 아무 효과가 없다.
}

CHATGPT_HOSTS = {"chatgpt.com", "chat.openai.com"}

# 등록(크기를 선언하고 업로드 URL 을 받는) 엔드포인트. 하나가 아니다.
#
# 2026-09-20 에 브라우저 콘솔로 쟀을 때는 /backend-api/files 였는데, 다음 날 실제
# UI 가 쓰는 것을 프록시로 보니 /backend-api/files/upload_reservations 였다 —
# 내가 고른 API 를 쟀지 제품이 쓰는 API 를 잰 게 아니었다. 정확히 일치하는 경로
# 하나만 보고 있었으므로 **파일이 검사 없이 그대로 나갔다.**
CHATGPT_REGISTER_PATHS = (
    "/backend-api/files",
    "/backend-api/files/upload_reservations",
)

# ChatGPT 이미지(2026-10-29 맥 캡처, 사용자 결정 A — 이미지는 늘 .md 로):
#   ① POST /backend-api/files  {"file_name":"image.png","mime_type":"image/png","client_resolved_mime_type":…,
#      "use_case":"multimodal","file_size":N} → 응답 file_id   (문서면 use_case "my_files")
#   ② PUT …oaiusercontent.com/…/raw  ct=image/png
#   ③ POST /backend-api/files/process_upload_stream {"file_id","file_name","mime_type","use_case":"multimodal"}
#   ④ f/conversation messages[].content {"content_type":"multimodal_text","parts":[{"content_type":"image_asset_pointer",
#      "asset_pointer":"sediment://file_…",…},"글"]} + metadata.attachments[{"id":"file_…","mime_type":"image/png","name",width,height}]
# 예전엔 ②만 바꿀 수 없어 가릴 것이 있으면 막았다(맥: 칩 "업로드 실패", 안내 없음). 넷을 file_id 로 이어 함께 바꾼다.
CHATGPT_PROCESS_PATH = "/backend-api/files/process_upload_stream"
_IMAGE_NAME = re.compile(r"\.(png|jpe?g|webp|bmp|gif|heic)$", re.I)

# 바이트가 실제로 올라가는 곳.
#
# 진짜 방어선은 여기다. 등록 경로가 또 바뀌든 응답 모양이 달라지든, 이 호스트로
# 가는 PUT 중 우리가 준비하지 않은 것은 막는다 — 경로 목록을 따라다니는 것보다
# 이쪽이 오래 간다.
CHATGPT_BLOB_HOST_SUFFIX = ".oaiusercontent.com"

# 업로드인 것은 아는데 그 형식을 아직 다룰 수 없는 자리.
#
# 통과시키면 원문이 그대로 나간다. "지원하지 않는다" 와 "그냥 보낸다" 는 다르고,
# 이 제품에서 후자는 곧 사고다. 그래서 막고 이유를 알린다.
# Grok 은 multipart 가 아니라 JSON 본문에 base64 를 넣어 보낸다(확장 쪽 실측).
UNSUPPORTED_UPLOADS: dict[str, tuple[str, ...]] = {
    "grok.com": ("/rest/app-chat/upload-file",),
}

# ─── Gemini (Google resumable upload) ────────────────────────────────────────
#
# 2026-09-21 로그인된 브라우저를 프록시에 붙여 직접 쟀다:
#   POST push.clients6.google.com/upload/  len=46      x-goog-upload-command: start
#         x-goog-upload-protocol: resumable
#         x-goog-upload-header-content-length: 554273   ← 여기서 크기를 미리 선언한다
#   POST push.clients6.google.com/upload/  len=554273  (본문 전체가 파일 바이트)
#
# 구조가 ChatGPT 와 같다 — 크기를 먼저 선언하고 나중에 바이트를 보낸다. 그래서
# 대응도 같다: 선언값을 N' 으로 바꿔 두고 본문을 N' 까지 채운다.
# multipart 가 아니라 raw 본문이라 오히려 다루기 쉽다(파트를 찾을 필요가 없다).
# ─── Claude 메시지 RPC (파일 업로드가 아닌 인라인 텍스트 경로) ─────────────────
#
# 2026-09-23 실측: 새 대화에서 텍스트 파일(.txt/.md)을 첨부하면 업로드가 아니라
# 메시지 protobuf 에 인라인돼 이 RPC 로 나간다. 손으로 친 프롬프트도 같은 경로다.
# 즉 파일 업로드 검사를 다 통과해도 텍스트는 여기로 샌다.
#
# protobuf 를 풀어 사용자 텍스트 필드만 마스킹본으로 바꾸고 다시 싼다(_claude_rpc).
# content-type 이 application/proto 라(connect+proto 아님) 봉투 없이 바로 protobuf 다 —
# 그 가정이 깨지면(decode 실패) 통과가 아니라 차단으로 떨어진다.
# 프롬프트를 검사하는 사이트는 이것뿐이다. 확장(content.js PROXY_PROMPT_HOSTS)은 이 목록에
# 있는 사이트에서만 프롬프트 검사를 프록시에 넘긴다 — 늘리면 거기도 같이 늘린다.
CLAUDE_RPC_HOSTS = {"claude.ai"}
CLAUDE_MESSAGE_RPC_PATHS = (
    "/claudeai-rpc/anthropic.bard.api.v1alpha.ConversationService/PerformAction",
)
# 2026-09-24 실측(캡처 5건 디코드): 사용자 콘텐츠는 최상위 필드 2(=전송 액션) 안에만 있다.
#   2.3     손으로 친 메시지 텍스트
#   2.15.4  인라인된 텍스트 파일의 내용 (2.15.1=파일명, 2.15.3=mime, 파일마다 2.15 반복)
# 나머지(세션 id·UUID·모델명 'claude-opus-5-5'·'ko-KR'·'Asia/Seoul')는 메타데이터라
# 손대지 않는다. 필드 2 가 없는 액션(메타 핑 등)은 사용자 콘텐츠가 없어 통과시킨다.
CLAUDE_PROMPT_PATH = (2, 3)
CLAUDE_USER_TEXT_PATHS = {CLAUDE_PROMPT_PATH, (2, 15, 4)}

# ─── 다른 사이트의 프롬프트 전송 ──────────────────────────────────────────────
#
# 2026-10-22 실측: 임시 크롬 프로필(비로그인, 이어서 로그인)을 측정용 프록시에 붙여, 표식이 든
# 문장을 입력·전송하고 그 표식이 실린 요청·웹소켓 메시지를 모두 기록했다(표식은 막았다).
#   ChatGPT(로그인)    POST /backend-api/f/conversation — JSON messages[*].content.parts[*] (role=user)
#   ChatGPT(비로그인)  POST /unauth-mweb/conversation/updates — form-urlencoded, 필드 prompt
#   Perplexity         POST /rest/sse/perplexity_ask — JSON query_str 와 params.dsl_query(같은 글)
#   Grok               WS   grok.com/ws/mgw/ — event.type=response.create, input_chunks[*].text.text
# 모두 2턴째에도 새 글만 싣고 앞 턴은 서버가 들고 있다(ChatGPT 비로그인·Perplexity 로 확인).
#
# Copilot 은 여기 없다. copilot.com 으로 옮긴 뒤 글은 WS substrate.office.com/m365Copilot/Chathub/,
# 파일은 graph.microsoft.com createUploadSession → OneDrive(회사 계정은 SharePoint) PUT 으로 간다.
# 모두 Outlook·Teams·OneDrive 가 같이 쓰는 M365 호스트라, PAC 에 넣으면 그 프로그램들의 통신까지
# 이 프록시를 지나고 엔진이 재시작하는 동안 끊긴다. Copilot 은 확장이 맡는다(content.js 'copilot.com',
# 입력 중 자동완성은 rules/typing-leaks.json).
#
# 칸 찾기 함수의 약속: None = 이 메시지는 전송이 아니다(통과), [] = 전송인데 글 칸을 못 찾았다
# (사이트 개편 — 통과가 아니라 차단). 칸은 (담는 dict/list, 키) 쌍이라 그 자리에 마스킹본을 넣는다.


def _chatgpt_slots(body):
    slots = []
    for m in body.get("messages") or []:
        if isinstance(m, dict) and (m.get("author") or {}).get("role") == "user":
            parts = (m.get("content") or {}).get("parts")
            if isinstance(parts, list):
                slots += [(parts, i) for i, p in enumerate(parts) if isinstance(p, str)]
    # 다시 생성(variant) 등은 새 글이 없다. 새 메시지(next)인데 글 칸이 없으면 모르는 모양이다.
    return slots or ([] if body.get("action") == "next" else None)


def _perplexity_slots(body):
    query = body.get("query_str")
    if not isinstance(query, str):
        return []
    # 같은 글이 query_str·params.dsl_query 말고 다른 칸에도 실릴 수 있다(Windows v0.2.59 WPX-1: 모델은 가린 글을 받았는데
    # 저장된 말풍선은 원문) — 공백만 다른 것까지 같은 글이 실린 칸을 모두 가린다. 남은 칸은 _raw_left 가 막는다.
    norm = " ".join(query.split())
    return list(_same_text_slots(body, norm)) if norm else [(body, "query_str")]


def _same_text_slots(obj, norm):
    """obj 안에서 글(공백 접음)이 norm 과 같은 칸 (담는 dict/list, 키) 를 모두 찾는다."""
    items = obj.items() if isinstance(obj, dict) else enumerate(obj) if isinstance(obj, list) else ()
    for k, v in items:
        if isinstance(v, str):
            if " ".join(v.split()) == norm:
                yield obj, k
        else:
            yield from _same_text_slots(v, norm)


def _grok_slots(msg):
    event = msg.get("event") if isinstance(msg, dict) else None
    if not isinstance(event, dict) or event.get("type") != "response.create":
        return None
    chunks = (((event.get("item") or {}).get("x_grok") or {}).get("input_chunks")) or []
    return [(c["text"], "text") for c in chunks
            if isinstance(c, dict) and isinstance(c.get("text"), dict)
            and isinstance(c["text"].get("text"), str)]


PROMPT_FORM_ENDPOINTS: dict[tuple[str, str], str] = {
    ("chatgpt.com", "/unauth-mweb/conversation/updates"): "prompt",
    ("chat.openai.com", "/unauth-mweb/conversation/updates"): "prompt",
}
PROMPT_JSON_ENDPOINTS = {
    ("chatgpt.com", "/backend-api/f/conversation"): _chatgpt_slots,
    ("chat.openai.com", "/backend-api/f/conversation"): _chatgpt_slots,
    ("www.perplexity.ai", "/rest/sse/perplexity_ask"): _perplexity_slots,
    ("perplexity.ai", "/rest/sse/perplexity_ask"): _perplexity_slots,
}
# (호스트, 경로 접두사) → 칸 찾기. 메시지 하나가 JSON 하나다.
PROMPT_WS_ENDPOINTS = {
    ("grok.com", "/ws/mgw/"): _grok_slots,
}

# Gemini 프롬프트(2026-10-29 맥 실측, 확장 끔·프록시 켬): batchexecute·StreamGenerate 가 아니라 WebChannel 로 간다.
#   POST geminiweb-pa.clients6.google.com/v1/processSession?VER=8&…  form "count=N&ofs=K&req0___data__=<base64 protobuf>…"
#   ofs=0 세션 설정(언어·대화 id·첨부 목록), 다음이 프롬프트 {2: {1: 1, 9: {1: "글"}}}, 끝이 {4: {1: 1}}.
#   답은 같은 경로의 GET(TYPE=xmlhttp) 롱폴로 흘러온다.
# 이 호스트가 PAC 밖이라 크롬이 QUIC 로 직결해 프록시가 전송을 아예 못 봤다 — 저장본에 원문이 그대로 남았다.
GEMINI_PROMPT_HOST = "geminiweb-pa.clients6.google.com"
GEMINI_PROMPT_PATH = "/v1/processSession"
GEMINI_PROMPT_FIELD = (2, 9, 1)
_WEBCHANNEL_DATA = re.compile(r"req\d+___data__")

# 전송이 아닌데 친 글을 그대로 싣는 요청(같은 실측). 전송을 가려도 원문이 여기로 샌다.
# 검사할 자리가 아니라(입력 중에 계속 나간다) 아예 보내지 않는다 — 자동완성·분석만 빠진다.
#   ChatGPT     POST /backend-api/f/conversation/prepare  partial_query(입력 중인 글, 전송 전 미리 보냄)
#               — 실패해도 전송은 client_prepare_state=failure 로 그대로 나간다(실측).
#   Grok        GET  /_worker/typeahead?q=<입력 중인 글>
#   Perplexity  WS   suggest.perplexity.ai/suggest/ws {"q": 입력 중인 글}
#               POST suggest.perplexity.ai/search/v2/navigate {"query": 글}
#               POST /rest/event/analytics  event_data.queryStr (질문 제출·실패 이벤트)
# 확장도 같은 목록을 막는다(extension/rules/typing-leaks.json) — 프록시를 끈 상태를 위해.
PROMPT_ECHO_HOSTS = {"suggest.perplexity.ai"}
PROMPT_ECHO_PATHS: dict[str, tuple[str, ...]] = {
    "chatgpt.com": ("/backend-api/f/conversation/prepare",),
    "chat.openai.com": ("/backend-api/f/conversation/prepare",),
    "grok.com": ("/_worker/typeahead",),
    "www.perplexity.ai": ("/rest/event/analytics",),
    "perplexity.ai": ("/rest/event/analytics",),
}

GEMINI_UPLOAD_HOST = "push.clients6.google.com"
GEMINI_UPLOAD_PATH = "/upload/"
# 실제 파일은 위 업로드 호스트로 가지만, 확장은 현재 탭과 같은 호스트의 응답만
# "이 탭이 프록시를 지난다"는 증거로 인정한다. 메인 호스트를 PAC 에도 넣어야
# gemini.google.com 의 same-origin probe 응답에 표식을 달 수 있다. 이 호스트의
# 프롬프트는 프록시가 해석하지 않고 확장이 계속 검사한다(PROXY_PROMPT_HOSTS 참고).
GEMINI_ROUTE_HOST = "gemini.google.com"
# Gemini 파일(2026-10-29 맥 캡처, cfprobe.txt):
#   ① POST push.clients6/upload/ start   본문 "File name: cfprobe.txt", 길이 헤더 x-goog-upload-header-content-length
#   ② POST …?upload_id=… "upload, finalize"  본문 = 파일 바이트, 응답 본문 = 파일 경로 "/contrib_service/ttl_1d/…"
#      (서버는 여기서 형식을 보지 않는다)
#   ③ POST gemini.google.com …/BardFrontendService/ProcessFile  f.req 안 [[경로, null, 1, "text/plain"], "cfprobe.txt"]
#      → 파일 id. **형식(MIME)과 이름은 여기 적힌다.**
#   ④ 전송 processSession ofs=0 세션 설정 안 첨부 항목 {1: {1: 경로, 2: 3, 4: MIME, 5: 파일 id}, 2: 이름, 7: 0}
# 예전엔 ②만 보고 가린 글을 넣으면 Gemini 가 docx 로 읽으려다 깨진 파일로 거부해 막기만 했다(맥: docx·png·py 전부 막힘).
# 확장처럼 등록부터 _masked.md 로 보이게 ①의 이름, ②의 본문, ③·④의 형식·이름을 함께 바꾸고, 경로로 잇는다.
_GEMINI_NAME_PREFIX = "File name: "
GEMINI_PROCESS_FILE_PATH = "/_/BardChatUi/data/assistant.lamda.BardFrontendService/ProcessFile"
GEMINI_MASKED_MIME = "text/plain"
_GOOG_CMD = "x-goog-upload-command"
_GOOG_LEN = "x-goog-upload-header-content-length"
_GOOG_URL = "x-goog-upload-url"


def gemini_session_key(url: str) -> str:
    """업로드 세션 식별자.

    URL 문자열을 통째로 키로 쓰면 안 된다 — 응답이 준 URL 과 브라우저가 실제로
    보내는 URL 이 포트 표기나 파라미터 순서에서 어긋나면 매칭이 깨지고, 그러면
    (fail-closed 라) Gemini 업로드가 통째로 막힌다. 세션을 가리키는 값만 쓴다.
    """
    query = url.split("?", 1)[1] if "?" in url else ""
    for part in query.split("&"):
        if part.startswith("upload_id="):
            return part[len("upload_id="):]
    return url.split("?", 1)[0]


# 추적 중인 업로드 수 상한(ChatGPT·Gemini 공통). 재시도를 위해 약속값을 남겨 두기 때문에
# 그냥 두면 무한히 쌓인다. 동시에 올릴 수 있는 수보다 넉넉하면 충분하다.
_MAX_TRACKED_UPLOADS = 64

# 확장이 검토해 사람이 승인한 마스킹본 — 확장이 사이트에 다시 넣기 직전에 알린다(/proxy/approved-uploads).
# 프록시가 이걸 처음 보는 업로드로 여겨 다시 검사하면 사용자 프롬프트 없이 인젝션을 판정해 승인한 글을 말없이
# 바꾸고(맥 v0.2.58 실측: 마지막 줄을 [인젝션 마스킹]으로), ChatGPT 등록의 이름·크기까지 고쳤다("x_masked_masked.md",
# 크기 4N+64K). 해시가 같은 바이트만 그대로 보낸다 — 내용 속 표식이었다면 문서가 흉내 낼 수 있다.
_APPROVED_TTL_S = 600.0
_approved: dict[str, tuple[str, int, float]] = {}  # sha256 → (파일 이름, 크기, 만료 시각)


def approve_uploads(items: list[tuple[str, str, int]]) -> None:
    now = time.monotonic()
    for key in [k for k, v in _approved.items() if v[2] < now]:
        _approved.pop(key, None)
    for sha, name, size in items:
        _approved[sha] = (unicodedata.normalize("NFC", name), size, now + _APPROVED_TTL_S)
    while len(_approved) > _MAX_TRACKED_UPLOADS:
        _approved.pop(next(iter(_approved)))


def _approved_bytes(data: bytes) -> bool:
    hit = _approved.get(hashlib.sha256(data).hexdigest())
    return hit is not None and hit[2] >= time.monotonic()


def _approved_meta(name: str, size: int) -> bool:
    # 이름은 NFC 로 견준다 — 확장은 NFC 로 알리는데 맥 사이트는 NFD 로 보내, 같은 마스킹본을 못 알아보고 이름을 또 바꿨다.
    name = unicodedata.normalize("NFC", name)
    now = time.monotonic()
    return any(n == name and s == size and exp >= now for n, s, exp in _approved.values())


def intercept_hosts() -> tuple[list[str], list[str]]:
    """검사 대상 호스트 — (정확히 일치하는 것, 이 접미사로 끝나는 것).

    이 한 목록에서 두 가지가 나온다:
      - mitmproxy allow_hosts: 이 호스트만 TLS 를 풀고 나머지는 터널로 넘긴다
      - 앱의 PAC 파일: 이 호스트만 프록시로 보내고 나머지는 직접 나간다
    어긋나면 그 호스트는 조용히 검사 밖으로 빠진다. 그래서 **위 표들에서 만들고**
    손으로 따로 적지 않는다.
    """
    exact = (
        MULTIPART_HOSTS
        | CHATGPT_HOSTS
        | {GEMINI_UPLOAD_HOST, GEMINI_ROUTE_HOST, GEMINI_PROMPT_HOST}
        | set(UNSUPPORTED_UPLOADS)
        | {h for h, _ in PROMPT_FORM_ENDPOINTS}
        | {h for h, _ in PROMPT_JSON_ENDPOINTS}
        | {h for h, _ in PROMPT_WS_ENDPOINTS}
        | PROMPT_ECHO_HOSTS
        | set(PROMPT_ECHO_PATHS)
        | config.PROXY_EXTRA_HOSTS
    )
    # 서브도메인이 매번 바뀌는 blob 호스트(sdmntprcentralus.oaiusercontent.com …)
    suffixes = [CHATGPT_BLOB_HOST_SUFFIX.lstrip(".")]
    return sorted(exact), suffixes


def intercept_host_patterns() -> list[str]:
    """intercept_hosts() 를 mitmproxy allow_hosts 정규식으로.

    여기 없는 호스트는 복호화하지 않는다. 전부 풀면 Windows 인증서 저장소를 쓰지
    않는 프로그램(git·Python·Node 등)이 우리 CA 를 몰라 인증서 오류로 죽는다.
    """
    import re

    exact, suffixes = intercept_hosts()
    pats = [rf"^{re.escape(h)}(:\d+)?$" for h in exact]
    pats += [rf"^(.+\.)?{re.escape(s)}(:\d+)?$" for s in suffixes]
    return pats


def _multipart_hosts() -> set[str]:
    """매번 읽는다 — 환경변수로 더한 호스트가 재시작 없이 먹어야 한다."""
    return MULTIPART_HOSTS | config.PROXY_EXTRA_HOSTS


@dataclass
class ChatGptUpload:
    """등록 단계에서 잡아 둔 값. PUT 이 올 때 쓴다."""

    # 등록에 써 넣은 N'. None 이면 **크기를 선언하지 않는 흐름**이라 패딩이 필요 없다.
    #
    # ChatGPT 에는 업로드 흐름이 둘 있다(2026-09-21 실측):
    #   (a) /backend-api/files            — 요청에 file_size 가 있다 → 맞춰야 한다
    #   (b) /backend-api/files/upload_reservations — 크기 선언이 없다 → 그냥 보내면 된다
    # (b) 는 UI 가 실제로 쓰는 쪽이고, 패딩이 없으니 낭비도 없다.
    declared: int | None
    masked_name: str
    original_name: str
    # 판단이 끝난 뒤의 최종 본문. 같은 PUT 이 다시 오면 이걸 그대로 쓴다.
    #
    # 예전엔 PUT 을 처리하면서 약속값을 pop 했는데, 그러면 **재시도가 통과한다** —
    # 두 번째 PUT 은 키를 못 찾아 분기를 안 타고 원본이 그대로 나간다. 업로드
    # 재시도는 흔한 일이라 그냥 두면 실제로 밟힌다.
    settled: bytes | None = None
    blocked: bool = False  # 한 번 막았으면 재시도도 막는다
    # 그림을 등록부터 _masked.md 로 바꿨다(ChatGPT 이미지, 사용자 결정 A) — 원본 그림은 보낼 수 없고 늘 글(OCR)로 간다.
    as_text: bool = False
    file_id: str | None = None  # 등록 응답의 file_id — 전송 메시지의 첨부와 잇는다(인젝션 2단계)


@dataclass
class PendingFile:
    """프록시 방식에서 올렸지만 아직 보내지 않은 첨부 — 전송 때 그 프롬프트와 함께 인젝션을 판정한다."""

    name: str        # 사용자에게 알릴 이름(_masked.md, 등록어가 있으면 document_masked.md)
    text: str        # 업로드 때 읽어 가린 글
    ids: tuple[str, ...]  # 전송 본문에서 찾을 첨부 참조(ChatGPT file_id). 비면 이 사이트의 다음 전송에 붙은 것으로 본다
    at: float


# 인젝션 2단계(프록시 방식): 업로드 때는 사용자 프롬프트가 없어 문서 속 평범한 요청("…정리해 주세요")까지 인젝션으로
# 잡혔다(맥 v0.2.58). 인젝션 판정은 1·2차 모두 사용자 요청을 기준으로 하는데, 프롬프트 없이 "뚜렷한 공격 문구" 만 고를
# 보정된 기준은 없다. 그래서 업로드 때는 PII·사용자 지정 단어만 보고, 전송 때 그 프롬프트와 함께 첨부 글을 판정한다.
# 이미 올라간 파일은 다시 가릴 수 없으니 인젝션이면 전송을 막고 이유를 알린다.
_PENDING_TTL_S = 1800.0
# 참조를 모르는 사이트(Claude·Grok·Perplexity)의 첨부를 "다음 전송" 에 붙이는 시간 — 올린 뒤 이보다 지나면 올렸다
# 지운 첨부일 수 있어 붙이지 않는다(_judge_attached).
_GUESS_WINDOW_S = 300.0


def _site(host: str) -> str:
    """업로드 호스트와 전송 호스트를 같은 사이트로 묶는다(Gemini 업로드는 push.clients6, ChatGPT 는 blob 호스트)."""
    if host in CHATGPT_HOSTS or host.endswith(CHATGPT_BLOB_HOST_SUFFIX):
        return "chatgpt.com"
    if host in (GEMINI_UPLOAD_HOST, GEMINI_PROMPT_HOST, GEMINI_ROUTE_HOST):
        return GEMINI_ROUTE_HOST
    if "perplexity" in host or host.startswith("ppl-ai-file-upload."):  # 업로드는 S3 presigned 호스트로 간다
        return "perplexity.ai"
    return host


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
        # 그림을 _masked.md 로 바꿔 등록한 file_id → 바꾼 이름. ③ process_upload_stream·④ 전송에서 맞춘다.
        self._chatgpt_files: dict[str, str] = {}
        # 사이트 → 올렸지만 아직 안 보낸 첨부(인젝션 2단계)
        self._pending: dict[str, list[PendingFile]] = {}
        # Gemini WebChannel 세션(SID) → 그 세션 POST 를 들어온 순서대로 내보내는 자물쇠(_on_request 참고)
        self._gemini_order: dict[str, asyncio.Lock] = {}
        # Gemini WebChannel 세션(SID) → 그 세션에서 본 데이터(풀어 둔 protobuf). 첨부 항목의 파일 경로로 전송에 실린
        # 첨부를 잇는다 — 세션 설정과 프롬프트가 따로 오는 경우가 있다(_gemini_session).
        self._gemini_refs: dict[str, bytes] = {}
        # Gemini 는 두 요청이 **같은 경로**(/upload/)로 와서 경로로는 못 가른다.
        # 시작 응답이 돌려주는 업로드 URL 의 세션 식별자로 가른다.
        self._gemini: dict[str, ChatGptUpload] = {}
        # 업로드를 마친 Gemini 파일의 경로(② 응답 본문) → 바꾼 이름. ③ ProcessFile·④ 전송에서 형식·이름을 맞춘다.
        self._gemini_files: dict[str, str] = {}

    # ── mitmproxy 훅 ─────────────────────────────────────────────────────────

    async def request(self, flow: http.HTTPFlow) -> None:
        try:
            await self._on_request(flow)
        except DecisionTimeout as exc:
            # 확인창이 떠 있던 건이라 데스크톱이 이미 "검토 시간이 지나" 를 알린다 — 알림을 또 띄우지 않는다.
            self._block(flow, f"검토 시간이 지나 전송을 취소했습니다 ({exc})", notify=False)
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
        if flow.request.path.split("?")[0] == PROXY_PROBE_PATH:
            # 확장의 경로 확인(HEAD, interceptor.js _probeProxyPath)은 사이트까지 보내지 않고 여기서 답한다 — 예전엔
            # AI 사이트가 404 로 답해 콘솔에 빨간 줄이 남았고, 확인에 인터넷 왕복(~1초)이 들어 그동안 전송을 붙들었다.
            flow.response = http.Response.make(204, b"", {PROXY_MARK_HEADER: "1", "cache-control": "no-store"})
            return
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
        key = flow.request.path.split("?")[0]
        if host in CHATGPT_HOSTS and key == CHATGPT_PROCESS_PATH and flow.request.method == "POST":
            self._chatgpt_process(flow)
            return
        if host in CHATGPT_HOSTS and key in CHATGPT_REGISTER_PATHS:
            self._chatgpt_register(flow)
            return
        if flow.request.method == "PUT" and host.endswith(CHATGPT_BLOB_HOST_SUFFIX):
            if key not in self._chatgpt:
                # 등록을 못 봤다 = 어떤 크기를 약속했는지 모른다. 예전엔 이 경우
                # 조용히 통과했고, 등록 경로가 바뀐 날 파일이 그대로 나갔다.
                logger.warning("[proxy] 준비되지 않은 ChatGPT 업로드 — 차단 %s%s", host, key)
                self._block(flow, "업로드 세션을 확인하지 못해 전송을 막았습니다")
                return
            await self._chatgpt_put(flow, key)
            return
        if host == GEMINI_UPLOAD_HOST and key == GEMINI_UPLOAD_PATH:
            await self._gemini_upload(flow)
            return
        if flow.request.method == "POST" and key in UNSUPPORTED_UPLOADS.get(host, ()):
            logger.warning("[proxy] 다루지 못하는 업로드 형식 — 차단 %s%s", host, key)
            self._block(flow, "아직 지원하지 않는 업로드 형식이라 전송을 막았습니다")
            return
        if host in CLAUDE_RPC_HOSTS and key in CLAUDE_MESSAGE_RPC_PATHS:
            await self._claude_rpc(flow)
            return
        if host == GEMINI_PROMPT_HOST and key == GEMINI_PROMPT_PATH and flow.request.method == "POST":
            # 같은 세션의 POST 는 들어온 순서대로 내보낸다. h2/h3 에서는 클라이언트가 forward POST 를 여러 개 동시에
            # 보내는데, 프롬프트(ofs=1)를 검사하느라 붙드는 동안 뒤따르는 "턴 끝"(ofs=2)이 먼저 서버에 가면 답이 시작은
            # 되지만 턴이 마감되지 않아 멈추고 대화가 저장되지 않았다(맥 v0.2.59 R-1, 2회 재현). asyncio.Lock 은
            # 기다린 순서대로 넘겨 준다. SID 가 없는 세션 열기 POST 는 막지 않는다.
            sid = flow.request.query.get("SID") or flow.request.query.get("gsessionid") or ""
            if not sid:
                await self._gemini_session(flow)
                return
            lock = self._gemini_order.setdefault(sid, asyncio.Lock())
            while len(self._gemini_order) > _MAX_TRACKED_UPLOADS:  # 오래된 세션부터 — 쓰는 중인 자물쇠는 남긴다
                old = next(k for k in self._gemini_order)
                if self._gemini_order[old].locked() or old == sid:
                    break
                self._gemini_order.pop(old)
            async with lock:
                await self._gemini_session(flow)
            return
        if host in PROMPT_ECHO_HOSTS or key in PROMPT_ECHO_PATHS.get(host, ()):
            self._swallow(flow)
            return
        if flow.request.method == "POST" and (host, key) in PROMPT_FORM_ENDPOINTS:
            await self._form_prompt(flow, host, PROMPT_FORM_ENDPOINTS[(host, key)])
            return
        if flow.request.method == "POST" and (host, key) in PROMPT_JSON_ENDPOINTS:
            await self._json_prompt(flow, host, PROMPT_JSON_ENDPOINTS[(host, key)])
            return
        if host == GEMINI_ROUTE_HOST and key == GEMINI_PROCESS_FILE_PATH and flow.request.method == "POST":
            self._gemini_process_file(flow)
            return
        if host in _multipart_hosts() and flow.request.method == "POST":
            await self._multipart(flow, host)

    def _on_response(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host

        # 확장 프로그램에게 "네 트래픽이 실제로 이 프록시를 지난다"를 알린다.
        # 확장은 이 헤더가 보일 때만 손을 뗀다(파일 가로채기 중단. 프롬프트는 이 애드온이
        # 검사하는 claude.ai 에서만 넘긴다 — content.js PROXY_PROMPT_HOSTS).
        # "프록시 프로세스가 떠 있나"(status)로 판단하면, PAC 를 안 따르는 브라우저가
        # 확장까지 끈 채 무방비가 된다 — 실제 경로 증거로만 양보하게 하려는 헤더다.
        # AI 사이트 응답에만 붙으므로 확장 content script(같은 오리진)가 읽을 수 있다.
        if flow.response is not None:
            flow.response.headers[PROXY_MARK_HEADER] = "1"
            # 다른 서브도메인에서 부르는 경우(cross-origin)에도 JS 가 읽게 노출한다.
            prev = flow.response.headers.get("access-control-expose-headers", "")
            if PROXY_MARK_HEADER.lower() not in prev.lower():
                flow.response.headers["access-control-expose-headers"] = (
                    f"{prev}, {PROXY_MARK_HEADER}".lstrip(", ") if prev else PROXY_MARK_HEADER
                )

        if (config.PROXY_LOG_REQUESTS and host in CHATGPT_HOSTS
                and flow.request.path.endswith("/claim_and_finish")):
            logger.info("[proxy]   확정 req %s", (flow.request.get_text() or "")[:400])
        if host in CHATGPT_HOSTS and flow.request.path.split("?")[0] in CHATGPT_REGISTER_PATHS:
            if config.PROXY_LOG_REQUESTS and flow.response is not None:
                # 등록의 요청·응답 모양을 남긴다. 필드 이름이 사이트 개편마다
                # 바뀌는 자리라, 바뀐 걸 로그로 알 수 있어야 한다.
                logger.info("[proxy]   등록 req %s", (flow.request.get_text() or "")[:400])
                logger.info("[proxy]   등록 res %s", (flow.response.get_text() or "")[:400])
            self._chatgpt_register_response(flow)
        elif host == GEMINI_UPLOAD_HOST:
            self._gemini_start_response(flow)
            self._gemini_finalize_response(flow)

    # ── Claude 메시지 RPC 경로 ───────────────────────────────────────────────

    async def _claude_rpc(self, flow: http.HTTPFlow) -> None:
        from app.adapters.proxy import protobuf as pb

        body = flow.request.content or b""

        # 필드 구조를 뜨기 위한 캡처(옵션). 값은 검증용 가짜 텍스트라 민감하지 않다.
        cap = config.PROXY_CAPTURE_DIR
        if cap:
            try:
                import os
                import time

                os.makedirs(cap, exist_ok=True)
                fn = os.path.join(cap, f"perform_{int(time.time()*1000)}_{len(body)}.bin")
                with open(fn, "wb") as fh:
                    fh.write(body)
                logger.info("[proxy] RPC 캡처 %s (%d bytes, ct=%s)",
                            fn, len(body), flow.request.headers.get("content-type", "-"))
            except Exception:
                logger.exception("[proxy] RPC 캡처 실패")

        # 봉투 없는 application/proto 만 다룬다. connect+proto(봉투 있음)로 바뀌면
        # 우리 코덱 가정이 깨지므로 통과가 아니라 차단으로 떨어진다.
        ctype = flow.request.headers.get("content-type", "")
        if "connect" in ctype.lower() or "grpc" in ctype.lower():
            logger.warning("[proxy] Claude RPC 가 봉투 형식(%s) — 아직 못 다뤄 차단", ctype)
            self._block(flow, "아직 지원하지 않는 메시지 형식이라 전송을 막았습니다")
            return

        try:
            msg = pb.decode(body)
        except pb.DecodeError:
            # protobuf 로 안 읽힌다 = 우리가 모르는 모양이다. 통과시키면 검사 없이 나간다.
            logger.warning("[proxy] Claude RPC 본문을 못 읽었다 — 차단")
            self._block(flow, "메시지 형식을 해석하지 못해 전송을 막았습니다")
            return

        # 필드 2(전송 액션)가 없으면 사용자 콘텐츠가 없는 액션이다 — 그대로 통과.
        if not any(f.number == 2 for f in msg.fields):
            return

        # 사용자 콘텐츠 필드만 고른다(2.3, 2.15.4). looks_like_text 로 한 번 더 거른다 —
        # 파일 참조 id 등이 그 자리에 오면 텍스트가 아니므로 지나간다.
        picked = [
            (path, f) for path, f in msg.walk()
            if path in CLAUDE_USER_TEXT_PATHS
            and f.message is None and f.value is not None and pb.looks_like_text(f.value)
        ]
        # 확장이 검토해 사람이 승인한 마스킹본을 Claude 는 업로드하지 않고 여기(2.15.4)에 글로 넣는다(맥 실측: files 요청
        # 없이 이 RPC 하나). 다시 검사하면 사용자 프롬프트 없이 인젝션을 판정해 승인한 글을 또 묻는다 — 해시가 같은 바이트만
        # 뺀다. 프롬프트 칸은 승인 대상이 아니라 그대로 검사한다.
        approved = [f.value for path, f in picked if path != CLAUDE_PROMPT_PATH and _approved_bytes(f.value)]
        picked = [(path, f) for path, f in picked if path == CLAUDE_PROMPT_PATH or not _approved_bytes(f.value)]
        if not picked:
            return  # 전송인데 텍스트 콘텐츠가 없다(드묾) — 건드릴 것 없음.

        masked = await self._scan_texts_and_decide(
            [f for _, f in picked], host="claude.ai",
            prompt_fields=[f for path, f in picked if path == CLAUDE_PROMPT_PATH],
            flow=flow,
        )
        if masked is None:
            self._block(flow, _CANCELLED)
            return
        if masked is _ORIGINAL:
            return  # 사람이 원본 전송을 택했다 — 본문 그대로 보낸다.
        for fld, new_bytes in masked:
            fld.set_bytes(new_bytes)
        flow.request.content = msg.serialize()
        if self._raw_left(flow, flow.request.content, exclude=approved):
            self._block(flow, _RAW_LEFT)

    # ── Gemini 프롬프트 (WebChannel processSession) ─────────────────────────

    async def _gemini_session(self, flow: http.HTTPFlow) -> None:
        """form 의 reqN___data__(base64 protobuf) 마다 2.9.1 프롬프트를 검사하고 가린 글로 다시 싼다.

        form 은 parse_qsl 로 풀지 않는다 — 값의 base64 '+' 가 날것으로 오면 공백으로 바뀐다. 바꾼 칸만 다시 쓰고
        나머지 칸(count·ofs·세션 설정)은 바이트 그대로 둔다. 못 읽는 데이터 칸이나, 사용자 턴 모양({2: {9: …}})인데
        글 칸이 없는 것은 모르는 모양이라 통과가 아니라 막는다.
        """
        from urllib.parse import quote, unquote

        from app.adapters.proxy import protobuf as pb

        pairs = [p.split("=", 1) for p in (flow.request.get_text() or "").split("&")]
        found = []  # (pairs 인덱스, 메시지, 프롬프트 필드들)
        renamed = {}  # pairs 인덱스 → 첨부 항목의 형식·이름을 고친 메시지(세션 설정)
        sid = flow.request.query.get("SID") or flow.request.query.get("gsessionid") or ""
        refs = self._gemini_refs.get(sid, b"")  # 이 세션에서 지금까지 본 데이터(첨부 항목의 파일 경로가 들어 있다)
        for i, pair in enumerate(pairs):
            if len(pair) != 2 or not _WEBCHANNEL_DATA.fullmatch(unquote(pair[0])):
                continue
            try:
                raw = base64.b64decode(unquote(pair[1]), validate=True)
                msg = pb.decode(raw)
                refs = (refs + raw)[-65536:]
            except (ValueError, pb.DecodeError):
                logger.warning("[proxy] Gemini 전송 데이터를 읽지 못했다 — 차단")
                self._block(flow, "메시지 형식을 해석하지 못해 전송을 막았습니다")
                return
            fields = [f for path, f in msg.walk() if path == GEMINI_PROMPT_FIELD
                      and f.wire_type == 2 and f.value is not None and pb.looks_like_text(f.value)]
            if fields:
                found.append((i, msg, fields))
            # 사용자 턴은 최상위가 필드 2 하나뿐이다(실측). 세션 설정은 최상위 필드가 여럿이라 여기 안 걸린다.
            elif {f.number for f in msg.fields} == {2} and any(path == GEMINI_PROMPT_FIELD[:2] for path, _ in msg.walk()):
                logger.warning("[proxy] Gemini 사용자 턴인데 글 칸이 없다 — 차단")
                self._block(flow, "메시지 형식을 해석하지 못해 전송을 막았습니다")
                return
            # 우리가 _masked.md 로 바꿔 올린 첨부는 세션 설정 안 형식·이름도 맞춘다(④) — 원래 형식(docx 등)으로 두면
            # Gemini 가 가린 글을 그 형식으로 읽으려다 깨진 파일로 본다.
            if self._gemini_files and _rename_gemini_items(msg, self._gemini_files):
                renamed[i] = msg
        # 세션 설정(첨부 목록)과 프롬프트가 한 POST 에 묶여 오기도, 따로 오기도 한다(맥 캡처) — 세션별로 모아 둔다.
        if sid:
            self._gemini_refs[sid] = refs
            while len(self._gemini_refs) > _MAX_TRACKED_UPLOADS:
                self._gemini_refs.pop(next(iter(self._gemini_refs)))
        flow.metadata["campfire_attach_refs"] = refs
        if not found and not renamed:
            return  # 세션 열기·설정·턴 끝 — 사용자 글이 없다

        changed = dict(renamed)
        if found:
            prompts = [f for _, _, fields in found for f in fields]
            masked = await self._scan_texts_and_decide(prompts, host=GEMINI_ROUTE_HOST, prompt_fields=prompts, flow=flow)
            if masked is None:
                self._block(flow, _CANCELLED)
                return
            if masked is not _ORIGINAL:
                for fld, new_bytes in masked:
                    fld.set_bytes(new_bytes)
                changed.update({i: msg for i, msg, _ in found})
        if not changed:
            return
        for i, msg in changed.items():
            pairs[i][1] = quote(base64.b64encode(msg.serialize()).decode("ascii"), safe="=")
        flow.request.text = "&".join("=".join(p) for p in pairs)
        if self._raw_left(flow, flow.request.content):
            self._block(flow, _RAW_LEFT)

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        # Gemini 답은 processSession GET 롱폴로 흘러온다. 프록시는 본문을 통째로 받은 뒤 넘기므로(stream_large_bodies
        # 없음) 그대로 두면 답이 다 끝날 때까지 안 보이거나 멈춘다. 이 호스트 응답은 검사하지 않으니 흘려보낸다.
        if flow.request.pretty_host == GEMINI_PROMPT_HOST and flow.response is not None:
            flow.response.stream = True

    # ── 다른 사이트의 프롬프트 전송 (form / JSON) ────────────────────────────

    async def _form_prompt(self, flow: http.HTTPFlow, host: str, field: str) -> None:
        from urllib.parse import parse_qsl, urlencode

        pairs = parse_qsl(flow.request.get_text() or "", keep_blank_values=True)
        idx = [i for i, (k, _) in enumerate(pairs) if k == field]
        if not idx:
            logger.warning("[proxy] 프롬프트 칸(%s)이 없는 전송 — 차단 %s", field, host)
            self._block(flow, "메시지 형식을 해석하지 못해 전송을 막았습니다")
            return
        replaced = await self._mask_prompt_texts(flow, host, [pairs[i][1] for i in idx])
        if replaced is None:
            self._block(flow, _CANCELLED)
            return
        for i in idx:
            pairs[i] = (field, replaced.get(pairs[i][1], pairs[i][1]))
        flow.request.text = urlencode(pairs)
        if self._raw_left(flow, flow.request.content):
            self._block(flow, _RAW_LEFT)

    async def _json_prompt(self, flow: http.HTTPFlow, host: str, find_slots) -> None:
        try:
            body = json.loads(flow.request.get_text() or "")
        except json.JSONDecodeError:
            body = None
        # ④ _masked.md 로 바꿔 올린 그림을 메시지에서 그림 포인터가 아니라 첨부 문서로 가리키게 한다.
        converted = (host in CHATGPT_HOSTS and isinstance(body, dict) and bool(self._chatgpt_files)
                     and _chatgpt_images_to_files(body, self._chatgpt_files))
        slots = find_slots(body) if isinstance(body, dict) else []
        if slots is None:
            if converted:
                flow.request.text = _dumps(body)
            return  # 새 글이 없는 전송(다시 생성 등)
        if not await self._mask_slots(flow, host, slots):
            self._block(flow, "메시지 형식을 해석하지 못했거나 검토 결과 전송하지 않기로 했습니다")
            return
        flow.request.text = _dumps(body)
        if self._raw_left(flow, flow.request.content):
            self._block(flow, _RAW_LEFT)

    async def websocket_message(self, flow: http.HTTPFlow) -> None:
        """웹소켓으로 보내는 프롬프트(Grok). 막을 때는 그 메시지만 버린다."""
        msg = flow.websocket.messages[-1]
        if not msg.from_client or not msg.is_text:
            return
        key = flow.request.path.split("?")[0]
        host = flow.request.pretty_host
        find_slots = next((f for (h, prefix), f in PROMPT_WS_ENDPOINTS.items()
                           if h == host and key.startswith(prefix)), None)
        if find_slots is None:
            return
        try:
            try:
                obj = json.loads(msg.text)
            except json.JSONDecodeError:
                return  # 핑처럼 JSON 이 아닌 메시지는 그대로
            slots = find_slots(obj)
            if slots is None:
                return
            if not await self._mask_slots(flow, host, slots):
                raise ValueError("글 칸이 없거나 검토 결과 전송하지 않기로 했다")
            msg.text = _dumps(obj)
            if self._raw_left(flow, msg.content):
                raise ValueError("가린 뒤에도 원문 값이 남았다")
        except Exception as exc:
            # 통과시키면 원본이 나간다. 그 메시지만 버린다(사이트는 답을 못 받고 멈춘다).
            logger.warning("[proxy] 웹소켓 프롬프트 차단 %s%s (%s)", host, key, exc)
            msg.drop()
            reason = flow.metadata.pop("campfire_block_reason", None)  # 첨부 인젝션 등 — 사이트는 이유를 못 보여 준다
            if reason:
                broker.notify(_site(host), reason)

    async def _mask_slots(self, flow, host, slots) -> bool:
        """(dict/list, 키) 칸의 글을 검사해 그 자리에 마스킹본을 넣는다. 보내도 되면 True.

        칸이 비었으면(전송인데 글 칸을 못 찾음) False — 모르는 모양은 보내지 않는다.
        """
        if not slots:
            logger.warning("[proxy] 프롬프트 칸이 없는 전송 — 차단 %s", host)
            return False
        replaced = await self._mask_prompt_texts(flow, host, [c[k] for c, k in slots])
        if replaced is None:
            return False
        for container, k in slots:
            container[k] = replaced.get(container[k], container[k])
        return True

    async def _mask_prompt_texts(self, flow, host, texts) -> dict[str, str] | None:
        """프롬프트 글들을 검사해 {원문: 마스킹본} 을 돌려준다. 바꿀 게 없으면 {}.

        None 이면 보내지 않는다(사람이 취소했거나 정책 차단). 같은 글이 여러 칸에 있으면
        (Perplexity query_str·dsl_query) 한 번만 검사해 판단도 한 번만 받는다.
        """
        distinct = [t for t in dict.fromkeys(texts) if t.strip()]
        if not distinct:
            return {}
        slots = [_TextSlot(t) for t in distinct]
        masked = await self._scan_texts_and_decide(slots, host=host, prompt_fields=slots, flow=flow)
        if masked is None:
            return None
        if masked is _ORIGINAL:
            return {}
        return {slot.text: new.decode("utf-8") for slot, new in masked}

    # ── Gemini 경로 (세션 시작 → 본문) ──────────────────────────────────────

    async def _gemini_upload(self, flow: http.HTTPFlow) -> None:
        cmd = flow.request.headers.get(_GOOG_CMD, "").lower()

        if "start" in cmd:
            # 크기를 미리 선언하는 자리. ChatGPT 의 file_size 와 같은 역할이라
            # 여기서 N' 으로 올려 두고 나중에 본문을 그 크기에 맞춘다.
            declared = int(flow.request.headers.get(_GOOG_LEN) or 0)
            if declared <= 0:
                # 우리가 아는 모양이 아니다. 통과시키면 검사 없이 나간다.
                logger.warning("[proxy] Gemini 시작 요청에 길이 선언이 없다 — 차단")
                self._block(flow, "업로드 형식을 해석하지 못해 전송을 막았습니다")
                return
            # 본문은 "File name: 보고서.docx" 하나다(2026-10-29 맥 캡처). 이름을 여기서 _masked.md 로 바꾸고, 뒤따르는
            # ProcessFile(③)·전송(④)의 형식·이름도 같은 이름으로 맞춘다(_gemini_files). 서버는 ②에서 형식을 보지 않는다.
            # 헤더의 charset 에 기대지 않는다 — 없으면 mitmproxy 가 latin-1 로 풀어 한글 이름이 깨진다.
            body = (flow.request.content or b"").decode("utf-8", "replace")
            if not body.startswith(_GEMINI_NAME_PREFIX):
                logger.warning("[proxy] Gemini 시작 요청 본문이 모르는 모양이다 — 차단")
                self._block(flow, "업로드 형식을 해석하지 못해 전송을 막았습니다")
                return
            original = body[len(_GEMINI_NAME_PREFIX):]
            nprime = size_ceiling(declared)
            flow.request.headers[_GOOG_LEN] = str(nprime)
            # 확장이 검토한 마스킹본(이름·크기로 알린 것)은 이름을 그대로 둔다 — 다시 바꾸면 x_masked_masked.md 가 된다.
            masked_name = original if _approved_meta(original, declared) else masked_name_for(original)
            if masked_name != original:
                flow.request.content = (_GEMINI_NAME_PREFIX + masked_name).encode("utf-8")
            flow.metadata["campfire_gemini"] = ChatGptUpload(
                declared=nprime, masked_name=masked_name, original_name=original
            )
            return

        if "upload" not in cmd:
            return  # 취소·조회 같은 부수 명령. 바이트가 없으므로 건드리지 않는다.

        promise = self._gemini.get(gemini_session_key(flow.request.url))
        if promise is None:
            # 시작 요청을 못 봤다 = 어떤 크기를 약속했는지 모른다. 보내면 안 된다.
            logger.warning("[proxy] Gemini 업로드의 약속값을 찾지 못했다 — 차단")
            self._block(flow, "업로드 세션을 확인하지 못해 전송을 막았습니다")
            return
        if promise.blocked:
            self._block(flow, _CANCELLED)
            return
        if promise.settled is not None:
            flow.request.content = promise.settled
            return

        masked = await self._scan_and_decide(
            data=flow.request.content or b"",
            file_name=promise.original_name,
            mime=flow.request.headers.get("content-type", ""),
            host="gemini.google.com",
            flow=flow,
            # 시작 요청에서 이미 _masked.md 로 바꿔 등록했다 — 깨끗해도 원본 바이트가 아니라 글을 올린다.
            # 확장이 검토한 마스킹본(이름을 안 바꿈)은 해시로 그대로 보낸다(_scan_and_decide 맨 앞).
            always_text=promise.masked_name != promise.original_name,
        )
        if masked is _UNREPLACEABLE:
            promise.blocked = True
            self._block(flow, "검사를 끝내지 못한 파일이라 보내지 않았습니다 — 지원하지 않는 형식이거나 이미지 검사가 꺼져 있습니다")
            return
        if masked is None:
            promise.blocked = True
            self._block(flow, _CANCELLED)
            return
        if masked is _ORIGINAL:
            masked = flow.request.content or b""
        if len(masked) > promise.declared:
            promise.blocked = True
            self._block(flow, "마스킹 결과가 예상보다 커서 전송을 막았습니다")
            return
        promise.settled = masked.ljust(promise.declared, b" ")
        flow.request.content = promise.settled

    def _gemini_start_response(self, flow: http.HTTPFlow) -> None:
        """시작 요청의 응답이 알려 주는 업로드 URL 을 약속값에 묶는다.

        두 요청이 같은 경로로 오기 때문에 이 URL 이 유일한 구분자다.
        """
        pending = flow.metadata.get("campfire_gemini")
        if pending is None or flow.response is None:
            return
        url = flow.response.headers.get(_GOOG_URL)
        if not url:
            return
        self._gemini[gemini_session_key(url)] = pending
        while len(self._gemini) > _MAX_TRACKED_UPLOADS:
            self._gemini.pop(next(iter(self._gemini)))

    def _gemini_finalize_response(self, flow: http.HTTPFlow) -> None:
        """② 응답 본문(파일 경로)을 바꾼 이름에 묶는다 — ③·④ 가 이 경로로 파일을 가리킨다."""
        if flow.response is None or "finalize" not in flow.request.headers.get(_GOOG_CMD, "").lower():
            return
        promise = self._gemini.get(gemini_session_key(flow.request.url))
        path = (flow.response.get_text() or "").strip()
        if promise is None or promise.settled is None or not path or promise.masked_name == promise.original_name:
            return
        self._gemini_files[path] = promise.masked_name
        while len(self._gemini_files) > _MAX_TRACKED_UPLOADS:
            self._gemini_files.pop(next(iter(self._gemini_files)))
        # 전송 세션 설정(ofs=0)의 첨부 항목이 이 경로로 파일을 가리킨다 — 그 세션에 실린 첨부만 판정한다(_judge_attached).
        pending = flow.metadata.get("campfire_pending")
        if pending is not None:
            pending.ids = (path,)

    def _gemini_process_file(self, flow: http.HTTPFlow) -> None:
        """③ ProcessFile 의 f.req 에서 우리가 바꾼 파일의 형식·이름을 맞춘다.

        모르는 모양이면 손대지 않는다 — 바이트는 ②에서 이미 가린 글이라 내용이 새지 않는다(형식이 어긋나 Gemini 가
        파일을 못 읽을 뿐이다)."""
        from urllib.parse import parse_qsl, urlencode

        if not self._gemini_files:
            return
        pairs = parse_qsl(flow.request.get_text() or "", keep_blank_values=True)
        changed = False
        for idx, (k, v) in enumerate(pairs):
            if k != "f.req":
                continue
            try:
                outer = json.loads(v)
                inner = json.loads(outer[1])
            except (ValueError, TypeError, IndexError, KeyError):
                return
            if _rename_gemini_file(inner, self._gemini_files):
                outer[1] = _dumps(inner)
                pairs[idx] = (k, _dumps(outer))
                changed = True
        if changed:
            flow.request.text = urlencode(pairs)

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
                data=part.body, file_name=name, mime=part.content_type, host=host, flow=flow
            )
            if masked is None:
                self._block(flow, _CANCELLED)
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
        original_name = str(body.get("file_name") or "upload.bin")
        if declared > 0 and _approved_meta(original_name, declared):
            # 확장이 검토한 마스킹본 — 이름·크기를 고치지 않는다. 뒤따르는 PUT 은 해시로 그대로 보낸다.
            flow.metadata["campfire_upload"] = ChatGptUpload(
                declared=None, masked_name=original_name, original_name=original_name
            )
            return
        masked_name = masked_name_for(original_name)
        nprime: int | None = None
        # 그림이면 그림 처리(use_case multimodal)로 등록돼 MD 를 넣으면 invalid_image_format 이 된다(2026-10-26 실측).
        # 사용자 결정 A: 그림은 늘 OCR 글(.md)로 보낸다 — 문서와 같이 my_files 로 등록하고 ③④ 도 맞춘다.
        as_text = declared > 0 and (
            body.get("use_case") == "multimodal" or str(body.get("mime_type") or "").startswith("image/")
            or bool(_IMAGE_NAME.search(original_name))
        )

        if declared > 0:
            # (a) 크기를 선언하는 흐름. 선언값과 실제 크기가 다르면 거부당하므로
            # 넉넉히 올려 두고 나중에 그 크기에 맞춘다.
            nprime = size_ceiling(declared)
            body["file_size"] = nprime
            # 이름과 타입도 같이 바꾼다. `.docx` 로 등록해 놓고 MD 바이트를 올리면
            # 받는 쪽이 DOCX 로 열려다 처리 단계에서 실패한다.
            body["file_name"] = masked_name
            body["mime_type"] = MASKED_MIME
            if "client_resolved_mime_type" in body:
                body["client_resolved_mime_type"] = MASKED_MIME
            if as_text and "use_case" in body:
                body["use_case"] = "my_files"
            flow.request.set_text(json.dumps(body))
        # (b) 크기 선언이 없는 흐름이면 등록 요청은 건드릴 게 없다. 그래도
        # **무장은 해야 한다** — 무장하지 않으면 뒤따르는 PUT 이 차단된다.
        # 응답에서 upload_url 을 받을 때 이어 붙이려고 잠시 들고 있는다.
        flow.metadata["campfire_upload"] = ChatGptUpload(
            declared=nprime, masked_name=masked_name, original_name=original_name, as_text=as_text
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
        if isinstance(data.get("file_id"), str):
            pending.file_id = data["file_id"]
        if pending.as_text and isinstance(data.get("file_id"), str):
            self._chatgpt_files[data["file_id"]] = pending.masked_name
            while len(self._chatgpt_files) > _MAX_TRACKED_UPLOADS:
                self._chatgpt_files.pop(next(iter(self._chatgpt_files)))
        # PUT 은 chatgpt.com 이 아니라 oaiusercontent.com 으로 간다. 경로만 키로 쓴다.
        path = url.split("?", 1)[0]
        idx = path.find("/", path.find("://") + 3)
        self._chatgpt[path[idx:] if idx > 0 else path] = pending
        # 재시도를 위해 남겨 두는 항목이라 스스로는 안 줄어든다. 오래된 것부터 버린다.
        while len(self._chatgpt) > _MAX_TRACKED_UPLOADS:
            self._chatgpt.pop(next(iter(self._chatgpt)))

    def _chatgpt_process(self, flow: http.HTTPFlow) -> None:
        """③ process_upload_stream — _masked.md 로 바꿔 등록한 그림이면 이름·형식·use_case 를 등록과 맞춘다.

        그대로 두면 ChatGPT 가 그림 처리(multimodal)로 MD 를 읽으려다 실패한다. 모르는 모양이면 손대지 않는다 —
        바이트는 ②에서 이미 가린 글이다."""
        try:
            body = json.loads(flow.request.get_text() or "")
        except json.JSONDecodeError:
            return
        name = self._chatgpt_files.get(body.get("file_id")) if isinstance(body, dict) else None
        if name is None:
            return
        body["file_name"] = name
        body["mime_type"] = MASKED_MIME
        if body.get("use_case") == "multimodal":
            body["use_case"] = "my_files"
        flow.request.text = _dumps(body)

    async def _chatgpt_put(self, flow: http.HTTPFlow, key: str) -> None:
        # pop 하지 않는다 — 재시도된 PUT 이 분기를 못 타고 원본으로 나가는 것을 막는다.
        promise = self._chatgpt[key]
        if promise.blocked:
            self._block(flow, _CANCELLED)
            return
        if promise.settled is not None:
            # 이미 판단이 끝난 업로드의 재시도. 사람에게 다시 묻지 않는다.
            flow.request.content = promise.settled
            return

        data = flow.request.content or b""
        mime = flow.request.headers.get("content-type", "")
        if promise.file_id:
            flow.metadata["campfire_file_ids"] = (promise.file_id,)  # 전송 메시지 attachments[].id 와 같다(맥 캡처)
        masked = await self._scan_and_decide(
            data=data,
            file_name=promise.original_name,
            mime=mime,
            host="chatgpt.com",
            flow=flow,
            # 그림 처리로 등록된 이미지는 본문을 MD 로 바꾸면 invalid_image_format 으로 거부된다(2026-10-26 실측).
            # 등록부터 _masked.md 로 바꾼 그림(as_text)은 늘 OCR 글로 보낸다. 문서는 지금처럼 바꿔 넣는다.
            can_replace=promise.as_text or not (is_image(data) or mime.lower().startswith("image/")),
            always_text=promise.as_text,
        )
        if masked is _UNREPLACEABLE:
            promise.blocked = True
            self._block(flow, "검사를 끝내지 못한 그림이라 보내지 않았습니다 — 이미지 검사가 꺼져 있거나 글을 읽지 못했습니다"
                        if promise.as_text else
                        "ChatGPT 는 이미지 자리에 가린 글을 받지 않아 보내지 않았습니다 — 가릴 내용이 있는 이미지입니다")
            return
        if masked is None:
            promise.blocked = True
            self._block(flow, _CANCELLED)
            return
        if masked is _ORIGINAL:
            # 원본을 보내기로 했는데 등록은 이미 N' 으로 고쳐 버렸다. 원본 크기와
            # 다르므로 그대로 두면 사이트가 거부한다 — 원본도 같은 규칙으로 채운다.
            masked = flow.request.content or b""
        if promise.declared is None:
            # 크기를 약속한 적이 없다. 패딩할 이유가 없고, 안 하는 편이 낫다 —
            # 패딩은 업로드를 최대 4배로 부풀린다.
            promise.settled = masked
        else:
            if len(masked) > promise.declared:
                # N' 이 모자랐다. 잘라서 보내면 사용자가 모르는 채로 내용이
                # 사라지므로 차단한다 — 조용한 손실보다 실패가 낫다.
                logger.warning(
                    "[proxy] 마스킹 결과가 선언값을 넘었다 %d > %d",
                    len(masked), promise.declared,
                )
                promise.blocked = True
                self._block(flow, "마스킹 결과가 예상보다 커서 전송을 막았습니다")
                return
            # 선언값에 정확히 맞춘다. 공백 패딩은 텍스트에서 안전하다(실측 확인).
            promise.settled = masked.ljust(promise.declared, b" ")
        flow.request.content = promise.settled
        # 본문을 바꿨으면 타입도 바꿔야 한다.
        #
        # 마스킹 결과는 원본 포맷이 아니라 MD 텍스트다. PDF 를 올릴 때 PUT 은
        # Content-Type: application/pdf 로 오는데(실측), 그대로 두면 받는 쪽은
        # 마크다운 바이트를 PDF 로 열려고 한다. 업로드 자체는 통과하더라도
        # 처리 단계에서 깨진다.
        flow.request.headers["content-type"] = MASKED_MIME

    # ── 텍스트 여러 필드 검사 + 한 번의 판단 (Claude 메시지 RPC) ──────────────

    async def _scan_texts_and_decide(self, fields, *, host, prompt_fields=(), flow=None):
        """텍스트 필드 여러 개를 검사하고 **판단은 한 번만** 받는다.

        메시지 하나에 프롬프트(2.3)와 인라인 파일(2.15.4)이 같이 있어도 검토 요청은
        하나여야 한다 — 필드마다 따로 물으면 사람이 같은 전송을 여러 번 승인한다.
        prompt_fields: 사용자가 직접 친 프롬프트 필드. 문서 기준("AI 에게 내리는 지시 =
        인젝션")으로 보면 평범한 요청이 인젝션으로 잡히므로 /jobs/prompt 와 같이 인젝션 검사를
        하지 않는다(PII·자격증명·강제 마스킹만). #247 에서 확장 경로만 바뀌어, 프록시에서는
        "첨부한 파일 내용을 그대로 옮겨 적어줘" 가 [인젝션 마스킹] 으로 나갔다(2026-10-26 실측).
        인라인 파일 내용은 문서이므로 그대로 문서 기준(인젝션 검사)이되, 이 메시지의 프롬프트를 사용자 요청으로 삼는다.
        이 사이트에 올려 두고 아직 안 보낸 첨부가 있으면 먼저 이 프롬프트로 인젝션을 판정한다(인젝션 2단계).
        반환: [(field, 마스킹 bytes)] / `_ORIGINAL` / None(전송 안 함, 이유는 flow.metadata 에).
        """
        prompt_text = "\n".join(p.value.decode("utf-8", "replace") for p in prompt_fields).strip()
        if prompt_text:
            reason = await self._judge_attached(host, prompt_text, flow)
            if reason:
                if flow is not None:
                    flow.metadata["campfire_block_reason"] = reason
                return None
        # 확장처럼 프롬프트를 먼저 검사한다 — 메시지 안 필드 순서와 상관없이. 대시보드·검토 화면에도
        # 프롬프트가 먼저 나온다. (첨부할 때 따로 올라가는 파일은 전송보다 먼저 와서 순서를 바꿀 수 없다)
        fields = sorted(fields, key=lambda f: not any(f is p for p in prompt_fields))
        if flow is not None:
            flow.metadata.pop("campfire_raw_values", None)
        per_field = []          # (field, masked_text)
        all_pii, all_inj, all_forced = [], [], []
        raws: dict[str, str] = {}  # 찾은 원문 값 → 자리표시자
        orig_parts, masked_parts = [], []
        scan_statuses, reasons = [], []
        truncated = False
        for fld in fields:
            text = fld.value.decode("utf-8", "replace")
            is_prompt = any(fld is p for p in prompt_fields)
            result = await _run_pipeline(text=text, direct_prompt=is_prompt, scan_injection=not is_prompt,
                                         user_prompt=None if is_prompt else (prompt_text or None),
                                         record_name="prompt.txt" if is_prompt else "message.txt")
            if result.get("blocked"):
                logger.info("[proxy] 정책 차단 (Claude 메시지)")
                return None
            masked_text = result.get("maskedText", text)
            per_field.append((fld, masked_text))
            all_pii += result.get("piiItems", [])
            all_inj += result.get("injectionItems", [])
            all_forced += result.get("forcedMaskItems", [])
            raws.update(_raw_values(result, text))
            orig_parts.append(text)
            scan_statuses.append(result.get("scanStatus"))
            if result.get("reason"):
                reasons.append(str(result["reason"]))
            truncated = truncated or bool(result.get("truncated"))

        # 찾은 값이 같은 글(또는 다른 칸)에 한 번 더 나오는데 탐지가 놓친 자리도 가린다 — Perplexity 는 이메일을
        # "[x](mailto:x)" 로 싣는데 괄호 안 두 번째 이메일을 놓치면 원문이 남는다(WPX-1). 긴 값부터 바꾼다.
        order = sorted(raws, key=len, reverse=True)
        for i, (fld, mt) in enumerate(per_field):
            for r in order:
                mt = mt.replace(r, raws[r])
            per_field[i] = (fld, mt)
        masked_parts = [mt for _, mt in per_field]

        scan_status = "ok" if scan_statuses and all(s == "ok" for s in scan_statuses) else (
            next((s for s in scan_statuses if s and s != "ok"), "failed")
        )
        combined = {
            "originalText": "\n---\n".join(orig_parts),
            "maskedText": "\n---\n".join(masked_parts),
            "piiItems": all_pii,
            "injectionItems": all_inj,
            "forcedMaskItems": all_forced,
            "scanStatus": scan_status,
            "reason": " / ".join(dict.fromkeys(reasons)) or None,
            "truncated": truncated,
            "stats": {
                "piiCount": len(all_pii),
                "injectionCount": len(all_inj),
                "forcedMaskCount": len(all_forced),
            },
        }
        if _is_clean_result(combined):
            return _ORIGINAL
        action = await broker.wait(
            file_name="메시지", host=host, result=combined,
            timeout_s=config.PROXY_DECISION_TIMEOUT_S, is_alive=_client_alive(flow),
        )
        if action == "cancel":
            return None
        if action == "send_original" and not all_forced:
            return _ORIGINAL
        if flow is not None:
            flow.metadata["campfire_raw_values"] = order  # 다시 싼 본문에 남았는지 _raw_left 가 본다
        return [(fld, mt.encode("utf-8")) for fld, mt in per_field]

    async def _judge_attached(self, host: str, prompt: str, flow) -> str | None:
        """이 사이트에 올려 두고 아직 안 보낸 첨부를 이 프롬프트와 함께 인젝션 판정한다. 막아야 하면 이유를 돌려준다.

        참조(ids)가 있는 첨부는 전송에 그 참조가 실렸을 때만 판정한다 — ChatGPT 는 전송 본문의 file_id, Gemini 는 그 세션
        설정의 첨부 항목에 실린 파일 경로(campfire_attach_refs). 예전엔 Gemini 도 "다음 전송에 붙은 것" 으로 봐, 앞선 시험에서
        올렸던(또는 뺀) 첨부까지 판정해 막았고, 막힌 첨부가 남아 첨부 없는 새 대화도 계속 막혔다(맥 v0.2.59 R-2).
        참조를 모르는 사이트(Claude·Grok·Perplexity)는 올린 지 _GUESS_WINDOW_S 안의 첨부를 "이 사이트의 다음 전송" 에 붙은
        것으로 본다(그보다 오래된 것은 뺀다 — 올렸다 지운 첨부일 수 있다).
        인젝션이거나 검사를 끝내지 못했으면: 참조로 이은 첨부는 남겨 둔다(빼지 않고 다시 보내면 또 막는다). 추정으로 이은
        첨부는 한 번 막고 대기 목록에서 뺀다 — 남겨 두면 그 사이트의 이후 전송이 첨부 없는 새 대화까지 계속 막힌다(Gemini
        R-2 와 같은 모양). 대신 이유에 "그 파일을 지우고 다시 보내 주세요" 를 싣는다.
        """
        site = _site(host)
        now = time.monotonic()
        files = [p for p in self._pending.get(site, [])
                 if now - p.at < (_PENDING_TTL_S if p.ids else _GUESS_WINDOW_S)]
        body = b""
        if flow is not None:
            body = (flow.request.content or b"") + flow.metadata.get("campfire_attach_refs", b"")
        attached = [p for p in files if not p.ids or any(i.encode() in body for i in p.ids)]
        self._pending[site] = files
        if not attached:
            return None

        from app.core.pipeline.orchestrator import run_pipeline

        hits, failed = [], []
        for p in attached:
            try:
                r = await run_pipeline(text=p.text, user_prompt=prompt, scan_injection=True, wrap_file=False)
            except Exception:  # noqa: BLE001 - 판정 실패는 아래에서 막는 쪽으로 다룬다
                logger.exception("[proxy] 첨부 인젝션 판정 실패")
                r = {}
            # 다음 시험에서 원인을 바로 가리게 판정 근거를 남긴다(맥 v0.2.59 R-3: 이유·점수가 안 찍혀 오탐인지 몰랐다).
            # 글은 남기지 않는다 — 구간은 (종류, 시작, 끝, 점수, 출처) 와 첨부 글자 수만. 이름은 등록어가 들면 document_masked.md.
            spans = [(i.get("type"), i.get("start"), i.get("end"), round(float(i.get("confidence") or 0), 3), i.get("source"))
                     for i in (r.get("injectionItems") or [])]
            logger.info("[proxy] 첨부 판정 host=%s 첨부=%s 상태=%s 인젝션=%d 첨부글자=%d 프롬프트글자=%d 구간=%s",
                        host, p.name, r.get("scanStatus"), len(spans), len(p.text), len(prompt), spans[:10])
            if r.get("scanStatus") != "ok":
                failed.append(p.name)
            elif r.get("injectionItems"):
                hits.append(p.name)
        if hits or failed:
            guessed = [p for p in attached if not p.ids]
            self._pending[site] = [p for p in files if p not in guessed]  # 추정 연결은 한 번만 막는다
            tail = "그 파일을 지우고 다시 보내 주세요" if guessed else "그 첨부를 빼고 다시 보내 주세요"
        if hits:
            logger.info("[proxy] 첨부에 인젝션 — 전송 차단 host=%s 첨부 %d개", host, len(hits))
            return f"첨부한 파일({', '.join(hits)})에 AI 를 조종하려는 문구(프롬프트 인젝션)가 있어 전송을 막았습니다 — {tail}"
        if failed:
            return (f"첨부한 파일({', '.join(failed)})의 인젝션 검사를 끝내지 못해 전송을 막았습니다 — "
                    + (tail if guessed else "잠시 뒤 다시 보내 주세요"))
        self._pending[site] = [p for p in files if p not in attached]
        return None

    # ── 공통: 검사 + 사람 판단 ───────────────────────────────────────────────

    async def _scan_and_decide(self, **kw) -> bytes | None | object:
        """업로드를 검사하고 판단을 받는다(_scan_upload). 보내기로 했으면 그 첨부를 이 사이트의 "아직 안 보낸 첨부" 로
        기억해 전송 때 프롬프트와 함께 인젝션을 판정한다(_judge_attached)."""
        out = await self._scan_upload(**kw)
        flow = kw.get("flow")
        scanned = flow.metadata.get("campfire_scan") if flow is not None else None
        if scanned and out is not None and out is not _UNREPLACEABLE:
            site = _site(kw["host"])
            now = time.monotonic()
            keep = [p for p in self._pending.get(site, []) if now - p.at < _PENDING_TTL_S]
            pending = PendingFile(scanned[0], scanned[1], tuple(flow.metadata.get("campfire_file_ids", ())), now)
            keep.append(pending)
            self._pending[site] = keep[-_MAX_TRACKED_UPLOADS:]
            flow.metadata["campfire_pending"] = pending  # 응답에서 첨부 참조를 알게 되면 채운다(Gemini 파일 경로)
        return out

    async def _scan_upload(
        self, *, data: bytes, file_name: str, mime: str, host: str, flow=None, can_replace: bool = True,
        always_text: bool = False,
    ) -> bytes | None | object:
        """검사하고 판단을 받는다.

        can_replace: 이 업로드 자리에 가린 글(MD)을 넣을 수 있는가. 아니면(ChatGPT 이미지) 끝까지
        검사해 가릴 것이 있을 때 묻지 않고 `_UNREPLACEABLE` 을 돌려준다 — 가린 글을 넣으면 사이트가 깨진 파일로
        거부한다(2026-10-26 실측). 그래서 이 경우는 오탐이어도 원본을 보내는 선택지가 없다. 미지원·잘림처럼
        검사를 끝내지 못한 건은 지금처럼 묻는다(원본/취소).
        always_text: 등록 때 이미 이름·형식을 `_masked.md` 로 바꿔 둔 자리(Gemini) — 원본 바이트는 보낼 수 없다.
        깨끗해도 추출한 글을 돌려주고, 검사를 끝내지 못했으면(미지원·이미지 검사 꺼짐 등) `_UNREPLACEABLE`,
        "원본 보내기" 를 골라도 가린 글을 보낸다.
        반환: 마스킹 바이트 / `_ORIGINAL`(원본 그대로) / `_UNREPLACEABLE` / None(보내지 않음).
        """
        if _approved_bytes(data):
            logger.info("[proxy] 확장이 검토한 업로드 — 다시 검사하지 않음 host=%s", host)
            return _ORIGINAL
        # 이름이나 타입을 못 받았으면 바이트를 보고 채운다.
        #
        # ChatGPT(upload_reservations)도 Gemini 도 등록에 파일 이름을 안 싣는다.
        # Gemini 는 한술 더 떠 Content-Type 을 x-www-form-urlencoded 라고 준다 —
        # 그대로 넘기면 PDF·DOCX 가 "미지원" 으로 떨어져 **검사되지 않는다.**
        file_name, mime = refine(data, file_name, mime)

        # 업로드 때는 사용자 프롬프트가 없다 — 인젝션은 전송 때 프롬프트와 함께 판정한다(_judge_attached).
        result = await _run_pipeline(
            file_bytes=data, mime_type=mime, file_name=file_name, wrap_file=False, record_name=file_name,
            scan_injection=False,
        )
        if result.get("blocked"):
            logger.info("[proxy] 정책 차단 host=%s", host)  # 파일명엔 등록어가 있을 수 있다
            return None
        if flow is not None and result.get("scanStatus") == "ok":
            # 전송 때 판정에는 원문을 쓴다 — 확장 경로(/jobs userPrompt)와 같은 기준이 되게(로컬 1차 판정은 원문, 외부
            # Solar 에는 파이프라인이 PII 를 가려 보낸다). 가린 글로 판정하면 [이름 마스킹] 같은 토큰 때문에 결과가 갈렸다.
            # 원문은 메모리에만 30분(_PENDING_TTL_S) — job_registry 가 /jobs 결과를 메모리에 두는 것과 같은 범위다.
            flow.metadata["campfire_scan"] = (masked_name_for(file_name), result.get("originalText") or "")

        # 이름에 등록어가 있으면 내용이 깨끗해도 원본(=원래 이름)을 보내면 안 된다.
        name_forced = bool(forced_mask.registry.snapshot().find(file_name))
        # 이미지 속 글자 검사가 꺼져 문서 속 그림을 검사하지 못했으면 원본(그림째)을 보내지 않고 글만 보낸다(사용자 결정 2026-10-28).
        images_removed = bool(result.get("imagesRemoved"))
        if always_text:
            if result.get("scanStatus") != "ok":
                logger.info("[proxy] 검사를 끝내지 못한 업로드 — 원본을 보낼 수 없어 차단 host=%s", host)
                return _UNREPLACEABLE
            if _is_clean_result(result) and not name_forced:
                return (result.get("maskedText") or "").encode("utf-8")
        if _is_clean_result(result) and not name_forced:
            if not images_removed:
                return _ORIGINAL
            if can_replace:
                return (result.get("maskedText") or "").encode("utf-8")
        if not can_replace and _scan_complete(result):
            logger.info("[proxy] 가린 글로 바꿔 넣을 수 없는 업로드 — 차단 host=%s", host)
            return _UNREPLACEABLE

        action = await broker.wait(
            file_name=file_name,
            host=host,
            result=result,
            timeout_s=config.PROXY_DECISION_TIMEOUT_S,
            is_alive=_client_alive(flow),
        )
        if action == "cancel":
            return None
        if (action == "send_original" and not result.get("forcedMaskItems") and not name_forced and not images_removed
                and not always_text):
            return _ORIGINAL
        return (result.get("maskedText") or "").encode("utf-8")

    # ── 차단 ─────────────────────────────────────────────────────────────────

    def _block(self, flow: http.HTTPFlow, reason: str, *, notify: bool = True) -> None:
        """요청을 사이트에 보내지 않고 여기서 끝낸다.

        상태 코드는 403 이다. 사이트 JS 가 이걸 업로드 실패로 보여 주는 게, 조용히
        성공한 척하는 것보다 낫다. 검사 단계가 더 구체적인 이유를 남겼으면(첨부 인젝션 등) 그걸 쓴다.
        사이트는 이유를 보여 주지 않으므로 데스크톱 알림으로 알린다(broker.notify) — 사람이 확인창에서 직접
        취소한 건은 알리지 않는다.
        """
        specific = flow.metadata.get("campfire_block_reason")
        reason = specific or reason
        if notify and (specific or reason != _CANCELLED):
            broker.notify(_site(flow.request.pretty_host), reason)
        flow.response = http.Response.make(
            403,
            json.dumps({"error": "campfire_blocked", "reason": reason}, ensure_ascii=False).encode(),
            {"Content-Type": "application/json; charset=utf-8"},
        )

    def _raw_left(self, flow: http.HTTPFlow, content: bytes, exclude=()) -> bool:
        """가린 뒤 다시 싼 본문에 이번 검사에서 찾은 원문 값이 남았는가 — 다섯 사이트 공통 안전장치(WPX-1).

        검사한 칸 말고 같은 글이 실린 모르는 칸이 있으면 모델은 가린 글을 받아도 서버엔 원문이 저장된다. 남았으면 막을
        이유를 flow.metadata 에 둔다. exclude: 검사하지 않고 그대로 보내는 칸(확장이 승인한 첨부)의 바이트.
        """
        raws = flow.metadata.pop("campfire_raw_values", None)
        if not raws:
            return False
        for b in exclude:
            content = content.replace(b, b"")
        where = _raw_left_where(content, raws)
        if not where:
            return False
        logger.warning("[proxy] 가린 뒤에도 원문 값이 남음 — 차단 host=%s 자리=%s",
                       flow.request.pretty_host, where[:10])  # 값은 남기지 않는다
        flow.metadata["campfire_block_reason"] = _RAW_LEFT
        return True

    def _swallow(self, flow: http.HTTPFlow) -> None:
        """친 글을 싣는 부수 요청(자동완성·분석)을 보내지 않고 빈 성공으로 끝낸다.

        막는 게 아니라 "결과 없음" 으로 보이게 한다 — 사이트가 오류를 띄우거나 재시도하지 않게.
        웹소켓 시작 요청이면 연결이 열리지 않아 자동완성만 꺼진다.
        """
        flow.response = http.Response.make(204)


# "원본을 그대로 보낸다" 를 나타내는 표식. None(보내지 않음)과 구분해야 해서
# bytes 도 None 도 아닌 고유 객체를 쓴다.
_ORIGINAL = object()
# "가릴 것이 있는데 이 업로드 자리에는 가린 글을 넣을 수 없다" — 이유를 붙여 막는다(_scan_and_decide can_replace).
_UNREPLACEABLE = object()
# 사람이 확인창에서 보내지 않기로 한 차단 이유 — 데스크톱 알림을 띄우지 않는다(_block).
_CANCELLED = "검토 결과 전송하지 않기로 했습니다"
# 가린 뒤 다시 싼 본문에 찾은 원문 값이 남았다(_raw_left).
_RAW_LEFT = "가리지 못한 칸이 있어 전송을 막았습니다 — 원문이 그대로 나갈 뻔했습니다"
_B64_TOKEN = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")


def _raw_values(result: dict, text: str) -> dict[str, str]:
    """검사 결과에서 가린 원문 값(PII·자격증명·등록어) → 자리표시자. 2자 이하는 뺀다 — 다른 글 안에도 흔하다."""
    from app.core.masker.masker import placeholder_for

    src = result.get("originalText") or text
    out = {}
    for it in (result.get("piiItems") or []) + (result.get("forcedMaskItems") or []):
        raw = it.get("text")
        if not raw and isinstance(it.get("start"), int) and isinstance(it.get("end"), int):
            raw = src[it["start"]:it["end"]]
        if raw and len(raw.strip()) > 2:
            out[raw] = placeholder_for(str(it.get("type") or ""))
    return out


def _json_strings(obj, path=""):
    """JSON 안의 글을 (자리, 글) 로 모두 낸다."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, (dict, list)):
        for k, v in (obj.items() if isinstance(obj, dict) else enumerate(obj)):
            yield from _json_strings(v, f"{path}.{k}" if path else str(k))


def _raw_left_where(content: bytes, raws) -> list[str]:
    """보내는 본문을 풀 수 있는 대로 풀어 원문 값이 남은 자리 이름을 돌려준다(글은 담지 않는다).

    날것(protobuf 도 글이 바이트 그대로 이어 있다)·URL 풀기(form)·JSON 칸·base64 조각(Gemini 의 protobuf)을 본다.
    """
    from urllib.parse import unquote, unquote_plus

    text = content.decode("utf-8", "replace")
    views = [("본문", text), ("form", unquote_plus(text))]
    try:
        views += list(_json_strings(json.loads(text)))
    except ValueError:
        pass
    for tok in _B64_TOKEN.findall(unquote(text)):
        try:
            views.append(("base64", base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4)).decode("utf-8", "replace")))
        except ValueError:
            continue
    return list(dict.fromkeys(where for where, s in views if any(r in s for r in raws)))


class _TextSlot:
    """_scan_texts_and_decide 가 받는 칸 — protobuf 필드처럼 value(bytes) 를 가진다."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.value = text.encode("utf-8")


def _dumps(obj) -> str:
    """브라우저 JSON.stringify 와 같은 모양(공백 없음, 한글 그대로)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _chatgpt_images_to_files(body: dict, files: dict[str, str]) -> bool:
    """④ f/conversation 에서 _masked.md 로 바꿔 올린 그림(file_id)을 그림 포인터가 아니라 첨부 문서로 가리키게 한다.

    content.parts 의 image_asset_pointer 를 빼고(글만 남으면 content_type 을 "text" 로), metadata.attachments 의 그 항목은
    형식·이름을 바꾸고 그림 크기를 뺀다. 고쳤으면 True."""
    changed = False
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if isinstance(parts, list):
            kept = [p for p in parts if not (
                isinstance(p, dict) and p.get("content_type") == "image_asset_pointer"
                and str(p.get("asset_pointer") or "").rsplit("/", 1)[-1] in files)]
            if len(kept) != len(parts):
                content["parts"] = kept or [""]  # 그림만 보낸 메시지 — 글 없는 첨부 메시지 모양
                if content.get("content_type") == "multimodal_text" and all(isinstance(p, str) for p in content["parts"]):
                    content["content_type"] = "text"
                changed = True
        meta = m.get("metadata")
        for att in (meta.get("attachments") or []) if isinstance(meta, dict) else []:
            if isinstance(att, dict) and att.get("id") in files:
                att["mime_type"] = MASKED_MIME
                att["name"] = files[att["id"]]
                att.pop("width", None)
                att.pop("height", None)
                changed = True
    return changed


def _rename_gemini_file(node, files: dict[str, str]) -> bool:
    """③ ProcessFile f.req 의 [[경로, null, 1, MIME], 이름] 에서 우리가 바꾼 파일의 MIME·이름을 고친다. 고쳤으면 True."""
    if not isinstance(node, list):
        return False
    head = node[0] if node else None
    if isinstance(head, list) and head and isinstance(head[0], str) and head[0] in files:
        if len(head) > 3 and isinstance(head[3], str):
            head[3] = GEMINI_MASKED_MIME
        if len(node) > 1 and isinstance(node[1], str):
            node[1] = files[head[0]]
        return True
    return any([_rename_gemini_file(child, files) for child in node])  # 목록으로 — 하나 고쳐도 나머지도 본다


def _rename_gemini_items(msg, files: dict[str, str]) -> bool:
    """④ processSession 세션 설정의 첨부 항목 {1: {1: 경로, 4: MIME}, 2: 이름} 에서 MIME·이름을 고친다. 고쳤으면 True."""
    changed = False
    items = [f.message for _, f in msg.walk() if f.message is not None]
    for item in items:
        ref = next((f.message for f in item.fields if f.number == 1 and f.message is not None), None)
        path = next((f.value for f in ref.fields if f.number == 1 and f.value is not None), None) if ref else None
        name = files.get(path.decode("utf-8", "replace")) if path else None
        if name is None:
            continue
        for f in ref.fields:
            if f.number == 4 and f.wire_type == 2:
                f.set_bytes(GEMINI_MASKED_MIME.encode())
        for f in item.fields:
            if f.number == 2 and f.wire_type == 2:
                f.set_bytes(name.encode("utf-8"))
        changed = True
    return changed


def _client_alive(flow):
    """판단을 기다리는 동안 브라우저 연결이 살아 있는지 알려 주는 함수. 모르면 None.

    mitmproxy 는 클라이언트 연결이 닫히면 기다리는 훅과 상관없이 client_conn.timestamp_end 를
    채운다. 같은 연결에서 요청만 버린 경우(HTTP/2 스트림 취소)는 여기서 알 수 없다 — 그때는
    앱이 판단 창을 닫는 쪽(desktop/main/proxy-decision-ui.js)이 맡는다.
    """
    conn = getattr(flow, "client_conn", None)
    if conn is None:
        return None
    return lambda: conn.timestamp_end is None


def _scan_complete(result: dict) -> bool:
    """검사를 끝까지 했는가(미지원·잘림·정책 차단이 아님)."""
    return result.get("scanStatus") == "ok" and not result.get("blocked") and not result.get("truncated")


def _is_clean_result(result: dict) -> bool:
    """완전히 검사했고 탐지나 사용자 지정 마스킹이 하나도 없는 결과인가."""
    stats = result.get("stats") or {}
    pii_count = max(int(stats.get("piiCount") or 0), len(result.get("piiItems") or []))
    injection_count = max(
        int(stats.get("injectionCount") or 0), len(result.get("injectionItems") or [])
    )
    forced_count = max(
        int(stats.get("forcedMaskCount") or 0), len(result.get("forcedMaskItems") or [])
    )
    return (
        _scan_complete(result)
        and pii_count == 0
        and injection_count == 0
        and forced_count == 0
    )
