"""
StockAdvisor Daily — execution layer.
Runs inside GitHub Actions (or locally for Stage A test).
Reads tickers, calls analyze.php for BDR/BRF scoring, ranks, emails, writes data file.
"""

import json
import os
import smtplib
import ssl
import sys
import time
import traceback
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

STAGE_A_TICKERS = ["AAPL", "NVDA", "XOM"]

ANALYZE_URL = "https://eandhconsulting.com/portal/public/StockAdvisorII/analyze.php"
ANALYZE_TIMEOUT = 45
REQUEST_GAP_SECONDS = 0.5

GRADE_RANK = {"SUPER": 0, "ALFA": 1, "FAIR": 2, "UNDERPERFORM": 3}


def log(phase: str, msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] [{phase}] {msg}", flush=True)


def load_env_local(script_dir: Path) -> None:
    candidates = [
        Path(os.environ["ENV_FILE"]) if os.environ.get("ENV_FILE") else None,
        script_dir.parent.parent.parent / ".env" / ".env",
        script_dir / ".env",
    ]
    for env_path in candidates:
        if env_path and env_path.exists():
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())
            return


def require_env(*names: str) -> dict:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")
    return {n: os.environ[n] for n in names}


def load_tickers(script_dir: Path, override: bool) -> list[str]:
    if override:
        return list(STAGE_A_TICKERS)
    path = script_dir / "tickers.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return [entry["ticker"] for entry in data]


def analyze_ticker(ticker: str) -> dict:
    resp = requests.post(
        ANALYZE_URL,
        json={"ticker": ticker},
        headers={"Content-Type": "application/json"},
        timeout=ANALYZE_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    return {
        "ticker": ticker,
        "company": body.get("company"),
        "sector": body.get("sector"),
        "currentPrice": body.get("currentPrice"),
        "bdrGrade": (body.get("bdr") or {}).get("grade"),
        "bdrScore": (body.get("bdr") or {}).get("bdrScore"),
        "brfLevel": (body.get("brf") or {}).get("brfLevel"),
        "finalGrade": (body.get("cooling") or {}).get("finalGrade"),
    }


def rank_results(results: list[dict]) -> list[dict]:
    def key(r: dict) -> tuple:
        g = GRADE_RANK.get(r.get("finalGrade") or "", 99)
        bdr = r.get("bdrScore")
        bdr = float(bdr) if isinstance(bdr, (int, float, str)) and str(bdr) not in ("", "None") else 9999
        return (g, bdr)

    return sorted(results, key=key)


def build_email_body(today: str, ranked: list[dict], errors: list[dict], total: int) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append(f"StockAdvisor MVP — {today} | {len(ranked)} of {total} analyzed")
    lines.append("=" * 60)
    lines.append("")
    lines.append("RESULTS (RANKED)")
    lines.append("-" * 60)
    if ranked:
        for i, r in enumerate(ranked, 1):
            lines.append(
                f"{i}. {r['ticker']} — {r.get('finalGrade','?')} "
                f"(BDR: {r.get('bdrScore','?')}yrs, "
                f"BRF: {r.get('brfLevel','?')}, "
                f"Price: ${r.get('currentPrice','?')})"
            )
    else:
        lines.append("(no results)")
    lines.append("")
    lines.append("ERRORS")
    lines.append("-" * 60)
    if errors:
        for e in errors:
            lines.append(f"- [{e['phase']}] {e['ticker']}: {e['error']}")
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("PIPELINE STATUS")
    lines.append("-" * 60)
    lines.append(f"- Phase 2 (analysis): {len(ranked)} of {total} tickers returned")
    lines.append("- Phase 3 (ranking):  PASS" if ranked else "- Phase 3 (ranking):  SKIPPED")
    lines.append("- Phase 4 (email):    PASS (you are reading this)")
    return "\n".join(lines)


def send_email(subject: str, body: str, env: dict) -> None:
    msg = MIMEMultipart()
    msg["From"] = f"{env.get('SMTP_FROM_NAME','StockAdvisor')} <{env['SMTP_USER']}>"
    msg["To"] = env["NOTIFY_EMAIL"]
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    port = int(env["SMTP_PORT"])
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(env["SMTP_HOST"], port, context=ctx, timeout=30) as s:
            s.login(env["SMTP_USER"], env["SMTP_PASS"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(env["SMTP_HOST"], port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(env["SMTP_USER"], env["SMTP_PASS"])
            s.send_message(msg)


def write_data_file(script_dir: Path, today: str, payload: dict) -> Path:
    out = script_dir / "data" / f"{today}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out


def write_error_file(script_dir: Path, today: str, phase: str, error: str) -> Path:
    out = script_dir / "data" / f"{today}-ERROR.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        f"# StockAdvisor run failed — {today}\n\n"
        f"**Phase:** {phase}\n\n"
        f"**Raw error (verbatim, do not paraphrase):**\n\n```\n{error}\n```\n",
        encoding="utf-8",
    )
    return out


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")

    load_env_local(script_dir)

    stage_a = os.environ.get("STAGE_A", "0").strip().lower() in ("1", "true", "yes", "on")

    try:
        env = require_env(
            "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "NOTIFY_EMAIL"
        )
        env.setdefault("SMTP_FROM_NAME", os.environ.get("SMTP_FROM_NAME", "StockAdvisor"))
    except Exception as e:
        log("INIT", f"FAIL: {e}")
        write_error_file(script_dir, today, "INIT", str(e))
        return 2

    log("INIT", f"today={today} stage_a={stage_a}")

    try:
        tickers = load_tickers(script_dir, stage_a)
        log("INIT", f"tickers={tickers}")
    except Exception as e:
        log("INIT", f"FAIL loading tickers: {e}")
        write_error_file(script_dir, today, "INIT", traceback.format_exc())
        return 2

    results: list[dict] = []
    errors: list[dict] = []

    for t in tickers:
        try:
            log("PHASE2", f"POST {t}")
            r = analyze_ticker(t)
            results.append(r)
            log("PHASE2", f"OK {t} grade={r.get('finalGrade')} price={r.get('currentPrice')}")
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            body = e.response.text[:300] if e.response is not None else ""
            msg = f"HTTP {code}: {body}"
            log("PHASE2", f"FAIL {t} {msg}")
            errors.append({"ticker": t, "phase": "PHASE2", "error": msg})
        except Exception as e:
            log("PHASE2", f"FAIL {t} {type(e).__name__}: {e}")
            errors.append({"ticker": t, "phase": "PHASE2", "error": f"{type(e).__name__}: {e}"})
        time.sleep(REQUEST_GAP_SECONDS)

    try:
        ranked = rank_results(results)
        log("PHASE3", f"ranked {len(ranked)} tickers")
    except Exception as e:
        log("PHASE3", f"FAIL {e}")
        errors.append({"ticker": "-", "phase": "PHASE3", "error": str(e)})
        ranked = results

    payload = {
        "date": today,
        "stageA": stage_a,
        "tickers": tickers,
        "results": ranked,
        "errors": errors,
    }

    try:
        data_path = write_data_file(script_dir, today, payload)
        log("PHASE4", f"wrote {data_path}")
    except Exception as e:
        log("PHASE4", f"FAIL writing data file: {e}")

    try:
        subject = f"StockAdvisor MVP — {today} | {len(ranked)} of {len(tickers)} analyzed"
        body = build_email_body(today, ranked, errors, len(tickers))
        send_email(subject, body, env)
        log("PHASE4", f"email sent to {env['NOTIFY_EMAIL']}")
    except Exception as e:
        log("PHASE4", f"FAIL email: {type(e).__name__}: {e}")
        write_error_file(script_dir, today, "PHASE4", traceback.format_exc())
        return 3

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
