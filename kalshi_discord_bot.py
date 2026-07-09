#!/usr/bin/env python3
"""
kalshi_discord_bot.py — Discord front-end for kalshi_bot.py
===========================================================
Keep this file in the SAME FOLDER as kalshi_bot.py. It imports that file as the
engine and exposes everything as simple slash commands:

  /pick              the single best bet right now
  /scan              full 20-city ranking + top pick
  /city   code:LAX   detailed forecast + edge for one city
  /backtest          accuracy / calibration of your logged picks
  /calibrate         (slow) rebuild sigmas.json in the background
  /help              list commands

------------------------------------------------------------------------------
ONE-TIME SETUP
------------------------------------------------------------------------------
1. Create a bot:
     https://discord.com/developers/applications  ->  New Application
     -> Bot -> Reset Token -> copy the token
2. Invite it to your server (OAuth2 -> URL Generator):
     scopes:  bot, applications.commands
     bot permissions:  Send Messages, Embed Links
     open the generated URL and add it to your server.
3. Install libs:
     pip install -U discord.py requests
4. Set your token (Mac/Linux):
     export DISCORD_TOKEN="your-token-here"
     export GUILD_ID="your-server-id"   # optional: instant command sync
5. Run:
     python kalshi_discord_bot.py

(No GUILD_ID -> commands sync globally and can take up to ~1 hour the first
time. With GUILD_ID they appear instantly in that one server.)
------------------------------------------------------------------------------
"""
import asyncio
import functools
import os
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

import kalshi_bot as kb   # the engine (must be in the same folder)
kb.WETHR_KEY = os.environ.get("WETHR_API_KEY")  # propagate Wethr key to the engine

TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")
WETHR_API_KEY = os.environ.get("WETHR_API_KEY")  # optional Wethr integration
CACHE_TTL = 900  # seconds; reuse a scan for 15 min so repeat commands are instant

# ---------------------------------------------------------------------------
# Engine wrappers (blocking — run inside an executor so Discord stays responsive)
# ---------------------------------------------------------------------------
_cache = {}

def perform_scan(offset, bucket, use_afd, use_kalshi, detail_n):
    """Score all cities, return (picks_sorted, details_by_code). Logs detailed picks."""
    key = (offset, bucket, use_afd, use_kalshi, detail_n)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1], hit[2]

    sigmas = kb.load_sigmas()
    now = datetime.now(timezone.utc)
    picks = []
    for code, cfg in kb.STATIONS.items():
        target = (now + timedelta(days=offset) + kb.std_offset(cfg["tz"])).strftime("%Y-%m-%d")
        try:
            r = kb.score_city(code, cfg, target, bucket, sigmas, use_afd=use_afd)
            if r:
                picks.append(r)
        except Exception:
            continue
    picks.sort(key=lambda r: r["best"]["conf"], reverse=True)

    details = {}
    for r in picks[:detail_n]:
        series, aligned, plus_ev = (None, None, [])
        if use_kalshi:
            try:
                series, aligned, plus_ev = kb.find_edges(r, kb.STATIONS[r["code"]])
            except Exception:
                pass
        details[r["code"]] = (series, aligned, plus_ev)
        try:
            kb.log_prediction(r, series or "", aligned)
        except Exception:
            pass

    _cache[key] = (time.time(), picks, details)
    return picks, details

def perform_backtest(bucket):
    """Run the engine's backtest but capture its printed output as text."""
    import io, contextlib
    buf = io.StringIO()

    class A:  # mimic argparse namespace
        pass
    a = A(); a.bucket = bucket
    with contextlib.redirect_stdout(buf):
        kb.cmd_backtest(a)
    return buf.getvalue() or "No settled predictions yet."

def perform_calibrate(years, only):
    import io, contextlib
    buf = io.StringIO()

    class A:
        pass
    a = A(); a.years = years; a.only = only
    with contextlib.redirect_stdout(buf):
        kb.cmd_calibrate(a)
    return buf.getvalue()

def perform_histtest(years, bucket, only):
    import io, contextlib
    buf = io.StringIO()

    class A:
        pass
    a = A(); a.years = years; a.bucket = bucket; a.only = only
    with contextlib.redirect_stdout(buf):
        kb.cmd_histtest(a)
    return buf.getvalue()

def chunk_text(text, n=1900):
    """Split long text into <2000-char blocks on line boundaries for Discord."""
    out, buf = [], ""
    for ln in text.splitlines():
        if len(buf) + len(ln) + 1 > n:
            out.append(buf); buf = ""
        buf += ln + "\n"
    if buf.strip():
        out.append(buf)
    return out

async def run_blocking(fn, *a):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *a))

# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def edge_line(details, code):
    series, aligned, plus_ev = details.get(code, (None, None, []))
    if not series:
        return None
    if not aligned:
        return f"`{series}` — no matching open bracket (check suffix via CLI `discover`)."
    a = aligned
    tag = "  ✅ **+EV BUY**" if a["edge"] >= kb.SETTINGS["edge_threshold"] else ""
    line = (f"`{series}`  model **{a['p']*100:.0f}%** vs ask **{a['b']['ask']*100:.0f}¢**  "
            f"edge **{a['edge']*100:+.0f}%**{tag}")
    if a["edge"] >= kb.SETTINGS["edge_threshold"] and a["contracts"] > 0:
        line += f"\nSize (¼-Kelly, ${kb.SETTINGS['bankroll']:.0f}): ~**{a['contracts']}** contracts"
    return line

def pick_embed(r, details, bucket, day_label):
    b = r["best"]
    lo_b = b["point"] - (1 if bucket == 2 else 0)
    rng = f"{b['point']}°F" if bucket == 1 else f"{lo_b}–{b['point']}°F"
    color = 0xE23B3B if b["side"] == "HIGH" else 0x3B82E2
    e = discord.Embed(
        title=f"🌡️ {r['code']} ({r['settle']}) — bet the {b['side']}",
        description=f"**{day_label}**",
        color=color, timestamp=datetime.now(timezone.utc))
    e.add_field(name="Estimate", value=f"**{b['point']}°F**  (bracket {rng})", inline=True)
    e.add_field(name="Confidence", value=f"**{b['conf']*100:.1f}%**", inline=True)
    e.add_field(name="σ (spread)", value=f"{b['sigma']:.1f}°F", inline=True)
    model_str = f"NWS {b['nws']:.0f} vs ensemble {b['ens']:.1f} (Δ{b['disagree']:.1f})"
    if b.get("wethr") is not None:
        model_str += f"\nWethr confirmed so far: **{b['wethr']:.0f}°F**"
    e.add_field(name="Model check", value=model_str, inline=False)
    notes = []
    if b.get("clamped"):
        notes.append("🔒 **Wethr clamp** — estimate pinned to a temp already reached today")
    if r["pattern"]:
        notes.append(f"⚠️ pattern: **{r['pattern']}**")
    if r["alert"]:
        notes.append("⚠️ **active NWS alert**")
    if b["disagree"] > 2.5:
        notes.append("⚠️ model split")
    if notes:
        e.add_field(name="Flags", value="\n".join(notes), inline=False)
    el = edge_line(details, r["code"])
    if el:
        e.add_field(name="Kalshi", value=el, inline=False)
    e.set_footer(text="Not financial advice · verify settlement station before trading")
    return e

def scan_embed(picks, bucket, day_label):
    # Expand to BOTH sides -> 40 ranked rows (each city's high AND low)
    rows = []
    for r in picks:
        for sd in ("hi", "lo"):
            rows.append((r, r[sd]))
    rows.sort(key=lambda x: x[1]["conf"], reverse=True)
    header = f"{'#':<3}{'CITY':<5}{'STN':<5}{'SD':<3}{'EST':<5}{'CONF':<6}{'NOTE'}"
    lines = []
    for i, (r, b) in enumerate(rows, 1):
        note = []
        if r["pattern"]: note.append(r["pattern"][:4])
        if r["alert"]: note.append("alert")
        if b.get("clamped"): note.append("wclmp")
        lines.append(f"{i:<3}{r['code']:<5}{r['settle']:<5}{b['side'][:1]:<3}"
                     f"{b['point']:<5}{b['conf']*100:4.0f}% {','.join(note)}")
    # split into two columns of ~20 to stay well under embed limits
    mid = (len(lines) + 1) // 2
    col1 = "```\n" + header + "\n" + "\n".join(lines[:mid]) + "\n```"
    col2 = "```\n" + header + "\n" + "\n".join(lines[mid:]) + "\n```"
    e = discord.Embed(title=f"📊 Kalshi temp ranking (40 = highs + lows) — {day_label}",
                      color=0x2ECC71, timestamp=datetime.now(timezone.utc))
    e.add_field(name="Rank 1–20", value=col1, inline=False)
    e.add_field(name=f"Rank {mid+1}–{len(lines)}", value=col2, inline=False)
    e.set_footer(text="Top = highest confidence · /pick for the single best bet")
    return e

# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    try:
        if GUILD_ID:
            g = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=g)
            await bot.tree.sync(guild=g)
        else:
            await bot.tree.sync()
        print(f"Logged in as {bot.user}. Commands synced"
              f"{' to guild '+GUILD_ID if GUILD_ID else ' globally'}.")
    except Exception as e:
        print(f"Sync error: {e}")

def day_label(offset):
    return "today" if offset == 0 else ("tomorrow" if offset == 1 else f"+{offset} days")

@bot.tree.command(name="pick", description="The single best temperature bet")
@app_commands.describe(day="0 = today (default), 1 = tomorrow",
                       bucket="Bracket width in °F (1 or 2, default 2)")
async def pick(interaction: discord.Interaction, day: int = 0, bucket: int = 2):
    await interaction.response.defer(thinking=True)
    bucket = 2 if bucket not in (1, 2) else bucket
    picks, details = await run_blocking(perform_scan, day, bucket, True, True, 1)
    if not picks:
        await interaction.followup.send("No results — check the engine config / network.")
        return
    await interaction.followup.send(embed=pick_embed(picks[0], details, bucket, day_label(day)))

@bot.tree.command(name="scan", description="Full 20-city ranking + top pick")
@app_commands.describe(day="0 = today (default), 1 = tomorrow",
                       bucket="Bracket width in °F (1 or 2, default 2)",
                       top="How many detailed picks to attach (default 1)",
                       kalshi="Include live Kalshi edges (default true)")
async def scan(interaction: discord.Interaction, day: int = 0, bucket: int = 2,
               top: int = 1, kalshi: bool = True):
    await interaction.response.defer(thinking=True)
    bucket = 2 if bucket not in (1, 2) else bucket
    top = max(1, min(top, 5))
    picks, details = await run_blocking(perform_scan, day, bucket, True, kalshi, top)
    if not picks:
        await interaction.followup.send("No results — check the engine config / network.")
        return
    embeds = [scan_embed(picks, bucket, day_label(day))]
    for r in picks[:top]:
        embeds.append(pick_embed(r, details, bucket, day_label(day)))
    await interaction.followup.send(embeds=embeds[:10])

@bot.tree.command(name="city", description="Detailed forecast + edge for one city")
@app_commands.describe(code="City code, e.g. LAX, SAT, NYC",
                       day="0 = today (default), 1 = tomorrow",
                       bucket="Bracket width in °F (1 or 2, default 2)")
async def city(interaction: discord.Interaction, code: str, day: int = 0, bucket: int = 2):
    code = code.upper().strip()
    if code not in kb.STATIONS:
        await interaction.response.send_message(
            f"Unknown city `{code}`. Options: {', '.join(kb.STATIONS)}", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    bucket = 2 if bucket not in (1, 2) else bucket
    picks, details = await run_blocking(perform_scan, day, bucket, True, True, 20)
    r = next((p for p in picks if p["code"] == code), None)
    if not r:
        await interaction.followup.send(f"Couldn't score {code} right now.")
        return
    await interaction.followup.send(embed=pick_embed(r, details, bucket, day_label(day)))

@bot.tree.command(name="backtest", description="Accuracy / calibration of logged picks")
@app_commands.describe(bucket="Bracket width used when scoring (1 or 2, default 2)")
async def backtest(interaction: discord.Interaction, bucket: int = 2):
    await interaction.response.defer(thinking=True)
    bucket = 2 if bucket not in (1, 2) else bucket
    text = await run_blocking(perform_backtest, bucket)
    if len(text) > 1900:
        text = text[:1900] + "\n... (truncated)"
    await interaction.followup.send(f"```\n{text}\n```")

@bot.tree.command(name="calibrate", description="(Slow) rebuild sigmas.json in the background")
@app_commands.describe(years="Years of history (default 2)",
                       only="Space-separated city codes, or blank for all")
async def calibrate(interaction: discord.Interaction, years: float = 2.0, only: str = ""):
    codes = [c.upper() for c in only.split()] if only.strip() else None
    if codes:
        bad = [c for c in codes if c not in kb.STATIONS]
        if bad:
            await interaction.response.send_message(
                f"Unknown code(s): {', '.join(bad)}", ephemeral=True)
            return
    await interaction.response.send_message(
        f"⏳ Calibration started ({'all cities' if not codes else ', '.join(codes)}, "
        f"{years}y). This is slow — I'll post the summary here when it's done.")
    channel = interaction.channel

    async def worker():
        text = await run_blocking(perform_calibrate, years, codes)
        if len(text) > 1900:
            text = text[:1900] + "\n... (truncated)"
        try:
            await channel.send(f"✅ Calibration complete:\n```\n{text}\n```")
        except Exception:
            pass
    bot.loop.create_task(worker())

@bot.tree.command(name="histtest", description="Backtest forecast accuracy vs 1-2 years of actual temps")
@app_commands.describe(city="One city code (e.g. LAX), or '20' for all 20 cities (slow).",
                       years="Years of history: 1 or 2 (default 1)",
                       bucket="Bracket width in degrees F (1 or 2, default 2)")
async def histtest(interaction: discord.Interaction, city: str = "20", years: float = 1.0, bucket: int = 2):
    only = None
    if city != "20":
        c = city.upper().strip()
        if c not in kb.STATIONS:
            await interaction.response.send_message(f"Unknown city: {c}. Use '20' for all cities.", ephemeral=True)
            return
        only = [c]
    bucket = 2 if bucket not in (1, 2) else bucket
    years = 2.0 if years >= 2 else 1.0
    scope = only[0] if only else "ALL 20 cities"
    slow = "" if only else " (5-10 min)"
    await interaction.response.send_message(
        f"Testing {scope}, {years}y, {bucket}F bucket{slow}. Results coming soon...")
    channel = interaction.channel
    async def worker():
        text = await run_blocking(perform_histtest, years, bucket, only)
        for block in chunk_text(text):
            try:
                await channel.send(f"```\n{block}\n```")
            except Exception:
                pass
    bot.loop.create_task(worker())

@bot.tree.command(name="help", description="List commands")
async def help_cmd(interaction: discord.Interaction):
    e = discord.Embed(title="Kalshi temp bot — commands", color=0x9B59B6)
    e.add_field(name="/pick", value="The single best bet (city, side, bracket, edge).", inline=False)
    e.add_field(name="/scan", value="Full ranking of all 40 (each city's high AND low) + top pick(s). Options: day, bucket, top, kalshi.", inline=False)
    e.add_field(name="/city code:LAX", value="Detailed forecast + edge for one city.", inline=False)
    e.add_field(name="/histtest", value="Backtest forecast accuracy vs 1-2yr of real temps (high & low, per city). Options: city, years, bucket.", inline=False)
    e.add_field(name="/backtest", value="Hit rate, calibration and P&L of your logged live picks.", inline=False)
    e.add_field(name="/calibrate", value="Rebuild per-station sigmas (slow, runs in background).", inline=False)
    e.set_footer(text="Common options — day: 0 today / 1 tomorrow · bucket: 1 or 2 °F")
    await interaction.response.send_message(embed=e, ephemeral=True)

def main():
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN first:  export DISCORD_TOKEN='...'")
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
