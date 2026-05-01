"""
StockAdvaisor Orchestrator — runs after daily data action.
Calls Claude via Kie.ai for insights, then sends one branded HTML email.
"""

import json
import os
import smtplib
import ssl
import sys
import requests
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
MEMORY_PATH = SCRIPT_DIR / "memory.json"

KIE_URL = "https://api.kie.ai/claude/v1/messages"
MODEL = "claude-opus-4-5"
MAX_TOKENS = 2048

GRADE_COLOR = {
    "SUPER":        "#4ADE80",
    "ALFA":         "#4A90E2",
    "FAIR":         "#8EA5BC",
    "UNDERPERFORM": "#F87171",
}
GRADE_TEXT_COLOR = {
    "SUPER":        "#080E1A",
    "ALFA":         "#080E1A",
    "FAIR":         "#080E1A",
    "UNDERPERFORM": "#080E1A",
}


# ── Utilities ────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] [ORCH] {msg}", flush=True)


def write_ack(today: str, reason: str) -> None:
    path = DATA_DIR / f"{today}-routine-ack.md"
    path.write_text(f"Orchestrator did not run on {today}: {reason}\n", encoding="utf-8")


def write_error(today: str, error: str) -> None:
    path = DATA_DIR / f"{today}-routine-ERROR.md"
    path.write_text(
        f"# Orchestrator failed — {today}\n\n**Raw error:**\n\n```\n{error}\n```\n",
        encoding="utf-8",
    )


# ── Claude via Kie.ai ────────────────────────────────────────────────────────

def call_claude(api_key: str, prompt: str) -> str:
    resp = requests.post(
        KIE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    body = resp.json()
    log(f"Kie.ai response keys: {list(body.keys())}")
    # Anthropic format
    if "content" in body:
        return body["content"][0]["text"]
    # OpenAI format
    if "choices" in body:
        return body["choices"][0]["message"]["content"]
    raise ValueError(f"Unexpected response format: {body}")


def parse_json_response(raw: str) -> dict:
    text = raw.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    return json.loads(text)


def build_prompt(today: str, data: dict, memory: dict) -> str:
    results = data.get("results", [])
    summary = [
        {
            "ticker": r["ticker"],
            "sector": r.get("sector"),
            "grade": r.get("finalGrade"),
            "bdr": r.get("bdrScore"),
            "brf": r.get("brfLevel"),
            "price": r.get("currentPrice"),
        }
        for r in results
    ]
    prev_grades = {k: v.get("lastGrade") for k, v in memory.get("analyzedTickers", {}).items()}
    run_count = memory.get("runCount", 0)

    return f"""You are the AI brain of StockAdvaisor. Today is {today}. Run #{run_count + 1}.

RANKED RESULTS ({len(summary)} tickers):
{json.dumps(summary, indent=2)}

PREVIOUS GRADES (from memory):
{json.dumps(prev_grades)}

RULES:
- Identify sector patterns, grade flips vs previous, BDR/BRF signals, anomalies.
- 3-5 concise bullet points only. No tautologies.
- Never suggest modifying BDR/BRF scoring logic.

Respond with ONLY valid JSON — no prose:
{{
  "insights_md": "- Bullet one\\n- Bullet two\\n- Bullet three",
  "memory_updates": {{
    "sectorPatterns": {{}},
    "modifierInsights": {{}},
    "marketConditionNotes": [],
    "topPicksHistory": [],
    "analyzedTickers": {{}}
  }},
  "improvement_pr": null
}}"""


# ── HTML Email Builder ───────────────────────────────────────────────────────

def grade_badge(grade: str) -> str:
    color = GRADE_COLOR.get(grade, "#7A8FA6")
    text_color = GRADE_TEXT_COLOR.get(grade, "#080E1A")
    return (
        f'<span style="background-color:{color};color:{text_color};'
        f'padding:3px 10px;border-radius:4px;font-family:Arial,sans-serif;'
        f'font-size:11px;font-weight:bold;letter-spacing:0.5px;">'
        f'{grade or "?"}</span>'
    )


def build_top3_cells(ranked: list[dict]) -> str:
    cells = ""
    for r in ranked[:3]:
        grade = r.get("finalGrade", "?")
        color = GRADE_COLOR.get(grade, "#7A8FA6")
        price = r.get("currentPrice", "?")
        price_fmt = f"${price}" if price != "?" else "?"
        cells += f"""
          <td width="33%" style="padding:0 6px;" align="center">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="background-color:#1E3A5F;border-radius:6px;
                           border-top:3px solid {color};padding:14px 8px;"
                    align="center">
                  <div style="font-family:Georgia,serif;font-size:20px;
                              font-weight:bold;color:#F8F6F1;">{r['ticker']}</div>
                  <div style="font-family:Arial,sans-serif;font-size:11px;
                              color:#8EA5BC;margin-top:2px;">{r.get('company','') or ''}</div>
                  <div style="margin-top:8px;">{grade_badge(grade)}</div>
                  <div style="font-family:Arial,sans-serif;font-size:11px;
                              color:#8EA5BC;margin-top:8px;">BDR: {r.get('bdrScore','?')} yrs</div>
                  <div style="font-family:Arial,sans-serif;font-size:12px;
                              color:#F8F6F1;margin-top:2px;">{price_fmt}</div>
                </td>
              </tr>
            </table>
          </td>"""
    return cells


def build_results_rows(ranked: list[dict]) -> str:
    rows = ""
    for i, r in enumerate(ranked):
        bg = "#0D1A2B" if i % 2 == 0 else "#080E1A"
        grade = r.get("finalGrade", "?")
        price = r.get("currentPrice", "?")
        rows += f"""
            <tr style="background-color:{bg};">
              <td style="padding:8px 12px;font-family:Arial,sans-serif;
                         font-size:12px;color:#7A8FA6;">{i + 1}</td>
              <td style="padding:8px 12px;font-family:Arial,sans-serif;
                         font-size:13px;color:#F8F6F1;font-weight:bold;">{r['ticker']}</td>
              <td style="padding:8px 12px;font-family:Arial,sans-serif;
                         font-size:11px;color:#8EA5BC;">{r.get('sector','') or ''}</td>
              <td style="padding:8px 12px;">{grade_badge(grade)}</td>
              <td style="padding:8px 12px;font-family:Arial,sans-serif;
                         font-size:12px;color:#8EA5BC;">{r.get('bdrScore','?')} yrs</td>
              <td style="padding:8px 12px;font-family:Arial,sans-serif;
                         font-size:12px;color:#8EA5BC;">{r.get('brfLevel','?')}</td>
              <td style="padding:8px 12px;font-family:Arial,sans-serif;
                         font-size:12px;color:#F8F6F1;">${price}</td>
            </tr>"""
    return rows


def build_insights_html(insights_md: str) -> str:
    if not insights_md:
        return ""
    bullets = ""
    for line in insights_md.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        text = line.lstrip("-•* ").strip()
        if text:
            bullets += f"""
            <tr>
              <td style="padding:5px 0 5px 12px;font-family:Arial,sans-serif;
                         font-size:13px;color:#F8F6F1;line-height:1.5;
                         border-left:3px solid #2E5C99;">&#8226; {text}</td>
            </tr>"""
    return f"""
          <!-- INSIGHTS -->
          <tr>
            <td style="background-color:#0D1A2B;padding:24px 32px;">
              <div style="font-family:Georgia,serif;font-size:13px;font-weight:bold;
                          color:#4A90E2;letter-spacing:1px;margin-bottom:14px;">
                AI INSIGHTS
              </div>
              <table width="100%" cellpadding="0" cellspacing="0">
                {bullets}
              </table>
            </td>
          </tr>
          <tr><td style="background-color:#2E5C99;height:1px;"></td></tr>"""


def build_pr_html(pr: dict | None) -> str:
    if not pr:
        return ""
    return f"""
          <!-- IMPROVEMENT PR -->
          <tr>
            <td style="background-color:#0D1A2B;padding:24px 32px;">
              <div style="font-family:Georgia,serif;font-size:13px;font-weight:bold;
                          color:#4A90E2;letter-spacing:1px;margin-bottom:14px;">
                &#9881; IMPROVEMENT FLAGGED
              </div>
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="background-color:#1E3A5F;border-left:3px solid #2E5C99;
                             border-radius:0 4px 4px 0;padding:14px 16px;">
                    <div style="font-family:Arial,sans-serif;font-size:13px;
                                color:#F8F6F1;font-weight:bold;">{pr.get('title','')}</div>
                    <div style="font-family:Arial,sans-serif;font-size:12px;
                                color:#8EA5BC;margin-top:8px;line-height:1.5;">
                      {pr.get('body','')}</div>
                    <div style="font-family:Arial,sans-serif;font-size:11px;
                                color:#7A8FA6;margin-top:10px;">
                      Branch: <span style="color:#4A90E2;">{pr.get('branch','')}</span>
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr><td style="background-color:#2E5C99;height:1px;"></td></tr>"""


def build_errors_html(errors: list[dict]) -> str:
    if not errors:
        return ""
    rows = ""
    for e in errors:
        rows += f"""
                <tr>
                  <td style="padding:4px 0;font-family:Arial,sans-serif;
                             font-size:12px;color:#F87171;">
                    &#9679; [{e['phase']}] {e['ticker']}: {e['error']}
                  </td>
                </tr>"""
    return f"""
          <!-- ERRORS -->
          <tr>
            <td style="background-color:#0D1A2B;padding:24px 32px;">
              <div style="font-family:Georgia,serif;font-size:13px;font-weight:bold;
                          color:#F87171;letter-spacing:1px;margin-bottom:14px;">
                ERRORS ({len(errors)})
              </div>
              <table width="100%" cellpadding="0" cellspacing="0">
                {rows}
              </table>
            </td>
          </tr>
          <tr><td style="background-color:#2E5C99;height:1px;"></td></tr>"""


def build_html_email(today: str, data: dict, insights_md: str, pr: dict | None, run_count: int) -> str:
    ranked = data.get("results", [])
    errors = data.get("errors", [])
    total = len(data.get("tickers", []))

    top3_cells   = build_top3_cells(ranked)
    results_rows = build_results_rows(ranked)
    insights_sec = build_insights_html(insights_md)
    pr_sec       = build_pr_html(pr)
    errors_sec   = build_errors_html(errors)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background-color:#080E1A;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0"
         style="background-color:#080E1A;">
    <tr>
      <td align="center" style="padding:24px 16px;">
        <table width="600" cellpadding="0" cellspacing="0" border="0"
               style="max-width:600px;width:100%;">

          <!-- HEADER -->
          <tr>
            <td style="background-color:#1E3A5F;padding:24px 32px;
                       border-radius:8px 8px 0 0;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td>
                    <div style="font-family:Georgia,serif;font-size:26px;
                                font-weight:bold;color:#F8F6F1;">StockAdvaisor</div>
                    <div style="font-family:Arial,sans-serif;font-size:12px;
                                color:#8EA5BC;margin-top:4px;">
                      Daily Report &mdash; {today}
                    </div>
                  </td>
                  <td align="right" valign="middle">
                    <div style="font-family:Arial,sans-serif;font-size:12px;
                                color:#4A90E2;font-weight:bold;">E&amp;H Consulting</div>
                    <div style="font-family:Arial,sans-serif;font-size:11px;
                                color:#7A8FA6;margin-top:4px;">
                      Run #{run_count} &bull; {len(ranked)}/{total} analyzed
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr><td style="background-color:#2E5C99;height:3px;"></td></tr>

          <!-- TOP 3 -->
          <tr>
            <td style="background-color:#0D1A2B;padding:24px 32px;">
              <div style="font-family:Georgia,serif;font-size:13px;font-weight:bold;
                          color:#4A90E2;letter-spacing:1px;margin-bottom:16px;">
                &#9733; TODAY&apos;S TOP 3 PICKS
              </div>
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  {top3_cells}
                </tr>
              </table>
            </td>
          </tr>
          <tr><td style="background-color:#2E5C99;height:1px;"></td></tr>

          <!-- FULL RANKINGS -->
          <tr>
            <td style="background-color:#080E1A;padding:24px 32px;">
              <div style="font-family:Georgia,serif;font-size:13px;font-weight:bold;
                          color:#4A90E2;letter-spacing:1px;margin-bottom:14px;">
                FULL RANKINGS &mdash; {len(ranked)} TICKERS
              </div>
              <table width="100%" cellpadding="0" cellspacing="0"
                     style="border-collapse:collapse;">
                <tr style="background-color:#1E3A5F;">
                  <td style="padding:8px 12px;font-family:Arial,sans-serif;font-size:10px;
                             color:#8EA5BC;font-weight:bold;letter-spacing:1px;">#</td>
                  <td style="padding:8px 12px;font-family:Arial,sans-serif;font-size:10px;
                             color:#8EA5BC;font-weight:bold;letter-spacing:1px;">TICKER</td>
                  <td style="padding:8px 12px;font-family:Arial,sans-serif;font-size:10px;
                             color:#8EA5BC;font-weight:bold;letter-spacing:1px;">SECTOR</td>
                  <td style="padding:8px 12px;font-family:Arial,sans-serif;font-size:10px;
                             color:#8EA5BC;font-weight:bold;letter-spacing:1px;">GRADE</td>
                  <td style="padding:8px 12px;font-family:Arial,sans-serif;font-size:10px;
                             color:#8EA5BC;font-weight:bold;letter-spacing:1px;">BDR</td>
                  <td style="padding:8px 12px;font-family:Arial,sans-serif;font-size:10px;
                             color:#8EA5BC;font-weight:bold;letter-spacing:1px;">BRF</td>
                  <td style="padding:8px 12px;font-family:Arial,sans-serif;font-size:10px;
                             color:#8EA5BC;font-weight:bold;letter-spacing:1px;">PRICE</td>
                </tr>
                {results_rows}
              </table>
            </td>
          </tr>
          <tr><td style="background-color:#2E5C99;height:1px;"></td></tr>

          {insights_sec}
          {pr_sec}
          {errors_sec}

          <!-- FOOTER -->
          <tr>
            <td style="background-color:#0D1A2B;padding:16px 32px;
                       border-radius:0 0 8px 8px;border-top:2px solid #2E5C99;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="font-family:Arial,sans-serif;font-size:11px;
                             color:#7A8FA6;">
                    E&amp;H Consulting &bull; StockAdvaisor Daily
                  </td>
                  <td align="right" style="font-family:Arial,sans-serif;
                                          font-size:11px;color:#7A8FA6;">
                    {today}
                  </td>
                </tr>
              </table>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


# ── Email Send ───────────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str, env: dict) -> None:
    recipients = [e.strip() for e in env["NOTIFY_EMAIL"].split(",") if e.strip()]

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{env.get('SMTP_FROM_NAME', 'StockAdvaisor')} <{env['SMTP_USER']}>"
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    port = int(env["SMTP_PORT"])
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(env["SMTP_HOST"], port, context=ctx, timeout=30) as s:
            s.login(env["SMTP_USER"], env["SMTP_PASS"])
            s.sendmail(env["SMTP_USER"], recipients, msg.as_string())
    else:
        with smtplib.SMTP(env["SMTP_HOST"], port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(env["SMTP_USER"], env["SMTP_PASS"])
            s.sendmail(env["SMTP_USER"], recipients, msg.as_string())


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    data_path  = DATA_DIR / f"{today}.json"
    error_path = DATA_DIR / f"{today}-ERROR.md"

    if error_path.exists():
        log("Data action failed — writing ack, skipping")
        write_ack(today, "data action produced an ERROR file")
        return 1

    if not data_path.exists():
        log(f"No data file for {today} — action may not have fired")
        write_ack(today, "no data file found")
        return 1

    api_key = os.environ.get("KIE_API_KEY", "").strip()
    if not api_key:
        log("KIE_API_KEY missing")
        write_error(today, "KIE_API_KEY environment variable not set")
        return 2

    data   = json.loads(data_path.read_text(encoding="utf-8"))
    memory = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))

    log(f"Loaded {len(data.get('results',[]))} tickers | memory runCount={memory.get('runCount',0)}")

    # Call Claude
    try:
        raw = call_claude(api_key, build_prompt(today, data, memory))
        log("Claude responded")
    except Exception as e:
        log(f"Claude call failed: {e}")
        write_error(today, str(e))
        return 2

    try:
        result = parse_json_response(raw)
    except Exception as e:
        log(f"JSON parse failed: {e}")
        write_error(today, f"Parse error: {e}\n\nRaw:\n{raw}")
        return 2

    insights_md = result.get("insights_md", "")
    pr          = result.get("improvement_pr")

    # Write insights file
    insights_path = DATA_DIR / f"{today}-insights.md"
    insights_path.write_text(insights_md, encoding="utf-8")
    log(f"Wrote {insights_path.name}")

    # Update memory
    updates = result.get("memory_updates", {})
    memory["runCount"]   = memory.get("runCount", 0) + 1
    memory["lastUpdated"] = today

    for key in ["sectorPatterns", "modifierInsights"]:
        if updates.get(key):
            memory.setdefault(key, {}).update(updates[key])
    for key in ["marketConditionNotes", "topPicksHistory", "improvementNotes"]:
        if updates.get(key):
            memory.setdefault(key, []).extend(updates[key])
    if updates.get("analyzedTickers"):
        for ticker, info in updates["analyzedTickers"].items():
            memory.setdefault("analyzedTickers", {})[ticker] = info

    MEMORY_PATH.write_text(json.dumps(memory, indent=2), encoding="utf-8")
    log(f"Updated memory.json — runCount={memory['runCount']}")

    # Save improvement PR
    if pr:
        log(f"Improvement flagged: {pr.get('title')}")
        imp_path = DATA_DIR / f"{today}-improvement.json"
        imp_path.write_text(json.dumps(pr, indent=2), encoding="utf-8")

    # Send HTML email
    smtp_env = {k: os.environ.get(k, "") for k in
                ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "NOTIFY_EMAIL", "SMTP_FROM_NAME"]}
    smtp_ready = all(smtp_env.get(k) for k in ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "NOTIFY_EMAIL"])

    if smtp_ready:
        try:
            ranked = data.get("results", [])
            top1 = f"{ranked[0]['ticker']} ({ranked[0].get('finalGrade','?')})" if ranked else "N/A"
            subject = (
                f"StockAdvaisor {today} | Top: {top1} | "
                f"{len(ranked)}/{len(data.get('tickers',[]))} analyzed"
            )
            html = build_html_email(today, data, insights_md, pr, memory["runCount"])
            send_email(subject, html, smtp_env)
            log(f"HTML email sent to {smtp_env['NOTIFY_EMAIL']}")
        except Exception as e:
            log(f"Email failed: {type(e).__name__}: {e}")
            write_error(today, f"Email error: {e}")
            return 3
    else:
        log("SMTP env vars missing — skipping email")

    return 0


if __name__ == "__main__":
    sys.exit(main())
