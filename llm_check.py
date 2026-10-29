"""Daily AI-answer check for viact.ai.

Asks today's rotating group of buyer questions to the AI engines whose API keys
are set (GEMINI_API_KEY, OPENAI_API_KEY, PERPLEXITY_API_KEY) and records, per
engine, whether the real answer cites a viact.ai page (CITED), names viAct
without a link (MENTIONED) or neither (QUIET).

Writes seo-llm/data/latest.json and seo-llm/data/history/<date>.json.
Standard library only. Engines without a key are skipped.
"""
import json, os, re, sys, time, datetime, urllib.request, urllib.error
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
START = datetime.date(2026, 10, 30)          # group A day; same rotation as the daily audit
BRAND = re.compile(r"\bvi\s?act(\.ai)?\b", re.I)


def today_ist():
    return datetime.datetime.now(IST).date()


def pick_questions(qs, mode):
    d = today_ist()
    group = "ABCDE"[(d - START).days % 5]
    if mode == "all":
        return group, qs
    if mode in list("ABCDE"):
        group = mode
    return group, [q for q in qs if q["group"] == group]


def post(url, body, headers, timeout=90):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def resolve(url):
    """Follow a redirect link (Gemini returns redirect URLs) to the real page."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.geturl()
    except urllib.error.HTTPError as e:
        return e.geturl() or url
    except Exception:
        return url


def is_viact(url_or_title):
    s = (url_or_title or "").lower()
    host = urlparse(s).netloc if "://" in s else s
    return host == "viact.ai" or host.endswith(".viact.ai")


def score(text, sources):
    viact = [s["url"] for s in sources if is_viact(s.get("url")) or is_viact(s.get("title"))]
    named = bool(BRAND.search(text or ""))
    status = "CITED" if viact else ("MENTIONED" if named else "QUIET")
    domains = sorted({(urlparse(s["url"]).netloc or s.get("title") or "").replace("www.", "") for s in sources if s.get("url")})
    return {"status": status, "viactUrls": viact, "named": named, "sources": [s["url"] for s in sources][:15], "domains": domains[:15], "snippet": (text or "")[:400]}


# ---------- engines ----------
def ask_gemini(q, key):
    model = os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    r = post(url, {"contents": [{"parts": [{"text": q}]}], "tools": [{"google_search": {}}]}, {})
    cand = (r.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
    chunks = (cand.get("groundingMetadata") or {}).get("groundingChunks") or []
    sources = []
    for c in chunks:
        w = c.get("web") or {}
        u, t = w.get("uri", ""), w.get("title", "")
        if is_viact(t) or "viact" in (t or "").lower():
            u = resolve(u)                     # record the real viact.ai page
        sources.append({"url": u, "title": t})
    return model, text, sources


def ask_openai(q, key):
    model = os.environ.get("OPENAI_MODEL") or "gpt-4.1-mini"
    r = post("https://api.openai.com/v1/responses", {"model": model, "tools": [{"type": "web_search"}], "input": q}, {"Authorization": f"Bearer {key}"})
    text, sources = "", []
    for item in r.get("output", []):
        for c in item.get("content", []) or []:
            if c.get("type") == "output_text":
                text += c.get("text", "")
                for a in c.get("annotations", []) or []:
                    if a.get("url"):
                        sources.append({"url": a["url"], "title": a.get("title", "")})
    return model, text, sources


def ask_perplexity(q, key):
    model = os.environ.get("PERPLEXITY_MODEL") or "sonar"
    r = post("https://api.perplexity.ai/chat/completions", {"model": model, "messages": [{"role": "user", "content": q}]}, {"Authorization": f"Bearer {key}"})
    text = ((r.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    urls = [x.get("url") for x in (r.get("search_results") or []) if x.get("url")] or (r.get("citations") or [])
    return model, text, [{"url": u, "title": ""} for u in urls]


ENGINES = [("gem", "GEMINI_API_KEY", ask_gemini), ("gpt", "OPENAI_API_KEY", ask_openai), ("pplx", "PERPLEXITY_API_KEY", ask_perplexity)]


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("LLM_MODE") or "today").strip()
    qs = json.load(open(os.path.join(HERE, "questions.json")))
    group, todo = pick_questions(qs, mode)
    d = today_ist().isoformat()
    out = {"date": d, "fetchedAt": datetime.datetime.now(IST).isoformat(timespec="seconds"), "group": group if mode != "all" else "all", "engines": {}, "results": []}
    active = [(k, os.environ.get(env, "").strip(), fn) for k, env, fn in ENGINES]
    for k, key, _ in active:
        out["engines"][k] = {"enabled": bool(key), "ok": 0, "failed": 0, "model": None, "error": None}
    for q in todo:
        row = {"n": q["n"], "q": q["q"], "group": q["group"]}
        for k, key, fn in active:
            if not key:
                continue
            e = out["engines"][k]
            try:
                model, text, sources = fn(q["q"], key)
                e["model"] = model
                row[k] = score(text, sources)
                e["ok"] += 1
            except urllib.error.HTTPError as ex:
                msg = ex.read().decode(errors="ignore")[:300]
                row[k] = {"status": "ERROR", "error": f"HTTP {ex.code}: {msg}"}
                e["failed"] += 1; e["error"] = f"HTTP {ex.code}: {msg}"
            except Exception as ex:
                row[k] = {"status": "ERROR", "error": str(ex)[:300]}
                e["failed"] += 1; e["error"] = str(ex)[:300]
            time.sleep(1.5)
        out["results"].append(row)
        print(q["n"], {k: row.get(k, {}).get("status") for k, _, _ in ENGINES if k in row}, flush=True)
    os.makedirs(os.path.join(OUT, "history"), exist_ok=True)
    for p in (os.path.join(OUT, "latest.json"), os.path.join(OUT, "history", d + ".json")):
        json.dump(out, open(p, "w"), ensure_ascii=False, indent=1)
    print("engines:", json.dumps(out["engines"]))
    if not any(v["enabled"] for v in out["engines"].values()):
        print("::warning::No AI API keys set; nothing checked.")


if __name__ == "__main__":
    main()
