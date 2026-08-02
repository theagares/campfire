#!/usr/bin/env python3
"""Local prompt-injection detector runtime for the EXAONE-4.0-1.2B hybrid
(attention token-pair + segment hidden-state) regularized MLP detector.

Reproduces the exact feature-extraction/training protocol from ho's
injection_diag project (src/diag_common.py, src/extract_hybrid.py,
src/train_hybrid.py) so the bundled model.pt/norm_stats.pt/calibration.json
under ../attn/ and ../hybrid/ can be used for single-request inference.

JSONL input for --stdio:
  {"id":"1","system_prompt":"...","user_prompt":"...","tool_response":"..."}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn as nn


ID_TO_LABEL = {0: "misaligned", 1: "aligned", 2: "non_instruction"}
MIS = 0
TOOL_RESPONSE_TEMPLATE = "<tool_response>\n{content}\n</tool_response>"


def build_messages(record: dict[str, str]) -> list[dict[str, str]]:
    """tool_msg_mode="separate" (injection_diag/src/diag_common.py::build_messages).

    system 메시지는 렌더링에만 쓰고, user_prompt/tool_response 스팬 탐색에는 system
    을 배제한다(diag_common.py 주석 그대로 — user<->tool 상호작용만 특징으로 씀).
    """
    wrapped = TOOL_RESPONSE_TEMPLATE.format(content=record["tool_response"])
    return [
        {"role": "system", "content": record["system_prompt"]},
        {"role": "user", "content": record["user_prompt"]},
        {"role": "user", "content": wrapped},
    ]


def render_chat(tokenizer, messages: list[dict[str, str]]) -> str:
    try:
        return tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=False, enable_thinking=False
        )
    except TypeError:
        return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)


def locate_spans(text: str, user_prompt: str, tool_response: str) -> tuple[tuple[int, int], tuple[int, int]]:
    u_start = text.find(user_prompt)
    if u_start < 0:
        raise ValueError("user_prompt was not found in the rendered chat template.")
    u_end = u_start + len(user_prompt)
    x_start = text.find(tool_response, u_end)
    if x_start < 0:
        raise ValueError("tool_response was not found in the rendered chat template.")
    return (u_start, u_end), (x_start, x_start + len(tool_response))


def char_span_to_token_indices(offsets: list[tuple[int, int]], start: int, end: int) -> list[int]:
    indices = []
    for idx, (left, right) in enumerate(offsets):
        if left == right:
            continue
        if left < end and right > start:
            indices.append(idx)
    return indices


def stable_seed(record: dict[str, str], base_seed: int) -> int:
    payload = "\n".join([record["system_prompt"], record["user_prompt"], record["tool_response"]])
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return (int.from_bytes(digest[:8], "big") ^ int(base_seed)) % (2**32)


# ── 모델 구조 (injection_diag/src/train_hybrid.py::build_encoder/build_classifier/build_model 그대로) ──
def build_encoder(d: int, dropout: float) -> nn.Sequential:
    return nn.Sequential(
        nn.LayerNorm(d), nn.Linear(d, 256), nn.GELU(), nn.Dropout(dropout),
        nn.Linear(256, 128), nn.GELU(), nn.Dropout(dropout),
    )


def build_classifier(d: int, dropout: float) -> nn.Sequential:
    return nn.Sequential(
        nn.LayerNorm(d), nn.Linear(d, 128), nn.GELU(), nn.Dropout(dropout), nn.Linear(128, 3),
    )


class AttnOnly(nn.Module):
    def __init__(self, lh: int, dropout: float):
        super().__init__()
        self.enc = build_encoder(lh, dropout)
        self.clf = build_classifier(128, dropout)

    def forward(self, pairs: torch.Tensor, hidden: torch.Tensor) -> torch.Tensor:
        h = self.enc(pairs)              # (B,K,128)
        return self.clf(h.mean(dim=1))   # (B,3)


class Hybrid(nn.Module):
    def __init__(self, lh: int, hd: int, dropout: float):
        super().__init__()
        self.attn_enc = build_encoder(lh, dropout)
        self.hidden_enc = build_encoder(hd, dropout)
        self.clf = build_classifier(256, dropout)

    def forward(self, pairs: torch.Tensor, hidden: torch.Tensor) -> torch.Tensor:
        a = self.attn_enc(pairs).mean(dim=1)  # (B,128)
        h = self.hidden_enc(hidden)           # (B,128)
        return self.clf(torch.cat([a, h], dim=1))


class InjectionDetector:
    def __init__(
        self,
        engine_dir: str | Path,
        variant: str = "hybrid",
        device: str = "auto",
        dtype: str = "bfloat16",
        max_seq_len: int = 4096,
        trust_remote_code: bool = True,
        sampling: str = "stable",
        dropout: float = 0.2,
        quantization_config: Any | None = None,
    ):
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.engine_dir = Path(engine_dir)
        self.variant = variant
        self.device = self._resolve_device(device)
        self.max_seq_len = max_seq_len
        self.sampling = sampling
        self.seed = 42

        cfg = json.loads((self.engine_dir / "extract_config.json").read_text(encoding="utf-8"))
        self.backend_model = cfg["model"]
        self.hs_layer_idxs: list[int] = cfg["hs_layer_idxs"]
        self.hs_pools: list[str] = cfg["hs_pools"]  # ["seg0","seg1","seg2","last"]
        self.hs_cols: list[str] = cfg["hs_cols"]
        self.n_seg = len([p for p in self.hs_pools if p != "last"])
        self.lh = int(cfg["lh"])
        self.hd = int(cfg["hd"])
        self.max_pairs = int(cfg["max_pairs"])

        variant_dir = self.engine_dir / variant
        norm = torch.load(variant_dir / "norm_stats.pt", map_location="cpu", weights_only=False)
        self.pair_mu = norm["pair_mu"].to(torch.float32)
        self.pair_sd = norm["pair_sd"].to(torch.float32).clamp_min(1e-8)
        self.hidden_mu = norm["hidden_mu"].to(torch.float32)
        self.hidden_sd = norm["hidden_sd"].to(torch.float32).clamp_min(1e-8)

        calib = json.loads((variant_dir / "calibration.json").read_text(encoding="utf-8"))
        self.misaligned_bias = float(calib["misaligned_bias"])

        state = torch.load(variant_dir / "model.pt", map_location="cpu", weights_only=False)
        if variant == "attn":
            self.detector: nn.Module = AttnOnly(self.lh, dropout)
        elif variant == "hybrid":
            self.detector = Hybrid(self.lh, self.hd, dropout)
        else:
            raise ValueError(f"unknown variant: {variant!r} (expected 'attn' or 'hybrid')")
        self.detector.load_state_dict(state)
        self.detector.to(self.device)
        self.detector.eval()

        torch_dtype = self._resolve_dtype(dtype, self.device)
        self.tokenizer = AutoTokenizer.from_pretrained(self.backend_model, trust_remote_code=trust_remote_code)
        backend_kwargs: dict[str, Any] = dict(
            dtype=torch_dtype,
            attn_implementation="eager",
            trust_remote_code=trust_remote_code,
            # Windows 에서 device_map="auto" 의 safetensors device-mapped fast-load 경로가
            # 페이지 파일 크기에 따라 "OS error 1455"로 실패하는 경우가 있어(injection
            # EXAONE-3.5-2.4B 연결 때 실측), CPU 전체 로드 후 .to(device) 하는 경로를 쓴다.
            low_cpu_mem_usage=False,
        )
        if quantization_config is not None:
            # bitsandbytes 양자화 로드는 CPU 풀로드 후 .to(device) 경로와 호환되지 않고
            # 로드 시점에 device_map 으로 바로 배치해야 한다 — 정확도 비교(§ eval_quantization
            # _drift.py)용 경로에서만 쓰이며 기본 운영 경로(quantization_config=None)는 그대로다.
            backend_kwargs["quantization_config"] = quantization_config
            backend_kwargs["low_cpu_mem_usage"] = True
            backend_kwargs["device_map"] = {"": self.device}
        self.backend = AutoModelForCausalLM.from_pretrained(self.backend_model, **backend_kwargs)
        if quantization_config is None:
            try:
                self.backend.to(self.device)
            except Exception:
                # MPS 로 올리다 실패하면(지원 안 되는 연산/dtype, 메모리 등) CPU 로 되돌린다.
                # 이 런타임은 Apple Silicon 실기기에서 검증하지 못했으므로, 가속을 못 받는
                # 것보다 아예 안 뜨는 게 훨씬 나쁘다 — 느려도 동작하는 쪽을 택한다.
                # (CUDA 는 원래 되던 경로라 폴백 대상에서 제외한다.)
                if self.device != "mps":
                    raise
                self.device = "cpu"
                self.detector.to("cpu")
                self.backend = AutoModelForCausalLM.from_pretrained(
                    self.backend_model, **{**backend_kwargs, "dtype": getattr(torch, dtype)}
                )
                self.backend.to("cpu")
        self.backend.eval()

    @staticmethod
    def _resolve_device(device: str) -> str:
        if device != "auto":
            return device
        if torch.cuda.is_available():
            return "cuda"
        # Apple Silicon: PII 런타임(local_pii_inference.py)과 같은 순서로 MPS 를 본다.
        # 예전엔 이 분기가 없어 arm64 Mac 에서 1.2B 백본이 통째로 CPU 로 돌았다 —
        # 정작 가벼운 PII 모델만 MPS 가속을 받고 무거운 쪽이 못 받는 비대칭이었다.
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    @staticmethod
    def _resolve_dtype(dtype: str, device: str):
        """MPS 에서 안전한 dtype 으로 낮춘다.

        bfloat16 은 MPS 에서 macOS 14 이전이거나 일부 연산에서 지원되지 않아, 그대로
        쓰면 로드나 첫 forward 에서 죽는다. MPS 에서 bf16 을 못 쓰면 float16 으로
        내린다(둘 다 16비트라 메모리/대역폭 이득은 같다).
        """
        resolved = getattr(torch, dtype)
        if device != "mps" or resolved is not torch.bfloat16:
            return resolved
        try:
            torch.zeros(1, dtype=torch.bfloat16, device="mps") + 1
            return torch.bfloat16
        except Exception:
            return torch.float16

    @torch.inference_mode()
    def extract_feature(self, record: dict[str, str]) -> tuple[torch.Tensor, torch.Tensor]:
        messages = build_messages(record)
        text = render_chat(self.tokenizer, messages)
        (u_a, u_b), (x_a, x_b) = locate_spans(text, record["user_prompt"], record["tool_response"])
        encoded = self.tokenizer(
            text, return_offsets_mapping=True, return_tensors="pt",
            truncation=True, max_length=self.max_seq_len, add_special_tokens=False,
        )
        offsets = encoded.pop("offset_mapping")[0].tolist()
        s_idx = char_span_to_token_indices(offsets, u_a, u_b)   # user_prompt tokens (keys)
        x_idx = char_span_to_token_indices(offsets, x_a, x_b)   # tool_response tokens (queries)
        if not s_idx or not x_idx:
            raise ValueError("Could not locate non-empty user/tool token spans after tokenization.")

        encoded = {k: v.to(self.device) for k, v in encoded.items()}
        outputs = self.backend(**encoded, output_attentions=True, output_hidden_states=True, use_cache=False)

        x_t = torch.tensor(x_idx, device=self.device)
        s_t = torch.tensor(s_idx, device=self.device)

        # ---- attention 토큰쌍 (extract_hybrid.py 와 동일: tool_response(query) -> user_prompt(key)) ----
        blocks = []
        for att in outputs.attentions:
            blk = att[0].index_select(1, x_t).index_select(2, s_t)  # (H,|x|,|s|)
            blocks.append(blk.float())
        A = torch.stack(blocks, dim=0)          # (L,H,|x|,|s|)
        Z = A.permute(2, 3, 0, 1).reshape(-1, self.lh)  # (P, LH)
        P = Z.shape[0]
        K = self.max_pairs
        if self.sampling == "stable":
            rng = np.random.default_rng(stable_seed(record, self.seed))
        else:
            rng = np.random.default_rng()
        sel = rng.choice(P, size=K, replace=(P < K))
        pairs = Z[torch.tensor(np.sort(sel), device=Z.device)]  # (K, LH)

        # ---- hidden state 3구간 평균 + 마지막 토큰(전체 시퀀스 기준) 풀링 ----
        n_x = x_t.shape[0]
        seg_bounds = torch.linspace(0, n_x, self.n_seg + 1).round().long()
        hs_vecs = [None] * len(self.hs_cols)
        for li in self.hs_layer_idxs:
            h = outputs.hidden_states[li][0]       # (T, hidden)
            x_h = h.index_select(0, x_t)           # (n_x, hidden) — tool_response 토큰만
            for s in range(self.n_seg):
                a, b = seg_bounds[s].item(), max(seg_bounds[s + 1].item(), seg_bounds[s].item() + 1)
                col = self.hs_cols.index(f"L{li}_seg{s}")
                hs_vecs[col] = x_h[a:b].mean(0).float()
            col_last = self.hs_cols.index(f"L{li}_last")
            hs_vecs[col_last] = h[-1].float()      # 시퀀스 전체의 마지막 토큰(diag_common 그대로)
        hidden = torch.cat(hs_vecs, dim=0)         # (S*hidden,) = (hd,)

        pairs = (pairs.to(self.device) - self.pair_mu.to(self.device)) / self.pair_sd.to(self.device)
        hidden = (hidden.to(self.device) - self.hidden_mu.to(self.device)) / self.hidden_sd.to(self.device)
        return pairs, hidden

    @torch.inference_mode()
    def predict_one(self, record: dict[str, str]) -> dict[str, Any]:
        pairs, hidden = self.extract_feature(record)
        logits = self.detector(pairs.unsqueeze(0), hidden.unsqueeze(0))[0]  # (3,)
        logits = logits.clone()
        logits[MIS] += self.misaligned_bias
        probs = torch.softmax(logits, dim=-1).cpu().numpy()
        pred_id = int(probs.argmax())
        return {
            "label": ID_TO_LABEL[pred_id],
            "is_injection": pred_id == MIS,
            "scores": {ID_TO_LABEL[i]: float(probs[i]) for i in range(3)},
            "backend_model": self.backend_model,
            "detector": {"variant": self.variant, "misaligned_bias": self.misaligned_bias, "sampling": self.sampling},
        }


def write_response(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run_stdio(detector: InjectionDetector) -> None:
    write_response({
        "ready": True,
        "backend_model": detector.backend_model,
        "variant": detector.variant,
        "device": detector.device,
    })
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            result = detector.predict_one(request)
            result["id"] = request.get("id")
            write_response(result)
        except Exception as exc:
            write_response({"id": None, "error": str(exc)})


def main() -> None:
    default_engine_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-dir", default=str(default_engine_dir))
    parser.add_argument("--variant", choices=["attn", "hybrid"], default="hybrid")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--dtype", default="bfloat16", choices=["bfloat16", "float16", "float32"])
    parser.add_argument("--max-seq-len", type=int, default=4096)
    parser.add_argument("--sampling", choices=["stable", "random"], default="stable")
    parser.add_argument("--text-json")
    parser.add_argument("--stdio", action="store_true")
    args = parser.parse_args()

    detector = InjectionDetector(
        engine_dir=args.engine_dir,
        variant=args.variant,
        device=args.device,
        dtype=args.dtype,
        max_seq_len=args.max_seq_len,
        sampling=args.sampling,
    )
    if args.stdio:
        run_stdio(detector)
        return
    if not args.text_json:
        parser.error("Pass --text-json or --stdio.")
    record = json.loads(args.text_json)
    print(json.dumps(detector.predict_one(record), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
