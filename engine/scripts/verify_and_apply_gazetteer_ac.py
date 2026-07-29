#!/usr/bin/env python3
"""PII gazetteer 매칭(현재 세션 프로파일링 기준 전체 PII 추론 시간의 ~72%)을
Aho-Corasick(pyahocorasick, C 확장)으로 교체하기 *전에* 원본
GazetteerTrie.match_sentence() 와 완전히 동일한 결과를 내는지부터 검증하고,
통과한 경우에만 속도를 비교한다("확인 후 적용" — 모델 재학습 없음, 순수
알고리즘/자료구조 교체).

원본 병목 두 곳:
  1) 정확매칭: 각 시작 위치에서 트라이를 처음부터 다시 훑음 (재시작 비용).
  2) 접미사매칭: 문서의 단어마다 라벨 19개 x 접미사 최대 900여개를
     전부 순회하며 `word.find(suf)` 를 호출 (실측상 가장 느린 부분).

Aho-Corasick 로 두 매칭 다 "패턴 집합 전체를 한 번의 자동자 순회로" 찾고,
접미사 쪽은 실제로 매칭된 소수의 패턴에 대해서만 라벨별 우선순위(원본과 동일한
"긴 접미사부터" 순서)를 적용해 원본과 동일한 첫-매칭 결과를 재구성한다.

사용법:
    python verify_and_apply_gazetteer_ac.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

RUNTIME_DIR = Path(__file__).resolve().parent.parent / "app" / "models" / "pii_engine" / "runtime"
sys.path.insert(0, str(RUNTIME_DIR))

from gazetteer import GazetteerTrie, load_gazetteer  # noqa: E402

GAZ_PATH = RUNTIME_DIR.parent / "models" / "seed42" / "gazetteer.json"
LABEL_MAP_PATH = RUNTIME_DIR.parent / "models" / "seed42" / "label_map.json"

_WORD_RE = re.compile(r"\S+")

# 데모 문서 발췌 + 반복으로 만든 긴 텍스트(속도 비교용 큰 입력) — 이번 세션에서
# 실제로 탐지에 쓰인 문장들이라 대표성이 있다.
TEST_SENTENCES = [
    "성명: 김도윤, 주민등록번호: 900312-1047815, 이메일: doyoon.kim90@navermail.com, 휴대전화: 010-3948-2211",
    "카드번호: 4092-8817-3350-2261, 결제 연동 계좌: 신한은행 110-482-773910 (예금주 김도윤)",
    "(주)한빛커머스 고객경험팀 김서연 팀장(seoyeon.kim@hanbit-commerce.co.kr, 내선 2041)",
    "VIP 회원 박준호 님은 신한카드 5312-8890-2245-1187로 결제하셨고, 국민은행 계좌 612301-04-889210으로 환급했습니다.",
    "이서진 님의 배송지는 경기도 성남시 분당구 판교로 228, 105동 1203호이며 연락처는 010-2299-5510입니다.",
    "정하늘 님은 여권번호 M12345678로 신원을 확인했고, 등록 이메일은 haneul.jeong@outlook.kr입니다.",
    "(주)밝은유통 최민아 부장님(010-7712-3388, minah.choi@brightretail.kr)의 세금계산서를 재발행했습니다.",
    "오늘 날씨는 맑고 기온은 22도이며 저녁에는 약한 바람이 불겠습니다.",
]


class GazetteerTrieAC:
    """GazetteerTrie 와 동일한 match_sentence() 출력을 내는 Aho-Corasick 구현."""

    def __init__(self, gaz: dict, target_labels: list[str]):
        import ahocorasick

        self.target_labels = target_labels
        self.label2idx = {l: i for i, l in enumerate(target_labels)}

        if "exact" in gaz or "suffix" in gaz:
            exact = gaz.get("exact", {}) or {}
            suffix_pool = gaz.get("suffix", {}) or {}
        else:
            exact = gaz
            suffix_pool = {}
        self.suffix_pool = suffix_pool

        # ── 정확매칭 자동자: 패턴(form) -> 라벨 집합(같은 form 이 여러 라벨에 있으면 합침) ──
        exact_map: dict[str, set] = {}
        for label, forms in exact.items():
            if label not in self.label2idx:
                continue
            for form in forms:
                exact_map.setdefault(form, set()).add(label)
        self.exact_automaton = ahocorasick.Automaton()
        for form, labels in exact_map.items():
            self.exact_automaton.add_word(form, (len(form), labels))
        self._has_exact = len(exact_map) > 0
        if self._has_exact:
            self.exact_automaton.make_automaton()

        # ── 접미사매칭 자동자: 패턴 -> 라벨 집합, + 라벨별 순위(원본 리스트 순서=길이 내림차순) ──
        self.suffix_rank: dict[str, dict[str, int]] = {}
        pattern_labels: dict[str, set] = {}
        for label, sufs in suffix_pool.items():
            if label not in self.label2idx:
                continue
            self.suffix_rank[label] = {s: i for i, s in enumerate(sufs)}
            for s in sufs:
                pattern_labels.setdefault(s, set()).add(label)
        self.pattern_labels = pattern_labels
        self.suffix_automaton = ahocorasick.Automaton()
        for pat, labels in pattern_labels.items():
            self.suffix_automaton.add_word(pat, (len(pat), labels))
        self._has_suffix = len(pattern_labels) > 0
        if self._has_suffix:
            self.suffix_automaton.make_automaton()

    def _exact_hits(self, sentence: str) -> dict[int, tuple[int, set]]:
        best: dict[int, tuple[int, set]] = {}
        if not self._has_exact:
            return best
        for end_index, (length, labels) in self.exact_automaton.iter(sentence):
            end = end_index + 1
            start = end - length
            cur = best.get(start)
            if cur is None or end > cur[0]:
                best[start] = (end, labels)
        return best

    def _suffix_word_hits(self, sentence: str, min_prefix: int = 1) -> list[tuple[int, int, str]]:
        if not self._has_suffix:
            return []
        hits = []
        for m in _WORD_RE.finditer(sentence):
            word = m.group()
            w_start = m.start()

            pattern_leftmost: dict[str, int] = {}
            for end_index, (length, _labels) in self.suffix_automaton.iter(word):
                end = end_index + 1
                start = end - length
                pat = word[start:end]
                if pat not in pattern_leftmost or start < pattern_leftmost[pat]:
                    pattern_leftmost[pat] = start

            per_label_best: dict[str, tuple[int, int, int]] = {}  # label -> (rank, start, end)
            for pat, start in pattern_leftmost.items():
                if start < min_prefix:
                    continue
                end = start + len(pat)
                for label in self.pattern_labels.get(pat, ()):
                    rank_map = self.suffix_rank.get(label)
                    if rank_map is None or pat not in rank_map:
                        continue
                    rank = rank_map[pat]
                    cur = per_label_best.get(label)
                    if cur is None or rank < cur[0]:
                        per_label_best[label] = (rank, start, end)

            for label, (_rank, start, end) in per_label_best.items():
                # 원본과 동일하게 b=w_start(단어 시작)를 그대로 쓴다 — 실제 접미사
                # 시작 위치(w_start+start)가 아니다. 이건 원본의 알려진 동작(버그)을
                # 의도적으로 그대로 재현하는 것 — 결과 동일성이 이 스크립트의 목표.
                hits.append((w_start, w_start + end, label))
        return hits

    def match_sentence(self, sentence: str) -> np.ndarray:
        n = len(sentence)
        num_labels = len(self.target_labels)
        feat = np.zeros((n, num_labels), dtype=np.float32)

        exact_hits = self._exact_hits(sentence)
        i = 0
        while i < n:
            hit = exact_hits.get(i)
            if hit:
                end, labels = hit
                for lbl in labels:
                    feat[i:end, self.label2idx[lbl]] = 1.0
                i = end
            else:
                i += 1

        for b, e, label in self._suffix_word_hits(sentence):
            feat[b:e, self.label2idx[label]] = 1.0

        return feat


def main() -> None:
    gaz = load_gazetteer(str(GAZ_PATH))
    label_map = json.loads(LABEL_MAP_PATH.read_text(encoding="utf-8"))
    target_labels = label_map["target_labels"]

    print(f"gazetteer: exact {sum(len(v) for v in gaz.get('exact', {}).values())}개, "
          f"suffix {sum(len(v) for v in gaz.get('suffix', {}).values())}개, "
          f"라벨 {len(target_labels)}개")

    trie = GazetteerTrie(gaz, target_labels)
    ac = GazetteerTrieAC(gaz, target_labels)

    print("\n[1] 정확성 검증 (원본 트라이 vs Aho-Corasick, 문장 단위)")
    all_match = True
    for i, sent in enumerate(TEST_SENTENCES):
        feat_trie = trie.match_sentence(sent)
        feat_ac = ac.match_sentence(sent)
        same = np.array_equal(feat_trie, feat_ac)
        all_match &= same
        print(f"  [{i}] {'✅' if same else '❌'} {sent[:40]}...")
        if not same:
            diff_pos = np.argwhere(feat_trie != feat_ac)
            print(f"      불일치 위치(char_idx, label_idx) 예시: {diff_pos[:10].tolist()}")

    # 긴 텍스트(문장 반복)로도 한 번 더 — 위치 누적 버그가 짧은 문장에서는 안 드러날 수 있음.
    long_text = " ".join(TEST_SENTENCES * 20)
    feat_trie_long = trie.match_sentence(long_text)
    feat_ac_long = ac.match_sentence(long_text)
    same_long = np.array_equal(feat_trie_long, feat_ac_long)
    all_match &= same_long
    print(f"  [long, {len(long_text)}자] {'✅' if same_long else '❌'}")

    print(f"\n검증 결과: {'✅ 전부 일치' if all_match else '❌ 불일치 있음 — 적용 보류'}")
    if not all_match:
        return

    print("\n[2] 속도 비교")
    repeats = 200
    t0 = time.perf_counter()
    for _ in range(repeats):
        for sent in TEST_SENTENCES:
            trie.match_sentence(sent)
    trie_dt = (time.perf_counter() - t0) / repeats

    t0 = time.perf_counter()
    for _ in range(repeats):
        for sent in TEST_SENTENCES:
            ac.match_sentence(sent)
    ac_dt = (time.perf_counter() - t0) / repeats

    print(f"  문장 {len(TEST_SENTENCES)}개 배치 x {repeats}회 평균:")
    print(f"    원본 GazetteerTrie   : {trie_dt*1000:.2f}ms")
    print(f"    Aho-Corasick         : {ac_dt*1000:.2f}ms")
    print(f"    배율                 : {trie_dt/ac_dt:.2f}x")

    repeats_long = 30
    t0 = time.perf_counter()
    for _ in range(repeats_long):
        trie.match_sentence(long_text)
    trie_long_dt = (time.perf_counter() - t0) / repeats_long

    t0 = time.perf_counter()
    for _ in range(repeats_long):
        ac.match_sentence(long_text)
    ac_long_dt = (time.perf_counter() - t0) / repeats_long

    print(f"\n  긴 텍스트({len(long_text)}자) x {repeats_long}회 평균:")
    print(f"    원본 GazetteerTrie   : {trie_long_dt*1000:.2f}ms")
    print(f"    Aho-Corasick         : {ac_long_dt*1000:.2f}ms")
    print(f"    배율                 : {trie_long_dt/ac_long_dt:.2f}x")

    print("\n=== 결론: 검증 통과 — GazetteerTrieAC 를 gazetteer.py 에 적용 가능 ===")


if __name__ == "__main__":
    main()
