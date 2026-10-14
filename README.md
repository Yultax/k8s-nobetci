# Nöbetçi

Kubernetes kümesi için hafif bir anomali nöbetçisi. Prometheus'taki ölçümleri izler, olağan dışı
davranışı yakalar, ilgili bulguları tek olayda toplar, olayı küçük bir yerel LLM'e yorumlatır ve
canlı bir ekranda, terminalde ve isteğe bağlı webhook ile bildirir.

Tek pod, salt okunur yetki, **sıfır bağımlılık** (yalnızca Python standart kütüphanesi, imaj ~50 MB).
Dış ağa kapalı ortamlar için tasarlandı; LLM olmadan da çalışır.

## Nasıl çalışır

```
Prometheus ──► toplama ──► tespit ──► olay birleştirme ──► LLM yorumu ──► ekran / log / webhook
 (cAdvisor,     dakikada     │              │                  │
  kube-state-   ~15 sorgu    │              │                  └ olay başına tek kısa çağrı
  metrics)                   │              └ aynı iş yükünün bulguları tek olay; Kubernetes API'den
                             │                pod durumu, olaylar ve çöken konteynerin logları eklenir
                             ├ ani artış/düşüş: son 1 saatin medyanına göre sağlam z-skoru (MAD)
                             ├ saatlik profil: her gün aynı saatte tekrarlayan iş "olağan" sayılır
                             ├ limit tahmini: bellek bu hızla limite ne zaman dayanır
                             └ durum kontrolleri: OOMKilled, CrashLoopBackOff, Pending, hazır olmayan kopya...
```

- Ölçüm saklamaz; geçmişi açılışta Prometheus'tan okur (3 saat ayrıntılı + 7 gün saatlik profil).
- LLM karar vermez ve komut üretmez: hazır kanıtı üç satırda (özet, neden, öneri) yorumlar.
  Önerilen `kubectl` komutları kurallardan gelir. LLM yoksa ya da cevap vermezse kural tabanlı özet kalır.
- Kendi ölçümlerini `/metrics` ile yayınlar (`nobetci_open_incidents`, `nobetci_anomaly_score`...).

## Kurulum (kubectl'in bağlı olduğu makinede)

Tek dosya indirilir; kod dosyanın içine gömülüdür:

```bash
mkdir -p ~/nobetci && cd ~/nobetci
curl -fsSLO https://github.com/Yultax/k8s-nobetci/releases/latest/download/nobetci-kurulum.sh
curl -fsSLO https://github.com/Yultax/k8s-nobetci/releases/latest/download/SHA256SUMS
sha256sum -c SHA256SUMS && chmod +x nobetci-kurulum.sh
./nobetci-kurulum.sh        # menü
```

Güncelleme: `./nobetci-kurulum.sh guncelle` son sürümü indirir, özetini doğrular, kendini yeniler ve
kayıtlı ayarlarla soru sormadan yeniden kurar (olay kayıtları korunur). `durum` yeni sürüm varsa söyler.
Yeni sürüm yayınlamak: `nobetci/__init__.py` içindeki `__version__` artırılır, commit'lenir, `tools/yayinla.sh`.

Menü: kur/güncelle, GitHub'dan güncelle, ekranı ağda aç, canlı bildirimleri izle, durum, demo sorunları kur/kaldır, alarm sistemi örneğini kur/kaldır,
kümeyi incele, kaldır. Aynı işler alt komutla da yapılır (`./nobetci-kurulum.sh kur`, `ekran`, `izle`…).

Sihirbaz Prometheus'u kendisi bulur ve gereken ölçümlerin varlığını denetler; Docker temel imajı
çekemezse yereldeki bir Python imajını kullanır; kind kümesinde imajı doğrudan node'a yükler
(registry gerekmez). Verilen cevaplar `~/.nobetci/ayarlar` dosyasına yazılır, güncellemede varsayılan olur.
Ekran, oturum kapansa da çalışan bir `port-forward` ile `http://<sunucu>:8080` adresinde açılır.

## Ekran

- **Çalışma hattı:** Prometheus → dedektörler → saatlik profil filtresi → olaylar → bildirim; her ölçüm
  turunda aşama sayıları güncellenir, aşamaya tıklayınca nasıl çalıştığını gerçek parametrelerle anlatır.
- **Olay ayrıntısı:** neden alarm verdiği (normal, sapma, eşik), eşik zarflı grafikler (artı imleci
  bütün grafiklerde eşzamanlı), bellek için limite dolma tahmini, bulgu zaman çizelgesi, kopyalanabilir komutlar.
- **Sapma haritası:** pod × son 60 ölçüm; renk değerin kendi normalinden sapması (mavi düşüş, kırmızı artış).
- **Node grafikleri, olay zaman çizelgesi (1/6/24 sa), sıralanabilir pod tablosu.** Satıra tıklayınca pod görünümü açılır.
- Namespace filtresi, pod arama, açık/koyu tema, yeni olayda isteğe bağlı ses; kısayollar `j`/`k`, `/`, `Esc`, `?`.

## Keşif ekranı (`/kesif`)

Kümeyi ve nöbetçinin nasıl karar verdiğini öğrenmek için ayrı bir sayfa; ana ekrandaki **Keşif** düğmesiyle açılır.

- **Küme haritası:** node → namespace → service'ler ve pod kartları, canlı (Kubernetes API, birkaç saniyede bir).
  Renk yalnızca durum için (yeşil/sarı/kırmızı). Kartta CPU, bellek/limit çubuğu, kendi normalinden sapma (↑ z) ve
  açık olay şeridi. Service → pod çizgileri üzerine gelince ya da seçince görünür (istenirse hepsi); "Veri yolu"
  kubelet → kube-state-metrics → Prometheus → nöbetçi akışını numaralar. Her nesneye tıklayınca ne olduğunu,
  durumunu, sahiplik zincirini ve denenecek `kubectl` komutlarını anlatır. Son Kubernetes olayları ve rehberli tur.
- **Simülasyonlar** (kümeye dokunmaz): pod silinirse, ölçeklenirse, bellek sızarsa, node çökerse adım adım ne olur.
  Adımlar kendiliğinden ilerlemez; "Sonraki adım" düğmesi ya da → tuşuyla ilerlenir.
- **Öğren ve sına:** kavram haritası ve yedi aşamalı ders (pod, node ve kontrol düzlemi, istenen durum, Service,
  dışarıdan erişim, izleme, ayakta kalmak). Her aşamada kısa anlatım ve sorular; soruların bir kısmı canlı kümeden
  üretilir ("bu service hangi pod'lara gidiyor?", "bu pod silinirse ne olur?") ve cevaptan sonra haritada gösterilir.
- **Örnek sistem:** iki sunuculu bir kümede uzaktan alarm kontrolü (telefon → alarm-api → role-gecidi → röleler).
  Sunucu kapat, pod sil, kopya sayısını ve yaymayı değiştir; komutların düşüp düşmediğini ve Kubernetes'in tepkisini
  adım adım izle. Tarayıcıda çalışan bir modeldir. Gerçeği `./kur.sh ornek` ile kümeye kurulabilir
  (`deploy/ornek-alarm.yaml`, `alarm-ornek` namespace'i, busybox; `./kur.sh ornek-kaldir` ile silinir).
- **Nöbetçinin beyni:** tespit hattı bu turun gerçek sayılarıyla akar; aşamalara tıklayınca parametreleriyle anlatır.
  **Algoritma laboratuvarı:** gerçek bir seride eşik, kalıcılık ve pencereyi değiştirip kararın nasıl değiştiğini,
  medyan/MAD ile ortalama/standart sapma farkını ve limit tahminini görürsünüz (hesap `detect.py` ile birebir aynı).
- **Ne izleyelim?:** izlenen sinyaller ve eklenebilecek ölçümler (senaryo + PromQL); kümedeki Prometheus'ta zaten
  bulunanlar işaretlenir (`/api/olcumler`).

Harita için yetki listesine salt okunur `services`, `namespaces`, `deployments`, `replicasets`, `statefulsets`,
`daemonsets` eklendi; `guncelle` yeni yetkiyi kendiliğinden uygular. API okunamazsa harita Prometheus'taki pod'larla kurulur.

## Dışa aktarım ve alarmlar

- `/metrics` — Prometheus biçimi (node exporter gibi kazınır; pod'daki `prometheus.io/scrape` işaretiyle kümedeki
  Prometheus kendiliğinden toplar): `nobetci_up`, `nobetci_deviation` ve `nobetci_baseline` (her seri için sapma ve
  normal), `nobetci_incident_open`, `nobetci_open_incidents`, `nobetci_fill_eta_seconds`, `nobetci_findings_total`…
- `/api/alarmlar` — açık olayların sade JSON listesi (başka sistemlerin okuması için).
- Webhook — `NOBETCI_WEBHOOK_URL` tanımlıysa olay açılış/ciddileşme/kapanışında JSON gönderilir (`text` alanı Slack/Mattermost uyumlu).

## LLM yorumcusu (isteğe bağlı)

`./nobetci-kurulum.sh llm` ile seçilir: kapalı, Google AI Studio / Gemini API (Gemma), küme içinde Ollama ya da
herhangi bir OpenAI uyumlu uç. Anahtar `nobetci/nobetci-llm` Secret'ında tutulur. Dış servislere varsayılan olarak
yalnızca ölçüm özeti gider; pod logları ancak açıkça istenirse (`NOBETCI_LLM_LOGS=true`) gönderilir.

## Küme olmadan deneme

```bash
python3 tools/sahte_prometheus.py 9090 &      # aynı portta sahte Kubernetes API de var (tools/sahte_kube.py)
NOBETCI_PROM_URL=http://127.0.0.1:9090 NOBETCI_KUBE_URL=http://127.0.0.1:9090 NOBETCI_BIND=127.0.0.1 NOBETCI_DB=/tmp/nobetci.db \
NOBETCI_TIME_SPEED=120 python3 -m nobetci      # http://127.0.0.1:8080
python3 -m unittest discover -s tests
```

`NOBETCI_TIME_SPEED=120` sanal saati hızlandırır; senaryo (`tools/sahte_prometheus.py` başında yazılı)
yarım dakikada oynar.

## Ayarlar

Hepsi `NOBETCI_` önekli ortam değişkeni; tam liste ve varsayılanlar `nobetci/config.py` içinde.
En sık gerekenler: `PROM_URL`, `LLM_URL`, `LLM_MODEL`, `WEBHOOK_URL`, `NS_EXCLUDE` (regex),
`Z` (eşik, varsayılan 5), `INTERVAL` (sn, varsayılan 60).

## Bilinen sınırlar

- Saatlik profil saat çözünürlüğündedir: günlük bir işle aynı saate denk gelen benzer boyuttaki
  gerçek bir sıçrama da olağan sayılır.
- Profil en az 3 günlük Prometheus geçmişi ister; daha azında yalnızca yakın geçmişe bakılır.
- Yeni doğan pod'da ilk ~20 dakika istatistiksel tespit yapılmaz (durum kontrolleri çalışır).
- Prophet yok: aynı işi (mevsimsellik) bağımlılıksız saatlik profil görüyor.
