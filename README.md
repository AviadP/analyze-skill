# /analyze — OCS-CI Test Failure Analyzer

> A Claude Code skill that automates OCS-CI test failure triage from ReportPortal — from RP link to root cause classification in one command.

**Claude Code Skill** | Python 3.8+ (stdlib only, no pip install) | No virtual environment needed

## What it does

`/analyze` takes a ReportPortal test failure URL and runs a full triage pipeline: queries the RP API for test metadata and traceback, checks a local known-failures cache for instant deduplication, crawls the Magna logs directory (concurrent BFS with 20 workers) to locate must-gather archives and debug logs, downloads the relevant files (pod status, events, component-specific logs), classifies the failure into one of four categories, presents the evidence, and saves the result for future lookups.

### The 10-step workflow

1. **Validate input** — verify URL format and RP token file
2. **Query Report Portal** — extract test name, traceback, error message, and Magna logs URL
3. **Check known failures** — hash the traceback and look for a cached match
4. **Crawl Magna directory** — BFS crawl with 20 concurrent workers to build a file listing
5. **Download logs** — phase 1 (always: debug log, pod status, events) + phase 2 (conditional by subsystem)
6. **Analyze logs** — systematic review of each file for failure signals
7. **Classify failure** — framework issue / cluster unhealthy / infra / product bug
8. **Present results** — structured report with evidence and cluster state
9. **Confirm and save** — cache the classification to `known_failures.jsonl`
10. **Cleanup** — optionally delete downloaded logs

## Prerequisites

1. **RP API token** stored at `~/.ssh/report_portal`:
   ```bash
   echo '<your_report_portal_api_token>' > ~/.ssh/report_portal
   chmod 600 ~/.ssh/report_portal
   ```
   Override the path with: `export RP_TOKEN_FILE="/path/to/token"`

2. **Python 3.8+** — `rp_cli.py` uses only the standard library (`urllib`, `json`, `hashlib`, `concurrent.futures`). No `pip install` required.

3. **Claude Code** installed and configured.

## Installation

1. Clone the repository:
   ```bash
   git clone <repo_url> ~/my_claude_skills
   ```

2. Create a symlink so Claude Code discovers the skill:
   ```bash
   ln -s ~/my_claude_skills/skills/analyze ~/.claude/skills/analyze
   ```

3. Verify it works:
   ```
   /analyze help
   ```

## Usage

```
/analyze <report_portal_url>    # Full analysis
/analyze help                   # Show help
```

**Example RP URL format:**
```
https://reportportal.example.com/ui/#ocs/launches/all/12345/678/90/log
```

The URL must contain both `launches/` and `log` segments.

## How it works

1. **Query RP API** — extracts test name, status, ERROR-level logs (traceback), launch description (contains Magna logs URL), and attributes (ODF/OCP versions, platform, run ID)
2. **Known-failures cache** — normalizes the traceback (strips timestamps, UUIDs, pod name suffixes), computes a SHA-256 hash, and checks `~/memories/known_failures.jsonl` for a match
3. **Crawl Magna** — performs a concurrent BFS crawl (20 workers, configurable depth) of the HTTP directory listing to build a complete file tree
4. **Download & analyze** — downloads debug log, pod status (`all_-o_wide`), and events (`events_get`) first, then conditionally downloads subsystem-specific logs (NooBaa, Ceph, RGW, UI, PVC) based on keywords in the traceback
5. **Classify** — applies a priority-ordered ruleset: framework issue > cluster unhealthy > infra > product bug
6. **Cache** — saves the classification as a JSON line for future instant lookups

## Classification categories

| Category | Description |
|----------|-------------|
| **Test framework issue** | Exception originates in `conftest.py`, fixtures, or framework modules (e.g., `ImportError`, `AttributeError` in setup/teardown) |
| **Cluster unhealthy before test** | Pre-existing cluster problems — pods in `CrashLoopBackOff`, Ceph `HEALTH_ERR`, OSDs down, or `NotReady` nodes before the test started |
| **Infrastructure issue** | External service failures — network timeouts, DNS resolution errors, cloud provider API errors, certificate issues, or resource scheduling pressure |
| **Product bug** | Cluster was healthy, test logic is correct, but the product behaved incorrectly — wrong state after a valid operation, `AssertionError` in verification |

## Known failures cache

- **Location:** `~/memories/known_failures.jsonl`
- **Format:** one JSON object per line
- **Key:** SHA-256 hash of the normalized traceback (last 5 non-empty lines)
- **Deduplication normalization:** strips timestamps (`YYYY-MM-DDTHH:MM:SS`), UUIDs, and pod name suffixes (`-[a-z0-9]{5,10}$`) before hashing

**Example entry:**
```json
{
  "traceback_hash": "a1b2c3...",
  "classification": "product bug",
  "test_name": "test_create_pvc",
  "error_type": "AssertionError",
  "summary": "PVC stuck in Pending due to missing StorageClass",
  "date": "2026-02-18",
  "rp_url": "https://reportportal.example.com/ui/#ocs/launches/all/12345/678/90/log"
}
```

## Architecture

```
~/my_claude_skills/skills/analyze/
├── SKILL.md       # Workflow definition (consumed by Claude Code)
├── README.md      # This file
└── scripts/
    └── rp_cli.py  # Python CLI tool (stdlib only)
```

### `rp_cli.py` subcommands

| Subcommand | Usage | Description |
|------------|-------|-------------|
| *(default)* | `rp_cli.py "<rp_url>"` | Query RP API — returns JSON with test metadata, traceback, Magna URL |
| `crawl` | `rp_cli.py crawl [-d DEPTH] "<url>"` | Crawl HTTP directory listing — concurrent BFS, outputs `d`/`f` prefixed paths |
| `hash` | `echo "<traceback>" \| rp_cli.py hash` | Compute normalized traceback hash — reads from stdin, prints SHA-256 hex |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `RP_TOKEN_FILE` | `~/.ssh/report_portal` | Path to file containing RP API bearer token |

No other configuration is needed. The RP base URL is derived automatically from the provided ReportPortal link.
