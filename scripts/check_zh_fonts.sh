#!/usr/bin/env bash
set -euo pipefail

if ! command -v fc-list >/dev/null 2>&1; then
  echo "[fonts] fc-list not found; install fontconfig first"
  exit 1
fi

if fc-list :lang=zh family | grep -q .; then
  echo "[fonts] Chinese fonts detected"
  fc-list :lang=zh family | sed -n '1,20p'
  exit 0
fi

echo "[fonts] no Chinese fonts detected"
echo "[fonts] run: sudo bash scripts/install_playwright_fonts.sh"
exit 1
