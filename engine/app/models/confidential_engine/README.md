# confidential_engine — 기밀 탐지 리트리버

회사 KG 트리플을 문서가 말하는지 채점하는 A.X-Encoder 기반 모델 (`runtime/local_retriever_inference.py`).
엔진 쪽: `app/core/detectors/confidential/`. 설계·실험: `문서/리트리버/` (저장소 밖).

가중치는 앱 번들에 없다. `config.MODELS_ROOT/confidential_engine/models/` 에 둔다:

| 파일 | 내용 |
|---|---|
| `model.safetensors` | DocPair 전체 state_dict (fp16, 약 363MB) — 실험 모델 `docre_dds8_ft2_atgl` |
| `config.json` | A.X-Encoder(ModernBERT) 설정 |
| `tokenizer.json`, `tokenizer_config.json` | A.X 토크나이저 |

가중치가 없으면 리트리버 없이 사전 매칭 후보를 전부 Solar 에 넘긴다 (`model_status.confidential_ready`).
아직 `/models/fetch` 자동 다운로드 자산에 없다 — 릴리스 자산(`models-v1`)에 올리고 sha 를 고정하면 붙인다.
