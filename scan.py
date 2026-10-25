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
             "book": "Sportsbook gap", "cheap": "Cheap offer", "tier": "Riskless", "flat": "Same price, more coverage"}
CONF = {"arb": 1.0, "sum": 1.0, "tier": 1.0, "shape": 0.5, "book": 0.6, "cheap": 0.3, "flat": 0.4}


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


def slug(x):
    return re.sub(r"[^a-z0-9]+", "-", str(x).lower()).strip("-")[:60] or "market"


def add_links(it):
    """Per-leg links. A market ticker is 'EVENT-SUFFIX', and the series is the first part of the event ticker.
    The series page alone can open a different event (fantasy markets share one series), so link the event too."""
    for l in it["legs"]:
        ev = l["ticker"].rsplit("-", 1)[0]
        series = ev.split("-")[0].lower()
        l["event"] = ev
        l["eurl"] = f"https://kalshi.com/markets/{series}/{slug(it['title'])}/{ev.lower()}"
        l["surl"] = f"https://kalshi.com/markets/{series}"


def buy_block(pay, worth, edge, size, qty_notes, unit="pair"):
    """Plain 'how much to buy' lines for trades that need the same number of every leg."""
    sizes = sorted({min(10, size), size})
    lines = [f"{n} {unit}{'s' if n != 1 else ''}: pay about ${pay * n / 100:.2f}, get back at least ${worth * n / 100:.2f}, "
             f"gain about ${edge * n / 100:.2f} after fees" for n in sizes]
    return {"intro": f"Buy the same number of every leg. The most the order books allow at these prices is {size} "
                     f"({qty_notes}). If you buy different amounts, the extra is an unhedged bet.", "lines": lines}


def short_rules(m):
    t = " ".join(str(m.get("rules_primary") or "").split())
    if re.match(r"(?i)^if\s+", t) and re.search(r"(?i),?\s*then the market resolves to yes\.?$", t):
        t = re.sub(r"(?i)^if\s+", "", t)                              # "If X, then the market resolves to Yes." -> "X"
        t = re.sub(r"(?i),?\s*then the market resolves to yes\.?$", "", t)
    return t if len(t) <= 220 else t[:217].rsplit(" ", 1)[0] + "..."


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
def subject_key(m):
    """Wording of the market with every number blanked, so 'Gibbs: 25+' and 'Strong Jr.: 20+' differ
    but 'Above 45' and 'Above 50' match."""
    txt = (m.get("yes_sub_title") or m.get("title") or "").lower()
    return re.sub(r"-?\d[\d,]*\.?\d*", "#", txt).strip()


def build_ladders(markets):
    groups = {}
    for m in markets:
        s = strike_of(m)
        if s:
            groups.setdefault((m["_event"], s[0], subject_key(m)), []).append((s[1], m))
    ladders = []
    for (ev, d, _subj), items in groups.items():
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
                         "label": m["_sub"] or fk(k), "bq": 0, "aq": 0, "vol": num(m, "volume_24h"), "rules": short_rules(m)})
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
            if False and i > 0 and j < nb - 1:  # single-peak "shape" check removed: it needs a buy-one-sell-another bet
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


def win_text(leg):
    """What buying this leg wins on, in the market's own words. 'Below X' markets are bought on the NO side."""
    lab = leg["label"]
    if not leg["flip"]:
        return lab
    r = re.match(r"(?i)^\s*(below|under|less than|fewer than)\s+(.*)$", lab)
    return f"At or above {r.group(2)}" if r else f"Not \u201c{lab}\u201d"


def flat_candidates(L, a):
    """A lower threshold that costs the same as (or less than) the next higher one, although it pays in more cases."""
    legs = [l for l in L["legs"] if l["bid"] is not None and l["ask"] is not None
            and l["ask"] - l["bid"] <= a.max_spread]
    if len(legs) < 2:
        return []
    mids = [(l["bid"] + l["ask"]) / 2 for l in legs]
    dens = [max(0, mids[i] - mids[i + 1]) / (legs[i + 1]["k"] - legs[i]["k"]) for i in range(len(legs) - 1)]
    out = []
    for i in range(len(legs) - 1):
        lo, hi = legs[i], legs[i + 1]
        if lo["ask"] > hi["ask"]:
            continue
        q = 0.0   # extra coverage: what the two neighbouring ranges suggest (mid prices), capped by their mass
        if 0 < i < len(dens) - 1:
            q = min(dens[i - 1], dens[i + 1]) * (hi["k"] - lo["k"])
            q = min(q, dens[i - 1] * (lo["k"] - legs[i - 1]["k"]), dens[i + 1] * (legs[i + 2]["k"] - hi["k"]), a.flat_max_q)
        far = max([mids[j] for j in range(i + 2, len(legs))] or [0])
        fair = max(mids[i + 1] + q, far)
        edge = fair - lo["ask"] - fee_c(lo["ask"])
        if edge >= a.flat_min_edge:
            out.append({"kind": "flat", "L": L, "lo": dict(lo), "hi": dict(hi), "q": q, "est": edge,
                        "pre": edge * min(max(lo.get("vol", 0), 10), 2000) / 100})
    return out


def flat_reason(c, a):
    """Why a same-price candidate did or did not become an alert (used by the debug panel)."""
    lo, hi, q = c["lo"], c["hi"], c["q"]
    if lo["ask"] is None or hi["bid"] is None or hi["ask"] is None:
        return "no live quote on one side"
    if lo["ask"] > hi["ask"]:
        return f"no longer the same price: the lower tier now costs {lo['ask']}\u00a2 vs {hi['ask']}\u00a2"
    edge = (hi["bid"] + hi["ask"]) / 2 + q - lo["ask"] - fee_c(lo["ask"])
    if edge < a.flat_min_edge:
        return f"value gap {edge:.2f}\u00a2 is below the {a.flat_min_edge}\u00a2 minimum"
    if lo["aq"] < a.min_size:
        return f"only {lo['aq']} contracts available (minimum {a.min_size})"
    if edge * lo["aq"] / 100 < a.flat_min_profit:
        return f"estimated profit ${edge * lo['aq'] / 100:.2f} is below the ${a.flat_min_profit} minimum"
    if edge > a.max_edge_spec:
        return f"dropped as implausible (edge {edge:.0f}\u00a2)"
    return "shown"


def flat_item(c, a):
    L, lo, hi, q = c["L"], c["lo"], c["hi"], c["q"]
    if lo["ask"] is None or hi["bid"] is None or hi["ask"] is None:
        return None
    mid_hi = (hi["bid"] + hi["ask"]) / 2
    fair = mid_hi + q
    edge = fair - lo["ask"] - fee_c(lo["ask"])
    size = lo["aq"]
    if lo["ask"] > hi["ask"] or edge < a.flat_min_edge or size < a.min_size or edge * size / 100 < a.flat_min_profit:
        return None
    lo_t, hi_t = win_text(lo), win_text(hi)
    action = "Buy NO" if lo["flip"] else "Buy YES"
    if lo["ask"] == hi["ask"]:
        headline = f"\u201c{lo_t}\u201d and \u201c{hi_t}\u201d both cost {lo['ask']}¢, but the first wins in more cases"
    else:
        headline = (f"\u201c{lo_t}\u201d costs {lo['ask']}¢, less than \u201c{hi_t}\u201d ({hi['ask']}¢), "
                    f"but it wins in more cases")
    why = (f"\u201c{lo_t}\u201d wins whenever \u201c{hi_t}\u201d does, and also when the result lands in the gap between "
           f"the two. So it should cost more, not the same or less. For comparison, \u201c{hi_t}\u201d is bid {hi['bid']}¢ and "
           f"offered at {hi['ask']}¢ (about {mid_hi:.0f}¢), so \u201c{lo_t}\u201d should be worth at least about {fair:.0f}¢. "
           f"You can buy it for {lo['ask']}¢. This is one plain purchase; nothing needs to be sold.")
    return {"pay": lo["ask"], "worth": round(fair, 1), "sure": False,
            "id": f"{lo['ticker']}|flat|{hi['ticker']}", "kind": "flat", "cat": L["cat"], "title": L["title"],
            "headline": headline, "why": why,
            "means": ((f"Buying NO on \u201c{lo['label']}\u201d wins if this does NOT happen: {lo['rules']}" if lo["flip"]
                       else f"Buying YES on \u201c{lo['label']}\u201d wins if: {lo['rules']}") if lo.get("rules")
                      else f"This purchase wins if the result is: {lo_t}"),
            "refs": [{"label": hi_t, "bid": hi["bid"], "ask": hi["ask"], "note": "wins in fewer cases"}],
            "legs": [{"action": action, "ticker": lo["ticker"], "label": lo["label"], "price": lo["ask"], "qty": size}],
            "edge": round(edge, 2), "size": size, "profit": round(edge * size / 100, 2), "risk": 0, "fair": round(fair, 1),
            "url": f"https://kalshi.com/markets/{L['series'].lower()}" if L["series"] else ""}


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
        lo_t, hi_t = win_text(lo), win_text(hi)
        headline = f"\u201c{hi_t}\u201d is bid above what \u201c{lo_t}\u201d costs, which can't be right"
        why = (f"\u201c{lo_t}\u201d wins whenever \u201c{hi_t}\u201d does, so it can never be worth less. Yet \u201c{hi_t}\u201d "
               f"is bid at {hi['bid']}¢ while \u201c{lo_t}\u201d can be bought for {lo['ask']}¢. Buying both legs below "
               f"costs {both:.0f}¢ in total and pays at least 100¢ in every outcome, so it locks in about "
               f"{edge:.1f}¢ per contract after fees.")
    else:
        lo_mid, hi_mid = (lo["bid"] + lo["ask"]) / 2, (hi["bid"] + hi["ask"]) / 2
        headline = f"Range {k1} to {k2} looks underpriced"
        why = (f"\u201c{lo['label']}\u201d trades near {lo_mid:.0f}¢ and \u201c{hi['label']}\u201d near {hi_mid:.0f}¢, "
               f"so the market gives only about {max(0, lo_mid - hi_mid):.0f}% to a result between {k1} and {k2}. "
               f"The ranges on either side imply at least {lb:.0f}% if the outcome has a single peak. "
               f"Both legs cost {both:.0f}¢ and always pay at least 100¢, plus another 100¢ if the result lands "
               f"in that range. Net risk is about {cost + fees:.1f}¢ per contract.")
    lo_t2, hi_t2 = win_text(lo), win_text(hi)
    cases = [f"Neither \u201c{lo_t2}\u201d nor \u201c{hi_t2}\u201d happens: the first leg pays 0¢ and the second pays 100¢, so you get 100¢.",
             f"\u201c{lo_t2}\u201d happens but \u201c{hi_t2}\u201d does not: both legs pay, so you get 200¢.",
             f"Both happen: the first leg pays 100¢ and the second pays 0¢, so you get 100¢."]
    return {"pay": round(100 + cost), "worth": 100, "sure": kind == "arb", "cases": cases, "unit": "pair",
            "buy": buy_block(round(100 + cost), 100, edge, size, f"{lo['aq']} of the first leg and {hi['bq']} of the second"),
            "id": f"{L['event']}|{lo['ticker']}|{hi['ticker']}", "kind": kind, "cat": L["cat"],
            "title": L["title"], "headline": headline, "why": why, "legs": legs,
            "edge": round(edge, 2), "size": size, "profit": round(edge * size / 100, 2),
            "risk": round(cost + fees, 2), "fair": round(lb, 1),
            "url": f"https://kalshi.com/markets/{L['series'].lower()}" if L["series"] else ""}


def ladder_view(ladders, a):
    """All threshold ladders that have at least one flat step (adjacent tiers priced within a tick of each other)."""
    out = []
    for L in ladders:
        legs = [l for l in L["legs"] if l["bid"] is not None and l["ask"] is not None
                and l["ask"] - l["bid"] <= a.max_spread]
        if len(legs) < 3:
            continue
        steps, span = [], (legs[-1]["k"] - legs[0]["k"]) or 1
        for lo, hi in zip(legs, legs[1:]):
            m_lo, m_hi = (lo["bid"] + lo["ask"]) / 2, (hi["bid"] + hi["ask"]) / 2
            cost = lo["ask"] - hi["bid"]
            fees = fee_c(lo["ask"]) + fee_c(100 - hi["bid"])
            dead = (m_lo >= 98.5 and m_hi >= 98.5) or (m_lo <= 1.5 and m_hi <= 1.5)
            steps.append({"lo": fk(lo["k"]), "hi": fk(hi["k"]), "prob": round(m_lo - m_hi, 1),
                          "breakeven": round(cost + fees, 1), "w": hi["k"] - lo["k"],
                          "flat": (m_lo - m_hi) <= 1.0 and not dead})
        flat = [x for x in steps if x["flat"]]
        if not flat:
            continue
        share = sum(x["w"] for x in flat) / span
        out.append({"cat": L["cat"], "title": L["title"], "flat": len(flat), "share": round(share, 2),
                    "tiers": [{"label": l["label"], "bid": l["bid"], "ask": l["ask"]} for l in legs], "steps": steps,
                    "url": f"https://kalshi.com/markets/{L['series'].lower()}" if L["series"] else ""})
    out.sort(key=lambda x: (x["share"], x["flat"]), reverse=True)
    per, kept = {}, []
    for x in out:
        if per.get(x["cat"], 0) < a.ladders_per_cat:
            per[x["cat"]] = per.get(x["cat"], 0) + 1
            kept.append(x)
    return kept[:400]


# ---------- sets ("at most N can pay") and rank tiers ----------
CONFLICT = {"yards", "yds", "touchdowns", "receptions", "sacks", "interceptions", "rushing", "receiving",
            "passing", "goals", "assists", "rebounds", "steals", "blocks", "strikeouts", "homers", "saves"}


def event_n(e, ms):
    """Most outcomes of this event that can resolve YES: 1 if exclusive, N for 'top N' sets, else None."""
    if e.get("mutually_exclusive"):
        return 1
    text = norm(f"{e.get('title') or ''} {ms[0].get('title') or ''} {ms[0].get('rules_primary') or ''}")
    r = re.search(r"\btop (\d+)\b", text)
    return int(r.group(1)) if r else None


def field_list(evs):
    """Events that are a field of different subjects (players, teams, candidates) with a known maximum N."""
    out = []
    for e, ms in evs:
        if not (3 <= len(ms) <= 80) or any(strike_of(m) for m in ms):
            continue
        n = event_n(e, ms)
        if n is None:
            continue
        subj = {}
        for m in ms:
            k = norm_name(m["_sub"] or m["_title"])
            if k and k not in subj:
                subj[k] = m
        if len(subj) < 3:
            continue
        text = norm(f"{e.get('title') or ''} {ms[0].get('title') or ''} {ms[0].get('rules_primary') or ''}")
        out.append({"e": e, "N": n, "subj": subj, "cat": ms[0]["_cat"], "title": ms[0]["_title"],
                    "series": ms[0]["_series"], "conflict": frozenset(set(text.split()) & CONFLICT)})
    return out


def sum_candidates(fl, a):
    """At most N of the listed outcomes can pay, so YES bids must add up to <= 100*N."""
    out = []
    for f in fl:
        ms = [m for m in f["subj"].values() if quotes(m)[0]]
        if len(ms) <= f["N"]:
            continue
        q = [quotes(m)[0] for m in ms]
        edge = sum(q) - 100 * f["N"] - sum(fee_c(x) for x in q)
        if edge >= a.min_edge:
            out.append({"kind": "sum", "f": f, "ms": ms, "N": f["N"], "est": edge})
    return out


def sum_item(c, books, a):
    f, N, bids = c["f"], c["N"], []
    for m in c["ms"]:
        yes, _ = books[m["ticker"]]
        if yes:
            bids.append((m, yes[0][0], yes[0][1]))
    n = len(bids)
    if n <= N:
        return None
    sumb = sum(b for _, b, _ in bids)
    edge = sumb - 100 * N - sum(fee_c(b) for _, b, _ in bids)
    size = min(q for _, _, q in bids)
    if edge < a.min_edge or size < a.min_size:
        return None
    legs = [{"action": "Buy NO", "ticker": m["ticker"], "label": m["_sub"] or m["_title"],
             "price": 100 - b, "qty": q} for m, b, q in bids]
    word = "can win" if N == 1 else f"can finish in the top {N}" if "top" in norm(f["title"]) else f"can pay"
    pay_set = sum(100 - b for _, b, _ in bids)
    cases = [(f"{N} of the {n} outcomes happens (the most possible)" if N == 1 else f"{N} of the {n} outcomes happen (the most possible)")
             + f": the other {n - N} NOs pay 100¢ each, so you get {100 * (n - N)}¢.",
             ("None happen" if N == 1 else f"Fewer than {N} happen") + f": more of the NOs pay, up to {100 * n}¢ if none happen."]
    return {"pay": pay_set, "worth": 100 * (n - N), "sure": True, "cases": cases, "unit": "bundle (one NO on every outcome)",
            "buy": buy_block(pay_set, 100 * (n - N), edge, size, f"{n} NO legs, one on each outcome", "bundle"),
            "id": f"{f['e'].get('event_ticker')}|sum", "kind": "sum", "N": N, "cat": f["cat"], "title": f["title"],
            "headline": f"At most {N} of {n} outcomes can pay, but YES bids add up to {sumb}¢ (limit {100 * N}¢)",
            "why": (f"Only {N} of these {n} outcomes {word}, so their YES prices should never add up to more than "
                    f"{100 * N}¢. They add up to {sumb}¢. Buying NO on all of them costs {100 * n - sumb}¢ per bundle (one NO on every outcome) and "
                    f"pays at least {100 * (n - N)}¢, locking in about {edge:.1f}¢ per bundle after fees."),
            "legs": legs, "edge": round(edge, 2), "size": size, "profit": round(edge * size / 100, 2),
            "risk": 0, "fair": 0,
            "url": f"https://kalshi.com/markets/{f['series'].lower()}" if f["series"] else ""}


def tier_candidates(fl, a):
    """Same subject in two sets with different N (e.g. top 1 and top 10): P(top small) <= P(top big)."""
    out, pool = [], [f for f in fl if len(f["subj"]) >= 5]
    for A in pool:
        for B in pool:
            if A["N"] >= B["N"] or A["cat"] != B["cat"] or A["conflict"] != B["conflict"]:
                continue
            shared = A["subj"].keys() & B["subj"].keys()
            if len(shared) < 5 or len(shared) < 0.5 * min(len(A["subj"]), len(B["subj"])):
                continue
            for s in shared:
                ma, mb = A["subj"][s], B["subj"][s]
                bid_small, ask_big = quotes(ma)[0], quotes(mb)[1]
                if bid_small is None or ask_big is None:
                    continue
                edge = -(ask_big - bid_small) - fee_c(ask_big) - fee_c(100 - bid_small)
                if edge >= a.min_edge:
                    leg = lambda m: {"ticker": m["ticker"], "label": m["_sub"] or m["_title"], "bid": None,
                                     "ask": None, "flip": False, "bq": 0, "aq": 0}
                    out.append({"kind": "tier", "est": edge, "A": A, "B": B, "name": ma["_sub"] or s,
                                "small": leg(ma), "big": leg(mb)})
    return out


def tier_item(c, a):
    sm, bg, A, B = c["small"], c["big"], c["A"], c["B"]
    if sm["bid"] is None or bg["ask"] is None:
        return None
    cost = bg["ask"] - sm["bid"]
    edge = -cost - fee_c(bg["ask"]) - fee_c(100 - sm["bid"])
    size = min(bg["aq"], sm["bq"])
    if edge < a.min_edge or size < a.min_size:
        return None
    pay = bg["ask"] + 100 - sm["bid"]
    nm = c["name"]
    a_, b_ = A["N"], B["N"]
    c1 = f"{nm} finishes #1" if a_ == 1 else f"{nm} finishes in the top {a_}"
    c2 = (f"{nm} finishes #{a_ + 1}" if b_ == a_ + 1 else f"{nm} finishes #{a_ + 1} to #{b_}")
    c3 = f"{nm} finishes outside the top {b_}"
    cases = [f"{c1}: the top-{b_} YES pays 100¢ and the top-{a_} NO pays 0¢, so you get 100¢.",
             f"{c2}: both legs pay, so you get 200¢.",
             f"{c3}: the top-{b_} YES pays 0¢ and the top-{a_} NO pays 100¢, so you get 100¢."]
    return {"pay": pay, "worth": 100, "sure": True, "cases": cases, "unit": "pair",
            "buy": buy_block(pay, 100, edge, size, f"{bg['aq']} of the YES at {bg['ask']}¢ and {sm['bq']} of the NO at {100 - sm['bid']}¢"),
            "id": f"{sm['ticker']}|{bg['ticker']}|tier", "kind": "tier", "cat": A["cat"],
            "title": f"{c['name']}: top {A['N']} vs top {B['N']}",
            "headline": f"Priced higher to finish top {A['N']} than top {B['N']}",
            "why": (f"Finishing in the top {A['N']} means finishing in the top {B['N']}, so the top-{A['N']} price can "
                    f"never be above the top-{B['N']} price. The two legs together cost {100 + cost:.0f}¢ and pay at "
                    f"least 100¢ in every outcome, locking in about {edge:.1f}¢ per contract after fees. "
                    f"Check that both markets use the same ranking source and season."),
            "legs": [{"action": "Buy YES", "ticker": bg["ticker"], "label": f"{bg['label']} (top {B['N']})",
                      "price": bg["ask"], "qty": bg["aq"]},
                     {"action": "Buy NO", "ticker": sm["ticker"], "label": f"{sm['label']} (top {A['N']})",
                      "price": 100 - sm["bid"], "qty": sm["bq"]}],
            "edge": round(edge, 2), "size": size, "profit": round(edge * size / 100, 2), "risk": 0, "fair": 0,
            "url": f"https://kalshi.com/markets/{A['series'].lower()}" if A["series"] else ""}


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
    return {"pay": pay, "worth": round(p100 if side == "buy" else 100 - p100, 1), "sure": False,
            "id": f"{m['ticker']}|book|{side}", "kind": "book", "cat": m["_cat"], "title": m["_title"],
            "headline": f"Kalshi is {edge:.0f}¢ {'below' if side == 'buy' else 'above'} sportsbook prices",
            "why": why, "compare": f"{desc}: {p100:.0f}% across {l['n']} books",
            "legs": [{"action": action, "ticker": leg["ticker"], "label": leg["label"], "price": pay, "qty": size}],
            "edge": round(edge, 2), "size": size, "profit": round(edge * size / 100, 2), "risk": 0, "fair": 0,
            "url": f"https://kalshi.com/markets/{m['_series'].lower()}" if m["_series"] else ""}


# ---------- cheap offers ----------
def cheap_items(m, book, a):
    """Lowest offer far below the next one AND far below where the market last traded."""
    yes, no = book
    last = cents(m, "last_price")
    if not last:
        return []
    out = []
    rules = short_rules(m)
    name = m["_sub"] or m["_title"]
    sides = (("Buy YES", [(100 - p, q) for p, q in no], "YES", last),            # YES asks, lowest first
             ("Buy NO", [(100 - p, q) for p, q in yes], "NO", 100 - last))       # NO asks, lowest first
    for action, asks, nm, ref in sides:
        if len(asks) < 2:
            continue
        (a1, q1), (a2, _) = asks[0], asks[1]
        if a2 - a1 < a.ask_gap or q1 > a.cheap_max_size or a1 < a.cheap_min_price or ref - a1 < a.cheap_last_gap:
            continue
        edge = min(a2, ref) - a1 - fee_c(a1)
        if edge <= 0 or q1 < a.min_size:
            continue
        wins = (f"Buying YES on \u201c{name}\u201d wins if: {rules}" if nm == "YES" and rules else
                f"Buying NO on \u201c{name}\u201d wins if this does NOT happen: {rules}" if rules else
                f"Buying {nm} on \u201c{name}\u201d in the market \u201c{m['_title']}\u201d.")
        out.append({"pay": a1, "worth": min(a2, ref), "sure": False,
                    "id": f"{m['ticker']}|cheap|{nm}{a1}", "kind": "cheap", "cat": m["_cat"], "title": m["_title"],
                    "headline": f"{nm} on \u201c{name}\u201d can be bought for {a1}¢, but it last traded at {ref}¢",
                    "means": wins, "note": f"Last traded at {ref}¢. The next-cheapest seller wants {a2}¢.",
                    "why": (f"The cheapest {nm} offer ({a1}¢, {q1} contracts) is {ref - a1}¢ below the last trade ({ref}¢) "
                            f"and {a2 - a1}¢ below the next offer ({a2}¢). If the price is really about {min(a2, ref)}¢, "
                            f"this is cheap by roughly {edge:.0f}¢ after fees. It may also be a mistake that gets pulled, "
                            f"or the last trade may be old."),
                    "legs": [{"action": action, "ticker": m["ticker"], "label": name, "price": a1, "qty": q1}],
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


# ---------- spikes: sudden moves to fade (anti-momentum) ----------
def cc(d, key):
    if not isinstance(d, dict):
        return None
    v = d.get(key)
    if v is not None:
        return float(v)
    v = d.get(key + "_dollars")
    return float(v) * 100 if v is not None else None


def candle_mid(c):
    b, x = cc(c.get("yes_bid"), "close"), cc(c.get("yes_ask"), "close")
    if b is not None and x is not None and b > 0 and x < 100:
        return (b + x) / 2
    return cc(c.get("price"), "close")


def hourly(ticker, series, now):
    d = get(f"/series/{series}/markets/{ticker}/candlesticks",
            {"start_ts": int(now - 48 * 3600), "end_ts": int(now), "period_interval": 60})
    out = []
    for c in d.get("candlesticks", []):
        mid = candle_mid(c)
        vol = c.get("volume", c.get("volume_fp", 0)) or 0
        if mid is not None:
            out.append((c.get("end_period_ts", 0), mid, float(vol)))
    out.sort()
    return out


def detect_spikes(markets, state, now, a, ctx, fetch_book, books):
    base_old = state.get("base", {})
    cur, dev, base_new, by_t, by_event = {}, {}, {}, {}, {}
    for m in markets:
        yb, ya = quotes(m)
        mid = (yb + ya) / 2 if (yb and ya) else (cents(m, "last_price") or None)
        if not mid:
            continue
        t = m["ticker"]
        b0 = base_old.get(t, mid)
        cur[t], dev[t], by_t[t] = mid, mid - b0, m
        base_new[t] = round(b0 + a.spike_alpha * (mid - b0), 1)
        by_event.setdefault(m["_event"], []).append(t)
    ctx["base"] = base_new

    # keep the track record up to date: how far did each logged spike retrace after 1h / 6h / 24h?
    log = [e for e in state.get("spike_log", []) if now - e["t"] < 14 * 86400]
    for e in log:
        for h in (1, 6, 24):
            k = f"r{h}"
            if k not in e and now - e["t"] >= h * 3600 and e["ticker"] in cur and e["now"] != e["base"]:
                e[k] = round((e["now"] - cur[e["ticker"]]) / (e["now"] - e["base"]), 2)
    logged = {e["id"] for e in log}

    cands = sorted(((abs(d), t) for t, d in dev.items() if abs(d) >= a.spike_min * 0.6 and by_t[t]["_series"]),
                   reverse=True)[:a.spike_verify]
    spikes = []
    for _, t in cands:
        m = by_t[t]
        try:
            h = hourly(t, m["_series"], now)
        except Exception as ex:
            ctx["spike_err"] = f"{type(ex).__name__}: {ex}"[:150]
            continue
        base_pts = [mid for ts_, mid, _ in h if ts_ < now - 6 * 3600]
        if len(h) < 8 or len(base_pts) < 4:
            continue
        baseline, now_mid = median(base_pts), cur[t]
        move = now_mid - baseline
        if abs(move) < a.spike_min:
            continue
        hrs = 0
        for _, mid, _ in reversed(h):
            if abs(mid - baseline) <= 5:
                break
            hrs += 1
        if hrs > a.spike_max_hours:
            continue
        vol_move = sum(v for _, _, v in h[-(hrs + 1):])
        thin = vol_move <= a.spike_thin
        sibs = [x for x in by_event.get(m["_event"], []) if x != t and abs(dev.get(x, 0)) >= 6]
        iso = not sibs
        sid = f"{t}|{int(now // 43200)}"
        if sid not in logged and not any(e["ticker"] == t and now - e["t"] < 12 * 3600 for e in log):
            log.append({"id": sid, "t": now, "ticker": t, "cat": m["_cat"], "dir": "up" if move > 0 else "down",
                        "base": round(baseline, 1), "now": round(now_mid, 1), "thin": thin, "iso": iso})
            logged.add(sid)
        if not fetch_book(t):
            continue
        yes, no = books[t]
        yb = yes[0][0] if yes else None
        ya = 100 - no[0][0] if no else None
        if yb is None or ya is None or ya - yb > a.spike_max_spread:
            continue
        s, target = ya - yb, (now_mid + baseline) / 2
        if move > 0:    # price jumped: fade it by buying NO
            action, price, qty = "Buy NO", 100 - yb, yes[0][1]
            gain = yb - (target + s / 2) - fee_c(price) - fee_c(100 - target - s / 2)
        else:           # price crashed: fade it by buying YES
            action, price, qty = "Buy YES", ya, no[0][1]
            gain = (target - s / 2) - ya - fee_c(price) - fee_c(target - s / 2)
        if gain < a.spike_min_gain or qty < a.min_size:
            continue
        rank = abs(move) * (1.0 if thin else 0.5) * (1.0 if iso else 0.6)
        spikes.append({"id": sid, "ticker": t, "cat": m["_cat"], "title": m["_title"], "sub": m["_sub"],
                       "dir": "up" if move > 0 else "down", "base": round(baseline), "now": round(now_mid),
                       "move": round(move), "hours": hrs, "vol_move": round(vol_move), "thin": thin, "iso": iso,
                       "related": len(sibs), "target": round(target), "gain": round(gain, 1), "risk": price,
                       "breakeven": round(100 * price / (price + gain)), "rank": round(rank, 1),
                       "legs": [{"action": action, "ticker": t, "label": m["_sub"] or m["_title"], "price": price, "qty": qty}],
                       "url": f"https://kalshi.com/markets/{m['_series'].lower()}" if m["_series"] else ""})
    spikes.sort(key=lambda x: x["rank"], reverse=True)
    ctx["spike_log"] = log[-500:]
    ctx["spike_stats"] = spike_stats(log)
    return spikes


def spike_stats(log):
    def agg(rows, h):
        v = sorted(e[f"r{h}"] for e in rows if f"r{h}" in e)
        if not v:
            return None
        return {"n": len(v), "median": round(median(v) * 100),
                "half": round(100 * sum(1 for x in v if x >= 0.5) / len(v)),
                "worse": round(100 * sum(1 for x in v if x < 0) / len(v))}
    groups = {"all": log, "thin_iso": [e for e in log if e["thin"] and e["iso"]],
              "other": [e for e in log if not (e["thin"] and e["iso"])]}
    return {g: {"count": len(rows), **{f"h{h}": agg(rows, h) for h in (1, 6, 24)}} for g, rows in groups.items()}


CRYPTO_TICK = re.compile(r"^KX(?:BTC|ETH|XRP|DOGE|SHIB|CRYPTO)|^KXSOL(?:D|W|M|USD|15M)?$")
CRYPTO_TEXT = re.compile(r"\b(?:bitcoin|btc|ethereum|solana|dogecoin|ripple|xrp|crypto|cryptocurrency|stablecoin|altcoin|nft|coinbase)\b")


def is_crypto(e, cat):
    if "crypto" in str(cat).lower() or CRYPTO_TICK.match(str(e.get("series_ticker") or "").upper()):
        return True
    ms = e.get("markets") or [{}]
    return bool(CRYPTO_TEXT.search(norm(f"{e.get('title') or ''} {ms[0].get('title') or ''}")))


FILM_TEXT = re.compile(r"\b(?:rotten tomatoes|box office|opening weekend|cinemascore|imdb|movie|movies|film|films|oscar|oscars|"
                       r"academy awards?|emmy|emmys|golden globes?|bafta|sag awards?|critics choice|tv|television|netflix|hbo|"
                       r"disney|hulu|streaming|episode|episodes|season finale|premiere|trailer|sequel|marvel|star wars)\b")
ENT_CATS = {"entertainment", "culture", "pop culture", "media", "arts"}


def refine_cat(e, cat):
    """Split movies and TV out of Entertainment so they get their own topic chip."""
    ms = e.get("markets") or [{}]
    text = norm(f"{e.get('title') or ''} {ms[0].get('title') or ''} {e.get('series_ticker') or ''}")
    if (str(cat).lower() in ENT_CATS or "rotten tomatoes" in text) and FILM_TEXT.search(text):
        return "Film/TV"
    return cat


LEAGUE_TICK = re.compile(r"(?:^|[^A-Z])(?:KX)?(?:NBA|NFL)")
LEAGUE_TEXT = re.compile(r"\b(?:nfl|nba|pro football|pro basketball|fantasy football|super bowl|nba finals)\b")
OTHER_TEXT = re.compile(r"\b(?:wnba|ncaa|ncaaf|ncaab|college)\b")


def league_ok(e):
    """True for NFL and NBA events (including fantasy football). Everything else in Sports is ignored."""
    ms = e.get("markets") or [{}]
    tick = f"{e.get('series_ticker') or ''} {e.get('event_ticker') or ''}".upper()
    text = norm(f"{e.get('title') or ''} {ms[0].get('title') or ''} {ms[0].get('rules_primary') or ''}")
    if OTHER_TEXT.search(text) or "WNBA" in tick or "NCAA" in tick:
        return False
    return bool(LEAGUE_TICK.search(tick) or LEAGUE_TEXT.search(text))


def debug_report(term, evs, ladders, flats, sel, flat_log, a):
    """Explain what the scanner saw for markets matching a search term, and why each same-price candidate did or didn't show."""
    t = norm(term)
    out = {"term": term, "events": []}
    sel_ids = {(c["lo"]["ticker"], c["hi"]["ticker"]) for c in sel}
    for e, ms in evs:
        blob = norm(f"{e.get('title') or ''} " + " ".join(f"{m['_title']} {m['_sub']}" for m in ms[:60]))
        if t not in blob:
            continue
        tick = {m["ticker"] for m in ms}
        rec = {"event": e.get("event_ticker"), "title": e.get("title"), "category": ms[0]["_cat"] if ms else None,
               "n": len(ms), "markets": [], "ladders": []}
        for m in ms[:40]:
            yb, ya = quotes(m)
            rec["markets"].append({"sub": m["_sub"] or m["_title"], "strike_type": m.get("strike_type") or "none",
                                   "floor": m.get("floor_strike"), "cap": m.get("cap_strike"), "bid": yb, "ask": ya,
                                   "recognized": bool(strike_of(m))})
        for L in ladders:
            if not any(l["ticker"] in tick for l in L["legs"]):
                continue
            usable = [l for l in L["legs"] if l["bid"] is not None and l["ask"] is not None
                      and l["ask"] - l["bid"] <= a.max_spread]
            cands = []
            for c in (c for c in flats if c["L"] is L):
                key = (c["lo"]["ticker"], c["hi"]["ticker"])
                res = flat_log.get(key) or ("checked" if key in sel_ids else
                                            "not checked this run: ranked below the cutoff for its topic")
                cands.append({"pair": f"{c['lo']['label']} / {c['hi']['label']}", "value_gap": round(c["est"], 2), "result": res})
            rec["ladders"].append({"tiers": len(L["legs"]), "usable": len(usable), "candidates": cands})
        out["events"].append(rec)
    return out


def scan(a, state, now, ctx):
    events = fetch_events()
    cats = category_map(events)
    markets, evs, excluded, crypto_excluded = [], [], 0, 0
    for e in events:
        cat = e.get("category") or cats.get(e.get("series_ticker")) or "Other"
        cat = refine_cat(e, cat)
        if not a.crypto and is_crypto(e, cat):
            crypto_excluded += len(e.get("markets", []))
            continue
        if a.sports != "all" and "sport" in cat.lower() and not league_ok(e):
            excluded += len(e.get("markets", []))
            continue
        ms = []
        for m in e.get("markets", []):
            m["_cat"], m["_event"] = cat, e.get("event_ticker") or ""
            m["_series"] = e.get("series_ticker") or ""
            m["_title"], m["_sub"] = describe(e, m)
            ms.append(m)
        markets += ms
        evs.append((e, ms))
    print("strike types:", dict(Counter((m.get("strike_type") or "none") for m in markets).most_common(8)))

    ladders = build_ladders(markets)
    cands = []
    for L in ladders:
        cands += analyze(L, a)
    ctx["ladders"] = ladder_view(ladders, a)
    fl = field_list(evs)
    cands += sum_candidates(fl, a)
    cands += tier_candidates(fl, a)

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

    flats = []
    for L in ladders:
        flats += flat_candidates(L, a)
    flats.sort(key=lambda c: c["pre"], reverse=True)
    per, sel = {}, []
    for c in flats:   # a quota per topic, so one busy topic can't crowd out the others
        k = c["L"]["cat"]
        if per.get(k, 0) < a.flat_per_cat and len(sel) < a.flat_books:
            per[k] = per.get(k, 0) + 1
            sel.append(c)
    cands.sort(key=lambda c: c["est"], reverse=True)
    cands = cands[:a.max_books] + sel
    flat_log = {}
    print(f"{len(ladders)} ladders, {len(cands)} candidates to verify")

    need = []
    for c in cands:
        if c["kind"] == "ladder":
            ts_ = [c["lo"]["ticker"], c["hi"]["ticker"]]
        elif c["kind"] == "sum":
            ts_ = [m["ticker"] for m in c["ms"]]
        elif c["kind"] == "tier":
            ts_ = [c["small"]["ticker"], c["big"]["ticker"]]
        elif c["kind"] == "flat":
            ts_ = [c["lo"]["ticker"], c["hi"]["ticker"]]
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
        elif c["kind"] == "flat":
            apply_book(c["lo"], books[c["lo"]["ticker"]])
            apply_book(c["hi"], books[c["hi"]["ticker"]])
            it = flat_item(c, a)
            flat_log[(c["lo"]["ticker"], c["hi"]["ticker"])] = flat_reason(c, a)
        elif c["kind"] == "tier":
            apply_book(c["small"], books[c["small"]["ticker"]])
            apply_book(c["big"], books[c["big"]["ticker"]])
            it = tier_item(c, a)
        else:
            apply_book(c["leg"], books[c["leg"]["ticker"]])
            it = book_item(c, a)
        if it:
            items.append(it)

    recent = [m for m in markets if (ts(m.get("open_time") or m.get("created_time") or "") or 0) >= now - a.new_hours * 3600]
    elig = sorted((m for m in markets if any(quotes(m))), key=lambda m: m["ticker"])
    cur = (state.get("cursor", 0) % len(elig)) if elig else 0
    sweep = (elig + elig)[cur:cur + a.cheap_sweep] if elig else []
    ctx["cursor"] = (cur + a.cheap_sweep) % len(elig) if elig else 0
    pool = sorted(markets, key=lambda m: num(m, "volume_24h"), reverse=True)[:a.cheap_top] + sweep + recent[:30]
    seen_t = set()
    for m in pool:
        if m["ticker"] in seen_t:
            continue
        seen_t.add(m["ticker"])
        if fetch_book(m["ticker"]):
            items += cheap_items(m, books[m["ticker"]], a)

    spikes = []
    try:
        spikes = detect_spikes(markets, state, now, a, ctx, fetch_book, books)
    except Exception as ex:
        ctx["spike_err"] = f"{type(ex).__name__}: {ex}"[:150]
        print("spike detection failed:", ctx["spike_err"])
    ctx["spikes"] = spikes

    for it in items:
        it["conf"] = CONF[it["kind"]]
        it["score"] = round(it["profit"] * it["conf"], 2)
    items.sort(key=lambda x: x["score"], reverse=True)
    watch = new_markets(markets, a, now)
    if getattr(a, "debug", None):
        try:
            ctx["debug"] = debug_report(a.debug, evs, ladders, flats, sel, flat_log, a)
        except Exception as ex:
            ctx["debug"] = {"term": a.debug, "error": f"{type(ex).__name__}: {ex}"[:200], "events": []}
    cat_stats = {}
    for m in markets:
        cat_stats.setdefault(m["_cat"], {"markets": 0, "ladders": 0, "flagged": 0})["markets"] += 1
    for L in ladders:
        cat_stats.setdefault(L["cat"], {"markets": 0, "ladders": 0, "flagged": 0})["ladders"] += 1
    return items, watch, {"events": len(events), "markets": len(markets), "ladders": len(ladders),
                          "checked": len(cands), "books": books_info, "cats": cat_stats, "sports_excluded": excluded, "crypto_excluded": crypto_excluded}


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
    ap.add_argument("--time-budget", type=int, default=240, help="seconds for order-book checks")
    ap.add_argument("--push-min-profit", type=float, default=3.0, help="min score ($) to send a push")
    ap.add_argument("--keep", type=int, default=200)
    ap.add_argument("--max-edge", type=float, default=60, help="drop anything with a bigger edge (cents): almost surely a data/matching error")
    ap.add_argument("--sports", default="nfl_nba", help="nfl_nba (default) or all")
    ap.add_argument("--crypto", action="store_true", help="include crypto markets (ignored by default)")
    ap.add_argument("--max-edge-spec", type=float, default=25, help="drop shape/same-price/sportsbook items claiming more than this (cents)")
    ap.add_argument("--flat-per-cat", type=int, default=15, help="same-price candidates verified per topic per run")
    ap.add_argument("--ladders-per-cat", type=int, default=40, help="ladders shown per topic on the Ladders tab")
    ap.add_argument("--flat-max-q", type=float, default=3.0, help="most value (cents) to credit for the extra range")
    ap.add_argument("--flat-min-edge", type=float, default=0.2, help="min value gap (cents) to flag a same-price tier")
    ap.add_argument("--flat-min-profit", type=float, default=0.25, help="min est. profit ($) for a same-price tier")
    ap.add_argument("--flat-books", type=int, default=100, help="same-price tiers to verify per run")
    # cheap offers
    ap.add_argument("--ask-gap", type=int, default=15, help="cents between best offer and the next one")
    ap.add_argument("--cheap-last-gap", type=int, default=10, help="the offer must also be this far below the last trade")
    ap.add_argument("--cheap-max-size", type=int, default=500, help="max contracts at the cheap offer")
    ap.add_argument("--cheap-min-price", type=int, default=5, help="ignore offers below this many cents")
    ap.add_argument("--cheap-top", type=int, default=30, help="most-traded markets to always check for cheap offers")
    ap.add_argument("--cheap-sweep", type=int, default=120, help="other markets checked per run, rotating through all of them")
    # spikes (anti-momentum)
    ap.add_argument("--spike-min", type=int, default=15, help="min move in cents vs the pre-spike level")
    ap.add_argument("--spike-max-hours", type=int, default=12, help="ignore moves that started longer ago than this")
    ap.add_argument("--spike-thin", type=int, default=150, help="contracts traded during the move to call it thin")
    ap.add_argument("--spike-push", type=int, default=25, help="push alert for thin, unconfirmed spikes this big")
    ap.add_argument("--spike-verify", type=int, default=25, help="biggest movers to check against price history")
    ap.add_argument("--spike-alpha", type=float, default=0.08, help="how fast the baseline follows price (per scan)")
    ap.add_argument("--spike-max-spread", type=int, default=12, help="skip spikes in markets with wider spreads")
    ap.add_argument("--spike-min-gain", type=float, default=3.0, help="min gain in cents if it retraces halfway")
    # new markets
    ap.add_argument("--new-hours", type=float, default=6)
    ap.add_argument("--wide-spread", type=int, default=10)
    ap.add_argument("--new-keep", type=int, default=40)
    # sportsbooks
    ap.add_argument("--book-min-edge", type=float, default=8.0, help="min gap vs sportsbooks, cents after fees")
    ap.add_argument("--min-books", type=int, default=3, help="min bookmakers agreeing on a line")
    ap.add_argument("--odds-sports", default="", help="e.g. basketball_nba,americanfootball_nfl (off by default; costs credits)")
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
    term = (os.environ.get("DEBUG_SEARCH") or "").strip()
    if term == "-":
        term = ""
    elif not term:
        term = state.get("debug_term", "")
    a.debug = term
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
    info["spike_err"] = ctx.get("spike_err")

    def cap(it):  # sets pay per full set; judgment-based types get a tighter cap
        if it["kind"] == "sum":
            return a.max_edge * it.get("N", 1)
        return a.max_edge_spec if it["kind"] in ("shape", "flat", "book") else a.max_edge
    dropped = [it for it in items if it["edge"] > cap(it)]
    if dropped:
        print(f"dropped {len(dropped)} implausible items (edge too large):", [d["title"] for d in dropped][:5])
    items = [it for it in items if it["edge"] <= cap(it)]
    for it in items:
        add_links(it)
    fs = state.get("first_seen", {})
    for it in items:
        it["first_seen"] = fs.get(it["id"], now)
    items = items[:a.keep]
    cats = info.setdefault("cats", {})
    for it in items:
        cats.setdefault(it["cat"], {"markets": 0, "ladders": 0, "flagged": 0})["flagged"] += 1

    pushed = {k: v for k, v in state.get("pushed", {}).items() if now - v < 86400}
    page_url = os.environ.get("PAGES_URL")
    if not info["error"]:
        fresh = [it for it in items if it["id"] not in pushed and it["score"] >= a.push_min_profit]
        spk = [x for x in ctx.get("spikes", []) if x["thin"] and x["iso"] and abs(x["move"]) >= a.spike_push
               and f"{x['ticker']}|spike|{int(now // 21600)}" not in pushed]
        if fresh or spk:
            lines = [f"{KIND_NAME[it['kind']]} | {it['cat']} | {it['title'][:70]}\n"
                     f"{it['headline']}\nPay {it.get('pay', it['legs'][0]['price'])}¢, gain about {it['edge']:.1f}¢ per contract"
                     f"{'' if it.get('sure') else ' (estimate)'}; up to ${it['profit']:.2f} on {it['size']} contracts." for it in fresh[:5]]
            if len(fresh) > 5:
                lines.append(f"+{len(fresh) - 5} more on the page")
            for x in spk[:3]:
                lines.append(f"Spike {x['dir']} | {x['cat']} | {x['title'][:60]}\n{x['base']}c to {x['now']}c on only "
                             f"{x['vol_move']} contracts, no related market moved. Fade: {x['legs'][0]['action']} at {x['risk']}c.")
                pushed[f"{x['ticker']}|spike|{int(now // 21600)}"] = now
            n = len(fresh) + len(spk[:3])
            push(f"Kalshi: {n} new item{'' if n == 1 else 's'}", "\n\n".join(lines), page_url)
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
               "odds": ctx.get("odds", state.get("odds")), "odds_meter": ctx.get("odds_meter", state.get("odds_meter")),
               "cursor": ctx.get("cursor", state.get("cursor", 0)), "debug_term": term, "ladders": ctx.get("ladders", state.get("ladders", [])), 
               "base": ctx.get("base", state.get("base", {})), "spike_log": ctx.get("spike_log", state.get("spike_log", [])),
               "spikes": ctx.get("spikes", state.get("spikes", []))},
              open(STATE_PATH, "w", encoding="utf-8"), separators=(",", ":"))
    os.makedirs(SITE, exist_ok=True)
    json.dump({"updated": now, "scan": info, "items": items, "watch": watch,
               "spikes": ctx.get("spikes", state.get("spikes", [])),
               "ladders": ctx.get("ladders", state.get("ladders", [])), "spike_stats": ctx.get("spike_stats", spike_stats(state.get("spike_log", []))), "debug": ctx.get("debug")},
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
.card.arb,.card.sum,.card.tier{border-left-color:var(--green)}
.card.spike{border-left-color:var(--red)}.card.set{border-left-color:var(--muted)}.card.tier{border-left-color:var(--green)}
.card.book{border-left-color:var(--purple)}.card.cheap{border-left-color:var(--amber)}.card.new{border-left-color:var(--teal)}
.top{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;margin-bottom:5px}
.kind{font-weight:700;color:var(--blue)}.arb .kind,.sum .kind,.tier .kind{color:var(--green)}
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
<p><b>Same price, more coverage</b>: a lower threshold (say "above 50") costs the same as a higher one ("above 55") even though it wins in more cases. It should cost more, so it is the cheap one. This is a plain purchase of one contract, not a spread. The value gap is usually small at prices near 97&cent; and larger in the middle of a ladder.</p>
<p><b>Sportsbook gap</b>: Kalshi is on the wrong side of the sportsbook consensus (vig removed) by a wide margin. Totals, spreads and player props only. Books refund a player who doesn't play while Kalshi may not, so check that risk. Compare the sportsbook line on the card with the Kalshi rules before trading.</p>
<p><b>Riskless</b> also covers lists of candidates where only a few can win. For example "Top 10 TE" lists 16 players but only 10 can finish top 10, so their YES prices can't add up to more than 1,000&cent;. If they do, buying NO on every player is riskless. The same player also can't be priced higher for "top 1" than for "top 10".</p>
<p><b>Spikes</b> are a separate idea: a price that jumped far above (or crashed far below) its recent level. The page suggests the opposite side (fade it) and shows the worst case. These are speculation, not mispricings. The track record panel measures whether fading has actually worked.</p>
<p><b>Cheap offer</b>: the lowest offer is far below the next one in the book. It might be a mistake or might disappear before you act.</p>
<p><b>How to read the numbers.</b> A contract pays $1 (100&cent;) if it wins and $0 if not, and its price is what you pay. <b>You pay</b> is the price now. <b>Gain per contract</b> is how many cents you expect to make on each one after fees. If the card says <i>guaranteed</i>, the trades pay at least what they cost in every outcome. If it says <i>estimate</i>, the gain depends on the market really being worth more than the price. <b>Most $ possible</b> is that gain times the contracts available at those exact prices, the ceiling if you bought everything on offer. <b>Best</b> sorting weights it by how certain each type is. Prices can move before you trade, and only the top of the book is checked.</p>
</details>
<details id="bk" style="display:none"></details>
<details id="cov" style="display:none"></details>
<details id="track" style="display:none"></details>
<details id="dbg" style="display:none"></details>
<div class="chips" id="tabs"></div>
<input class="search" id="q" type="search" placeholder="Search (e.g. Rotten Tomatoes, CPI, Brunson)">
<div class="lbl">Topic</div><div class="chips" id="cats"></div>
<div id="typewrap"><div class="lbl">Type</div><div class="chips" id="types"></div></div>
<div id="sortwrap"><div class="lbl">Sort by</div><div class="chips" id="sorts"></div></div>
<div class="count" id="count"></div>
<div id="feed"></div>
</div>
<script>
const TN={arb:"Riskless",sum:"Riskless",tier:"Riskless",flat:"Same price, more coverage",shape:"Likely mispriced",book:"Sportsbook gap",cheap:"Cheap offer"};
const GRP={arb:"safe",sum:"safe",tier:"safe",flat:"flat",shape:"shape",book:"book",cheap:"cheap"};
const TYPES=[["safe","Riskless"],["flat","Same price, more coverage"],["book","Sportsbook gap"],["cheap","Cheap offer"]];
const SORTS=[["score","Best"],["profit","Most $ possible"],["edge","Gain per contract"],["new","Newest"]];
let S={tab:"opps",cat:"ALL",type:"ALL",sort:"score",q:""},data=null;
try{const x=JSON.parse(localStorage.getItem("kalshi-board2")||"{}");S.cat=x.cat||"ALL";S.type=x.type||"ALL";S.sort=x.sort||"score";S.tab=x.tab||"opps";if(S.tab==="sets")S.tab="opps"}catch(e){}
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
  '&cent;</span><span class="q">'+l.qty+' avail</span></div>'+
  '<div class="leg" style="font-size:12px;padding-top:0"><span class="tk">'+esc(l.ticker)+'</span>'+
  (okurl(l.eurl)?'<a href="'+esc(l.eurl)+'" target="_blank" rel="noopener" style="color:var(--blue)">event page</a>':"")+
  (okurl(l.surl)?'<a href="'+esc(l.surl)+'" target="_blank" rel="noopener" style="color:var(--blue)">series page</a>':"")+'</div>').join("")+
  (i.legs.length>12?'<div class="leg"><span class="q">+'+(i.legs.length-12)+' more legs</span></div>':"");
 const multi=i.legs.length>1,sure=i.sure===true;
 const pay=i.pay!=null?i.pay:i.legs[0].price,worth=i.worth!=null?i.worth:null;
 const k=Math.max(1,Math.min(i.size,100));
 const box1='<div class="num"><b>'+pay+'&cent;</b><span>'+(multi?'you pay, all legs together':'you pay per contract')+'</span></div>';
 const box2=worth!=null?'<div class="num"><b>'+(sure?'at least ':'about ')+worth+'&cent;</b><span>'+(sure?'you get back, guaranteed':'what it is worth (estimate)')+'</span></div>':
  '<div class="num"><b>'+i.size+'</b><span>contracts available</span></div>';
 const box3='<div class="num"><b>+'+i.edge.toFixed(1)+'&cent;</b><span>'+(sure?'guaranteed gain, after fees':'estimated gain, not guaranteed')+'</span></div>';
 const money='<div class="cmp">Buying '+k+' contract'+(k===1?'':'s')+': you pay about $'+(pay*k/100).toFixed(2)+' and '+(sure?'gain about $':'would gain about $')+(i.edge*k/100).toFixed(2)+
  (sure?'':' if the estimate is right')+'. All '+i.size+' available at these prices: about $'+i.profit.toFixed(2)+'.</div>';
 return '<div class="card '+esc(i.kind)+'"><div class="top"><span class="kind">'+esc(TN[i.kind]||i.kind)+'</span><span class="cat">'+esc(i.cat)+
  '</span><span class="age">seen '+ago(i.first_seen)+'</span></div><div class="title">'+esc(i.title)+'</div><div class="head">'+esc(i.headline)+'</div>'+
  (i.means?'<div class="cmp" style="margin-top:6px"><b>What this means:</b> '+esc(i.means)+'</div>':"")+
  '<div class="nums">'+box1+box2+box3+'</div>'+
  '<div class="trade">'+legs+'</div>'+
  (i.cases?'<div class="cmp"><b>What you get in each case</b> (you pay '+pay+'&cent; per '+esc(i.unit||"pair")+' in total):<ul style="margin:6px 0 0 18px;padding:0">'+i.cases.map(c=>'<li>'+esc(c)+'</li>').join("")+'</ul></div>':"")+
  (i.buy?'<div class="cmp"><b>How much to buy:</b> '+esc(i.buy.intro)+'<ul style="margin:6px 0 0 18px;padding:0">'+i.buy.lines.map(c=>'<li>'+esc(c)+'</li>').join("")+'</ul></div>':money)+(i.note?'<div class="cmp">'+esc(i.note)+'</div>':"")+
  (i.compare?'<div class="cmp"><b>Sportsbooks:</b> '+esc(i.compare)+'</div>':"")+
  (i.refs?i.refs.map(r=>'<div class="cmp"><b>For comparison:</b> '+esc(r.label)+' is bid '+r.bid+'&cent; and offered at '+r.ask+'&cent; ('+esc(r.note)+')</div>').join(""):"")+
  '<div class="why">'+esc(i.why)+'</div>'+
  (!i.legs[0].eurl&&url?'<div class="meta"><a href="'+esc(url)+'" target="_blank" rel="noopener">Open on Kalshi</a></div>':"")+'</div>'}
function wcard(w){
 const url=okurl(w.url);
 const px=(w.bid||w.ask)?("Bid "+(w.bid?w.bid+"\u00a2":"none")+" \u00b7 Ask "+(w.ask?w.ask+"\u00a2":"none")):"No prices yet";
 return '<div class="card new"><div class="top"><span class="kind">New market</span><span class="cat">'+esc(w.cat)+
  '</span><span class="age">opened '+ago(w.opened)+' ago</span></div><div class="title">'+esc(w.title)+'</div>'+
  (w.sub?'<div class="osub">'+esc(w.sub)+'</div>':"")+'<div class="head">'+px+'</div><div class="meta"><span class="tk">'+esc(w.ticker)+'</span>'+
  (url?'<a href="'+esc(url)+'" target="_blank" rel="noopener">Open on Kalshi</a>':"")+'</div></div>'}
function lcard(x){
 const url=okurl(x.url);
 let rows="";
 x.tiers.forEach((t,i)=>{
  rows+='<div class="leg"><b>'+esc(t.label)+'</b><span class="q">bid '+t.bid+'&cent; / ask '+t.ask+'&cent;</span></div>';
  const st=x.steps[i];
  if(st){const f=st.flat;
   rows+='<div class="leg" style="'+(f?'background:var(--bg);color:var(--amber)':'color:var(--muted)')+';font-size:12.5px"><span>'+(f?'<b>Priced the same:</b> ':'')+
    esc(st.lo)+' to '+esc(st.hi)+' is priced at about '+st.prob+'%.'+(f?' The lower tier wins in more cases, so it is the better buy at the same price.':'')+'</span></div>'}
 });
 return '<div class="card"><div class="top"><span class="kind">Ladder</span><span class="cat">'+esc(x.cat)+'</span><span class="age">'+x.flat+' flat step'+(x.flat===1?"":"s")+'</span></div>'+
  '<div class="title">'+esc(x.title)+'</div><div class="trade" style="margin-top:8px">'+rows+'</div>'+
  (url?'<div class="meta"><a href="'+esc(url)+'" target="_blank" rel="noopener">Open on Kalshi</a></div>':"")+'</div>'}
function spcard(x){
 const url=okurl(x.url),up=x.dir==="up";
 const l=x.legs[0];
 const tags=(x.thin?'<span class="cat">Thin volume</span>':'<span class="cat">Heavy volume</span>')+
  (x.iso?'<span class="cat">No related market moved</span>':'<span class="cat">'+x.related+' related market'+(x.related===1?"":"s")+' moved</span>');
 return '<div class="card spike"><div class="top"><span class="kind" style="color:var(--red)">Spike '+(up?"up":"down")+'</span><span class="cat">'+esc(x.cat)+
  '</span>'+tags+'<span class="age">started ~'+x.hours+' h ago</span></div><div class="title">'+esc(x.title)+'</div>'+(x.sub?'<div class="osub">'+esc(x.sub)+'</div>':"")+
  '<div class="head">YES moved from '+x.base+'&cent; to '+x.now+'&cent; ('+(x.move>0?"+":"")+x.move+'&cent;) on about '+x.vol_move+' contracts.</div>'+
  '<div class="nums"><div class="num"><b>+'+x.gain.toFixed(1)+'&cent;</b><span>gain if it retraces halfway (to '+x.target+'&cent;)</span></div>'+
  '<div class="num"><b>'+x.risk+'&cent;</b><span>worst case loss per contract</span></div>'+
  '<div class="num"><b>'+x.breakeven+'%</b><span>retrace rate needed if the worst case is total loss</span></div></div>'+
  '<div class="trade"><div class="leg"><b>'+esc(l.action)+'</b><span>'+esc(l.label)+'</span><span class="px">'+l.price+'&cent;</span><span class="q">'+l.qty+' avail</span></div></div>'+
  '<div class="why">Fade idea, not a guaranteed edge. Spikes on thin volume with nothing else moving are more likely noise or a single eager buyer. Spikes on heavy volume, or where related markets moved, are more likely real news, and fading real news loses.</div>'+
  (url?'<div class="meta"><a href="'+esc(url)+'" target="_blank" rel="noopener">Open on Kalshi</a></div>':"")+'</div>'}
function debugPanel(){
 const el=document.getElementById("dbg"),d=data.debug;
 if(!d){el.style.display="none";return}
 el.style.display="";
 let h="<summary>Debug: what the scanner saw for \u201c"+esc(d.term)+"\u201d</summary>";
 if(d.error)h+='<p class="bad">'+esc(d.error)+"</p>";
 if(!d.events.length)h+="<p>No open Kalshi event matched that text. Check the spelling, or the market may not be in the open-events list.</p>";
 d.events.forEach(e=>{
  h+="<p><b>"+esc(e.title)+"</b> ("+esc(e.event)+", topic: "+esc(e.category)+", "+e.n+" markets)</p>";
  h+="<p>"+e.markets.map(m=>esc(m.sub)+" \u00b7 strike_type "+esc(m.strike_type)+" "+(m.floor!=null?"floor "+m.floor:"")+(m.cap!=null?" cap "+m.cap:"")+" \u00b7 bid "+(m.bid==null?"-":m.bid)+" / ask "+(m.ask==null?"-":m.ask)+" \u00b7 "+(m.recognized?"recognized as a threshold":"NOT recognized as a threshold")).join("<br>")+"</p>";
  if(!e.ladders.length)h+="<p>No price ladder was built from this event.</p>";
  e.ladders.forEach(l=>{
   h+="<p>Ladder: "+l.tiers+" tiers, "+l.usable+" with usable quotes. "+(l.candidates.length?"Same-price candidates:<br>"+l.candidates.map(c=>esc(c.pair)+" \u00b7 value gap "+c.value_gap+"\u00a2 \u00b7 <b>"+esc(c.result)+"</b>").join("<br>"):"No same-price candidates.")+"</p>"})
 });
 el.innerHTML=h}
function track(){
 const el=document.getElementById("track"),t=data.spike_stats;
 if(S.tab!=="spikes"||!t){el.style.display="none";return}
 el.style.display="";
 const row=(name,g)=>{if(!g||!g.count)return "";const f=(h,k)=>g[h]?g[h][k]:null;
  const cell=h=>g[h]?("median retrace "+g[h].median+"%, half or more: "+g[h].half+"%, kept going: "+g[h].worse+"% (n="+g[h].n+")"):"not enough yet";
  return "<p><b>"+name+"</b> ("+g.count+" logged)<br>After 1 h: "+cell("h1")+"<br>After 6 h: "+cell("h6")+"<br>After 24 h: "+cell("h24")+"</p>"};
 el.innerHTML="<summary>Track record: did these spikes actually fade?</summary><p>Every spike the scanner sees is logged, and its price is checked again 1, 6 and 24 hours later. Retrace 100% means back to the pre-spike level; below 0% means it kept going.</p>"+
  row("Thin volume, nothing else moved",t.thin_iso)+row("All other spikes",t.other)+(t.all.count?"":"<p>Nothing logged yet. Outcomes fill in as time passes.</p>")}
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
function coverage(){
 const el=document.getElementById("cov"),c=data.scan.cats;
 if(!c||!Object.keys(c).length){el.style.display="none";return}
 el.style.display="";
 el.innerHTML="<summary>Coverage by topic</summary>"+Object.keys(c).sort((x,y)=>c[y].markets-c[x].markets).map(k=>
  "<p><b>"+esc(k)+"</b>: "+c[k].markets.toLocaleString()+" markets, "+c[k].ladders+" price ladders, "+(c[k].flagged||0)+" flagged</p>").join("")+
  (data.scan.sports_excluded?"<p>Sports are limited to NFL and NBA. "+data.scan.sports_excluded.toLocaleString()+" markets in other sports are ignored.</p>":"")+
  (data.scan.crypto_excluded?"<p>Crypto is ignored: "+data.scan.crypto_excluded.toLocaleString()+" markets skipped.</p>":"")}
function emptyMsg(){
 const c=(data.scan.cats||{})[S.cat];
 if(S.cat!=="ALL"&&c)return "Scanned "+c.markets.toLocaleString()+" "+esc(S.cat)+" markets and "+c.ladders+" price ladders in this topic. Nothing flagged right now.";
 return "Nothing matches right now. Real inconsistencies are rare and short-lived."}
function render(){
 if(!data)return;
 const sc=data.scan,age=Date.now()/1000-data.updated,watch=data.watch||[];
 let sub="Updated "+ago(data.updated)+" ago \u00b7 "+(sc.ladders||0)+" price ladders and "+(sc.markets||0).toLocaleString()+" markets checked";
 if(age>1200)sub+=' <span class="warn">(stale: scans may be paused)</span>';
 if(sc.error)sub+='<br><span class="bad">Last scan failed: '+esc(sc.error)+'</span>';
 else if(!sc.ladders)sub+='<br><span class="warn">No ladders were detected. The strike data may have a different format than expected.</span>';
 document.getElementById("sub").innerHTML=sub;
 books();coverage();track();debugPanel();
 const tb=document.getElementById("tabs");tb.innerHTML="";
 tb.appendChild(chip("Opportunities",data.items.length,S.tab==="opps",()=>{S.tab="opps";save();render()}));
 tb.appendChild(chip("Ladders",(data.ladders||[]).length,S.tab==="ladders",()=>{S.tab="ladders";save();render()}));
 tb.appendChild(chip("Spikes",(data.spikes||[]).length,S.tab==="spikes",()=>{S.tab="spikes";save();render()}));
 tb.appendChild(chip("New markets",watch.length,S.tab==="new",()=>{S.tab="new";save();render()}));
 const opps=S.tab==="opps";
 document.getElementById("typewrap").style.display=opps?"":"none";
 document.getElementById("sortwrap").style.display=opps?"":"none";
 const q=S.q.trim().toLowerCase();
 const pool=opps?data.items:S.tab==="spikes"?(data.spikes||[]):S.tab==="ladders"?(data.ladders||[]):watch;
 const text=i=>opps?(i.title+" "+i.headline+" "+i.cat+" "+i.legs.map(l=>l.label+" "+l.ticker).join(" ")):S.tab==="ladders"?(i.title+" "+i.cat):(i.title+" "+(i.sub||"")+" "+i.cat+" "+i.ticker);
 const base=pool.filter(i=>!q||text(i).toLowerCase().includes(q));
 const byType=opps?base.filter(i=>S.type==="ALL"||GRP[i.kind]===S.type):base;
 const cov=sc.cats||{};
 const cats={};byType.forEach(i=>cats[i.cat]=(cats[i.cat]||0)+1);
 Object.keys(cov).forEach(c=>{if(!(c in cats))cats[c]=0});
 const ce=document.getElementById("cats");ce.innerHTML="";
 ce.appendChild(chip("All",byType.length,S.cat==="ALL",()=>{S.cat="ALL";save();render()}));
 Object.keys(cats).sort((x,y)=>(cats[y]-cats[x])||(((cov[y]||{}).markets||0)-((cov[x]||{}).markets||0))).forEach(c=>{
  const b=chip(c,cats[c],S.cat===c,()=>{S.cat=c;save();render()});if(!cats[c])b.style.opacity=".55";ce.appendChild(b)});
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
 document.getElementById("count").textContent=opps?list.length+" opportunit"+(list.length===1?"y":"ies"):S.tab==="spikes"?list.length+" recent price spikes (thin, unconfirmed moves first)":S.tab==="ladders"?list.length+" ladders with tiers priced the same (widest flat stretches first). Each step shows how much chance the market gives to that range.":list.length+" newly opened, unpriced or wide-spread markets (newest first)";
 document.getElementById("feed").innerHTML=list.length?list.map(opps?card:S.tab==="spikes"?spcard:S.tab==="ladders"?lcard:wcard).join(""):
  '<div class="empty">'+emptyMsg()+'</div>';
}
document.getElementById("q").addEventListener("input",e=>{S.q=e.target.value;render()});
async function load(){try{const r=await fetch("board.json?"+Date.now());data=await r.json();render()}
 catch(e){document.getElementById("sub").textContent="Could not load board.json"}}
load();setInterval(load,60000);
</script></body></html>"""

if __name__ == "__main__":
    main()
