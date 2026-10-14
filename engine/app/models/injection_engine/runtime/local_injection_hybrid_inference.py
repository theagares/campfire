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
import contextlib
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
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
        batch_size: int = 8,
        pad_tokens: int = 192,
        compile_backend: bool = False,
        compact_attentions: bool = True,
        compact_hidden_states: bool = True,
    ):
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.engine_dir = Path(engine_dir)
        self.variant = variant
        self.device = self._resolve_device(device)
        self.max_seq_len = max_seq_len
        self.sampling = sampling
        self.seed = 42
        self.batch_size = max(1, int(batch_size))
        self.pad_tokens = max(1, min(int(pad_tokens), self.max_seq_len))
        self.compact_attentions = compact_attentions
        self.compact_hidden_states = compact_hidden_states
        self.compile_requested = bool(compile_backend)
        self.compile_active = False
        self.compile_error: str | None = None
        self._attention_pair_indices: tuple[torch.Tensor, torch.Tensor] | None = None
        self._hidden_capture_specs: list[
            tuple[torch.Tensor, tuple[tuple[int, int], ...], int]
        ] | None = None
        self._hidden_captures: dict[int, torch.Tensor] = {}
        self._prepare_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="injection-prepare",
        )

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

        # Normalization tensors are immutable at inference time.  Keeping them on the
        # execution device avoids four host-to-device copies for every batch.
        self.pair_mu = self.pair_mu.to(self.device)
        self.pair_sd = self.pair_sd.to(self.device)
        self.hidden_mu = self.hidden_mu.to(self.device)
        self.hidden_sd = self.hidden_sd.to(self.device)

        # Transformers normally retains every (B,H,T,T) attention matrix until the
        # full forward finishes.  The detector needs only K sampled query/key pairs.
        # This hook runs before HF's output-capture hook, replaces each layer's full
        # matrix with (B,H,K), and therefore keeps the exact trained feature while
        # releasing the large matrix as soon as that layer is done.
        model_core = getattr(self.backend, "model", None)
        layers = getattr(model_core, "layers", None)
        self._attention_hook_handles: list[Any] = []
        if self.compact_attentions:
            if layers is None:
                raise RuntimeError("Compact attention capture requires backend.model.layers.")
            for layer in layers:
                self._attention_hook_handles.append(
                    layer.self_attn.register_forward_hook(self._compact_attention_hook)
                )

        # output_hidden_states=True retains every (B,T,D) layer output.  The trained
        # hybrid head only uses four layers, and only as three tool-span means plus
        # the final real token.  Capture those pooled vectors at the producing layer
        # and leave the full tensor available only for the next transformer layer.
        self._hidden_hook_handles: list[Any] = []
        if self.compact_hidden_states:
            if layers is None or model_core is None:
                raise RuntimeError("Compact hidden capture requires backend.model.layers.")
            layer_count = len(layers)
            for hidden_index in self.hs_layer_idxs:
                if hidden_index == 0:
                    target = model_core.embed_tokens
                elif 0 < hidden_index < layer_count:
                    target = layers[hidden_index - 1]
                elif hidden_index == layer_count:
                    # Transformers replaces hidden_states[-1] with the normalized
                    # last_hidden_state; hook the norm to preserve that exact value.
                    target = model_core.norm
                else:
                    raise RuntimeError(
                        f"Hidden layer index {hidden_index} is outside 0..{layer_count}."
                    )
                self._hidden_hook_handles.append(
                    target.register_forward_hook(self._make_compact_hidden_hook(hidden_index))
                )

        # Compilation is opt-in and CUDA-only.  It is deliberately installed after
        # the compact hooks so the compiled graph sees the production execution path.
        # A first-forward failure is caught in _forward_backend and falls back to the
        # original eager model without taking the sidecar down.
        self._eager_backend = self.backend
        compile_supported = False
        if self.compile_requested and self.device == "cuda" and hasattr(torch, "compile"):
            with contextlib.suppress(Exception):
                from torch.utils._triton import has_triton

                compile_supported = bool(has_triton())
            if not compile_supported:
                self.compile_error = "torch.compile skipped: no compatible Triton runtime"
        if compile_supported:
            try:
                self.backend = torch.compile(
                    self._eager_backend, mode="reduce-overhead", fullgraph=False, dynamic=False
                )
                self.compile_active = True
            except Exception as exc:
                self.compile_error = f"{type(exc).__name__}: {exc}"
                self.backend = self._eager_backend

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

    def _compact_attention_hook(self, module, args, output):
        """Retain only the preselected query/key probabilities for one layer."""
        pair_indices = self._attention_pair_indices
        if pair_indices is None:
            return output
        if not isinstance(output, tuple) or len(output) < 2 or output[1] is None:
            raise RuntimeError("The eager attention backend did not return attention weights.")
        weights = output[1]
        query_indices, key_indices = pair_indices
        batch_size, head_count, _query_length, key_length = weights.shape
        flat_indices = (query_indices * key_length + key_indices).unsqueeze(1)
        flat_indices = flat_indices.expand(batch_size, head_count, -1)
        selected = weights.reshape(batch_size, head_count, -1).gather(
            2,
            flat_indices,
        )  # (B,H,K)
        return (output[0], selected, *output[2:])

    def _make_compact_hidden_hook(self, hidden_index: int):
        """Build a hook that keeps only the trained pools for one hidden layer."""

        def capture(module, args, output):
            specs = self._hidden_capture_specs
            if specs is None:
                return output
            hidden = output[0] if isinstance(output, (tuple, list)) else output
            if not isinstance(hidden, torch.Tensor) or hidden.ndim != 3:
                raise RuntimeError(
                    f"Hidden layer {hidden_index} returned an unsupported value."
                )
            pooled_batch: list[torch.Tensor] = []
            for batch_index, (tool_indices, segment_bounds, last_index) in enumerate(specs):
                tool_hidden = hidden[batch_index].index_select(0, tool_indices)
                pools = [
                    tool_hidden[left:right].mean(0).float()
                    for left, right in segment_bounds
                ]
                pools.append(hidden[batch_index, last_index].float())
                pooled_batch.append(torch.stack(pools, dim=0))
            self._hidden_captures[hidden_index] = torch.stack(pooled_batch, dim=0)
            return output

        return capture

    def _forward_backend(self, encoded: dict[str, torch.Tensor]):
        kwargs = dict(
            **encoded,
            output_attentions=True,
            output_hidden_states=not self.compact_hidden_states,
            use_cache=False,
            logits_to_keep=1,
        )
        try:
            return self.backend(**kwargs)
        except Exception as exc:
            if not self.compile_active:
                raise
            # torch.compile support varies by torch/transformers/GPU combination.
            # Preserve correctness and portability by reverting to eager once.
            self.compile_error = f"{type(exc).__name__}: {exc}"
            self.compile_active = False
            self.backend = self._eager_backend
            with contextlib.suppress(Exception):
                torch._dynamo.reset()
            return self.backend(**kwargs)

    def _sample_pair_indices(
        self,
        record: dict[str, str],
        user_indices: list[int],
        tool_indices: list[int],
    ) -> tuple[torch.Tensor, torch.Tensor]:
        population = len(tool_indices) * len(user_indices)
        if self.sampling == "stable":
            rng = np.random.default_rng(stable_seed(record, self.seed))
        else:
            rng = np.random.default_rng()
        selected = np.sort(
            rng.choice(population, size=self.max_pairs, replace=(population < self.max_pairs))
        )
        query = np.asarray(tool_indices, dtype=np.int64)[selected // len(user_indices)]
        key = np.asarray(user_indices, dtype=np.int64)[selected % len(user_indices)]
        return (
            torch.as_tensor(query),
            torch.as_tensor(key),
        )

    def _prepare_batch(self, records: list[dict[str, str]]) -> dict[str, Any]:
        """Tokenize and build sampling metadata without touching CUDA.

        Keeping this phase CPU-only lets ``predict_batch`` prepare batch N+1 on a
        dedicated thread while batch N is running on the GPU.
        """
        if not records:
            raise ValueError("At least one record is required.")

        actual_count = len(records)
        if actual_count > self.batch_size:
            raise ValueError(f"Batch has {actual_count} records; maximum is {self.batch_size}.")

        # A compiled CUDA graph uses a fixed batch dimension.  The final partial
        # batch is padded with a duplicate and sliced away after classification.
        # Eager CUDA and CPU/MPS avoid that extra work.
        padded_records = list(records)
        if self.device == "cuda" and self.compile_active:
            padded_records.extend([records[-1]] * (self.batch_size - actual_count))

        rendered = [render_chat(self.tokenizer, build_messages(record)) for record in padded_records]
        char_spans = [
            locate_spans(text, record["user_prompt"], record["tool_response"])
            for text, record in zip(rendered, padded_records, strict=True)
        ]

        original_padding_side = self.tokenizer.padding_side
        original_pad_token = self.tokenizer.pad_token
        self.tokenizer.padding_side = "right"
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        try:
            # Tokenize once.  The previous path first tokenized every record to
            # discover its length and then tokenized the whole batch again.  Padding
            # the resulting IDs preserves the exact model input while removing the
            # duplicate Rust-tokenizer pass.
            unpadded = self.tokenizer(
                rendered,
                return_offsets_mapping=True,
                padding=False,
                truncation=True,
                max_length=self.max_seq_len,
                add_special_tokens=False,
            )
            offsets_unpadded = unpadded.pop("offset_mapping")
            longest = max(len(input_ids) for input_ids in unpadded["input_ids"])
            # 고정 길이(pad_tokens)로 채우는 것은 CUDA 에서 같은 shape 를 다시 쓰기 위해서다.
            # CPU·MPS 는 묶음의 가장 긴 것에 맞춘다(오른쪽 채움 + 마스크라 결과는 같고 계산만 준다).
            bucket = ((longest + 31) // 32) * 32
            target_length = max(self.pad_tokens, bucket) if self.device == "cuda" else bucket
            target_length = min(target_length, self.max_seq_len)
            rows = [
                {key: values[index] for key, values in unpadded.items()}
                for index in range(len(padded_records))
            ]
            encoded = self.tokenizer.pad(
                rows,
                return_tensors="pt",
                padding="max_length",
                max_length=target_length,
            )
            offsets_batch = [
                [tuple(pair) for pair in offsets]
                + [(0, 0)] * (target_length - len(offsets))
                for offsets in offsets_unpadded
            ]
        finally:
            self.tokenizer.padding_side = original_padding_side
            self.tokenizer.pad_token = original_pad_token

        token_spans: list[tuple[list[int], list[int]]] = []
        for offsets, ((user_start, user_end), (tool_start, tool_end)) in zip(
            offsets_batch, char_spans, strict=True
        ):
            user_indices = char_span_to_token_indices(offsets, user_start, user_end)
            tool_indices = char_span_to_token_indices(offsets, tool_start, tool_end)
            if not user_indices or not tool_indices:
                raise ValueError("Could not locate non-empty user/tool token spans after tokenization.")
            token_spans.append((user_indices, tool_indices))

        sampled = [
            self._sample_pair_indices(record, user_indices, tool_indices)
            for record, (user_indices, tool_indices) in zip(
                padded_records, token_spans, strict=True
            )
        ]
        return {
            "actual_count": actual_count,
            "records": padded_records,
            "encoded": dict(encoded),
            "token_spans": token_spans,
            "pair_indices": sampled,
        }

    @torch.inference_mode()
    def _extract_prepared_features(
        self, prepared: dict[str, Any]
    ) -> tuple[torch.Tensor, torch.Tensor]:
        actual_count = int(prepared["actual_count"])
        token_spans: list[tuple[list[int], list[int]]] = prepared["token_spans"]
        encoded_cpu: dict[str, torch.Tensor] = prepared["encoded"]
        attention_mask = encoded_cpu["attention_mask"]
        pair_indices = (
            torch.stack([query for query, _key in prepared["pair_indices"]]).to(
                self.device
            ),
            torch.stack([key for _query, key in prepared["pair_indices"]]).to(
                self.device
            ),
        )
        encoded = {key: value.to(self.device) for key, value in encoded_cpu.items()}

        # Prepare the exact token selections before the backend forward so hidden
        # hooks can reduce each requested (B,T,D) tensor immediately.  The bounds
        # intentionally match the original post-forward pooling implementation.
        tool_tensors: list[torch.Tensor] = []
        hidden_capture_specs: list[
            tuple[torch.Tensor, tuple[tuple[int, int], ...], int]
        ] = []
        for batch_index, (_, tool_indices) in enumerate(token_spans):
            tool_tensor = torch.tensor(tool_indices, device=self.device)
            raw_bounds = torch.linspace(
                0, tool_tensor.shape[0], self.n_seg + 1
            ).round().long()
            segment_bounds = tuple(
                (
                    int(raw_bounds[segment].item()),
                    max(
                        int(raw_bounds[segment + 1].item()),
                        int(raw_bounds[segment].item()) + 1,
                    ),
                )
                for segment in range(self.n_seg)
            )
            last_index = int(attention_mask[batch_index].sum().item()) - 1
            tool_tensors.append(tool_tensor)
            hidden_capture_specs.append((tool_tensor, segment_bounds, last_index))

        self._attention_pair_indices = pair_indices
        self._hidden_capture_specs = (
            hidden_capture_specs if self.compact_hidden_states else None
        )
        self._hidden_captures = {}
        try:
            outputs = self._forward_backend(encoded)
            hidden_captures = dict(self._hidden_captures)
        finally:
            self._attention_pair_indices = None
            self._hidden_capture_specs = None
            self._hidden_captures = {}

        normalized_pairs: list[torch.Tensor] = []
        compact_pairs: torch.Tensor | None = None
        if self.compact_attentions:
            # Convert and stack once per layer rather than once per sample.  The
            # resulting (B,K,L*H) layout is identical to the former per-sample path.
            layers = torch.stack(
                [attention.float() for attention in outputs.attentions],
                dim=1,
            )  # (B,L,H,K)
            compact_pairs = layers.permute(0, 3, 1, 2).reshape(
                layers.shape[0], self.max_pairs, self.lh
            )
            compact_pairs = (compact_pairs - self.pair_mu) / self.pair_sd
        normalized_hidden: list[torch.Tensor] = []
        for batch_index, (user_indices, tool_indices) in enumerate(token_spans):
            tool_tensor = tool_tensors[batch_index]

            if self.compact_attentions:
                assert compact_pairs is not None
                pairs = compact_pairs[batch_index]
            else:
                user_tensor = torch.tensor(user_indices, device=self.device)
                blocks = [
                    attention[batch_index]
                    .index_select(1, tool_tensor)
                    .index_select(2, user_tensor)
                    .float()
                    for attention in outputs.attentions
                ]
                population = torch.stack(blocks, dim=0).permute(2, 3, 0, 1).reshape(-1, self.lh)
                query_indices = pair_indices[0][batch_index]
                key_indices = pair_indices[1][batch_index]
                # Recover the sampled population offsets for the compatibility path.
                selected = (
                    torch.searchsorted(tool_tensor, query_indices) * len(user_indices)
                    + torch.searchsorted(user_tensor, key_indices)
                )
                pairs = population[selected]
                pairs = (pairs - self.pair_mu) / self.pair_sd
            normalized_pairs.append(pairs)

            hidden_vectors: list[torch.Tensor | None] = [None] * len(self.hs_cols)
            _, segment_bounds, last_index = hidden_capture_specs[batch_index]
            if self.compact_hidden_states:
                for layer_index in self.hs_layer_idxs:
                    captured = hidden_captures.get(layer_index)
                    if captured is None:
                        raise RuntimeError(
                            f"Hidden layer {layer_index} was not captured during forward."
                        )
                    for segment in range(self.n_seg):
                        column = self.hs_cols.index(f"L{layer_index}_seg{segment}")
                        hidden_vectors[column] = captured[batch_index, segment]
                    column = self.hs_cols.index(f"L{layer_index}_last")
                    hidden_vectors[column] = captured[batch_index, self.n_seg]
            else:
                for layer_index in self.hs_layer_idxs:
                    hidden_state = outputs.hidden_states[layer_index][batch_index]
                    tool_hidden = hidden_state.index_select(0, tool_tensor)
                    for segment, (left, right) in enumerate(segment_bounds):
                        column = self.hs_cols.index(f"L{layer_index}_seg{segment}")
                        hidden_vectors[column] = tool_hidden[left:right].mean(0).float()
                    column = self.hs_cols.index(f"L{layer_index}_last")
                    hidden_vectors[column] = hidden_state[last_index].float()
            if any(vector is None for vector in hidden_vectors):
                raise RuntimeError("Hidden feature columns do not match extract_config.json.")
            hidden = torch.cat([vector for vector in hidden_vectors if vector is not None])
            normalized_hidden.append((hidden - self.hidden_mu) / self.hidden_sd)

        return (
            torch.stack(normalized_pairs[:actual_count]),
            torch.stack(normalized_hidden[:actual_count]),
        )

    @torch.inference_mode()
    def extract_features(
        self, records: list[dict[str, str]]
    ) -> tuple[torch.Tensor, torch.Tensor]:
        return self._extract_prepared_features(self._prepare_batch(records))

    @torch.inference_mode()
    def extract_feature(self, record: dict[str, str]) -> tuple[torch.Tensor, torch.Tensor]:
        pairs, hidden = self.extract_features([record])
        return pairs[0], hidden[0]

    @torch.inference_mode()
    def predict_one(self, record: dict[str, str]) -> dict[str, Any]:
        return self.predict_batch([record])[0]

    @torch.inference_mode()
    def predict_batch(self, records: list[dict[str, str]]) -> list[dict[str, Any]]:
        if not records:
            return []
        probability_batches: list[torch.Tensor] = []
        batches = [
            records[start : start + self.batch_size]
            for start in range(0, len(records), self.batch_size)
        ]

        def infer(prepared: dict[str, Any]) -> None:
            pairs, hidden = self._extract_prepared_features(prepared)
            logits = self.detector(pairs, hidden).clone()
            logits[:, MIS] += self.misaligned_bias
            probability_batches.append(torch.softmax(logits, dim=-1))

        if self.device == "cuda" and len(batches) > 1:
            next_prepared = self._prepare_pool.submit(self._prepare_batch, batches[0])
            for index in range(len(batches)):
                prepared = next_prepared.result()
                if index + 1 < len(batches):
                    next_prepared = self._prepare_pool.submit(
                        self._prepare_batch,
                        batches[index + 1],
                    )
                infer(prepared)
        else:
            for batch in batches:
                infer(self._prepare_batch(batch))

        # Keep every small result tensor on the execution device until all model
        # batches have been queued.  Copying each batch to NumPy used to force one
        # CUDA synchronization per batch (eleven for the 10-page fixture).
        probabilities = torch.cat(probability_batches, dim=0).cpu().numpy()
        results: list[dict[str, Any]] = []
        for probs in probabilities:
            pred_id = int(probs.argmax())
            results.append(
                {
                    "label": ID_TO_LABEL[pred_id],
                    "is_injection": pred_id == MIS,
                    "scores": {ID_TO_LABEL[i]: float(probs[i]) for i in range(3)},
                    "backend_model": self.backend_model,
                    "detector": {
                        "variant": self.variant,
                        "misaligned_bias": self.misaligned_bias,
                        "sampling": self.sampling,
                        "batch_size": self.batch_size,
                        "compact_attentions": self.compact_attentions,
                        "compact_hidden_states": self.compact_hidden_states,
                        "compile_active": self.compile_active,
                    },
                }
            )
        return results


def write_response(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run_stdio(detector: InjectionDetector) -> None:
    write_response({
        "ready": True,
        "backend_model": detector.backend_model,
        "variant": detector.variant,
        "device": detector.device,
        "batch_size": detector.batch_size,
        "pad_tokens": detector.pad_tokens,
        "compact_attentions": detector.compact_attentions,
        "compact_hidden_states": detector.compact_hidden_states,
        "compile_active": detector.compile_active,
        "compile_error": detector.compile_error,
    })
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request: dict[str, Any] = {}
        try:
            request = json.loads(line)
            if "records" in request:
                records = request["records"]
                if not isinstance(records, list) or not records:
                    raise ValueError("records must be a non-empty list")
                results = detector.predict_batch(records)
                write_response({"id": request.get("id"), "results": results})
            else:
                result = detector.predict_one(request)
                result["id"] = request.get("id")
                write_response(result)
        except Exception as exc:
            write_response({"id": request.get("id"), "error": str(exc)})


def main() -> None:
    default_engine_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-dir", default=str(default_engine_dir))
    parser.add_argument("--variant", choices=["attn", "hybrid"], default="hybrid")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--dtype", default="bfloat16", choices=["bfloat16", "float16", "float32"])
    parser.add_argument("--max-seq-len", type=int, default=4096)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--pad-tokens", type=int, default=192)
    parser.add_argument("--compile", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--compact-attentions", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--compact-hidden-states", action=argparse.BooleanOptionalAction, default=True
    )
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
        batch_size=args.batch_size,
        pad_tokens=args.pad_tokens,
        compile_backend=args.compile,
        compact_attentions=args.compact_attentions,
        compact_hidden_states=args.compact_hidden_states,
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
