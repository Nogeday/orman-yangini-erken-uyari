#!/usr/bin/env bash
# Linux/macOS sarmalayıcı: bash simulation/run.sh [--source demo ...]
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 simulation/run.py "$@"
