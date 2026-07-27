# 실 모델 아티팩트 배포 (PII / 인젝션)

`engine/app/models/`(git 미추적, `.gitignore` 처리)에 두는 두 번들에 대한 문서.
지금은 로컬 개발 환경 한 대에만 수동으로 복사돼 있는 상태 — 이 문서는 **GitHub
Releases로 배포하는 방법을 정리만 해둔 것**이고, 실제 업로드/자동 다운로드
스크립트는 아직 만들지 않았다(다음 단계).

## 어떤 모델이 왜 필요한가

| 위치 | 내용 | 크기 | 출처 |
|---|---|---|---|
| `app/models/pii_engine/` | skt/A.X-Encoder-base + CRF + gazetteer, 3-seed(seed42/43/44) 앙상블 | ~1.7GB | hwan님 GPU 서버(`123.37.28.197`, `/data/team/hwan/real/models/skt_crf_gaz_x3/mix_syn_all`), 로컬엔 `PIImodel/pii_skt_crf_gaz_mix_all_x3_local_app/pii_engine/`로 이미 받아져 있었음 |
| `app/models/injection_engine/` | EXAONE-3.5-2.4B-Instruct attention 특징 위에 얹은 regularized MLP 분류기(`enc_pooled_regularized.pt` 등) | ~26MB | 같은 서버, `injection_exaone_regularized_mlp_engine.tar.gz`를 SFTP로 받음 |

**EXAONE-3.5-2.4B-Instruct 자체(LLM 백본, ~5GB)는 이 배포 대상이 아니다** —
`transformers`가 최초 실행 시 HuggingFace Hub(`LGAI-EXAONE/EXAONE-3.5-2.4B-Instruct`,
공개 모델)에서 알아서 받아 `~/.cache/huggingface/`에 캐싱한다. 여기서 다루는 건
그 위에 우리가 따로 학습/이식한 **작은** 아티팩트(PII 인코더 전체, 인젝션 MLP
헤드)뿐이다.

## 왜 GitHub Releases인가

다른 옵션(HF private repo, Git LFS, 자체 오브젝트 스토리지, 팀 서버 직접 서빙)과
비교한 결론: 이 레포(`theagares/securedoc-gateway`)가 이미 있고 팀원 전원이
GitHub 접근 권한을 갖고 있으므로, **새 인증 체계(토큰 발급/배포/회수)를 따로
만들 필요가 없다**는 게 결정적 장점. 단점은 release 첨부파일 용량 제한(개당
대략 2GB) — `pii_engine`(1.7GB)은 아슬아슬하게 들어가지만, 혹시 넘으면 seed별로
쪼개면 된다(아래 "용량 초과 시" 참고).

## 배포 절차 (실행 전 — 계획만)

### 1. 번들 패키징

```bash
# PII 앙상블 (models/ 하위 seed42·43·44 + runtime/ 전체)
cd engine/app/models
tar -czf pii_engine_v1.tar.gz pii_engine/

# 인젝션 MLP 헤드 (exaone_4_0_1_2b 제외하고 exaone_3_5_2_4b_instruct 만 — 지금
# 로컬에 있는 것도 이 상태)
tar -czf injection_engine_v1.tar.gz injection_engine/
```

### 2. GitHub Release 생성 + 업로드

```bash
# 코드 릴리스와 별개로 "모델 전용" 태그를 쓴다(코드 버전과 모델 버전을 분리)
git tag models-v1
git push origin models-v1

gh release create models-v1 \
  --title "Model artifacts v1 (PII ensemble + injection MLP head)" \
  --notes "PII: skt/A.X-Encoder-base CRF+Gaz 3-seed ensemble. Injection: EXAONE-3.5-2.4B-Instruct regularized MLP head. EXAONE 백본 자체는 포함 안 함(HF에서 별도 자동 다운로드)." \
  pii_engine_v1.tar.gz \
  injection_engine_v1.tar.gz
```

### 3. 용량 초과 시 (2GB 제한에 걸리면)

`pii_engine`을 seed별로 쪼개서 3개 파일로 올리고, 받는 쪽에서 재조립:

```bash
for seed in seed42 seed43 seed44; do
  tar -czf pii_engine_${seed}_v1.tar.gz \
    -C engine/app/models/pii_engine \
    manifest.json README.md runtime models/${seed}
done
```
(단, `runtime/`이 각 tar에 중복 포함됨 — 압축률상 큰 문제는 아니지만, 더
깔끔하게 하려면 `runtime/`만 별도 4번째 파일로 분리해도 됨)

## 다운로드 절차 (계획만 — 아직 스크립트 없음)

팀원이 로컬에 처음 세팅할 때:

```bash
mkdir -p engine/app/models
cd engine/app/models

gh release download models-v1 --pattern "pii_engine_v1.tar.gz" --pattern "injection_engine_v1.tar.gz"
tar -xzf pii_engine_v1.tar.gz
tar -xzf injection_engine_v1.tar.gz
rm pii_engine_v1.tar.gz injection_engine_v1.tar.gz
```

레포가 private이면 `gh` CLI가 이미 로그인돼 있는 계정 권한을 그대로 쓰므로 별도
토큰 설정이 필요 없다(팀원이 이 레포에 접근 권한만 있으면 됨).

## 다음 단계 (아직 안 함)

- [ ] 실제로 `models-v1` 릴리스 만들고 두 번들 업로드
- [ ] `engine/scripts/fetch_models.sh`(또는 `.ps1`) 같은 원클릭 다운로드
      스크립트 작성 — 위 "다운로드 절차"를 자동화
- [ ] `README.md`/`BUILD_PROGRESS.md`에 "로컬 세팅 시 모델 받는 법" 링크 추가
- [ ] 모델이 갱신될 때(예: 인젝션 MLP 표준화 통계 재계산 후) `models-v2` 태그로
      올리고, `config.py`나 여기 문서에 현재 권장 버전을 명시하는 규칙 정하기
