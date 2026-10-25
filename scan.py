#!/usr/bin/env python3
"""
Kalshi consistency board. Runs on GitHub Actions every ~5 minutes (read-only, public API).

It looks for prices on the SAME underlying that cannot all be right:

  Ladders (e.g. "Rotten Tomatoes above 45 / 50 / 55", "CPI above 3.0 / 3.2 / 3.4"):
    * RISKLESS  - a higher threshold is bid above a lower threshold's ask
    * LIKELY MISPRICED - a range between two thresholds is priced far below what the
      ranges on either side imply, assuming the outcome has a single peak
  Exclusive outcomes (only one can win):
    * RISKLESS  - YES bids add up to more than 100

Sportsbook gaps (needs ODDS_API_KEY; totals, spreads and player props, not moneylines):
    * Kalshi is priced on the wrong side of the sportsbook consensus (vig removed) by a
      large margin. Lines don't need to match exactly: a Kalshi threshold easier than the book
      line must be worth at least the book probability, and a harder one at most that.

Cheap offers:
    * The lowest ask (or NO ask) sits far below the next one in the book.

Every candidate is re-checked against the live order book (for price and size). Results are
ranked by score = edge after fees x contracts available x a confidence weight.
A separate list shows newly opened markets that haven't been priced yet.

Outputs: site/index.html + site/board.json (deployed to GitHub Pages), state.json (cached),
and one ntfy push per run for new opportunities (if NTFY_TOPIC is set).
"""
import argparse, json, os, re, time
from collections import Counter
from datetime import datetime, timezone
from statistics import median

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
S = requests.Session()
S.headers["User-Agent"] = "kalshi-board/1.0"
STATE_PATH, SITE = "state.json", "site"
KIND_NAME = {"arb": "Riskless", "sum": "Riskless", "shape": "Likely mispriced",
             "book": "Sportsbook gap", "cheap": "Cheap offer"}
CONF = {"arb": 1.0, "sum": 1.0, "shape": 0.5, "book": 0.6, "cheap": 0.3}


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


def ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


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


def category_map(events):
    cats = {}
    if any(not e.get("category") for e in events):
        try:
            for s in get("/series").get("series", []):
                cats[s.get("ticker")] = s.get("category")
        except Exception as ex:
            print("series lookup failed:", ex)
    return cats


# ---------- helpers ----------
def fee_c(price_c):
    """Approx taker fee per contract in cents."""
    p = price_c / 100
    return 7 * p * (1 - p)


def fk(x):
    return str(int(x)) if float(x).is_integer() else str(x)


def quotes(m):
    yb, ya = cents(m, "yes_bid"), cents(m, "yes_ask")
    return (yb if yb and yb > 0 else None), (ya if ya and ya < 100 else None)


def describe(e, m):
    title = (e.get("title") or m.get("title") or m.get("ticker") or "").strip()
    sub = (m.get("yes_sub_title") or "").strip()
    if not sub or sub.lower() == title.lower():
        sub = ""
    return title[:110], sub[:70]


GT = {"greater", "greater_or_equal"}
LT = {"less", "less_or_equal"}
NUM = r"(-?\d[\d,]*\.?\d*)"


def to_f(s):
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def strike_of(m):
    """('gt'|'lt', number) if the market is a one-sided threshold, else None."""
    st = (m.get("strike_type") or "").lower()
    fl, cp = m.get("floor_strike"), m.get("cap_strike")
    if st in GT and fl is not None:
        return "gt", float(fl)
    if st in LT and (cp is not None or fl is not None):
        return "lt", float(cp if cp is not None else fl)
    if st:
        return None
    text = f"{m.get('yes_sub_title') or ''} {m.get('title') or ''}"
    for pat, d in ((r"(?:above|over|more than|greater than|at least|>=|>|\u2265)\s*\$?" + NUM, "gt"),
                   (r"(?:below|under|less than|at most|<=|<|\u2264)\s*\$?" + NUM, "lt"),
                   (r"\$?" + NUM + r"\s*(?:\+|or more|or above|or higher)", "gt")):
        r = re.search(pat, text, re.I)
        if r and to_f(r.group(1)) is not None:
            return d, to_f(r.group(1))
    return None


# ---------- ladders ----------
def build_ladders(markets):
    groups = {}
    for m in markets:
        s = strike_of(m)
        if s:
            groups.setdefault((m["_event"], s[0]), []).append((s[1], m))
    ladders = []
    for (ev, d), items in groups.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda x: x[0])
        legs, last = [], None
        for k, m in items:
            if k == last:
                continue
            last = k
            yb, ya = quotes(m)
            if d == "gt":
                bid, ask, flip = yb, ya, False
            else:  # "X < k" markets: use the NO side so every leg reads "X >= k"
                bid = 100 - ya if ya is not None else None
                ask = 100 - yb if yb is not None else None
                flip = True
            legs.append({"k": k, "bid": bid, "ask": ask, "flip": flip, "ticker": m["ticker"],
                         "label": m["_sub"] or fk(k), "bq": 0, "aq": 0})
        m0 = items[0][1]
        ladders.append({"event": ev, "cat": m0["_cat"], "title": m0["_title"],
                        "series": m0["_series"], "legs": legs})
    return ladders


def analyze(L, a):
    """Candidate bands (list prices). Band i sits between legs[i] and legs[i+1].
    Buying the band = buy S_i at its ask + buy NOT-S_{i+1} at 100 - bid. Pays 100 always, +100 in the band."""
    legs = [l for l in L["legs"] if l["bid"] is not None and l["ask"] is not None
            and l["ask"] - l["bid"] <= a.max_spread]
    out = []
    if len(legs) < 2:
        return out
    bands = []
    for i in range(len(legs) - 1):
        lo, hi = legs[i], legs[i + 1]
        w = hi["k"] - lo["k"]
        bands.append({"w": w, "dlow": max(0, lo["bid"] - hi["ask"]) / w})
    nb, spans = len(bands), []
    for i in range(nb):
        for j in range(i, nb):  # a span covers bands i..j, i.e. legs[i] to legs[j+1]
            lo, hi = legs[i], legs[j + 1]
            lb = 0.0
            if i > 0 and j < nb - 1:  # unimodal floor: density >= min(best on left, best on right)
                left = max(b["dlow"] for b in bands[:i])
                right = max(b["dlow"] for b in bands[j + 1:])
                lb = min(left, right) * (hi["k"] - lo["k"])
            cost = lo["ask"] - hi["bid"]
            fees = fee_c(lo["ask"]) + fee_c(100 - hi["bid"])
            est = max(-cost - fees, lb - cost - fees)
            if est >= a.min_edge:
                spans.append((est, i, j, lb, lo, hi))
    spans.sort(key=lambda x: x[0], reverse=True)
    used = set()
    for est, i, j, lb, lo, hi in spans:  # keep the best non-overlapping spans
        rng = set(range(i, j + 1))
        if rng & used:
            continue
        used |= rng
        out.append({"kind": "ladder", "L": L, "lo": dict(lo), "hi": dict(hi), "lb": lb, "est": est})
    return out


def apply_book(leg, book):
    yes, no = book
    yb = yes[0][0] if yes else None
    ya = 100 - no[0][0] if no else None
    yq = yes[0][1] if yes else 0
    nq = no[0][1] if no else 0
    if not leg["flip"]:
        leg["bid"], leg["ask"], leg["bq"], leg["aq"] = yb, ya, yq, nq
    else:
        leg["bid"] = 100 - ya if ya is not None else None
        leg["ask"] = 100 - yb if yb is not None else None
        leg["bq"], leg["aq"] = nq, yq


def band_item(c, a):
    L, lo, hi, lb = c["L"], c["lo"], c["hi"], c["lb"]
    if lo["ask"] is None or hi["bid"] is None:
        return None
    cost = lo["ask"] - hi["bid"]
    fees = fee_c(lo["ask"]) + fee_c(100 - hi["bid"])
    arb, shape = -cost - fees, lb - cost - fees
    size = min(lo["aq"], hi["bq"])
    if size < a.min_size:
        return None
    if arb >= a.min_edge:
        kind, edge = "arb", arb
    elif shape >= a.min_edge:
        kind, edge = "shape", shape
    else:
        return None
    k1, k2 = fk(lo["k"]), fk(hi["k"])
    legs = [{"action": "Buy NO" if lo["flip"] else "Buy YES", "ticker": lo["ticker"], "label": lo["label"],
             "price": lo["ask"], "qty": lo["aq"]},
            {"action": "Buy YES" if hi["flip"] else "Buy NO", "ticker": hi["ticker"], "label": hi["label"],
             "price": 100 - hi["bid"], "qty": hi["bq"]}]
    both = 100 + cost
    if kind == "arb":
        headline = f"Thresholds {k1} and {k2} are priced inconsistently"
        why = (f"The two legs together cost {both:.0f}¢ and pay at least 100¢ in every outcome, "
               f"so this locks in about {edge:.1f}¢ per contract after fees.")
    else:
        lo_mid, hi_mid = (lo["bid"] + lo["ask"]) / 2, (hi["bid"] + hi["ask"]) / 2
        headline = f"Range {k1} to {k2} looks underpriced"
        why = (f"\u201c{lo['label']}\u201d trades near {lo_mid:.0f}¢ and \u201c{hi['label']}\u201d near {hi_mid:.0f}¢, "
               f"so the market gives only about {max(0, lo_mid - hi_mid):.0f}% to a result between {k1} and {k2}. "
               f"The ranges on either side imply at least {lb:.0f}% if the outcome has a single peak. "
               f"Both legs cost {both:.0f}¢ and always pay at least 100¢, plus another 100¢ if the result lands "
               f"in that range. Net risk is about {cost + fees:.1f}¢ per contract.")
    return {"id": f"{L['event']}|{lo['ticker']}|{hi['ticker']}", "kind": kind, "cat": L["cat"],
            "title": L["title"], "headline": headline, "why": why, "legs": legs,
            "edge": round(edge, 2), "size": size, "profit": round(edge * size / 100, 2),
            "risk": round(cost + fees, 2), "fair": round(lb, 1),
            "url": f"https://kalshi.com/markets/{L['series'].lower()}" if L["series"] else ""}


# ---------- exclusive-outcome sums ----------
def sum_candidates(me_events, a):
    out = []
    for e, ms in me_events:
        if not (2 <= len(ms) <= 30) or any(strike_of(m) for m in ms):
            continue
        q = [quotes(m)[0] for m in ms]
        if not all(q):
            continue
        edge = sum(q) - 100 - sum(fee_c(x) for x in q)
        if edge >= a.min_edge:
            out.append({"kind": "sum", "e": e, "ms": ms, "est": edge})
    return out


def sum_item(c, books, a):
    ms, legs, bids = c["ms"], [], []
    for m in ms:
        yes, _ = books[m["ticker"]]
        if not yes:
            return None
        bids.append((m, yes[0][0], yes[0][1]))
    sumb = sum(b for _, b, _ in bids)
    edge = sumb - 100 - sum(fee_c(b) for _, b, _ in bids)
    size = min(q for _, _, q in bids)
    if edge < a.min_edge or size < a.min_size:
        return None
    n = len(bids)
    legs = [{"action": "Buy NO", "ticker": m["ticker"], "label": m["_sub"] or m["_title"],
             "price": 100 - b, "qty": q} for m, b, q in bids]
    m0 = ms[0]
    return {"id": f"{c['e'].get('event_ticker')}|sum", "kind": "sum", "cat": m0["_cat"], "title": m0["_title"],
            "headline": f"{n} exclusive outcomes add up to {sumb}¢ (over 100¢)",
            "why": (f"Only one of these outcomes can happen, yet the YES bids add up to {sumb}¢. Buying NO on all "
                    f"of them costs {100 * n - sumb}¢ per set and pays at least {100 * (n - 1)}¢, "
                    f"locking in about {edge:.1f}¢ per set after fees."),
            "legs": legs, "edge": round(edge, 2), "size": size, "profit": round(edge * size / 100, 2),
            "risk": 0, "fair": 0,
            "url": f"https://kalshi.com/markets/{m0['_series'].lower()}" if m0["_series"] else ""}


# ---------- sportsbook odds (optional, needs ODDS_API_KEY) ----------
ODDS_BASE = "https://api.the-odds-api.com/v4"
PROPS = {
    "basketball_nba": ["player_points", "player_rebounds", "player_assists", "player_threes"],
    "americanfootball_nfl": ["player_pass_yds", "player_rush_yds", "player_reception_yds", "player_receptions"],
    "icehockey_nhl": ["player_points", "player_shots_on_goal"],
}
STAT_WORDS = {
    "player_points": r"\bpoints?\b", "player_rebounds": r"\brebounds?\b", "player_assists": r"\bassists?\b",
    "player_threes": r"\b(?:threes?|3 ?pointers?|3pt)\b", "player_pass_yds": r"\bpass(?:ing)? (?:yards?|yds)\b",
    "player_rush_yds": r"\brush(?:ing)? (?:yards?|yds)\b", "player_reception_yds": r"\breceiving (?:yards?|yds)\b",
    "player_receptions": r"\breceptions?\b", "player_shots_on_goal": r"\bshots? on goal\b",
}
SUFFIX = {"jr", "sr", "ii", "iii", "iv"}


def norm(s):
    s = re.sub(r"[^a-z0-9\s]", " ", str(s).lower().replace("'", ""))
    return re.sub(r"\s+", " ", s).strip()


def norm_name(s):
    return " ".join(t for t in norm(s).split() if t not in SUFFIX)


def mascot(team):
    t = norm(team).split()
    return t[-1] if t else ""


def has(blob, word):
    return bool(word) and (" " + word + " ") in (" " + blob + " ")


def parse_line(m):
    """('over'|'under', half-integer line L). YES = X > L for over, YES = X < L for under."""
    txt = f"{m.get('yes_sub_title') or ''} {m.get('title') or ''}"
    r = re.search(r"(\d+(?:\.\d+)?)\s*\+", txt)
    if r:
        v = float(r.group(1))
        return "over", (v - 0.5 if v.is_integer() else v)
    r = re.search(r"\b(over|more than|above|greater than|at least)\s+(\d+(?:\.\d+)?)", txt, re.I)
    if r:
        v, ge = float(r.group(2)), r.group(1).lower() == "at least"
        return "over", ((v - 0.5 if ge else v + 0.5) if v.is_integer() else v)
    r = re.search(r"\b(under|fewer than|less than|below)\s+(\d+(?:\.\d+)?)", txt, re.I)
    if r:
        v = float(r.group(2))
        return "under", (v - 0.5 if v.is_integer() else v)
    s = strike_of(m)
    if s:
        d, k = s
        orq = (m.get("strike_type") or "").lower().endswith("_or_equal")
        if d == "gt":
            return "over", ((k - 0.5 if orq else k + 0.5) if float(k).is_integer() else k)
        return "under", ((k + 0.5 if orq else k - 0.5) if float(k).is_integer() else k)
    return None


def devig(over, under):
    po, pu = 1 / over, 1 / under
    return po / (po + pu)


def consensus(ev, min_books):
    """Median devigged probability per (market, who, line) across bookmakers."""
    acc = {}
    for bk in ev.get("bookmakers", []):
        for mk in bk.get("markets", []):
            key, outs = mk.get("key", ""), mk.get("outcomes", [])
            if key == "totals":
                grp = {}
                for o in outs:
                    grp.setdefault(o.get("point"), {})[o.get("name")] = o.get("price")
                for pt, d in grp.items():
                    if pt is not None and d.get("Over") and d.get("Under"):
                        acc.setdefault(("totals", "", pt, pt), []).append(devig(d["Over"], d["Under"]))
            elif key == "spreads":
                if len(outs) == 2 and all(o.get("point") is not None and o.get("price") for o in outs):
                    x, y = outs
                    px = devig(x["price"], y["price"])
                    acc.setdefault(("spreads", x["name"], x["point"], -x["point"]), []).append(px)
                    acc.setdefault(("spreads", y["name"], y["point"], -y["point"]), []).append(1 - px)
            elif key.startswith("player_"):
                grp = {}
                for o in outs:
                    grp.setdefault((o.get("description"), o.get("point")), {})[o.get("name")] = o.get("price")
                for (who, pt), d in grp.items():
                    if who and pt is not None and d.get("Over") and d.get("Under"):
                        acc.setdefault((key, who, pt, pt), []).append(devig(d["Over"], d["Under"]))
    lines = []
    for (mk, who, pt, L), ps in acc.items():
        if float(pt).is_integer() or len(ps) < min_books or max(ps) - min(ps) > 0.10:
            continue  # skip whole-number lines (pushes), thin consensus, or books that disagree
        lines.append({"mk": mk, "who": who, "pt": pt, "L": L, "p": round(median(ps), 4), "n": len(ps)})
    return lines


def odds_get(path, params, key, meter):
    r = requests.get(ODDS_BASE + path, params={**params, "apiKey": key}, timeout=25)
    try:
        meter["used"] = meter.get("used", 0) + float(r.headers.get("x-requests-last", 0))
        meter["remaining"] = float(r.headers["x-requests-remaining"])
    except Exception:
        pass
    r.raise_for_status()
    return r.json()


def refresh_odds(a, key, state, now):
    cache = state.get("odds") or {}
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    meter = dict(state.get("odds_meter") or {})
    if meter.get("day") != day:
        meter = {"day": day, "used": 0, "remaining": meter.get("remaining")}
    err = None
    if cache.get("events") and now - cache.get("t", 0) < a.odds_interval * 60:
        return cache, meter, err

    def can_spend():
        rem = meter.get("remaining")
        return meter["used"] < a.odds_daily_credits and (rem is None or rem > a.odds_reserve)

    events = []
    try:
        for sport in [s.strip() for s in a.odds_sports.split(",") if s.strip()]:
            if not can_spend():
                err = "credit limit reached; using older lines"
                break
            feat = odds_get(f"/sports/{sport}/odds", {"regions": "us", "markets": "totals,spreads",
                                                      "oddsFormat": "decimal"}, key, meter)
            recs = {}
            for ev in feat:
                st = ts(ev.get("commence_time"))
                if st:
                    recs[ev["id"]] = {"id": ev["id"], "sport": sport, "home": ev["home_team"],
                                      "away": ev["away_team"], "start": st, "lines": consensus(ev, a.min_books)}
            if sport in PROPS:
                soon = sorted((r for r in recs.values() if 0 < r["start"] - now < a.odds_horizon * 3600),
                              key=lambda r: r["start"])[:a.odds_max_events]
                for r in soon:
                    if not can_spend():
                        err = "credit limit reached; props only partly refreshed"
                        break
                    d = odds_get(f"/sports/{sport}/events/{r['id']}/odds",
                                 {"regions": "us", "markets": ",".join(PROPS[sport]), "oddsFormat": "decimal"},
                                 key, meter)
                    r["lines"] += consensus(d, a.min_books)
            events += list(recs.values())
    except Exception as ex:
        err = f"{type(ex).__name__}: {ex}"[:200]
    if events:
        cache = {"t": now, "events": events}
    return cache, meter, err


def match_books(markets, odds_events, a, now):
    dbg = {"sports_markets": 0, "with_line": 0, "matched": 0, "unmatched": []}
    live, ids = [], set()
    for e in odds_events:
        if e["id"] not in ids and now - 4 * 3600 <= e["start"] <= now + 60 * 3600:
            ids.add(e["id"])
            live.append(e)
    players = {}
    for e in live:
        for ln in e["lines"]:
            if ln["mk"].startswith("player_"):
                players.setdefault(norm_name(ln["who"]), []).append((e, ln))
    pnames = sorted(players, key=len, reverse=True)
    out = []
    for m in markets:
        if "sport" not in (m["_cat"] or "").lower():
            continue
        dbg["sports_markets"] += 1
        ol = parse_line(m)
        if not ol:
            continue
        dbg["with_line"] += 1
        orient, Lk = ol
        blob = norm(f"{m['_title']} {m['_sub']} {m.get('title') or ''} {m.get('subtitle') or ''}")
        recs = []
        hit = next((p for p in pnames if p and has(blob, p)), None)
        if hit:
            mks = [mk for mk, rx in STAT_WORDS.items()
                   if re.search(rx, blob) and any(l["mk"] == mk for _, l in players[hit])]
            if len(mks) == 1:
                recs = [(e, l) for e, l in players[hit] if l["mk"] == mks[0]]
        else:
            evs = [e for e in live if has(blob, mascot(e["home"])) and has(blob, mascot(e["away"]))]
            if len(evs) == 1:
                e = evs[0]
                if re.search(r"\btotals?\b|\bcombined\b|over under|points scored|goals scored|runs scored", blob):
                    recs = [(e, l) for l in e["lines"] if l["mk"] == "totals"]
                elif re.search(r"\bwins? by\b|\bspread\b|\bmargin\b|\bcover", blob):
                    sub = norm(m["_sub"] or m.get("yes_sub_title") or "")
                    team = [t for t in (e["home"], e["away"]) if has(sub, mascot(t))]
                    if len(team) == 1:
                        recs = [(e, l) for l in e["lines"] if l["mk"] == "spreads" and l["who"] == team[0]]
        if not recs:
            if len(dbg["unmatched"]) < 6:
                dbg["unmatched"].append(f"{m['_title']} | {m['_sub']}"[:120])
            continue
        dbg["matched"] += 1
        yb, ya = quotes(m)
        if orient == "over":
            bid, ask, flip = yb, ya, False
        else:
            bid = 100 - ya if ya is not None else None
            ask = 100 - yb if yb is not None else None
            flip = True
        best = None
        for e, l in recs:
            p100, Lb = l["p"] * 100, l["L"]
            if Lk <= Lb + 1e-9 and ask is not None:  # Kalshi needs an equal-or-easier result: worth >= book
                edge = p100 - ask - fee_c(ask)
                if edge >= a.book_min_edge and (not best or edge > best[0]):
                    best = (edge, "buy", e, l)
            if Lk >= Lb - 1e-9 and bid is not None:  # equal-or-harder result: worth <= book
                edge = bid - p100 - fee_c(bid)
                if edge >= a.book_min_edge and (not best or edge > best[0]):
                    best = (edge, "sell", e, l)
        if best:
            edge, side, e, l = best
            leg = {"k": Lk, "bid": bid, "ask": ask, "flip": flip, "ticker": m["ticker"],
                   "label": m["_sub"] or m["_title"], "bq": 0, "aq": 0}
            out.append({"kind": "book", "m": m, "leg": leg, "side": side, "rec": l, "ev": e, "est": edge})
    return out, dbg


def book_desc(l, e):
    if l["mk"] == "totals":
        return f"{e['away']} at {e['home']}: total over {l['L']:g}"
    if l["mk"] == "spreads":
        return f"{l['who']} margin over {l['L']:g}"
    return f"{l['who']} over {l['L']:g} {l['mk'].replace('player_', '').replace('_', ' ')}"


def book_item(c, a):
    leg, l, e, side, m = c["leg"], c["rec"], c["ev"], c["side"], c["m"]
    p100 = l["p"] * 100
    if side == "buy":
        if leg["ask"] is None:
            return None
        price, size = leg["ask"], leg["aq"]
        edge = p100 - price - fee_c(price)
        action, pay = ("Buy NO" if leg["flip"] else "Buy YES"), price
    else:
        if leg["bid"] is None:
            return None
        price, size = leg["bid"], leg["bq"]
        edge = price - p100 - fee_c(price)
        action, pay = ("Buy YES" if leg["flip"] else "Buy NO"), 100 - price
    if edge < a.book_min_edge or size < a.min_size:
        return None
    desc = book_desc(l, e)
    if side == "buy":
        why = (f"Sportsbooks (median of {l['n']}, vig removed) put \u201c{desc}\u201d at {p100:.0f}%. This Kalshi "
               f"market needs an equal-or-easier result, so it should be worth at least {p100:.0f}¢, "
               f"but it can be bought for {price}¢.")
    else:
        why = (f"Sportsbooks (median of {l['n']}, vig removed) put \u201c{desc}\u201d at {p100:.0f}%. This Kalshi "
               f"market needs an equal-or-harder result, so it should be worth at most {p100:.0f}¢, "
               f"but the market bids {price}¢ for it, which makes the opposite side cheap.")
    return {"id": f"{m['ticker']}|book|{side}", "kind": "book", "cat": m["_cat"], "title": m["_title"],
            "headline": f"Kalshi is {edge:.0f}¢ {'below' if side == 'buy' else 'above'} sportsbook prices",
            "why": why, "compare": f"{desc}: {p100:.0f}% across {l['n']} books",
            "legs": [{"action": action, "ticker": leg["ticker"], "label": leg["label"], "price": pay, "qty": size}],
            "edge": round(edge, 2), "size": size, "profit": round(edge * size / 100, 2), "risk": 0, "fair": 0,
            "url": f"https://kalshi.com/markets/{m['_series'].lower()}" if m["_series"] else ""}


# ---------- cheap offers ----------
def cheap_items(m, book, a):
    yes, no = book
    out = []
    sides = (("Buy YES", [(100 - p, q) for p, q in no], "YES"),    # YES asks, lowest first
             ("Buy NO", [(100 - p, q) for p, q in yes], "NO"))     # NO asks, lowest first
    for action, asks, nm in sides:
        if len(asks) < 2:
            continue
        (a1, q1), (a2, _) = asks[0], asks[1]
        if a2 - a1 >= a.ask_gap and q1 <= a.cheap_max_size and a1 >= a.cheap_min_price:
            edge = (a2 - a1) - fee_c(a1)
            if edge <= 0 or q1 < a.min_size:
                continue
            out.append({"id": f"{m['ticker']}|cheap|{nm}{a1}", "kind": "cheap", "cat": m["_cat"], "title": m["_title"],
                        "headline": f"{nm} is offered at {a1}¢; the next offer is {a2}¢",
                        "why": (f"The cheapest {nm} offer ({a1}¢, {q1} contracts) sits {a2 - a1}¢ below the next one "
                                f"({a2}¢). If the real price is closer to {a2}¢, this offer is cheap by about "
                                f"{edge:.0f}¢ after fees. It may also just be a mistake that gets pulled."),
                        "legs": [{"action": action, "ticker": m["ticker"], "label": m["_sub"] or nm,
                                  "price": a1, "qty": q1}],
                        "edge": round(edge, 2), "size": q1, "profit": round(edge * q1 / 100, 2), "risk": 0, "fair": 0,
                        "url": f"https://kalshi.com/markets/{m['_series'].lower()}" if m["_series"] else ""})
    return out


def new_markets(markets, a, now):
    cut, out = now - a.new_hours * 3600, []
    for m in markets:
        t_open = ts(m.get("open_time") or m.get("created_time") or "")
        if not t_open or t_open < cut:
            continue
        yb, ya = quotes(m)
        spread = (ya - yb) if (ya and yb) else None
        if spread is None or spread >= a.wide_spread:
            out.append({"ticker": m["ticker"], "cat": m["_cat"], "title": m["_title"], "sub": m["_sub"],
                        "opened": t_open, "bid": yb, "ask": ya,
                        "url": f"https://kalshi.com/markets/{m['_series'].lower()}" if m["_series"] else ""})
    out.sort(key=lambda x: x["opened"], reverse=True)
    return out[:a.new_keep]


# ---------- scan ----------
def scan(a, state, now, ctx):
    events = fetch_events()
    cats = category_map(events)
    markets, me_events = [], []
    for e in events:
        cat = e.get("category") or cats.get(e.get("series_ticker")) or "Other"
        ms = []
        for m in e.get("markets", []):
            m["_cat"], m["_event"] = cat, e.get("event_ticker") or ""
            m["_series"] = e.get("series_ticker") or ""
            m["_title"], m["_sub"] = describe(e, m)
            ms.append(m)
        markets += ms
        if e.get("mutually_exclusive"):
            me_events.append((e, ms))
    print("strike types:", dict(Counter((m.get("strike_type") or "none") for m in markets).most_common(8)))

    ladders = build_ladders(markets)
    cands = []
    for L in ladders:
        cands += analyze(L, a)
    cands += sum_candidates(me_events, a)

    books_info = {"enabled": False}
    key = os.environ.get("ODDS_API_KEY")
    if key:
        books_info["enabled"] = True
        try:
            cache, meter, err = refresh_odds(a, key, state, now)
            ctx["odds"], ctx["odds_meter"] = cache, meter
            books_info.update({"error": err, "credits_used_today": meter.get("used"),
                               "credits_remaining": meter.get("remaining"),
                               "odds_events": len(cache.get("events", [])),
                               "odds_lines": sum(len(e["lines"]) for e in cache.get("events", [])),
                               "age_min": round((now - cache.get("t", now)) / 60) if cache else None,
                               "odds_sample": [f"{e['away']} at {e['home']}" for e in cache.get("events", [])[:4]]})
            if cache.get("events") and now - cache["t"] <= a.odds_max_age * 60:
                bc, dbg = match_books(markets, cache["events"], a, now)
                books_info.update(dbg)
                cands += bc
            elif cache.get("events"):
                books_info["error"] = "sportsbook lines are too old to trust; waiting for a refresh"
        except Exception as ex:
            books_info["error"] = f"{type(ex).__name__}: {ex}"[:200]
        print("sportsbooks:", {k: v for k, v in books_info.items() if k not in ("unmatched", "odds_sample")})

    cands.sort(key=lambda c: c["est"], reverse=True)
    cands = cands[:a.max_books]
    print(f"{len(ladders)} ladders, {len(cands)} candidates to verify")

    need = []
    for c in cands:
        if c["kind"] == "ladder":
            ts_ = [c["lo"]["ticker"], c["hi"]["ticker"]]
        elif c["kind"] == "sum":
            ts_ = [m["ticker"] for m in c["ms"]]
        else:
            ts_ = [c["leg"]["ticker"]]
        c["tickers"] = ts_
        need += [t for t in ts_ if t not in need]

    books, t0 = {}, time.time()

    def fetch_book(t):
        if t in books:
            return True
        if time.time() - t0 > a.time_budget:
            return False
        try:
            books[t] = orderbook(t)
        except Exception as ex:
            print(f"skip {t}: {ex}")
            return False
        time.sleep(0.08)
        return True

    for t in need:
        if not fetch_book(t):
            if time.time() - t0 > a.time_budget:
                print("time budget reached; stopping book checks")
                break

    items = []
    for c in cands:
        if not all(t in books for t in c["tickers"]):
            continue
        if c["kind"] == "ladder":
            apply_book(c["lo"], books[c["lo"]["ticker"]])
            apply_book(c["hi"], books[c["hi"]["ticker"]])
            it = band_item(c, a)
        elif c["kind"] == "sum":
            it = sum_item(c, books, a)
        else:
            apply_book(c["leg"], books[c["leg"]["ticker"]])
            it = book_item(c, a)
        if it:
            items.append(it)

    recent = [m for m in markets if (ts(m.get("open_time") or m.get("created_time") or "") or 0) >= now - a.new_hours * 3600]
    pool = sorted(markets, key=lambda m: num(m, "volume_24h"), reverse=True)[:a.cheap_top] + recent[:30]
    seen_t = set()
    for m in pool:
        if m["ticker"] in seen_t:
            continue
        seen_t.add(m["ticker"])
        if fetch_book(m["ticker"]):
            items += cheap_items(m, books[m["ticker"]], a)

    for it in items:
        it["conf"] = CONF[it["kind"]]
        it["score"] = round(it["profit"] * it["conf"], 2)
    items.sort(key=lambda x: x["score"], reverse=True)
    watch = new_markets(markets, a, now)
    return items, watch, {"events": len(events), "markets": len(markets), "ladders": len(ladders),
                          "checked": len(cands), "books": books_info}


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
    ap.add_argument("--min-edge", type=float, default=2.0, help="min edge in cents/contract after fees")
    ap.add_argument("--min-size", type=int, default=5, help="min contracts available at those prices")
    ap.add_argument("--max-spread", type=int, default=25, help="ignore ladder legs with wider spreads (cents)")
    ap.add_argument("--max-books", type=int, default=60, help="candidates to verify against order books")
    ap.add_argument("--time-budget", type=int, default=200, help="seconds for order-book checks")
    ap.add_argument("--push-min-profit", type=float, default=3.0, help="min score ($) to send a push")
    ap.add_argument("--keep", type=int, default=200)
    # cheap offers
    ap.add_argument("--ask-gap", type=int, default=15, help="cents between best offer and the next one")
    ap.add_argument("--cheap-max-size", type=int, default=500, help="max contracts at the cheap offer")
    ap.add_argument("--cheap-min-price", type=int, default=5, help="ignore offers below this many cents")
    ap.add_argument("--cheap-top", type=int, default=80, help="most-traded markets to check for cheap offers")
    # new markets
    ap.add_argument("--new-hours", type=float, default=6)
    ap.add_argument("--wide-spread", type=int, default=10)
    ap.add_argument("--new-keep", type=int, default=40)
    # sportsbooks
    ap.add_argument("--book-min-edge", type=float, default=8.0, help="min gap vs sportsbooks, cents after fees")
    ap.add_argument("--min-books", type=int, default=3, help="min bookmakers agreeing on a line")
    ap.add_argument("--odds-sports", default="basketball_nba,americanfootball_nfl,icehockey_nhl")
    ap.add_argument("--odds-interval", type=int, default=60, help="minutes between sportsbook refreshes")
    ap.add_argument("--odds-max-age", type=int, default=150, help="ignore sportsbook lines older than this (min)")
    ap.add_argument("--odds-horizon", type=float, default=12, help="hours ahead to fetch player props for")
    ap.add_argument("--odds-max-events", type=int, default=10, help="games per sport to fetch props for")
    ap.add_argument("--odds-daily-credits", type=int, default=150, help="daily cap on Odds API credits")
    ap.add_argument("--odds-reserve", type=int, default=25, help="stop when this few credits remain")
    a = ap.parse_args()

    try:
        state = json.load(open(STATE_PATH, encoding="utf-8"))
    except Exception:
        state = {}
    now = time.time()
    ctx = {}
    info = {"events": 0, "markets": 0, "ladders": 0, "checked": 0, "duration": None, "error": None}
    t0 = time.time()
    try:
        items, watch, st = scan(a, state, now, ctx)
        info.update(st)
    except Exception as ex:
        info["error"] = f"{type(ex).__name__}: {ex}"[:300]
        print("scan error:", info["error"])
        items, watch = state.get("items", []), state.get("watch", [])
    info["duration"] = round(time.time() - t0, 1)

    fs = state.get("first_seen", {})
    for it in items:
        it["first_seen"] = fs.get(it["id"], now)
    items = items[:a.keep]

    pushed = {k: v for k, v in state.get("pushed", {}).items() if now - v < 86400}
    page_url = os.environ.get("PAGES_URL")
    if not info["error"]:
        fresh = [it for it in items if it["id"] not in pushed and it["score"] >= a.push_min_profit]
        if fresh:
            lines = [f"{KIND_NAME[it['kind']]} | {it['cat']} | {it['title'][:70]}\n"
                     f"{it['headline']}: +{it['edge']:.1f}¢ x {it['size']} = ${it['profit']:.2f}" for it in fresh[:5]]
            if len(fresh) > 5:
                lines.append(f"+{len(fresh) - 5} more on the page")
            push(f"Kalshi: {len(fresh)} new opportunit{'y' if len(fresh) == 1 else 'ies'}", "\n\n".join(lines), page_url)
            for it in fresh:
                pushed[it["id"]] = now

    err_pushed = state.get("err_pushed", False)
    if info["error"] and not err_pushed:
        push("Kalshi scanner error", info["error"], page_url)
        err_pushed = True
    elif not info["error"]:
        err_pushed = False

    json.dump({"items": items, "watch": watch, "first_seen": {it["id"]: it["first_seen"] for it in items},
               "pushed": pushed, "err_pushed": err_pushed,
               "odds": ctx.get("odds", state.get("odds")), "odds_meter": ctx.get("odds_meter", state.get("odds_meter"))},
              open(STATE_PATH, "w", encoding="utf-8"), separators=(",", ":"))
    os.makedirs(SITE, exist_ok=True)
    json.dump({"updated": now, "scan": info, "items": items, "watch": watch},
              open(os.path.join(SITE, "board.json"), "w", encoding="utf-8"))
    open(os.path.join(SITE, "index.html"), "w", encoding="utf-8").write(PAGE)
    print(f"done: {len(items)} opportunities, {len(watch)} new markets, {info['ladders']} ladders, "
          f"{info['markets']} markets, {info['duration']}s")


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalshi Board</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--text:#14181f;--muted:#667085;--line:#e4e7ec;
--green:#0f9d58;--blue:#2563eb;--purple:#7c3aed;--teal:#0e9aa7;--red:#dc2626;--amber:#b7791f;--accent:#14181f}
@media (prefers-color-scheme:dark){:root{--bg:#0e1115;--card:#171b21;--text:#eceef1;--muted:#8d95a3;--line:#262c35;
--green:#34c77b;--blue:#6b9bff;--purple:#a78bfa;--teal:#2dd4d4;--red:#f87171;--amber:#f0b429;--accent:#eceef1}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:14px}
h1{font-size:19px;margin:2px 0}
.sub{color:var(--muted);font-size:13px;margin-bottom:10px}
.warn{color:var(--amber)}.bad{color:var(--red)}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 12px;margin-bottom:10px;font-size:14px}
summary{cursor:pointer;font-weight:600}
details p{margin:8px 0 0;color:var(--muted)}
.search{width:100%;padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--text);font-size:15px;margin:8px 0 6px}
.lbl{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:9px 0 5px}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:999px;padding:6px 12px;font-size:13px;cursor:pointer}
.chip.on{background:var(--accent);color:var(--bg);border-color:var(--accent)}
.chip small{opacity:.65;margin-left:3px}
.count{color:var(--muted);font-size:13px;margin:14px 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--blue);border-radius:10px;padding:12px 13px;margin-bottom:10px}
.card.arb,.card.sum{border-left-color:var(--green)}
.card.book{border-left-color:var(--purple)}.card.cheap{border-left-color:var(--amber)}.card.new{border-left-color:var(--teal)}
.top{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;margin-bottom:5px}
.kind{font-weight:700;color:var(--blue)}.arb .kind,.sum .kind{color:var(--green)}
.book .kind{color:var(--purple)}.cheap .kind{color:var(--amber)}.new .kind{color:var(--teal)}
.cat{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:1px 7px;color:var(--muted)}
.age{margin-left:auto;color:var(--muted)}
.title{font-weight:650;font-size:15.5px}
.osub{color:var(--muted);font-size:14px}
.head{color:var(--muted);margin-top:1px}
.nums{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:10px 0}
.num{background:var(--bg);border-radius:8px;padding:7px 9px}
.num b{display:block;font-size:17px}.num span{color:var(--muted);font-size:11.5px}
.trade{border:1px solid var(--line);border-radius:8px;overflow:hidden;font-size:13.5px}
.leg{display:flex;gap:8px;align-items:baseline;padding:7px 9px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.leg:last-child{border:0}
.leg b{min-width:62px}.leg .px{margin-left:auto;font-variant-numeric:tabular-nums}
.leg .q{color:var(--muted);font-size:12px}
.why{margin-top:9px;color:var(--muted);font-size:13.5px}
.cmp{margin-top:8px;font-size:13.5px}
.meta{margin-top:8px;font-size:12.5px;display:flex;gap:10px;flex-wrap:wrap}.meta a{color:var(--blue)}
.tk{font-family:ui-monospace,Menlo,Consolas,monospace;color:var(--muted)}
.empty{padding:28px;text-align:center;color:var(--muted)}
</style></head><body><div class="wrap">
<h1>Kalshi board</h1>
<div class="sub" id="sub">loading...</div>
<details><summary>How to read this</summary>
<p><b>Riskless</b>: trades that pay at least what they cost in every outcome, after fees (a higher threshold priced above a lower one, or exclusive outcomes whose YES bids add up past 100).</p>
<p><b>Likely mispriced</b>: a range between two thresholds priced far below what the ranges on either side imply, assuming a single-peaked outcome. A bet that can lose a little, not a guarantee.</p>
<p><b>Sportsbook gap</b>: Kalshi is on the wrong side of the sportsbook consensus (vig removed) by a wide margin. Totals, spreads and player props only. Books refund a player who doesn't play while Kalshi may not, so check that risk. Compare the sportsbook line on the card with the Kalshi rules before trading.</p>
<p><b>Cheap offer</b>: the lowest offer is far below the next one in the book. It might be a mistake or might disappear before you act.</p>
<p><b>Edge</b> is cents per contract after estimated fees. <b>Size</b> is contracts available at those exact prices. <b>Est. profit</b> = edge x size. <b>Best</b> sorting weights est. profit by how certain each type is. Prices can move before you trade, and only the top of the book is checked.</p>
</details>
<details id="bk" style="display:none"></details>
<div class="chips" id="tabs"></div>
<input class="search" id="q" type="search" placeholder="Search (e.g. Rotten Tomatoes, CPI, Brunson)">
<div class="lbl">Topic</div><div class="chips" id="cats"></div>
<div id="typewrap"><div class="lbl">Type</div><div class="chips" id="types"></div></div>
<div id="sortwrap"><div class="lbl">Sort by</div><div class="chips" id="sorts"></div></div>
<div class="count" id="count"></div>
<div id="feed"></div>
</div>
<script>
const TN={arb:"Riskless",sum:"Riskless",shape:"Likely mispriced",book:"Sportsbook gap",cheap:"Cheap offer"};
const GRP={arb:"safe",sum:"safe",shape:"shape",book:"book",cheap:"cheap"};
const TYPES=[["safe","Riskless"],["shape","Likely mispriced"],["book","Sportsbook gap"],["cheap","Cheap offer"]];
const SORTS=[["score","Best"],["profit","Est. profit"],["edge","Edge"],["new","Newest"]];
let S={tab:"opps",cat:"ALL",type:"ALL",sort:"score",q:""},data=null;
try{const x=JSON.parse(localStorage.getItem("kalshi-board2")||"{}");S.cat=x.cat||"ALL";S.type=x.type||"ALL";S.sort=x.sort||"score";S.tab=x.tab||"opps"}catch(e){}
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const ago=t=>{const s=Math.max(0,Math.round(Date.now()/1000-t));
 return s<90?s+"s":s<5400?Math.round(s/60)+" min":s<172800?Math.round(s/3600)+" h":Math.round(s/86400)+" d"};
function save(){try{localStorage.setItem("kalshi-board2",JSON.stringify({cat:S.cat,type:S.type,sort:S.sort,tab:S.tab}))}catch(e){}}
function chip(label,n,on,fn){const b=document.createElement("button");b.className="chip"+(on?" on":"");
 b.innerHTML=esc(label)+(n==null?"":"<small>"+n+"</small>");b.onclick=fn;return b}
const okurl=u=>(u||"").indexOf("https://kalshi.com/")===0?u:"";
function card(i){
 const url=okurl(i.url);
 const legs=i.legs.slice(0,12).map(l=>'<div class="leg"><b>'+esc(l.action)+'</b><span>'+esc(l.label)+'</span><span class="px">'+l.price+
  '&cent;</span><span class="q">'+l.qty+' avail</span></div>').join("")+(i.legs.length>12?'<div class="leg"><span class="q">+'+(i.legs.length-12)+' more legs</span></div>':"");
 const second=i.kind==="shape"?'<div class="num"><b>$'+i.profit.toFixed(2)+'</b><span>est. profit</span></div>':
  '<div class="num"><b>'+i.size+'</b><span>contracts available</span></div>';
 const third=i.kind==="shape"?'<div class="num"><b>'+i.risk.toFixed(1)+'&cent;</b><span>risked per contract</span></div>':
  '<div class="num"><b>$'+i.profit.toFixed(2)+'</b><span>est. profit</span></div>';
 return '<div class="card '+esc(i.kind)+'"><div class="top"><span class="kind">'+esc(TN[i.kind]||i.kind)+'</span><span class="cat">'+esc(i.cat)+
  '</span><span class="age">seen '+ago(i.first_seen)+'</span></div><div class="title">'+esc(i.title)+'</div><div class="head">'+esc(i.headline)+'</div>'+
  '<div class="nums"><div class="num"><b>+'+i.edge.toFixed(1)+'&cent;</b><span>edge per contract</span></div>'+second+third+'</div>'+
  (i.kind==="shape"?'<div class="head" style="margin:-2px 0 8px">'+i.size+' contracts available at these prices</div>':"")+
  '<div class="trade">'+legs+'</div>'+(i.compare?'<div class="cmp"><b>Sportsbooks:</b> '+esc(i.compare)+'</div>':"")+
  '<div class="why">'+esc(i.why)+'</div>'+
  (url?'<div class="meta"><a href="'+esc(url)+'" target="_blank" rel="noopener">Open on Kalshi</a></div>':"")+'</div>'}
function wcard(w){
 const url=okurl(w.url);
 const px=(w.bid||w.ask)?("Bid "+(w.bid?w.bid+"\u00a2":"none")+" \u00b7 Ask "+(w.ask?w.ask+"\u00a2":"none")):"No prices yet";
 return '<div class="card new"><div class="top"><span class="kind">New market</span><span class="cat">'+esc(w.cat)+
  '</span><span class="age">opened '+ago(w.opened)+' ago</span></div><div class="title">'+esc(w.title)+'</div>'+
  (w.sub?'<div class="osub">'+esc(w.sub)+'</div>':"")+'<div class="head">'+px+'</div><div class="meta"><span class="tk">'+esc(w.ticker)+'</span>'+
  (url?'<a href="'+esc(url)+'" target="_blank" rel="noopener">Open on Kalshi</a>':"")+'</div></div>'}
function books(){
 const el=document.getElementById("bk"),b=data.scan.books;
 if(!b){el.style.display="none";return}
 el.style.display="";let h="<summary>Sportsbook matching</summary>";
 if(!b.enabled)h+="<p>Off. Add an ODDS_API_KEY secret to compare Kalshi with sportsbook lines.</p>";
 else{
  h+="<p>"+(b.odds_events||0)+" games and "+(b.odds_lines||0)+" lines loaded"+(b.age_min!=null?" ("+b.age_min+" min old)":"")+
   " \u00b7 credits used today: "+(b.credits_used_today||0)+(b.credits_remaining!=null?" \u00b7 remaining: "+b.credits_remaining:"")+"</p>";
  h+="<p>Kalshi sports markets: "+(b.sports_markets||0)+" \u00b7 readable line: "+(b.with_line||0)+" \u00b7 matched to a sportsbook line: "+(b.matched||0)+"</p>";
  if(b.error)h+='<p class="bad">'+esc(b.error)+"</p>";
  if(b.unmatched&&b.unmatched.length)h+="<p>Unmatched examples:<br>"+b.unmatched.map(esc).join("<br>")+"</p>";
  if(b.odds_sample&&b.odds_sample.length)h+="<p>Sportsbook games loaded, e.g.:<br>"+b.odds_sample.map(esc).join("<br>")+"</p>";
 }
 el.innerHTML=h}
function render(){
 if(!data)return;
 const sc=data.scan,age=Date.now()/1000-data.updated,watch=data.watch||[];
 let sub="Updated "+ago(data.updated)+" ago \u00b7 "+(sc.ladders||0)+" price ladders and "+(sc.markets||0).toLocaleString()+" markets checked";
 if(age>1200)sub+=' <span class="warn">(stale: scans may be paused)</span>';
 if(sc.error)sub+='<br><span class="bad">Last scan failed: '+esc(sc.error)+'</span>';
 else if(!sc.ladders)sub+='<br><span class="warn">No ladders were detected. The strike data may have a different format than expected.</span>';
 document.getElementById("sub").innerHTML=sub;
 books();
 const tb=document.getElementById("tabs");tb.innerHTML="";
 tb.appendChild(chip("Opportunities",data.items.length,S.tab==="opps",()=>{S.tab="opps";save();render()}));
 tb.appendChild(chip("New markets",watch.length,S.tab==="new",()=>{S.tab="new";save();render()}));
 const opps=S.tab==="opps";
 document.getElementById("typewrap").style.display=opps?"":"none";
 document.getElementById("sortwrap").style.display=opps?"":"none";
 const q=S.q.trim().toLowerCase();
 const pool=opps?data.items:watch;
 const text=i=>opps?(i.title+" "+i.headline+" "+i.cat+" "+i.legs.map(l=>l.label+" "+l.ticker).join(" ")):(i.title+" "+i.sub+" "+i.cat+" "+i.ticker);
 const base=pool.filter(i=>!q||text(i).toLowerCase().includes(q));
 const byType=opps?base.filter(i=>S.type==="ALL"||GRP[i.kind]===S.type):base;
 const cats={};byType.forEach(i=>cats[i.cat]=(cats[i.cat]||0)+1);
 const ce=document.getElementById("cats");ce.innerHTML="";
 ce.appendChild(chip("All",byType.length,S.cat==="ALL",()=>{S.cat="ALL";save();render()}));
 Object.keys(cats).sort((x,y)=>cats[y]-cats[x]).forEach(c=>ce.appendChild(chip(c,cats[c],S.cat===c,()=>{S.cat=c;save();render()})));
 const byCat=base.filter(i=>S.cat==="ALL"||i.cat===S.cat);
 if(opps){
  const te=document.getElementById("types");te.innerHTML="";
  te.appendChild(chip("All",byCat.length,S.type==="ALL",()=>{S.type="ALL";save();render()}));
  TYPES.forEach(([k,n])=>te.appendChild(chip(n,byCat.filter(i=>GRP[i.kind]===k).length,S.type===k,()=>{S.type=k;save();render()})));
  const se=document.getElementById("sorts");se.innerHTML="";
  SORTS.forEach(([k,n])=>se.appendChild(chip(n,null,S.sort===k,()=>{S.sort=k;save();render()})));
 }
 const list=byCat.filter(i=>!opps||S.type==="ALL"||GRP[i.kind]===S.type);
 if(opps)list.sort((x,y)=>S.sort==="edge"?y.edge-x.edge:S.sort==="profit"?y.profit-x.profit:S.sort==="new"?y.first_seen-x.first_seen:y.score-x.score);
 document.getElementById("count").textContent=opps?list.length+" opportunit"+(list.length===1?"y":"ies"):list.length+" newly opened, unpriced or wide-spread markets (newest first)";
 document.getElementById("feed").innerHTML=list.length?list.map(opps?card:wcard).join(""):
  '<div class="empty">Nothing matches right now. Real inconsistencies are rare and short-lived.</div>';
}
document.getElementById("q").addEventListener("input",e=>{S.q=e.target.value;render()});
async function load(){try{const r=await fetch("board.json?"+Date.now());data=await r.json();render()}
 catch(e){document.getElementById("sub").textContent="Could not load board.json"}}
load();setInterval(load,60000);
</script></body></html>"""

if __name__ == "__main__":
    main()
