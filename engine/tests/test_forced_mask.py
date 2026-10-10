import unicodedata

import pytest

from app.core import forced_mask


def test_literal_terms_match_all_occurrences_case_insensitively():
    snapshot = forced_mask.ForcedMaskRegistry().configure(["Project Aurora"])
    items = snapshot.find("PROJECT AURORA / project aurora")
    assert [(item["start"], item["end"]) for item in items] == [(0, 14), (17, 31)]
    assert all(item["mandatory"] is True for item in items)
    assert all("text" not in item for item in items)


def test_nfc_rule_matches_nfd_text_with_original_offsets():
    term = "기밀"
    text = f"앞 {unicodedata.normalize('NFD', term)} 뒤"
    item = forced_mask.ForcedMaskRegistry().configure([term]).find(text)[0]
    assert text[item["start"]:item["end"]] == unicodedata.normalize("NFD", term)


def test_overlapping_terms_are_all_reported_for_masker_to_merge():
    items = forced_mask.ForcedMaskRegistry().configure(["abc", "bcde"]).find("abcde")
    assert {(item["start"], item["end"]) for item in items} == {(0, 3), (1, 5)}


def test_terms_are_literal_not_regular_expressions():
    items = forced_mask.ForcedMaskRegistry().configure(["a+b"] ).find("aaab a+b")
    assert [(item["start"], item["end"]) for item in items] == [(5, 8)]


def test_normalization_deduplicates_and_enforces_limits():
    assert forced_mask.normalize_terms([" Secret ", "secret", ""]) == ("Secret",)
    with pytest.raises(forced_mask.ForcedMaskRuleError):
        forced_mask.normalize_terms(["x" * (forced_mask.MAX_TERM_CHARS + 1)])
    with pytest.raises(forced_mask.ForcedMaskRuleError):
        forced_mask.normalize_terms(["line\nbreak"])


def test_managed_registry_is_not_ready_until_configured():
    rules = forced_mask.ForcedMaskRegistry(managed=True)
    assert rules.status()["ready"] is False
    rules.configure([])
    assert rules.status() == {
        "managed": True,
        "ready": True,
        "active": False,
        "count": 0,
        "revision": rules.status()["revision"],
    }


@pytest.mark.parametrize("text", [
    "Project\nAurora", "Project Aurora", "Project  \t Aurora", "Pro​ject Aurora",
])
def test_whitespace_and_zero_width_variants_still_match(text):
    # PDF 줄바꿈·NBSP·제로폭 문자로 갈린 등록어를 놓치면 "깨끗함" 으로 원본이 나간다.
    items = forced_mask.ForcedMaskRegistry().configure(["Project Aurora"]).find(text)
    assert [(i["start"], i["end"]) for i in items] == [(0, len(text))]


def test_compatibility_and_case_folding_forms_match():
    snapshot = forced_mask.ForcedMaskRegistry().configure(["ABC123", "Straße"])
    assert len(snapshot.find("ＡＢＣ１２３")) == 1
    assert len(snapshot.find("STRASSE")) == 1


@pytest.mark.parametrize("term,text", [("김민수", "김민숙"), ("이수", "이숙"), ("cafe", "café")])
def test_match_never_ends_inside_an_original_character(term, text):
    # 예전엔 분해된 자모 접두사(ㅅㅜ ⊂ ㅅㅜㄱ)에서 매치가 끝나 다른 이름까지 강제 마스킹됐다.
    assert forced_mask.ForcedMaskRegistry().configure([term]).find(text) == []
    nfd = unicodedata.normalize("NFD", text)
    assert forced_mask.ForcedMaskRegistry().configure([term]).find(nfd) == []
