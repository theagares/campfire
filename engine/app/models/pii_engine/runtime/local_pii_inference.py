#!/usr/bin/env python3
"""Local-app PII inference entrypoint for ax_char_boundary/direct_full
(A.X encoder + Gazetteer + CRF + 문자 경계 MLP + 개체별 MASK/KEEP head, seed42).

Modes:
  python local_pii_inference.py --model-dir ./model --text "홍길동 010-1234-5678"
  python local_pii_inference.py --model-dir ./model --stdio

STDIO mode accepts one JSON object per line:
  {"id":"1","text":"홍길동 010-1234-5678"}
  {"id":"2","texts":["문장1","문장2"]}

응답은 예전 모델과 같은 모양이다(KDPII 코드 label·begin·end·form) — gateway 라벨 매핑
(encoder.LABEL_MAP)과 아래 주소·정규식 보정을 그대로 쓰려고 번들 라벨(NAME…)을 코드로 되돌린다.
달라진 점:
  - 모델이 KEEP(가리지 않아도 됨)으로 판정한 개체는 내지 않는다.
  - score 는 MASK 확률이다. 도메인을 앱이 모르므로 번들의 6개 도메인 모두로 판정하고 최댓값을
    쓴다(하나라도 가리라고 하면 가린다). 인코더는 한 번만 돌고 판정 head 만 6번 돈다.
  - 정규식·주소 보정으로 더한 개체에는 score 가 없다 — 항상 가린다.
  - 번들은 CPU FP32 로만 검증됐다. FP16·MPS 는 CRF·문자 경계 수치가 달라질 수 있어 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import torch


# 이메일은 이 모델이 중간에서 자르는 일이 잦다(시연 문서 실측: "doyoon.kim@…" → "kim@…",
# "billing-check@paymint-support.com" → "paymint-support.com") — 앞부분이 그대로 나가므로 정규식이
# 전체를 잡는다. 다른 규칙과 겹치면 먼저 차지하도록 맨 앞에 둔다.
REGEX_RULES = [
    ("TMI_EMAIL", re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![A-Za-z0-9-])")),
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


def merge_lc_address(entities: list[dict[str, Any]], sentence: str) -> list[dict[str, Any]]:
    merged = []
    for entity in entities:
        if entity["label"] == "LC_ADDRESS" and merged and merged[-1]["label"] == "LC_ADDRESS":
            gap = sentence[merged[-1]["end"]:entity["begin"]]
            if gap and all(ch in " ,(" for ch in gap):  # "…12길 34, 101동 1203호" 처럼 쉼표·괄호로 끊긴 주소도 하나로
                merged[-1]["end"] = entity["end"]
                merged[-1]["form"] = sentence[merged[-1]["begin"]:merged[-1]["end"]]
                continue
        merged.append(entity)
    return merged


# 주소 보강 — 모델은 학습 데이터처럼 쉼표·괄호 없는 주소만 잘 잡는다. 실측(2026-10-20):
#   "…성미산로 12길 34, 101동 1203호" → 동·호를 놓침,  "…12길 34(101동 1203호)" → 주소를 통째로 놓침,
#   "성미산로12길 34, …" → "서울특별시" · "성미산로12길 34" 로 쪼개져 구·동·호를 놓침.
# 시/도부터 번지(·동·호)까지 정규식으로 찾아 모델 주소와 합치고, 주소 바로 뒤의 동·호를 붙인다.
# REGEX_RULES 처럼 모델 결과를 덮어쓰지 않는다 — 모델이 건물명까지 더 넓게 잡았으면 그쪽을 지킨다.
_UNIT = r"(?:[가-힣A-Za-z]+\s*)?(?:\d+동\s*)?\d+호"
ADDRESS_TAIL = re.compile(rf"(?:\s*\(\s*{_UNIT}\s*\)|(?:\s*,\s*|\s+){_UNIT})")
_SIDO = (r"(?:(?:서울|부산|대구|인천|광주|대전|울산|세종)(?:특별시|광역시|특별자치시|시)?"
         r"|(?:경기|강원|충청북|충청남|전라북|전라남|경상북|경상남|충북|충남|전북|전남|경북|경남|제주)(?:특별자치도|도)?)")
ADDRESS_RE = re.compile(
    _SIDO + r"(?:\s?[가-힣]+(?:시|군|구)){1,2}"
    r"\s?(?:[가-힣0-9]+(?:로|길)(?:\s?\d+번?(?:로|길))?|[가-힣0-9]+(?:동|읍|면|리))\s?\d+(?:-\d+)?"
    rf"{ADDRESS_TAIL.pattern}?"
)


def address_postprocess(sentence: str, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    addrs = [dict(e) for e in entities if e["label"] == "LC_ADDRESS"]
    others = [e for e in entities if e["label"] != "LC_ADDRESS"]
    for m in ADDRESS_RE.finditer(sentence):
        hit = [a for a in addrs if a["begin"] < m.end() and m.start() < a["end"]]
        addrs = [a for a in addrs if a not in hit] + [{
            "label": "LC_ADDRESS",
            "begin": min([m.start()] + [a["begin"] for a in hit]),
            "end": max([m.end()] + [a["end"] for a in hit]),
        }]
    for a in addrs:
        tail = ADDRESS_TAIL.match(sentence, a["end"])
        if tail:
            a["end"] = tail.end()
        a["form"] = sentence[a["begin"]:a["end"]]
    return sorted(others + addrs, key=lambda e: e["begin"])


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
    """batch_size·max_length 는 예전 CLI 와 맞추려고 받기만 한다 — 번들이 자기 config(256토큰·64 stride·8창)를 쓴다."""

    def __init__(
        self,
        model_dir: str | Path,
        device: str = "auto",
        dtype: str = "float32",
        batch_size: int = 8,
        max_length: int = 256,
    ):
        self.model_dir = Path(model_dir)
        self.batch_size = batch_size
        self.device = self._resolve_device(device)
        self.dtype = self._resolve_dtype(dtype, self.device)

        # network.py 가 members/ 아래 패키지를 sys.path 앞에 붙여 불러온다(번들 원본 그대로).
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import network
        from safetensors.torch import load_file
        from stagea.kdpii import LABEL_MAP

        self.domains = list(network.DOMAINS)
        self.codes = {name: code for code, name in LABEL_MAP.items()}
        cfg = json.loads((self.model_dir / "config.json").read_text(encoding="utf-8"))
        if (cfg.get("model"), cfg.get("condition")) != ("ax_char_boundary", "direct_full") or cfg["domains"] != self.domains:
            raise ValueError("Unsupported PII bundle: expected ax_char_boundary/direct_full with the trained domain order")
        self.threshold = float(cfg["mask_threshold"])
        checkpoint = self.model_dir / cfg["checkpoint"]
        expected = json.loads((self.model_dir / cfg["checkpoint_status"]).read_text(encoding="utf-8"))["checkpoint_sha256"]
        with checkpoint.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != expected:
                raise ValueError("PII checkpoint SHA-256 mismatch")
        stage_a = dict(cfg["stage_a"])
        for key in ("run_dir", "backbone_path"):
            stage_a[key] = str(self.model_dir / stage_a[key])
        self.model = network.Integrated({**cfg, "stage_a": stage_a}, [], "direct_full", device="cpu")
        self.model.load_state_dict(load_file(str(checkpoint), device="cpu"), strict=True)
        self.model.requires_grad_(False).eval().to(self.device)

    @staticmethod
    def _resolve_device(device: str) -> str:
        if device == "auto":
            return "cuda" if torch.cuda.is_available() else "cpu"
        if device not in {"cpu", "cuda"}:
            raise ValueError(f"Unsupported PII device: {device} (cpu, cuda, auto)")
        return device

    @staticmethod
    def _resolve_dtype(dtype: str, device: str) -> "torch.dtype":
        """요청과 무관하게 FP32 — 번들은 FP32 로만 검증됐다(값 검사는 예전 설정 호환용)."""
        if dtype not in {"float16", "float32"}:
            raise ValueError(f"Unsupported PII dtype: {dtype}")
        return torch.float32

    def _mask_probabilities(self, hidden: "torch.Tensor", data: dict, entities: list[dict]) -> list[float]:
        if not entities:
            return []
        best = None
        for domain in self.domains:
            output, _ = self.model.contextual(hidden, data, entities, domain)
            prob = output.float().sigmoid()
            best = prob if best is None else torch.maximum(best, prob)
        return best.tolist()

    @torch.inference_mode()
    def predict(self, texts: list[str]) -> list[list[dict[str, Any]]]:
        """texts 를 한 문서의 문장들로 넘긴다 — 같은 개체가 여러 창에 나오면 한 개체로 묶여 같은 판정을 받는다."""
        found: list[list[dict[str, Any]]] = [[] for _ in texts]
        rows = [{"sent_idx": str(i), "sentence": t} for i, t in enumerate(texts) if t.strip()]
        if rows:
            try:
                data = self.model.tokenize({"sentences": rows})
            except ValueError as exc:
                if "no tokens" not in str(exc):
                    raise
                data = None
            if data is not None:
                hidden, entities, _ = self.model.encode(data, predict=True)
                for entity, score in zip(entities, self._mask_probabilities(hidden, data, entities), strict=True):
                    if score < self.threshold:
                        continue  # KEEP
                    for m in entity["mentions"]:
                        found[int(m["sent_idx"])].append({
                            "form": m["form"], "label": self.codes[entity["entity_type"]],
                            "begin": m["begin"], "end": m["end"], "score": score,
                        })
        results = []
        for text, entities in zip(texts, found, strict=True):
            entities = merge_lc_address(sorted(entities, key=lambda e: (e["begin"], e["end"])), text)
            entities = address_postprocess(text, entities)
            entities = regex_postprocess(text, entities)
            entities.sort(key=lambda item: (item["begin"], item["end"], item["label"]))
            results.append(entities)
        return results


def write_response(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run_stdio(detector: PIIDetector) -> None:
    write_response({"ready": True, "device": detector.device, "dtype": str(detector.dtype)})
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
    parser.add_argument("--dtype", default="float32", choices=["float16", "float32"])
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-length", type=int, default=256)
    args = parser.parse_args()

    detector = PIIDetector(
        args.model_dir,
        device=args.device,
        dtype=args.dtype,
        batch_size=args.batch_size,
        max_length=args.max_length,
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
