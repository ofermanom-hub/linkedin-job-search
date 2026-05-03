"""
LinkedIn Job Search → Gmail Digest
Run with: python3 linkedin_job_search.py
Requires: pip install anthropic
          ANTHROPIC_API_KEY env var set
          RECIPIENT_EMAIL env var set
          Claude Code CLI with Gmail MCP connected
"""

import anthropic
import html
import json
import logging
import os
import re
import stat
import tempfile
from datetime import date
from urllib.parse import urlparse

# ── LOGGING ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("job_search.log"),
    ],
)
log = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────

RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "Ofer.man.om@gmail.com")
LOCATION = "Tel Aviv"
TITLES = [
    "Senior Implementation Engineer",
    "Senior Onboarding Engineer",
    "Senior Technical Account Manager",
    "Senior Technical Consultant",
    "Senior CX Engineer",
    "Senior Consultant",
    "Enablement Team Lead",
    "Senior Enablement Manager",
    "Implementation Manager",
    "Senior Implementation Manager",
]
INDUSTRY_KEYWORDS = '("SaaS" OR "software" OR "high-tech" OR "B2B" OR "cloud" OR "IT management" OR "HR tech")'
TODAY = date.today().strftime("%B %d, %Y")

GMAIL_MCP = {
    "type": "url",
    "url": "https://gmailmcp.googleapis.com/mcp/v1",
    "name": "gmail-mcp",
}

_LINKEDIN_JOB_RE = re.compile(r'^https://(?:www\.)?linkedin\.com/jobs/view/\d+', re.IGNORECASE)


def safe_linkedin_url(url: str) -> str | None:
    """Return url only if it is a valid LinkedIn job URL, else None."""
    if url and _LINKEDIN_JOB_RE.match(url.split("?")[0].rstrip("/")):
        return url
    return None


# ── SEARCH QUERIES ────────────────────────────────────────────────────────────

def build_queries():
    queries = []
    for title in TITLES:
        queries.append(
            f'site:linkedin.com/jobs/view "{title}" "{LOCATION}" {INDUSTRY_KEYWORDS}'
        )
    queries.append(
        f'site:linkedin.com/jobs/view ("Implementation Manager" OR "Enablement Manager" OR "Enablement Lead" '
        f'OR "Senior Onboarding" OR "Senior TAM") "{LOCATION}" {INDUSTRY_KEYWORDS}'
    )
    return queries

# ── PROMPTS ───────────────────────────────────────────────────────────────────

SEARCH_SYSTEM = """You are a job search assistant.
When given a Google search query for LinkedIn jobs, use the web_search tool to run it.
Extract ALL job results found and return them as a JSON array with this structure:
[
  {
    "title": "Job Title",
    "company": "Company Name",
    "location": "City, Country",
    "url": "https://linkedin.com/jobs/view/XXXXXXX"
  }
]
Return ONLY the JSON array, no other text. If no results, return [].
"""

GMAIL_HISTORY_SYSTEM = """You are an assistant with access to Gmail via MCP.
Search the user's Gmail inbox for previous LinkedIn Job Digest emails sent by this script.
Use the search_threads tool with query: subject:"LinkedIn Job Digest"
Fetch up to 10 most recent threads. For each thread, read its content and extract every
linkedin.com/jobs/view/ URL found in the email body.
Return ONLY a JSON array of URL strings, e.g.:
["https://linkedin.com/jobs/view/123", "https://linkedin.com/jobs/view/456"]
If no previous digests found, return [].
"""

EMAIL_SYSTEM = """You are an assistant that sends emails via Gmail MCP.
You will receive an HTML email body and must send it using the Gmail MCP send_email tool.
Do not modify the content — just send it as-is.
"""

# ── GMAIL HISTORY ─────────────────────────────────────────────────────────────

def get_previously_seen_urls(client: anthropic.Anthropic) -> set:
    """Search previous digest emails in Gmail and return all job URLs found."""
    log.info("Checking Gmail for previously seen jobs...")
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            system=GMAIL_HISTORY_SYSTEM,
            mcp_servers=[GMAIL_MCP],
            messages=[{
                "role": "user",
                "content": "Search my Gmail for previous LinkedIn Job Digest emails and extract all job URLs."
            }],
        )
        for block in response.content:
            if block.type == "text":
                text = block.text.strip()
                match = re.search(r'\[.*?\]', text, re.DOTALL)
                if match:
                    raw_urls = json.loads(match.group())
                    normalized = set()
                    for url in raw_urls:
                        base = url.split("?")[0].rstrip("/")
                        if safe_linkedin_url(base):
                            normalized.add(base)
                    log.info(f"Found {len(normalized)} previously seen job URLs in Gmail history.")
                    return normalized
    except Exception as e:
        log.error(f"Could not fetch Gmail history: {e}")
    return set()

# ── HTML EMAIL TEMPLATE ───────────────────────────────────────────────────────

def job_row(j: dict, muted: bool = False) -> str:
    title    = html.escape(j.get("title", "—"))
    company  = html.escape(j.get("company", "—"))
    location = html.escape(j.get("location", "—"))
    url      = safe_linkedin_url(j.get("url", "")) or "#"
    style    = "color:#999;" if muted else ""
    link_color = "#999" if muted else "#0a66c2"
    return f"""
        <tr>
          <td style="padding:10px;border-bottom:1px solid #eee;{style}">{title}</td>
          <td style="padding:10px;border-bottom:1px solid #eee;{style}">{company}</td>
          <td style="padding:10px;border-bottom:1px solid #eee;{style}">{location}</td>
          <td style="padding:10px;border-bottom:1px solid #eee;">
            <a href="{url}" style="color:{link_color};text-decoration:none;">View Job</a>
          </td>
        </tr>"""

def build_email_html(new_jobs: list, seen_jobs: list) -> str:
    new_rows  = "".join(job_row(j) for j in new_jobs)
    seen_rows = "".join(job_row(j, muted=True) for j in seen_jobs)

    seen_section = ""
    if seen_jobs:
        seen_section = f"""
  <h3 style="color:#999;margin-top:40px;border-top:2px solid #eee;padding-top:20px;">
    Previously Seen Roles ({len(seen_jobs)})
  </h3>
  <p style="color:#aaa;font-size:13px;">These appeared in a previous digest — included for reference.</p>
  <table style="width:100%;border-collapse:collapse;">
    <thead>
      <tr style="background:#aaa;color:white;">
        <th style="padding:10px;text-align:left;">Title</th>
        <th style="padding:10px;text-align:left;">Company</th>
        <th style="padding:10px;text-align:left;">Location</th>
        <th style="padding:10px;text-align:left;">Link</th>
      </tr>
    </thead>
    <tbody>{seen_rows}</tbody>
  </table>"""

    return f"""
<html><body style="font-family:Arial,sans-serif;color:#333;max-width:800px;margin:auto;">
  <h2 style="color:#0a66c2;">LinkedIn Job Digest — {TODAY}</h2>
  <p>Senior tech roles in <strong>{html.escape(LOCATION)}</strong> matching your profile:</p>
  <p><em>Titles: Implementation Engineer · Onboarding Engineer · TAM · Technical Consultant · CX · Consultant</em></p>

  <table style="width:100%;border-collapse:collapse;margin-top:20px;">
    <thead>
      <tr style="background:#0a66c2;color:white;">
        <th style="padding:12px;text-align:left;">Title</th>
        <th style="padding:12px;text-align:left;">Company</th>
        <th style="padding:12px;text-align:left;">Location</th>
        <th style="padding:12px;text-align:left;">Link</th>
      </tr>
    </thead>
    <tbody>{new_rows if new_rows else '<tr><td colspan="4" style="padding:20px;color:#999;">No new roles found today.</td></tr>'}</tbody>
  </table>

  {seen_section}

  <p style="margin-top:30px;font-size:12px;color:#999;">
    {len(new_jobs)} new · {len(seen_jobs)} previously seen · Generated by Claude Code Job Search
  </p>
</body></html>
"""

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    client = anthropic.Anthropic()
    all_jobs = []
    seen_urls = set()

    log.info(f"Searching LinkedIn for {len(TITLES)} job titles in {LOCATION}...")

    queries = build_queries()
    for i, query in enumerate(queries, 1):
        log.info(f"[{i}/{len(queries)}] {query[:80]}...")
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1000,
                system=SEARCH_SYSTEM,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": f"Search for jobs using this query: {query}"}],
            )
            for block in response.content:
                if block.type == "text":
                    text = block.text.strip()
                    if text.startswith("["):
                        try:
                            jobs = json.loads(text)
                            for job in jobs:
                                url = job.get("url", "")
                                if not safe_linkedin_url(url):
                                    log.warning(f"Skipping invalid URL: {url!r}")
                                    continue
                                if url not in seen_urls:
                                    seen_urls.add(url)
                                    all_jobs.append(job)
                                    log.info(f"  + {job.get('title')} @ {job.get('company')}")
                        except json.JSONDecodeError:
                            pass
        except Exception as e:
            log.error(f"Search error: {e}")

    log.info(f"Total unique jobs found: {len(all_jobs)}")
    if not all_jobs:
        log.info("No jobs found.")
        return

    previously_seen = get_previously_seen_urls(client)
    new_jobs, seen_jobs = [], []
    for job in all_jobs:
        base_url = job.get("url", "").split("?")[0].rstrip("/")
        (seen_jobs if base_url in previously_seen else new_jobs).append(job)

    log.info(f"{len(new_jobs)} new jobs · {len(seen_jobs)} previously seen")

    html_body = build_email_html(new_jobs, seen_jobs)
    subject = f"LinkedIn Job Digest — {len(new_jobs)} New Roles in {LOCATION} ({TODAY})"

    log.info(f"Sending email to {RECIPIENT_EMAIL}...")
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=EMAIL_SYSTEM,
            mcp_servers=[GMAIL_MCP],
            messages=[{
                "role": "user",
                "content": (
                    f"Send an email using Gmail MCP:\n"
                    f"To: {RECIPIENT_EMAIL}\n"
                    f"Subject: {subject}\n"
                    f"Body (HTML): {html_body}\n\nUse the send_email tool now."
                ),
            }],
        )
        for block in response.content:
            if block.type == "text":
                log.info(f"Response: {block.text[:200]}")
        log.info("Email sent!")
    except Exception as e:
        log.error(f"Email failed: {e}")
        fd, path = tempfile.mkstemp(suffix=".html", prefix="job_digest_")
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w") as f:
            f.write(html_body)
        log.info(f"Preview saved to {path}")


if __name__ == "__main__":
    main()
