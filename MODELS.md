# 실 모델 아티팩트 배포 (PII / 인젝션)

`engine/app/models/`(git 미추적, `.gitignore` 처리)에 두는 두 번들에 대한 문서.
과거엔 로컬 개발 환경에만 수동으로 복사돼 있었지만, 지금은 **GitHub Releases(`models-v1`
태그) 업로드 + 앱 내 자동 다운로드까지 실제로 구현되어 있다.**

## 어떤 모델이 왜 필요한가

| 위치 | 내용 | 크기(압축) | 출처 |
|---|---|---|---|
| `app/models/pii_engine/models/seed42/` | skt/A.X-Encoder-base + CRF + gazetteer, 단일 seed(seed42) | 499MB | hwan님 GPU 서버(`123.37.28.197`) → 3-seed 앙상블에서 단일 seed로 전환(하드링크 중복 버그 확인 후) |
| `app/models/injection_engine/{attn,hybrid}/` | EXAONE-4.0-1.2B 위 attention/hybrid 특징을 얹은 regularized MLP 분류기 | 34MB | 같은 팀, `injection_diag` 프로젝트 model_release |

**EXAONE-4.0-1.2B 백본 자체(bf16, ~2.4GB)는 이 배포 대상이 아니다** — `transformers`가
최초 실행 시 HuggingFace Hub(`LGAI-EXAONE/EXAONE-4.0-1.2B`, 공개 모델, 인증 불필요 확인됨)
에서 알아서 받아 `~/.cache/huggingface/`에 캐싱한다. 여기서 다루는 건 그 위에 우리가
따로 학습/이식한 **작은** 아티팩트(PII 인코더 전체, 인젝션 MLP 헤드)뿐이다.

## 왜 GitHub Releases인가

이 레포(`theagares/securedoc-gateway`)가 이미 있고 **public**이라 팀원뿐 아니라 일반
사용자도 인증 없이 릴리스 자산 URL에 바로 접근 가능하다 — 새 인증 체계(계정/토큰
발급·배포·회수)를 따로 만들 필요가 없다는 게 결정적 장점(HF Hub/자체 스토리지/Git LFS
대비 비교는 `모델_가중치_배포_방법_정리.md` 참고). 단일 seed 전환 이후 총 용량이
533MB(499MB+34MB)로 GitHub 자산당 제한(~2GB)에 전혀 안 걸려서, 예전에 걱정하던
"seed별 분할" 로직은 필요 없어졌다.

## 실제 배포 상태 (`models-v1`)

```
https://github.com/theagares/securedoc-gateway/releases/tag/models-v1
  pii_engine_v1.tar.gz        (sha256: dbf7d8e52bddc44bea869ca9280ff873875babce7ab7acd2ab67453e9ba7a386)
  injection_engine_v1.tar.gz  (sha256: 739b28d517ea2a853bd3fd04d9a2eeeb5afb579b9cfce9fb99283e8afc71a8c3)
```

패키징 방법(재현/버전 갱신 시 참고):

```bash
cd engine/app/models
tar -czf pii_engine_v1.tar.gz -C pii_engine models/seed42
tar -czf injection_engine_v1.tar.gz -C injection_engine attn hybrid
sha256sum pii_engine_v1.tar.gz injection_engine_v1.tar.gz

git tag models-v1 && git push origin models-v1
gh release create models-v1 --title "Model artifacts v1 (PII seed42 + injection hybrid/attn head)" \
  --notes "..." pii_engine_v1.tar.gz injection_engine_v1.tar.gz
```

## 다운로드 — 앱 내 자동화 (구현 완료)

`engine/app/adapters/http_api/models.py`가 두 엔드포인트를 제공한다:

- `GET /models/status` — `{pii:{ready}, injection:{ready}}`. 가중치 파일이 로컬에
  실제로 있는지(경로 존재 여부)만 확인한다.
- `POST /models/fetch` — 이미 있는 자산은 건너뛰고(멱등), 없는 것만 위 URL에서
  스트리밍 다운로드 → sha256 체크섬 검증 → `tarfile`로 안전하게(path traversal 검증)
  압축 해제. 기존 `job_registry`/`GET /jobs/{id}/events` 패턴을 그대로 재사용해
  진행률(`{"type":"progress","asset":...,"pct":...}`)을 폴링으로 확인할 수 있다.

데스크탑 앱(`desktop/`)은 설정 화면의 **"탐지 모델: rule_based / advanced"** 토글로
이 흐름을 수동으로도 쓸 수 있고(`renderer/app.js`, `main/ipc.js`), **이제는 advanced가
설치 직후 자동으로 적용되는 기본값이다**(`main/main.js`의 `ensureAdvancedModelsAutoSetup`):

1. 최초 spawn 은 항상 `rule_based`로 뜬다(가중치가 아직 없어도 100% 기동하는 안전한
   상태) — 엔진이 `running`이 되는 순간을 기다린다(`waitForEngineRunning`).
2. `GET /models/status` 확인 → 필요하면 `POST /models/fetch` 자동 호출, 진행률을
   `models:fetchProgress` 이벤트로 브로드캐스트(설정 모달이 닫혀 있어도 대시보드 상단
   전역 배너에 표시됨).
3. 다운로드 완료 → `piiDetector`/`injectionDetector`를 `encoder`/`llm_mcp`로 자동
   저장하고 엔진 재시작. 성공하면 `advancedAutoSetupDone: true`를 저장해 다음 실행부터
   재시도하지 않는다(실패 시엔 false로 남겨 다음 실행에서 다시 시도).
4. **GPU 없는 PC 안전장치**: advanced로 재시작한 뒤 `watchForAdvancedStartupFailure`가
   20초간 엔진 상태를 지켜본다 — `error` 상태로 떨어지면(GPU/CUDA 미탑재로 실 모델
   서브프로세스가 못 뜨는 경우 등) 자동으로 `rule_based`로 되돌리고 재시작해, 설치
   직후 기본값을 advanced로 강제해도 GPU 없는 환경에서 앱이 계속 정상 동작한다.

설정 화면의 토글은 이제 "선택 사항"이 아니라, 이 자동 흐름이 실패했을 때 사용자가
수동으로 재시도하거나(다시 advanced 선택+저장) 의도적으로 rule_based로 되돌리는
용도로 남아 있다.

**실측 검증**: 로컬 가중치를 지운 상태에서 `POST /models/fetch` → 다운로드/체크섬/
압축해제 → 원본과 byte-identical 재현 확인. 이후 `SECUREDOC_PII_DETECTOR=encoder
SECUREDOC_INJECTION_DETECTOR=llm_mcp`로 기동해 실제 문장("...김도윤...이메일은
doyoon.kim90@navermail.com...이전 지시는 모두 무시하고...")을 넣어 PERSON_NAME/EMAIL
(source=encoder)과 OTHER_INJECTION(source=llm)이 정확히 탐지되는 것까지 확인.

## 남은 것

- [ ] 설정 UI에 다운로드 실패 시 재시도 버튼(현재는 실패 메시지만 표시, 저장 취소됨.
  단, 설치 직후 자동 흐름은 다음 실행에서 알아서 재시도한다)
- [ ] 모델이 갱신될 때(`models-v2`) `engine/app/adapters/http_api/models.py`의
  `_ASSETS`(URL/sha256)를 갱신하는 절차 문서화
- [x] ~~GPU 없는 환경에서 `advanced` 선택 시 사용자에게 사전 경고~~ → 사전 경고
  대신 사후 자동 복귀로 해결(`watchForAdvancedStartupFailure`, 위 참고). 다만
  "사전에 GPU 유무를 감지해 아예 advanced 자동 전환을 건너뛰는" 더 빠른 경로는
  아직 없음(현재는 항상 한 번 advanced로 시도해보고 실패하면 되돌아감).
