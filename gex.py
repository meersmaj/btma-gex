#!/usr/bin/env python3
"""
gex.py - dealer gamma exposure for the S&P 500 complex, from the free CBOE
delayed-quote chain. Writes a small JSON the Bullish Tools Morning Brief reads.

Outputs, for BOTH the whole chain (regime) and the front expiry alone (0DTE):
  gamma_flip  - spot where net dealer gamma crosses zero
  call_wall   - strike with the most positive call gamma (magnet / resistance)
  put_wall    - strike with the most negative put gamma (support)
  net_gex     - $ of dealer gamma per 1% move

Convention: the standard naive assumption - dealers are long calls and short
puts against customer flow. Positive net GEX = dealers sell rallies and buy
dips = vol suppressed, ranges hold. Negative = they amplify moves instead.
Rates and dividends are ignored (r = q = 0), worth a point or two on the flip.

Usage:
  python3 gex.py                    # fetch live, write out/gex-latest.json
  python3 gex.py --fixture f.json   # run against a saved chain (testing)
  python3 gex.py --schema           # print what the feed actually returned
"""
import argparse, datetime as dt, json, math, re, sys, urllib.request, zoneinfo

FEEDS = {
    "SPX": "https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json",
}
OSI = re.compile(r"^(?P<root>[A-Z]+?)(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")
ET = zoneinfo.ZoneInfo("America/New_York")
MULT = 100          # index option contract multiplier
BAND = 0.07         # walls must sit within +/-7% of spot to count
SQ2PI = math.sqrt(2.0 * math.pi)


def load(url, fixture=None):
    if fixture:
        with open(fixture) as fh:
            return json.load(fh)
    req = urllib.request.Request(url, headers={"User-Agent": "btma-gex/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def spot_from(data):
    for k in ("current_price", "close", "prev_day_close", "last"):
        v = data.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v), k
    raise SystemExit("could not find spot in feed; run --schema to see keys")


def parse(data, now):
    """-> list of dicts, one per live contract."""
    out = []
    for o in data.get("options", []):
        m = OSI.match(o.get("option", ""))
        if not m:
            continue
        oi = o.get("open_interest") or 0
        iv = o.get("iv") or 0
        if oi <= 0 or iv <= 0:
            continue
        y, mo, d = 2000 + int(m["ymd"][:2]), int(m["ymd"][2:4]), int(m["ymd"][4:6])
        expiry = dt.datetime(y, mo, d, 16, 0, tzinfo=ET)
        T = (expiry - now).total_seconds() / (365.0 * 86400.0)
        if T <= 0 or T > 1.0:                 # drop expired and >1y noise
            continue
        out.append({
            "root": m["root"],
            "expiry": expiry.date(),
            "is_call": m["cp"] == "C",
            "K": int(m["strike"]) / 1000.0,
            "oi": float(oi),
            "iv": float(iv),
            "T": T,
            "gamma_feed": float(o.get("gamma") or 0.0),
        })
    return out


def bs_gamma(S, K, T, sigma):
    """Black-Scholes gamma, r = q = 0."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / v
    return math.exp(-0.5 * d1 * d1) / (SQ2PI * S * v)


def net_gex_at(contracts, S, recompute=True):
    """$ dealer gamma per 1% move, at hypothetical spot S."""
    tot = 0.0
    for c in contracts:
        g = bs_gamma(S, c["K"], c["T"], c["iv"]) if recompute else c["gamma_feed"]
        sign = 1.0 if c["is_call"] else -1.0
        tot += sign * g * c["oi"] * MULT * S * S * 0.01
    return tot


def gamma_flip(contracts, spot):
    """Lowest spot in +/-10% where net gamma crosses from negative to positive."""
    lo, hi = spot * 0.90, spot * 1.10
    step = (hi - lo) / 160.0
    grid = [lo + i * step for i in range(161)]
    vals = [net_gex_at(contracts, s) for s in grid]
    cross = None
    for i in range(1, len(grid)):
        if vals[i - 1] < 0 <= vals[i] or vals[i - 1] > 0 >= vals[i]:
            a, b, fa, fb = grid[i - 1], grid[i], vals[i - 1], vals[i]
            for _ in range(24):                       # bisect to the point
                m = 0.5 * (a + b)
                fm = net_gex_at(contracts, m)
                if (fa < 0) == (fm < 0):
                    a, fa = m, fm
                else:
                    b, fb = m, fm
            cand = 0.5 * (a + b)
            if cross is None or abs(cand - spot) < abs(cross - spot):
                cross = cand
    return cross


def walls(contracts, spot):
    lo, hi = spot * (1 - BAND), spot * (1 + BAND)
    call, put = {}, {}
    for c in contracts:
        if not (lo <= c["K"] <= hi):
            continue
        g = bs_gamma(spot, c["K"], c["T"], c["iv"])
        dollars = g * c["oi"] * MULT * spot * spot * 0.01
        (call if c["is_call"] else put).setdefault(c["K"], 0.0)
        (call if c["is_call"] else put)[c["K"]] += dollars
    cw = max(call.items(), key=lambda kv: kv[1], default=(None, 0.0))
    pw = max(put.items(), key=lambda kv: kv[1], default=(None, 0.0))
    return (cw[0], cw[1]), (pw[0], pw[1])


def summarize(contracts, spot, label):
    if not contracts:
        return {"label": label, "contracts": 0, "note": "no open interest found"}
    cw, pw = walls(contracts, spot)
    net = net_gex_at(contracts, spot)
    flip = gamma_flip(contracts, spot)
    return {
        "label": label,
        "contracts": len(contracts),
        "total_oi": round(sum(c["oi"] for c in contracts)),
        "net_gex_per_1pct": round(net),
        "net_gex_billions": round(net / 1e9, 3),
        "regime": "positive" if net > 0 else "negative",
        "gamma_flip": round(flip, 1) if flip else None,
        "spot_vs_flip": round(spot - flip, 1) if flip else None,
        "call_wall": cw[0],
        "call_wall_gex": round(cw[1]),
        "put_wall": pw[0],
        "put_wall_gex": round(pw[1]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture")
    ap.add_argument("--schema", action="store_true")
    ap.add_argument("--out", default="out/gex-latest.json")
    a = ap.parse_args()

    raw = load(FEEDS["SPX"], a.fixture)
    data = raw.get("data", raw)

    if a.schema:
        print("top-level keys :", list(raw.keys()))
        print("data keys      :", [k for k in data if k != "options"])
        opts = data.get("options", [])
        print("contracts      :", len(opts))
        roots = sorted({OSI.match(o["option"])["root"] for o in opts
                        if OSI.match(o.get("option", ""))})
        print("roots          :", roots)
        if opts:
            print("sample contract:", json.dumps(opts[0], indent=2)[:400])
        return

    now = dt.datetime.now(ET)
    spot, spot_key = spot_from(data)
    contracts = parse(data, now)
    if not contracts:
        raise SystemExit("parsed 0 contracts - feed schema may have changed")

    front = min(c["expiry"] for c in contracts)
    zero_dte = [c for c in contracts if c["expiry"] == front]

    payload = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "generated_et": now.isoformat(timespec="seconds"),
        "feed_timestamp": raw.get("timestamp"),
        "underlying": "SPX",
        "spot": spot,
        "spot_source": spot_key,
        "front_expiry": front.isoformat(),
        "regime_all_expiries": summarize(contracts, spot, "all expiries"),
        "front_expiry_only": summarize(zero_dte, spot, f"0DTE ({front})"),
        "convention": "dealers long calls / short puts; r=q=0; $ per 1% move",
    }

    import os
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump(payload, fh, indent=2)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
