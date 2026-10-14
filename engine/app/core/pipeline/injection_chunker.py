"""Token-aware sliding windows for prompt-injection classification.

The hybrid detector compresses each tool response into three mean-pooled hidden-state
segments. Long character chunks therefore wash out short attacks. This module keeps
PII's existing character chunks unchanged and builds smaller windows with the exact
EXAONE tokenizer used by the injection model.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from app import config
from app.core.detectors.injection import backbone


LOGGER = logging.getLogger(__name__)
_TOKENIZER: Any | None = None
_TOKENIZER_LOCK = threading.Lock()
_SENTENCE_ENDINGS = frozenset(".!?。！？")
_SENTENCE_CLOSERS = frozenset("\"'”’)]}」』〉》")
_SENTENCE_LOOKBACK_TOKENS = 32
_FALLBACK_CHARS = 300
_FALLBACK_OVERLAP_CHARS = 160


class _FileTokenizer:
    """캐시된 tokenizer.json 을 tokenizers 로 직접 읽는다.

    transformers.AutoTokenizer 는 torch 까지 엔진(게이트웨이) 프로세스에 올려 첫 검사에서
    상주 메모리가 수백 MB 늘었다. fast tokenizer 의 offset 은 같은 tokenizers 백엔드에서
    나오므로 결과가 같다. split_token_chunks 가 쓰는 호출 모양만 맞춘다.
    """

    def __init__(self, path: Path) -> None:
        from tokenizers import Tokenizer

        self._tokenizer = Tokenizer.from_file(str(path))

    def __call__(self, text: str, **_kwargs) -> dict[str, Any]:
        encoding = self._tokenizer.encode(text, add_special_tokens=False)
        return {"offset_mapping": encoding.offsets}


def _tokenizer_file() -> Path | None:
    model_id = backbone.model_id()
    if not model_id:
        return None
    if Path(model_id).is_dir():
        candidates = [Path(model_id)]
    else:
        snapshots = (backbone.repo_cache_dir() or Path()) / "snapshots"
        candidates = sorted(p for p in snapshots.iterdir() if p.is_dir()) if snapshots.is_dir() else []
    return next((c / "tokenizer.json" for c in candidates if (c / "tokenizer.json").is_file()), None)


def _get_tokenizer():
    """Load and cache the detector tokenizer without loading a second model."""
    global _TOKENIZER
    with _TOKENIZER_LOCK:
        if _TOKENIZER is not None:
            return _TOKENIZER
        path = _tokenizer_file()
        if path is None:
            raise RuntimeError("인젝션 모델의 tokenizer.json 을 찾지 못했습니다")
        _TOKENIZER = _FileTokenizer(path)
        return _TOKENIZER


def _is_sentence_boundary(
    text: str, offsets: list[tuple[int, int]], token_end: int
) -> bool:
    """Whether the boundary before token_end follows punctuation or a newline."""
    if token_end >= len(offsets):
        return True
    previous_end = offsets[token_end - 1][1]
    next_start = offsets[token_end][0]
    probe = text[max(0, previous_end - 8) : next_start]
    if "\n" in probe or "\r" in probe:
        return True
    stripped = probe.rstrip()
    while stripped and stripped[-1] in _SENTENCE_CLOSERS:
        stripped = stripped[:-1].rstrip()
    return bool(stripped and stripped[-1] in _SENTENCE_ENDINGS)


def _choose_window_end(
    text: str,
    offsets: list[tuple[int, int]],
    token_start: int,
    max_tokens: int,
    overlap_tokens: int,
) -> int:
    nominal_end = min(token_start + max_tokens, len(offsets))
    if nominal_end >= len(offsets):
        return nominal_end
    minimum_size = max(
        overlap_tokens + 1,
        max_tokens - min(_SENTENCE_LOOKBACK_TOKENS, max_tokens // 4),
    )
    minimum_end = min(nominal_end, token_start + minimum_size)
    for candidate in range(nominal_end, minimum_end - 1, -1):
        if _is_sentence_boundary(text, offsets, candidate):
            return candidate
    return nominal_end


def split_token_chunks(
    text: str,
    *,
    max_tokens: int,
    overlap_tokens: int,
    tokenizer=None,
) -> list[dict[str, Any]]:
    """Split text into overlapping model-token windows with original char offsets."""
    if not text:
        return [{"text": "", "offset": 0, "token_count": 0, "chunking": "token"}]
    if max_tokens < 2:
        raise ValueError("max_tokens must be at least 2")
    overlap_tokens = max(0, min(overlap_tokens, max_tokens - 1))
    tokenizer = tokenizer or _get_tokenizer()
    # Fast tokenizers are normally thread-safe, but serializing this short operation also
    # prevents a first load racing with another request in the same engine process.
    with _TOKENIZER_LOCK:
        encoded = tokenizer(
            text,
            add_special_tokens=False,
            return_offsets_mapping=True,
            truncation=False,
        )
    offsets = [
        (int(left), int(right))
        for left, right in encoded["offset_mapping"]
        if int(left) < int(right)
    ]
    if not offsets:
        return [{"text": text, "offset": 0, "token_count": 0, "chunking": "token"}]

    chunks: list[dict[str, Any]] = []
    token_start = 0
    while token_start < len(offsets):
        token_end = _choose_window_end(
            text, offsets, token_start, max_tokens, overlap_tokens
        )
        char_start = 0 if token_start == 0 else offsets[token_start][0]
        char_end = len(text) if token_end >= len(offsets) else offsets[token_end][0]
        chunks.append(
            {
                "text": text[char_start:char_end],
                "offset": char_start,
                "token_count": token_end - token_start,
                "chunking": "token",
            }
        )
        if token_end >= len(offsets):
            break
        next_start = token_end - overlap_tokens
        token_start = max(token_start + 1, next_start)
    return chunks


def _split_character_fallback(text: str) -> list[dict[str, Any]]:
    """Fail-safe used only if the already-installed model tokenizer cannot be opened."""
    if not text:
        return [{"text": "", "offset": 0, "chunking": "character_fallback"}]
    chunks = []
    step = _FALLBACK_CHARS - _FALLBACK_OVERLAP_CHARS
    offset = 0
    while offset < len(text):
        chunks.append(
            {
                "text": text[offset : offset + _FALLBACK_CHARS],
                "offset": offset,
                "chunking": "character_fallback",
            }
        )
        if offset + _FALLBACK_CHARS >= len(text):
            break
        offset += step
    return chunks


def split_injection_chunks(text: str) -> list[dict[str, Any]]:
    try:
        return split_token_chunks(
            text,
            max_tokens=config.INJECTION_CHUNK_TOKENS,
            overlap_tokens=config.INJECTION_CHUNK_OVERLAP_TOKENS,
        )
    except Exception:
        # The detector still gets a short, overlapping input rather than silently falling
        # back to the old 1,000-character windows that caused the missed detections.
        LOGGER.exception(
            "인젝션 tokenizer 청킹 실패; %d자/%d자 겹침으로 대체합니다",
            _FALLBACK_CHARS,
            _FALLBACK_OVERLAP_CHARS,
        )
        return _split_character_fallback(text)
