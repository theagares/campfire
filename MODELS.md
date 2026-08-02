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

이 레포(`theagares/campfire`)가 이미 있고 **public**이라 팀원뿐 아니라 일반
사용자도 인증 없이 릴리스 자산 URL에 바로 접근 가능하다 — 새 인증 체계(계정/토큰
발급·배포·회수)를 따로 만들 필요가 없다는 게 결정적 장점(HF Hub/자체 스토리지/Git LFS
대비 비교는 `모델_가중치_배포_방법_정리.md` 참고). 단일 seed 전환 이후 총 용량이
533MB(499MB+34MB)로 GitHub 자산당 제한(~2GB)에 전혀 안 걸려서, 예전에 걱정하던
"seed별 분할" 로직은 필요 없어졌다.

## 실제 배포 상태 (`models-v1`)

```
https://github.com/theagares/campfire/releases/tag/models-v1
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

**룰베이스 폴백은 완전히 제거했다** — `pii: encoder`, `injection: llm_mcp`가 유일한
detector 다(`engine/app/core/detectors/registry.py`). 설정 화면에도 더 이상 고를
토글이 없다(예전엔 "탐지 모델: rule_based / advanced" 토글이 있었으나 삭제).

가중치가 아직 안 받아진 상태에서 `detect()`를 부르면 서브프로세스가 로딩에 실패해
예외로 죽으므로, 파이프라인(`app/core/pipeline/orchestrator.py`)이 매 요청마다
`app.core.model_status`로 먼저 준비 여부를 확인한다 — 준비 안 됐으면 탐지를 생략하고
**검사 없이 그대로 통과**시킨다(`scanStatus: "models_not_ready"`, §9.2 파싱 실패/미지원과
같은 경로). 조용히 룰베이스로 격하하는 대신, "모델이 없으면 아예 검사하지 않는다"가
지금의 정책이다.

데스크탑 앱(`desktop/main/main.js`의 `ensureModelsAutoDownload`)은 이 가중치 다운로드만
자동으로 트리거한다:

1. 엔진이 `running`이 되는 순간을 기다린다(`waitForEngineRunning`) — 가중치가 없어도
   엔진 자체는 정상 기동한다(생성자는 가중치를 요구하지 않고, 실제 로딩은 검사 요청
   시점에 지연 실행된다).
2. `GET /models/status` 확인 → 필요하면 `POST /models/fetch` 자동 호출, 진행률을
   `models:fetchProgress` 이벤트로 브로드캐스트(설정 모달이 닫혀 있어도 대시보드 상단
   전역 배너에 표시됨).
3. 다운로드가 끝나면 **엔진 재시작이 필요 없다** — 그다음 실제 검사 요청에서
   detector 가 알아서 실 모델 서브프로세스를 스폰한다(예전엔 `piiDetector`/
   `injectionDetector`를 `encoder`/`llm_mcp`로 바꾸고 엔진을 재시작하는 2단계였는데,
   이제 이 값은 항상 고정이라 그 단계 자체가 없어졌다).

다운로드 실패 시엔 다음 실행에서 다시 시도한다(`advancedAutoSetupDone` 같은 플래그
없이, 매번 실제 `/models/status`로 확인).

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
- [x] ~~GPU 없는 환경에서 advanced 기동 실패 시 rule_based로 자동 복귀~~ → 룰베이스
  자체를 없애면서 무의미해짐. encoder/injection 은 cuda→mps→cpu 순으로 디바이스를
  고르고(`local_pii_inference.py`, `local_injection_hybrid_inference.py`) CPU
  에서도 항상 뜨므로, GPU 없는 환경도 그냥 느리게 동작한다 — "안 뜨는" 상황
  자체가 거의 없고, 설사 있어도 model_status 게이트가 검사를 생략하고 통과시킨다.
