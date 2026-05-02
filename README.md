# LinkedIn Job Search Digest

Searches LinkedIn for senior Implementation / TAM / CX / Enablement roles in Tel Aviv and emails a formatted digest via Gmail MCP.

## Requirements

```bash
pip install anthropic
```

Set your API key:
```bash
export ANTHROPIC_API_KEY=your_key_here
```

## Run

```bash
python3 linkedin_job_search.py
```

## How it works

1. Builds Google `site:linkedin.com/jobs/view` queries for each target job title
2. Uses Claude (`claude-sonnet-4-6`) with the built-in `web_search` tool to run each query and extract job listings
3. Deduplicates results across queries
4. Sends a formatted HTML digest email to the configured recipient via Gmail MCP
