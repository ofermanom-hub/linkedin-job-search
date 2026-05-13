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

from flask import Flask, Response, jsonify, send_from_directory

ROOT = Path(__file__).parent
REJECTED_FILE = ROOT / "rejected_companies.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

CLAUDE_SCAN_PROMPT = """Find every company I'm in active hiring conversations with in the
LAST 6 MONTHS — including applications, recruiter outreach, interviews, and home tasks.

HARD LIMITS:
  - Call mcp__claude_ai_Gmail__search_threads AT MOST 2 TIMES.
  - Use pageSize: 50 (the max).
  - DO NOT call get_thread.
  - DO NOT paginate beyond the first page.

SEARCH 1 — application confirmations:
  newer_than:6m (subject:"thanks for applying" OR subject:"thank you for applying"
    OR subject:"we got it" OR subject:"application received"
    OR subject:"we received your application" OR subject:"application submitted"
    OR "thanks for your interest in" OR "we've received your application")

SEARCH 2 — recruiter / interview / home-task signals:
  newer_than:6m (subject:"opportunity at" OR subject:"opportunity with"
    OR subject:"interview with" OR subject:"interview at" OR subject:"video interview"
    OR subject:"phone interview" OR subject:"home task" OR subject:"take-home"
    OR subject:"home assignment" OR subject:"next steps" OR subject:"next step"
    OR subject:"regarding your" OR subject:"position at" OR subject:"role at")

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


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.route("/refresh-applied-stream")
def refresh_applied_stream():
    """SSE stream of progress events while claude scans Gmail."""

    def gen():
        claude_bin = shutil.which("claude")
        if not claude_bin:
            yield _sse("error", {"message": "`claude` CLI not on PATH"})
            return

        yield _sse("progress", {"step": 0, "message": "Spawning claude…"})

        proc = subprocess.Popen(
            [
                claude_bin,
                "-p", CLAUDE_SCAN_PROMPT,
                "--output-format", "stream-json",
                "--verbose",
                "--permission-mode", "bypassPermissions",
                "--model", "claude-sonnet-4-6",
                "--max-turns", "12",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(ROOT),
        )

        tool_count = 0
        search_count = 0
        read_count = 0
        final_text = ""

        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = evt.get("type")
                if t == "assistant":
                    for block in evt.get("message", {}).get("content", []):
                        btype = block.get("type")
                        if btype == "tool_use":
                            tool_count += 1
                            name = block.get("name", "")
                            inp = block.get("input", {}) or {}
                            if "search" in name.lower():
                                search_count += 1
                                q = str(inp.get("query") or inp)[:80]
                                msg = f"Searching Gmail (#{search_count}): {q}"
                            elif "thread" in name.lower() or "message" in name.lower():
                                read_count += 1
                                msg = f"Reading thread #{read_count}"
                            else:
                                msg = f"Tool: {name}"
                            yield _sse("progress", {
                                "step": tool_count,
                                "searches": search_count,
                                "reads": read_count,
                                "message": msg,
                            })
                        elif btype == "text":
                            final_text = block.get("text", "") or final_text
                elif t == "result":
                    final_text = evt.get("result") or final_text
            proc.wait(timeout=CLAUDE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            yield _sse("error", {"message": "Scan timed out"})
            return
        except GeneratorExit:
            # Browser disconnected — kill the subprocess so it doesn't keep running.
            log.info("Client disconnected, killing claude subprocess")
            proc.kill()
            return
        except Exception as e:
            proc.kill()
            yield _sse("error", {"message": str(e)})
            return

        try:
            CLAUDE_LOG.write_text(
                f"--- exit {proc.returncode} ---\n[final_text]\n{final_text}\n"
                f"[stderr]\n{proc.stderr.read() if proc.stderr else ''}\n"
            )
        except Exception:
            pass

        if proc.returncode != 0:
            yield _sse("error", {"message": f"claude exit {proc.returncode}"})
            return

        match = re.search(r"\[.*\]", final_text, re.DOTALL)
        companies: list[str] = []
        if match:
            try:
                arr = json.loads(match.group())
                companies = [str(c).lower().strip() for c in arr if str(c).strip()]
            except json.JSONDecodeError:
                pass

        existing = load_existing()
        added = [c for c in companies if c not in existing]
        merged = existing | set(companies)
        save_companies(merged)
        log.info(f"Stream done: {len(companies)} scanned, {len(added)} new, "
                 f"{len(merged)} total ({tool_count} tool calls)")
        yield _sse("done", {
            "scanned": len(companies),
            "added": added,
            "total": len(merged),
            "tool_calls": tool_count,
        })

    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    if not shutil.which("claude"):
        log.warning("`claude` CLI not on PATH — /refresh-applied will fail.")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
