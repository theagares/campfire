# confidential_engine — 기밀 탐지 리트리버

회사 KG 트리플을 문서가 말하는지 채점하는 A.X-Encoder 기반 모델 (`runtime/local_retriever_inference.py`).
엔진 쪽: `app/core/detectors/confidential/`. 설계·실험: `문서/리트리버/` (저장소 밖).

가중치는 앱 번들에 없다. 기밀 탐지를 켜면 앱이 `/models/fetch` 로 GitHub 릴리스 `models-v1` 의
`confidential_engine_v1.tar.gz` (압축 327MB, sha256 고정 — `app/adapters/http_api/models.py`)를 받아
`config.MODELS_ROOT/confidential_engine/models/` 에 푼다. 기밀 탐지가 꺼져 있으면 받지 않는다.

| 파일 | 내용 |
|---|---|
| `model.safetensors` | DocPair 전체 state_dict (fp16, 약 363MB) — 3단계 학습본 `docre_syn3` |
| `config.json` | A.X-Encoder(ModernBERT) 설정 |
| `tokenizer.json`, `tokenizer_config.json` | A.X 토크나이저 |

`docre_syn3`: 영어 Re-DocRED 원격지도 사전학습 → 한국어 번역 Re-DocRED + 회사 문서식 합성 데이터(술어 180개, 금액·날짜·수치
목적어, 생략된 주어)로 미세조정. 한국어 Re-DocRED 테스트 F1 76.80, 합성 테스트 학습 술어 재현율 0.998 · 처음 보는 술어 0.99
(δ −10). 약점: 상대 회사·인물이 주어인 값 사실은 여유값이 낮다 — KG 사실의 주어를 자사·자사 제품으로 두면 잘 잡는다.

가중치가 없으면 리트리버 없이 사전 매칭 후보를 전부 Solar 에 넘긴다 (`model_status.confidential_ready`).

다시 묶기 (models/ 아래 4개 파일):

```bash
tar -czf confidential_engine_v1.tar.gz -C confidential_engine models/model.safetensors models/config.json \
    models/tokenizer.json models/tokenizer_config.json
sha256sum confidential_engine_v1.tar.gz     # → models.py _ASSETS["confidential"]["sha256"]
gh release upload models-v1 confidential_engine_v1.tar.gz
```
