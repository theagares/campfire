#!/usr/bin/env python3
"""Local-app PII inference entrypoint for the SKT CRF+Gaz Mix-all x3 ensemble.

Examples:
  python local_pii_ensemble_inference.py --ensemble-dir .. --text "홍길동 010-1234-5678"
  python local_pii_ensemble_inference.py --ensemble-dir .. --stdio

STDIO mode accepts one JSON object per line:
  {"id":"1","text":"홍길동 010-1234-5678"}
  {"id":"2","texts":["문장1","문장2"]}
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import torch

from gazetteer import aggregate_to_tokens
from local_pii_inference import PIIDetector, merge_lc_address, regex_postprocess


def _public_entity(entity: dict[str, Any], include_votes: bool = False) -> dict[str, Any]:
    out = {
        "form": entity["form"],
        "label": entity["label"],
        "begin": int(entity["begin"]),
        "end": int(entity["end"]),
    }
    if include_votes and "votes" in entity:
        out["votes"] = int(entity["votes"])
    return out


def _overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return int(left["begin"]) < int(right["end"]) and int(right["begin"]) < int(left["end"])


def ensemble_vote(
    pred_lists: list[list[list[dict[str, Any]]]],
    texts: list[str],
    min_votes: int,
    include_votes: bool = False,
) -> list[list[dict[str, Any]]]:
    """Cluster same-label overlapping spans and keep clusters detected by min_votes models."""
    if not pred_lists:
        return [[] for _ in texts]

    combined: list[list[dict[str, Any]]] = []
    n_models = len(pred_lists)

    for text_idx, text in enumerate(texts):
        candidates: list[tuple[int, dict[str, Any]]] = []
        for model_idx in range(n_models):
            for entity in pred_lists[model_idx][text_idx]:
                candidates.append((model_idx, entity))

        parent = list(range(len(candidates)))

        def find(idx: int) -> int:
            while parent[idx] != idx:
                parent[idx] = parent[parent[idx]]
                idx = parent[idx]
            return idx

        def union(left: int, right: int) -> None:
            root_left, root_right = find(left), find(right)
            if root_left != root_right:
                parent[root_left] = root_right

        for left_idx, (_, left_entity) in enumerate(candidates):
            for right_idx in range(left_idx + 1, len(candidates)):
                _, right_entity = candidates[right_idx]
                if left_entity["label"] != right_entity["label"]:
                    continue
                if _overlaps(left_entity, right_entity):
                    union(left_idx, right_idx)

        clusters: dict[int, list[int]] = defaultdict(list)
        for idx in range(len(candidates)):
            clusters[find(idx)].append(idx)

        voted_entities: list[dict[str, Any]] = []
        for members in clusters.values():
            model_votes = {candidates[idx][0] for idx in members}
            if len(model_votes) < min_votes:
                continue

            span_counts = Counter(
                (int(candidates[idx][1]["begin"]), int(candidates[idx][1]["end"]))
                for idx in members
            )
            best_span, _ = span_counts.most_common(1)[0]
            representative = None
            for model_idx in range(n_models):
                for idx in members:
                    candidate_model_idx, entity = candidates[idx]
                    if candidate_model_idx != model_idx:
                        continue
                    span = (int(entity["begin"]), int(entity["end"]))
                    if span == best_span:
                        representative = dict(entity)
                        break
                if representative is not None:
                    break

            if representative is None:
                representative = dict(candidates[members[0]][1])
            representative["votes"] = len(model_votes)
            voted_entities.append(representative)

        voted_entities.sort(key=lambda item: (int(item["begin"]), int(item["end"]), item["label"]))
        voted_entities = merge_lc_address(voted_entities, text)
        voted_entities = regex_postprocess(text, voted_entities)
        voted_entities.sort(key=lambda item: (int(item["begin"]), int(item["end"]), item["label"]))
        combined.append([_public_entity(entity, include_votes=include_votes) for entity in voted_entities])

    return combined


def _sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


class EnsemblePIIDetector:
    def __init__(
        self,
        model_dirs: list[str | Path],
        device: str = "cpu",
        batch_size: int = 4,
        max_length: int = 256,
        min_votes: int = 2,
        include_votes: bool = False,
    ):
        if len(model_dirs) < 2:
            raise ValueError("At least two model directories are required for ensemble inference.")
        if not 1 <= min_votes <= len(model_dirs):
            raise ValueError(f"min_votes must be between 1 and {len(model_dirs)}.")

        self.model_dirs = [Path(path) for path in model_dirs]
        self.min_votes = min_votes
        self.include_votes = include_votes
        self.detectors = [
            PIIDetector(path, device=device, batch_size=batch_size, max_length=max_length)
            for path in self.model_dirs
        ]
        self.device = self.detectors[0].device if self.detectors else device
        self.batch_size = batch_size
        self.max_length = max_length

        # seed 들이 완전히 동일한 tokenizer.json/gazetteer.json 을 쓰는지 확인한다(현재
        # 번들은 3-seed 모두 동일 — sha256 으로 검증). 동일하면 토크나이즈+가제티어
        # 트라이 매칭을 seed 개수만큼 반복하지 않고 한 번만 계산해 공유한다(실측:
        # 전체 PII 추론 시간의 ~72%가 가제티어 계산 중복이었음). 혹시 나중에 seed
        # 별로 다른 tokenizer/gazetteer 를 쓰는 번들로 바뀌면 자동으로 안전하게
        # (조금 느린) 기존 방식으로 폴백한다.
        self._shared_preprocessing = False
        try:
            tok_hashes = {_sha256_file(d / "tokenizer.json") for d in self.model_dirs}
            gaz_hashes = {_sha256_file(d / "gazetteer.json") for d in self.model_dirs if (d / "gazetteer.json").exists()}
            self._shared_preprocessing = len(tok_hashes) == 1 and len(gaz_hashes) <= 1
        except OSError:
            self._shared_preprocessing = False

    def _predict_shared(self, texts: list[str]) -> list[list[dict[str, Any]]]:
        """tokenizer/gazetteer 가 전 seed 동일할 때: 배치별로 토크나이즈+가제티어를
        한 번만 계산해 모든 seed 모델의 forward/CRF decode 에 재사용한다."""
        primary = self.detectors[0]
        tokenizer = primary.tokenizer
        gaz_trie = primary.gaz_trie
        target_labels = primary.target_labels
        device = self.device

        prepared_batches = []
        for start in range(0, len(texts), self.batch_size):
            batch_texts = texts[start:start + self.batch_size]
            encoded = tokenizer(
                batch_texts, truncation=True, max_length=self.max_length,
                padding=True, return_offsets_mapping=True, return_tensors="pt",
            )
            offset_mapping = encoded.pop("offset_mapping").tolist()
            word_ids_list = [encoded.word_ids(i) for i in range(len(batch_texts))]
            input_ids = encoded["input_ids"].to(device)
            attention_mask = encoded["attention_mask"].to(device)

            label_mask = torch.zeros(input_ids.shape, dtype=torch.bool, device=device)
            for batch_idx, offsets in enumerate(offset_mapping):
                seen_words = set()
                for token_idx, (word_id, (_, char_end)) in enumerate(zip(word_ids_list[batch_idx], offsets)):
                    if word_id is None or char_end == 0 or word_id in seen_words:
                        continue
                    seen_words.add(word_id)
                    label_mask[batch_idx, token_idx] = True

            gaz_features = None
            if gaz_trie is not None:
                gaz_array = np.zeros(
                    (len(batch_texts), input_ids.shape[1], len(target_labels)), dtype=np.float32
                )
                for batch_idx, text in enumerate(batch_texts):
                    char_features = gaz_trie.match_sentence(text)
                    token_features = aggregate_to_tokens(char_features, offset_mapping[batch_idx])
                    gaz_array[batch_idx, : token_features.shape[0]] = token_features
                gaz_features = torch.tensor(gaz_array, device=device)

            prepared_batches.append(
                (batch_texts, input_ids, attention_mask, label_mask, offset_mapping, word_ids_list, gaz_features)
            )

        pred_lists = []
        for detector in self.detectors:
            entities: list[list[dict[str, Any]]] = []
            for batch in prepared_batches:
                entities.extend(detector.predict_prepared(*batch))
            pred_lists.append(entities)
        return pred_lists

    def predict(self, texts: list[str]) -> list[list[dict[str, Any]]]:
        if self._shared_preprocessing:
            pred_lists = self._predict_shared(texts)
        else:
            pred_lists = [detector.predict(texts) for detector in self.detectors]
        return ensemble_vote(
            pred_lists,
            texts,
            min_votes=self.min_votes,
            include_votes=self.include_votes,
        )


def resolve_model_dirs(args: argparse.Namespace) -> list[Path]:
    if args.model_dirs:
        return [Path(path) for path in args.model_dirs]

    ensemble_dir = Path(args.ensemble_dir).resolve()
    models_dir = ensemble_dir / "models"
    model_dirs = [models_dir / seed for seed in ("seed42", "seed43", "seed44")]
    if all(path.is_dir() for path in model_dirs):
        return model_dirs

    discovered = sorted(path for path in models_dir.glob("seed*") if path.is_dir())
    if discovered:
        return discovered
    raise FileNotFoundError(f"No seed model directories found under {models_dir}")


def write_response(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run_stdio(detector: EnsemblePIIDetector) -> None:
    write_response({
        "ready": True,
        "device": detector.device,
        "models": [path.name for path in detector.model_dirs],
        "min_votes": detector.min_votes,
    })
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            texts = request.get("texts")
            single = False
            if texts is None:
                texts = [request["text"]]
                single = True
            predictions = detector.predict(texts)
            write_response({
                "id": request.get("id"),
                "entities": predictions[0] if single else predictions,
            })
        except Exception as exc:
            write_response({"id": None, "error": str(exc)})


def main() -> None:
    default_ensemble_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--ensemble-dir", default=str(default_ensemble_dir))
    parser.add_argument("--model-dirs", nargs="+")
    parser.add_argument("--text")
    parser.add_argument("--texts-json")
    parser.add_argument("--stdio", action="store_true")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--min-votes", type=int, default=2)
    parser.add_argument("--include-votes", action="store_true")
    args = parser.parse_args()

    model_dirs = resolve_model_dirs(args)
    detector = EnsemblePIIDetector(
        model_dirs,
        device=args.device,
        batch_size=args.batch_size,
        max_length=args.max_length,
        min_votes=args.min_votes,
        include_votes=args.include_votes,
    )
    if args.stdio:
        run_stdio(detector)
        return

    if args.texts_json:
        texts = json.loads(args.texts_json)
    elif args.text:
        texts = [args.text]
    else:
        parser.error("Pass --text, --texts-json, or --stdio.")

    predictions = detector.predict(texts)
    print(json.dumps({"entities": predictions[0] if args.text else predictions}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
