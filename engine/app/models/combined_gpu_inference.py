#!/usr/bin/env python3
"""PII and injection inference in one CUDA process with two streams.

The gateway keeps Solar localization in the parent process. This worker only
returns local PII entities and EXAONE classifications, so no external request
can race ahead of PII redaction.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
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
            pii_entities, injection_results = self._run_parallel(
                lambda: self.pii.predict(pii_texts),
                lambda: self.injection.predict_batch(injection_records),
            )
            return {
                "pii_entities": pii_entities,
                "injection_results": injection_results,
            }
        if command == "pii":
            texts = request.get("texts")
            if not isinstance(texts, list):
                raise ValueError("pii requires a texts list")
            return {"pii_entities": self.pii.predict(texts)}
        if command == "injection":
            records = request.get("records")
            if not isinstance(records, list):
                raise ValueError("injection requires a records list")
            return {"injection_results": self.injection.predict_batch(records)}
        raise ValueError(f"Unknown command: {command!r}")


def write_response(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def run_stdio(worker: CombinedGpuWorker) -> None:
    write_response(
        {
            "ready": True,
            "device": "cuda",
            "pii_dtype": str(worker.pii.dtype),
            "injection_dtype": worker.injection_dtype,
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
