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
def scan(a, prev, emit):
    events = fetch_events()
    markets = [m for e in events for m in e.get("markets", [])]
    new_prev, fresh = {}, []
    new_cut = datetime.now(timezone.utc) - timedelta(hours=a.new_hours)
    new_emitted = 0

    for m in markets:
        t = m["ticker"]
        yb, ya = cents(m, "yes_bid"), cents(m, "yes_ask")
        yb = yb if yb and yb > 0 else None
        ya = ya if ya and ya < 100 else None
        vol = num(m, "volume_24h")
        lab = label(m)

        p = prev.get(t)
        if p and vol >= a.jump_min_vol:
            for name, old, new in (("bid", p[0], yb), ("ask", p[1], ya)):
                if old is not None and new is not None and abs(new - old) >= a.jump:
                    emit("JUMP", t, f"yes {name} {old} -> {new} | 24h vol {vol:.0f} | {lab}", f"{name}{new}")
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
                    if emit("NEW", t, f"opened {ot[:16]}Z | bid {yb} ask {ya} | {lab}", "new"):
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
        lab = label(m)
        g = gap_flag(yes, a.gap, a.max_size)
        if g:
            emit("GAP", t, f"YES bid {g[0]} x{g[1]} isolated, next level {g[2]} ({g[0]-g[2]}c gap) | {lab}", f"y{g[0]}")
        g = gap_flag(no, a.gap, a.max_size)
        if g:
            emit("GAP", t, f"YES ask {100-g[0]} x{g[1]} isolated, next level {100-g[2]} ({g[0]-g[2]}c gap) | {lab}", f"n{g[0]}")
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

    def emit(kind, ticker, msg, key=None):
        sk = f"{kind}|{ticker}|{key or msg}"
        if sk in seen:
            return False
        seen[sk] = now
        new_alerts.append({"t": now, "kind": kind, "ticker": ticker, "msg": msg})
        print(f"{kind:<5} {ticker}  {msg}")
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
    history = (new_alerts + state.get("history", []))[:a.history]

    page_url = os.environ.get("PAGES_URL")
    if new_alerts:
        counts = {}
        for x in new_alerts:
            counts[x["kind"]] = counts.get(x["kind"], 0) + 1
        title = f"Kalshi: {len(new_alerts)} new (" + ", ".join(f"{v} {k}" for k, v in counts.items()) + ")"
        lines = [f"{x['kind']} {x['ticker']}: {x['msg']}"[:160] for x in new_alerts[:6]]
        if len(new_alerts) > 6:
            lines.append(f"+{len(new_alerts) - 6} more on the page")
        push(title, "\n".join(lines), page_url)

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
:root{--bg:#f6f7f9;--card:#fff;--text:#14181f;--muted:#6b7280;--line:#e5e7eb;
--blue:#2563eb;--purple:#7c3aed;--teal:#0e9aa7;--red:#dc2626;--amber:#d98a00}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--text:#e8eaed;--muted:#8b93a1;--line:#262c35;
--blue:#5b8def;--purple:#a78bfa;--teal:#2dd4d4;--red:#f87171;--amber:#f0b429}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:14px}
h1{font-size:18px;margin:0 0 2px}
.sub{color:var(--muted);font-size:13px;margin-bottom:12px}
.warn{color:var(--amber)}.bad{color:var(--red)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:12px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 11px}
.stat b{display:block;font-size:19px}.stat span{color:var(--muted);font-size:12px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.chip{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:999px;padding:5px 11px;font-size:13px}
.chip.on{background:var(--text);color:var(--bg);border-color:var(--text)}
.feed{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.row{padding:10px 12px;border-bottom:1px solid var(--line)}
.row:last-child{border:0}
.top{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.badge{font-size:11px;font-weight:700;border-radius:6px;padding:2px 7px;color:#fff}
.GAP{background:var(--blue)}.JUMP{background:var(--purple)}.NEW{background:var(--teal)}
.tk{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;word-break:break-all}
.age{color:var(--muted);font-size:12px;margin-left:auto}
.msg{color:var(--muted);margin-top:3px;word-break:break-word}
.empty{padding:26px;text-align:center;color:var(--muted)}
</style></head><body><div class="wrap">
<h1>Kalshi alerts</h1>
<div class="sub" id="sub">loading…</div>
<div class="stats">
<div class="stat"><b id="s1">–</b><span>markets scanned</span></div>
<div class="stat"><b id="s2">–</b><span>alerts, last 24h</span></div>
<div class="stat"><b id="s3">–</b><span>last scan (s)</span></div>
</div>
<div class="chips" id="chips"></div>
<div class="feed" id="feed"><div class="empty">Loading…</div></div>
</div>
<script>
const KINDS=["ALL","GAP","JUMP","NEW"];let filter="ALL",data=null;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const ago=t=>{const s=Math.max(0,Math.round(Date.now()/1000-t));
 return s<90?s+"s ago":s<5400?Math.round(s/60)+"m ago":s<172800?Math.round(s/3600)+"h ago":Math.round(s/86400)+"d ago"};
function render(){
 if(!data)return;const sc=data.scan,age=Date.now()/1000-data.updated;
 let sub="Updated "+ago(data.updated);
 if(age>1200)sub+=' <span class="warn">(stale: scans may be paused)</span>';
 if(sc.error)sub+=' <span class="bad">· last scan failed: '+esc(sc.error)+'</span>';
 document.getElementById("sub").innerHTML=sub;
 document.getElementById("s1").textContent=sc.markets||"–";
 document.getElementById("s2").textContent=data.alerts.filter(a=>Date.now()/1000-a.t<86400).length;
 document.getElementById("s3").textContent=sc.duration??"–";
 const c=document.getElementById("chips");c.innerHTML="";
 KINDS.forEach(k=>{const n=k==="ALL"?data.alerts.length:data.alerts.filter(a=>a.kind===k).length;
  const b=document.createElement("button");b.className="chip"+(k===filter?" on":"");
  b.textContent=k+" "+n;b.onclick=()=>{filter=k;render()};c.appendChild(b)});
 const list=data.alerts.filter(a=>filter==="ALL"||a.kind===filter);
 document.getElementById("feed").innerHTML=list.length?list.map(a=>
  '<div class="row"><div class="top"><span class="badge '+a.kind+'">'+a.kind+'</span><span class="tk">'+esc(a.ticker)+
  '</span><span class="age">'+ago(a.t)+'</span></div><div class="msg">'+esc(a.msg)+'</div></div>').join("")
  :'<div class="empty">Nothing flagged yet. Real mispricings are rare.</div>';
}
async function load(){try{const r=await fetch("alerts.json?"+Date.now());data=await r.json();render()}
 catch(e){document.getElementById("sub").textContent="Could not load alerts.json"}}
load();setInterval(load,60000);
</script></body></html>"""

if __name__ == "__main__":
    main()
