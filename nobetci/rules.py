"""Kural tabanlı özet ve komut önerileri. LLM yokken de olay boş kalmaz;
komutlar her zaman buradan gelir, LLM'e komut uydurtulmaz."""
from __future__ import annotations

# Öncelik sırasıyla: olayın "asıl" bulgusu ilk eşleşendir.
HINTS = {
    "node_notready": ("Node kubelet üzerinden sağlık bildirmiyor; makine kapanmış, ağdan kopmuş ya da kaynakları tükenmiş olabilir.",
                      "Node'un koşullarına bakın; üzerindeki pod'lar başka node'a taşınmadıysa müdahale gerekir."),
    "oom": ("Konteyner bellek limitinden fazlasını kullanmaya çalıştı ve çekirdek tarafından öldürüldü.",
            "Bellek limitini yükseltin ya da uygulamadaki bellek artışının kaynağını araştırın."),
    "waiting": ("Konteyner ayağa kalkamıyor: uygulama açılışta çöküyor, imaj çekilemiyor ya da yapılandırma eksik olabilir.",
                "Önceki konteynerin loglarına ve pod olaylarına bakın."),
    "fill": ("Bellek kullanımı istikrarlı biçimde artıyor; bellek sızıntısı ya da sürekli büyüyen bir kuyruk/önbellek olabilir.",
             "Limit dolmadan pod'u yeniden başlatmayı planlayın ve artışın kaynağını bulun."),
    "unavailable": ("İstenen kopya sayısına ulaşılamıyor; yeni pod'lar hazır olamıyor ya da yerleşemiyor.",
                    "Deployment'ın pod'larının durumuna ve son olaylara bakın."),
    "pending": ("Zamanlayıcı pod'u yerleştirecek node bulamıyor: kaynak yetersizliği, bekleyen PVC ya da node seçici uyuşmazlığı olabilir.",
                "describe çıktısındaki Events bölümü nedeni söyler."),
    "restart": ("Konteyner yeniden başladı; uygulama çökmüş ya da sağlık kontrolünden geçememiş olabilir.",
                "Önceki konteynerin loglarına bakın."),
    "spike": ("Ani yük artışı: trafik patlaması, ağır bir iş ya da döngüye girmiş bir süreç olabilir.",
              "Aynı andaki dağıtımlara ve istek sayısına bakın; sürüyorsa kopya sayısını artırmayı düşünün."),
    "drop": ("Kullanım aniden düştü: pod trafik alamıyor, takılmış ya da bağlı olduğu bir servis kesilmiş olabilir.",
             "Pod'un Ready olup olmadığını ve önündeki Service uçlarını kontrol edin."),
    "throttle": ("Konteyner CPU limitine dayanıyor ve yavaşlatılıyor.",
                 "CPU limitini yükseltin ya da yükü kopyalara dağıtın."),
}
PRIORITY = list(HINTS)


def top_finding(findings: dict[str, dict]) -> dict:
    return min(findings.values(), key=lambda f: (-f["severity"], PRIORITY.index(f["kind"]), f["first_seen"]))


def summary(title: str, findings: dict[str, dict]) -> dict:
    neden, oneri = HINTS[top_finding(findings)["kind"]]
    return {"ozet": title, "neden": neden, "oneri": oneri, "source": "kural"}


def commands(scope: str, namespace: str, workload: str, findings: dict[str, dict]) -> list[str]:
    kinds = {f["kind"] for f in findings.values()}
    if scope == "node":
        by = "memory" if any(f["signal"] == "node_mem" for f in findings.values()) else "cpu"
        return [f"kubectl describe node {workload}", f"kubectl top pods -A --sort-by={by} | head -15"]
    out = []
    for pod in sorted({f["pod"] for f in findings.values() if f["pod"]})[:2]:
        out.append(f"kubectl -n {namespace} describe pod {pod}")
        prev = " --previous" if kinds & {"oom", "waiting", "restart"} else ""
        out.append(f"kubectl -n {namespace} logs {pod} --tail=50{prev}")
        if kinds & {"spike", "fill", "throttle"}:
            out.append(f"kubectl -n {namespace} top pod {pod} --containers")
    if "unavailable" in kinds or not out:
        out.append(f"kubectl -n {namespace} get pods | grep {workload}")
    out.append(f"kubectl -n {namespace} get events --sort-by=.lastTimestamp | tail -20")
    return out
