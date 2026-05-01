"""
StockAdvaisor Orchestrator — runs after daily data action.
Calls Claude via Kie.ai to synthesize insights and update memory.
"""

import json
import os
import sys
import requests
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
MEMORY_PATH = SCRIPT_DIR / "memory.json"

KIE_URL = "https://api.kie.ai/claude/v1/messages"
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048


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
    return resp.json()["content"][0]["text"]


def parse_json_response(raw: str) -> dict:
    text = raw.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    return json.loads(text)


def build_prompt(today: str, data: dict, memory: dict) -> str:
    return f"""You are the AI brain of StockAdvaisor. Today is {today}.

Your job: read today's ticker analysis, synthesize insights, and return structured updates.

TODAY'S DATA:
{json.dumps(data, indent=2)}

CURRENT MEMORY:
{json.dumps(memory, indent=2)}

RULES:
- Focus on sector patterns, grade flips vs memory, modifier signals, anomalies.
- Only add non-obvious insights. No tautologies.
- Never modify BDR/BRF scoring logic.
- Keep insights under 15 lines.
- If you spot a code improvement in stock_advisor.py, describe it under improvement_pr.

Respond with ONLY a valid JSON object — no prose before or after:
{{
  "insights_md": "# StockAdvaisor Insights — {today}\\n\\n<your bullet points here>",
  "memory_updates": {{
    "sectorPatterns": {{}},
    "modifierInsights": {{}},
    "marketConditionNotes": [],
    "topPicksHistory": [],
    "analyzedTickers": {{}}
  }},
  "improvement_pr": null
}}

For improvement_pr, use null if no improvement found, or:
{{"branch": "stockadvaisor/fix-<slug>", "title": "fix: <what>", "body": "<why it matters and what to change>"}}
"""


def main() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    data_path = DATA_DIR / f"{today}.json"
    error_path = DATA_DIR / f"{today}-ERROR.md"

    # Guard: data action failed
    if error_path.exists():
        log("Data action failed — writing ack, skipping synthesis")
        write_ack(today, "data action produced an ERROR file")
        return 1

    # Guard: no data file
    if not data_path.exists():
        log(f"No data file for {today} — action may not have fired")
        write_ack(today, "no data file found")
        return 1

    api_key = os.environ.get("KIE_API_KEY", "").strip()
    if not api_key:
        log("KIE_API_KEY missing")
        write_error(today, "KIE_API_KEY environment variable not set")
        return 2

    data = json.loads(data_path.read_text(encoding="utf-8"))
    memory = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))

    log(f"Loaded data ({len(data.get('results', []))} tickers) and memory (runCount={memory.get('runCount', 0)})")

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
        write_error(today, f"Parse error: {e}\n\nRaw response:\n{raw}")
        return 2

    # Write insights
    insights_path = DATA_DIR / f"{today}-insights.md"
    insights_path.write_text(result["insights_md"], encoding="utf-8")
    log(f"Wrote {insights_path.name}")

    # Update memory
    updates = result.get("memory_updates", {})
    memory["runCount"] = memory.get("runCount", 0) + 1
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

    # Log improvement PR if flagged (auto-creation handled in future phase)
    pr = result.get("improvement_pr")
    if pr:
        log(f"Improvement flagged: {pr.get('title')} — will be included in next email report")
        imp_path = DATA_DIR / f"{today}-improvement.json"
        imp_path.write_text(json.dumps(pr, indent=2), encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())
