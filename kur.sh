#!/usr/bin/env bash
# Nöbetçi kurulum sihirbazı. kubectl'in kümeye bağlı olduğu makinede çalıştırın.
#
#   ./kur.sh              menü
#   ./kur.sh kur          kur ya da güncelle      ./kur.sh ekran        ekranı ağda aç
#   ./kur.sh durum        durum özeti             ./kur.sh ekran-kapat
#   ./kur.sh izle         canlı bildirimler       ./kur.sh demo | demo-kaldir
#   ./kur.sh kesif        yalnızca incele         ./kur.sh kaldir
#
# EVET=1 ile bütün sorularda varsayılan kabul edilir.
# tools/paketle.sh bu betiğin sonuna kodu gömer; tek dosya olarak taşınır (dist/nobetci-kurulum.sh).
set -uo pipefail

VERSION=0.2.0
HOME_DIR="${NOBETCI_HOME:-$HOME/.nobetci}"
CONF="$HOME_DIR/ayarlar"
mkdir -p "$HOME_DIR"

# --- paketlenmiş sürüm: gömülü kodu aç -----------------------------------------
SELF="$(readlink -f "$0")"
ARC_LINE=$(awk '/^__ARSIV__$/ {print NR + 1; exit}' "$SELF")
if [ -n "$ARC_LINE" ]; then
  SRC="$HOME_DIR/surum-$VERSION"
  if [ ! -f "$SRC/.tamam" ]; then
    rm -rf "$SRC" && mkdir -p "$SRC"
    tail -n +"$ARC_LINE" "$SELF" | base64 -d | tar xz -C "$SRC" || { echo "Gömülü kod açılamadı." >&2; exit 1; }
    touch "$SRC/.tamam"
  fi
else
  SRC="$(dirname "$SELF")"
fi
cd "$SRC" || exit 1
ME="./$(basename "$SELF")"

# --- görünüm -------------------------------------------------------------------
if [ -t 1 ]; then B=$'\e[1m'; D=$'\e[2m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; N=$'\e[0m'; else B= D= G= Y= R= C= N=; fi
step() { printf '\n%s[%s]%s %s%s%s\n' "$C" "$1" "$N" "$B" "$2" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
fail() { printf '  %s✗%s %s\n' "$R" "$N" "$*"; }
info() { printf '  %s%s%s\n' "$D" "$*" "$N"; }
die()  { fail "$*"; exit 1; }
ask() { # ask "soru" "varsayılan" -> cevap
  local answer=""
  if [ "${EVET:-}" != 1 ]; then read -rp "  $1 ${D}[$2]${N}: " answer </dev/tty; fi
  echo "${answer:-$2}"
}
confirm() { # confirm "soru" -> 0 evet
  [ "${EVET:-}" = 1 ] && return 0
  local a; read -rp "  $1 ${D}[E/h]${N}: " a </dev/tty
  [[ -z "$a" || "$a" =~ ^[EeYy] ]]
}
save_conf() { : > "$CONF"; for k in CLUSTER PROM_URL WEBHOOK_URL NS_EXCLUDE PORT; do printf '%s=%q\n' "$k" "${!k:-}" >> "$CONF"; done; }
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"
PORT="${PORT:-8080}"

need_cluster() {
  command -v kubectl >/dev/null || die "kubectl bulunamadı."
  kubectl get nodes >/dev/null 2>&1 || die "kubectl kümeye bağlanamıyor (kubectl get nodes çalışmıyor)."
}
installed() { kubectl get deploy -n nobetci nobetci >/dev/null 2>&1; }
api() { kubectl get --raw "/api/v1/namespaces/nobetci/services/http:nobetci:80/proxy$1" 2>/dev/null; }
ekran_pid() { [ -f "$HOME_DIR/ekran.pid" ] && kill -0 "$(cat "$HOME_DIR/ekran.pid")" 2>/dev/null && cat "$HOME_DIR/ekran.pid"; }
my_ip() { hostname -I 2>/dev/null | awk '{print $1}'; }

# --- keşif ---------------------------------------------------------------------
KIND_NODES=""
detect_cluster() {
  local provider
  provider=$(kubectl get nodes -o jsonpath='{.items[0].spec.providerID}' 2>/dev/null)
  if [[ "$provider" == kind://* ]]; then
    KIND_NODES=$(kubectl get nodes -o jsonpath='{.items[*].metadata.name}')
    ok "kind kümesi: node'lar Docker konteyneri ($KIND_NODES)"
  else
    ok "küme: $(kubectl get nodes --no-headers 2>/dev/null | wc -l) node"
  fi
}

find_prometheus() {
  [ -n "${PROM_URL:-}" ] && return
  local cands best ns name port
  cands=$(kubectl get svc -A -o jsonpath='{range .items[*]}{.metadata.namespace} {.metadata.name} {.spec.ports[0].port}{"\n"}{end}' \
    | grep -i prometheus | grep -Eiv 'alertmanager|node-exporter|pushgateway|kube-state|operator|adapter' || true)
  if [ -z "$cands" ]; then warn "Adında prometheus geçen bir Service bulunamadı."; return; fi
  best=$(echo "$cands" | sed 's/$/ /' | grep -E ' (prometheus-server|prometheus-k8s|[a-z0-9-]*-prometheus|prometheus) ' | head -1)
  [ -n "$best" ] || best=$(echo "$cands" | head -1)
  read -r ns name port <<<"$best"
  PROM_URL="http://$name.$ns.svc:$port"
  ok "bulundu: $PROM_URL"
}

PROBLEMS=0
check_prometheus() {
  if [[ ! "$PROM_URL" =~ ^http://([^.]+)\.([^.]+)\.svc(:([0-9]+))?/?$ ]]; then
    warn "Küme dışı adres; erişim denetlenemedi: $PROM_URL"; return
  fi
  local proxy="/api/v1/namespaces/${BASH_REMATCH[2]}/services/http:${BASH_REMATCH[1]}:${BASH_REMATCH[4]:-80}/proxy/api/v1/query?query="
  probe() { [[ "$(kubectl get --raw "$proxy$2" 2>/dev/null)" == *'"value"'* ]] && ok "$1" || { warn "$1 — yok: $3"; PROBLEMS=$((PROBLEMS + 1)); }; }
  if [[ "$(kubectl get --raw "${proxy}count(up)" 2>/dev/null)" != *'"value"'* ]]; then
    fail "Prometheus'a ulaşılamadı: $PROM_URL"; PROBLEMS=$((PROBLEMS + 10)); return
  fi
  ok "Prometheus yanıt veriyor"
  probe "pod CPU/bellek/ağ ölçümleri (cAdvisor)" "count(container_cpu_usage_seconds_total)" "sıçrama/düşüş tespiti çalışmaz"
  probe "pod durum ölçümleri (kube-state-metrics)" "count(kube_pod_status_phase)" "çökme/OOM/Pending kontrolleri çalışmaz"
  probe "bellek limitleri" "count(kube_pod_container_resource_limits)" "limit dolma tahmini çalışmaz"
}

cmd_kesif() {
  need_cluster
  step 1/4 "Küme"; detect_cluster
  kubectl top nodes 2>/dev/null | sed 's/^/  /' || warn "metrics-server yok"
  step 2/4 "Prometheus"; find_prometheus; [ -n "${PROM_URL:-}" ] && check_prometheus
  step 3/4 "Docker"
  if docker version >/dev/null 2>&1; then
    ok "docker $(docker version --format '{{.Server.Version}}')"
    docker image inspect python:3.12-alpine >/dev/null 2>&1 && ok "python:3.12-alpine yerelde var" || info "python:3.12-alpine yerelde yok (kurulumda çekilmeye çalışılır)"
  else warn "docker yok ya da bu kullanıcının yetkisi yok"; fi
  step 4/4 "Dış erişim"
  for url in https://registry-1.docker.io/v2/ https://github.com; do
    code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$url" 2>/dev/null)
    [[ "$code" =~ ^[1-5] ]] && ok "$url ($code)" || warn "$url ulaşılamadı"
  done
  info "$(nproc) çekirdek · $(free -h | awk '/^Mem/ {print $2" RAM, "$7" kullanılabilir"}') · disk $(df -h / | awk 'NR==2 {print $4" boş"}')"
}

# --- imaj ----------------------------------------------------------------------
pick_base() {
  local base="python:3.12-alpine" local_imgs
  if docker image inspect "$base" >/dev/null 2>&1; then echo "$base"; return; fi
  info "$base çekiliyor…" >&2
  if docker pull -q "$base" >/dev/null 2>&1; then echo "$base"; return; fi
  warn "$base çekilemedi (dış ağ kapalı olabilir). Yereldeki Python imajlarına bakılıyor…" >&2
  local_imgs=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep -E '(^|/)python:3\.(9|1[0-9])' | head -5)
  [ -n "$local_imgs" ] || return 1
  echo "$local_imgs" | sed 's/^/    /' >&2
  ask "Hangisi temel alınsın (3.9 ve üstü yeter, ek paket gerekmez)" "$(echo "$local_imgs" | head -1)"
}

build_image() {
  IMAGE="${NOBETCI_IMAGE:-}"
  if [ -n "$IMAGE" ]; then ok "hazır imaj kullanılacak: $IMAGE"; return; fi
  docker version >/dev/null 2>&1 || die "docker yok. İmajı başka yerde derleyip NOBETCI_IMAGE=... ile verin."
  IMAGE="k8s-nobetci:$VERSION"
  local base; base=$(pick_base) || die "Kullanılabilir bir Python imajı yok. Bir makinede 'docker save python:3.12-alpine' alıp buraya 'docker load' ile yükleyin."
  docker build -q --build-arg "BASE=$base" -t "$IMAGE" . >/dev/null || die "imaj derlenemedi"
  ok "imaj derlendi: $IMAGE (temel: $base, $(docker image inspect -f '{{.Size}}' "$IMAGE" | awk '{printf "%.0f MB", $1/1e6}'))"
  if [ -n "$KIND_NODES" ]; then
    for node in $KIND_NODES; do  # 'kind load docker-image' ile aynı iş; kind komutu gerekmez
      docker save "$IMAGE" | docker exec -i "$node" ctr -n k8s.io images import - >/dev/null || die "imaj $node node'una yüklenemedi"
      ok "imaj node'a yüklendi: $node"
    done
  else
    local reg; reg=$(ask "Node'ların çekebileceği registry (ör. registry.sirket.local/ekip)" "")
    [ -n "$reg" ] || die "kind dışı kümede bir registry gerekli"
    docker tag "$IMAGE" "$reg/$IMAGE" && docker push -q "$reg/$IMAGE" >/dev/null || die "registry'ye gönderilemedi"
    IMAGE="$reg/$IMAGE"; ok "gönderildi: $IMAGE"
  fi
}

render() {
  sed -e "s|__IMAGE__|$IMAGE|" -e "s|__CLUSTER__|$CLUSTER|" -e "s|__PROM_URL__|$PROM_URL|" \
      -e "s|__LLM_URL__|${NOBETCI_LLM_URL:-}|" -e "s|__LLM_MODEL__|${NOBETCI_LLM_MODEL:-gemma3:4b}|" \
      -e "s|__WEBHOOK_URL__|${WEBHOOK_URL:-}|" "$1"
}

# --- komutlar ------------------------------------------------------------------
cmd_kur() {
  need_cluster
  step 1/5 "Ön kontrol"
  ok "kubectl bağlı: $(kubectl config current-context 2>/dev/null)"
  detect_cluster
  installed && info "nöbetçi zaten kurulu; güncellenecek (olay kayıtları korunur)"

  step 2/5 "Prometheus"
  find_prometheus
  PROM_URL=$(ask "Prometheus adresi" "${PROM_URL:-}")
  [ -n "$PROM_URL" ] || die "Prometheus adresi gerekli"
  check_prometheus
  [ "$PROBLEMS" -ge 10 ] && { confirm "Prometheus'a ulaşılamadı. Yine de devam edilsin mi?" || exit 1; }

  step 3/5 "Ayarlar"
  CLUSTER=$(ask "Ekranda görünecek küme adı" "${CLUSTER:-$(kubectl config current-context 2>/dev/null || echo kubernetes)}")
  NS_EXCLUDE=$(ask "İzlenmeyecek namespace'ler (regex, boş = hepsi izlenir)" "${NS_EXCLUDE:-}")
  WEBHOOK_URL=$(ask "Webhook adresi (boş = yok)" "${WEBHOOK_URL:-}")
  PORT=$(ask "Ekranın bu makinede açılacağı port" "$PORT")
  printf '\n  %sÖzet%s\n' "$B" "$N"
  row() { printf '  %s%*s %s\n' "$1" $((12 - ${#1})) "" "$2"; }
  row küme "$CLUSTER"; row prometheus "$PROM_URL"; row "hariç ns" "${NS_EXCLUDE:-—}"
  row webhook "${WEBHOOK_URL:-—}"; row ekran "http://$(my_ip):$PORT"; row namespace "nobetci (salt okunur yetki)"
  confirm "Kurulum başlasın mı?" || exit 0
  save_conf

  step 4/5 "İmaj"
  build_image

  step 5/5 "Kubernetes'e kurulum"
  render deploy/nobetci.yaml | kubectl apply -f - | sed 's/^/  /'
  [ -n "$NS_EXCLUDE" ] && kubectl -n nobetci set env deploy/nobetci "NOBETCI_NS_EXCLUDE=$NS_EXCLUDE" >/dev/null
  kubectl -n nobetci rollout restart deploy/nobetci >/dev/null
  if ! kubectl -n nobetci rollout status deploy/nobetci --timeout=180s >/dev/null; then
    kubectl -n nobetci get pods | sed 's/^/  /'
    kubectl -n nobetci logs deploy/nobetci --tail=20 2>/dev/null | sed 's/^/  /'
    die "nöbetçi ayağa kalkmadı"
  fi
  ok "pod hazır"
  for _ in 1 2 3 4 5 6 7 8 9 10; do [ "$(api /healthz)" = ok ] && break; sleep 2; done
  [ "$(api /healthz)" = ok ] && ok "sağlık kontrolü geçti" || warn "sağlık kontrolüne yanıt yok"

  printf '\n  %sKurulum tamam.%s\n' "$G$B" "$N"
  if confirm "Ekranı şimdi ağda açayım mı (port $PORT)?"; then cmd_ekran; fi
  info "Canlı bildirimler: $ME izle   ·   Durum: $ME durum   ·   Deneme sorunları: $ME demo"
}

cmd_ekran() {
  need_cluster
  installed || die "Önce kurulum yapın: $ME kur"
  if [ -n "$(ekran_pid)" ]; then ok "ekran zaten açık: http://$(my_ip):$PORT"; return; fi
  # port-forward oturum kapanınca ölmesin, koparsa yeniden bağlansın
  nohup bash -c "while true; do kubectl -n nobetci port-forward --address 0.0.0.0 svc/nobetci $PORT:80; sleep 2; done" \
    >"$HOME_DIR/ekran.log" 2>&1 &
  echo $! > "$HOME_DIR/ekran.pid"
  for _ in 1 2 3 4 5 6 7 8; do curl -s -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null && break; sleep 1; done
  if curl -s -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null; then
    ok "ekran açık: ${B}http://$(my_ip):$PORT${N}"
    info "Ağdaki herkes görebilir (salt okunur). Kapatmak için: $ME ekran-kapat"
    command -v ufw >/dev/null && sudo -n ufw status 2>/dev/null | grep -q "Status: active" && warn "ufw açık; gerekirse: sudo ufw allow $PORT/tcp"
  else
    fail "ekran açılamadı; günlük: $HOME_DIR/ekran.log"; tail -5 "$HOME_DIR/ekran.log" | sed 's/^/  /'
  fi
}

cmd_ekran_kapat() {
  local pid; pid=$(ekran_pid)
  [ -n "$pid" ] || { info "ekran zaten kapalı"; return; }
  pkill -P "$pid" 2>/dev/null; kill "$pid" 2>/dev/null; rm -f "$HOME_DIR/ekran.pid"
  ok "ekran kapatıldı"
}

cmd_durum() {
  need_cluster
  if ! installed; then warn "nöbetçi kurulu değil"; return; fi
  kubectl -n nobetci get pods -o wide | sed 's/^/  /'
  local ping; ping=$(api /api/ping)
  [ -n "$ping" ] && ok "çalışıyor ($ping)" || warn "API yanıt vermiyor"
  local state; state=$(api /metrics | grep '^nobetci_open_incidents' | awk '{s+=$2} END {print s+0}')
  info "açık olay: ${state:-?}"
  [ -n "$(ekran_pid)" ] && ok "ekran açık: http://$(my_ip):$PORT" || info "ekran kapalı ($ME ekran ile açılır)"
}

cmd_izle() { need_cluster; kubectl -n nobetci logs -f deploy/nobetci --tail=40; }
cmd_demo() { need_cluster; kubectl apply -f deploy/demo-yuk.yaml | sed 's/^/  /'
  info "çökme birkaç dakikada, bellek tahmini ~20 dk'da, CPU sıçraması ~25 dk'da görünür."; }
cmd_demo_kaldir() { need_cluster; kubectl delete ns nobetci-demo --ignore-not-found | sed 's/^/  /'; }
cmd_kaldir() {
  need_cluster
  confirm "Nöbetçi, olay kayıtları ve demo yükü silinsin mi?" || return
  cmd_ekran_kapat
  kubectl delete ns nobetci nobetci-demo --ignore-not-found | sed 's/^/  /'
  kubectl delete clusterrole,clusterrolebinding nobetci --ignore-not-found | sed 's/^/  /'
}

menu() {
  need_cluster
  while true; do
    local st="kurulu değil"
    installed && st="${G}kurulu${N}"
    [ -n "$(ekran_pid)" ] && st="$st · ekran ${B}http://$(my_ip):$PORT${N}"
    printf '\n  %sNöbetçi%s %sv%s%s · %s · %s\n\n' "$B" "$N" "$D" "$VERSION" "$N" "$(kubectl config current-context 2>/dev/null)" "$st"
    printf '  1) Kur / güncelle\n  2) Ekranı ağda aç\n  3) Canlı bildirimleri izle\n  4) Durum\n'
    printf '  5) Demo sorunları kur\n  6) Demo sorunları kaldır\n  7) Kümeyi incele (bir şey değiştirmez)\n  8) Ekranı kapat\n  9) Kaldır\n  q) Çık\n\n'
    local c; read -rp "  Seçim: " c </dev/tty
    case "$c" in
      1) (cmd_kur); [ -f "$CONF" ] && . "$CONF" ;; 2) cmd_ekran ;; 3) cmd_izle ;; 4) cmd_durum ;; 5) cmd_demo ;; 6) cmd_demo_kaldir ;;
      7) (cmd_kesif) ;; 8) cmd_ekran_kapat ;; 9) cmd_kaldir ;; q|Q) return ;; *) warn "geçersiz seçim" ;;
    esac
  done
}

case "${1:-menu}" in
  menu) menu ;; kur) cmd_kur ;; ekran) cmd_ekran ;; ekran-kapat) cmd_ekran_kapat ;; durum) cmd_durum ;;
  izle) cmd_izle ;; demo) cmd_demo ;; demo-kaldir) cmd_demo_kaldir ;; kesif) cmd_kesif ;; kaldir) cmd_kaldir ;;
  *) sed -n '2,12p' "$SELF" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
exit $?
