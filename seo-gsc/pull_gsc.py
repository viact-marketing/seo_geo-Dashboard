"""Daily Search Console pull for viact.ai -> seo-gsc/data/latest.json

Same method as the SEO + GEO tracking file:
  search_analytics, dimensions query,page, 28 days, rowLimit 1000,
  grouped by query, impression-weighted average position.
Needs secret GSC_SERVICE_ACCOUNT_JSON (full JSON key of a Google service
account that has been added as a user on the Search Console property).
"""
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

SITE_URL = os.environ.get("GSC_SITE_URL", "https://www.viact.ai/")
OUT = os.path.join(os.path.dirname(__file__), "data", "latest.json")
IST = timezone(timedelta(hours=5, minutes=30))

now = datetime.now(IST)
end = (now - timedelta(days=2)).date()        # Search Console lags ~2 days
start = end - timedelta(days=28)

creds = service_account.Credentials.from_service_account_info(
    json.loads(os.environ["GSC_SERVICE_ACCOUNT_JSON"]),
    scopes=["https://www.googleapis.com/auth/webmasters.readonly"],
)
gsc = build("searchconsole", "v1", credentials=creds, cache_discovery=False)
rows = gsc.searchanalytics().query(
    siteUrl=SITE_URL,
    body={
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "dimensions": ["query", "page"],
        "rowLimit": 1000,
    },
).execute().get("rows", [])

# Group by query: impression-weighted position, total clicks, top page
agg = defaultdict(lambda: {"impr": 0, "clicks": 0, "posw": 0.0, "pages": defaultdict(int)})
for r in rows:
    q, page = r["keys"]
    a = agg[q]
    a["impr"] += r["impressions"]
    a["clicks"] += r["clicks"]
    a["posw"] += r["position"] * r["impressions"]
    a["pages"][page] += r["impressions"]

keywords, striking = [], []
tot_impr = tot_posw = 0
for q, a in agg.items():
    if not a["impr"]:
        continue
    pos = a["posw"] / a["impr"]
    tot_impr += a["impr"]
    tot_posw += a["posw"]
    keywords.append({"q": q, "pos": round(pos, 1), "impr": a["impr"]})
    if 10 < pos <= 20:  # page 2: "striking distance"
        striking.append({
            "q": q, "pos": round(pos, 1), "impr": a["impr"], "clicks": a["clicks"],
            "page": max(a["pages"], key=a["pages"].get),
        })

keywords.sort(key=lambda k: -k["impr"])


def sitewide(s, e):
    """Search Console's own totals (same as the Performance report), no row limit."""
    r = gsc.searchanalytics().query(
        siteUrl=SITE_URL, body={"startDate": s.isoformat(), "endDate": e.isoformat()}
    ).execute().get("rows", [])
    if not r:
        return None
    r = r[0]
    return {
        "window": f"{s.isoformat()}..{e.isoformat()}",
        "clicks": r["clicks"],
        "impressions": r["impressions"],
        "ctr": round(r["ctr"] * 100, 2),        # percent
        "position": round(r["position"], 1),
    }

site = {
    "d28": sitewide(end - timedelta(days=27), end),   # matches the "28 days" button
    "d7": sitewide(end - timedelta(days=6), end),
    "d1": sitewide(end, end),
}
striking.sort(key=lambda k: -k["impr"])

out = {
    "date": now.date().isoformat(),
    "fetchedAt": now.isoformat(timespec="seconds"),
    "siteUrl": SITE_URL,
    "window": f"{start.isoformat()}..{end.isoformat()}",
    "queries": len(keywords),
    "avgPosition": round(tot_posw / tot_impr, 1) if tot_impr else None,
    "top10": sum(1 for k in keywords if k["pos"] <= 10.0),
    "sitewide": site,
    "striking": striking[:50],
    "keywords": keywords,
}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    json.dump(out, f, indent=1)
print(f"Saved {OUT}: avgPosition={out['avgPosition']} top10={out['top10']} queries={out['queries']}")
