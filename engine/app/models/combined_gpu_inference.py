#!/usr/bin/env python3
"""PII and injection inference in one CUDA process with two streams.

The gateway keeps Solar localization in the parent process. This worker only
returns local PII entities and EXAONE classifications, so no external request
can race ahead of PII redaction.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import gc
import importlib.util
import io
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import torch


def load_runtime(path: Path, name: str):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load runtime: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class OcrSlot:
    """VARCO 이미지 OCR 을 이 프로세스 안에서 — CUDA·torch 바닥(Windows 커밋 약 2GB·VRAM 약 0.4GB)을 PII·인젝션과 한 번만
    쓴다. 가중치는 첫 그림에서 올리고(ocr_load), 엔진이 유휴로 보면 가중치만 내린다(ocr_unload, 프로세스는 남는다).
    읽기 코드는 사이드카(varco_ocr_inference.py)의 Reader·_read_one 그대로다."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.runtime = None
        self.reader = None

    def load(self, cuda_graph: bool) -> dict[str, Any]:
        if self.runtime is None:
            # 엔진은 이 프로세스의 stderr 를 죽었을 때만 읽는다 — 처리기·모델 경고가 쌓여 파이프가 차면 PII·인젝션까지
            # 멈출 수 있어 사이드카(varco_ocr_inference.main)처럼 조용히 한다. 프로세스 전체라 이 뒤로는 PII·인젝션 경고도 안 남는다
            # (오류·죽을 때의 stderr 는 그대로)
            import warnings

            from transformers.utils import logging as hf_logging
            warnings.filterwarnings("ignore")
            hf_logging.set_verbosity_error()
            hf_logging.disable_progress_bar()
            self.runtime = load_runtime(self.args.ocr_runtime, "combined_ocr_runtime")
        V = self.runtime
        if self.reader is None:
            # PII·인젝션이 할당기에 남긴 빈 블록도 mem_get_info 는 '쓰는 중' 으로 본다 — 비우고 묶음을 정한다
            V._empty_cache("cuda")
            try:
                self.reader = V.Reader(self.args.ocr_model_dir, self.args.ocr_batch, self.args.ocr_token_budget,
                                       "cuda", cuda_graph)
            except BaseException:
                gc.collect()  # 반쯤 올린 가중치를 돌려준다 — PII·인젝션이 같은 카드를 계속 쓴다
                torch.cuda.empty_cache()
                raise
        elif not cuda_graph:
            self.reader.graph = None
        card = V._nvml_free()
        return {"device": "cuda", "batch": self.reader.batch, "budget": self.reader.budget,
                "graph": self.reader.graph is not None, "card_free_mb": None if card is None else round(card / 2**20)}

    def read(self, images: list[str], cuda_graph: bool) -> list[dict[str, Any]]:
        if self.reader is None:  # 엔진이 내린 뒤 worker 만 남아 있었다
            self.load(cuda_graph)
        elif not cuda_graph:
            self.reader.graph = None
        V = self.runtime
        from PIL import Image

        results = []
        for b64 in images:
            img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
            V._empty_cache("cuda")  # 그림마다 남은 VRAM 으로 묶음을 다시 정한다(_read_one) — 탐지가 남긴 캐시를 빼고
            results.append(V._read_one(self.reader, img, "cuda", self.args.ocr_batch, self.args.ocr_token_budget))
            V._empty_cache("cuda")
        return results

    def fatal(self, exc: Exception) -> bool:
        """그래프 경로 오류이거나 CUDA 오류 — 이 프로세스의 CUDA 문맥은 PII·인젝션도 같이 쓰므로, 그래프를 껐어도 CUDA
        오류면 더 믿지 않는다(illegal memory access 는 다음 동기화에서 다른 자리로도 나온다)."""
        return self.reader is not None and (self.runtime._fatal(self.reader, exc) or "CUDA error" in str(exc))

    def unload(self) -> dict[str, Any]:
        """가중치만 내려 VRAM 을 돌려준다(Windows 는 커밋도 — 4차 측정: VRAM 2GB 를 지우고 empty_cache 하면 커밋 4,054 →
        2,194MB). 다음 그림이 다시 올린다."""
        self.reader = None
        gc.collect()
        torch.cuda.empty_cache()
        return {"reserved_mb": round(torch.cuda.memory_reserved() / 2**20)}


class CombinedGpuWorker:
    def __init__(self, args: argparse.Namespace) -> None:
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is not available")

        pii_runtime_path = args.pii_engine_dir / "runtime" / "local_pii_inference.py"
        injection_runtime_path = (
            args.injection_engine_dir / "runtime" / "local_injection_hybrid_inference.py"
        )
        pii_module = load_runtime(pii_runtime_path, "combined_pii_runtime")
        injection_module = load_runtime(injection_runtime_path, "combined_injection_runtime")

        self.pii = pii_module.PIIDetector(
            args.pii_engine_dir / "models" / args.pii_model_seed,
            device="cuda",
            dtype=args.pii_dtype,
            batch_size=args.pii_batch_size,
            max_length=args.pii_max_length,
        )
        self.injection = injection_module.InjectionDetector(
            engine_dir=args.injection_engine_dir,
            variant=args.injection_variant,
            device="cuda",
            dtype=args.injection_dtype,
            max_seq_len=args.injection_max_seq_len,
            sampling="stable",
            batch_size=args.injection_batch_size,
            pad_tokens=args.injection_pad_tokens,
            compile_backend=args.injection_compile,
            compact_attentions=args.injection_compact_attentions,
            compact_hidden_states=args.injection_compact_hidden_states,
        )
        self.injection_dtype = args.injection_dtype
        if self.pii.device != "cuda" or self.injection.device != "cuda":
            raise RuntimeError(
                f"Both models must resolve to CUDA: pii={self.pii.device}, "
                f"injection={self.injection.device}"
            )

        self.pii_stream = torch.cuda.Stream()
        self.injection_stream = torch.cuda.Stream()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="combined-gpu")
        self._warmup(args.injection_batch_size)
        # 요청은 stdin 한 줄씩 차례로 처리한다 — OCR 이 CUDA 그래프를 잡는 동안 PII·인젝션 스레드는 쉬고 있다
        self.ocr = OcrSlot(args) if args.ocr_runtime else None

    def _run_parallel(
        self,
        pii_work: Callable[[], Any],
        injection_work: Callable[[], Any],
    ) -> tuple[Any, Any]:
        barrier = threading.Barrier(2)

        def launch(work: Callable[[], Any], stream: torch.cuda.Stream):
            barrier.wait()
            with torch.cuda.stream(stream):
                return work()

        pii_future = self.pool.submit(launch, pii_work, self.pii_stream)
        injection_future = self.pool.submit(
            launch, injection_work, self.injection_stream
        )
        pii_result = pii_future.result()
        injection_result = injection_future.result()
        torch.cuda.synchronize()
        return pii_result, injection_result

    def _warmup(self, injection_batch_size: int) -> None:
        pii_texts = ["warmup text"] * min(16, self.pii.batch_size)
        records = [
            {
                "system_prompt": "You review documents.",
                "user_prompt": "Summarize the document.",
                "tool_response": "Ordinary warmup document.",
            }
        ] * injection_batch_size
        with contextlib.suppress(Exception):
            self._run_parallel(
                lambda: self.pii.predict(pii_texts),
                lambda: self.injection.predict_batch(records),
            )

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request.get("command")
        if command == "parallel":
            pii_texts = request.get("pii_texts")
            injection_records = request.get("injection_records")
            if not isinstance(pii_texts, list) or not isinstance(injection_records, list):
                raise ValueError("parallel requires pii_texts and injection_records lists")
            include_keep = request.get("include_pii_keep") is True
            pii_output, injection_results = self._run_parallel(
                lambda: (
                    self.pii.predict_with_decisions(pii_texts)
                    if include_keep
                    else self.pii.predict(pii_texts)
                ),
                lambda: self.injection.predict_batch(injection_records),
            )
            pii_entities, pii_kept = pii_output if include_keep else (pii_output, None)
            response = {
                "pii_entities": pii_entities,
                "injection_results": injection_results,
            }
            if pii_kept is not None:
                response["pii_kept"] = pii_kept
            return response
        if command == "pii":
            texts = request.get("texts")
            if not isinstance(texts, list):
                raise ValueError("pii requires a texts list")
            if request.get("include_pii_keep") is True:
                entities, kept = self.pii.predict_with_decisions(texts)
                return {"pii_entities": entities, "pii_kept": kept}
            return {"pii_entities": self.pii.predict(texts)}
        if command == "injection":
            records = request.get("records")
            if not isinstance(records, list):
                raise ValueError("injection requires a records list")
            return {"injection_results": self.injection.predict_batch(records)}
        if command in ("ocr_load", "ocr_read", "ocr_unload"):
            return self._ocr(command, request)
        raise ValueError(f"Unknown command: {command!r}")

    def _ocr(self, command: str, request: dict[str, Any]) -> dict[str, Any]:
        if self.ocr is None:
            raise ValueError("ocr_unavailable")
        if command == "ocr_unload":
            return self.ocr.unload()
        graph = request.get("cuda_graph") is True
        try:
            if command == "ocr_load":
                return self.ocr.load(graph)
            return {"results": self.ocr.read(request.get("images") or [], graph)}
        except Exception as exc:
            if self.ocr.fatal(exc):
                # 이 CUDA 문맥은 PII·인젝션도 쓴다 — 답하고 끝낸다. 엔진이 worker 를 다시 띄우고(그래프면 끄고) 같은 그림을
                # 다시 읽으며, 기다리던 탐지 요청은 새 worker 가 받는다(image_ocr.VarcoOcrProcess·combined_gpu)
                marker = "graph_failed" if self.ocr.reader.graph is not None else "cuda_failed"
                write_response({"id": request.get("id"), "error": f"{marker}: {type(exc).__name__}: {exc}"})
                os._exit(3)
            raise


def write_response(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def run_stdio(worker: CombinedGpuWorker) -> None:
    write_response(
        {
            "ready": True,
            "device": "cuda",
            "pii_dtype": str(worker.pii.dtype),
            "injection_dtype": worker.injection_dtype,
            "ocr": worker.ocr is not None,
        }
    )
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request: dict[str, Any] = {}
        try:
            request = json.loads(line)
            result = worker.handle(request)
            write_response({"id": request.get("id"), **result})
        except Exception as exc:
            write_response({"id": request.get("id"), "error": str(exc)})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pii-engine-dir", required=True, type=Path)
    parser.add_argument("--pii-model-seed", required=True)
    parser.add_argument("--pii-dtype", default="float16")
    parser.add_argument("--pii-batch-size", type=int, default=16)
    parser.add_argument("--pii-max-length", type=int, default=256)
    parser.add_argument("--injection-engine-dir", required=True, type=Path)
    parser.add_argument("--injection-variant", default="hybrid")
    parser.add_argument("--injection-dtype", default="bfloat16")
    parser.add_argument("--injection-max-seq-len", type=int, default=4096)
    parser.add_argument("--injection-batch-size", type=int, default=8)
    parser.add_argument("--injection-pad-tokens", type=int, default=192)
    parser.add_argument("--injection-compile", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument(
        "--injection-compact-attentions",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--injection-compact-hidden-states",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    # 이미지 OCR(VARCO)을 이 프로세스에서도 읽는다 — 없으면 ocr_* 요청에 ocr_unavailable(엔진은 사이드카로)
    parser.add_argument("--ocr-runtime", type=Path)
    parser.add_argument("--ocr-model-dir", type=Path)
    parser.add_argument("--ocr-batch", type=int, default=16)
    parser.add_argument("--ocr-token-budget", type=int, default=18000)
    return parser.parse_args()


def main() -> None:
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    args = parse_args()
    try:
        worker = CombinedGpuWorker(args)
    except Exception as exc:
        write_response({"ready": False, "error": str(exc)})
        raise SystemExit(2) from exc
    run_stdio(worker)


if __name__ == "__main__":
    main()
