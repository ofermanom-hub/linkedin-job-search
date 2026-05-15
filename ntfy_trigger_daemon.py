"""
ntfy → Mac → git push daemon for LinkedIn Job Search.

Long-polls a private ntfy.sh topic. When a valid trigger arrives:
  1. Run gmail_scan.run_full_scan() (Gmail MCP via `claude -p`).
  2. git add rejected_companies.json last_refresh.json
  3. git commit + git push  (so GitHub Pages sees the new files)

Designed to run under a LaunchAgent (~/Library/LaunchAgents/com.ofer.linkedin-ntfy.plist).
Uses stdlib only — no extra deps.

Triggering manually:
    curl -d '{"token":"<TOKEN>","ts":1}' https://ntfy.sh/<TOPIC>
"""

import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import gmail_scan

# --- Config (must match index.html consts) ---------------------------------
NTFY_TOPIC = "ofer-linkedin-refresh-f2d0fd01b8cfe3abfafd6b10"
SHARED_TOKEN = "5238ea0e39e54f5acd7b125e439b4994"

NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}/json"
COOLDOWN_SECONDS = 60
RECONNECT_INITIAL = 2
RECONNECT_MAX = 60
HTTP_READ_TIMEOUT = 90  # ntfy keepalive is ~30s; fail fast if the socket dies

ROOT = Path(__file__).parent
DAEMON_LOG = ROOT / "ntfy_daemon.log"

# --- Logging ---------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(DAEMON_LOG),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("ntfy-daemon")

_last_scan_ts = 0.0


def git(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=str(ROOT), timeout=120
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def commit_and_push() -> None:
    code, _, err = git("add", "rejected_companies.json", "last_refresh.json")
    if code != 0:
        raise RuntimeError(f"git add failed: {err}")

    code, out, _ = git("status", "--porcelain", "rejected_companies.json", "last_refresh.json")
    if code != 0 or not out:
        log.info("Nothing to commit (no diff in tracked refresh files).")
        return

    code, _, err = git(
        "commit", "-m", "auto: refresh applied (ntfy)",
    )
    if code != 0:
        raise RuntimeError(f"git commit failed: {err}")

    code, _, err = git("push")
    if code != 0:
        raise RuntimeError(f"git push failed: {err}")
    log.info("Pushed updated rejected_companies.json + last_refresh.json")


def handle_message(raw_message: str) -> None:
    global _last_scan_ts

    try:
        body = json.loads(raw_message)
    except json.JSONDecodeError:
        log.warning(f"Ignoring non-JSON message: {raw_message[:120]}")
        return

    if body.get("token") != SHARED_TOKEN:
        log.warning("Ignoring message: bad/missing token")
        return

    now = time.time()
    if now - _last_scan_ts < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - (now - _last_scan_ts))
        log.info(f"Cooldown active, skipping scan (wait {wait}s)")
        gmail_scan.write_failure(f"cooldown: try again in {wait}s")
        try:
            commit_and_push()
        except Exception as e:
            log.warning(f"Could not push cooldown marker: {e}")
        return

    _last_scan_ts = now
    log.info("Trigger accepted — running scan")
    try:
        result = gmail_scan.run_full_scan()
        log.info(f"Scan done: {result}")
    except Exception as e:
        log.exception("Scan failed")
        gmail_scan.write_failure(str(e))

    try:
        commit_and_push()
    except Exception as e:
        log.exception(f"git push failed: {e}")


def stream_once() -> None:
    """One subscription attempt. Raises on connection issues so caller can backoff."""
    log.info(f"Subscribing to {NTFY_URL}")
    req = urllib.request.Request(NTFY_URL, headers={"User-Agent": "linkedin-ntfy-daemon/1"})
    with urllib.request.urlopen(req, timeout=HTTP_READ_TIMEOUT) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("event") == "message":
                msg_body = evt.get("message", "")
                handle_message(msg_body)
            # event=keepalive / open are silently ignored


def main() -> int:
    log.info(f"Daemon starting (topic suffix …{NTFY_TOPIC[-8:]}, cooldown {COOLDOWN_SECONDS}s)")
    backoff = RECONNECT_INITIAL
    while True:
        try:
            stream_once()
            # Clean disconnect — reset backoff and reconnect immediately.
            backoff = RECONNECT_INITIAL
        except KeyboardInterrupt:
            log.info("Shutting down")
            return 0
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            log.warning(f"Connection lost: {e!r}; reconnect in {backoff}s")
        except Exception as e:
            log.exception(f"Unexpected error: {e!r}; reconnect in {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, RECONNECT_MAX)


if __name__ == "__main__":
    raise SystemExit(main())
