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


class _TrieNode:
    __slots__ = ("children", "labels")

    def __init__(self):
        self.children: Dict[str, "_TrieNode"] = {}
        self.labels: set = set()


class GazetteerTrie:
    def __init__(self, gaz: Dict, target_labels: List[str]):
        self.target_labels = target_labels
        self.label2idx = {l: i for i, l in enumerate(target_labels)}
        self.root = _TrieNode()

        if "exact" in gaz or "suffix" in gaz:
            exact = gaz.get("exact", {}) or {}
            self.suffix_pool = gaz.get("suffix", {}) or {}
        else:
            exact = gaz
            self.suffix_pool = {}

        n_entries = 0
        for label, forms in exact.items():
            if label not in self.label2idx:
                continue
            for form in forms:
                self._insert(form, label)
                n_entries += 1
        self.n_entries = n_entries
        self.n_suffixes = sum(len(v) for v in self.suffix_pool.values())

    def _insert(self, form: str, label: str):
        node = self.root
        for ch in form:
            node = node.children.setdefault(ch, _TrieNode())
        node.labels.add(label)

    def _longest_match_at(self, sentence: str, start: int) -> Tuple[int, set]:
        node = self.root
        best_end = start
        best_labels: set = set()
        i = start
        n = len(sentence)
        while i < n and sentence[i] in node.children:
            node = node.children[sentence[i]]
            i += 1
            if node.labels:
                best_end = i
                best_labels = node.labels
        return best_end, best_labels

    def _suffix_word_hits(self, sentence: str, min_prefix: int = 1) -> List[Tuple[int, int, str]]:
        if not self.suffix_pool:
            return []
        hits = []
        for m in _WORD_RE.finditer(sentence):
            word = m.group()
            w_start = m.start()
            for label, sufs in self.suffix_pool.items():
                if label not in self.label2idx:
                    continue
                for suf in sufs:
                    idx = word.find(suf)
                    if idx < min_prefix:
                        continue
                    b = w_start
                    e = w_start + idx + len(suf)
                    hits.append((b, e, label))
                    break
        return hits

    def match_sentence(self, sentence: str) -> np.ndarray:
        n = len(sentence)
        num_labels = len(self.target_labels)
        feat = np.zeros((n, num_labels), dtype=np.float32)

        i = 0
        while i < n:
            end, labels = self._longest_match_at(sentence, i)
            if labels:
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
