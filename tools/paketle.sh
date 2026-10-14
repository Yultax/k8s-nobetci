#!/usr/bin/env bash
# Kurulum sihirbazını ve kodu tek bir dosyada toplar: dist/nobetci-kurulum.sh
# Sunucuya yalnızca bu dosya taşınır; ilk çalıştırmada kod ~/.nobetci/ altına açılır.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p dist
out=dist/nobetci-kurulum.sh
{
  cat kur.sh
  echo "__ARSIV__"
  tar czf - --exclude=__pycache__ nobetci deploy Dockerfile .dockerignore | base64
} > "$out"
chmod +x "$out"
echo "$out ($(du -h "$out" | cut -f1))"
