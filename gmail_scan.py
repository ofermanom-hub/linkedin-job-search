"""
Gmail-MCP scan logic for LinkedIn Job Search.

Shared by:
- server.py (local Flask, /refresh-applied + SSE)
- ntfy_trigger_daemon.py (cloud trigger via ntfy → git push)

The actual scan shells out to the `claude` CLI so the user's configured
Gmail MCP is used.
"""

import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).parent
REJECTED_FILE = ROOT / "rejected_companies.json"
LAST_REFRESH_FILE = ROOT / "last_refresh.json"
CLAUDE_LOG = ROOT / "claude_scan.log"

log = logging.getLogger(__name__)

CLAUDE_SCAN_PROMPT = """Find every company I'm in active hiring conversations with in the
LAST 6 MONTHS — including applications, recruiter outreach, interviews, and home tasks.

HARD LIMITS:
  - Call mcp__claude_ai_Gmail__search_threads AT MOST 3 TIMES.
  - Use pageSize: 50 (the max).
  - DO NOT call get_thread.
  - DO NOT paginate beyond the first page.

SEARCH 1 — application confirmations AND rejections (body-level match):
  Many rejection emails have neutral subjects ("Update on your application")
  but the BODY contains "Thanks for applying to the <role> role at <Company>".
  Drop subject: prefix so Gmail full-text searches body too.
  newer_than:6m ("thanks for applying to" OR "thank you for applying to"
    OR "thanks for your interest in" OR "thank you for your interest in"
    OR "we received your application" OR "we've received your application"
    OR "application received" OR "application submitted"
    OR subject:"thanks for applying" OR subject:"thank you for applying"
    OR subject:"we got it" OR subject:"application received")

SEARCH 2 — recruiter / interview / home-task signals:
  newer_than:6m (subject:"opportunity at" OR subject:"opportunity with"
    OR subject:"interview with" OR subject:"interview at" OR subject:"video interview"
    OR subject:"phone interview" OR subject:"home task" OR subject:"take-home"
    OR subject:"home assignment" OR subject:"next steps" OR subject:"next step"
    OR subject:"regarding your" OR subject:"position at" OR subject:"role at")

SEARCH 3 — rejection signals (subject + body):
  Catches rejections whose subject never says "thanks for applying".
  newer_than:6m ("decided to move forward with other candidates"
    OR "decided to move forward with candidates"
    OR "we've decided to move forward" OR "we have decided to move forward"
    OR "move forward with candidates whose"
    OR "not moving forward" OR "won't be moving forward"
    OR "closer fit" OR "best in your search" OR "best of luck in your search"
    OR "decided not to proceed" OR "pursue other candidates"
    OR subject:"update on your application" OR subject:"application update"
    OR subject:"regarding your application" OR subject:"your application at"
    OR subject:"your application to" OR subject:"application status")

For EACH returned thread, extract the company from subject + sender:

  1. Subject pattern "...at <Company>", "...with <Company>", "...| <Company>",
     "<Role> at <Company>", "<Role>| <Company>"
     → "Senior Technical Account Manager opportunity at Kissterra" → "kissterra"
     → "Technical Account Manager- Home Task| Kissterra" → "kissterra"

  2. Sender domain (strip ALL noise — subdomains AND compound vendor suffixes):
     no-reply@careers.evinced.com               → "evinced"
     steram@kissterra.com                       → "kissterra"
     notifications@kissterra.comeet-notifications.com → "kissterra"
       (strip ".comeet-notifications.com" / ".greenhouse-mail.io" /
        ".myworkday.com" / ".lever.co" / ".ashbyhq.com" first;
        then take the leftmost meaningful segment)
     jobs@hire.acme.io                          → "acme"

  3. NEVER use as the company: LinkedIn, Greenhouse, Lever, Workday, Ashby,
     SmartRecruiters, Comeet, SparkHire, Spark Hire, Indeed, Comeet-Notifications,
     Scheduler, Notifications.

  4. Skip recruiter-agency-only threads where no actual employer name appears.

Combine results from BOTH searches, deduplicate, alphabetise.

Return ONLY a JSON array of lowercase company names. Strip suffixes ("Inc", "Ltd",
"Technologies", ".com"). Example:
  ["acme", "evinced", "kissterra", "soylent"]

If empty, return [].
Output ONLY the JSON array — no prose, no markdown fences.
"""

CLAUDE_TIMEOUT_SECONDS = 600


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


def save_last_refresh(payload: dict) -> None:
    LAST_REFRESH_FILE.write_text(json.dumps(payload, indent=2) + "\n")


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


def run_full_scan() -> dict:
    """End-to-end: scan Gmail, merge into rejected list, write both files.

    Returns the same dict that gets written to last_refresh.json.
    """
    existing = load_existing()
    found = scan_gmail_for_applications()
    added = [c for c in found if c not in existing]
    merged = existing | set(found)
    save_companies(merged)
    payload = {
        "ok": True,
        "ts": int(time.time()),
        "scanned": len(found),
        "added": added,
        "total": len(merged),
    }
    save_last_refresh(payload)
    log.info(
        f"run_full_scan: {len(found)} from Gmail, {len(added)} new, total {len(merged)}"
    )
    return payload


def write_failure(error: str) -> dict:
    payload = {"ok": False, "ts": int(time.time()), "error": error[:500]}
    save_last_refresh(payload)
    return payload
