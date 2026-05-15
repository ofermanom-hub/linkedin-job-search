"""
Flask backend for LinkedIn Job Search (LOCAL mode).
- Serves index.html and rejected_companies.json
- POST /refresh-applied: scans Gmail (past 6 months) via Gmail MCP, updates JSON.
- GET  /refresh-applied-stream: SSE-streamed progress version.

Cloud mode (GitHub Pages) uses ntfy_trigger_daemon.py instead — see that file.

Run:
    pip install flask
    python3 server.py

Open: http://127.0.0.1:5000/
"""

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from flask import Flask, Response, jsonify, send_from_directory

import gmail_scan
from gmail_scan import (
    CLAUDE_SCAN_PROMPT,
    CLAUDE_TIMEOUT_SECONDS,
    load_existing,
    save_companies,
    save_last_refresh,
)

ROOT = Path(__file__).parent
CLAUDE_LOG = gmail_scan.CLAUDE_LOG

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


app = Flask(__name__, static_folder=None)


@app.route("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.route("/rejected_companies.json")
def rejected():
    return send_from_directory(ROOT, "rejected_companies.json")


@app.route("/last_refresh.json")
def last_refresh():
    return send_from_directory(ROOT, "last_refresh.json")


@app.route("/refresh-applied", methods=["POST"])
def refresh_applied():
    try:
        payload = gmail_scan.run_full_scan()
        return jsonify(payload)
    except Exception as e:
        log.exception("refresh-applied failed")
        gmail_scan.write_failure(str(e))
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
        # Also write last_refresh.json so cloud-mode pages would see the update
        # if this same repo were pushed after a local scan.
        import time as _t
        save_last_refresh({
            "ok": True,
            "ts": int(_t.time()),
            "scanned": len(companies),
            "added": added,
            "total": len(merged),
        })
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
