"""Canonical KDPII-19 data preparation and exact-span evaluation utilities."""
from __future__ import annotations

import collections
import hashlib
import json
import random
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
SOURCE = ROOT / "shared/datasets/PII_dataset_V3.json"
PREPARED = Path(__file__).resolve().parents[1] / "data/prepared_seed42"

LABEL_MAP = dict(zip(
    ["PS_NAME", "LC_ADDRESS", "OG_WORKPLACE", "OG_DEPARTMENT", "CV_POSITION", "OGG_EDUCATION",
     "QT_MOBILE", "QT_PHONE", "QT_RESIDENT_NUMBER", "QT_ALIEN_NUMBER", "QT_DRIVER_NUMBER",
     "QT_PLATE_NUMBER", "QT_ACCOUNT_NUMBER", "QT_CARD_NUMBER", "TMI_EMAIL", "QT_PASSPORT_NUMBER",
     "QT_AGE", "DT_BIRTH", "FD_MAJOR"],
    ["NAME", "ADDRESS", "WORKPLACE", "DEPARTMENT", "POSITION", "SCHOOL", "MOBILE_PHONE",
     "TELEPHONE", "RRN", "ALIEN_REGISTRATION_NUMBER", "DRIVER_LICENSE_NUMBER", "VEHICLE_NUMBER",
     "BANK_ACCOUNT_NUMBER", "CARD_NUMBER", "EMAIL", "PASSPORT_NUMBER", "AGE", "DATE_OF_BIRTH",
     "MAJOR"]
))
TYPES = list(LABEL_MAP.values())
BIO = ["O", *("B-" + k for k in TYPES), *("I-" + k for k in TYPES)]
BIO_ID = {label: i for i, label in enumerate(BIO)}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _normalise_sentence(raw: dict, audit: collections.Counter) -> dict:
    text = raw["form"]
    gold, ignored = [], []
    for entity in raw.get("PNE", []):
        begin, end = entity["begin"], entity["end"]
        if not 0 <= begin < end <= len(text):
            raise ValueError("Invalid annotation offset")
        form = entity.get("form", "")
        if form != text[begin:end]:
            audit["annotation_form_mismatch"] += 1
            if not form or text.count(form) != 1:
                raise ValueError("Ambiguous annotation form mismatch")
            begin = text.index(form)
            end = begin + len(form)
            audit["uniquely_realigned_annotations"] += 1
        label = LABEL_MAP.get(entity["label"])
        if label:
            gold.append({"begin": begin, "end": end, "label": label, "form": text[begin:end]})
        else:
            ignored.append([begin, end, entity["label"]])
            audit["non_target_annotations"] += 1
    gold = [dict(begin=b, end=e, label=k, form=f) for b, e, k, f in sorted(
        {(e["begin"], e["end"], e["label"], e["form"]) for e in gold})]
    if any(a["end"] > b["begin"] for a, b in zip(gold, gold[1:])):
        raise ValueError("Overlapping target annotations are not BIO-compatible")
    return {"text": text, "gold": gold, "ignored": ignored}


def load_documents(source: Path = SOURCE) -> tuple[list[dict], dict]:
    audit = collections.Counter()
    raw = json.loads(source.read_text(encoding="utf-8"))
    documents = []
    for index, document in enumerate(raw):
        documents.append({
            "id": f"v3:{index:05d}",
            "sentences": [_normalise_sentence(sentence, audit) for sentence in document["sentence"]],
        })
    return documents, dict(audit)


def split_documents(documents: list[dict], seed: int = 42) -> tuple[dict[str, str], dict]:
    """Leakage-aware deterministic 72/8/20 document split.

    Exact duplicate documents and documents sharing a target-bearing sentence are kept
    in one connected component before assignment.
    """
    parents = list(range(len(documents)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left, right = find(left), find(right)
        if left != right:
            parents[max(left, right)] = min(left, right)

    seen: dict[tuple[str, str], int] = {}
    for index, document in enumerate(documents):
        keys = [("document", digest(json.dumps([s["text"] for s in document["sentences"]], ensure_ascii=False)))]
        keys.extend(("pii_sentence", digest(s["text"])) for s in document["sentences"] if s["gold"])
        for key in keys:
            if key in seen:
                union(index, seen[key])
            else:
                seen[key] = index

    groups: dict[int, list[int]] = collections.defaultdict(list)
    for index in range(len(documents)):
        groups[find(index)].append(index)

    shuffled = list(groups.values())
    rng = random.Random(seed)
    rng.shuffle(shuffled)
    shuffled.sort(key=len, reverse=True)
    ratios = {"train": 0.72, "valid": 0.08, "test": 0.20}
    counts = collections.Counter()
    assignment: dict[str, str] = {}
    total = len(documents)
    for group in shuffled:
        split = max(ratios, key=lambda name: ratios[name] * total - counts[name])
        counts[split] += len(group)
        for index in group:
            assignment[documents[index]["id"]] = split
    metadata = {
        "ratios": ratios,
        "document_counts": dict(counts),
        "connected_components": len(groups),
        "largest_component": max(map(len, groups.values())),
        "seed": seed,
    }
    return assignment, metadata


def prepare(output: Path = PREPARED, source: Path = SOURCE, seed: int = 42) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    documents, audit = load_documents(source)
    assignment, split_meta = split_documents(documents, seed)
    # Keep every exact sentence in one split only. Held-out copies take priority,
    # preventing train/validation duplicates from leaking into final evaluation.
    priority = {"train": 0, "valid": 1, "test": 2}
    owners: dict[str, str] = {}
    for document in documents:
        split = assignment[document["id"]]
        for sentence in document["sentences"]:
            key = digest(sentence["text"])
            if key not in owners or priority[split] > priority[owners[key]]:
                owners[key] = split

    rows, label_counts = [], collections.defaultdict(collections.Counter)
    sentence_hashes = collections.defaultdict(set)
    document_rows = []
    dropped = collections.Counter()
    for document in documents:
        split = assignment[document["id"]]
        kept_sentences = []
        for sent_index, sentence in enumerate(document["sentences"]):
            key = digest(sentence["text"])
            if owners[key] != split:
                dropped[f"duplicate_removed_from_{split}"] += 1
                continue
            row = {**sentence, "id": f"{document['id']}:{sent_index}", "document": document["id"],
                   "sent_index": sent_index, "split": split, "source": "KDPII-19", "domain": "KDPII"}
            rows.append(row)
            kept_sentences.append({**sentence, "sent_index": sent_index})
            sentence_hashes[split].add(key)
            label_counts[split].update(entity["label"] for entity in sentence["gold"])
        if kept_sentences:
            document_rows.append({"id": document["id"], "sentences": kept_sentences, "split": split})
        else:
            dropped[f"empty_document_after_dedup_{split}"] += 1
    overlaps = {}
    for left, right in (("train", "valid"), ("train", "test"), ("valid", "test")):
        overlaps[f"{left}_{right}"] = len(sentence_hashes[left] & sentence_hashes[right])
    if any(overlaps.values()):
        raise AssertionError(f"Cross-split sentence leakage: {overlaps}")

    with (output / "rows.jsonl").open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (output / "documents.jsonl").open("w", encoding="utf-8") as stream:
        for document in document_rows:
            stream.write(json.dumps(document, ensure_ascii=False) + "\n")
    write_json(output / "split.json", assignment)
    report = {
        "dataset": "KDPII-19",
        "source": str(source),
        "source_sha256": sha256_file(source),
        "target_labels": TYPES,
        "target_label_count": len(TYPES),
        "documents": len(documents),
        "sentences": len(rows),
        "split": split_meta,
        "labels": {name: dict(label_counts[name]) for name in ("train", "valid", "test")},
        "cross_split_exact_sentence_overlap": overlaps,
        "dropped": dict(dropped),
        "audit": audit,
    }
    write_json(output / "report.json", report)
    report["rows_sha256"] = sha256_file(output / "rows.jsonl")
    report["documents_sha256"] = sha256_file(output / "documents.jsonl")
    write_json(output / "report.json", report)
    return report


def align_bio(row: dict, offsets: list[tuple[int, int]]) -> list[int]:
    labels, active = [], set()
    for begin, end in offsets:
        if end <= begin:
            labels.append(-100)
            continue
        intersect = [(i, entity) for i, entity in enumerate(row["gold"])
                     if begin < entity["end"] and entity["begin"] < end]
        if intersect:
            if len(intersect) != 1:
                labels.append(-100)
                continue
            index, entity = intersect[0]
            if begin < entity["begin"] or end > entity["end"]:
                labels.append(-100)
                continue
            prefix = "I-" if index in active else "B-"
            labels.append(BIO_ID[prefix + entity["label"]])
            active.add(index)
        elif any(begin < ignored_end and ignored_begin < end for ignored_begin, ignored_end, _ in row["ignored"]):
            labels.append(-100)
        else:
            labels.append(0)
    return labels


def decode_bio(offsets: list[tuple[int, int]], tag_ids: list[int]) -> list[dict]:
    spans, start, end, kind = [], None, None, None

    def finish() -> None:
        nonlocal start, end, kind
        if start is not None:
            spans.append({"begin": start, "end": end, "label": kind})
        start, end, kind = None, None, None

    for (begin, stop), label_id in zip(offsets, tag_ids, strict=True):
        if stop <= begin:
            continue
        tag = BIO[label_id]
        if tag == "O":
            finish()
        elif tag.startswith("B-") or kind != tag[2:]:
            finish()
            start, end, kind = begin, stop, tag[2:]
        else:
            end = stop
    finish()
    return spans


def _scores(counts: collections.Counter) -> dict:
    tp, fp, fn = (counts[key] for key in ("tp", "fp", "fn"))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0
    return {"precision": precision, "recall": recall, "f1": f1, "tp": tp, "fp": fp, "fn": fn}


class ExactSpanMetrics:
    def __init__(self) -> None:
        self.total = collections.Counter()
        self.types = collections.defaultdict(collections.Counter)

    def add(self, truth: list[dict], prediction: list[dict]) -> None:
        key = lambda entity: (entity["begin"], entity["end"], entity["label"])
        gold, pred = {key(entity) for entity in truth}, {key(entity) for entity in prediction}
        for name, items in (("tp", gold & pred), ("fp", pred - gold), ("fn", gold - pred)):
            self.total[name] += len(items)
            for _, _, label in items:
                self.types[label][name] += 1

    def compute(self) -> dict:
        by_type = {label: _scores(self.types[label]) for label in TYPES}
        return {
            **_scores(self.total),
            "macro_f1": sum(item["f1"] for item in by_type.values()) / len(TYPES),
            "types": by_type,
            "matching": "exact character begin/end/type within the original sentence",
        }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=PREPARED)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output, args.source, args.seed), ensure_ascii=False, indent=2))
