#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  SUDO="sudo"
else
  SUDO=""
fi

echo "[fonts] installing CJK fonts for Playwright screenshots"
$SUDO apt-get update
$SUDO apt-get install -y \
  fontconfig \
  fonts-noto-cjk \
  fonts-noto-cjk-extra \
  fonts-noto-color-emoji \
  fonts-wqy-zenhei

echo "[fonts] rebuilding font cache"
$SUDO fc-cache -f

echo "[fonts] installed Chinese-capable fonts:"
fc-list :lang=zh family | sed -n '1,20p'
