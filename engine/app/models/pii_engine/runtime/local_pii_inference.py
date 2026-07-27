#!/usr/bin/env python3
"""Local-app PII inference entrypoint for the SKT + Gaz + CRF model.

Modes:
  python local_pii_inference.py --model-dir ./model --text "홍길동 010-1234-5678"
  python local_pii_inference.py --model-dir ./model --stdio

STDIO mode accepts one JSON object per line:
  {"id":"1","text":"홍길동 010-1234-5678"}
  {"id":"2","texts":["문장1","문장2"]}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np
import torch
from transformers import AutoTokenizer

from gazetteer import GazetteerTrie, aggregate_to_tokens, load_gazetteer
from pii_model import TokenClassifierForPII


REGEX_RULES = [
    ("QT_DRIVER_NUMBER", re.compile(r"(?<!\d)\d{2}-\d{2}-\d{6}-\d{2}(?!\d)")),
    ("QT_CARD_NUMBER", re.compile(r"(?<!\d)\d{4}[\-\s]\d{4}[\-\s]\d{4}[\-\s]\d{4}(?!\d)")),
    ("QT_RESIDENT_NUMBER", re.compile(r"(?<!\d)\d{6}-[1-4]\d{6}(?!\d)")),
    ("QT_ALIEN_NUMBER", re.compile(r"(?<!\d)\d{6}-[5-9]\d{6}(?!\d)")),
    ("QT_ALIEN_NUMBER", re.compile(r"(?<![A-Z0-9])[A-Z]\d{12}(?![A-Z0-9])")),
    ("QT_MOBILE", re.compile(r"(?<!\d)01[016789][\-\s]?\d{3,4}[\-\s]?\d{4}(?!\d)")),
    ("QT_PHONE", re.compile(r"(?<!\d)0(?:2|[3-9]\d)[\-]\d{3,4}[\-]\d{4}(?!\d)")),
    (
        "QT_PLATE_NUMBER",
        re.compile(
            r"(?:(?:서울|경기|경남|경북|전남|전북|충남|충북|강원|울산|부산|대구|인천|광주|대전|세종|제주"
            r"|경상남도|경상북도|전라남도|전라북도|충청남도|충청북도|강원도"
            r"|서울특별시|부산광역시|대구광역시|인천광역시|광주광역시|대전광역시|울산광역시)\s+)?"
            r"\d{2,3}[가나다라마바사아자차카타파하거너더러머버서어저처커터퍼허고노도로모보소오조초코토포호구누두루무부수우주]\s?\d{4}(?!\d)"
        ),
    ),
    ("QT_ACCOUNT_NUMBER", re.compile(r"(?<!\d)\d{3,4}[\-]\d{3,4}[\-]\d{3,6}(?!\d)")),
]


def char_bio_to_entities(sentence: str, char_labels: list[str]) -> list[dict[str, Any]]:
    entities = []
    start = cur_label = None
    i = 0
    while i < len(char_labels):
        tag = char_labels[i]
        if tag.startswith("B-"):
            if start is not None:
                entities.append({"form": sentence[start:i], "label": cur_label, "begin": start, "end": i})
            start, cur_label = i, tag[2:]
        elif tag.startswith("I-") and cur_label == tag[2:]:
            pass
        elif tag == "O" and cur_label and i < len(sentence) and sentence[i] == " ":
            j = i + 1
            while j < len(char_labels) and char_labels[j] == "O" and j < len(sentence) and sentence[j] == " ":
                j += 1
            if j < len(char_labels) and char_labels[j] == f"I-{cur_label}":
                i = j
                continue
            entities.append({"form": sentence[start:i], "label": cur_label, "begin": start, "end": i})
            start = cur_label = None
        else:
            if start is not None:
                entities.append({"form": sentence[start:i], "label": cur_label, "begin": start, "end": i})
            start = cur_label = None
        i += 1
    if start is not None:
        entities.append({"form": sentence[start:], "label": cur_label, "begin": start, "end": len(char_labels)})
    return entities


def merge_lc_address(entities: list[dict[str, Any]], sentence: str) -> list[dict[str, Any]]:
    merged = []
    for entity in entities:
        if entity["label"] == "LC_ADDRESS" and merged and merged[-1]["label"] == "LC_ADDRESS":
            gap = sentence[merged[-1]["end"]:entity["begin"]]
            if gap and all(ch == " " for ch in gap):
                merged[-1]["end"] = entity["end"]
                merged[-1]["form"] = sentence[merged[-1]["begin"]:merged[-1]["end"]]
                continue
        merged.append(entity)
    return merged


def regex_postprocess(sentence: str, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hits = []
    claimed: list[tuple[int, int]] = []

    def overlaps_claimed(begin: int, end: int) -> bool:
        return any(begin < other_end and other_begin < end for other_begin, other_end in claimed)

    def overlaps(a0: int, a1: int, b0: int, b1: int) -> bool:
        return a0 < b1 and b0 < a1

    for label, pattern in REGEX_RULES:
        for match in pattern.finditer(sentence):
            begin, end = match.start(), match.end()
            if overlaps_claimed(begin, end):
                continue
            hits.append({"form": match.group(), "label": label, "begin": begin, "end": end})
            claimed.append((begin, end))

    if not hits:
        return entities
    kept = [e for e in entities if not any(overlaps(e["begin"], e["end"], h["begin"], h["end"]) for h in hits)]
    return kept + hits


class PIIDetector:
    def __init__(
        self,
        model_dir: str | Path,
        device: str = "auto",
        batch_size: int = 8,
        max_length: int = 256,
    ):
        self.model_dir = Path(model_dir)
        self.batch_size = batch_size
        self.max_length = max_length
        self.device = self._resolve_device(device)
        self.label_map = json.loads((self.model_dir / "label_map.json").read_text(encoding="utf-8"))
        self.target_labels = self.label_map["target_labels"]
        self.id2label = {int(k): v for k, v in self.label_map["id2label"].items()}

        self.tokenizer = AutoTokenizer.from_pretrained(str(self.model_dir), trust_remote_code=True, use_fast=True)
        if not self.tokenizer.is_fast:
            raise RuntimeError("Fast tokenizer is required for offset mapping.")

        self.model = TokenClassifierForPII.from_pretrained(str(self.model_dir), trust_remote_code=True)
        self.model = self.model.to(torch.float32).to(self.device)
        self.model.eval()

        self.gaz_trie = None
        if self.label_map.get("use_gazetteer"):
            self.gaz_trie = GazetteerTrie(load_gazetteer(str(self.model_dir / "gazetteer.json")), self.target_labels)

    @staticmethod
    def _resolve_device(device: str) -> str:
        if device != "auto":
            return device
        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    @torch.inference_mode()
    def predict(self, texts: list[str]) -> list[list[dict[str, Any]]]:
        all_entities: list[list[dict[str, Any]]] = []
        for start in range(0, len(texts), self.batch_size):
            batch_texts = texts[start:start + self.batch_size]
            encoded = self.tokenizer(
                batch_texts,
                truncation=True,
                max_length=self.max_length,
                padding=True,
                return_offsets_mapping=True,
                return_tensors="pt",
            )
            offset_mapping = encoded.pop("offset_mapping").tolist()
            word_ids_list = [encoded.word_ids(i) for i in range(len(batch_texts))]
            input_ids = encoded["input_ids"].to(self.device)
            attention_mask = encoded["attention_mask"].to(self.device)

            label_mask = torch.zeros(input_ids.shape, dtype=torch.bool, device=self.device)
            for batch_idx, offsets in enumerate(offset_mapping):
                seen_words = set()
                for token_idx, (word_id, (_, char_end)) in enumerate(zip(word_ids_list[batch_idx], offsets)):
                    if word_id is None or char_end == 0 or word_id in seen_words:
                        continue
                    seen_words.add(word_id)
                    label_mask[batch_idx, token_idx] = True

            gaz_features = None
            if self.gaz_trie is not None:
                gaz_array = np.zeros(
                    (len(batch_texts), input_ids.shape[1], len(self.target_labels)),
                    dtype=np.float32,
                )
                for batch_idx, text in enumerate(batch_texts):
                    char_features = self.gaz_trie.match_sentence(text)
                    token_features = aggregate_to_tokens(char_features, offset_mapping[batch_idx])
                    gaz_array[batch_idx, : token_features.shape[0]] = token_features
                gaz_features = torch.tensor(gaz_array, device=self.device)

            decoded = self.model.predict_tags(input_ids, attention_mask, label_mask, gaz_features=gaz_features)

            for batch_idx, text in enumerate(batch_texts):
                word_span: dict[int, list[int]] = {}
                word_order = []
                for word_id, (char_start, char_end) in zip(word_ids_list[batch_idx], offset_mapping[batch_idx]):
                    if word_id is None or char_end == 0:
                        continue
                    if word_id not in word_span:
                        word_span[word_id] = [char_start, char_end]
                        word_order.append(word_id)
                    else:
                        word_span[word_id][0] = min(word_span[word_id][0], char_start)
                        word_span[word_id][1] = max(word_span[word_id][1], char_end)

                char_labels = ["O"] * len(text)
                for idx, word_id in enumerate(word_order):
                    if idx >= len(decoded[batch_idx]):
                        break
                    tag = self.id2label.get(decoded[batch_idx][idx], "O")
                    if tag == "O":
                        continue
                    word_start, word_end = word_span[word_id]
                    cont_tag = "I-" + tag[2:] if tag.startswith("B-") else tag
                    for pos, char_idx in enumerate(range(word_start, min(word_end, len(text)))):
                        char_labels[char_idx] = tag if pos == 0 else cont_tag

                entities = char_bio_to_entities(text, char_labels)
                entities = merge_lc_address(entities, text)
                entities = regex_postprocess(text, entities)
                entities.sort(key=lambda item: (item["begin"], item["end"], item["label"]))
                all_entities.append(entities)
        return all_entities

    @torch.inference_mode()
    def predict_prepared(
        self,
        batch_texts: list[str],
        input_ids: "torch.Tensor",
        attention_mask: "torch.Tensor",
        label_mask: "torch.Tensor",
        offset_mapping: list[list[tuple[int, int]]],
        word_ids_list: list[list[int | None]],
        gaz_features: "torch.Tensor | None",
    ) -> list[list[dict[str, Any]]]:
        """이미 토크나이즈 + 가제티어 계산이 끝난 입력으로 모델 forward/CRF decode/후처리만
        수행한다. 앙상블(EnsemblePIIDetector)이 동일한 tokenizer/gazetteer 를 쓰는 여러
        seed 모델에 걸쳐 토크나이즈·가제티어 계산을 한 번만 하고 공유할 때 쓴다 —
        가제티어 트라이 매칭이 seed 개수만큼 중복 계산되던 것을 없애 전체 시간을
        크게 줄인다(실측: 전체 PII 시간의 ~72%가 가제티어 계산이었음).
        """
        decoded = self.model.predict_tags(input_ids, attention_mask, label_mask, gaz_features=gaz_features)
        all_entities: list[list[dict[str, Any]]] = []
        for batch_idx, text in enumerate(batch_texts):
            word_span: dict[int, list[int]] = {}
            word_order = []
            for word_id, (char_start, char_end) in zip(word_ids_list[batch_idx], offset_mapping[batch_idx]):
                if word_id is None or char_end == 0:
                    continue
                if word_id not in word_span:
                    word_span[word_id] = [char_start, char_end]
                    word_order.append(word_id)
                else:
                    word_span[word_id][0] = min(word_span[word_id][0], char_start)
                    word_span[word_id][1] = max(word_span[word_id][1], char_end)

            char_labels = ["O"] * len(text)
            for idx, word_id in enumerate(word_order):
                if idx >= len(decoded[batch_idx]):
                    break
                tag = self.id2label.get(decoded[batch_idx][idx], "O")
                if tag == "O":
                    continue
                word_start, word_end = word_span[word_id]
                cont_tag = "I-" + tag[2:] if tag.startswith("B-") else tag
                for pos, char_idx in enumerate(range(word_start, min(word_end, len(text)))):
                    char_labels[char_idx] = tag if pos == 0 else cont_tag

            entities = char_bio_to_entities(text, char_labels)
            entities = merge_lc_address(entities, text)
            entities = regex_postprocess(text, entities)
            entities.sort(key=lambda item: (item["begin"], item["end"], item["label"]))
            all_entities.append(entities)
        return all_entities


def write_response(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run_stdio(detector: PIIDetector) -> None:
    write_response({"ready": True, "device": detector.device})
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--text")
    parser.add_argument("--texts-json")
    parser.add_argument("--stdio", action="store_true")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-length", type=int, default=256)
    args = parser.parse_args()

    detector = PIIDetector(args.model_dir, device=args.device, batch_size=args.batch_size, max_length=args.max_length)
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
