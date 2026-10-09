"""User-defined literal terms that must always be masked.

The rules are kept in memory and replaced atomically by the desktop app.  No
registered term is returned in status, logs, or scan results.  Matching is
Unicode canonical-equivalent (NFC/NFD) and case-insensitive, while reported
offsets always refer to the original input string.
"""

from __future__ import annotations

import re
import secrets
import threading
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

from app import config

MAX_TERMS = 500
MAX_TERM_CHARS = 200
MAX_TOTAL_CHARS = 50_000
ITEM_TYPE = "USER_DEFINED_TERM"


class ForcedMaskRuleError(ValueError):
    """The desktop supplied an invalid rule set."""


def normalize_terms(values: Iterable[str]) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ForcedMaskRuleError("terms must be an array")

    terms: list[str] = []
    seen: set[str] = set()
    total = 0
    for raw in values:
        if not isinstance(raw, str):
            raise ForcedMaskRuleError("each term must be a string")
        term = unicodedata.normalize("NFC", raw.strip())
        if not term:
            continue
        if any(ch in term for ch in ("\r", "\n", "\x00")):
            raise ForcedMaskRuleError("terms cannot contain line breaks or NUL")
        if len(term) > MAX_TERM_CHARS:
            raise ForcedMaskRuleError(f"a term exceeds {MAX_TERM_CHARS} characters")
        key = unicodedata.normalize("NFD", term).lower()
        if key in seen:
            continue
        seen.add(key)
        total += len(term)
        if total > MAX_TOTAL_CHARS:
            raise ForcedMaskRuleError(f"terms exceed {MAX_TOTAL_CHARS} total characters")
        terms.append(term)
        if len(terms) > MAX_TERMS:
            raise ForcedMaskRuleError(f"no more than {MAX_TERMS} terms are allowed")
    return tuple(terms)


def _normalized_with_offsets(text: str) -> tuple[str, list[int]]:
    """Return NFD text plus a normalized-index -> original-index map."""
    parts: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(text):
        normalized = unicodedata.normalize(
            "NFD", unicodedata.normalize("NFD", char).lower()
        )
        parts.append(normalized)
        offsets.extend([index] * len(normalized))
    return "".join(parts), offsets


def _revision() -> str:
    """Opaque change token; never derive status metadata from the secret terms."""
    return secrets.token_hex(8)


@dataclass(frozen=True)
class ForcedMaskSnapshot:
    terms: tuple[str, ...]
    revision: str
    ready: bool
    managed: bool
    pattern: re.Pattern[str] | None

    @property
    def active(self) -> bool:
        return bool(self.terms)

    def find(self, text: str) -> list[dict[str, Any]]:
        if not text or self.pattern is None:
            return []
        normalized, offsets = _normalized_with_offsets(text)
        if not normalized or not offsets:
            return []

        found: list[dict[str, Any]] = []
        seen: set[tuple[int, int]] = set()
        for match in self.pattern.finditer(normalized):
            start_n, end_n = match.span(1)
            if start_n >= end_n:
                continue
            start = offsets[start_n]
            end = offsets[end_n - 1] + 1
            span = (start, end)
            if span in seen:
                continue
            seen.add(span)
            found.append({
                "type": ITEM_TYPE,
                "start": start,
                "end": end,
                "confidence": 1.0,
                "source": "user_dictionary",
                "mandatory": True,
            })
        return found


class ForcedMaskRegistry:
    def __init__(self, *, managed: bool = False) -> None:
        self._lock = threading.Lock()
        self._snapshot = ForcedMaskSnapshot(
            terms=(), revision=_revision(), ready=not managed, managed=managed, pattern=None
        )

    def configure(self, values: Iterable[str]) -> ForcedMaskSnapshot:
        terms = normalize_terms(values)
        normalized = sorted(
            (
                unicodedata.normalize("NFD", unicodedata.normalize("NFD", term).lower())
                for term in terms
            ),
            key=len,
            reverse=True,
        )
        # Lookahead keeps overlapping starts.  Longest-first makes the match at
        # one start deterministic; masker.merge_overlapping combines the rest.
        pattern = re.compile(
            f"(?=({'|'.join(re.escape(term) for term in normalized)}))",
        ) if normalized else None
        snapshot = ForcedMaskSnapshot(
            terms=terms,
            revision=_revision(),
            ready=True,
            managed=self._snapshot.managed,
            pattern=pattern,
        )
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def snapshot(self) -> ForcedMaskSnapshot:
        with self._lock:
            return self._snapshot

    def reset(self, *, managed: bool | None = None) -> None:
        current = self.snapshot()
        target_managed = current.managed if managed is None else managed
        with self._lock:
            self._snapshot = ForcedMaskSnapshot(
                terms=(), revision=_revision(), ready=not target_managed,
                managed=target_managed, pattern=None,
            )

    def status(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        return {
            "managed": snapshot.managed,
            "ready": snapshot.ready,
            "active": snapshot.active,
            "count": len(snapshot.terms),
            "revision": snapshot.revision,
        }


registry = ForcedMaskRegistry(managed=config.DESKTOP_MANAGED)
