#!/usr/bin/env bash
# Nöbetçi kurulum sihirbazı. kubectl'in kümeye bağlı olduğu makinede çalıştırın.
#
#   ./kur.sh              menü
#   ./kur.sh kur          kur ya da güncelle      ./kur.sh ekran        ekranı ağda aç
#   ./kur.sh durum        durum özeti             ./kur.sh ekran-kapat
#   ./kur.sh izle         canlı bildirimler       ./kur.sh demo | demo-kaldir
#   ./kur.sh kesif        yalnızca incele         ./kur.sh kaldir
#   ./kur.sh guncelle     GitHub'daki son sürümle kendini yenileyip yeniden kurar
#   ./kur.sh llm          olay yorumcusunu (Gemini API / yerel model / kapalı) ayarlar
#
# EVET=1 ile bütün sorularda varsayılan kabul edilir.
# tools/paketle.sh bu betiğin sonuna kodu gömer; tek dosya olarak taşınır (dist/nobetci-kurulum.sh).
set -uo pipefail

VERSION=dev   # tools/paketle.sh nobetci/__init__.py içindeki sürümü yazar
REPO="${NOBETCI_REPO:-Yultax/k8s-nobetci}"
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
    tail -n +"$ARC_LINE" "$SELF" | base64 -d | tar xzm -C "$SRC" || { echo "Gömülü kod açılamadı." >&2; exit 1; }
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
confirm_no() { # confirm_no "soru" -> 0 yalnızca açıkça "evet" denirse (varsayılan hayır)
  [ "${EVET:-}" = 1 ] && return 1
  local a; read -rp "  $1 ${D}[e/H]${N}: " a </dev/tty
  [[ "$a" =~ ^[EeYy] ]]
}
confirm() { # confirm "soru" -> 0 evet
  [ "${EVET:-}" = 1 ] && return 0
  local a; read -rp "  $1 ${D}[E/h]${N}: " a </dev/tty
  [[ -z "$a" || "$a" =~ ^[EeYy] ]]
}
save_conf() { : > "$CONF"; for k in CLUSTER PROM_URL WEBHOOK_URL NS_EXCLUDE PORT LLM_URL LLM_MODEL LLM_LOGS; do printf '%s=%q\n' "$k" "${!k:-}" >> "$CONF"; done; }
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"
PORT="${PORT:-8080}"

need_cluster() {
  command -v kubectl >/dev/null || die "kubectl bulunamadı."
  kubectl get nodes >/dev/null 2>&1 || die "kubectl kümeye bağlanamıyor (kubectl get nodes çalışmıyor)."
}
installed() { kubectl get deploy -n nobetci nobetci >/dev/null 2>&1; }
api() { kubectl get --raw "/api/v1/namespaces/nobetci/services/http:nobetci:80/proxy$1" 2>/dev/null; }
DOCKER=""
docker_cmd() { # docker yetkisi yoksa sudo ile dene (paylaşılan sunucuda kullanıcı docker grubunda olmayabilir)
  [ -n "$DOCKER" ] && return 0
  if docker version >/dev/null 2>&1; then DOCKER="docker"
  elif command -v sudo >/dev/null && sudo docker version >/dev/null; then DOCKER="sudo docker"; info "docker sudo ile kullanılacak"
  else return 1; fi
}
ekran_pid() { [ -f "$HOME_DIR/ekran.pid" ] && kill -0 "$(cat "$HOME_DIR/ekran.pid")" 2>/dev/null && cat "$HOME_DIR/ekran.pid"; }
port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
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
  if docker_cmd; then
    ok "docker $($DOCKER version --format '{{.Server.Version}}') ($DOCKER)"
    $DOCKER image inspect python:3.12-alpine >/dev/null 2>&1 && ok "python:3.12-alpine yerelde var" || info "python:3.12-alpine yerelde yok (kurulumda çekilmeye çalışılır)"
  else warn "docker yok ya da bu kullanıcının yetkisi yok"; fi
  step 4/4 "Dış erişim"
  for url in https://registry-1.docker.io/v2/ https://github.com https://generativelanguage.googleapis.com/ https://registry.ollama.ai/v2/; do
    code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$url" 2>/dev/null)
    [[ "$code" =~ ^[1-5] ]] && ok "$url ($code)" || warn "$url ulaşılamadı"
  done
  info "$(nproc) çekirdek · $(free -h | awk '/^Mem/ {print $2" RAM, "$7" kullanılabilir"}') · disk $(df -h / | awk 'NR==2 {print $4" boş"}')"
}

# --- imaj ----------------------------------------------------------------------
pick_base() {
  local base="python:3.12-alpine" local_imgs
  if $DOCKER image inspect "$base" >/dev/null 2>&1; then echo "$base"; return; fi
  info "$base çekiliyor…" >&2
  if $DOCKER pull -q "$base" >/dev/null 2>&1; then echo "$base"; return; fi
  warn "$base çekilemedi (dış ağ kapalı olabilir). Yereldeki Python imajlarına bakılıyor…" >&2
  local_imgs=$($DOCKER images --format '{{.Repository}}:{{.Tag}}' | grep -E '(^|/)python:3\.(9|1[0-9])' | head -5)
  [ -n "$local_imgs" ] || return 1
  echo "$local_imgs" | sed 's/^/    /' >&2
  ask "Hangisi temel alınsın (3.9 ve üstü yeter, ek paket gerekmez)" "$(echo "$local_imgs" | head -1)"
}

build_image() {
  IMAGE="${NOBETCI_IMAGE:-}"
  if [ -n "$IMAGE" ]; then ok "hazır imaj kullanılacak: $IMAGE"; return; fi
  docker_cmd || die "docker yok ya da kullanılamıyor. İmajı başka yerde derleyip NOBETCI_IMAGE=... ile verin."
  IMAGE="k8s-nobetci:$VERSION"
  local base; base=$(pick_base) || die "Kullanılabilir bir Python imajı yok. Bir makinede 'docker save python:3.12-alpine' alıp buraya 'docker load' ile yükleyin."
  $DOCKER build -q --build-arg "BASE=$base" -t "$IMAGE" . >/dev/null || die "imaj derlenemedi"
  ok "imaj derlendi: $IMAGE (temel: $base, $($DOCKER image inspect -f '{{.Size}}' "$IMAGE" | awk '{printf "%.0f MB", $1/1e6}'))"
  if [ -n "$KIND_NODES" ]; then
    for node in $KIND_NODES; do  # 'kind load docker-image' ile aynı iş; kind komutu gerekmez
      # containerd sürümüne göre biri tutar; kind komutu varsa son çare o
      $DOCKER save "$IMAGE" | $DOCKER exec -i "$node" ctr -n k8s.io images import - >/dev/null 2>&1 ||
        $DOCKER save "$IMAGE" | $DOCKER exec -i "$node" ctr -n k8s.io images import --all-platforms - >/dev/null 2>&1 ||
        { command -v kind >/dev/null && kind load docker-image "$IMAGE" --name "$(kubectl get nodes -o jsonpath='{.items[0].spec.providerID}' | cut -d/ -f4)" >/dev/null 2>&1; } ||
        die "imaj $node node'una yüklenemedi"
      $DOCKER exec "$node" ctr -n k8s.io images ls -q 2>/dev/null | grep -q "k8s-nobetci:$VERSION" || warn "imaj node'da görünmüyor; pod ImagePullBackOff verebilir"
      ok "imaj node'a yüklendi: $node"
    done
  else
    local reg; reg=$(ask "Node'ların çekebileceği registry (ör. registry.sirket.local/ekip)" "")
    [ -n "$reg" ] || die "kind dışı kümede bir registry gerekli"
    $DOCKER tag "$IMAGE" "$reg/$IMAGE" && $DOCKER push -q "$reg/$IMAGE" >/dev/null || die "registry'ye gönderilemedi"
    IMAGE="$reg/$IMAGE"; ok "gönderildi: $IMAGE"
  fi
}

render() {
  sed -e "s|__IMAGE__|$IMAGE|" -e "s|__CLUSTER__|$CLUSTER|" -e "s|__PROM_URL__|$PROM_URL|" \
      -e "s|__LLM_URL__|${LLM_URL:-}|" -e "s|__LLM_MODEL__|${LLM_MODEL:-}|" -e "s|__LLM_LOGS__|${LLM_LOGS:-false}|" \
      -e "s|__WEBHOOK_URL__|${WEBHOOK_URL:-}|" -e "s|__NS_EXCLUDE__|${NS_EXCLUDE:-}|" "$1"
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
  kubectl -n nobetci rollout restart deploy/nobetci >/dev/null
  if ! kubectl -n nobetci rollout status deploy/nobetci --timeout=180s >/dev/null; then
    kubectl -n nobetci get pods | sed 's/^/  /'
    kubectl -n nobetci logs deploy/nobetci --tail=20 2>/dev/null | sed 's/^/  /'
    die "nöbetçi ayağa kalkmadı"
  fi
  ok "pod hazır"
  for _ in 1 2 3 4 5 6 7 8 9 10; do [ "$(api /healthz)" = ok ] && break; sleep 2; done
  [ "$(api /healthz)" = ok ] && ok "sağlık kontrolü geçti" || warn "sağlık kontrolüne yanıt yok"

  printf '\n  %sKurulum tamam (sürüm %s).%s\n' "$G$B" "$VERSION" "$N"
  if [ -n "$(ekran_pid)" ]; then cmd_ekran_kapat >/dev/null; cmd_ekran   # yeni pod'a yeniden bağlansın
  elif confirm "Ekranı şimdi ağda açayım mı (port $PORT)?"; then cmd_ekran; fi
  info "Canlı bildirimler: $ME izle   ·   Durum: $ME durum   ·   Deneme sorunları: $ME demo"
}

cmd_ekran() {
  need_cluster
  installed || die "Önce kurulum yapın: $ME kur"
  if [ -n "$(ekran_pid)" ]; then ok "ekran zaten açık: http://$(my_ip):$PORT"; return; fi
  if port_busy "$PORT"; then
    local p=$PORT; while port_busy "$p" && [ "$p" -lt $((PORT + 20)) ]; do p=$((p + 1)); done
    warn "$PORT portu başka bir şey tarafından kullanılıyor; $p kullanılacak"
    PORT=$p; save_conf
  fi
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
  local latest; latest=$(latest_version)
  if [ -z "$latest" ]; then info "sürüm $VERSION (GitHub'a ulaşılamadı, yeni sürüm denetlenemedi)"
  elif [ "$latest" != "$VERSION" ]; then warn "yeni sürüm var: $VERSION → $latest  ($ME guncelle)"
  else ok "sürüm $VERSION (güncel)"; fi
}

latest_version() { # GitHub'daki son sürüm (ör. 0.3.0); ulaşılamazsa boş
  curl -fsSL -m 8 "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null |
    grep -m1 '"tag_name"' | sed -E 's/.*"v?([^"]+)".*/\1/'
}

cmd_guncelle() {
  need_cluster
  local url="https://github.com/$REPO/releases/latest/download" dir new
  dir="$(dirname "$SELF")"
  step 1/3 "Son sürüm indiriliyor ($REPO)"
  curl -fsSL -m 60 "$url/nobetci-kurulum.sh" -o "$dir/.yeni.sh" && curl -fsSL -m 20 "$url/SHA256SUMS" -o "$dir/.yeni.sums" ||
    { rm -f "$dir/.yeni.sh" "$dir/.yeni.sums"; die "GitHub'dan indirilemedi"; }
  if [ "$(sha256sum "$dir/.yeni.sh" | cut -d' ' -f1)" != "$(grep nobetci-kurulum.sh "$dir/.yeni.sums" | cut -d' ' -f1)" ]; then
    rm -f "$dir/.yeni.sh" "$dir/.yeni.sums"; die "özet tutmuyor; dosya bozuk inmiş olabilir, hiçbir şey değiştirilmedi"
  fi
  rm -f "$dir/.yeni.sums"
  new=$(grep -m1 '^VERSION=' "$dir/.yeni.sh" | sed -E 's/^VERSION=([^ ]+).*/\1/')
  ok "indirildi ve doğrulandı: $new"
  if [ "$new" = "$VERSION" ] && [ "${ZORLA:-}" != 1 ]; then
    rm -f "$dir/.yeni.sh"; ok "zaten güncel ($VERSION). Yine de yeniden kurmak için: ZORLA=1 $ME guncelle"; return
  fi
  step 2/3 "Kendini yeniliyor: $VERSION → $new"
  cp "$SELF" "$HOME_DIR/onceki-$VERSION.sh"
  chmod +x "$dir/.yeni.sh" && mv -f "$dir/.yeni.sh" "$SELF"   # aynı klasörde mv: çalışan betiğin altından dosya değişmez
  ok "eski sürüm saklandı: $HOME_DIR/onceki-$VERSION.sh (geri dönmek için onu çalıştırıp 'kur' deyin)"
  step 3/3 "Kayıtlı ayarlarla yeniden kurulum"
  exec env EVET=1 "$SELF" kur
}

GEMINI_URL="https://generativelanguage.googleapis.com/v1beta/openai"
llm_test() { # llm_test url model anahtar_dosyası -> 0 başarılı; hata metnini yazar
  local url="$1" model="$2" keyfile="$3" endpoint body code
  [[ "$url" =~ ^https?://[^/]+/?$ ]] && endpoint="${url%/}/v1/chat/completions" || endpoint="${url%/}/chat/completions"
  body=$(printf '{"model":"%s","max_tokens":20,"messages":[{"role":"user","content":"Sadece TAMAM yaz."}]}' "$model")
  local args=(-s -m 120 -o "$HOME_DIR/.llm-test" -w '%{http_code}' -H 'Content-Type: application/json' -d "$body")
  # anahtar komut satırına yazılmaz (ps'te görünmesin): başlık dosyadan okunur
  [ -s "$keyfile" ] && { printf 'Authorization: Bearer %s\n' "$(cat "$keyfile")" > "$keyfile.h"; args+=(-H "@$keyfile.h"); }
  code=$(curl "${args[@]}" "$endpoint")
  rm -f "$keyfile.h"
  if [ "$code" = 200 ] && grep -q '"choices"' "$HOME_DIR/.llm-test"; then rm -f "$HOME_DIR/.llm-test"; return 0; fi
  fail "LLM ucu yanıt vermedi (HTTP ${code:-yok}): $(head -c 300 "$HOME_DIR/.llm-test" 2>/dev/null | tr '\n' ' ')"
  rm -f "$HOME_DIR/.llm-test"; return 1
}

cmd_llm() {
  need_cluster
  installed || die "Önce kurulum yapın: $ME kur"
  step 1/3 "LLM yorumcusu"
  info "Şu an: ${LLM_URL:-kapalı}${LLM_URL:+ · ${LLM_MODEL} · loglar $([ "${LLM_LOGS:-false}" = true ] && echo gönderiliyor || echo gönderilmiyor)}"
  printf '\n  1) Kapalı (kural tabanlı özet)\n  2) Google AI Studio / Gemini API (Gemma, ücretsiz katman; anahtar gerekir)\n'
  printf '  3) Küme içinde yerel model (Ollama; veri dışarı çıkmaz, ~3 GB indirme)\n  4) Başka bir OpenAI uyumlu uç\n\n'
  local choice keyfile="$HOME_DIR/.llm-key"
  choice=$(ask "Seçim" "1")
  rm -f "$keyfile"; umask 077
  case "$choice" in
    1) LLM_URL=""; LLM_MODEL=""; LLM_LOGS=false ;;
    2) LLM_URL="$GEMINI_URL"
       info "Anahtar: https://aistudio.google.com/apikey → Create API key. Ücretsiz katmanda Google gönderilen veriyi"
       info "ürünlerini geliştirmek için kullanabilir; bu yüzden pod logları varsayılan olarak gönderilmez."
       LLM_MODEL=$(ask "Model" "gemma-3-27b-it")
       local key; read -rsp "  API anahtarı (ekranda görünmez): " key </dev/tty; echo
       [ -n "$key" ] || die "anahtar girilmedi"
       printf '%s' "$key" > "$keyfile"; key=""
       LLM_LOGS=false; confirm_no "Çöken konteynerin son log satırları da gönderilsin mi? (önerilmez)" && LLM_LOGS=true ;;
    3) LLM_URL="http://ollama.nobetci.svc:11434"; LLM_MODEL=$(ask "Model" "gemma3:1b"); LLM_LOGS=true ;;
    4) LLM_URL=$(ask "Uç adresi (ör. http://10.0.0.5:11434 ya da .../v1)" "${LLM_URL:-}")
       LLM_MODEL=$(ask "Model" "${LLM_MODEL:-}")
       local key2; read -rsp "  API anahtarı (yoksa boş bırakın): " key2 </dev/tty; echo
       [ -n "$key2" ] && printf '%s' "$key2" > "$keyfile"; key2=""
       LLM_LOGS=false; confirm_no "Çöken konteynerin log satırları da gönderilsin mi?" && LLM_LOGS=true ;;
    *) die "geçersiz seçim" ;;
  esac

  step 2/3 "Deneme"
  if [ "$choice" = 3 ]; then
    render deploy/ollama.yaml | kubectl apply -f - | sed 's/^/  /'
    info "Ollama ayağa kalkıyor ve modeli indiriyor (birkaç dakika sürebilir)…"
    kubectl -n nobetci rollout status deploy/ollama --timeout=600s >/dev/null || warn "ollama henüz hazır değil; hazır olunca nöbetçi kendiliğinden kullanır"
  elif [ "$choice" != 1 ]; then
    if llm_test "$LLM_URL" "$LLM_MODEL" "$keyfile"; then ok "LLM yanıt verdi ($LLM_MODEL)"; else
      rm -f "$keyfile"; die "ayar kaydedilmedi; anahtarı, model adını ve ağ erişimini kontrol edin"; fi
  else ok "LLM kapatılıyor"; fi

  step 3/3 "Uygulanıyor"
  if [ -s "$keyfile" ]; then
    kubectl -n nobetci create secret generic nobetci-llm --from-file=key="$keyfile" --dry-run=client -o yaml | kubectl apply -f - >/dev/null &&
      ok "anahtar Kubernetes Secret'ına yazıldı (nobetci/nobetci-llm)"
  elif [ "$choice" = 1 ]; then
    kubectl -n nobetci delete secret nobetci-llm --ignore-not-found >/dev/null
  fi
  rm -f "$keyfile"
  [ "$choice" != 3 ] && kubectl -n nobetci delete deploy/ollama svc/ollama --ignore-not-found >/dev/null 2>&1
  save_conf
  IMAGE=$(kubectl -n nobetci get deploy nobetci -o jsonpath='{.spec.template.spec.containers[0].image}')
  render deploy/nobetci.yaml | kubectl apply -f - >/dev/null
  kubectl -n nobetci rollout status deploy/nobetci --timeout=180s >/dev/null && ok "nöbetçi yeni ayarla çalışıyor" || warn "nöbetçi yeniden başlatılamadı: $ME durum"
  [ -n "$(ekran_pid)" ] && { cmd_ekran_kapat >/dev/null; cmd_ekran >/dev/null; }
  info "Yorumlar yeni olaylar açıldıkça ekranda 'Yorum · model' başlığıyla görünür."
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
    printf '  5) Demo sorunları kur\n  6) Demo sorunları kaldır\n  7) Kümeyi incele (bir şey değiştirmez)\n  8) Ekranı kapat\n  9) Kaldır\n'
    printf "  g) GitHub'dan güncelle\n  l) LLM yorumcusu\n  q) Çık\n\n"
    local c; read -rp "  Seçim: " c </dev/tty
    case "$c" in
      1) (cmd_kur); [ -f "$CONF" ] && . "$CONF" ;; 2) cmd_ekran ;; 3) cmd_izle ;; 4) cmd_durum ;; 5) cmd_demo ;; 6) cmd_demo_kaldir ;;
      7) (cmd_kesif) ;; 8) cmd_ekran_kapat ;; 9) cmd_kaldir ;; g|G) cmd_guncelle ;; l|L) (cmd_llm); [ -f "$CONF" ] && . "$CONF" ;; q|Q) return ;; *) warn "geçersiz seçim" ;;
    esac
  done
}

case "${1:-menu}" in
  menu) menu ;; kur) cmd_kur ;; ekran) cmd_ekran ;; ekran-kapat) cmd_ekran_kapat ;; durum) cmd_durum ;;
  izle) cmd_izle ;; guncelle) cmd_guncelle ;; llm) cmd_llm ;; demo) cmd_demo ;; demo-kaldir) cmd_demo_kaldir ;; kesif) cmd_kesif ;; kaldir) cmd_kaldir ;;
  *) sed -n '2,12p' "$SELF" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
exit $?
