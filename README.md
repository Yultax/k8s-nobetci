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

```bash
./kur.sh --kesif   # yalnızca inceler: Prometheus, gerekli ölçümler, docker, dış erişim
./kur.sh           # derler, kümeye yükler, kurar
./kur.sh --demo    # denemek için sorun çıkaran üç iş yükü
./kur.sh --kaldir
```

Sihirbaz Prometheus'u kendisi bulur, kind kümesinde imajı node'a doğrudan yükler (registry gerekmez),
LLM için üç seçenek sunar: LLM'siz, hazır bir OpenAI uyumlu uç ya da küme içinde Ollama.

## Küme olmadan deneme

```bash
python3 tools/sahte_prometheus.py 9090 &
NOBETCI_PROM_URL=http://127.0.0.1:9090 NOBETCI_BIND=127.0.0.1 NOBETCI_DB=/tmp/nobetci.db \
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
