#!/usr/bin/env python3
"""
StockAdvisor MVP — 3-ticker diagnostic pipeline.
Phases: analyze → rank → email.  Every phase wrapped in try/except.
"""

import json
import os
import smtplib
import sys
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
ANALYZE_URL  = "https://eandhconsulting.com/portal/public/StockAdvisorII/analyze.php"
SMTP_HOST    = os.getenv("SMTP_HOST", "smtp.hostinger.com")
SMTP_PORT    = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER    = os.getenv("SMTP_USER", "ezemach@eandhconsulting.com")
SMTP_PASS    = os.getenv("SMTP_PASS", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "ezumach@gmail.com")

GRADE_ORDER  = {"SUPER": 0, "ALFA": 1, "FAIR": 2, "UNDERPERFORM": 3}

# ── Phase 1 ───────────────────────────────────────────────────────────────────
today   = str(date.today())
tickers = ["AAPL", "NVDA", "XOM"]
results = []
errors  = []

print(f"[{today}] StockAdvisor MVP — starting")
print(f"Phase 1: today={today}, tickers={tickers}")

# ── Phase 2: Analysis ─────────────────────────────────────────────────────────
print("\nPhase 2: Analyzing tickers...")
for ticker in tickers:
    try:
        resp = requests.post(
            ANALYZE_URL,
            json={"ticker": ticker},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        bdr  = data.get("bdr", {})
        brf  = data.get("brf", {})
        cool = data.get("cooling", {})

        result = {
            "ticker":       ticker,
            "company":      data.get("company", ticker),
            "sector":       data.get("sector", "Unknown"),
            "currentPrice": data.get("currentPrice") or data.get("price") or 0,
            "bdr_grade":    bdr.get("grade", "N/A"),
            "bdr_score":    float(bdr.get("bdrScore") or bdr.get("score") or 0),
            "brf_level":    brf.get("brfLevel") or brf.get("level") or "N/A",
            "final_grade":  (cool.get("finalGrade") or bdr.get("grade") or "UNKNOWN").upper(),
        }
        results.append(result)
        print(f"  {ticker}: {result['final_grade']} | BDR {result['bdr_score']:.1f}yrs"
              f" | BRF {result['brf_level']} | ${result['currentPrice']}")

    except requests.exceptions.HTTPError as exc:
        msg = f"HTTP {exc.response.status_code} — {exc}"
        errors.append({"ticker": ticker, "phase": "PHASE 2", "error": msg})
        print(f"  [FAIL] {ticker}: {msg}")
    except Exception as exc:
        msg = str(exc)
        errors.append({"ticker": ticker, "phase": "PHASE 2", "error": msg})
        print(f"  [FAIL] {ticker}: {msg}")

phase2_status = f"PASS — {len(results)} of {len(tickers)} tickers returned"
if len(results) == 0:
    phase2_status = f"FAIL — 0 of {len(tickers)} tickers returned"
elif len(results) < len(tickers):
    phase2_status = f"PARTIAL — {len(results)} of {len(tickers)} tickers returned"

# ── Phase 3: Ranking ──────────────────────────────────────────────────────────
print("\nPhase 3: Ranking results...")
phase3_status = "PASS"
ranked = []
try:
    ranked = sorted(
        results,
        key=lambda r: (GRADE_ORDER.get(r["final_grade"], 99), r["bdr_score"]),
    )
    print(f"  Ranked {len(ranked)} results")
except Exception as exc:
    phase3_status = f"FAIL — {exc}"
    errors.append({"ticker": "ALL", "phase": "PHASE 3", "error": str(exc)})
    print(f"  [FAIL] ranking: {exc}")
    ranked = results  # unranked fallback so email still has data

# ── Phase 4: Email ────────────────────────────────────────────────────────────
print("\nPhase 4: Building email report...")

divider = "━" * 49

results_lines = []
for i, r in enumerate(ranked, 1):
    results_lines.append(
        f"{i}. {r['ticker']} — {r['final_grade']}"
        f" (BDR: {r['bdr_score']:.1f}yrs,"
        f" BRF: {r['brf_level']},"
        f" Price: ${r['currentPrice']})"
    )

errors_lines = []
if errors:
    for e in errors:
        errors_lines.append(f"- [{e['phase']}] {e['ticker']}: {e['error']}")
else:
    errors_lines.append("None")

body_parts = [
    divider,
    "RESULTS (RANKED)",
    divider,
    *results_lines,
    "",
    divider,
    "ERRORS (if any)",
    divider,
    *errors_lines,
    "",
    divider,
    "PIPELINE STATUS",
    divider,
    f"- Phase 2 (analysis): {phase2_status}",
    f"- Phase 3 (ranking):  {phase3_status}",
    "- Phase 4 (email):    PASS if you see this",
]

body    = "\n".join(body_parts)
subject = f"StockAdvisor MVP — {today} | {len(results)} of {len(tickers)} analyzed"

print(f"  Subject: {subject}")

try:
    if not SMTP_PASS:
        raise ValueError("SMTP_PASS is empty — set it in .env")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())

    print(f"  Email sent to {NOTIFY_EMAIL}")

except Exception as exc:
    print(f"\n[PHASE 4 ERROR] Email failed: {exc}", file=sys.stderr)
    print("\n── FALLBACK: email body printed to stdout ──")
    print(f"Subject: {subject}\n")
    print(body)
    sys.exit(1)

print(f"\n[{today}] MVP routine complete.")
