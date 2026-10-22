"""탐지된 PII 개체마다 "가려야 하는가(MASK)" 확률을 매긴다 — 회사 연락처처럼 가릴 필요 없는 개체를 골라내기 위함.

taehwan 의 joint_masking direct_full(ax_gazetteer_crf seed42) 추론부를 옮겼다.
원본: /data/upsecurity00/members/taehwan/joint_masking/network.py (Integrated.encode / contextual).
- 무엇을 가릴지(탐지)는 앱 v2 모델이 정한다. 같은 KDPII 시험셋(겹침 기준)에서 v2 F1 0.988,
  이 모델의 탐지부 0.872 라 탐지는 v2 가 낫다. 이 모델은 "가리지 않아도 되는가" 만 거든다.
- 점수는 이 모델 자신의 탐지부(CRF)가 찾은 개체에 매긴다 — 학습·평가 때와 같은 조건이다.
  v2 개체는 같은 종류로 겹치는 그 개체의 점수를 빌린다. 짝이 없으면 점수 없음(=가림).
  v2 경계를 그대로 넣으면 안 된다: v2 는 조사까지 잡는데('시디즈의') MLP 는 경계에 민감해
  '시디즈' 1.0 → '시디즈의' 0.003 으로 뒤집혔다(시험 문서 5개에서 가려야 할 개체 6% 가 풀림).
- 문장마다 토큰화(256 토큰, stride 64) → 인코더 특징 + 사전 특징 + 탐지 확률(evidence)을 토큰별로 평균
  → 개체 평균 + 문서 평균 + 종류·분야 one-hot → MLP → sigmoid.
- 학습 데이터는 분야 6종 중 하나를 알려 줬지만 앱은 분야를 모른다. 6종 모두 넣어 보고 가장 높은(가장 가리자는)
  확률을 쓴다 — 어느 분야로 보든 KEEP 일 때만 KEEP 이 된다.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

import torch
from torch import nn
from safetensors.torch import load_file
from transformers import AutoConfig, AutoModel

TYPES = ["NAME", "ADDRESS", "WORKPLACE", "DEPARTMENT", "POSITION", "SCHOOL", "MOBILE_PHONE",
         "TELEPHONE", "RRN", "ALIEN_REGISTRATION_NUMBER", "DRIVER_LICENSE_NUMBER", "VEHICLE_NUMBER",
         "BANK_ACCOUNT_NUMBER", "CARD_NUMBER", "EMAIL", "PASSPORT_NUMBER", "AGE", "DATE_OF_BIRTH", "MAJOR"]
# 앱 v2 모델 라벨 → 이 모델의 종류(같은 KDPII-19, 순서도 같다)
APP_TO_TYPE = dict(zip(
    ["PS_NAME", "LC_ADDRESS", "OG_WORKPLACE", "OG_DEPARTMENT", "CV_POSITION", "OGG_EDUCATION", "QT_MOBILE",
     "QT_PHONE", "QT_RESIDENT_NUMBER", "QT_ALIEN_NUMBER", "QT_DRIVER_NUMBER", "QT_PLATE_NUMBER",
     "QT_ACCOUNT_NUMBER", "QT_CARD_NUMBER", "TMI_EMAIL", "QT_PASSPORT_NUMBER", "QT_AGE", "DT_BIRTH", "FD_MAJOR"],
    TYPES))
DOMAINS = ["career_education", "contract", "financial", "legal", "medical", "support"]
BIO = ["O", *("B-" + t for t in TYPES), *("I-" + t for t in TYPES)]
BIO_SIZE = len(BIO)

# 줄바꿈, 또는 문장 끝 부호 뒤 공백에서 자른다.
_SENTENCE_BREAK = re.compile(r"\n+|(?<=[.!?。])\s+")


def norm(text: str) -> str:
    """개체 묶음 키 — 원본 masking.candidates.norm 과 같다."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


def split_sentences(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for m in _SENTENCE_BREAK.finditer(text):
        if text[start:m.start()].strip():
            spans.append((start, m.start()))
        start = m.end()
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


class _Gazetteer:
    """원본 stagea.base_modeling.GazetteerTrie.features — 글자마다 가장 긴 사전 일치의 종류를 켠다."""

    def __init__(self, entries: dict[str, list[str]]):
        self.root: dict = {}
        for label, forms in entries.items():
            for form in forms:
                if not form:
                    continue
                node = self.root
                for ch in form:
                    node = node.setdefault(ch, {})
                node.setdefault(None, set()).add(TYPES.index(label))

    def features(self, text: str, offsets: list[tuple[int, int]]) -> list[list[float]]:
        char_labels = [set() for _ in text]
        for start in range(len(text)):
            node, cursor, best = self.root, start, None
            while cursor < len(text) and text[cursor] in node:
                node = node[text[cursor]]
                cursor += 1
                if node.get(None):
                    best = (cursor, node[None])
            if best:
                for pos in range(start, best[0]):
                    char_labels[pos].update(best[1])
        rows = []
        for begin, end in offsets:
            vec = [0.0] * len(TYPES)
            for pos in range(max(0, begin), min(len(text), end)):
                for i in char_labels[pos]:
                    vec[i] = 1.0
            rows.append(vec)
        return rows


def _crf_constraints() -> tuple[torch.Tensor, torch.Tensor]:
    """원본 stagea.crf.CRF 의 BIO 제약: I-X 로 시작 못 하고, B-X/I-X 뒤에만 I-X 가 온다."""
    start, trans = torch.zeros(BIO_SIZE), torch.zeros(BIO_SIZE, BIO_SIZE)
    for dst, tag in enumerate(BIO):
        if tag.startswith("I-"):
            start[dst] = -10000.0
            for src, prev in enumerate(BIO):
                if prev not in ("B-" + tag[2:], "I-" + tag[2:]):
                    trans[src, dst] = -10000.0
    return start, trans


def _viterbi(em: torch.Tensor, start: torch.Tensor, end: torch.Tensor, trans: torch.Tensor) -> list[int]:
    """한 문장 창의 최적 BIO 경로(원본 stagea.crf.viterbi 의 배치 1 판)."""
    score, history = start + em[0], []
    for t in range(1, len(em)):
        nxt, idx = (score[:, None] + trans).max(0)
        score = nxt + em[t]
        history.append(idx)
    best = int((score + end).argmax())
    path = [best]
    for idx in reversed(history):
        best = int(idx[best])
        path.append(best)
    return path[::-1]


def _decode_bio(offsets: list[tuple[int, int]], tags: list[int]) -> list[dict]:
    """원본 stagea.kdpii.decode_bio."""
    spans, cur = [], None
    for (begin, stop), tag_id in zip(offsets, tags):
        if stop <= begin:
            continue
        tag = BIO[tag_id]
        if tag == "O":
            cur = None
        elif tag.startswith("B-") or cur is None or cur["label"] != tag[2:]:
            cur = {"begin": begin, "end": stop, "label": tag[2:]}
            spans.append(cur)
        else:
            cur["end"] = stop
    return spans


def _flat_spans(cands: list[dict]) -> list[dict]:
    """원본 stagea.models.flat_spans — 겹치지 않는 구간 중 점수 합이 가장 큰 조합."""
    import bisect
    cands = sorted(cands, key=lambda x: (x["end"], x["begin"], x["label"]))
    ends, scores, sols = [x["end"] for x in cands], [0.0], [[]]
    for k, c in enumerate(cands):
        p = bisect.bisect_right(ends, c["begin"], 0, k)
        if scores[p] + c["score"] > scores[-1]:
            scores.append(scores[p] + c["score"])
            sols.append(sols[p] + [c])
        else:
            scores.append(scores[-1])
            sols.append(sols[-1])
    return sorted(sols[-1], key=lambda x: (x["begin"], x["end"]))


def _mlp(width: int) -> nn.Sequential:
    """원본 masking.heads.make_mlp(width, [512, 256], 1, .15)."""
    return nn.Sequential(nn.Linear(width, 512), nn.GELU(), nn.LayerNorm(512), nn.Dropout(.15),
                         nn.Linear(512, 256), nn.GELU(), nn.LayerNorm(256), nn.Dropout(.15),
                         nn.Linear(256, 1))


class KeepScorer:
    def __init__(self, keep_dir: str | Path, tokenizer: Any, backbone_config_dir: str | Path, device: str = "cpu"):
        keep_dir = Path(keep_dir)
        cfg = json.loads((keep_dir / "keep_config.json").read_text(encoding="utf-8"))
        self.threshold = float(cfg["threshold"])
        self.max_length = int(cfg.get("max_length", 256))
        self.stride = int(cfg.get("stride", 64))
        self.tokenizer = tokenizer
        self.device = device
        self.gaz = _Gazetteer(json.loads((keep_dir / "train_gazetteer.json").read_text(encoding="utf-8")))

        arch = AutoConfig.from_pretrained(str(backbone_config_dir), trust_remote_code=True)
        if arch.model_type == "modernbert":
            arch.reference_compile = False
        self.backbone = AutoModel.from_config(arch, trust_remote_code=True)
        hidden = arch.hidden_size
        self.gaz_proj = nn.Sequential(nn.Linear(len(TYPES), 32), nn.GELU())
        self.classifier = nn.Linear(hidden + 32, BIO_SIZE)
        self.feature_dim = hidden + 32 + BIO_SIZE
        self.mask_head = _mlp(2 * self.feature_dim + len(TYPES) + len(DOMAINS))

        state = load_file(str(keep_dir / "keep_model.safetensors"))
        for module, prefix in ((self.backbone, "stage_a.backbone."), (self.gaz_proj, "stage_a.gazetteer_projection."),
                               (self.classifier, "stage_a.classifier."), (self.mask_head, "heads.mask.")):
            module.load_state_dict({k[len(prefix):]: v for k, v in state.items() if k.startswith(prefix)}, strict=True)
            module.to(device=device, dtype=torch.float32).eval()
        start_c, trans_c = _crf_constraints()
        self.crf = (state["stage_a.crf.start_transitions"].float() + start_c,
                    state["stage_a.crf.end_transitions"].float(),
                    state["stage_a.crf.transitions"].float() + trans_c)

    @torch.inference_mode()
    def _encode(self, sentences: list[tuple[Any, str]], decode: bool = False):
        """문장별 토큰 특징. 같은 (문장, 글자 구간) 토큰이 여러 창에 나오면 평균한다.

        decode=True 면 이 모델 탐지부의 개체도 돌려준다(원본 Integrated.encode(predict=True) + registry).
        """
        windows, keys, lookup = [], [], {}
        for sid, text in sentences:
            tok = self.tokenizer(text, truncation=True, max_length=self.max_length, stride=self.stride,
                                 return_overflowing_tokens=True, return_offsets_mapping=True)
            for ids, offsets in zip(tok["input_ids"], tok["offset_mapping"]):
                offsets = [tuple(x) for x in offsets]
                real = [i for i, (a, b) in enumerate(offsets) if b > a]
                if not real:
                    continue
                glob = []
                for i in real:
                    key = (sid, *offsets[i])
                    if key not in lookup:
                        lookup[key] = len(keys)
                        keys.append(key)
                    glob.append(lookup[key])
                windows.append((ids, real, glob, self.gaz.features(text, offsets), sid, offsets))
        if not keys:
            return [], None, []
        buckets: dict[Any, dict] = {}
        total = torch.zeros(len(keys), self.feature_dim, device=self.device)
        count = torch.zeros(len(keys), 1, device=self.device)
        pad = self.tokenizer.pad_token_id
        for start in range(0, len(windows), 8):
            batch = windows[start:start + 8]
            width = max(len(w[0]) for w in batch)
            ids = torch.full((len(batch), width), pad, dtype=torch.long, device=self.device)
            att = torch.zeros_like(ids)
            gaz = torch.zeros(len(batch), width, len(TYPES), device=self.device)
            for j, (wid, _, _, g, _, _) in enumerate(batch):
                ids[j, :len(wid)] = torch.tensor(wid, device=self.device)
                att[j, :len(wid)] = 1
                gaz[j, :len(wid)] = torch.tensor(g, device=self.device)
            h = self.backbone(input_ids=ids, attention_mask=att).last_hidden_state.float()
            feats = torch.cat((h, self.gaz_proj(gaz)), -1)
            logits = self.classifier(feats)
            joined = torch.cat((feats, logits.softmax(-1)), -1)
            for j, (_, real, glob, _, sid, offsets) in enumerate(batch):
                idx = torch.tensor(glob, device=self.device)
                total.index_add_(0, idx, joined[j, real])
                count.index_add_(0, idx, torch.ones(len(glob), 1, device=self.device))
                if not decode:
                    continue
                tags = [0] * len(offsets)
                for i, t in zip(real, _viterbi(logits[j, real].cpu(), *self.crf)):
                    tags[i] = t
                bucket = buckets.setdefault(sid, {})
                for s in _decode_bio(offsets, tags):
                    # 창 가장자리에서 먼 쪽을 믿는다(원본과 같은 점수)
                    cov = [i for i in real if offsets[i][0] < s["end"] and s["begin"] < offsets[i][1]]
                    score = 1 + min(cov[0] - real[0], real[-1] - cov[-1]) if cov else 1
                    key = (s["begin"], s["end"], s["label"])
                    if key not in bucket or score > bucket[key]["score"]:
                        bucket[key] = {**s, "score": score}
        entities: dict[tuple[str, str], dict] = {}
        texts = dict(sentences)
        for sid, bucket in buckets.items():
            for s in _flat_spans(list(bucket.values())):
                key = (s["label"], norm(texts[sid][s["begin"]:s["end"]]))
                ent = entities.setdefault(key, {"entity_type": s["label"], "mentions": []})
                m = {"sent_idx": sid, "begin": s["begin"], "end": s["end"]}
                if m not in ent["mentions"]:
                    ent["mentions"].append(m)
        return keys, total / count, list(entities.values())

    def score_entities(self, sentences: list[tuple[Any, str]], entities: list[dict], domains: list[str] | None = None):
        """주어진 개체(원본 registry 형식 [{entity_type, mentions:[{sent_idx, begin, end}]}])마다 {분야: MASK 확률}."""
        keys, h, _ = self._encode(sentences)
        return self._mask_probs(keys, h, entities, domains)

    def analyze(self, sentences: list[tuple[Any, str]], domains: list[str] | None = None):
        """이 모델 탐지부의 개체와 그 {분야: MASK 확률} — 학습·평가 때와 같은 조건."""
        keys, h, entities = self._encode(sentences, decode=True)
        return entities, self._mask_probs(keys, h, entities, domains)

    @torch.inference_mode()
    def _mask_probs(self, keys, h, entities: list[dict], domains: list[str] | None):
        """개체마다 {분야: MASK 확률}. 겹치는 토큰이 없는 개체는 None(=점수 없음, 가린다)."""
        if h is None or not entities:
            return [None] * len(entities)
        domains = domains or DOMAINS
        rows, slots = [], []
        for n, e in enumerate(entities):
            sel = [i for i, (sid, a, b) in enumerate(keys)
                   if any(sid == m["sent_idx"] and a < m["end"] and m["begin"] < b for m in e["mentions"])]
            if sel and e["entity_type"] in TYPES:
                rows.append((h[sel].mean(0), TYPES.index(e["entity_type"])))
                slots.append(n)
        out: list[dict | None] = [None] * len(entities)
        if not rows:
            return out
        v = torch.stack([r[0] for r in rows])
        types = nn.functional.one_hot(torch.tensor([r[1] for r in rows], device=self.device), len(TYPES)).float()
        doc = h.mean(0).expand(len(rows), -1)
        for d in domains:
            dom = nn.functional.one_hot(torch.tensor(DOMAINS.index(d), device=self.device), len(DOMAINS)).float()
            p = self.mask_head(torch.cat((v, doc, types, dom.expand(len(rows), -1)), -1)).squeeze(-1).sigmoid().tolist()
            for n, value in zip(slots, p):
                out[n] = {**(out[n] or {}), d: value}
        return out

    def score_text(self, text: str, entities: list[dict]) -> list[float | None]:
        """앱용: 원문 하나와 v2 개체([{begin, end, label}])를 받아 개체마다 MASK 확률(분야 6종 중 최댓값)을 준다.

        v2 개체와 같은 종류로 겹치는 이 모델 개체의 확률을 빌린다(여럿이면 가장 높은 값).
        짝이 없거나 모르는 라벨이면 None — 호출 쪽에서 가린다.
        """
        sentences = split_sentences(text)
        own, probs = self.analyze([(n, text[a:b]) for n, (a, b) in enumerate(sentences)])
        mentions = []  # (원문 시작, 원문 끝, 종류, 확률)
        for e, p in zip(own, probs):
            if p is None:
                continue
            for m in e["mentions"]:
                base = sentences[m["sent_idx"]][0]
                mentions.append((base + m["begin"], base + m["end"], e["entity_type"], max(p.values())))
        out: list[float | None] = []
        for e in entities:
            kind = APP_TO_TYPE.get(e["label"])
            hits = [p for a, b, t, p in mentions if t == kind and a < e["end"] and e["begin"] < b]
            out.append(max(hits) if hits else None)
        return out


def _load_tokenizer(model_dir: str):
    """PII 번들 토크나이저(같은 어휘·오프셋). 4.x 폴백은 local_pii_inference 와 같은 이유다."""
    from transformers import AutoTokenizer, PreTrainedTokenizerFast
    try:
        return AutoTokenizer.from_pretrained(model_dir, trust_remote_code=True, use_fast=True)
    except ValueError as exc:
        if "TokenizersBackend" not in str(exc):
            raise
        return PreTrainedTokenizerFast.from_pretrained(model_dir)


def main() -> None:
    """python keep_scorer.py --keep-dir <keep 모델> --pii-model-dir <PII 번들 seed 폴더> --stdio

    요청 한 줄: {"id", "text", "entities": [{begin, end, label}]} → {"id", "probs": [MASK 확률 | null], "threshold"}
    """
    import argparse
    import sys

    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-dir", required=True)
    ap.add_argument("--pii-model-dir", required=True)
    ap.add_argument("--device", default="auto")
    ap.add_argument("--stdio", action="store_true")
    args = ap.parse_args()
    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    scorer = KeepScorer(args.keep_dir, _load_tokenizer(args.pii_model_dir),
                        Path(args.pii_model_dir) / "backbone_config", device)
    print(json.dumps({"ready": True, "device": device}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            probs = scorer.score_text(req["text"], req["entities"])
            out = {"id": req_id, "probs": [None if p is None else round(p, 4) for p in probs],
                   "threshold": scorer.threshold}
        except Exception as e:  # noqa: BLE001 - 한 요청의 실패로 프로세스를 죽이지 않는다
            out = {"id": req_id, "error": f"{type(e).__name__}: {e}"}
        print(json.dumps(out, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
