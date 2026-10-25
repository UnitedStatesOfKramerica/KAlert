#!/usr/bin/env python3
"""
Kalshi mispricing scanner, built to run on GitHub Actions every ~5 minutes.

Flags (read-only, public API, no key needed):
  GAP  - top order-book level is isolated from the next one (stale / fat-finger)
  JUMP - best bid/ask moved a lot since the last scan (liquid markets only)
  NEW  - recently opened market with no quotes or a wide spread

Outputs:
  - one ntfy push per run (if NTFY_TOPIC is set)
  - site/index.html + site/alerts.json  (deployed to GitHub Pages by the workflow)
  - state.json (kept between runs by the workflow cache)
"""
import argparse, json, os, time
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
S = requests.Session()
S.headers["User-Agent"] = "kalshi-alerts/1.0"
STATE_PATH, SITE = "state.json", "site"
PRIORITY = {"GAP": 0, "NEW": 1, "JUMP": 2}


# ---------- Kalshi API ----------
def get(path, params=None):
    for attempt in range(6):
        r = S.get(BASE + path, params=params, timeout=20)
        if r.status_code == 429:
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"rate limited: {path}")


def cents(m, key):
    v = m.get(key)
    if v is not None:
        return int(round(float(v)))
    v = m.get(key + "_dollars")
    if v is not None:
        return int(round(float(v) * 100))
    return None


def num(m, key):
    for k in (key, key + "_fp"):
        if m.get(k) is not None:
            return float(m[k])
    return 0.0


def label(m):
    title, sub = m.get("title") or "", m.get("yes_sub_title") or ""
    out = f"{title} ({sub})" if sub and sub not in title else title
    return out[:70]


def fetch_events():
    events, cursor = [], None
    while True:
        params = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        d = get("/events", params)
        events += d.get("events", [])
        cursor = d.get("cursor")
        if not cursor:
            return events


def orderbook(ticker):
    d = get(f"/markets/{ticker}/orderbook")
    ob = d.get("orderbook") or {}
    fp = d.get("orderbook_fp") or {}

    def lv(side):
        raw = ob.get(side)
        if raw:
            out = [(int(p), int(q)) for p, q in raw]
        else:
            out = [(round(float(p) * 100), int(float(q)))
                   for p, q in (fp.get(side + "_dollars") or [])]
        return sorted(out, reverse=True)

    return lv("yes"), lv("no")


def gap_flag(levels, min_gap, max_size):
    if len(levels) >= 2:
        (p1, q1), (p2, _) = levels[0], levels[1]
        if p1 - p2 >= min_gap and q1 <= max_size:
            return p1, q1, p2
    return None


# ---------- scan ----------
KIND_NAME = {"GAP": "Odd order", "JUMP": "Price jump", "NEW": "New market"}


def category_map(events):
    """series_ticker -> category, used only if events don't carry a category themselves."""
    cats = {}
    if any(not e.get("category") for e in events):
        try:
            for s in get("/series").get("series", []):
                cats[s.get("ticker")] = s.get("category")
        except Exception as ex:
            print("series lookup failed:", ex)
    return cats


def describe(e, m):
    title = (e.get("title") or m.get("title") or m.get("ticker") or "").strip()
    sub = (m.get("yes_sub_title") or "").strip()
    if not sub or sub.lower() == title.lower():
        sub = ""
    return title[:110], sub[:70]


def ago_text(dt):
    mins = max(0, (datetime.now(timezone.utc) - dt).total_seconds() / 60)
    return f"{mins:.0f} min ago" if mins < 90 else f"{mins/60:.1f} hours ago"


def scan(a, prev, emit):
    events = fetch_events()
    cats = category_map(events)
    markets = []
    for e in events:
        cat = e.get("category") or cats.get(e.get("series_ticker")) or "Other"
        for m in e.get("markets", []):
            m["_cat"] = cat
            m["_series"] = e.get("series_ticker") or ""
            m["_title"], m["_sub"] = describe(e, m)
            markets.append(m)

    new_prev, fresh = {}, []
    new_cut = datetime.now(timezone.utc) - timedelta(hours=a.new_hours)
    new_emitted = 0

    for m in markets:
        t = m["ticker"]
        yb, ya = cents(m, "yes_bid"), cents(m, "yes_ask")
        yb = yb if yb and yb > 0 else None
        ya = ya if ya and ya < 100 else None
        vol = num(m, "volume_24h")
        extra = f"24h volume {vol:,.0f}" if vol else ""

        p = prev.get(t)
        if p and vol >= a.jump_min_vol:
            for name, old, new in (("bid", p[0], yb), ("ask", p[1], ya)):
                if old is not None and new is not None and abs(new - old) >= a.jump:
                    what = "buyers are bidding" if name == "bid" else "sellers are asking"
                    emit("JUMP", m, f"YES {what} {new}\u00a2, was {old}\u00a2 at the last scan ({new-old:+d}\u00a2)",
                         f"{name}{new}", extra)
        new_prev[t] = [yb, ya]

        ot = m.get("open_time") or m.get("created_time")
        if ot:
            try:
                opened = datetime.fromisoformat(ot.replace("Z", "+00:00"))
            except ValueError:
                continue
            if opened >= new_cut:
                fresh.append(m)
                spread = (ya - yb) if (ya and yb) else None
                if (spread is None or spread >= a.wide_spread) and new_emitted < a.new_per_run:
                    if spread is None:
                        q = "no buyers or sellers yet" if not (yb or ya) else \
                            f"only one side quoted (bid {yb}\u00a2 / ask {ya}\u00a2)".replace("None\u00a2", "none")
                    else:
                        q = f"wide spread: bid {yb}\u00a2 vs ask {ya}\u00a2 ({spread}\u00a2 apart)"
                    if emit("NEW", m, f"Opened {ago_text(opened)}; {q}", "new", extra):
                        new_emitted += 1

    active = sorted(markets, key=lambda m: num(m, "volume_24h"), reverse=True)[:a.top]
    cands = {m["ticker"]: m for m in active + fresh[:100]}
    t0 = time.time()
    for t, m in cands.items():
        if time.time() - t0 > a.time_budget:
            print("book check time budget reached; stopping early")
            break
        try:
            yes, no = orderbook(t)
        except Exception as ex:
            print(f"skip {t}: {ex}")
            continue
        vol = num(m, "volume_24h")
        extra = f"24h volume {vol:,.0f}" if vol else ""
        g = gap_flag(yes, a.gap, a.max_size)
        if g:
            emit("GAP", m, f"One buyer bids {g[0]}\u00a2 for YES ({g[1]} contracts), but the next bid is only "
                           f"{g[2]}\u00a2, {g[0]-g[2]}\u00a2 lower", f"y{g[0]}", extra)
        g = gap_flag(no, a.gap, a.max_size)
        if g:
            emit("GAP", m, f"One seller offers YES at {100-g[0]}\u00a2 ({g[1]} contracts), but the next offer is "
                           f"{100-g[2]}\u00a2, {g[0]-g[2]}\u00a2 higher", f"n{g[0]}", extra)
        time.sleep(0.08)

    return len(events), len(markets), new_prev


# ---------- push ----------
def push(title, body, click=None):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("NTFY_TOPIC not set; skipping push")
        return
    headers = {"Title": title, "Tags": "chart_with_upwards_trend"}
    if click:
        headers["Click"] = click
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers, timeout=15)
    except Exception as ex:
        print("push failed:", ex)


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap", type=int, default=8, help="min cents between top and next level")
    ap.add_argument("--max-size", type=int, default=500, help="max contracts at the isolated level")
    ap.add_argument("--jump", type=int, default=10, help="cents moved since last scan")
    ap.add_argument("--jump-min-vol", type=float, default=100, help="min 24h volume for JUMP alerts")
    ap.add_argument("--new-hours", type=float, default=6)
    ap.add_argument("--wide-spread", type=int, default=10)
    ap.add_argument("--new-per-run", type=int, default=10, help="cap on NEW alerts per scan")
    ap.add_argument("--top", type=int, default=150, help="order books to check, by 24h volume")
    ap.add_argument("--time-budget", type=int, default=240, help="seconds for order-book checks")
    ap.add_argument("--dedupe-hours", type=float, default=24)
    ap.add_argument("--history", type=int, default=300)
    a = ap.parse_args()

    try:
        state = json.load(open(STATE_PATH))
    except Exception:
        state = {}
    now = time.time()
    seen = {k: v for k, v in state.get("seen", {}).items() if now - v < a.dedupe_hours * 3600}
    new_alerts = []

    def emit(kind, m, headline, key=None, extra=""):
        sk = f"{kind}|{m['ticker']}|{key or headline}"
        if sk in seen:
            return False
        seen[sk] = now
        series = m.get("_series") or ""
        new_alerts.append({"t": now, "kind": kind, "ticker": m["ticker"], "cat": m["_cat"],
                           "title": m["_title"], "sub": m["_sub"], "headline": headline, "extra": extra,
                           "url": f"https://kalshi.com/markets/{series.lower()}" if series else ""})
        print(f"{kind:<5} [{m['_cat']}] {m['_title']} | {headline}")
        return True

    info = {"events": 0, "markets": 0, "duration": None, "error": None}
    prev = state.get("prev", {})
    t0 = time.time()
    try:
        info["events"], info["markets"], prev = scan(a, prev, emit)
    except Exception as ex:
        info["error"] = f"{type(ex).__name__}: {ex}"[:300]
        print("scan error:", info["error"])
    info["duration"] = round(time.time() - t0, 1)

    new_alerts.sort(key=lambda x: PRIORITY.get(x["kind"], 9))
    old = [h for h in state.get("history", []) if "headline" in h]
    history = (new_alerts + old)[:a.history]

    page_url = os.environ.get("PAGES_URL")
    if new_alerts:
        counts = {}
        for x in new_alerts:
            counts[x["kind"]] = counts.get(x["kind"], 0) + 1
        title = f"Kalshi: {len(new_alerts)} new (" + ", ".join(f"{v} {KIND_NAME[k].lower()}" for k, v in counts.items()) + ")"
        lines = []
        for x in new_alerts[:5]:
            name = x["title"][:60] + (f" - {x['sub'][:30]}" if x["sub"] else "")
            lines.append(f"{KIND_NAME[x['kind']]} | {x['cat']} | {name}\n{x['headline']}")
        if len(new_alerts) > 5:
            lines.append(f"+{len(new_alerts) - 5} more on the page")
        push(title, "\n\n".join(lines), page_url)

    err_pushed = state.get("err_pushed", False)
    if info["error"] and not err_pushed:
        push("Kalshi scanner error", info["error"], page_url)
        err_pushed = True
    elif not info["error"]:
        err_pushed = False

    json.dump({"prev": prev, "seen": seen, "history": history, "err_pushed": err_pushed},
              open(STATE_PATH, "w"), separators=(",", ":"))
    os.makedirs(SITE, exist_ok=True)
    json.dump({"updated": time.time(), "scan": info, "alerts": history},
              open(os.path.join(SITE, "alerts.json"), "w"))
    open(os.path.join(SITE, "index.html"), "w").write(PAGE)
    print(f"done: {len(new_alerts)} new alerts, {info['markets']} markets, {info['duration']}s")


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalshi Alerts</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--text:#14181f;--muted:#667085;--line:#e4e7ec;
--gap:#2563eb;--jump:#7c3aed;--new:#0e9aa7;--red:#dc2626;--amber:#b7791f;--accent:#14181f}
@media (prefers-color-scheme:dark){:root{--bg:#0e1115;--card:#171b21;--text:#eceef1;--muted:#8d95a3;--line:#262c35;
--gap:#6b9bff;--jump:#a78bfa;--new:#2dd4d4;--red:#f87171;--amber:#f0b429;--accent:#eceef1}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:14px}
h1{font-size:19px;margin:2px 0 2px}
.sub{color:var(--muted);font-size:13px;margin-bottom:12px}
.warn{color:var(--amber)}.bad{color:var(--red)}
.search{width:100%;padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--text);font-size:15px;margin-bottom:10px}
.lbl{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:8px 0 5px}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:999px;padding:6px 12px;font-size:13px;cursor:pointer}
.chip.on{background:var(--accent);color:var(--bg);border-color:var(--accent)}
.chip small{opacity:.65;margin-left:3px}
.count{color:var(--muted);font-size:13px;margin:14px 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--line);border-radius:10px;padding:11px 13px;margin-bottom:9px}
.card.GAP{border-left-color:var(--gap)}.card.JUMP{border-left-color:var(--jump)}.card.NEW{border-left-color:var(--new)}
.top{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;margin-bottom:5px}
.kind{font-weight:700}.GAP .kind{color:var(--gap)}.JUMP .kind{color:var(--jump)}.NEW .kind{color:var(--new)}
.cat{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:1px 7px;color:var(--muted)}
.age{margin-left:auto;color:var(--muted)}
.title{font-weight:650;font-size:15.5px}
.osub{color:var(--muted);font-size:14px}
.head{margin-top:6px}
.meta{margin-top:6px;color:var(--muted);font-size:12.5px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.meta a{color:var(--gap)}
.tk{font-family:ui-monospace,Menlo,Consolas,monospace}
.empty{padding:28px;text-align:center;color:var(--muted)}
</style></head><body><div class="wrap">
<h1>Kalshi alerts</h1>
<div class="sub" id="sub">loading...</div>
<input class="search" id="q" type="search" placeholder="Search markets (e.g. Fed, NBA, Oscars)">
<div class="lbl">Topic</div><div class="chips" id="cats"></div>
<div class="lbl">Type</div><div class="chips" id="kinds"></div>
<div class="count" id="count"></div>
<div id="feed"></div>
</div>
<script>
const KN={GAP:"Odd order",JUMP:"Price jump",NEW:"New market"};
const KDESC={GAP:"a lone order far from the rest of the book",JUMP:"price moved a lot since the last scan",NEW:"recently opened, not priced yet"};
let S={cat:"ALL",kind:"ALL",q:""},data=null;
try{Object.assign(S,JSON.parse(localStorage.getItem("kalshi-filters")||"{}"));S.q=""}catch(e){}
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const ago=t=>{const s=Math.max(0,Math.round(Date.now()/1000-t));
 return s<90?s+"s ago":s<5400?Math.round(s/60)+" min ago":s<172800?Math.round(s/3600)+" h ago":Math.round(s/86400)+" d ago"};
function save(){try{localStorage.setItem("kalshi-filters",JSON.stringify({cat:S.cat,kind:S.kind}))}catch(e){}}
function chip(label,n,on,fn){const b=document.createElement("button");b.className="chip"+(on?" on":"");
 b.innerHTML=esc(label)+"<small>"+n+"</small>";b.onclick=fn;return b}
function render(){
 if(!data)return;
 const sc=data.scan,age=Date.now()/1000-data.updated;
 let sub="Updated "+ago(data.updated)+" \u00b7 "+(sc.markets||0).toLocaleString()+" markets scanned";
 if(age>1200)sub+=' <span class="warn">(stale: scans may be paused)</span>';
 if(sc.error)sub+='<br><span class="bad">Last scan failed: '+esc(sc.error)+'</span>';
 document.getElementById("sub").innerHTML=sub;
 const q=S.q.trim().toLowerCase();
 const txt=a=>(a.title+" "+a.sub+" "+a.ticker+" "+a.cat).toLowerCase();
 const base=data.alerts.filter(a=>!q||txt(a).includes(q));
 const byKind=base.filter(a=>S.kind==="ALL"||a.kind===S.kind);
 const cats={};byKind.forEach(a=>cats[a.cat]=(cats[a.cat]||0)+1);
 const ce=document.getElementById("cats");ce.innerHTML="";
 ce.appendChild(chip("All",byKind.length,S.cat==="ALL",()=>{S.cat="ALL";save();render()}));
 Object.keys(cats).sort((x,y)=>cats[y]-cats[x]).forEach(c=>ce.appendChild(chip(c,cats[c],S.cat===c,()=>{S.cat=c;save();render()})));
 const byCat=base.filter(a=>S.cat==="ALL"||a.cat===S.cat);
 const ke=document.getElementById("kinds");ke.innerHTML="";
 ke.appendChild(chip("All",byCat.length,S.kind==="ALL",()=>{S.kind="ALL";save();render()}));
 ["GAP","JUMP","NEW"].forEach(k=>ke.appendChild(chip(KN[k],byCat.filter(a=>a.kind===k).length,S.kind===k,()=>{S.kind=k;save();render()})));
 const list=base.filter(a=>(S.cat==="ALL"||a.cat===S.cat)&&(S.kind==="ALL"||a.kind===S.kind));
 document.getElementById("count").textContent=S.kind!=="ALL"?KN[S.kind]+": "+KDESC[S.kind]:"Newest first";
 document.getElementById("feed").innerHTML=list.length?list.map(a=>{
  const url=(a.url||"").indexOf("https://kalshi.com/")===0?a.url:"";
  return '<div class="card '+esc(a.kind)+'"><div class="top"><span class="kind">'+esc(KN[a.kind]||a.kind)+
  '</span><span class="cat">'+esc(a.cat)+'</span><span class="age">'+ago(a.t)+'</span></div>'+
  '<div class="title">'+esc(a.title)+'</div>'+(a.sub?'<div class="osub">'+esc(a.sub)+'</div>':"")+
  '<div class="head">'+esc(a.headline)+'</div><div class="meta">'+(a.extra?'<span>'+esc(a.extra)+'</span>':"")+
  '<span class="tk">'+esc(a.ticker)+'</span>'+(url?'<a href="'+esc(url)+'" target="_blank" rel="noopener">Open on Kalshi</a>':"")+
  '</div></div>'}).join("")
  :'<div class="empty">Nothing matches these filters.</div>';
}
document.getElementById("q").addEventListener("input",e=>{S.q=e.target.value;render()});
async function load(){try{const r=await fetch("alerts.json?"+Date.now());data=await r.json();render()}
 catch(e){document.getElementById("sub").textContent="Could not load alerts.json"}}
load();setInterval(load,60000);
</script></body></html>"""

if __name__ == "__main__":
    main()
