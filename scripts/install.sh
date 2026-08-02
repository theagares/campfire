#!/usr/bin/env bash
# campfire 원커맨드 설치 (macOS / Linux)
#   curl -fsSL https://raw.githubusercontent.com/theagares/campfire/main/scripts/install.sh | bash
#
# git clone 없이 GitHub 저장소를 tarball로 받아 엔진(Python venv)과 데스크탑(Electron)
# 의존성까지 한 번에 설치한다. 실행/패키징은 하지 않고, 마지막에 실행 명령만 안내한다.
#
# 참고: HWPX 파싱(pyhwpx)은 한/글 COM 자동화라 Windows 전용이다. 이 스크립트로
# 설치하면 HWPX만 "미지원"으로 처리되고 그 외 기능(TXT/PDF/DOCX/XLSX/PPTX 파싱,
# PII·인젝션 탐지·마스킹)은 동일하게 동작한다.

set -euo pipefail

REPO="theagares/campfire"
DEST="${SECUREDOC_INSTALL_DIR:-$HOME/campfire}"

if [ -e "$DEST" ]; then
  echo "설치 대상 폴더가 이미 있습니다: $DEST" >&2
  echo "기존 폴더를 지우거나, SECUREDOC_INSTALL_DIR 로 다른 경로를 지정한 뒤 다시 실행하세요." >&2
  exit 1
fi

for cmd in node npm python3 curl tar; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "필수 프로그램을 찾을 수 없습니다: $cmd" >&2
    echo "Node.js 22+ 와 Python 3.10+ 를 먼저 설치하세요." >&2
    exit 1
  fi
done

echo "소스 다운로드 중... ($REPO)"
TMP_TAR="$(mktemp -t campfire.XXXXXX)"
curl -fsSL "https://github.com/$REPO/archive/refs/heads/main.tar.gz" -o "$TMP_TAR"

mkdir -p "$DEST"
tar -xzf "$TMP_TAR" -C "$DEST" --strip-components=1
rm -f "$TMP_TAR"

cd "$DEST"

echo "엔진(Python) 의존성 설치 중..."
python3 -m venv engine/.venv
engine/.venv/bin/python -m pip install --upgrade pip --quiet
engine/.venv/bin/python -m pip install -e "engine[test]" --quiet

echo "데스크탑 앱(Electron) 의존성 설치 중..."
(cd desktop && npm install --no-fund --no-audit)

echo ""
echo "설치 완료: $DEST"
echo "실행하려면:"
echo "  cd \"$DEST/desktop\""
echo "  npm start"
