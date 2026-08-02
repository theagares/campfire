#!/usr/bin/env bash
# 개선 단계별로 워크트리를 해당 커밋으로 checkout 하고 bench_local.py 를 돌린다.
#
# 이 스크립트는 자기가 들어있는 워크트리를 checkout 으로 옮겨 다닌다 — checkout 에
# 휩쓸려 bench_local.py 가 사라지지 않도록 미리 임시 폴더로 복사해서 쓴다.
#
# 사용법:
#   BENCH_PYTHON=<CUDA 파이썬> BENCH_MODELS_DIR=<스테이징 모델 디렉터리> \
#     REPEATS=7 bash run_stages.sh <문서1> [문서2 ...]
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WT="$(git -C "$HERE" rev-parse --show-toplevel)"
BRANCH="${BRANCH:-$(git -C "$WT" branch --show-current)}"

PY="${BENCH_PYTHON:?BENCH_PYTHON 에 CUDA 파이썬 실행파일 경로를 지정하세요}"
MODELS="${BENCH_MODELS_DIR:?BENCH_MODELS_DIR 에 스테이징 모델 디렉터리를 지정하세요}"
REPEATS="${REPEATS:-7}"
if [ "$#" -eq 0 ]; then
  echo "문서 경로를 인자로 넘기세요" >&2
  exit 2
fi
DOCS=("$@")

OUT="$HERE/results"
mkdir -p "$OUT"

TMP="$(mktemp -d)"
cp "$HERE/bench_local.py" "$TMP/bench_local.py"
trap 'rm -rf "$TMP"' EXIT

# 단계 정의: <이름>|<커밋>
STAGES=(
  "ref_3seed|155c9bd~1"
  "B2_single_seed|155c9bd"
  "S1_warmup|72471d8"
  "S2_solar_cache|5a0c32b"
  "S3_aho_corasick|e0a02b7"
  "S4_chunk_concurrent|72e8699"
  "S5_hf_offline|e4ce280"
  "S6_crf_cpu|786e8e3"
  "HEAD_current|$BRANCH"
)

for entry in "${STAGES[@]}"; do
  name="${entry%%|*}"
  commit="${entry##*|}"
  echo "=============================================================="
  echo "[stage] $name  ($commit)"
  echo "=============================================================="

  git -C "$WT" checkout --detach --force "$commit" >/dev/null 2>&1 || {
    echo "  !! checkout 실패: $commit"; continue; }

  # 이전 단계 바이트코드 제거 (stale .pyc 방지)
  find "$WT/engine" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null

  "$PY" "$TMP/bench_local.py" \
    --stage "$name" --commit "$commit" \
    --engine-dir "$WT/engine" \
    --models-dir "$MODELS" \
    --device cuda --repeats "$REPEATS" \
    --docs "${DOCS[@]}" \
    --out "$OUT/$name.json" > "$OUT/$name.log" 2>&1

  if [ $? -eq 0 ]; then
    echo "  OK -> $OUT/$name.json"
  else
    echo "  !! 실패 — 로그 마지막 20줄:"
    tail -20 "$OUT/$name.log"
  fi
done

# 워크트리를 원래 브랜치로 되돌린다
git -C "$WT" checkout --force "$BRANCH" >/dev/null 2>&1
echo "완료. 결과: $OUT"
