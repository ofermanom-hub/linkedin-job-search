"""
Flask backend for LinkedIn Job Search.
- Serves index.html and rejected_companies.json
- POST /refresh-applied: scans Gmail (past 6 months) for application-confirmation
  emails via Gmail MCP, extracts company names, merges into rejected_companies.json.

Run:
    pip install flask anthropic
    export ANTHROPIC_API_KEY=...
    python3 server.py

Open: http://127.0.0.1:5000/
"""

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from flask import Flask, jsonify, send_from_directory

ROOT = Path(__file__).parent
REJECTED_FILE = ROOT / "rejected_companies.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

CLAUDE_SCAN_PROMPT = """Use the Gmail MCP integration (mcp__claude_ai_Gmail__search_threads
and mcp__claude_ai_Gmail__get_thread) to find EVERY job application I submitted in the
LAST 6 MONTHS. Be exhaustive — missing one is worse than including a duplicate.

Run these searches separately (combine results, dedupe at the end):

1. Subject-line confirmations (highest signal):
   newer_than:6m subject:("thanks for applying" OR "thank you for applying"
     OR "we got it" OR "application received" OR "application confirmation"
     OR "we received your application" OR "your application" OR "application submitted")

2. Body-text confirmations:
   newer_than:6m ("thanks for applying" OR "thank you for applying"
     OR "we received your application" OR "your application has been received"
     OR "application has been submitted" OR "we've received your application"
     OR "thanks for your interest in" OR "thank you for your interest in"
     OR "we got your application")

3. ATS / careers sender domains:
   newer_than:6m (from:careers.* OR from:no-reply@careers.* OR from:talent@
     OR from:recruiting@ OR from:jobs@ OR from:hello@hi.greenhouse.io
     OR from:donotreply@notifications.greenhouse.io OR from:no-reply@ashbyhq.com
     OR from:notifications@lever.co OR from:hi@hire.lever.co
     OR from:noreply@smartrecruiters.com OR from:no-reply@comeet.co
     OR from:no-reply@hire.com)

For EACH search, paginate through ALL results — do not stop at the first page.

SPEED RULE: Do NOT call get_thread unless absolutely necessary. The thread list from
search_threads already gives you subject + sender, which is enough to identify the
company in 95% of cases. Extract from subject/sender directly. Only call get_thread
if subject and sender are both ambiguous.

Company extraction rules (apply in this order):
  1. Subject pattern "...at <Company>" or "...with <Company>" or "...for ... at <Company>"
     → use <Company>. Example: "Thanks for applying for TAM at Evinced" → "evinced"
  2. Sender domain — strip subdomain noise:
     no-reply@careers.evinced.com → "evinced"
     talent@kisstera.com → "kisstera"
     jobs@acme.io → "acme"
  3. Ignore the platform name (LinkedIn, Greenhouse, Lever, Workday, Ashby, SmartRecruiters,
     Comeet, SparkHire, Indeed, Spark Hire) — those are NEVER the company
  4. Ignore recruiter agency names; pick the actual employer

Return ONLY a JSON array of lowercase company names, deduplicated, alphabetised. Strip
suffixes ("Inc", "Ltd", "Technologies", ".com"). Example:
["acme", "evinced", "globex", "soylent"]

If you find zero confirmations across ALL searches, return [].
Output ONLY the JSON array — no prose, no markdown fences, no explanation.
"""

CLAUDE_TIMEOUT_SECONDS = 600
CLAUDE_LOG = ROOT / "claude_scan.log"


def load_existing() -> set[str]:
    if not REJECTED_FILE.exists():
        return set()
    try:
        data = json.loads(REJECTED_FILE.read_text())
        if isinstance(data, list):
            return {str(c).lower().strip() for c in data if str(c).strip()}
    except Exception as e:
        log.warning(f"Could not read {REJECTED_FILE}: {e}")
    return set()


def save_companies(companies: set[str]) -> None:
    sorted_list = sorted(companies)
    REJECTED_FILE.write_text(json.dumps(sorted_list, indent=2) + "\n")


def scan_gmail_for_applications() -> list[str]:
    """Shell out to `claude -p` so the user's configured Gmail MCP is used."""
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise RuntimeError("`claude` CLI not on PATH — required for Gmail MCP access")

    proc = subprocess.run(
        [
            claude_bin,
            "-p",
            CLAUDE_SCAN_PROMPT,
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
            "--model", "claude-sonnet-4-6",
        ],
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT_SECONDS,
        cwd=str(ROOT),
    )
    try:
        CLAUDE_LOG.write_text(
            f"--- exit {proc.returncode} ---\n[stdout]\n{proc.stdout}\n[stderr]\n{proc.stderr}\n"
        )
    except Exception:
        pass

    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:500]}")

    # `--output-format json` wraps the assistant text in a JSON envelope.
    raw = proc.stdout.strip()
    inner = raw
    try:
        envelope = json.loads(raw)
        inner = envelope.get("result") or envelope.get("response") or raw
        if not isinstance(inner, str):
            inner = json.dumps(inner)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\[.*\]", inner, re.DOTALL)
    if not match:
        log.warning(f"No JSON array in claude output: {inner[:300]}")
        return []
    try:
        arr = json.loads(match.group())
    except json.JSONDecodeError as e:
        log.warning(f"Could not parse company list: {e}")
        return []
    return [str(c).lower().strip() for c in arr if str(c).strip()]


app = Flask(__name__, static_folder=None)


@app.route("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.route("/rejected_companies.json")
def rejected():
    return send_from_directory(ROOT, "rejected_companies.json")


@app.route("/refresh-applied", methods=["POST"])
def refresh_applied():
    try:
        existing = load_existing()
        found = scan_gmail_for_applications()
        added = [c for c in found if c not in existing]
        merged = existing | set(found)
        save_companies(merged)
        log.info(f"Refresh applied: scanned {len(found)} from Gmail, "
                 f"{len(added)} new, total {len(merged)}")
        return jsonify({
            "ok": True,
            "scanned": len(found),
            "added": added,
            "total": len(merged),
        })
    except Exception as e:
        log.exception("refresh-applied failed")
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    if not shutil.which("claude"):
        log.warning("`claude` CLI not on PATH — /refresh-applied will fail.")
    app.run(host="127.0.0.1", port=5000, debug=False)
