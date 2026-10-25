"""
app/core/detectors/pii/keep.py
문맥 판단 PII 탐지기 — taehwan joint_masking direct_full (선택 기능, config.PII_KEEP_ENABLED).

켜고 모델(config.PII_KEEP_DIR)이 있으면 registry 가 v2(encoder.py) 대신 이 탐지기를 쓴다. 탐지와 개체별
MASK 확률을 이 모델 하나가 낸다(runtime/keep_scorer.py 하위 프로세스).
- maskProb 를 모든 탐지에 싣고, 풀어 줄 수 있는 종류(config.PII_KEEP_LABELS)이면서 임계값 미만이면 keep 을 단다.
  keep 은 확장 검토 화면이 해당 항목을 미리 풀어 두는 데만 쓴다 — maskedText 는 그대로 전부 가린다.
- 문장마다 v2 런타임의 후처리(주소 잇기 → 주소 보강 → 정규식)를 거친다(keep_scorer.postprocess). 다만 정규식은
  모델 구간을 줄이거나 지우지 않고 넓히거나 더하기만 한다 — v2 처럼 덮어쓰면 계좌 끝자리가 잘려 누출이 늘었다.
  라벨이 다르거나 정규식만 찾은 것은 점수 없이 항상 가린다.
  자격증명 정규식과 사용자 지정 강제 마스킹은 PII 모델과 별개 계층이라 그대로 돈다(orchestrator).
"""

from __future__ import annotations

import re

from app import config

from ..base import Detection
from .encoder import _SINGLE_MODEL_CONFIDENCE, LABEL_MAP, OTHER_PII, EncoderPiiDetector


_SENTENCE_END = re.compile(r"[.!?。]\s")


def split_chunks(text: str, size: int) -> list[tuple[str, int]]:
    """size 글자 이하로 자른다 — 줄바꿈, 없으면 문장 끝, 그것도 없으면 공백, 그것도 없으면 size 에서.

    예전엔 줄바꿈에서만 잘라 줄바꿈 없는 2만 자 글이 통째로 한 덩어리(= 학습 문서의 다섯 배 길이 문서)가 됐다.
    문장 끝을 공백보다 먼저 보는 건 여러 어절짜리 개체(주소 등)가 덩어리 경계에서 잘리지 않게 하려는 것이다.
    (덩어리, 시작 오프셋) 목록.
    """
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            window = text[start:end]
            cut = window.rfind("\n") + 1
            if not cut:
                ends = [m.end() for m in _SENTENCE_END.finditer(window)]
                cut = ends[-1] if ends else window.rfind(" ") + 1
            if cut:
                end = start + cut
        if text[start:end].strip():
            chunks.append((text[start:end], start))
        start = end
    return chunks


class KeepPiiDetector(EncoderPiiDetector):
    name = "pii_direct_full"
    kind = "pii"

    def _spawn_args(self) -> list[str]:
        return [
            config.PII_PYTHON_EXECUTABLE,
            str(config.PII_ENGINE_DIR / "runtime" / "keep_scorer.py"),
            "--keep-dir", str(config.PII_KEEP_DIR),
            # 토크나이저·백본 설정은 v2 번들 것을 쓴다(같은 A.X 어휘·오프셋)
            "--pii-model-dir", str(config.PII_ENGINE_DIR / "models" / config.PII_MODEL_SEED),
            "--device", config.PII_KEEP_DEVICE,
            "--stdio",
        ]

    async def _infer_batch(self, texts: list[str]) -> list[list[dict]]:
        reply = await self._rpc({"texts": texts})
        threshold = float(reply["threshold"])
        for entities in reply["entities"]:
            for e in entities:
                p = e["maskProb"]
                e["keep"] = p is not None and p < threshold and e["label"] in config.PII_KEEP_LABELS
        return reply["entities"]

    async def detect(self, text: str, *, meta=None) -> list[Detection]:
        chunks = split_chunks(text, config.PII_KEEP_CHUNK_CHARS)
        if not chunks:
            return []
        await self._ensure_process()
        out: list[Detection] = []
        try:
            for chunk, offset in chunks:  # 덩어리 하나 = 요청 하나(시간 상한 안에 끝나게)
                for e in (await self._infer_batch([chunk]))[0]:
                    start, end = offset + e["begin"], offset + e["end"]
                    det = Detection(type=LABEL_MAP.get(e["label"], OTHER_PII), start=start, end=end,
                                    text=text[start:end], confidence=_SINGLE_MODEL_CONFIDENCE, source="encoder",
                                    label=e["label"])
                    if e["maskProb"] is not None:
                        det["maskProb"] = e["maskProb"]
                    if e["keep"]:
                        det["keep"] = True
                    out.append(det)
        finally:
            self.residency.touch()
        return out


def build() -> KeepPiiDetector:
    return KeepPiiDetector()
