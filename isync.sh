#!/usr/bin/env bash
# iSync - macOS / Linux başlatıcı
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "[iSync] İlk kurulum yapılıyor..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip -q
  .venv/bin/python -m pip install -r requirements.txt -q
fi
exec .venv/bin/python -m isync "$@"
