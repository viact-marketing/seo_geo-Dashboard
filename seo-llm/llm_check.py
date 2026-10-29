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
GEM_CHOICE = {}


def gemini_models(key):
    """Newest Gemini 'flash' models this key can use (old models reject new AQ. keys)."""
    names = []
    try:
        req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", headers={"x-goog-api-key": key})
        with urllib.request.urlopen(req, timeout=30) as r:
            for m in json.loads(r.read().decode()).get("models", []):
                n = m.get("name", "").split("/")[-1]
                if "generateContent" in (m.get("supportedGenerationMethods") or []) and "flash" in n and not re.search(r"(image|tts|audio|live|embed|thinking|exp|preview|lite)", n):
                    names.append(n)
    except Exception as ex:
        print("Could not list Gemini models:", str(ex)[:200], flush=True)
    def ver(n):
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return float(m.group(1)) if m else 0
    names.sort(key=ver, reverse=True)
    pref = os.environ.get("GEMINI_MODEL")
    out = ([pref] if pref else []) + names + ["gemini-3.6-flash", "gemini-3-flash", "gemini-2.5-flash"]
    seen = []
    for n in out:
        if n and n not in seen:
            seen.append(n)
    return seen


GEM_TRIES = []


def gemini_interactions(model, q, key):
    """New Interactions API (works with the new AQ. auth keys)."""
    url = "https://generativelanguage.googleapis.com/v1beta/interactions"
    r = post(url, {"model": model, "input": q, "tools": [{"type": "google_search"}]}, {"x-goog-api-key": key, "Api-Revision": "2026-05-20"})
    text, sources = "", []
    for st in (r.get("steps") or r.get("outputs") or []):
        if st.get("type") not in (None, "model_output", "text"):
            continue
        items = st.get("content") if isinstance(st.get("content"), list) else [st]
        for c in items:
            if c.get("text"):
                text += c["text"]
            for an in c.get("annotations") or []:
                if an.get("url"):
                    sources.append({"url": an["url"], "title": an.get("title", "")})
    return text, sources


def gemini_generate(model, q, key):
    """Older generateContent API (works with AIza keys)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    r = post(url, {"contents": [{"parts": [{"text": q}]}], "tools": [{"google_search": {}}]}, {"x-goog-api-key": key})
    cand = (r.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
    sources = []
    for c in (cand.get("groundingMetadata") or {}).get("groundingChunks") or []:
        w = c.get("web") or {}
        sources.append({"url": w.get("uri", ""), "title": w.get("title", "")})
    return text, sources


def ask_gemini(q, key):
    if "model" in GEM_CHOICE:
        model, api = GEM_CHOICE["model"], GEM_CHOICE["api"]
        text, sources = api(model, q, key)
    else:
        pref = [os.environ["GEMINI_MODEL"]] if os.environ.get("GEMINI_MODEL") else []
        models = []
        for m in pref + ["gemini-3.5-flash", "gemini-3.6-flash"] + gemini_models(key) + ["gemini-3-flash"]:
            if m and m not in models:
                models.append(m)
        last = None
        for api in (gemini_interactions, gemini_generate):
            for model in models[:5]:
                try:
                    text, sources = api(model, q, key)
                    GEM_CHOICE.update(model=model, api=api)
                    GEM_TRIES.append(f"{api.__name__} {model}: OK")
                    print("Gemini in use:", api.__name__, model, flush=True)
                    break
                except urllib.error.HTTPError as ex:
                    last = ex
                    body = ex.read().decode(errors="ignore")
                    reason = (re.search(r'"reason":\s*"([A-Z_]+)"', body) or re.search(r'"message":\s*"([^"]{0,120})', body))
                    GEM_TRIES.append(f"{api.__name__} {model}: HTTP {ex.code} {reason.group(1) if reason else ''}")
                    print(GEM_TRIES[-1], flush=True)
                    if ex.code not in (400, 401, 403, 404):
                        raise
            if "model" in GEM_CHOICE:
                break
        else:
            raise RuntimeError("No Gemini model/API accepted this key: " + " | ".join(GEM_TRIES[-10:]))
    for s_ in sources:
        if is_viact(s_.get("title")) or "viact" in (s_.get("title") or "").lower() or "grounding-api-redirect" in s_.get("url", "") and "viact" in (s_.get("title") or "").lower():
            s_["url"] = resolve(s_["url"])            # record the real viact.ai page
    return GEM_CHOICE.get("model"), text, sources


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
    def clean(v):
        v = (v or "").strip().strip('"').strip("'").strip()
        if "=" in v and v.split("=", 1)[0].strip().upper().endswith("KEY"):
            v = v.split("=", 1)[1].strip().strip('"').strip("'")      # pasted as NAME=value
        return v
    active = [(k, clean(os.environ.get(env)), fn) for k, env, fn in ENGINES]
    for (k, key, _), (_, env, _) in zip(active, ENGINES):
        if key:   # safe fingerprint only: never print the key itself
            print(f"{env}: length {len(key)}, starts with '{key[:4]}', spaces inside: {' ' in key}", flush=True)
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
    if GEM_TRIES:
        out["engines"]["gem"]["tries"] = GEM_TRIES[:12]
        for p in (os.path.join(OUT, "latest.json"), os.path.join(OUT, "history", d + ".json")):
            json.dump(out, open(p, "w"), ensure_ascii=False, indent=1)
    print("engines:", json.dumps(out["engines"]))
    if not any(v["enabled"] for v in out["engines"].values()):
        print("::warning::No AI API keys set; nothing checked.")


if __name__ == "__main__":
    main()
