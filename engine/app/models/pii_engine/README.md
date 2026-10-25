# PII 엔진 (pii_direct_char_boundary_seed42)

A.X-Encoder 백본에 Gazetteer·CRF·문자 경계 MLP를 붙여 개체를 찾고, 개체마다 MASK/KEEP 확률을 내는 direct head로 가릴지 정한다(`ax_char_boundary / direct_full / seed42`). 관계 추론은 하지 않는다.

## 구성

```text
pii_engine/
  manifest.json
  models/direct_char_boundary_seed42/   # 릴리스 에셋 pii_engine_v3.tar.gz (가중치·토크나이저·Gazetteer)
    config.json  checkpoint/  assets/backbone/  assets/stage_a/
  runtime/                              # 앱과 함께 배포 (번들 원본 추론 코드 + 감싸는 진입점)
    local_pii_inference.py              # Campfire stdio sidecar 진입점
    network.py  members/  repo/         # 번들 runtime/ 그대로 (원본 대비 바꾼 점: RUNTIME_CHANGES.json)
```

## 앱과의 약속 (local_pii_inference.py)

- stdio JSONL, 요청 `{"id","text"}` 또는 `{"id","texts":[...]}`, 응답 `{"id","entities":[{"form","label","begin","end","score"?}]}`. 첫 줄은 `{"ready":true,"device","dtype"}`.
- `label` 은 KDPII 19종 코드(`PS_NAME`, `QT_MOBILE` …) — 번들 라벨(`NAME` …)을 되돌린 것이다.
- `texts` 는 한 문서의 문장들로 넘긴다. 같은 개체가 여러 창에 나오면 한 개체로 묶여 같은 판정을 받는다.
- MASK 확률은 6개 도메인 모두로 계산해 최댓값을 쓴다. 0.1 미만(KEEP)인 개체는 내지 않는다. 인코더는 한 번만 돈다.
- 주소 보강·정규식(이메일·전화·주민번호·외국인번호·면허·카드·계좌·차량번호)은 모델 결과 위에 덧붙이고 항상 가린다(`score` 없음).
- CPU 또는 CUDA, 항상 FP32. 번들 검증이 FP32 뿐이라 FP16·MPS 는 쓰지 않는다.
- 위치는 Python 코드포인트 기준, `end` 는 포함하지 않는다.

## 검증 (manifest.json `verified_environments`)

엔진과 같은 버전(Python 3.11, torch 2.14.1, transformers 5.20.2)과 Intel Mac 버전(torch 2.2.2, transformers 4.57.6, numpy 1.26)에서 번들 `verify_bundle.py` 를 통과했다(원본 대비 확률 차이 0). 두 조합에서 이 래퍼의 시연 문서 결과도 스팬이 모두 같았다.

```bash
cd pii_engine/runtime
python local_pii_inference.py --model-dir ../models/direct_char_boundary_seed42 --device cpu --text "홍길동 고객님의 연락처는 010-1234-5678 입니다."
```
