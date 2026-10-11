"""
기밀 탐지 리트리버 — 문서가 KG 트리플(주어, 관계, 목적어)을 말하는지 채점하는 로컬 모델.

구조 (실험 기록: 문서/리트리버/README.md, docre_docpair.py 의 추론부만 옮김):
  A.X-Encoder 로 문서를 한 번 읽음 → 엔티티 = 언급 앞 * 위치 벡터들의 logsumexp
  → 쌍마다 e_h ⊙ e_t 로 문서 토큰에 주의를 줘 문맥 c → z = tanh(W[e; c]) → 그룹 쌍선형 → p
  → 관계 이름(KG 술어 텍스트)을 같은 인코더로 읽은 r_k 와 내적. 0번 = "no relation" 기준점(TH)
  여유값 s_k − s_TH 가 클수록 "문서가 그 사실을 말한다".

엔진(app/core/detectors/confidential/retriever.py)과 JSONL 표준입출력으로 이야기한다:
  준비:  {"ready": true, "device": "cuda"}
  요청:  {"id", "text", "entities": [[[시작, 끝], ...], ...], "pairs": [[h, t, [술어번호...]], ...],
          "predicates": [술어 텍스트...], "sentences": [[시작, 끝], ...]}      (좌표는 text 기준)
  응답:  {"id", "pairs": [{"margins": [...] | null, "sent_att": [...] | null}, ...]}
         엔티티가 잘려(최대 길이 초과) 표현이 없으면 margins/sent_att 는 null.

    python local_retriever_inference.py --model-dir <폴더> --device auto --stdio
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
from pathlib import Path

import torch
import torch.nn as nn
from transformers import AutoConfig, AutoModel, AutoTokenizer

TH_TEXT = "no relation"


class DocPair(nn.Module):
    """학습 때와 같은 층 이름 — 저장된 state_dict 를 그대로 읽는다."""

    def __init__(self, enc, hidden: int, emb: int = 768, block: int = 64) -> None:
        super().__init__()
        self.enc = enc
        self.hidden, self.emb, self.block = hidden, emb, block
        self.head_ext = nn.Linear(2 * hidden, emb)
        self.tail_ext = nn.Linear(2 * hidden, emb)
        self.ctx = nn.Linear(hidden, hidden)
        self.bil = nn.Linear(emb * block, hidden)
        self.rel_proj = nn.Linear(hidden, hidden)

    def rel_vectors(self, ids, att):
        H = self.enc(input_ids=ids, attention_mask=att).last_hidden_state
        m = att.unsqueeze(-1).to(H.dtype)
        return self.rel_proj((H * m).sum(1) / m.sum(1))

    def pair_scores(self, H, ent_pos, hts, R):
        E = torch.stack([torch.logsumexp(H[p], 0) for p in ent_pos])
        ht = torch.tensor(hts, device=H.device)
        eh, et = E[ht[:, 0]], E[ht[:, 1]]
        a = ((self.ctx(eh * et) @ H.T) / math.sqrt(self.hidden)).softmax(-1)
        c = a @ H
        zh = torch.tanh(self.head_ext(torch.cat([eh, c], -1)))
        zt = torch.tanh(self.tail_ext(torch.cat([et, c], -1)))
        k = self.emb // self.block
        bl = (zh.view(-1, k, self.block, 1) * zt.view(-1, k, 1, self.block)).reshape(-1, self.emb * self.block)
        with torch.autocast(H.device.type, enabled=False):  # 마지막 점수는 fp32 (반올림되면 임계 조정이 안 됨)
            sc = self.bil(bl.float()) @ R.float().T
        return sc, a


def _resolve_device(name: str) -> str:
    if name != "auto":
        return name
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def mark_mentions(text: str, entities: list[list[list[int]]]) -> tuple[str, list[tuple[int, int]], list[int]]:
    """언급 앞뒤에 ' * ' 를 넣는다 (학습 때 * 는 띄어쓰기로 구분된 별도 단어였다).

    반환: 표시된 텍스트, (엔티티, 앞 * 의 글자 위치) 목록, 표시 텍스트 글자 → 원문 글자 위치.
    """
    opens: dict[int, list[int]] = {}
    closes: dict[int, int] = {}
    for ei, spans in enumerate(entities):
        for s, e in spans:
            opens.setdefault(int(s), []).append(ei)
            closes[int(e)] = closes.get(int(e), 0) + 1
    out: list[str] = []
    orig: list[int] = []
    stars: list[tuple[int, int]] = []

    def put(piece: str, src: int) -> None:
        out.append(piece)
        orig.extend([src] * len(piece))

    for i in range(len(text) + 1):
        src = min(i, len(text) - 1) if text else 0
        for _ in range(closes.get(i, 0)):
            put(" * ", max(i - 1, 0))
        for ei in opens.get(i, []):
            stars.append((ei, len(orig) + 1))
            put(" * ", src)
        if i < len(text):
            put(text[i], i)
    return "".join(out), stars, orig


class Retriever:
    def __init__(self, model_dir: Path, device: str, max_length: int) -> None:
        self.device = _resolve_device(device)
        self.max_length = max_length
        try:
            self.tok = AutoTokenizer.from_pretrained(model_dir)
        except Exception:  # noqa: BLE001 - 인텔 맥(transformers 4.x) 등에서 auto 매핑 실패 시
            from transformers import PreTrainedTokenizerFast
            self.tok = PreTrainedTokenizerFast(tokenizer_file=str(model_dir / "tokenizer.json"))
        cfg = AutoConfig.from_pretrained(model_dir)
        model = DocPair(AutoModel.from_config(cfg), cfg.hidden_size)
        from safetensors.torch import load_file
        model.load_state_dict(load_file(str(model_dir / "model.safetensors")))
        dtype = torch.float16 if self.device == "cuda" else torch.float32
        self.model = model.to(self.device, dtype=dtype).eval()
        self._rel_cache: dict[str, torch.Tensor] = {}

    @torch.no_grad()
    def _relations(self, names: list[str]) -> torch.Tensor:
        todo = [n for n in dict.fromkeys([TH_TEXT] + names) if n not in self._rel_cache]
        if todo:
            enc = self.tok(todo, return_tensors="pt", padding=True)
            vecs = self.model.rel_vectors(enc["input_ids"].to(self.device), enc["attention_mask"].to(self.device))
            self._rel_cache.update(zip(todo, vecs))
        return torch.stack([self._rel_cache[n] for n in [TH_TEXT] + names])

    @torch.no_grad()
    def score(self, req: dict) -> list[dict]:
        text = req["text"]
        sentences = [tuple(x) for x in req.get("sentences") or [[0, len(text)]]]
        marked, stars, orig = mark_mentions(text, req["entities"])
        enc = self.tok(marked, truncation=True, max_length=self.max_length, return_offsets_mapping=True)
        offs = enc["offset_mapping"]
        tok_starts = [s for s, _ in offs]
        n_tok = len(enc["input_ids"])
        ent_pos: list[list[int]] = [[] for _ in req["entities"]]
        for ei, c in stars:
            k = bisect.bisect_right(tok_starts, c) - 1
            if 0 < k < n_tok - 1 and offs[k][0] <= c < max(offs[k][1], c + 1):
                ent_pos[ei].append(k)
        sent_starts = [s for s, _ in sentences]
        tok_sent = [-1 if k == 0 or k == n_tok - 1 or s >= len(orig) else bisect.bisect_right(sent_starts, orig[s]) - 1
                    for k, (s, _e) in enumerate(offs)]

        pairs = req["pairs"]
        ok = [i for i, (h, t, _) in enumerate(pairs) if ent_pos[h] and ent_pos[t]]
        results: list[dict] = [{"margins": None, "sent_att": None} for _ in pairs]
        if not ok:
            return results
        R = self._relations(list(req["predicates"]))
        ids = torch.tensor([enc["input_ids"]], device=self.device)
        H = self.model.enc(input_ids=ids, attention_mask=torch.ones_like(ids)).last_hidden_state[0]
        sc, a = self.model.pair_scores(H, [sorted(set(p)) or [0] for p in ent_pos],
                                       [(pairs[i][0], pairs[i][1]) for i in ok], R)
        M = torch.zeros(n_tok, len(sentences), device=self.device)
        for k, si in enumerate(tok_sent):
            if si >= 0:
                M[k, si] = 1
        sa = a.float() @ M
        sa = sa / sa.sum(-1, keepdim=True).clamp_min(1e-10)
        for row, i in enumerate(ok):
            th = sc[row, 0]
            results[i] = {
                "margins": [round(float(sc[row, int(p) + 1] - th), 4) for p in pairs[i][2]],
                "sent_att": [round(float(x), 4) for x in sa[row]],
            }
        return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--device", default="auto")
    ap.add_argument("--max-length", type=int, default=1024)
    ap.add_argument("--stdio", action="store_true")
    args = ap.parse_args()
    retriever = Retriever(Path(args.model_dir), args.device, args.max_length)
    print(json.dumps({"ready": True, "device": retriever.device}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            out = {"id": req_id, "pairs": retriever.score(req)}
        except Exception as e:  # noqa: BLE001 - 한 요청의 실패로 프로세스를 죽이지 않는다
            out = {"id": req_id, "error": f"{type(e).__name__}: {e}"}
        print(json.dumps(out, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
