#!/usr/bin/env bash
# Kurulum sihirbazını ve kodu tek bir dosyada toplar: dist/nobetci-kurulum.sh
# Sunucuya yalnızca bu dosya taşınır; ilk çalıştırmada kod ~/.nobetci/ altına açılır.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p dist
out=dist/nobetci-kurulum.sh
version=$(sed -nE 's/^__version__ = "([^"]+)"/\1/p' nobetci/__init__.py)
{
  sed -E "s/^VERSION=dev .*/VERSION=$version/" kur.sh
  echo "__ARSIV__"
  tar czf - --exclude=__pycache__ nobetci deploy Dockerfile .dockerignore | base64
} > "$out"
chmod +x "$out"
(cd dist && sha256sum nobetci-kurulum.sh > SHA256SUMS)
echo "$out ($(du -h "$out" | cut -f1)) · sürüm $version"
