from __future__ import annotations

from app.core.pipeline import injection_chunker
from app.core.pipeline.injection_chunker import split_token_chunks


class _CharacterTokenizer:
    """One token per non-whitespace character, sufficient for offset tests."""

    is_fast = True

    def __call__(self, text: str, **_kwargs):
        return {
            "offset_mapping": [
                (index, index + 1) for index, char in enumerate(text) if not char.isspace()
            ]
        }


def _covered_by_one_chunk(chunks: list[dict], start: int, end: int) -> bool:
    return any(
        chunk["offset"] <= start
        and end <= chunk["offset"] + len(chunk["text"])
        for chunk in chunks
    )


def test_token_windows_are_bounded_and_overlap_by_48_tokens():
    chunks = split_token_chunks(
        "가" * 300,
        max_tokens=128,
        overlap_tokens=48,
        tokenizer=_CharacterTokenizer(),
    )

    assert [chunk["offset"] for chunk in chunks] == [0, 80, 160, 240]
    assert all(chunk["token_count"] <= 128 for chunk in chunks)
    assert all(
        chunks[index]["offset"] + chunks[index]["token_count"]
        - chunks[index + 1]["offset"]
        == 48
        for index in range(len(chunks) - 1)
    )


def test_sentence_boundary_is_preferred_near_window_end():
    text = "가" * 100 + ". " + "나" * 180
    chunks = split_token_chunks(
        text,
        max_tokens=128,
        overlap_tokens=48,
        tokenizer=_CharacterTokenizer(),
    )

    assert chunks[0]["token_count"] == 101
    assert chunks[0]["text"].endswith(". ")


def test_19_token_attack_is_wholly_present_regardless_of_alignment():
    text = "가" * 400
    chunks = split_token_chunks(
        text,
        max_tokens=128,
        overlap_tokens=48,
        tokenizer=_CharacterTokenizer(),
    )

    for start in range(0, len(text) - 19 + 1):
        assert _covered_by_one_chunk(chunks, start, start + 19)


def test_tokenizer_failure_uses_short_overlapping_character_fallback(monkeypatch):
    monkeypatch.setattr(
        injection_chunker,
        "split_token_chunks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("broken tokenizer")),
    )

    chunks = injection_chunker.split_injection_chunks("가" * 700)

    assert [chunk["offset"] for chunk in chunks] == [0, 140, 280, 420]
    assert all(chunk["chunking"] == "character_fallback" for chunk in chunks)
    assert max(len(chunk["text"]) for chunk in chunks) == 300
