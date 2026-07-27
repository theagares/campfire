"""
app/core/detectors/base.py
Detector 모듈화 인터페이스 (PLAN §5).

- Detection: 탐지 1건 (type, start, end, text, confidence, source)
- Detector Protocol: name, kind, async detect(text, *, meta) -> list[Detection]
- 탐지 유형 상수: PII / 인젝션

v1 은 rule_based 만 구현하지만, encoder/llm_mcp 교체 시 같은 Detection 을
반환하도록 인터페이스를 고정한다 — 코어·어댑터 무변경으로 detector 교체.
"""

from __future__ import annotations

from typing import Literal, Protocol, TypedDict, runtime_checkable


class ChunkMeta(TypedDict, total=False):
    """detect() 에 넘기는 청크 메타데이터 (PLAN §5)."""

    chunk_index: int
    total_chunks: int
    offset: int  # 원문 내 이 청크의 시작 오프셋
    file_name: str
    # 실제 사용자 프롬프트(있으면). 인젝션 LLM(llm_mcp)이 "이 문서/청크가 사용자의
    # 실제 지시를 무시/변조하려는가"를 판단할 때 placeholder 대신 사용한다.
    # PII detector 는 이 필드를 사용하지 않는다.
    user_prompt: str


class Detection(TypedDict):
    """탐지 1건 (PLAN §5).

    start/end 는 detect() 에 넘어온 text 기준 오프셋. 파이프라인이 청크 offset 을
    더해 원문 기준으로 보정한다.
    """

    type: str          # PERSON_NAME, EMAIL, ... / INSTRUCTION_OVERRIDE, ...
    start: int
    end: int
    text: str
    confidence: float
    source: str        # "rule" | "encoder" | "llm"


@runtime_checkable
class Detector(Protocol):
    """탐지기 인터페이스 (PLAN §5)."""

    name: str
    kind: Literal["pii", "injection"]

    async def detect(self, text: str, *, meta: ChunkMeta) -> list[Detection]:  # pragma: no cover - protocol
        ...


# ── PII 탐지 유형 상수 (PLAN §5) ──────────────────────────────────────────────
PERSON_NAME = "PERSON_NAME"
EMAIL = "EMAIL"
PHONE = "PHONE"
ADDRESS = "ADDRESS"
ID_NUMBER = "ID_NUMBER"
CREDIT_CARD = "CREDIT_CARD"
DATE_OF_BIRTH = "DATE_OF_BIRTH"
ORGANIZATION = "ORGANIZATION"
BANK_ACCOUNT = "BANK_ACCOUNT"
OTHER_PII = "OTHER_PII"

PII_TYPES: frozenset[str] = frozenset(
    {
        PERSON_NAME,
        EMAIL,
        PHONE,
        ADDRESS,
        ID_NUMBER,
        CREDIT_CARD,
        DATE_OF_BIRTH,
        ORGANIZATION,
        BANK_ACCOUNT,
        OTHER_PII,
    }
)

# ── 인젝션 탐지 유형 상수 (PLAN §5) ───────────────────────────────────────────
INSTRUCTION_OVERRIDE = "INSTRUCTION_OVERRIDE"
ROLE_MANIPULATION = "ROLE_MANIPULATION"
SYSTEM_PROMPT_LEAK = "SYSTEM_PROMPT_LEAK"
JAILBREAK = "JAILBREAK"
HIDDEN_COMMAND = "HIDDEN_COMMAND"
DATA_EXFILTRATION = "DATA_EXFILTRATION"
OTHER_INJECTION = "OTHER_INJECTION"

INJECTION_TYPES: frozenset[str] = frozenset(
    {
        INSTRUCTION_OVERRIDE,
        ROLE_MANIPULATION,
        SYSTEM_PROMPT_LEAK,
        JAILBREAK,
        HIDDEN_COMMAND,
        DATA_EXFILTRATION,
        OTHER_INJECTION,
    }
)
