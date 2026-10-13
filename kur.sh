#!/usr/bin/env bash
# Nöbetçi kurulum sihirbazı. kubectl'in kümeye bağlı olduğu makinede çalıştırın.
#   ./kur.sh            kur ya da güncelle (sorular sorar; EVET=1 ile varsayılanları kabul eder)
#   ./kur.sh --kesif    yalnızca kümeyi incele, hiçbir şey kurma
#   ./kur.sh --demo     sorun çıkaran örnek iş yüklerini kur (deploy/demo-yuk.yaml)
#   ./kur.sh --kaldir   nöbetçiyi ve demo yükünü kaldır
set -euo pipefail
cd "$(dirname "$0")"

VERSION=0.1.0
IMAGE="k8s-nobetci:$VERSION"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
die() { printf '\033[31mHATA: %s\033[0m\n' "$*" >&2; exit 1; }
ask() { # ask "soru" "varsayılan"
  local answer=""
  if [ "${EVET:-}" != 1 ]; then read -rp "$1 [$2]: " answer; fi
  echo "${answer:-$2}"
}

command -v kubectl >/dev/null || die "kubectl bulunamadı"
kubectl get nodes >/dev/null 2>&1 || die "kubectl kümeye bağlanamıyor (kubectl get nodes çalışmıyor)"

case "${1:-}" in
  --kaldir)
    kubectl delete ns nobetci nobetci-demo --ignore-not-found
    kubectl delete clusterrole,clusterrolebinding nobetci --ignore-not-found
    exit 0 ;;
  --demo)
    kubectl apply -f deploy/demo-yuk.yaml
    echo "Demo yükü kuruldu. İlk bulgular (cokme) birkaç dakikada, bellek tahmini ~20 dk'da, CPU sıçraması ~25 dk'da gelir."
    exit 0 ;;
esac

# --- 1. Küme -----------------------------------------------------------------
say "Küme"
kubectl get nodes -o wide
PROVIDER=$(kubectl get nodes -o jsonpath='{.items[0].spec.providerID}' 2>/dev/null || true)
KIND_NODES=""
if [[ "$PROVIDER" == kind://* ]]; then
  KIND_NODES=$(kubectl get nodes -o jsonpath='{.items[*].metadata.name}')
  echo "Bu bir kind kümesi (node'lar Docker konteyneri): $KIND_NODES"
fi

# --- 2. Prometheus -----------------------------------------------------------
say "Prometheus aranıyor"
CANDIDATES=$(kubectl get svc -A -o jsonpath='{range .items[*]}{.metadata.namespace} {.metadata.name} {.spec.ports[0].port}{"\n"}{end}' \
  | grep -i prometheus | grep -Eiv 'alertmanager|node-exporter|pushgateway|kube-state|operator|adapter' || true)
PROM_URL="${NOBETCI_PROM_URL:-}"
if [ -z "$PROM_URL" ]; then
  [ -n "$CANDIDATES" ] || warn "Adında prometheus geçen bir Service bulunamadı."
  echo "$CANDIDATES" | sed 's/^/  /'
  # prometheus-server (topluluk chart'ı) ve prometheus-k8s / -prometheus (operator) öncelikli
  BEST=$(echo "$CANDIDATES" | sed 's/$/ /' | grep -E ' (prometheus-server|prometheus-k8s|[a-z0-9-]*-prometheus|prometheus) ' | head -1 || true)
  [ -n "$BEST" ] || BEST=$(echo "$CANDIDATES" | head -1)
  DEFAULT=""
  if [ -n "$BEST" ]; then
    read -r P_NS P_NAME P_PORT <<<"$BEST"
    DEFAULT="http://$P_NAME.$P_NS.svc:$P_PORT"
  fi
  PROM_URL=$(ask "Prometheus adresi" "$DEFAULT")
fi
[ -n "$PROM_URL" ] || die "Prometheus adresi gerekli (NOBETCI_PROM_URL ile de verilebilir)"

# Adres küme içi Service ise API sunucusu üzerinden sorgulayıp gerekli ölçümler var mı bak.
if [[ "$PROM_URL" =~ ^http://([^.]+)\.([^.]+)\.svc(:([0-9]+))?$ ]]; then
  PROXY="/api/v1/namespaces/${BASH_REMATCH[2]}/services/http:${BASH_REMATCH[1]}:${BASH_REMATCH[4]:-80}/proxy/api/v1/query?query="
  probe() { # probe "açıklama" "promql"
    local out
    out=$(kubectl get --raw "$PROXY$2" 2>/dev/null || true)
    if [[ "$out" == *'"value"'* ]]; then echo "  var : $1"; else warn "yok : $1"; return 1; fi
  }
  probe "Prometheus yanıt veriyor" "count(up)" || die "Prometheus'a ulaşılamadı: $PROM_URL"
  probe "pod CPU/bellek ölçümleri (cAdvisor)" "count(container_cpu_usage_seconds_total)" || warn "CPU/bellek/ağ anomalileri çalışmaz"
  probe "pod durum ölçümleri (kube-state-metrics)" "count(kube_pod_status_phase)" || warn "çökme/OOM/Pending kontrolleri çalışmaz"
  probe "bellek limitleri" "count(kube_pod_container_resource_limits)" || warn "limit dolma tahmini çalışmaz"
fi

if [ "${1:-}" = "--kesif" ]; then
  say "Depolama";  kubectl get storageclass
  say "Kaynak";    kubectl top nodes 2>/dev/null || warn "metrics-server yok"
  say "Docker";    (docker version --format 'docker {{.Server.Version}}' 2>/dev/null || warn "docker yok ya da yetki yok")
  say "Dış erişim (5 sn)"
  for url in https://registry-1.docker.io/v2/ https://registry.ollama.ai/v2/ https://github.com; do
    code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$url" || true)
    echo "  $url -> ${code:-ulaşılamadı}"
  done
  say "Makine";    nproc; free -h | sed -n 1,2p; df -h / | tail -1
  exit 0
fi

# --- 3. LLM ------------------------------------------------------------------
say "LLM yorumcusu"
LLM_URL="${NOBETCI_LLM_URL:-}"
LLM_MODEL="${NOBETCI_LLM_MODEL:-gemma3:4b}"
OLLAMA=0
if [ -z "$LLM_URL" ]; then
  echo "  1) LLM'siz kur (kural tabanlı özet; sonradan eklenebilir)"
  echo "  2) Hazır bir OpenAI uyumlu uç kullan (Ollama, llama.cpp, vLLM...)"
  echo "  3) Küme içine Ollama kur (imaj ~2 GB + model ~3 GB internetten çekilir, ~4 GB RAM ister)"
  case $(ask "Seçim" "1") in
    2) LLM_URL=$(ask "Uç adresi (ör. http://10.0.0.5:11434)" "")
       LLM_MODEL=$(ask "Model adı" "$LLM_MODEL") ;;
    3) OLLAMA=1; LLM_URL="http://ollama.nobetci.svc:11434"
       LLM_MODEL=$(ask "Model adı" "$LLM_MODEL") ;;
  esac
fi

# --- 4. İmaj -----------------------------------------------------------------
say "İmaj: $IMAGE"
if [ -n "${NOBETCI_IMAGE:-}" ]; then
  IMAGE="$NOBETCI_IMAGE"; echo "Hazır imaj kullanılacak: $IMAGE"
else
  command -v docker >/dev/null || die "docker yok. İmajı başka yerde derleyip NOBETCI_IMAGE=... ile verin."
  docker build -t "$IMAGE" . || die "imaj derlenemedi (python:3.12-alpine çekilemiyor olabilir)"
  if [ -n "$KIND_NODES" ]; then
    for node in $KIND_NODES; do  # kind load ile aynı iş; kind komutuna gerek kalmaz
      echo "node'a yükleniyor: $node"
      docker save "$IMAGE" | docker exec -i "$node" ctr -n k8s.io images import - >/dev/null
    done
  else
    REGISTRY=$(ask "İmajın gönderileceği registry (ör. registry.sirket.local/ekip)" "")
    [ -n "$REGISTRY" ] || die "kind dışı kümede node'ların imajı çekebileceği bir registry gerekli"
    docker tag "$IMAGE" "$REGISTRY/$IMAGE" && docker push "$REGISTRY/$IMAGE"
    IMAGE="$REGISTRY/$IMAGE"
  fi
fi

# --- 5. Kurulum --------------------------------------------------------------
say "Kuruluyor"
CLUSTER=$(ask "Ekranda görünecek küme adı" "$(kubectl config current-context 2>/dev/null || echo kubernetes)")
render() {
  sed -e "s|__IMAGE__|$IMAGE|" -e "s|__CLUSTER__|$CLUSTER|" -e "s|__PROM_URL__|$PROM_URL|" \
      -e "s|__LLM_URL__|$LLM_URL|" -e "s|__LLM_MODEL__|$LLM_MODEL|" -e "s|__WEBHOOK_URL__|${NOBETCI_WEBHOOK_URL:-}|" "$1"
}
render deploy/nobetci.yaml | kubectl apply -f -
if [ "$OLLAMA" = 1 ]; then
  render deploy/ollama.yaml | kubectl apply -f -
  echo "Ollama modeli arka planda indiriyor; bitene kadar nöbetçi kural tabanlı özetle çalışır."
fi
kubectl -n nobetci rollout restart deploy/nobetci >/dev/null
kubectl -n nobetci rollout status deploy/nobetci --timeout=180s || {
  kubectl -n nobetci get pods; kubectl -n nobetci describe pod -l app=nobetci | tail -20; die "nöbetçi ayağa kalkmadı"; }

say "Hazır"
cat <<EOF
Canlı bildirimler (terminal):
  kubectl -n nobetci logs -f deploy/nobetci

Canlı ekran (bu makinenin 8080 portundan, ağdaki herkese açık):
  kubectl -n nobetci port-forward --address 0.0.0.0 svc/nobetci 8080:80
  -> http://$(hostname -I 2>/dev/null | awk '{print $1}'):8080

Deneme için sorunlu iş yükleri:  ./kur.sh --demo
Kaldırmak için:                  ./kur.sh --kaldir
EOF
