"""문맥 판단 PII 모델 — PII 를 찾고, 개체마다 "가려야 하는가(MASK)" 확률을 매긴다.

taehwan 의 joint_masking direct_full(ax_gazetteer_crf seed42) 추론부를 그대로 옮겼다(탐지기 포함).
원본: /data/upsecurity00/members/taehwan/joint_masking/network.py (Integrated.encode(predict=True) / contextual).
- 문장마다 토큰화(256 토큰, stride 64) → 인코더 특징 + 사전 특징 + 탐지 확률(evidence)을 토큰별로 평균
  → CRF 로 개체를 찾고(창 가장자리에서 먼 쪽을 믿음), 같은 종류·같은 글자끼리 묶는다(registry)
  → 개체 평균 + 문서 평균 + 종류·분야 one-hot → MLP → sigmoid.
- MLP 는 경계에 민감하다. 다른 탐지기(앱 v2, 조사까지 잡음)의 개체를 넣으면 '시디즈' 1.0 →
  '시디즈의' 0.003 처럼 뒤집혀 가려야 할 개체가 풀렸다 — 이 모델이 찾은 개체에만 점수를 매긴다.
- 학습 데이터는 분야 6종 중 하나를 알려 줬지만 앱은 분야를 모른다. 6종 모두 넣어 보고 가장 높은(가장 가리자는)
  확률을 쓴다 — 어느 분야로 보든 KEEP 일 때만 KEEP 이 된다.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np
import torch
import transformers
from torch import nn
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
TYPE_TO_APP = {v: k for k, v in APP_TO_TYPE.items()}
DOMAINS =["career_education", "contract", "financial", "legal", "medical", "support"]
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


def _viterbi(em: torch.Tensor, mask: torch.Tensor, start: torch.Tensor, end: torch.Tensor,
             trans: torch.Tensor) -> torch.Tensor:
    """묶음 전체의 최적 BIO 경로 [B, T] — 원본 stagea.crf.viterbi 와 같은 계산을 numpy 로. mask 밖은 0.

    토큰마다 아주 작은 연산(묶음×39×39)을 이어 하는 구조라 GPU 는 연산을 띄우는 고정 비용만 쌓인다.
    16창×60토큰 실측: torch-cuda 9.6ms, torch-cpu 4.2ms, numpy 2.6ms(세 결과 같음). 예전엔 창마다 따로 돌렸다.
    """
    em, mask, start, end, trans = (x.detach().cpu().numpy() for x in (em, mask, start, end, trans))
    b, n, k = em.shape
    score = start[None, :] + em[:, 0]
    history = np.zeros((n, b, k), dtype=np.int64)
    for t in range(1, n):
        cand = score[:, :, None] + trans[None, :, :]
        index = cand.argmax(axis=1)
        nxt = np.take_along_axis(cand, index[:, None, :], axis=1)[:, 0, :] + em[:, t]
        score = np.where(mask[:, t, None], nxt, score)
        history[t] = index
    current = (score + end[None, :]).argmax(axis=1)
    lengths = mask.sum(1)
    paths = np.zeros((b, n), dtype=np.int64)
    rows = np.arange(b)
    for t in range(n - 1, -1, -1):
        valid = t < lengths
        paths[:, t] = np.where(valid, current, 0)
        if t > 0:
            current = np.where(valid, history[t][rows, current], current)
    return torch.from_numpy(paths)


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


def _no_init_weights():
    """transformers 의 무작위 초기화 끄기 — 5.x 는 initialization, 4.x(Intel mac) 는 modeling_utils 에 있다."""
    import contextlib
    for module in ("transformers.initialization", "transformers.modeling_utils"):
        try:
            return getattr(__import__(module, fromlist=["no_init_weights"]), "no_init_weights")()
        except (ImportError, AttributeError):
            continue
    return contextlib.nullcontext()


_CRF_PREFIX = "stage_a.crf."


_ST_DTYPES = {"F32": torch.float32, "F16": torch.float16, "BF16": torch.bfloat16}


@torch.no_grad()
def _load_weights(path: Path, modules) -> dict[str, torch.Tensor]:
    """가중치를 텐서 하나씩 읽어 바로 모듈에 복사한다(load_state_dict(strict=True) 와 같은 검사).

    기동 순간 메모리 최고점을 v2 수준으로 — 파일 전체(약 600MB)를 한 번에 읽으면 막 만든 모델과 합쳐 두 벌이
    올라갔다(최고점 1.94GB, v2 1.52GB). safetensors.safe_open 도 메모리 매핑이라 읽은 페이지가 프로세스에 잡혀
    마찬가지였다. 그래서 형식(8바이트 헤더 길이 + JSON 헤더 + 원시 데이터)을 직접 읽어 텐서 하나씩 복사한다 —
    더 드는 건 가장 큰 텐서(임베딩 약 150MB) 하나뿐이다. CRF 전이(모듈 밖)는 따로 돌려준다.
    """
    targets = {prefix + name: t for module, prefix in modules for name, t in module.state_dict().items()}
    with open(path, "rb") as f:
        header = json.loads(f.read(int.from_bytes(f.read(8), "little")))
        header.pop("__metadata__", None)
        base = f.tell()
        crf_keys = {k for k in header if k.startswith(_CRF_PREFIX)}
        missing, unexpected = set(targets) - set(header), set(header) - set(targets) - crf_keys
        if missing or unexpected:
            raise RuntimeError(f"keep 가중치 키 불일치: 없음 {sorted(missing)[:5]}, 남음 {sorted(unexpected)[:5]}")

        def read(key: str) -> torch.Tensor:
            meta = header[key]
            begin, end = meta["data_offsets"]
            f.seek(base + begin)
            buf = bytearray(f.read(end - begin))
            return torch.frombuffer(buf, dtype=_ST_DTYPES[meta["dtype"]]).reshape(meta["shape"])

        for k, t in targets.items():
            meta = header[k]
            if list(t.shape) != meta["shape"]:
                raise RuntimeError(f"keep 가중치 모양 불일치: {k} {meta['shape']} != {list(t.shape)}")
            if _ST_DTYPES[meta["dtype"]] == t.dtype and t.is_contiguous() and t.device.type == "cpu":
                # 모듈 텐서 메모리로 바로 읽는다 — 중간 버퍼 없음
                begin, end = meta["data_offsets"]
                f.seek(base + begin)
                if f.readinto(memoryview(t.numpy()).cast("B")) != end - begin:
                    raise RuntimeError(f"keep 가중치 파일이 잘렸다: {k}")
            else:
                t.copy_(read(k))
        return {k[len(_CRF_PREFIX):]: read(k).float().clone() for k in crf_keys}


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
        # 한 번에 넣는 창 수(원본 8). 실측(2만 자, 길이순): CPU 는 8 이 가장 빠르고(16·32·64 는 더 느리고 메모리만 늚),
        # CUDA(RTX 5070)는 16 이 줄바꿈 있는 글·없는 글 모두 고르게 빨랐다(32 는 한쪽만 빠르고 메모리 2배).
        self.batch = 16 if str(device).startswith("cuda") else 8
        self.gaz = _Gazetteer(json.loads((keep_dir / "train_gazetteer.json").read_text(encoding="utf-8")))

        arch = AutoConfig.from_pretrained(str(backbone_config_dir), trust_remote_code=True)
        if arch.model_type == "modernbert":
            arch.reference_compile = False
        # 백본 설정에 dtype bfloat16 이 적혀 있어 transformers 5.20 의 from_config 는 bf16 으로 만든다(5.14 는 fp32).
        # 그대로 fp32 가중치를 복사하면 bf16 으로 반올림돼 원본과 달라진다 — fp32 로 만든다(4.x 는 torch_dtype).
        setattr(arch, "dtype" if int(transformers.__version__.split(".")[0]) >= 5 else "torch_dtype", torch.float32)
        # 무작위 초기화는 건너뛴다 — 곧 덮어쓰고, 초기화하는 동안 잠깐 약 500MB 를 더 썼다(기동 최고점).
        with _no_init_weights():
            self.backbone = AutoModel.from_config(arch, trust_remote_code=True)
        if next(self.backbone.parameters()).dtype != torch.float32:
            self.backbone = self.backbone.float()
        hidden = arch.hidden_size
        self.gaz_proj = nn.Sequential(nn.Linear(len(TYPES), 32), nn.GELU())
        self.classifier = nn.Linear(hidden + 32, BIO_SIZE)
        self.feature_dim = hidden + 32 + BIO_SIZE
        self.mask_head = _mlp(2 * self.feature_dim + len(TYPES) + len(DOMAINS))

        modules = ((self.backbone, "stage_a.backbone."), (self.gaz_proj, "stage_a.gazetteer_projection."),
                   (self.classifier, "stage_a.classifier."), (self.mask_head, "heads.mask."))
        crf = _load_weights(keep_dir / "keep_model.safetensors", modules)
        for module, _ in modules:
            module.to(device=device, dtype=torch.float32).eval()
        start_c, trans_c = _crf_constraints()
        self.crf = (crf["start_transitions"] + start_c, crf["end_transitions"], crf["transitions"] + trans_c)  # CPU(_viterbi)

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
        # 길이순으로 묶는다 — 문장 길이가 제각각이라 순서대로 묶으면 짧은 문장이 긴 문장 길이만큼 패딩된다.
        # 결과는 토큰 자리별로 모으므로 순서와 무관하다. 묶음 크기는 __init__ 의 실측 참고.
        windows.sort(key=lambda w: len(w[0]))
        for start in range(0, len(windows), self.batch):
            batch = windows[start:start + self.batch]
            # 묶음 입력은 CPU 에서 다 만든 뒤 한 번에 보낸다 — 창마다 보내면 GPU 에서 그 전송이 가장 큰 단일 비용이었다
            width = max(len(w[0]) for w in batch)
            ids = np.full((len(batch), width), pad, dtype=np.int64)
            att = np.zeros((len(batch), width), dtype=np.int64)
            gaz = np.zeros((len(batch), width, len(TYPES)), dtype=np.float32)
            rows, cols, slot, glob = [], [], [], []  # 실제 토큰: 묶음 안 (행, 열), 앞으로 모은 자리, 문서 토큰 번호
            for j, (wid, real, g_idx, g, _, _) in enumerate(batch):
                ids[j, :len(wid)] = wid
                att[j, :len(wid)] = 1
                gaz[j, :len(wid)] = g
                rows += [j] * len(real)
                cols += real
                slot += range(len(real))
                glob += g_idx
            h = self.backbone(input_ids=torch.from_numpy(ids).to(self.device),
                              attention_mask=torch.from_numpy(att).to(self.device)).last_hidden_state.float()
            feats = torch.cat((h, self.gaz_proj(torch.from_numpy(gaz).to(self.device))), -1)
            logits = self.classifier(feats)
            joined = torch.cat((feats, logits.softmax(-1)), -1)
            r = torch.tensor(rows, device=self.device)
            c = torch.tensor(cols, device=self.device)
            idx = torch.tensor(glob, device=self.device)
            total.index_add_(0, idx, joined[r, c])
            count.index_add_(0, idx, torch.ones(len(glob), 1, device=self.device))
            if not decode:
                continue
            # 실제 토큰만 앞으로 모아 묶음 전체를 한 번에 디코딩한다(원본은 창마다 실제 토큰만 넣었다)
            longest = max(len(w[1]) for w in batch)
            s = torch.tensor(slot, device=self.device)
            em = torch.zeros(len(batch), longest, BIO_SIZE, device=self.device)
            mask = torch.zeros(len(batch), longest, dtype=torch.bool, device=self.device)
            em[r, s] = logits[r, c]
            mask[r, s] = True
            paths = _viterbi(em, mask, *self.crf).tolist()
            for j, (_, real, _, _, sid, offsets) in enumerate(batch):
                tags = [0] * len(offsets)
                for i, t in zip(real, paths[j]):
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
        by_sent: dict[Any, list[tuple[int, int, int]]] = {}  # 문장별 토큰만 본다 — 예전엔 개체마다 문서의 모든 토큰을 훑었다
        for i, (sid, a, b) in enumerate(keys):
            by_sent.setdefault(sid, []).append((a, b, i))
        rows, slots = [], []
        for n, e in enumerate(entities):
            sel = sorted({i for m in e["mentions"] for a, b, i in by_sent.get(m["sent_idx"], ())
                          if a < m["end"] and m["begin"] < b})
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

    def detect_text(self, text: str) -> list[dict]:
        """앱용: 원문 하나 → 언급마다 {begin, end, label(앱 v2 라벨), maskProb(분야 6종 중 최댓값 | None)}."""
        sentences = split_sentences(text)
        own, probs = self.analyze([(n, text[a:b]) for n, (a, b) in enumerate(sentences)])
        out = []
        for e, p in zip(own, probs):
            for m in e["mentions"]:
                base = sentences[m["sent_idx"]][0]
                out.append({"begin": base + m["begin"], "end": base + m["end"], "label": TYPE_TO_APP[e["entity_type"]],
                            "maskProb": None if p is None else round(max(p.values()), 4)})
        return sorted(out, key=lambda x: (x["begin"], x["end"]))


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

    요청 한 줄: {"id", "text"} 또는 {"id", "texts": [...]}
    → {"id", "entities": [[{begin, end, label, maskProb}]...], "threshold"}  (texts 순서대로)
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
            texts = req["texts"] if "texts" in req else [req["text"]]
            out = {"id": req_id, "entities": [scorer.detect_text(t) for t in texts], "threshold": scorer.threshold}
        except Exception as e:  # noqa: BLE001 - 한 요청의 실패로 프로세스를 죽이지 않는다
            out = {"id": req_id, "error": f"{type(e).__name__}: {e}"}
        print(json.dumps(out, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
