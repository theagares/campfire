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

**주의 — 지금 가중치는 영어(Re-DocRED)로만 학습했다.** 2026-10-11 한국어 예문 실측에서 모든 후보의
여유값이 −3 ~ −23 으로 나와(가장 기밀인 "개발 예산"이 −23) δ 를 넘는 후보가 없었고, 그래서 Solar 까지
가지 않아 탐지 0건이었다. 같은 예문을 가중치 없이(Solar 만) 돌리면 기밀 두 문장을 맞게 잡고 공개 사실은
걸렀다. 한국어 리트리버가 준비될 때까지는 가중치를 두지 않는 쪽이 안전하다.
아직 `/models/fetch` 자동 다운로드 자산에 없다 — 릴리스 자산(`models-v1`)에 올리고 sha 를 고정하면 붙인다.
