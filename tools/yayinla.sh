#!/usr/bin/env bash
# Yeni sürüm yayınlar: testler → paket → etiket → GitHub release (kurulum dosyası + SHA256SUMS).
# Önce nobetci/__init__.py içindeki __version__ değerini artırın.
# Sunucuda güncellemek için: ./nobetci-kurulum.sh guncelle
set -euo pipefail
cd "$(dirname "$0")/.."
version=$(sed -nE 's/^__version__ = "([^"]+)"/\1/p' nobetci/__init__.py)
[ -z "$(git status --porcelain)" ] || { echo "Önce değişiklikleri commit'leyin." >&2; exit 1; }
git rev-parse "v$version" >/dev/null 2>&1 && { echo "v$version zaten var; __version__ değerini artırın." >&2; exit 1; }
python3 -m unittest discover -s tests -q
bash -n kur.sh
tools/paketle.sh
git tag "v$version"
git push -q origin main "v$version"
gh release create "v$version" dist/nobetci-kurulum.sh dist/SHA256SUMS --title "v$version" \
  --notes "Sunucuda: \`./nobetci-kurulum.sh guncelle\` — ya da ilk kurulum için: \`curl -fsSLO https://github.com/Yultax/k8s-nobetci/releases/latest/download/nobetci-kurulum.sh\`"
