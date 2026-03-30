#!/usr/bin/env bash

set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

if command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -ims .venv/bin/python -m streamlit run app.py --server.address 0.0.0.0 --server.port 8501
fi

exec .venv/bin/python -m streamlit run app.py --server.address 0.0.0.0 --server.port 8501
