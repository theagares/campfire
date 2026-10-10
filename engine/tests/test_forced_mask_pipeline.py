import asyncio

from app.core import forced_mask, model_status
from app.core.pipeline.orchestrator import run_pipeline


def test_forced_term_masks_when_ml_models_are_unavailable(monkeypatch):
    forced_mask.registry.configure(["Project Aurora"])
    monkeypatch.setattr(model_status, "all_ready", lambda: False)

    result = asyncio.run(run_pipeline(text="share PROJECT AURORA now"))

    assert result["scanStatus"] == "models_not_ready"
    assert result["maskedText"] == "share [사용자 지정 마스킹] now"
    assert result["stats"]["forcedMaskCount"] == 1
    assert result["forcedMaskItems"][0]["mandatory"] is True
    assert result["policy"]["forcedMask"]["complete"] is True


def test_forced_term_also_masks_attached_user_prompt(monkeypatch):
    forced_mask.registry.configure(["Blue Finch"])
    monkeypatch.setattr(model_status, "all_ready", lambda: False)

    result = asyncio.run(run_pipeline(text="document", user_prompt="summarize Blue Finch"))

    assert result["userPromptMasked"] == "summarize [사용자 지정 마스킹]"
    assert len(result["userPromptForcedMaskItems"]) == 1


def test_active_rules_block_uninspectable_file():
    forced_mask.registry.configure(["secret"])
    result = asyncio.run(run_pipeline(
        file_bytes=b"\x00\x01", mime_type="application/octet-stream", file_name="blob.bin"
    ))
    assert result["scanStatus"] != "ok"
    assert result["blocked"] is True
    assert result["policy"]["forcedMask"]["complete"] is False


def test_active_rules_block_truncated_document(monkeypatch):
    from app import config

    forced_mask.registry.configure(["secret"])
    monkeypatch.setattr(config, "MAX_TEXT_CHARS", 5)
    monkeypatch.setattr(model_status, "all_ready", lambda: False)
    result = asyncio.run(run_pipeline(text="secret after limit"))
    assert result["truncated"] is True
    assert result["blocked"] is True


def test_desktop_managed_policy_fails_closed_before_sync():
    forced_mask.registry.reset(managed=True)
    result = asyncio.run(run_pipeline(text="plain text"))
    assert result["scanStatus"] == "policy_not_ready"
    assert result["blocked"] is True
    assert result["policy"]["forcedMask"]["ready"] is False


def test_file_name_with_forced_term_is_flagged_and_not_reused(monkeypatch):
    # 내용을 가려도 "Aurora_계획_masked.md" 처럼 이름으로 샌다 — 사실만 알리고 이름은 안 쓴다.
    forced_mask.registry.configure(["Aurora"])
    monkeypatch.setattr(model_status, "all_ready", lambda: False)
    result = asyncio.run(run_pipeline(
        file_bytes="plain body".encode(), mime_type="text/plain", file_name="Aurora_plan.txt"
    ))
    assert result["policy"]["forcedMask"]["fileNameForced"] is True
    plain = asyncio.run(run_pipeline(
        file_bytes="plain body".encode(), mime_type="text/plain", file_name="plain.txt"
    ))
    assert plain["policy"]["forcedMask"]["fileNameForced"] is False
