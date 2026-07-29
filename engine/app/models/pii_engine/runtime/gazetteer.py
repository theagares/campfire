"""
Gazetteer(사전) 피처 모듈.

train.json 정답 span 표면형 + 접미사 매칭으로 inference 시 모델 입력 피처 주입.
(augment_data.py 의 외부 사전 증강과는 다른 축 — feature injection)
"""

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import ahocorasick
import numpy as np

MIN_FORM_LEN = 2
MAX_FORM_LEN = 30
MIN_FREQ = 1

SUFFIX_MIN_LEN = 2
SUFFIX_MAX_LEN = 4
SUFFIX_MIN_FREQ = 3

_WORD_RE = re.compile(r"\S+")


def build_gazetteer(
    train_data: List[dict],
    target_labels: List[str],
    min_len: int = MIN_FORM_LEN,
    max_len: int = MAX_FORM_LEN,
    min_freq: int = MIN_FREQ,
    build_suffix: bool = True,
    suffix_min_len: int = SUFFIX_MIN_LEN,
    suffix_max_len: int = SUFFIX_MAX_LEN,
    suffix_min_freq: int = SUFFIX_MIN_FREQ,
) -> Dict[str, Dict[str, List[str]]]:
    counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for item in train_data:
        for pii in item.get("PII_set", []):
            label = pii.get("label")
            form = pii.get("form", "").strip()
            if label not in target_labels:
                continue
            if not (min_len <= len(form) <= max_len):
                continue
            counts[label][form] += 1

    exact = {}
    for label, forms in counts.items():
        kept = [f for f, c in forms.items() if c >= min_freq]
        exact[label] = sorted(kept, key=len, reverse=True)

    suffix = {}
    if build_suffix:
        suf_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for item in train_data:
            for pii in item.get("PII_set", []):
                label = pii.get("label")
                form = pii.get("form", "").strip()
                if label not in target_labels:
                    continue
                for L in range(suffix_min_len, min(suffix_max_len, max(len(form) - 1, 0)) + 1):
                    if L <= 0 or L >= len(form):
                        continue
                    suf_counts[label][form[-L:]] += 1
        for label, sufs in suf_counts.items():
            kept = [s for s, c in sufs.items() if c >= suffix_min_freq]
            suffix[label] = sorted(kept, key=len, reverse=True)

    return {"exact": exact, "suffix": suffix}


def save_gazetteer(gaz: Dict, path: str):
    Path(path).write_text(
        json.dumps(gaz, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_gazetteer(path: str) -> Dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


class GazetteerTrie:
    """gazetteer 매칭 — Aho-Corasick(C 확장, pyahocorasick) 기반.

    이전엔 순수 Python 캐릭터 트라이로 구현되어 있었는데(각 시작 위치에서 매번
    루트부터 재시작 + 접미사 매칭은 단어마다 라벨19종 x 접미사 최대 900여개를
    전부 `str.find()` 로 훑음), 실측상 PII 추론 전체 시간의 ~72%를 차지하는
    병목이었다. 패턴 집합 전체를 한 번의 자동자 순회로 찾는 Aho-Corasick 으로
    교체해 문장 기준 ~73배, 긴 문서 기준 ~73배 빨라졌다(원본 대비 완전 동일한
    출력을 내는지 검증 스크립트로 확인 후 반영 — engine/scripts/
    verify_and_apply_gazetteer_ac.py).

    구 트라이의 접미사 매칭에 있던 동작(라벨별로 "단어 안 어딘가에 접미사가
    idx>=1 위치로 나타나는지"만 보고, 마킹 시작점은 실제 매칭 위치가 아니라
    항상 "단어 전체의 시작"을 씀)을 그대로 재현한다 — 별개의 개선 과제이지,
    이 교체의 목적이 아니다.
    """

    def __init__(self, gaz: Dict, target_labels: List[str]):
        self.target_labels = target_labels
        self.label2idx = {l: i for i, l in enumerate(target_labels)}

        if "exact" in gaz or "suffix" in gaz:
            exact = gaz.get("exact", {}) or {}
            self.suffix_pool = gaz.get("suffix", {}) or {}
        else:
            exact = gaz
            self.suffix_pool = {}

        # ── 정확매칭 자동자: 패턴(form) -> 라벨 집합(같은 form 이 여러 라벨에 있으면 합침) ──
        exact_map: Dict[str, set] = {}
        n_entries = 0
        for label, forms in exact.items():
            if label not in self.label2idx:
                continue
            for form in forms:
                exact_map.setdefault(form, set()).add(label)
                n_entries += 1
        self.n_entries = n_entries
        self.exact_automaton = ahocorasick.Automaton()
        for form, labels in exact_map.items():
            self.exact_automaton.add_word(form, (len(form), labels))
        self._has_exact = len(exact_map) > 0
        if self._has_exact:
            self.exact_automaton.make_automaton()

        # ── 접미사매칭 자동자: 패턴 -> 라벨 집합 + 라벨별 순위(리스트 순서=길이 내림차순) ──
        self.suffix_rank: Dict[str, Dict[str, int]] = {}
        pattern_labels: Dict[str, set] = {}
        for label, sufs in self.suffix_pool.items():
            if label not in self.label2idx:
                continue
            self.suffix_rank[label] = {s: i for i, s in enumerate(sufs)}
            for s in sufs:
                pattern_labels.setdefault(s, set()).add(label)
        self.pattern_labels = pattern_labels
        self.n_suffixes = sum(len(v) for v in self.suffix_pool.values())
        self.suffix_automaton = ahocorasick.Automaton()
        for pat, labels in pattern_labels.items():
            self.suffix_automaton.add_word(pat, (len(pat), labels))
        self._has_suffix = len(pattern_labels) > 0
        if self._has_suffix:
            self.suffix_automaton.make_automaton()

    def _exact_hits(self, sentence: str) -> Dict[int, Tuple[int, set]]:
        """시작 위치별로 그 위치에서 시작하는 가장 긴 매칭만 남긴다(구 트라이의
        "longest-match-from-this-exact-start" 와 동일)."""
        best: Dict[int, Tuple[int, set]] = {}
        if not self._has_exact:
            return best
        for end_index, (length, labels) in self.exact_automaton.iter(sentence):
            end = end_index + 1
            start = end - length
            cur = best.get(start)
            if cur is None or end > cur[0]:
                best[start] = (end, labels)
        return best

    def _suffix_word_hits(self, sentence: str, min_prefix: int = 1) -> List[Tuple[int, int, str]]:
        if not self._has_suffix:
            return []
        hits = []
        for m in _WORD_RE.finditer(sentence):
            word = m.group()
            w_start = m.start()

            # 이 단어 안에서 실제로 등장하는 패턴들의 가장 왼쪽 위치만 모은다
            # (str.find() 의 "첫 occurrence" 를 재현) — 라벨x접미사 전수조사 대신
            # 자동자가 실제로 찾아낸 소수의 패턴에 대해서만 이후 작업한다.
            pattern_leftmost: Dict[str, int] = {}
            for end_index, (length, _labels) in self.suffix_automaton.iter(word):
                end = end_index + 1
                start = end - length
                pat = word[start:end]
                if pat not in pattern_leftmost or start < pattern_leftmost[pat]:
                    pattern_leftmost[pat] = start

            per_label_best: Dict[str, Tuple[int, int, int]] = {}  # label -> (rank, start, end)
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

            for label, (_rank, _start, end) in per_label_best.items():
                # b=w_start(단어 시작)를 그대로 쓴다 — 실제 접미사 시작 위치가
                # 아니다. 구 트라이의 동작을 그대로 재현(위 클래스 docstring 참고).
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


def aggregate_to_tokens(
    char_feat: np.ndarray, offsets: List[Tuple[int, int]]
) -> np.ndarray:
    num_labels = char_feat.shape[1] if char_feat.size else 0
    out = np.zeros((len(offsets), num_labels), dtype=np.float32)
    L = char_feat.shape[0]
    for t, (cs, ce) in enumerate(offsets):
        if ce <= cs:
            continue
        cs_c = max(0, min(cs, L))
        ce_c = max(0, min(ce, L))
        if ce_c > cs_c:
            out[t] = char_feat[cs_c:ce_c].max(axis=0)
    return out
