#!/usr/bin/env python3
"""
StockAdvisor Daily Analysis Routine
BDR/BRF methodology — ranks 20 S&P 500 tickers daily and emails results.
"""

import json
import os
import random
import smtplib
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
ANALYZE_URL = "https://eandhconsulting.com/portal/public/StockAdvisorII/analyze.php"
GIST_API    = "https://api.github.com/gists"
GITHUB_PAT  = os.getenv("GITHUB_GIST_PAT", "")
MEMORY_GIST = os.getenv("MEMORY_GIST_ID",  "8f2743ea81110faa6b83f06e84d690fe")
TICKER_GIST = os.getenv("TICKER_GIST_ID",  "72c802fdaa59b6b735a8c01fb0cb82cb")

SMTP_HOST   = os.getenv("SMTP_HOST", "smtp.hostinger.com")
SMTP_PORT   = int(os.getenv("SMTP_PORT", 465))
SMTP_USER   = os.getenv("SMTP_USER", "ezemach@eandhconsulting.com")
SMTP_PASS   = os.getenv("SMTP_PASS", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "ezumach@gmail.com")

GRADE_ORDER = {"SUPER": 0, "ALFA": 1, "FAIR": 2, "UNDERPERFORM": 3}
BATCH_SIZE  = 10
BATCH_DELAY = 65   # seconds between batches


# ──────────────────────────────────────────────
# PHASE 1: INITIALIZATION
# ──────────────────────────────────────────────

def _gist_headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_PAT}",
        "Accept": "application/vnd.github.v3+json",
    }


def fetch_memory() -> dict:
    """Fetch and parse the BDR memory Gist."""
    resp = requests.get(f"{GIST_API}/{MEMORY_GIST}", headers=_gist_headers(), timeout=15)
    resp.raise_for_status()
    files = resp.json().get("files", {})
    raw = next(iter(files.values()))["content"]
    return json.loads(raw)


def fetch_tickers() -> list[dict]:
    """Fetch the S&P 500 ticker list Gist → [{ticker, sector}, ...]."""
    resp = requests.get(f"{GIST_API}/{TICKER_GIST}", headers=_gist_headers(), timeout=15)
    resp.raise_for_status()
    files = resp.json().get("files", {})
    raw = next(iter(files.values()))["content"]
    return json.loads(raw)


def push_memory(memory: dict) -> None:
    """Write updated memory back to the Gist."""
    payload = {
        "files": {
            "bdr_memory.json": {
                "content": json.dumps(memory, indent=2)
            }
        }
    }
    resp = requests.patch(
        f"{GIST_API}/{MEMORY_GIST}",
        headers=_gist_headers(),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()


# ──────────────────────────────────────────────
# PHASE 2: TICKER SELECTION
# ──────────────────────────────────────────────

def select_tickers(
    all_tickers: list[dict],
    memory: dict,
    manual_override: list[str] | None = None,
    count: int = 20,
) -> list[dict]:
    """
    Select `count` tickers to analyze.
    Manual override → use those tickers verbatim (matched from master list).
    Default → random weighted selection avoiding recently over-sampled sectors.
    """
    if manual_override:
        lookup = {t["ticker"]: t for t in all_tickers}
        selected = []
        for sym in manual_override:
            sym = sym.strip().upper()
            selected.append(lookup.get(sym, {"ticker": sym, "sector": "Unknown"}))
        return selected

    # Build sector weights: sectors with higher SUPER/ALFA historical rate get lower weight
    # (they've been sampled enough; give under-sampled sectors more attention)
    sector_super_rate: dict[str, float] = {}
    for sector, data in memory.get("sectorPatterns", {}).items():
        if data.get("sampleCount", 0) > 0:
            sector_super_rate[sector] = data.get("superAlfaRate", 0.5)

    # Track sectors used in last 7 days via analyzedTickers
    today = date.today()
    recent_sector_counts: dict[str, int] = defaultdict(int)
    for ticker_data in memory.get("analyzedTickers", {}).values():
        try:
            last = datetime.strptime(ticker_data["lastAnalyzed"], "%Y-%m-%d").date()
            if (today - last).days <= 7:
                # We don't store sector per analyzed ticker; skip for now
                pass
        except (KeyError, ValueError):
            pass

    # Weight each ticker: avoid recently analyzed, prefer less-sampled sectors
    recently_analyzed = set()
    for sym, td in memory.get("analyzedTickers", {}).items():
        try:
            last = datetime.strptime(td["lastAnalyzed"], "%Y-%m-%d").date()
            if (today - last).days <= 3:
                recently_analyzed.add(sym)
        except (KeyError, ValueError):
            pass

    pool = [t for t in all_tickers if t["ticker"] not in recently_analyzed]
    if len(pool) < count:
        pool = all_tickers  # fallback: no exclusions

    # Assign weights: sectors with low historical SUPER/ALFA rate get higher weight
    def weight(t: dict) -> float:
        rate = sector_super_rate.get(t["sector"], 0.5)
        return max(0.1, 1.0 - rate)

    weights = [weight(t) for t in pool]
    total = sum(weights)
    probs = [w / total for w in weights]

    k = min(count, len(pool))
    indices = random.choices(range(len(pool)), weights=probs, k=k * 3)
    seen, selected = set(), []
    for i in indices:
        sym = pool[i]["ticker"]
        if sym not in seen:
            seen.add(sym)
            selected.append(pool[i])
        if len(selected) == k:
            break

    return selected


# ──────────────────────────────────────────────
# PHASE 3: API ANALYSIS
# ──────────────────────────────────────────────

def analyze_ticker(ticker: str) -> dict | None:
    """POST to StockAdvisor API. Returns parsed result or None on failure."""
    try:
        resp = requests.post(
            ANALYZE_URL,
            json={"ticker": ticker},
            timeout=30,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        print(f"  [WARN] {ticker} failed: {exc}", file=sys.stderr)
        return None


def run_batch_analysis(tickers: list[dict]) -> list[dict]:
    """
    Analyze all tickers in batches of BATCH_SIZE with BATCH_DELAY between batches.
    Returns list of result dicts: {ticker, sector, raw_response, bdr, brf, grade, price}.
    """
    results = []
    batches = [tickers[i:i + BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]

    for b_idx, batch in enumerate(batches):
        if b_idx > 0:
            print(f"  Waiting {BATCH_DELAY}s before next batch...")
            time.sleep(BATCH_DELAY)

        print(f"  Batch {b_idx + 1}/{len(batches)}: {[t['ticker'] for t in batch]}")
        for t in batch:
            sym = t["ticker"]
            raw = analyze_ticker(sym)
            if raw is None:
                print(f"  [SKIP] {sym} — API call failed")
                continue

            result = _parse_result(t, raw)
            results.append(result)
            print(f"    {sym}: {result['grade']} | BDR {result['bdr_score']} | BRF {result['brf_level']} | ${result['price']}")

    return results


def _parse_result(ticker_info: dict, raw: dict) -> dict:
    """Normalize the raw API response into a flat result dict."""
    bdr  = raw.get("bdr", {})
    brf  = raw.get("brf", {})
    cool = raw.get("cooling", {})

    grade     = (cool.get("finalGrade") or bdr.get("grade") or "UNKNOWN").upper()
    bdr_score = bdr.get("bdrScore") or bdr.get("score") or 0
    brf_level = (brf.get("brfLevel") or brf.get("level") or "Unknown")
    price     = raw.get("currentPrice") or raw.get("price") or 0

    # Normalize BRF level labels
    brf_norm = brf_level
    if isinstance(brf_level, str):
        b = brf_level.upper()
        if "LOW"  in b: brf_norm = "Low"
        elif "MOD" in b: brf_norm = "Moderate"
        elif "HIGH" in b: brf_norm = "High"

    return {
        "ticker":    ticker_info["ticker"],
        "sector":    ticker_info.get("sector", "Unknown"),
        "grade":     grade,
        "bdr_score": float(bdr_score) if bdr_score else 0.0,
        "brf_level": brf_norm,
        "price":     price,
        "raw":       raw,
    }


# ──────────────────────────────────────────────
# PHASE 4: RANKING
# ──────────────────────────────────────────────

def rank_results(results: list[dict]) -> list[dict]:
    """Sort by grade tier (SUPER > ALFA > FAIR > UNDERPERFORM), then BDR score asc."""
    def sort_key(r):
        grade_rank = GRADE_ORDER.get(r["grade"], 99)
        return (grade_rank, r["bdr_score"])

    return sorted(results, key=sort_key)


# ──────────────────────────────────────────────
# PHASE 5 & 6: MEMORY LEARNING + INSIGHTS
# ──────────────────────────────────────────────

def extract_insights(ranked: list[dict], memory: dict) -> dict:
    """
    Compare today's run against memory to surface patterns, anomalies, and confirmations.
    Returns a dict of insight strings for the email.
    """
    insights = {
        "sector_patterns": [],
        "modifier_notes": [],
        "market_notes": [],
        "anomalies": [],
        "confirmations": [],
    }

    today_str = str(date.today())

    # ── Sector SUPER/ALFA rates today ──
    sector_today: dict[str, dict] = defaultdict(lambda: {"total": 0, "super_alfa": 0})
    for r in ranked:
        s = r["sector"]
        sector_today[s]["total"] += 1
        if r["grade"] in ("SUPER", "ALFA"):
            sector_today[s]["super_alfa"] += 1

    sector_patterns = memory.get("sectorPatterns", {})
    for sector, counts in sector_today.items():
        if counts["total"] == 0:
            continue
        today_rate = counts["super_alfa"] / counts["total"]
        hist = sector_patterns.get(sector, {})
        hist_rate = hist.get("superAlfaRate", None)
        hist_n    = hist.get("sampleCount", 0)

        if hist_rate is not None and hist_n >= 5:
            diff = today_rate - hist_rate
            if abs(diff) >= 0.15:
                direction = "↑ higher" if diff > 0 else "↓ lower"
                insights["sector_patterns"].append(
                    f"{sector} SUPER/ALFA rate today {today_rate:.0%} vs historical {hist_rate:.0%} — {direction} than average"
                )
        elif hist_rate is None:
            insights["sector_patterns"].append(
                f"{sector} first-time sector data: {today_rate:.0%} SUPER/ALFA rate ({counts['total']} stocks)"
            )

    # ── Anomaly detection: high-confidence sectors producing opposite grades ──
    top_picks_hist = {t["ticker"]: t for t in memory.get("topPicksHistory", [])}
    for r in ranked:
        sym = r["ticker"]
        if sym in top_picks_hist:
            prev_grade = top_picks_hist[sym].get("grade", "")
            if prev_grade in ("SUPER", "ALFA") and r["grade"] in ("FAIR", "UNDERPERFORM"):
                insights["anomalies"].append(
                    f"{sym} was {prev_grade} on {top_picks_hist[sym].get('date','?')} but is now {r['grade']} — notable downgrade"
                )
            elif prev_grade in ("FAIR", "UNDERPERFORM") and r["grade"] in ("SUPER", "ALFA"):
                insights["confirmations"].append(
                    f"{sym} upgraded from {prev_grade} to {r['grade']} — potential turnaround"
                )
            elif prev_grade == r["grade"] and r["grade"] in ("SUPER", "ALFA"):
                insights["confirmations"].append(
                    f"{sym} re-confirmed as {r['grade']} (consistent top performer)"
                )

    # ── BRF/BDR modifier insight ──
    low_brf_super = [r for r in ranked if r["brf_level"] == "Low" and r["grade"] in ("SUPER", "ALFA")]
    if low_brf_super:
        insights["modifier_notes"].append(
            f"{len(low_brf_super)} stock(s) match Low BRF + SUPER/ALFA signal: "
            + ", ".join(r["ticker"] for r in low_brf_super[:3])
        )

    high_brf_super = [r for r in ranked if r["brf_level"] == "High" and r["grade"] in ("SUPER", "ALFA")]
    if high_brf_super:
        insights["modifier_notes"].append(
            f"{len(high_brf_super)} SUPER/ALFA stock(s) carry High BRF risk: "
            + ", ".join(r["ticker"] for r in high_brf_super[:3])
            + " — elevated leverage warrants caution"
        )

    # ── Grade distribution market note ──
    grade_dist: dict[str, int] = defaultdict(int)
    for r in ranked:
        grade_dist[r["grade"]] += 1
    total = len(ranked)
    super_pct = grade_dist.get("SUPER", 0) / total if total else 0
    under_pct = grade_dist.get("UNDERPERFORM", 0) / total if total else 0
    if under_pct >= 0.3:
        insights["market_notes"].append(
            f"Broad weakness: {under_pct:.0%} of analyzed stocks rated UNDERPERFORM — possible macro headwind"
        )
    if super_pct >= 0.35:
        insights["market_notes"].append(
            f"Strong market signal: {super_pct:.0%} SUPER rate today — above typical threshold"
        )

    # ── Sector imbalance ──
    sector_counts: dict[str, int] = defaultdict(int)
    for r in ranked:
        sector_counts[r["sector"]] += 1
    dominant = max(sector_counts, key=sector_counts.get)
    if sector_counts[dominant] >= total * 0.4:
        insights["sector_patterns"].append(
            f"Sector imbalance: {dominant} represents {sector_counts[dominant]}/{total} stocks this run"
        )

    return insights


def _investment_thesis(result: dict, memory: dict) -> str:
    """Generate a 1-sentence thesis from result data + memory context."""
    sym   = result["ticker"]
    grade = result["grade"]
    bdr   = result["bdr_score"]
    brf   = result["brf_level"]
    sector = result["sector"]

    hist = memory.get("topPicksHistory", [])
    reconfirm_count = sum(1 for h in hist if h.get("ticker") == sym and h.get("grade") in ("SUPER", "ALFA"))

    sector_data = memory.get("sectorPatterns", {}).get(sector, {})
    sector_rate = sector_data.get("superAlfaRate", None)

    if grade == "SUPER":
        brf_note = "with low financial risk" if brf == "Low" else "despite elevated financial risk" if brf == "High" else ""
        confirm_note = f"; confirmed top performer across {reconfirm_count + 1} runs" if reconfirm_count > 0 else ""
        sector_note = f"; {sector} SUPER/ALFA rate is {sector_rate:.0%}" if sector_rate else ""
        return f"Highest conviction buy — BDR payback of {bdr:.1f} yrs {brf_note}{confirm_note}{sector_note}."
    elif grade == "ALFA":
        return f"Strong buy candidate with {bdr:.1f}-yr BDR payback and {brf} financial risk in {sector}."
    elif grade == "FAIR":
        return f"Hold or monitor — fair value with {bdr:.1f}-yr payback; no clear edge vs sector peers."
    else:
        return f"Avoid — weak fundamentals; BDR of {bdr:.1f} yrs not justified at current price."


# ──────────────────────────────────────────────
# PHASE 7: EMAIL REPORT
# ──────────────────────────────────────────────

def build_email_body(
    ranked: list[dict],
    insights: dict,
    memory: dict,
    today_str: str,
    run_count: int,
    new_learnings: list[str],
) -> str:
    top5  = ranked[:5]
    top3_tickers = ", ".join(r["ticker"] for r in ranked[:3])

    lines = []
    lines.append(f"StockAdvisor Daily Analysis — {today_str}")
    lines.append(f"Run #{run_count} | Top Picks: {top3_tickers}")
    lines.append("")

    lines.append("━" * 52)
    lines.append("TOP 5 INVESTMENT PICKS")
    lines.append("━" * 52)
    lines.append("")
    for i, r in enumerate(top5, 1):
        thesis = _investment_thesis(r, memory)
        lines.append(
            f"{i}. {r['ticker']} — {r['grade']} "
            f"(BDR: {r['bdr_score']:.1f}yrs, BRF: {r['brf_level']}, Price: ${r['price']})"
        )
        lines.append(f"   {thesis}")
        lines.append("")

    lines.append("━" * 52)
    lines.append(f"ALL {len(ranked)} ANALYZED (Ranked)")
    lines.append("━" * 52)
    lines.append("")
    lines.append(f"{'Rank':<5} {'Ticker':<8} {'Sector':<25} {'Grade':<14} {'BDR(yrs)':<10} {'BRF'}")
    lines.append("-" * 75)
    for i, r in enumerate(ranked, 1):
        lines.append(
            f"{i:<5} {r['ticker']:<8} {r['sector'][:24]:<25} {r['grade']:<14} "
            f"{r['bdr_score']:<10.1f} {r['brf_level']}"
        )

    lines.append("")
    lines.append("━" * 52)
    lines.append("MEMORY INSIGHTS APPLIED THIS RUN")
    lines.append("━" * 52)
    lines.append("")

    all_insights = (
        insights["sector_patterns"]
        + insights["modifier_notes"]
        + insights["market_notes"]
        + insights["anomalies"]
        + insights["confirmations"]
    )
    if all_insights:
        for note in all_insights:
            lines.append(f"• {note}")
    else:
        lines.append("• First run — no historical patterns to compare yet.")

    lines.append("")
    lines.append("━" * 52)
    lines.append("TODAY'S LEARNINGS (Added to Memory)")
    lines.append("━" * 52)
    lines.append("")
    if new_learnings:
        for note in new_learnings:
            lines.append(f"• {note}")
    else:
        lines.append("• No new divergences detected from prior memory.")

    lines.append("")
    lines.append("─" * 52)
    lines.append("Powered by StockAdvisor BDR/BRF Methodology")

    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())


# ──────────────────────────────────────────────
# PHASE 8: MEMORY UPDATE
# ──────────────────────────────────────────────

def update_memory(memory: dict, ranked: list[dict], insights: dict, run_count: int) -> tuple[dict, list[str]]:
    """
    Merge today's results into memory. Returns (updated_memory, new_learnings list).
    new_learnings are non-obvious observations to include in the email.
    """
    today_str = str(date.today())
    new_learnings = []

    # ── Sector patterns ──
    sector_today: dict[str, dict] = defaultdict(lambda: {"total": 0, "super_alfa": 0})
    for r in ranked:
        s = r["sector"]
        sector_today[s]["total"] += 1
        if r["grade"] in ("SUPER", "ALFA"):
            sector_today[s]["super_alfa"] += 1

    sector_patterns = memory.setdefault("sectorPatterns", {})
    for sector, counts in sector_today.items():
        if counts["total"] == 0:
            continue
        today_rate = counts["super_alfa"] / counts["total"]
        if sector not in sector_patterns:
            sector_patterns[sector] = {"superAlfaRate": today_rate, "sampleCount": counts["total"], "lastUpdated": today_str}
        else:
            old = sector_patterns[sector]
            old_n    = old.get("sampleCount", 0)
            old_rate = old.get("superAlfaRate", today_rate)
            new_n    = old_n + counts["total"]
            # Rolling weighted average
            new_rate = (old_rate * old_n + today_rate * counts["total"]) / new_n
            old_rate_snap = old_rate
            sector_patterns[sector] = {
                "superAlfaRate": round(new_rate, 4),
                "sampleCount": new_n,
                "lastUpdated": today_str,
            }
            # Flag significant shifts
            if old_n >= 10 and abs(new_rate - old_rate_snap) >= 0.10:
                direction = "up" if new_rate > old_rate_snap else "down"
                learning = (
                    f"Run {run_count}: {sector} SUPER/ALFA rate shifted {direction} "
                    f"from {old_rate_snap:.0%} to {new_rate:.0%} — investigate sector catalyst"
                )
                new_learnings.append(learning)

    # ── Modifier insights ──
    modifier_insights = memory.setdefault("modifierInsights", {})
    low_brf_super = [r for r in ranked if r["brf_level"] == "Low" and r["grade"] in ("SUPER", "ALFA")]
    if low_brf_super:
        key = "low_brf_super_alfa_signal"
        prev = modifier_insights.get(key, "")
        note = f"Run {run_count}: {len(low_brf_super)} stocks had Low BRF + SUPER/ALFA — strong signal confirmed"
        if note != prev:
            modifier_insights[key] = note

    high_brf_super = [r for r in ranked if r["brf_level"] == "High" and r["grade"] in ("SUPER", "ALFA")]
    if high_brf_super:
        key = "high_brf_despite_super"
        note = (
            f"Run {run_count}: {len(high_brf_super)} SUPER/ALFA stock(s) carry High BRF — "
            + ", ".join(r["ticker"] for r in high_brf_super)
        )
        modifier_insights[key] = note
        new_learnings.append(note)

    # ── Market condition notes ──
    market_notes: list = memory.setdefault("marketConditionNotes", [])
    grade_dist: dict[str, int] = defaultdict(int)
    for r in ranked:
        grade_dist[r["grade"]] += 1
    total = len(ranked)
    under_pct = grade_dist.get("UNDERPERFORM", 0) / total if total else 0
    super_pct = grade_dist.get("SUPER", 0) / total if total else 0

    market_note = None
    if under_pct >= 0.4:
        market_note = f"{today_str}: {under_pct:.0%} UNDERPERFORM — possible broad market stress"
    elif super_pct >= 0.4:
        market_note = f"{today_str}: {super_pct:.0%} SUPER rate — strong broad market signal"

    if market_note and (not market_notes or market_notes[-1] != market_note):
        market_notes.append(market_note)
        new_learnings.append(market_note)
    memory["marketConditionNotes"] = market_notes[-20:]  # keep last 20

    # ── Top picks history ──
    top_picks: list = memory.setdefault("topPicksHistory", [])
    top5 = ranked[:5]
    existing_map = {h["ticker"]: h for h in top_picks}
    for r in top5:
        sym = r["ticker"]
        if sym in existing_map:
            existing_map[sym]["reconfirmed"] = existing_map[sym].get("reconfirmed", 0) + 1
            existing_map[sym]["grade"] = r["grade"]
            existing_map[sym]["date"]  = today_str
        else:
            top_picks.append({
                "ticker":     sym,
                "date":       today_str,
                "grade":      r["grade"],
                "bdrScore":   r["bdr_score"],
                "reconfirmed": 0,
            })
    memory["topPicksHistory"] = top_picks[-100:]  # keep last 100 entries

    # ── Analyzed tickers ──
    analyzed = memory.setdefault("analyzedTickers", {})
    for r in ranked:
        analyzed[r["ticker"]] = {
            "lastAnalyzed": today_str,
            "lastGrade": r["grade"],
        }

    # ── Anomaly learnings from insights ──
    for anomaly in insights.get("anomalies", []):
        new_learnings.append(f"Run {run_count}: ANOMALY — {anomaly}")

    # ── Improvement notes ──
    improvement_notes: list = memory.setdefault("improvementNotes", [])
    if new_learnings:
        improvement_notes.append(
            f"Run {run_count} ({today_str}): {len(new_learnings)} new insight(s) recorded"
        )
    memory["improvementNotes"] = improvement_notes[-50:]

    # Update top-level fields
    memory["lastUpdated"] = today_str
    memory["runCount"]    = run_count

    return memory, new_learnings


# ──────────────────────────────────────────────
# ORCHESTRATOR
# ──────────────────────────────────────────────

def run(manual_tickers: list[str] | None = None) -> None:
    today_str = str(date.today())
    print(f"[{today_str}] StockAdvisor Daily Analysis — starting")

    # Phase 1: Init
    print("Phase 1: Fetching memory and ticker list...")
    memory  = fetch_memory()
    run_count = memory.get("runCount", 0) + 1
    memory["runCount"] = run_count

    all_tickers = fetch_tickers()
    print(f"  Loaded {len(all_tickers)} tickers from Gist")
    print(f"  Run #{run_count}")

    # Phase 2: Ticker selection
    print("Phase 2: Selecting tickers...")
    selected = select_tickers(all_tickers, memory, manual_override=manual_tickers)
    print(f"  Selected: {[t['ticker'] for t in selected]}")

    # Phase 3: Analysis
    print("Phase 3: Running batch analysis...")
    results = run_batch_analysis(selected)
    if not results:
        print("ERROR: No results returned. Aborting.", file=sys.stderr)
        sys.exit(1)
    print(f"  Analyzed {len(results)} tickers successfully")

    # Phase 4: Ranking
    print("Phase 4: Ranking results...")
    ranked = rank_results(results)

    # Phase 5 & 6: Memory insights
    print("Phase 5-6: Learning from memory and synthesizing insights...")
    insights = extract_insights(ranked, memory)

    # Phase 8 first pass: update memory (needed for email learnings)
    print("Phase 8: Updating memory...")
    updated_memory, new_learnings = update_memory(memory, ranked, insights, run_count)

    # Phase 7: Email
    print("Phase 7: Sending email report...")
    top3 = ", ".join(r["ticker"] for r in ranked[:3])
    subject = f"StockAdvisor Daily — {today_str} | Top Picks: {top3} | Run #{run_count}"
    body = build_email_body(ranked, insights, memory, today_str, run_count, new_learnings)

    try:
        send_email(subject, body)
        print(f"  Email sent to {NOTIFY_EMAIL}")
    except Exception as exc:
        print(f"  [ERROR] Email failed: {exc}", file=sys.stderr)
        print("  ── Email body (fallback stdout) ──")
        print(body)

    # Phase 8: Push memory to Gist
    print("Phase 8: Writing memory back to Gist...")
    push_memory(updated_memory)
    print(f"  Memory updated (run #{run_count})")

    print(f"[{today_str}] Done. Top pick: {ranked[0]['ticker']} ({ranked[0]['grade']})")


# ──────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────

if __name__ == "__main__":
    # Optional: pass comma-separated tickers as CLI arg
    # e.g. python stock_advisor.py AAPL,NVDA,XOM
    manual = None
    if len(sys.argv) > 1:
        manual = [s.strip() for s in sys.argv[1].split(",") if s.strip()]
        print(f"Manual override: {manual}")
    run(manual_tickers=manual)
