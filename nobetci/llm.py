"""LLM yorumcusu: olay başına tek, kısa bir çağrı. OpenAI uyumlu her uçla çalışır (Ollama, llama.cpp, vLLM...).

Küçük modelden verim almak için model karar vermez, araç çağırmaz; hazır kanıtı üç satırda özetler."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import Config
from .kube import Kube
from .signals import fmt_duration

SYSTEM = (
    "Sen bir Kubernetes nöbetçi asistanısın. Sana bir olayın ölçüm ve kayıt kanıtları verilecek. "
    "Yalnızca bu kanıtlara dayan; kanıtta olmayan bir şeyi uydurma, emin değilsen 'olabilir' de. "
    "Sade Türkçe yaz, komut yazma. Tam olarak şu üç satırı üret, her biri en çok iki cümle:\n"
    "ÖZET: ne oldu\nNEDEN: en olası sebep\nÖNERİ: ilk yapılacak iş"
)

_LABELS = {"ÖZET": "ozet", "OZET": "ozet", "NEDEN": "neden", "ÖNERİ": "oneri", "ÖNERI": "oneri", "ONERI": "oneri"}
_SPLIT = re.compile(r"^[\s*#>-]*(" + "|".join(_LABELS) + r")\s*[*]*\s*:\s*[*]*", re.M)


class LLMError(Exception):
    pass


def parse(text: str) -> dict | None:
    """Model çıktısından ÖZET/NEDEN/ÖNERİ alanlarını ayıklar; üçü de yoksa None."""
    parts = _SPLIT.split(text)
    out = {_LABELS[parts[i]]: " ".join(parts[i + 1].split()) for i in range(1, len(parts) - 1, 2)}
    return out if all(out.get(k) for k in ("ozet", "neden", "oneri")) else None


class LLM:
    def __init__(self, cfg: Config):
        base = cfg.llm_url.rstrip("/")
        self.enabled = bool(base)
        # Yalnızca host:port verildiyse (Ollama, vLLM) /v1 eklenir; yol içeren adres olduğu gibi kullanılır
        path = urllib.parse.urlsplit(base).path
        self.url = (base if path else base + "/v1") + "/chat/completions"
        self.provider = urllib.parse.urlsplit(base).hostname or ""
        self.model = cfg.llm_model
        self.key = cfg.llm_key
        self.timeout = cfg.llm_timeout
        self.calls = 0
        self.failures = 0
        self.last_error = ""

    def summarise(self, context: str) -> dict:
        body = {
            "model": self.model, "temperature": 0.2, "max_tokens": 300, "stream": False,
            # Gemma gibi bazı modeller (ör. Gemini API'de) sistem mesajını kabul etmiyor; tek kullanıcı mesajı her yerde çalışır
            "messages": [{"role": "user", "content": SYSTEM + "\n\n---\n\n" + context}],
        }
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers)
        self.calls += 1
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                text = json.load(r)["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            self.failures += 1
            detail = e.read().decode("utf-8", "replace")[:300]
            self.last_error = f"HTTP {e.code}: {detail}"
            raise LLMError(self.last_error) from e
        except (OSError, ValueError, KeyError, IndexError) as e:
            self.failures += 1
            self.last_error = str(e)
            raise LLMError(str(e)) from e
        parsed = parse(text)
        if parsed is None:
            self.failures += 1
            self.last_error = "çıktı beklenen biçimde değil"
            raise LLMError(self.last_error + ": " + text[:200])
        self.last_error = ""
        return parsed | {"source": "llm", "model": self.model, "seconds": round(time.time() - started, 1)}


def _clock(ts: float, tz: float) -> str:
    return time.strftime("%H:%M", time.gmtime(ts + tz * 3600))


def _pod_lines(pod: dict) -> list[str]:
    st = pod.get("status", {})
    lines = [f"Pod {pod['metadata']['name']}: durum={st.get('phase')}, node={pod.get('spec', {}).get('nodeName')}"]
    specs = {c["name"]: c for c in pod.get("spec", {}).get("containers", [])}
    for cs in st.get("containerStatuses", []):
        res = specs.get(cs["name"], {}).get("resources", {})
        line = f"- konteyner {cs['name']}: imaj={cs.get('image', '').rsplit('/', 1)[-1]}, yeniden başlama={cs.get('restartCount', 0)}"
        if res.get("limits"):
            line += ", limit=" + "/".join(f"{k}:{v}" for k, v in res["limits"].items())
        waiting = cs.get("state", {}).get("waiting")
        if waiting:
            line += f", bekliyor: {waiting.get('reason')} {waiting.get('message', '')[:120]}"
        term = cs.get("lastState", {}).get("terminated")
        if term:
            line += f", son kapanış: {term.get('reason')} (çıkış kodu {term.get('exitCode')}, {term.get('finishedAt')})"
        lines.append(line)
    return lines


def build_context(inc: dict, others: list[str], top: list[str], kube: Kube | None, cfg: Config, now: float) -> str:
    """Olayı küçük bir modelin tek seferde okuyabileceği kısa bir kanıt metnine çevirir."""
    sev = {1: "bilgi", 2: "uyarı", 3: "kritik"}
    lines = [f"Küme: {cfg.cluster}", f"Olay: {inc['title']}", f"Başlangıç: {_clock(inc['opened'], cfg.tz_offset)} "
             f"({fmt_duration(now - inc['opened'])} önce)", "", "Bulgular:"]
    for f in sorted(inc["findings"].values(), key=lambda f: -f["severity"]):
        who = f["pod"] or f["workload"]
        lines.append(f"- [{sev[f['severity']]}] {who}: {f['title']} (ilk {_clock(f['first_seen'], cfg.tz_offset)})")
    if kube:
        ns = inc["namespace"]
        crashed = {f["pod"]: f["detail"].get("container", "") for f in inc["findings"].values()
                   if f["pod"] and f["kind"] in ("oom", "waiting", "restart")}
        for name in sorted({f["pod"] for f in inc["findings"].values() if f["pod"]})[:2]:
            pod = kube.pod(ns, name)
            if pod:
                lines += ["", *_pod_lines(pod)]
            logs = kube.logs(ns, name, crashed.get(name, ""), previous=name in crashed) if cfg.llm_logs else []
            if logs:
                lines += ["", f"Son loglar ({name}{', çöken konteyner' if name in crashed else ''}):", *logs]
        events = kube.events(ns if inc["scope"] == "pod" else "", inc["workload"])
        if events:
            lines += ["", "Son Kubernetes olayları:"]
            lines += [f"- {e.get('type')} {e.get('reason')}: {(e.get('message') or '')[:160]}"
                      + (f" (x{e['count']})" if e.get("count", 1) > 1 else "") for e in events]
    if top:
        lines += ["", "Şu an en çok kaynak kullanan pod'lar:", *[f"- {t}" for t in top]]
    if others:
        lines += ["", "Aynı anda açık diğer olaylar:", *[f"- {o}" for o in others[:5]]]
    return "\n".join(lines)
