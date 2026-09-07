#!/usr/bin/env bash
# 국내 네트워크가 있는 PC/서버에서 바로 돌리기 위한 스크립트 (macOS / Linux)
#
#   ./scripts/run_local.sh probe            # 연결·엔드포인트 진단
#   ./scripts/run_local.sh collect --dry-run --no-notion   # Notion 에 쓰지 않고 결과만
#   ./scripts/run_local.sh collect          # 실제 수집·적재
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "[!] .env 가 없습니다. .env.example 을 복사해 값을 채우세요:"
  echo "      cp .env.example .env"
  exit 1
fi

if [ ! -d .venv ]; then
  echo "[*] 가상환경을 만듭니다..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

export PYTHONPATH=src
exec python -m g2b_watch.cli "$@"
