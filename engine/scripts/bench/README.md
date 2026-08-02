# 탐지 속도 벤치마크

PII 탐지 + 인젝션 탐지의 **탐지 시간만** 재는 하네스. 개선 단계(커밋)별로 같은
조건에서 다시 측정해 비교하기 위한 것이다. 결과 해석은 리포지토리 루트의
`속도_개선_단계별_벤치마크.md` 참고.

## 측정 범위

포함: 청크별 PII 탐지, 청크별 인젝션 1차 판정(EXAONE hybrid).

제외:

- **Solar Pro 3 인젝션 2단계 위치 특정** — `UPSTAGE_API_KEY` 를 비워
  `config.INJECTION_LOCALIZE_ENABLED=False` 로 만들어 코드 경로를 아예 타지 않게 한다.
- 모델 로딩/서브프로세스 기동 — 측정 시작 전에 끝내고, 참고용으로 `load_sec` 에 따로 기록.
- 문서 파싱, 마스킹, 결과 직렬화.

각 문서마다 `first_*`(로딩 직후 1회)와 `warm_*`(이후 N회 중앙값)을 따로 낸다.
워밍업 같은 "첫 요청" 개선은 `first_*` 에서만 드러나므로 둘을 나눠 봐야 한다.

## 파일

| 파일 | 역할 |
|---|---|
| `bench_local.py` | 로컬 탐지기(encoder + EXAONE) 측정. 단계 커밋으로 checkout 된 트리를 `--engine-dir` 로 받는다 |
| `bench_api.py` | 베이스라인1 — PII·인젝션을 전부 Solar Pro 3 로 하던 구성 재현 |
| `run_stages.sh` | 워크트리를 단계별 커밋으로 checkout 하며 `bench_local.py` 순회 |
| `gen_table.py` | 결과 JSON → 마크다운 표 |
| `results/`, `results_api.json` | 측정 원본 |

## 전제

- CUDA 가 되는 파이썬 환경(배포 번들의 `torch+cpu` 로도 돌아가지만 GPU 기준 수치와는 다르다).
- 모델 가중치가 설치돼 있어야 한다(`%LOCALAPPDATA%/Campfire/models`).
- `bench_local.py` 는 가중치를 건드리지 않는다. 단계별 런타임 코드만 스테이징
  디렉터리(`--models-dir`)로 복사하고, 가중치는 junction 으로 연결해 쓴다 —
  사용자가 설치해 둔 앱의 런타임 파일을 과거 버전으로 덮어쓰지 않기 위해서다.
- 가중치 위치는 커밋에 따라 두 가지 레이아웃이 있다(엔진 트리 안 → 설치 폴더 밖).
  `bench_local.py` 가 `app/models_sync.py` 존재 여부로 판별해 둘 다 처리한다.

## 실행

```bash
REPEATS=7 bash engine/scripts/bench/run_stages.sh
python engine/scripts/bench/gen_table.py
```
