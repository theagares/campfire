#!/usr/bin/env python3
"""Full fine-tuning for one controlled KDPII-19 model/seed condition."""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import random
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from safetensors.torch import load_file, save_file
from transformers import AutoTokenizer

from kdpii import (BIO, PREPARED, ROOT, TYPES, ExactSpanMetrics, align_bio,
                   decode_bio, read_jsonl, sha256_file, write_json)
from modeling import GazetteerTrie, MODEL_SPECS, PIITagger, parameter_counts


HERE = Path(__file__).resolve().parents[1]


def seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def build_gazetteer(rows: list[dict]) -> dict[str, list[str]]:
    entries = {label: set() for label in TYPES}
    for row in rows:
        if row["split"] == "train":
            for entity in row["gold"]:
                entries[entity["label"]].add(entity["form"])
    return {label: sorted(forms) for label, forms in entries.items()}


def encode(rows: list[dict], tokenizer, gazetteer: GazetteerTrie, max_length: int) -> tuple[list[dict], dict]:
    encoded, audit = [], collections.Counter()
    for row in rows:
        tokenized = tokenizer(row["text"], return_offsets_mapping=True, truncation=True, max_length=max_length)
        offsets = [tuple(pair) for pair in tokenized.pop("offset_mapping")]
        visible_end = max((end for begin, end in offsets if end > begin), default=0)
        if any(entity["end"] > visible_end for entity in row["gold"]):
            audit["truncated_gold_spans"] += 1
        labels = align_bio(row, offsets)
        encoded.append({
            **row,
            "input_ids": tokenized["input_ids"],
            "attention_mask": tokenized["attention_mask"],
            "offsets": offsets,
            "labels": labels,
            "gazetteer": gazetteer.features(row["text"], offsets),
        })
    return encoded, dict(audit)


def batches(rows: list[dict], batch_size: int, shuffle: bool, seed: int):
    indices = list(range(len(rows)))
    if shuffle:
        random.Random(seed).shuffle(indices)
    for start in range(0, len(indices), batch_size):
        yield indices[start:start + batch_size]


def collate(rows: list[dict], indices: list[int], pad_id: int):
    width = max(len(rows[index]["input_ids"]) for index in indices)
    shape = (len(indices), width)
    input_ids = torch.full(shape, pad_id, dtype=torch.long)
    attention = torch.zeros(shape, dtype=torch.long)
    labels = torch.full(shape, -100, dtype=torch.long)
    gazetteer = torch.zeros((*shape, len(TYPES)), dtype=torch.float32)
    real_tokens = torch.zeros(shape, dtype=torch.bool)
    for batch_index, row_index in enumerate(indices):
        row = rows[row_index]
        length = len(row["input_ids"])
        input_ids[batch_index, :length] = torch.tensor(row["input_ids"])
        attention[batch_index, :length] = torch.tensor(row["attention_mask"])
        labels[batch_index, :length] = torch.tensor(row["labels"])
        gazetteer[batch_index, :length] = torch.tensor(row["gazetteer"])
        real_tokens[batch_index, :length] = torch.tensor([end > begin for begin, end in row["offsets"]])
    device = torch.device("cuda")
    return tuple(item.to(device, non_blocking=True) for item in
                 (input_ids, attention, gazetteer, labels, real_tokens))


def decode(model: PIITagger, logits: torch.Tensor, real_tokens: torch.Tensor) -> list[list[int]]:
    if not model.use_crf:
        return logits.float().argmax(-1).cpu().tolist()
    sequences = [logits[index, mask] for index, mask in enumerate(real_tokens)]
    lengths = torch.tensor([len(sequence) for sequence in sequences], device=logits.device)
    packed = torch.nn.utils.rnn.pad_sequence(sequences, batch_first=True)
    mask = torch.arange(packed.shape[1], device=logits.device)[None, :] < lengths[:, None]
    paths = model.crf.decode(packed, mask).cpu().tolist()
    result = []
    for row_mask, path in zip(real_tokens.cpu().tolist(), paths):
        tags, cursor = [0] * len(row_mask), 0
        for position, valid in enumerate(row_mask):
            if valid:
                tags[position] = path[cursor]
                cursor += 1
        result.append(tags)
    return result


@torch.inference_mode()
def evaluate(model, rows, tokenizer, batch_size: int, save_predictions: Path | None = None):
    model.eval()
    metric = ExactSpanMetrics()
    predictions = []
    started = time.monotonic()
    for indices in batches(rows, batch_size, False, 0):
        input_ids, attention, gazetteer, _, real_tokens = collate(rows, indices, tokenizer.pad_token_id)
        with torch.autocast("cuda", dtype=torch.bfloat16):
            logits = model(input_ids, attention, gazetteer)
        tags = decode(model, logits, real_tokens)
        for row_index, tag_ids in zip(indices, tags):
            row = rows[row_index]
            pred = decode_bio(row["offsets"], tag_ids[:len(row["offsets"])])
            metric.add(row["gold"], pred)
            if save_predictions is not None:
                predictions.append({"id": row["id"], "text": row["text"], "gold": row["gold"], "prediction": pred})
    elapsed = time.monotonic() - started
    result = metric.compute()
    result.update({"sentences": len(rows), "seconds": elapsed,
                   "sentences_per_second": len(rows) / elapsed if elapsed else 0.0})
    if save_predictions is not None:
        with save_predictions.open("w", encoding="utf-8") as stream:
            for row in predictions:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    return result


def save_weights(path: Path, model: PIITagger) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp.safetensors")
    save_file({key: value.detach().cpu().contiguous() for key, value in model.state_dict().items()}, str(temporary))
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=MODEL_SPECS, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--prepared", type=Path, default=PREPARED)
    parser.add_argument("--epochs", type=int, default=12)
    parser.add_argument("--patience", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--accumulation", type=int, default=1)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--lr", type=float)
    parser.add_argument("--head-multiplier", type=float, default=5.0)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--warmup-ratio", type=float, default=0.1)
    parser.add_argument("--max-train-steps", type=int)
    parser.add_argument("--limit-rows", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--skip-test", action="store_true", help="Validation-only smoke/pilot; never writes test metrics")
    args = parser.parse_args()

    if not torch.cuda.is_available():
        parser.error("CUDA GPU is required")
    run = args.run.resolve()
    if run.exists() and not args.resume:
        parser.error(f"Run directory already exists: {run}")
    run.mkdir(parents=True, exist_ok=True)
    spec = MODEL_SPECS[args.model]
    backbone_path = ROOT / "shared/models" / spec["backbone"]
    report = json.loads((args.prepared / "report.json").read_text(encoding="utf-8"))
    if report["source_sha256"] != sha256_file(Path(report["source"])):
        raise ValueError("KDPII source hash changed")
    if report["rows_sha256"] != sha256_file(args.prepared / "rows.jsonl"):
        raise ValueError("Prepared rows hash changed")

    seed_everything(args.seed)
    torch.set_num_threads(4)
    torch.backends.cuda.matmul.allow_tf32 = True
    rows = read_jsonl(args.prepared / "rows.jsonl")
    if args.limit_rows:
        limited = []
        for split in ("train", "valid", "test"):
            limited.extend([row for row in rows if row["split"] == split][:args.limit_rows])
        rows = limited
    gazetteer_entries = build_gazetteer(rows)
    write_json(run / "train_gazetteer.json", gazetteer_entries)
    gazetteer = GazetteerTrie(gazetteer_entries)
    tokenizer = AutoTokenizer.from_pretrained(backbone_path, local_files_only=True, use_fast=True)
    encoded, tokenization_audit = encode(rows, tokenizer, gazetteer, args.max_length)
    if tokenization_audit.get("truncated_gold_spans"):
        raise ValueError(f"Gold spans truncated: {tokenization_audit}")
    partitions = {split: [row for row in encoded if row["split"] == split]
                  for split in ("train", "valid", "test")}

    model = PIITagger(backbone_path, spec["gazetteer"], spec["crf"]).to("cuda")
    model.requires_grad_(True)
    counts = parameter_counts(model)
    encoder, head = [], []
    for name, parameter in model.named_parameters():
        (encoder if name.startswith("backbone.") else head).append(parameter)
    learning_rate = args.lr or spec["lr"]
    optimizer = torch.optim.AdamW([
        {"params": encoder, "lr": learning_rate},
        {"params": head, "lr": learning_rate * args.head_multiplier},
    ], weight_decay=args.weight_decay)
    steps_per_epoch = math.ceil(len(partitions["train"]) / args.batch_size / args.accumulation)
    total_steps = max(1, steps_per_epoch * args.epochs)
    warmup_steps = max(1, round(total_steps * args.warmup_ratio))

    def schedule(step):
        if step < warmup_steps:
            return (step + 1) / warmup_steps
        return max(0.0, (total_steps - step) / max(1, total_steps - warmup_steps))

    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, schedule)
    config = {
        "dataset": "KDPII-19", "dataset_report": str(args.prepared / "report.json"),
        "rows_sha256": report["rows_sha256"], "source_sha256": report["source_sha256"],
        "target_labels": TYPES, "model": args.model, "spec": spec, "backbone": str(backbone_path),
        "seed": args.seed, "full_finetuning": True, "parameters": counts,
        "epochs": args.epochs, "patience": args.patience, "batch_size": args.batch_size,
        "accumulation": args.accumulation, "effective_batch_size": args.batch_size * args.accumulation,
        "max_length": args.max_length, "encoder_lr": learning_rate,
        "head_lr": learning_rate * args.head_multiplier, "weight_decay": args.weight_decay,
        "warmup_ratio": args.warmup_ratio, "tokenization_audit": tokenization_audit,
        "split_counts": {split: len(values) for split, values in partitions.items()},
        "selection": "validation exact-span micro-F1", "test_tuning": False,
        "torch": torch.__version__, "cuda": torch.version.cuda,
    }
    write_json(run / "config.json", config)

    best, best_epoch, bad, global_step, begin_epoch = -1.0, 0, 0, 0, 1
    last_path = run / "last.pt"
    if args.resume and last_path.exists():
        state = torch.load(last_path, map_location="cpu", weights_only=False)
        model.load_state_dict(state["model"])
        optimizer.load_state_dict(state["optimizer"])
        scheduler.load_state_dict(state["scheduler"])
        best, best_epoch, bad = state["best"], state["best_epoch"], state["bad"]
        global_step, begin_epoch = state["global_step"], state["epoch"] + 1

    stop_requested = False
    for epoch in range(begin_epoch, args.epochs + 1):
        model.train()
        optimizer.zero_grad(set_to_none=True)
        loss_sum, valid_tokens, started = 0.0, 0, time.monotonic()
        train_batches = list(batches(partitions["train"], args.batch_size, True, args.seed + epoch))
        for batch_number, indices in enumerate(train_batches, 1):
            input_ids, attention, gazetteer_features, labels, _ = collate(
                partitions["train"], indices, tokenizer.pad_token_id)
            with torch.autocast("cuda", dtype=torch.bfloat16):
                logits = model(input_ids, attention, gazetteer_features)
                if model.use_crf:
                    numerator = model.crf.loss(logits, labels)
                else:
                    numerator = F.cross_entropy(logits.float().reshape(-1, len(BIO)), labels.reshape(-1),
                                                ignore_index=-100, reduction="sum")
                denominator = max(1, int((labels != -100).sum()))
                loss = numerator / denominator / args.accumulation
            loss.backward()
            loss_sum += float(numerator.detach())
            valid_tokens += denominator
            if batch_number % args.accumulation == 0 or batch_number == len(train_batches):
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0, error_if_nonfinite=True)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad(set_to_none=True)
                global_step += 1
                if global_step % 50 == 0:
                    print(json.dumps({"event": "train", "model": args.model, "seed": args.seed,
                                      "epoch": epoch, "step": global_step,
                                      "loss": loss_sum / max(1, valid_tokens)}, ensure_ascii=False), flush=True)
                if args.max_train_steps and global_step >= args.max_train_steps:
                    stop_requested = True
                    break

        validation = evaluate(model, partitions["valid"], tokenizer, args.batch_size)
        score = validation["f1"]
        improved = score > best + 1e-6
        if improved:
            best, best_epoch, bad = score, epoch, 0
            save_weights(run / "best/model.safetensors", model)
            write_json(run / "best/validation.json", validation)
        else:
            bad += 1
        epoch_result = {"epoch": epoch, "global_step": global_step,
                        "train_loss": loss_sum / max(1, valid_tokens), "validation": validation,
                        "best_validation_f1": best, "best_epoch": best_epoch,
                        "seconds": time.monotonic() - started}
        write_json(run / f"epoch_{epoch:03d}.json", epoch_result)
        temporary = run / "last.tmp.pt"
        torch.save({"model": {key: value.detach().cpu() for key, value in model.state_dict().items()},
                    "optimizer": optimizer.state_dict(), "scheduler": scheduler.state_dict(),
                    "best": best, "best_epoch": best_epoch, "bad": bad,
                    "global_step": global_step, "epoch": epoch}, temporary)
        temporary.replace(last_path)
        write_json(run / "status.json", {"status": "training", "model": args.model, "seed": args.seed,
                                          "epoch": epoch, "step": global_step,
                                          "best_epoch": best_epoch, "best_validation_f1": best})
        print(json.dumps({"event": "validation", **epoch_result}, ensure_ascii=False), flush=True)
        if stop_requested or bad >= args.patience:
            break

    model.load_state_dict(load_file(str(run / "best/model.safetensors")), strict=True)
    test_metrics = None if args.skip_test else evaluate(
        model, partitions["test"], tokenizer, args.batch_size, run / "test_predictions.jsonl")
    result = {"model": args.model, "seed": args.seed, "best_epoch": best_epoch,
              "best_validation_f1": best, "test": test_metrics, "parameters": counts,
              "gpu_peak_allocated_gib": torch.cuda.max_memory_allocated() / 2 ** 30,
              "scope": "validation-only pilot" if args.skip_test else "final test evaluation"}
    write_json(run / "result.json", result)
    write_json(run / "status.json", {"status": "complete", **result})
    if last_path.exists():
        last_path.unlink()
    print(json.dumps({"event": "complete", **result}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
