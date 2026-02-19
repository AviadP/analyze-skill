---
name: analyze
description: >
  Analyze an OCS-CI test failure from ReportPortal. Extracts test metadata,
  checks known failures cache, crawls Magna logs, downloads and analyzes
  relevant files, classifies the failure, and suggests next steps.
  Use when the user provides a ReportPortal test failure URL.
allowed_prompts:
  - tool: Bash
    prompt: "run rp_cli.py to query Report Portal API"
  - tool: Bash
    prompt: "run rp_cli.py crawl to crawl Magna directory"
  - tool: Bash
    prompt: "run rp_cli.py hash to compute traceback hash"
  - tool: Bash
    prompt: "download logs from magna with curl"
  - tool: Bash
    prompt: "extract must-gather tarball with tar"
  - tool: Bash
    prompt: "create working directory"
  - tool: Bash
    prompt: "search through downloaded log files"
  - tool: Bash
    prompt: "save classification to known_failures.jsonl"
  - tool: Bash
    prompt: "clean up downloaded logs"
  - tool: Bash
    prompt: "check if token file exists"
  - tool: Bash
    prompt: "run rp_cli.py decide to submit decision to Report Portal"
---

# Analyze — OCS-CI Test Failure Analysis

## Overview

Automate the full triage drill for a failed OCS-CI regression test:
RP link → metadata extraction → known-failure check → log download →
root cause classification → suggested next step.

## Invocation

```
/analyze <report_portal_url>
```

## Skill Directory

This skill is self-contained under its directory. All paths below are relative
to the skill root: `~/my_claude_skills/skills/analyze/`

| File | Purpose |
|------|---------|
| `SKILL.md` | This workflow definition |
| `scripts/rp_cli.py` | Python CLI — RP API queries + directory crawler (stdlib only) |
| `lessons/{test_name}.md` | Per-test lessons learned from past analyses |

## External Data

| File | Location | Purpose |
|------|----------|---------|
| `known_failures.jsonl` | `~/memories/known_failures.jsonl` | Lean dedup cache — traceback hash → classification |

## Prerequisites

1. **RP API token** stored in a file (default `~/.ssh/report_portal`):
   ```bash
   echo '<your_report_portal_api_token>' > ~/.ssh/report_portal
   chmod 600 ~/.ssh/report_portal
   ```
   Override location with: `export RP_TOKEN_FILE="$HOME/.ssh/report_portal"`

2. **Python 3.8+** — no virtual env needed, `rp_cli.py` uses stdlib only.

## Help

If the user runs `/analyze` with no arguments, or `/analyze help`, display:

```
/analyze — OCS-CI Test Failure Analyzer

Usage:
  /analyze <report_portal_url>
  /analyze help

Prerequisites:
  1. RP token file at ~/.ssh/report_portal (or set RP_TOKEN_FILE env var)
     Create with: echo '<token>' > ~/.ssh/report_portal && chmod 600 ~/.ssh/report_portal
  2. Python 3.8+ (stdlib only, no venv needed)

What it does:
  - Queries Report Portal API for test metadata and traceback
  - Checks known failures cache for quick matches
  - Crawls Magna logs directory for test-specific logs
  - Downloads and analyzes debug logs, pod status, events
  - Classifies failure: framework issue / cluster unhealthy / infra / product bug
  - Saves classification for future quick lookups
```

Then stop — do not proceed with analysis.

---

## Workflow

### Step 1: Validate Input

1. If no argument provided or argument is `help`, show the help text above and stop.

2. The argument is a Report Portal URL. It MUST contain both `launches/` and `log`.
   If not, stop and tell the user the expected format.

3. Verify the token file exists:
   ```bash
   test -f "${RP_TOKEN_FILE:-$HOME/.ssh/report_portal}" || echo "Token file missing"
   ```

4. Create working directory:
   ```bash
   mkdir -p ~/ocs-log-analysis
   ```

### Input Sanitization

Before using any API-sourced value (`test_name`, `run_id`, `filename`,
`cluster_name`) in a shell command:

1. **Validate characters** — reject values containing shell metacharacters
   (`;`, `|`, `&`, `$`, `` ` ``, `(`, `)`, `{`, `}`, `<`, `>`, `\n`) or
   path traversal sequences (`..`, leading `/`).
2. **Quote all interpolated values** in double quotes when used in bash commands.
3. **For filenames** — use only the basename (strip any directory components).

If any value fails validation, stop and warn the user before proceeding.

### Step 2: Query Report Portal

Run the CLI tool (path relative to this skill's directory):

```bash
python3 ~/my_claude_skills/skills/analyze/scripts/rp_cli.py "<rp_url>"
```

This outputs JSON to stdout. Parse it and extract:
- `test_name` — the test method name
- `traceback` — full ERROR-level log output
- `error_message` — last line of traceback (the exception)
- `logs_url_root` — Magna base URL for this run's logs
- `cluster_name` — cluster identifier from the Magna path
- `status` — test status (FAILED, etc.)
- `attributes` — dict with `odf_version`, `ocp_version`, `platform`, etc.
- `attributes.run_id` — **KEY**: this ID connects Magna directories together
- `launch_description` — raw launch description text

**IMPORTANT**: The `run_id` from `attributes` is the directory connector on Magna:
- `failed_testcase_ocs_logs_{run_id}/` — must-gather tarballs
- `ocs-ci-logs-{run_id}/` — pytest debug logs

If `status` is not `FAILED`, inform the user and ask whether to continue.

If `logs_url_root` is empty, warn that Magna logs are unavailable and proceed
with traceback-only analysis (skip Steps 4-5).

### Step 3: Check Known Failures Cache

1. Compute a traceback hash using the built-in `hash` subcommand:
   ```bash
   echo "<traceback_string>" | python3 ~/my_claude_skills/skills/analyze/scripts/rp_cli.py hash
   ```
   This normalizes the traceback (strips timestamps, UUIDs, pod suffixes)
   and returns a SHA-256 hex digest.

2. Read `~/memories/known_failures.jsonl` (if it exists).
   Search for a line where `traceback_hash` matches.

3. If a match is found:
   - Display the cached classification and summary to the user
   - Ask: **"This matches a known failure. Proceed with full analysis anyway?"**
   - If user says no → stop and show the cached result
   - If user says yes → continue to Step 4

4. If NO hash match, check for per-test lessons learned:
   ```bash
   cat ~/my_claude_skills/skills/analyze/lessons/{test_name}.md 2>/dev/null
   ```
   If a lessons file exists, read it before proceeding. It contains:
   - What the test does and its key call chain
   - Known timing sensitivities and platform-specific behavior
   - What to look for in logs (speeds up Step 6)
   - Past failure history (different failure modes of the same test)

   Use this context to guide log analysis, but still perform the full workflow.

### Step 4: Crawl Magna Directory

**Single crawl, then search locally.** This avoids multiple network round-trips.

1. Crawl the full logs root once and save the listing:
   ```bash
   python3 ~/my_claude_skills/skills/analyze/scripts/rp_cli.py crawl -d 6 "<logs_url_root>" \
     > ~/ocs-log-analysis/{test_name}/magna_listing.txt
   ```

2. Use `run_id` from Step 2 to narrow down to the right batch:
   ```bash
   grep "{run_id}" ~/ocs-log-analysis/{test_name}/magna_listing.txt
   ```
   This finds both `failed_testcase_ocs_logs_{run_id}/` and `ocs-ci-logs-{run_id}/`.

3. **Primary path** — Look for test-specific must-gather:
   ```
   failed_testcase_ocs_logs_{run_id}/{test_name}_ocs_logs/{cluster_name}/
   ```

4. **Fallback path** (fixture failures) — If the test name is NOT found in any
   `failed_testcase` directory, the test likely failed in a fixture (e.g.,
   `health_checker`). In this case:
   - Look for `ceph_health_recover_*` entries in the same `run_id` batch
   - Use the closest `ceph_health_recover_*_ocs_logs/{cluster_name}/` must-gather
   - The debug log is still available at:
     ```
     ocs-ci-logs-{run_id}/tests/.../test_name.py/TestClass/test_method/logs
     ```

5. For the must-gather path, crawl it for the full file listing (if not already
   covered by the initial deep crawl):
   ```bash
   grep "<target_path>" ~/ocs-log-analysis/{test_name}/magna_listing.txt
   ```

6. If no matching directory at all, warn the user and proceed with
   traceback-only analysis.

### Step 5: Download Logs

Create a local directory for this analysis:
```bash
mkdir -p ~/ocs-log-analysis/{test_name}
```

Use the file listing from Step 4 to locate files. Download with:
```bash
curl -k -s -o ~/ocs-log-analysis/{test_name}/{filename} "<file_url>"
```

#### Phase 1 — Always download (quick triage):

These three files are sufficient for initial classification in most cases.

1. **Python debug log** — the `.log` file in the test-specific directory root.
   This is the pytest output containing the full traceback with timestamps.

2. **Pod status** — search the file listing for a path matching:
   `ocs_must_gather/quay*/namespaces/openshift-storage/oc_output/all_-o_wide`
   Shows all pods, their status, restart counts, node placement.

3. **Events** — search the file listing for a path matching:
   `ocs_must_gather/quay*/namespaces/openshift-storage/oc_output/events_get`
   Kubernetes events sorted by time — scheduling failures, volume issues, etc.

#### Phase 2 — Conditional downloads (based on failure area):

Examine the `traceback`, `error_message`, and `test_name` for keywords.
Download additional logs only for the relevant subsystem.

| Keywords found in traceback/test_name | Files to download |
|---------------------------------------|-------------------|
| `noobaa`, `mcg`, `bucket`, `backingstore`, `namespacestore` | `noobaa-default-backing-store.yaml`, `noobaa-core-0` pod log, `noobaa-endpoint` pod logs |
| `ceph`, `rbd`, `cephfs`, `pool`, `osd`, `mon`, `mds` | `ceph_health_detail`, `ceph_status`, `ceph_osd_tree`, `ceph_df` |
| `rgw`, `object`, `s3` | `ceph_status`, RGW pod logs |
| `ui/` in test path or `selenium`, `webdriver` | Contents of `ui_test_logs/` directory (screenshots, DOM dumps) |
| `pvc`, `pv`, `volume`, `storageclass` | `oc_output/pv`, `oc_output/pvc`, `oc_output/sc` |

For must-gather tarballs (`.tar.gz`), extract after download:
```bash
tar xzf ~/ocs-log-analysis/{test_name}/{archive} -C ~/ocs-log-analysis/{test_name}/
```

### Step 6: Analyze Logs

Work through each downloaded file systematically. For each file, read the
relevant sections and extract signals.

#### 6a. Debug log (Python test log)

**Start from the tail** — debug logs can be 1M+ lines. Read the last 5000 lines
first to find the traceback, then work backwards only if needed.

1. Read tail of the file first (last ~5000 lines)
2. Search for `FAILED` or `ERROR` near the end
3. Extract the full traceback + 30 lines of context before it
4. Identify the last successful operation before the failure
5. Note any warnings or retries that preceded the failure
6. Check timestamps — how long did the test run before failing?
7. Check for `MG collection is skipped` — indicates cluster was already degraded

#### 6b. Pod status (`all_-o_wide`)

Check for:
- Pods NOT in `Running` or `Completed` status
- High restart counts (> 2)
- `OOMKilled` in status
- `CrashLoopBackOff` status
- `Pending` pods (scheduling issues)
- `Init:Error` or `Init:CrashLoopBackOff`
- Pods with AGE much younger than others (recent restarts)

#### 6c. Events (`events_get`)

Check for:
- `FailedScheduling` — resource pressure or node affinity issues
- `FailedAttachVolume` / `FailedMount` — storage issues
- `Evicted` — resource pressure
- `BackOff` — container crash loops
- `FailedCreate` — resource creation failures
- Events timestamped BEFORE the test started (pre-existing cluster issues)

#### 6d. Component-specific logs (if downloaded)

**NooBaa logs:**
- Search for `FATAL`, `Error`, `panic`
- Check `noobaa-core` for connection failures to backing stores
- Check endpoint logs for S3 operation errors

**Ceph logs:**
- `ceph_health_detail`: Look for `HEALTH_WARN` or `HEALTH_ERR` and their detail
- `ceph_status`: Check OSD counts (up vs total), PG state, IO summary
- `ceph_osd_tree`: Look for `down` OSDs
- Look for `slow ops`, `blocked requests`

**UI logs:**
- Check screenshots for visual state at failure time
- Check DOM dumps for element presence/absence

#### 6e. Iterative deepening

If the above analysis is inconclusive:
1. Review the file listing from Step 4
2. Identify additional logs that might be relevant
3. Download and analyze them
4. Repeat until a classification can be made with confidence

### Step 7: Classify Failure

Apply classifications in priority order. **First match wins.**

| Priority | Classification | Signals |
|----------|---------------|---------|
| 1 | **Test framework issue** | Exception originates in `conftest.py`, `testlib.py`, or fixture code. `ImportError`, `AttributeError`, `TypeError` in framework modules. Test setup/teardown failure, not the test body. |
| 2 | **Cluster unhealthy before test** | Pods in `CrashLoopBackOff` or `OOMKilled` with timestamps BEFORE the test started. Ceph `HEALTH_ERR`. OSDs down. Node `NotReady`. Events showing pre-existing issues. |
| 3 | **Infrastructure issue** | Network timeouts to external services (quay.io, brew, etc.). DNS resolution failures. Cloud provider API errors (AWS/Azure/GCP). `FailedScheduling` due to resource pressure. Certificate errors to external endpoints. |
| 4 | **Product bug** | Everything else — the cluster was healthy, the test logic is correct, but the product behaved incorrectly. Wrong behavior after a valid operation. `AssertionError` in a verification step. Unexpected state in a resource. |

**CRITICAL RULES:**
- NEVER suggest "increase timeout" as a fix. Instead, analyze WHY the operation
  was slow — was it a real slowness, a hung process, or a resource starvation?
- If the traceback shows a timeout, check pod status and events to determine
  whether the timeout was caused by a cluster issue (classification 2-3) or
  a genuine product slowness (classification 4).
- If unsure between two classifications, present both with evidence and let
  the user decide.

### Step 8: Present Results

Format the analysis as:

```
## Analysis Results

**Test**: {test_name}
**Version**: ODF {odf_version} / OCP {ocp_version}
**Platform**: {platform}
**RP Link**: {original_rp_url}

### Classification: {classification}

### Root Cause
{2-3 sentence explanation of what happened and why}

### Evidence
1. {file_path}:{line} — {relevant log excerpt}
2. {file_path}:{line} — {relevant log excerpt}
...up to 5 evidence items

### Cluster State at Failure Time
- **Pods**: {summary — e.g., "all Running" or "noobaa-core-0 CrashLoopBackOff (12 restarts)"}
- **Ceph**: {status — e.g., "HEALTH_OK" or "HEALTH_WARN: 1 OSD down"}
- **Events**: {summary — e.g., "no anomalies" or "3 FailedScheduling events in last hour"}
```

If classification is **product bug**, additionally present:

```
### Reproduction Steps
1. {step-by-step from the test logic}
2. ...

### Suggested Bug Report
**Title**: [ODF {version}] {one-line summary}
**Component**: {Ceph | NooBaa | OCS Operator | UI | MCG}
**Severity**: {based on impact}
**Description**: {2-3 sentences}
```

If classification is **cluster unhealthy before test**, additionally present:

```
### Cluster Issue Details
- **Affected components**: {list}
- **Since when**: {timestamp if determinable from events}
- **Recommendation**: Check cluster health, this test failure is a symptom not a cause
```

### Step 9: Confirm and Save

1. Ask the user: **"Does this classification look correct?"**

2. If the user confirms (or adjusts the classification):
   - Build a cache entry:
     ```json
     {
       "traceback_hash": "{hash from Step 3}",
       "classification": "{confirmed classification}",
       "test_name": "{test_name}",
       "error_type": "{exception class name}",
       "summary": "{one-line root cause summary}",
       "date": "{today YYYY-MM-DD}",
       "rp_url": "{original RP URL}"
     }
     ```
   - Append to `~/memories/known_failures.jsonl`:
     ```bash
     echo '{json_entry}' >> ~/memories/known_failures.jsonl
     ```

3. If the user disagrees with the classification:
   - Ask what the correct classification is
   - Update the entry accordingly
   - Save the corrected entry

4. Update (or create) the per-test lessons file:
   ```
   ~/my_claude_skills/skills/analyze/lessons/{test_name}.md
   ```
   Include only operationally useful information:
   - What the test does and its key call chain
   - Known timing sensitivities or platform-specific behavior
   - What to look for in logs (specific files, patterns, keywords)
   - Append a row to the "Past failures" table with date, classification, one-line summary

   Keep it concise. No general insights — only things that speed up the next analysis of this same test.

### Step 9b: Submit Decision to Report Portal

After the user confirms (or adjusts) the classification in Step 9, submit the
decision to Report Portal via the API.

1. **Map classification to RP defect type:**

   | Skill Classification | issue_type arg | RP Locator |
   |---|---|---|
   | Product bug | `product_bug` | PB001 |
   | Test framework issue | `automation_bug` | AB001 |
   | Cluster unhealthy before test | `system_issue` | SI001 |
   | Infrastructure issue | `system_issue` | SI001 |

2. **Build the comment** from the analysis summary (root cause + evidence,
   keep it concise — 2-3 sentences max).

3. **If Automation Bug**: ask the user for a fix PR URL or GitHub issue URL.
   This becomes `--link-url` and `--link-id`.

4. **If Product Bug**: ask the user for a Jira defect URL.
   This becomes `--link-url` and `--link-id`.

5. **If System Issue**: show a short summary, ask for final approval before
   submitting. No external link needed.

6. **Submit the decision:**
   ```bash
   python3 ~/my_claude_skills/skills/analyze/scripts/rp_cli.py decide \
     "<base_url>" "<test_item_id>" "<issue_type>" \
     --comment "<comment>" [--link-url "<url>" --link-id "<id>"]
   ```
   `base_url` and `test_item_id` are already known from Step 2.

7. **Confirm** the submission succeeded by checking the JSON output.
   If it fails, show the error and let the user decide whether to retry.

### Step 10: Cleanup

1. Ask the user: **"Delete downloaded logs from ~/ocs-log-analysis/{test_name}/?"**

2. If yes:
   ```bash
   rm -rf ~/ocs-log-analysis/{test_name}
   ```

3. If no, inform the user where the logs are stored for manual review.

---

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| Test status is PASSED | Inform user, ask if they still want to analyze |
| No Magna logs available (`logs_url_root` empty) | Analyze traceback only, skip Steps 4-5 |
| Test not found in any `failed_testcase` dir | Warn, analyze traceback only |
| Traceback is empty | Check debug log for errors, fall back to pod/event analysis |
| Multiple matching `failed_testcase` dirs | Use the first match |
| Crawl returns empty | URL may be wrong or server down — fall back to `curl -k -s` and parse HTML manually |
| `known_failures.jsonl` doesn't exist | Skip cache check, create file on first save |

## Quality Rules

- Always read the actual logs before classifying. Never guess from test name alone.
- Provide specific evidence (file + line/excerpt) for every claim.
- If a timeout caused the failure, explain what was being waited on and why it was slow.
- Never suggest increasing timeouts or adding retries as a fix.
- When presenting Ceph status, always mention OSD count and health status together.
