# EXAONE-4.0-1.2B Hybrid Injection Detector (segment-hidden, risk-calibrated)

backend LLM은 freeze, 이 폴더의 `hybrid/model.pt`(작은 MLP)만 학습됨. 다른 곳에서 재사용할 때
backend는 반드시 `extract_config.json`의 `model`(LGAI-EXAONE/EXAONE-4.0-1.2B)과 동일해야 함 —
attention/hidden feature의 의미와 차원이 그 모델 아키텍처에 묶여 있어서 backend를 바꾸면 이 모델은 못 씀.

## 측정 성능 (pooled, 8도메인, test n=6,400) — 이 폴더 그대로 재현된 숫자

| variant | Acc | FPR | FNR | risk(0.25·FPR+0.75·FNR) | 임계값 보정 | 비고 |
|---|---|---|---|---|---|---|
| attn (hidden 안 씀) | 0.979 | 0.0169 | 0.0056 | 0.0084 | 적용(bias -0.20) | hidden 추출 없이 attention만으로 동작, 더 간단 |
| **hybrid (권장)** | **0.992** | **0.0025** | **0.0066** | **0.0055** | **미적용(bias 0)** | attn + segment hidden, 최고 성능 |

비교: EXAONE-3.5-2.4B pooled(목표) = Acc 0.967 / FPR 0.006 / FNR 0.023 / risk 0.0188.
hybrid가 모든 지표에서 이김(risk 기준 **3.4배** 우수).

**hybrid는 왜 임계값 보정을 안 쓰나** — 체크포인트 선택 기준을 val-risk로 바꾼 것만으로 이미 risk 0.0055까지
내려가서, 보정을 추가로 걸어도 FPR은 완전히 동일(0.0025)하고 FNR은 test 3,200건 중 약 1건 차이
(0.0066→0.0063)에 그쳤다. 노이즈 수준의 이득 대비 "보정 적용을 빠뜨리면 성능이 달라진다"는 운영 리스크가
더 크다고 판단해 hybrid는 보정 없이(그냥 argmax) 쓰는 걸로 확정했다. attn은 보정 효과가 더 뚜렷해서
(risk 0.0088→0.0084) 그대로 적용 유지.

## 폴더 구성

```
extract_config.json     backend/추출 설정 (아래 참고)
attn/
  model.pt               AttnOnly 모델 state_dict
  norm_stats.pt           {pair_mu, pair_sd, hidden_mu, hidden_sd} — hidden_*는 attn엔 안 씀
  calibration.json        {"misaligned_bias": -0.20, "misaligned_class_id": 0}  ← 적용 필요
hybrid/                  ← 권장
  model.pt               Hybrid 모델 state_dict (attn_enc + hidden_enc + classifier 전부 포함)
  norm_stats.pt           {pair_mu, pair_sd, hidden_mu, hidden_sd}
  calibration.json        {"misaligned_bias": 0.0, "misaligned_class_id": 0}  ← 의도적으로 0, 그냥 argmax
```

## 추출 설정 (extract_config.json 요약, hybrid 기준)

- backend: `LGAI-EXAONE/EXAONE-4.0-1.2B` (trust_remote_code=True), attn_implementation="eager" 필수
- tool_msg_mode: "separate" (user_prompt와 tool_response를 별도 user 메시지 2개로)
- attention pairs: K=1024 토큰쌍 무작위 서브샘플(seed=42), LH=960 (L=30×H=32)
- hidden state: 레이어 [8, 15, 22, 30] × {head/mid/tail 3구간 평균, 마지막 토큰} = 16개 벡터 × 2048차원 = 32,768차원
- label: `{"misaligned": 0, "aligned": 1, "non_instruction": 2}`

## 추론 절차 (hybrid 기준)

1. backend로 EXAONE-4.0-1.2B를 eager attention으로 로드, `extract_config.json`과 동일한 방식으로
   (system, user_prompt, tool_response)를 넣어 forward pass 1회 → attention + hidden_states 획득
   (injection_diag의 `src/diag_common.py` + `src/extract_hybrid.py`의 추출 로직 그대로 재사용 권장)
2. 위 규격대로 pairs(K=1024, 960d)와 hidden(16×2048=32768d) 벡터 생성
3. `norm_stats.pt`의 pair_mu/pair_sd, hidden_mu/hidden_sd로 표준화(z-score)
4. `model.pt`를 `Hybrid(lh=960, hd=32768, dropout=0.2)` 구조(src/train_hybrid.py의 build_model 참고)에
   `load_state_dict`로 로드 후 forward → 3개 로짓(misaligned/aligned/non_instruction)
5. **hybrid는 그냥 argmax**(calibration.json의 bias=0이라 더해도 안 더해도 결과 동일, 형식상 파일만 유지).
   **attn을 쓴다면** misaligned(class 0) 로짓에 `attn/calibration.json`의 bias(-0.20)를 반드시 더한 뒤
   argmax — 이걸 빠뜨리면 attn의 raw 성능(FPR 0.0191/FNR 0.0053/risk 0.0088)만 나온다.

## 주의

- attn의 bias는 이 프로젝트의 validation set(8도메인 pooled, ~3,840건)과 목표 함수(0.25·FPR+0.75·FNR)에
  맞춰 보정된 값이다. 배포 환경의 injection 비율/비용 구조가 크게 다르면 그 환경 데이터로 같은 절차
  (validation 로짓에 -3~+3 bias를 스윕해 risk 최소점 찾기, `train_pooled_from_dumps.py`의 로직 참고)를
  재실행해 재보정 권장.
- backend를 다른 모델로 바꾸면 이 model.pt/norm_stats.pt는 전부 무효 — L·H·hidden_size가 다른 모델의
  attention/hidden과는 차원 자체가 안 맞음.
