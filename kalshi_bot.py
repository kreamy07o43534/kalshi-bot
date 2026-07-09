#!/usr/bin/env python3
"""
kalshi_bot.py — all-in-one Kalshi temperature trading bot
=========================================================
One file, four commands:

  python kalshi_bot.py discover              list the real Kalshi series tickers
  python kalshi_bot.py calibrate --years 2   measure real per-station sigmas
  python kalshi_bot.py scan --top 5          daily ranking + best bet + edges
  python kalshi_bot.py backtest              score logged picks vs actuals

Running with no command defaults to `scan`.
Everything uses free, no-key data: NWS, Open-Meteo, IEM, and Kalshi public API.

Setup:
  pip install requests
  (edit the UA line below to include your real email)

See the command reference at the bottom of this file, or run:
  python kalshi_bot.py -h
"""
import argparse
import csv
import json
import math
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    raise SystemExit("Python 3.9+ required (zoneinfo).")

import requests

# NWS requires a descriptive User-Agent with a real contact. EDIT THIS.
UA = {"User-Agent": "kalshi-temp-bot (contact: your-email@example.com)"}

# ===========================================================================
# CONFIG — the 20 stations + tunable knobs
# ===========================================================================
# FIELDS: settle=ICAO Kalshi settles on | iem=IEM id | net=IEM network
#         wfo=NWS office (for AFD) | lat/lon | tz | k_high/k_low=Kalshi suffix
#
# VERIFY: Chicago=Midway(KMDW), Houston=Hobby(KHOU), NYC=Central Park(KNYC).
# Kalshi suffixes are best-guesses EXCEPT NY. Run `discover` and paste real ones.
STATIONS = {
    "ATL": dict(settle="KATL", iem="ATL", net="GA_ASOS", wfo="FFC",
                lat=33.6367, lon=-84.4281, tz="America/New_York",     k_high="ATL",  k_low="ATL"),
    "AUS": dict(settle="KAUS", iem="AUS", net="TX_ASOS", wfo="EWX",
                lat=30.1975, lon=-97.6664, tz="America/Chicago",      k_high="AUS",  k_low="AUS"),
    "BOS": dict(settle="KBOS", iem="BOS", net="MA_ASOS", wfo="BOX",
                lat=42.3656, lon=-71.0096, tz="America/New_York",     k_high="BOS",  k_low="BOS"),
    "ORD": dict(settle="KMDW", iem="MDW", net="IL_ASOS", wfo="LOT",   # Kalshi = Midway
                lat=41.7860, lon=-87.7524, tz="America/Chicago",      k_high="CHI",  k_low="CHI"),
    "DFW": dict(settle="KDFW", iem="DFW", net="TX_ASOS", wfo="FWD",
                lat=32.8998, lon=-97.0403, tz="America/Chicago",      k_high="DFW",  k_low="DFW"),
    "DEN": dict(settle="KDEN", iem="DEN", net="CO_ASOS", wfo="BOU",
                lat=39.8561, lon=-104.6737, tz="America/Denver",      k_high="DEN",  k_low="DEN"),
    "IAH": dict(settle="KHOU", iem="HOU", net="TX_ASOS", wfo="HGX",   # Kalshi = Hobby
                lat=29.6454, lon=-95.2789, tz="America/Chicago",      k_high="HOU",  k_low="HOU"),
    "LAS": dict(settle="KLAS", iem="LAS", net="NV_ASOS", wfo="VEF",
                lat=36.0840, lon=-115.1537, tz="America/Los_Angeles", k_high="LAS",  k_low="LAS"),
    "LAX": dict(settle="KLAX", iem="LAX", net="CA_ASOS", wfo="LOX",
                lat=33.9416, lon=-118.4085, tz="America/Los_Angeles", k_high="LAX",  k_low="LAX"),
    "MIA": dict(settle="KMIA", iem="MIA", net="FL_ASOS", wfo="MFL",
                lat=25.7959, lon=-80.2870, tz="America/New_York",     k_high="MIA",  k_low="MIA"),
    "MSP": dict(settle="KMSP", iem="MSP", net="MN_ASOS", wfo="MPX",
                lat=44.8848, lon=-93.2223, tz="America/Chicago",      k_high="MSP",  k_low="MSP"),
    "MSY": dict(settle="KMSY", iem="MSY", net="LA_ASOS", wfo="LIX",
                lat=29.9934, lon=-90.2580, tz="America/Chicago",      k_high="MSY",  k_low="MSY"),
    "NYC": dict(settle="KNYC", iem="NYC", net="NY_ASOS", wfo="OKX",   # Central Park
                lat=40.7789, lon=-73.9692, tz="America/New_York",     k_high="NY",   k_low="NY"),
    "OKC": dict(settle="KOKC", iem="OKC", net="OK_ASOS", wfo="OUN",
                lat=35.3931, lon=-97.6007, tz="America/Chicago",      k_high="OKC",  k_low="OKC"),
    "PHL": dict(settle="KPHL", iem="PHL", net="PA_ASOS", wfo="PHI",
                lat=39.8729, lon=-75.2437, tz="America/New_York",     k_high="PHIL", k_low="PHIL"),
    "PHX": dict(settle="KPHX", iem="PHX", net="AZ_ASOS", wfo="PSR",
                lat=33.4342, lon=-112.0116, tz="America/Phoenix",     k_high="PHX",  k_low="PHX"),
    "SAT": dict(settle="KSAT", iem="SAT", net="TX_ASOS", wfo="EWX",
                lat=29.5337, lon=-98.4698, tz="America/Chicago",      k_high="SAT",  k_low="SAT"),
    "SFO": dict(settle="KSFO", iem="SFO", net="CA_ASOS", wfo="MTR",
                lat=37.6188, lon=-122.3750, tz="America/Los_Angeles", k_high="SF",   k_low="SF"),
    "SEA": dict(settle="KSEA", iem="SEA", net="WA_ASOS", wfo="SEW",
                lat=47.4502, lon=-122.3088, tz="America/Los_Angeles", k_high="SEA",  k_low="SEA"),
    "DCA": dict(settle="KDCA", iem="DCA", net="VA_ASOS", wfo="LWX",
                lat=38.8512, lon=-77.0402, tz="America/New_York",     k_high="DC",   k_low="DC"),
}

ENSEMBLE_MODELS = {          # Open-Meteo ensemble models + weights (tune via backtest)
    "gfs_seamless":  1.0,    # NOAA GEFS
    "ecmwf_ifs025":  1.2,    # ECMWF (usually best)
    "icon_seamless": 1.0,    # DWD
    "gem_global":    0.8,    # Canadian
}

SETTINGS = dict(
    nws_weight_base=0.45, disagree_soft=1.0, disagree_hard=3.0,
    alert_multiplier=0.80, sigma_floor=1.2,
    edge_threshold=0.05, kelly_fraction=0.25, bankroll=1000.0,
    predictions_log="predictions.csv", sigmas_file="sigmas.json",
)

AFD_CONFIDENT = ["high confidence", "robust", "strong ridge", "strong high pressure",
                 "well established", "little change", "lock", "no significant"]
AFD_UNCERTAIN = ["uncertain", "low confidence", "spread in guidance", "model differences",
                 "difficult forecast", "timing remains", "could vary", "questionable"]
AFD_PATTERNS = {
    "frontal":    ["cold front", "warm front", "frontal passage", "trough", "shortwave",
                   "backdoor front", "boundary"],
    "convective": ["thunderstorm", "convection", "showers", "storms", "cloud cover",
                   "outflow", "mcs"],
    "seabreeze":  ["sea breeze", "sea-breeze", "marine layer", "marine push",
                   "onshore flow", "bay breeze", "marine influence"],
    "wind":       ["gusty", "downslope", "chinook", "santa ana", "offshore flow",
                   "foehn", "high winds"],
}

NWS = "https://api.weather.gov"
OM_ENS = "https://ensemble-api.open-meteo.com/v1/ensemble"
OM_HIST = "https://historical-forecast-api.open-meteo.com/v1/forecast"
KALSHI = "https://external-api.kalshi.com/trade-api/v2"
IEM_ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
WETHR = "https://wethr.net/api/v2"
WETHR_KEY = os.environ.get("WETHR_API_KEY")  # optional; pulls from env if set
_wethr_notified = set()

# ===========================================================================
# SHARED HELPERS
# ===========================================================================
def http_json(url, params=None, tries=4, timeout=30, pause=0.4, headers=None):
    hdr = dict(UA)
    if headers:
        hdr.update(headers)
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=hdr, timeout=timeout)
            if r.status_code == 404:
                r.raise_for_status()
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.HTTPError(str(r.status_code))
            r.raise_for_status()
            time.sleep(pause)
            return r.json()
        except requests.HTTPError as e:
            if "404" in str(e):
                raise
            last = e; time.sleep(pause * (2 ** i))
        except Exception as e:
            last = e; time.sleep(pause * (2 ** i))
    raise last

def http_text(url, params=None, tries=4, timeout=60, pause=0.6):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=timeout)
            r.raise_for_status()
            time.sleep(pause)
            return r.text
        except Exception as e:
            last = e; time.sleep(pause * (2 ** i))
    raise last

def std_offset(tzname):
    """Local STANDARD-time offset (Kalshi settles on the LST midnight-midnight day)."""
    return datetime(2025, 1, 15, 12, tzinfo=ZoneInfo(tzname)).utcoffset()

def season_of(date_str):
    m = int(date_str[5:7])
    return {12:"DJF",1:"DJF",2:"DJF",3:"MAM",4:"MAM",5:"MAM",
            6:"JJA",7:"JJA",8:"JJA",9:"SON",10:"SON",11:"SON"}[m]

def norm_cdf(x, mu, sigma):
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))

def interval_prob(lo, hi, mu, sigma):
    return norm_cdf(hi, mu, sigma) - norm_cdf(lo, mu, sigma)

def iem_tmpf_daily(cfg, start_dt, end_dt):
    """{lst_date: (max_f, min_f)} of actual obs, bucketed by Local Standard Time."""
    txt = http_text(IEM_ASOS, params={
        "station": cfg["iem"], "network": cfg["net"], "data": "tmpf",
        "sts": start_dt.strftime("%Y-%m-%dT00:00:00Z"),
        "ets": end_dt.strftime("%Y-%m-%dT00:00:00Z"),
        "tz": "Etc/UTC", "format": "comma", "latlon": "no", "missing": "M", "trace": "T",
    })
    off = std_offset(cfg["tz"])
    daily, header = {}, None
    for line in txt.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if header is None:
            header = [h.strip() for h in line.split(",")]
            continue
        r = line.split(",")
        try:
            raw = r[header.index("tmpf")].strip()
            if raw in ("M", "T", ""):
                continue
            t = float(raw)
            dt = datetime.strptime(r[header.index("valid")].strip(),
                                   "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            d = (dt + off).strftime("%Y-%m-%d")
            hi, lo = daily.get(d, (-999, 999))
            daily[d] = (max(hi, t), min(lo, t))
        except (ValueError, IndexError):
            continue
    return {d: v for d, v in daily.items() if v[0] > -900 and v[1] < 900}

def _wethr_note(msg):
    """Print a Wethr status line once per process (visible in Railway logs)."""
    if msg not in _wethr_notified:
        _wethr_notified.add(msg)
        print(msg, flush=True)

def wethr_confirmed(station_code):
    """Confirmed Wethr High/Low so far for the current trading day (NWS logic).
    Returns (high, low) or (None, None). Used as a same-day clamp on the forecast:
    the day's high can't end up below what's already been reached, and vice versa.
    Auth is a Bearer token header, base host is wethr.net."""
    if not WETHR_KEY:
        return None, None
    try:
        data = http_json(f"{WETHR}/observations.php",
                         params={"station_code": station_code,
                                 "mode": "wethr_high", "logic": "nws"},
                         headers={"Authorization": f"Bearer {WETHR_KEY}"})
        hi, lo = data.get("wethr_high"), data.get("wethr_low")
        _wethr_note(f"[wethr] connected OK (sample {station_code}: H{hi}/L{lo})")
        return (float(hi) if hi is not None else None,
                float(lo) if lo is not None else None)
    except Exception as ex:
        _wethr_note(f"[wethr] request failed ({station_code}): {ex} — running without Wethr")
        return None, None

# ===========================================================================
# COMMAND: discover  — list real Kalshi series tickers
# ===========================================================================
def cmd_discover(args):
    print("Scanning Kalshi for live KXHIGH*/KXLOW* series...\n")
    found, cursor = {}, None
    for _ in range(40):
        params = {"status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = http_json(f"{KALSHI}/markets", params=params)
        for m in data.get("markets", []):
            t = m.get("ticker", "")
            if t.startswith("KXHIGH") or t.startswith("KXLOW"):
                found.setdefault(t.split("-")[0], m.get("title", ""))
        cursor = data.get("cursor")
        if not cursor:
            break
    if not found:
        print("None found right now (markets may be closed). Try during US daytime.")
        return
    highs = sorted(s for s in found if s.startswith("KXHIGH"))
    lows = sorted(s for s in found if s.startswith("KXLOW"))
    print("HIGH series:")
    for s in highs:
        print(f"   {s:<12} suffix='{s[6:]}'   {found[s]}")
    print("\nLOW series:")
    for s in lows:
        print(f"   {s:<12} suffix='{s[5:]}'   {found[s]}")
    print("\nPaste the correct suffixes into STATIONS -> k_high / k_low.")

# ===========================================================================
# COMMAND: calibrate  — measure real per-station sigmas -> sigmas.json
# ===========================================================================
def fetch_om_forecasts(cfg, start, end):
    data = http_json(OM_HIST, params={
        "latitude": cfg["lat"], "longitude": cfg["lon"],
        "start_date": start.strftime("%Y-%m-%d"), "end_date": end.strftime("%Y-%m-%d"),
        "hourly": "temperature_2m", "temperature_unit": "fahrenheit", "timezone": "GMT",
    }, timeout=60)
    h = data.get("hourly", {})
    off = std_offset(cfg["tz"])
    daily = {}
    for ts, tp in zip(h.get("time", []), h.get("temperature_2m", [])):
        if tp is None:
            continue
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
        d = (dt + off).strftime("%Y-%m-%d")
        hi, lo = daily.get(d, (-999, 999))
        daily[d] = (max(hi, tp), min(lo, tp))
    return daily

def cmd_calibrate(args):
    codes = args.only if args.only else list(STATIONS.keys())
    results = {}
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(365 * args.years))
    for code in codes:
        cfg = STATIONS[code]
        print(f"Calibrating {code} ({cfg['settle']}) ...", flush=True)
        try:
            actual = iem_tmpf_daily(cfg, start, end)
            fcst = fetch_om_forecasts(cfg, start, end)
            eh = {s: [] for s in ("DJF","MAM","JJA","SON")}
            el = {s: [] for s in ("DJF","MAM","JJA","SON")}
            for d, (ahi, alo) in actual.items():
                if d not in fcst:
                    continue
                fhi, flo = fcst[d]
                s = season_of(d)
                eh[s].append(ahi - fhi); el[s].append(alo - flo)
            sig = lambda e: round(statistics.pstdev(e), 2) if len(e) > 5 else None
            out = {"high": {}, "low": {}, "n": {}}
            for s in ("DJF","MAM","JJA","SON"):
                out["high"][s] = sig(eh[s]); out["low"][s] = sig(el[s])
                out["n"][s] = min(len(eh[s]), len(el[s]))
            out["high"]["ALL"] = sig([x for v in eh.values() for x in v])
            out["low"]["ALL"] = sig([x for v in el.values() for x in v])
            results[code] = out
            print(f"   sigma_high(ALL)={out['high']['ALL']}  sigma_low(ALL)={out['low']['ALL']}")
        except Exception as e:
            print(f"   FAILED: {e}")
    with open(SETTINGS["sigmas_file"], "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nWrote {SETTINGS['sigmas_file']} for {len(results)} stations.")

# ===========================================================================
# COMMAND: scan  — daily ranking + best bet + Kalshi edges
# ===========================================================================
def load_sigmas():
    p = SETTINGS["sigmas_file"]
    return json.load(open(p)) if os.path.exists(p) else {}

def climo_sigma(sigmas, code, side, season):
    default = 2.5 if side == "high" else 2.2
    d = sigmas.get(code, {}).get(side, {})
    return d.get(season) or d.get("ALL") or default

_grid = {}
def nws_hourly_lst(cfg):
    key = (round(cfg["lat"],4), round(cfg["lon"],4))
    if key not in _grid:
        pt = http_json(f"{NWS}/points/{cfg['lat']},{cfg['lon']}")
        _grid[key] = pt["properties"]["forecastHourly"]
    data = http_json(_grid[key])
    off = std_offset(cfg["tz"])
    daily = {}
    for p in data["properties"]["periods"]:
        dt = datetime.fromisoformat(p["startTime"]).astimezone(timezone.utc)
        d = (dt + off).strftime("%Y-%m-%d")
        t = float(p["temperature"])
        hi, lo = daily.get(d, (-999, 999))
        daily[d] = (max(hi, t), min(lo, t))
    return daily

def ensemble_lst(cfg, target_date):
    off = std_offset(cfg["tz"])
    means_hi, means_lo, weights, pooled_hi, pooled_lo = [], [], [], [], []
    for model, w in ENSEMBLE_MODELS.items():
        try:
            data = http_json(OM_ENS, params={
                "latitude": cfg["lat"], "longitude": cfg["lon"],
                "hourly": "temperature_2m", "models": model,
                "temperature_unit": "fahrenheit", "timezone": "GMT", "forecast_days": 5,
            }, pause=0.2)
        except Exception:
            continue
        h = data.get("hourly", {})
        idx = {}
        for i, ts in enumerate(h.get("time", [])):
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
            idx.setdefault((dt + off).strftime("%Y-%m-%d"), []).append(i)
        rows = idx.get(target_date, [])
        if not rows:
            continue
        m_hi, m_lo = [], []
        for k, vals in h.items():
            if not k.startswith("temperature_2m"):
                continue
            day = [vals[i] for i in rows if vals[i] is not None]
            if day:
                m_hi.append(max(day)); m_lo.append(min(day))
        if m_hi:
            means_hi.append(statistics.mean(m_hi)); means_lo.append(statistics.mean(m_lo))
            weights.append(w); pooled_hi += m_hi; pooled_lo += m_lo
    if not means_hi:
        return None
    wmean = lambda v: sum(a*b for a, b in zip(v, weights)) / sum(weights)
    return dict(hi_center=wmean(means_hi), lo_center=wmean(means_lo),
                hi_spread=statistics.pstdev(pooled_hi) if len(pooled_hi) > 1 else None,
                lo_spread=statistics.pstdev(pooled_lo) if len(pooled_lo) > 1 else None)

def nws_alerts(cfg):
    try:
        d = http_json(f"{NWS}/alerts/active", params={"point": f"{cfg['lat']},{cfg['lon']}"})
        return len(d.get("features", [])) > 0
    except Exception:
        return False

def afd_analyze(cfg):
    try:
        idx = http_json(f"{NWS}/products/types/AFD/locations/{cfg['wfo']}")
        items = idx.get("@graph") or idx.get("features") or []
        if not items:
            return 1.0, None
        pid = items[0].get("id") or items[0].get("@id", "").split("/")[-1]
        text = (http_json(f"{NWS}/products/{pid}").get("productText") or "").lower()
    except Exception:
        return 1.0, None
    mult = 1.0
    if any(k in text for k in AFD_CONFIDENT): mult *= 1.05
    if any(k in text for k in AFD_UNCERTAIN): mult *= 0.85
    for name, kws in AFD_PATTERNS.items():
        if any(k in text for k in kws):
            return mult, name
    return mult, None

def pattern_side_mult(pattern, code, side):
    if pattern is None:
        return 1.0
    if pattern == "frontal":
        return 0.80
    if pattern == "convective":
        return 0.85 if side == "high" else 0.92
    if pattern == "seabreeze":
        coastal = code in ("LAX","SFO","MIA","BOS","SEA","SAT")
        return (0.95 if side == "low" else 0.82) if coastal else 0.95
    if pattern == "wind":
        return 0.85
    return 1.0

def blend(nws_v, ens_center):
    base = SETTINGS["nws_weight_base"]
    disagree = abs(nws_v - ens_center)
    if disagree < SETTINGS["disagree_soft"]:
        w = base - 0.15
    elif disagree > SETTINGS["disagree_hard"]:
        w = 0.5
    else:
        w = base
    center = w * nws_v + (1 - w) * ens_center
    agree_mult = 1.0 / (1.0 + disagree / SETTINGS["disagree_hard"])
    return center, disagree, agree_mult

def eff_sigma(climo, ens_spread):
    floor = SETTINGS["sigma_floor"]
    if ens_spread is None:
        return max(floor, climo)
    return max(floor, 0.6 * climo + 0.4 * ens_spread)

def score_city(code, cfg, target, bucket, sigmas, use_afd=True):
    nws = nws_hourly_lst(cfg)
    if target not in nws:
        return None
    nws_hi, nws_lo = nws[target]
    ens = ensemble_lst(cfg, target)
    ens_hi_c = ens["hi_center"] if ens else nws_hi
    ens_lo_c = ens["lo_center"] if ens else nws_lo
    ens_hi_s = ens["hi_spread"] if ens else None
    ens_lo_s = ens["lo_spread"] if ens else None
    
    # Wethr confirmed High/Low so far (same-day only) -> hard clamp on the forecast
    w_hi, w_lo = wethr_confirmed(cfg["settle"])
    lst_today = (datetime.now(timezone.utc) + std_offset(cfg["tz"])).strftime("%Y-%m-%d")
    is_today = (target == lst_today)

    season = season_of(target)
    alert = nws_alerts(cfg)
    alert_mult = SETTINGS["alert_multiplier"] if alert else 1.0
    afd_mult, pattern = afd_analyze(cfg) if use_afd else (1.0, None)
    half = bucket / 2.0

    def one(side, nws_v, ens_c, ens_s, wconf=None):
        center, disagree, agree_mult = blend(nws_v, ens_c)
        # Same-day clamp: today's high can't finish below a temp already reached,
        # and today's low can't finish above a temp already reached.
        clamped = False
        if is_today and wconf is not None:
            if side == "high" and wconf > center:
                center, clamped = wconf, True
            elif side == "low" and wconf < center:
                center, clamped = wconf, True
        sigma = eff_sigma(climo_sigma(sigmas, code, side, season), ens_s)
        point = int(round(center))
        p_bucket = interval_prob(point - half, point + half, center, sigma)
        conf = (p_bucket * alert_mult * afd_mult * agree_mult
                * pattern_side_mult(pattern, code, side))
        return dict(side=side.upper(), center=round(center,1), sigma=round(sigma,2),
                    point=point, p_bucket=p_bucket, conf=min(conf, 0.99),
                    nws=round(nws_v,1), ens=round(ens_c,1), disagree=round(disagree,1),
                    wethr=round(wconf,1) if wconf is not None else None, clamped=clamped)

    hi = one("high", nws_hi, ens_hi_c, ens_hi_s, w_hi)
    lo = one("low",  nws_lo, ens_lo_c, ens_lo_s, w_lo)
    best = hi if hi["conf"] >= lo["conf"] else lo
    return dict(code=code, settle=cfg["settle"], target=target, alert=alert,
                pattern=pattern, best=best, hi=hi, lo=lo)

def kalshi_brackets(series_ticker):
    out = []
    try:
        data = http_json(f"{KALSHI}/markets",
                         params={"series_ticker": series_ticker, "status": "open"})
    except Exception:
        return out
    for m in data.get("markets", []):
        ask = m.get("yes_ask")
        if ask is None:
            continue
        out.append(dict(floor=m.get("floor_strike"), cap=m.get("cap_strike"),
                        ask=ask/100.0, bid=(m.get("yes_bid") or 0)/100.0,
                        ticker=m.get("ticker",""), sub=m.get("subtitle","")))
    return out

def bracket_interval(b):
    lo = b["floor"] - 0.5 if b["floor"] is not None else -1e9
    hi = b["cap"] + 0.5 if b["cap"] is not None else 1e9
    return lo, hi

def find_edges(pick, cfg):
    side = pick["best"]["side"]
    series = "KXHIGH" + cfg["k_high"] if side == "HIGH" else "KXLOW" + cfg["k_low"]
    brackets = kalshi_brackets(series)
    if not brackets:
        return series, None, []
    center, sigma = pick["best"]["center"], pick["best"]["sigma"]
    edges = []
    for b in brackets:
        if b["ask"] <= 0.02 or b["ask"] >= 0.98:
            continue
        lo, hi = bracket_interval(b)
        p = interval_prob(lo, hi, center, sigma)
        edge = p - b["ask"]
        f_full = (p - b["ask"]) / (1 - b["ask"]) if b["ask"] < 1 else 0
        stake = max(0.0, SETTINGS["kelly_fraction"] * f_full) * SETTINGS["bankroll"]
        contracts = int(stake / b["ask"]) if b["ask"] > 0 else 0
        edges.append(dict(b=b, p=p, edge=edge, contracts=contracts))
    edges.sort(key=lambda e: e["edge"], reverse=True)
    pt = pick["best"]["point"]
    aligned = next((e for e in edges
                    if bracket_interval(e["b"])[0] <= pt < bracket_interval(e["b"])[1]), None)
    return series, aligned, [e for e in edges if e["edge"] >= SETTINGS["edge_threshold"]]

def log_prediction(pick, series, aligned):
    row = dict(ts=datetime.now(timezone.utc).isoformat(), date=pick["target"],
               city=pick["code"], station=pick["settle"], side=pick["best"]["side"],
               point=pick["best"]["point"], center=pick["best"]["center"],
               sigma=pick["best"]["sigma"], confidence=round(pick["best"]["conf"],4),
               pattern=pick["pattern"] or "", alert=int(pick["alert"]), series=series,
               model_p=round(aligned["p"],4) if aligned else "",
               ask=round(aligned["b"]["ask"],3) if aligned else "",
               edge=round(aligned["edge"],4) if aligned else "")
    p = SETTINGS["predictions_log"]
    new = not os.path.exists(p)
    with open(p, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new: w.writeheader()
        w.writerow(row)

def cmd_scan(args):
    sigmas = load_sigmas()
    if not sigmas:
        print("(!) No sigmas.json — using default priors. Run `calibrate` for real accuracy.\n")
    now = datetime.now(timezone.utc)
    picks = []
    for code, cfg in STATIONS.items():
        target = (now + timedelta(days=args.offset) + std_offset(cfg["tz"])).strftime("%Y-%m-%d")
        try:
            r = score_city(code, cfg, target, args.bucket, sigmas, use_afd=not args.no_afd)
            if r:
                picks.append(r)
                b = r["best"]
                print(f"  {code:<4} {r['settle']:<5} {b['side']:<4} {b['point']:>3}F "
                      f"conf {b['conf']*100:5.1f}%  sig {b['sigma']:.1f}"
                      f"{'  ['+r['pattern']+']' if r['pattern'] else ''}"
                      f"{'  ALERT' if r['alert'] else ''}")
        except Exception as e:
            print(f"  {code:<4} skipped: {e}")
    if not picks:
        print("\nNo results (check network / config).")
        return
    picks.sort(key=lambda r: r["best"]["conf"], reverse=True)

    # Expand to BOTH sides -> 40 ranked rows (each city's high AND low)
    rows = []
    for r in picks:
        for sd in ("hi", "lo"):
            rows.append(dict(code=r["code"], settle=r["settle"], b=r[sd],
                             pattern=r["pattern"], alert=r["alert"]))
    rows.sort(key=lambda x: x["b"]["conf"], reverse=True)

    print("\n" + "="*78)
    print(f"{'RK':<3}{'CITY':<6}{'STN':<6}{'SIDE':<5}{'EST':<5}{'CONF':<8}{'SIG':<6}{'NOTE'}")
    print("-"*78)
    for i, x in enumerate(rows, 1):
        b = x["b"]; note = []
        if x["pattern"]: note.append(x["pattern"])
        if x["alert"]: note.append("ALERT")
        if b["disagree"] > 2.5: note.append("split")
        if b.get("clamped"): note.append("wethr-clamp")
        print(f"{i:<3}{x['code']:<6}{x['settle']:<6}{b['side']:<5}{b['point']:<5}"
              f"{b['conf']*100:5.1f}%  {b['sigma']:<5.1f} {','.join(note)}")
    print("="*78)

    for r in picks[:args.top]:
        b = r["best"]
        lo_b = b["point"] - (1 if args.bucket == 2 else 0)
        rng = f"{b['point']}F" if args.bucket == 1 else f"{lo_b}-{b['point']}F"
        print(f"\n>>> {r['code']} ({r['settle']})  BET THE {b['side']}")
        print(f"    estimate {b['point']}F  |  bracket {rng}  |  conf {b['conf']*100:.1f}%"
              f"  |  sigma {b['sigma']:.1f}F")
        print(f"    NWS {b['nws']:.0f} vs ensemble {b['ens']:.1f} (spread {b['disagree']:.1f})"
              f"{'  pattern='+r['pattern'] if r['pattern'] else ''}")
        series, aligned = None, None
        if not args.no_kalshi:
            series, aligned, plus_ev = find_edges(r, STATIONS[r["code"]])
            if aligned:
                a = aligned
                verdict = "  <-- +EV BUY" if a["edge"] >= SETTINGS["edge_threshold"] else ""
                print(f"    Kalshi {series}: your P={a['p']*100:.0f}%  ask={a['b']['ask']*100:.0f}c"
                      f"  edge={a['edge']*100:+.0f}%{verdict}")
                if a["edge"] >= SETTINGS["edge_threshold"] and a["contracts"] > 0:
                    print(f"    Size (1/4-Kelly, ${SETTINGS['bankroll']:.0f} bank): "
                          f"~{a['contracts']} contracts")
            elif not aligned:
                print(f"    Kalshi {series}: no open bracket matched (verify suffix via `discover`).")
            if plus_ev:
                print("    Other +EV brackets:")
                for e in plus_ev[:3]:
                    bb = e["b"]; label = bb["sub"] or f"{bb['floor']}-{bb['cap']}"
                    print(f"       {label:<14} P={e['p']*100:.0f}% ask={bb['ask']*100:.0f}c "
                          f"edge={e['edge']*100:+.0f}%  ~{e['contracts']} contracts")
        log_prediction(r, series or "", aligned)
    print(f"\nLogged {min(args.top,len(picks))} pick(s) to {SETTINGS['predictions_log']}.\n")

# ===========================================================================
# COMMAND: backtest  — score logged picks vs actuals
# ===========================================================================
def cmd_backtest(args):
    half = args.bucket / 2.0
    log = SETTINGS["predictions_log"]
    if not os.path.exists(log):
        print(f"No {log} yet — run `scan` first.")
        return
    rows = list(csv.DictReader(open(log)))
    today = datetime.now(timezone.utc).date()
    settled = []
    for row in rows:
        try:
            d = datetime.strptime(row["date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        if d >= today:
            continue
        cfg = STATIONS.get(row["city"])
        if not cfg:
            continue
        d0 = datetime.strptime(row["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        try:
            daily = iem_tmpf_daily(cfg, d0 - timedelta(days=1), d0 + timedelta(days=2))
        except Exception as e:
            print(f"  {row['city']} {row['date']} fetch failed: {e}")
            continue
        if row["date"] not in daily:
            continue
        ahi, alo = daily[row["date"]]
        actual = ahi if row["side"] == "HIGH" else alo
        point = int(row["point"])
        hit = (point - half) <= round(actual) <= (point + half)
        pnl = None
        if row.get("ask") not in ("", None):
            try:
                ask = float(row["ask"]); pnl = (1.0 - ask) if hit else (-ask)
            except ValueError:
                pass
        settled.append(dict(city=row["city"], date=row["date"], hit=hit,
                            conf=float(row["confidence"]), pnl=pnl,
                            pattern=row.get("pattern","")))
    if not settled:
        print("No fully-settled predictions to score yet.")
        return
    n = len(settled); hits = sum(s["hit"] for s in settled)
    avg_conf = sum(s["conf"] for s in settled)/n
    print("\n" + "="*66)
    print(f"SETTLED: {n}   Hit rate: {hits}/{n} = {hits/n*100:.1f}%")
    print(f"Avg stated conf: {avg_conf*100:.1f}%   "
          f"({'calibrated' if abs(avg_conf-hits/n)<0.08 else 'MISCALIBRATED'})")
    pnls = [s["pnl"] for s in settled if s["pnl"] is not None]
    if pnls:
        print(f"Realized P&L: {sum(pnls):+.2f}/contract over {len(pnls)} trades "
              f"({sum(pnls)/len(pnls)*100:+.1f}c avg)")
    print("="*66)
    bands = defaultdict(list)
    for s in settled:
        bands[int(s["conf"]*10)*10].append(s["hit"])
    print("\nCalibration (stated band -> actual hit rate):")
    for b in sorted(bands):
        v = bands[b]; print(f"  {b:>3}-{b+9}%: {sum(v)}/{len(v)} = {sum(v)/len(v)*100:.0f}%")
    bycity = defaultdict(list)
    for s in settled:
        bycity[s["city"]].append(s["hit"])
    print("\nPer-city hit rate:")
    for c in sorted(bycity, key=lambda c: -sum(bycity[c])/len(bycity[c])):
        v = bycity[c]; print(f"  {c:<5} {sum(v)}/{len(v)} = {sum(v)/len(v)*100:.0f}%")
    bypat = defaultdict(list)
    for s in settled:
        bypat[s["pattern"] or "clear"].append(s["hit"])
    print("\nHit rate by AFD pattern:")
    for p in sorted(bypat, key=lambda p: -sum(bypat[p])/len(bypat[p])):
        v = bypat[p]; print(f"  {p:<11} {sum(v)}/{len(v)} = {sum(v)/len(v)*100:.0f}%")
    print()

# ===========================================================================
# COMMAND: histtest  — accuracy of the forecast vs 1-2 years of actual temps
# ===========================================================================
def cmd_histtest(args):
    codes = args.only if args.only else list(STATIONS.keys())
    half = args.bucket / 2.0
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(365 * args.years))
    print(f"\nHistorical accuracy test — {args.years}y, {args.bucket}F bucket")
    print("(Open-Meteo archived forecast vs IEM actual settlement temps)\n")
    tot = dict(hh=0, hn=0, hae=0.0, lh=0, ln=0, lae=0.0)
    per = []
    for code in codes:
        cfg = STATIONS[code]
        print(f"  testing {code} ({cfg['settle']}) ...", flush=True)
        try:
            actual = iem_tmpf_daily(cfg, start, end)
            fcst = fetch_om_forecasts(cfg, start, end)
        except Exception as e:
            print(f"     failed: {e}"); continue
        hh = hn = lh = ln = 0; hae = lae = 0.0
        for d, (ahi, alo) in actual.items():
            if d not in fcst:
                continue
            fhi, flo = fcst[d]
            hn += 1; hae += abs(ahi - fhi)
            if (round(fhi) - half) <= round(ahi) <= (round(fhi) + half):
                hh += 1
            ln += 1; lae += abs(alo - flo)
            if (round(flo) - half) <= round(alo) <= (round(flo) + half):
                lh += 1
        if hn == 0:
            print("     no overlapping data"); continue
        per.append(dict(code=code, hi=hh/hn, lo=lh/ln, hmae=hae/hn, lmae=lae/ln, n=hn))
        for k, v in (("hh",hh),("hn",hn),("hae",hae),("lh",lh),("ln",ln),("lae",lae)):
            tot[k] += v
        print(f"     HIGH {hh/hn*100:.0f}% hit (MAE {hae/hn:.1f}F)   "
              f"LOW {lh/ln*100:.0f}% hit (MAE {lae/ln:.1f}F)   n={hn}")
    if not per:
        print("\nNo data."); return
    per.sort(key=lambda x: (x["hi"] + x["lo"]) / 2, reverse=True)
    print("\n" + "="*72)
    print(f"{'CITY':<6}{'STN':<6}{'HIGH-hit':<10}{'H-MAE':<8}{'LOW-hit':<10}{'L-MAE':<8}{'days'}")
    print("-"*72)
    for x in per:
        print(f"{x['code']:<6}{STATIONS[x['code']]['settle']:<6}"
              f"{x['hi']*100:>6.0f}%   {x['hmae']:<7.1f}{x['lo']*100:>6.0f}%   "
              f"{x['lmae']:<7.1f}{x['n']}")
    print("-"*72)
    print(f"OVERALL HIGH: {tot['hh']/tot['hn']*100:.1f}% hit, MAE {tot['hae']/tot['hn']:.1f}F")
    print(f"OVERALL LOW:  {tot['lh']/tot['ln']*100:.1f}% hit, MAE {tot['lae']/tot['ln']:.1f}F")
    print("="*72)
    print("Higher hit% and lower MAE = easier to predict. This is the forecast's raw")
    print("accuracy (the bot's realistic ceiling before Kalshi pricing).\n")

# ===========================================================================
# CLI
# ===========================================================================
def build_parser():
    p = argparse.ArgumentParser(
        prog="kalshi_bot.py",
        description="All-in-one Kalshi temperature trading bot.")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("scan", help="daily ranking + best bet + Kalshi edges")
    s.add_argument("--offset", type=int, default=0, help="0=today, 1=tomorrow, ...")
    s.add_argument("--bucket", type=int, default=2, choices=[1, 2], help="bracket width (F)")
    s.add_argument("--top", type=int, default=1, help="how many detailed picks to show")
    s.add_argument("--no-kalshi", action="store_true", help="skip live prices (forecast only)")
    s.add_argument("--no-afd", action="store_true", help="skip Area Forecast Discussion parsing")
    s.set_defaults(func=cmd_scan)

    c = sub.add_parser("calibrate", help="measure real per-station sigmas -> sigmas.json")
    c.add_argument("--years", type=float, default=2.0, help="years of history to use")
    c.add_argument("--only", nargs="*", default=None, help="subset of city codes")
    c.set_defaults(func=cmd_calibrate)

    d = sub.add_parser("discover", help="list real Kalshi series tickers")
    d.set_defaults(func=cmd_discover)

    b = sub.add_parser("backtest", help="score logged picks vs actuals")
    b.add_argument("--bucket", type=int, default=2, choices=[1, 2])
    b.set_defaults(func=cmd_backtest)

    h = sub.add_parser("histtest", help="test forecast accuracy vs 1-2yr of actual temps")
    h.add_argument("--years", type=float, default=1.0, help="years of history (1 or 2)")
    h.add_argument("--bucket", type=int, default=2, choices=[1, 2])
    h.add_argument("--only", nargs="*", default=None, help="subset of city codes")
    h.set_defaults(func=cmd_histtest)
    return p

def main():
    parser = build_parser()
    known = {"scan", "calibrate", "discover", "backtest", "histtest", "-h", "--help"}
    argv = sys.argv[1:]
    if not argv or argv[0] not in known:   # default to `scan`
        argv = ["scan"] + argv
    args = parser.parse_args(argv)
    args.func(args)

if __name__ == "__main__":
    main()

# ===========================================================================
# COMMAND REFERENCE
# ===========================================================================
# python kalshi_bot.py discover
#     List every live Kalshi KXHIGH/KXLOW series so you can paste the correct
#     k_high/k_low suffixes into STATIONS. Run this FIRST.
#
# python kalshi_bot.py calibrate --years 2
# python kalshi_bot.py calibrate --years 3 --only LAX SFO SAT MIA PHX
#     Build sigmas.json from real IEM actuals vs Open-Meteo archived forecasts.
#     Slow (IEM throttles). Re-run every few months.
#
# python kalshi_bot.py                      (same as `scan`)
# python kalshi_bot.py scan
# python kalshi_bot.py scan --top 5
# python kalshi_bot.py scan --offset 1
# python kalshi_bot.py scan --bucket 1
# python kalshi_bot.py scan --no-kalshi
# python kalshi_bot.py scan --no-afd
#     Rank all 20 cities by confidence, show the best bet(s), compare to live
#     Kalshi prices for +EV, size with quarter-Kelly, and log to predictions.csv.
#       --offset N    day to trade (0 today, 1 tomorrow)
#       --bucket 1|2  score on 1F or Kalshi's 2F brackets (default 2)
#       --top N       show N detailed picks + their edges (default 1)
#       --no-kalshi   forecast ranking only, skip market prices
#       --no-afd      skip the Area Forecast Discussion pattern/confidence step
#
# python kalshi_bot.py backtest
# python kalshi_bot.py backtest --bucket 1
#     Settle logged picks against IEM actuals; print hit rate, confidence
#     calibration, realized P&L, and per-city / per-pattern breakdowns.
#
# python kalshi_bot.py -h        (help for any command: `scan -h`, etc.)
