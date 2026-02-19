#!/usr/bin/env python3
"""
CLI tool to query Report Portal API and crawl Magna log directories.

Usage:
    python3 rp_cli.py "<report_portal_url>"       # Query RP, output JSON
    python3 rp_cli.py crawl [-d DEPTH] "<url>"     # Crawl directory listing
    python3 rp_cli.py hash                          # Hash traceback (stdin)
    python3 rp_cli.py decide <base_url> <item_id> <issue_type> [--comment "..."]
                                                    # Set defect type on item

Environment variables:
    RP_TOKEN_FILE - Path to file containing RP API token
                    (default: ~/.ssh/report_portal)
    RP_PROJECT   - Report Portal project name (default: ocs)

"""

import hashlib
import json
import os
import re
import ssl
import stat
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


RP_PROJECT = os.environ.get("RP_PROJECT", "ocs")
LINK_RE = re.compile(r'<a\s+[^>]*href="([^"]*)"[^>]*>([^<]*)</a>')


def read_token() -> str:
    """
    Read RP API token from file.

    Reads the file path from RP_TOKEN_FILE env var,
    falls back to ~/.ssh/report_portal.

    Returns:
        str: API bearer token.

    Raises:
        FileNotFoundError: If the token file does not exist.

    """
    token_file = os.environ.get(
        "RP_TOKEN_FILE", os.path.expanduser("~/.ssh/report_portal")
    )
    if not os.path.isfile(token_file):
        raise FileNotFoundError(
            f"RP token file not found: {token_file}\n"
            "Create it with: echo '<your_token>' > ~/.ssh/report_portal"
        )
    mode = os.stat(token_file).st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(
            f"Warning: {token_file} has insecure permissions "
            f"({oct(mode & 0o777)}). Run: chmod 600 {token_file}",
            file=sys.stderr,
        )
    with open(token_file) as f:
        return f.read().strip()


def extract_base_url(rp_url: str) -> str:
    """
    Derive RP API base URL from a full Report Portal UI URL.

    Args:
        rp_url (str): Full RP URL, e.g. https://rp.example.com/ui/#project/...

    Returns:
        str: Base URL, e.g. https://rp.example.com

    """
    parsed = urllib.parse.urlparse(rp_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def extract_ids(rp_url: str) -> tuple:
    """
    Parse a Report Portal URL into launch_id and test_item_id.

    Args:
        rp_url (str): Full RP UI URL containing 'launches/' and 'log'.

    Returns:
        tuple: (launch_id, test_item_id) as strings.

    Raises:
        ValueError: If URL format is invalid.

    """
    if "launches/" not in rp_url or "log" not in rp_url:
        raise ValueError(
            "Invalid URL format. Expected a ReportPortal test page URL "
            "containing 'launches/' and 'log'."
        )
    parts = rp_url.split("launches/")[1].split("/")
    launch_id = parts[1]
    test_item_id = parts[3]
    return launch_id, test_item_id


def _ssl_context() -> ssl.SSLContext:
    """Create an SSL context that skips certificate verification."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def fetch_json(url: str, api_key: str) -> dict:
    """
    HTTP GET with Bearer auth, returns parsed JSON.

    Args:
        url (str): API endpoint URL.
        api_key (str): Bearer token for authentication.

    Returns:
        dict: Parsed JSON response.

    Raises:
        urllib.error.HTTPError: If the request fails.

    """
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, context=_ssl_context()) as resp:
        return json.loads(resp.read().decode())


def put_json(url: str, api_key: str, payload: dict) -> dict:
    """
    HTTP PUT with Bearer auth and JSON body, returns parsed JSON.

    Args:
        url (str): API endpoint URL.
        api_key (str): Bearer token for authentication.
        payload (dict): JSON-serializable request body.

    Returns:
        dict: Parsed JSON response.

    Raises:
        urllib.error.HTTPError: If the request fails.

    """
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, context=_ssl_context()) as resp:
        return json.loads(resp.read().decode())


def fetch_launch_info(base_url: str, launch_id: str, api_key: str) -> dict:
    """
    Get launch description, extract logs_url_root and cluster_name.

    Args:
        base_url (str): RP base URL.
        launch_id (str): Launch ID from extract_ids.
        api_key (str): Bearer token.

    Returns:
        dict: logs_url_root, cluster_name, launch_description, attributes.

    Raises:
        LookupError: If no launch found with the given ID.

    """
    url = f"{base_url}/api/v1/{RP_PROJECT}/launch?filter.eq.id={launch_id}"
    data = fetch_json(url, api_key)

    content = data.get("content", [])
    if not content:
        raise LookupError(f"No launch found with id={launch_id}")

    launch = content[0]
    description = launch.get("description", "")

    logs_url_root = ""
    cluster_name = ""
    if "Logs URL:" in description:
        logs_url_root = description.split("Logs URL:")[1].strip()
        if "openshift-clusters/" in logs_url_root:
            cluster_name = (
                logs_url_root.split("openshift-clusters/")[1].split("/")[0]
            )

    attributes = {}
    for attr in launch.get("attributes", []):
        key = attr.get("key", "")
        if key:
            attributes[key] = attr.get("value", "")

    return {
        "logs_url_root": logs_url_root,
        "cluster_name": cluster_name,
        "launch_description": description,
        "attributes": attributes,
    }


def fetch_test_info(base_url: str, item_id: str, api_key: str) -> dict:
    """
    Get test name, status, and ERROR-level logs (traceback).

    Args:
        base_url (str): RP base URL.
        item_id (str): Test item ID from extract_ids.
        api_key (str): Bearer token.

    Returns:
        dict: test_name, status, traceback, error_message.

    """
    item_url = f"{base_url}/api/v1/{RP_PROJECT}/item/{item_id}"
    item_data = fetch_json(item_url, api_key)

    test_name = item_data.get("name", "")
    status = item_data.get("status", "")

    log_url = (
        f"{base_url}/api/v1/{RP_PROJECT}/log"
        f"?filter.eq.item={item_id}&filter.in.level=ERROR"
    )
    log_data = fetch_json(log_url, api_key)

    traceback_lines = []
    error_message = ""
    for entry in log_data.get("content", []):
        msg = entry.get("message", "")
        traceback_lines.append(msg)
        if not error_message and msg:
            error_message = msg.split("\n")[-1].strip()

    traceback = "\n".join(traceback_lines)

    return {
        "test_name": test_name,
        "status": status,
        "traceback": traceback,
        "error_message": error_message,
    }


ISSUE_TYPE_MAP = {
    "product_bug": "PB001",
    "automation_bug": "AB001",
    "system_issue": "SI001",
}


def update_defect(
    base_url: str,
    item_id: str,
    api_key: str,
    issue_type: str,
    comment: str,
    external_url: str = "",
    ticket_id: str = "",
) -> dict:
    """
    Set defect type and comment on a test item via RP API.

    Args:
        base_url (str): RP base URL.
        item_id (str): Test item ID.
        api_key (str): Bearer token.
        issue_type (str): RP locator, e.g. PB001, AB001, SI001.
        comment (str): Defect comment text.
        external_url (str): Optional link URL for external ticket.
        ticket_id (str): Optional ticket ID label.

    Returns:
        dict: RP API response.

    Raises:
        urllib.error.HTTPError: If the request fails.

    """
    issue = {
        "issueType": issue_type,
        "comment": comment,
        "autoAnalyzed": False,
        "ignoreAnalyzer": False,
    }
    if external_url:
        issue["externalSystemIssues"] = [
            {
                "url": external_url,
                "ticketId": ticket_id or external_url,
                "btsUrl": external_url,
                "btsProject": "",
            }
        ]

    payload = {
        "issues": [
            {"testItemId": int(item_id), "issue": issue}
        ]
    }

    url = f"{base_url}/api/v1/{RP_PROJECT}/item"
    return put_json(url, api_key, payload)


def _fetch_page(url: str) -> str:
    """
    Fetch an HTML page, return body as string.

    Args:
        url (str): URL to fetch.

    Returns:
        str: Page HTML content.

    """
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, context=_ssl_context()) as resp:
        return resp.read().decode()


def _parse_links(html: str, base_url: str) -> list:
    """
    Parse <a href> links from an Apache/nginx directory listing.

    Args:
        html (str): Raw HTML of directory page.
        base_url (str): Base URL for resolving relative hrefs.

    Returns:
        list: List of (name, absolute_url, is_dir) tuples.

    """
    seen = set()
    links = []
    for match in LINK_RE.finditer(html):
        href, name = match.group(1), match.group(2).strip()
        if not href or href == "../":
            continue
        if href.startswith(("?", "#", "/")):
            continue
        if "://" in href:
            continue
        if href in seen:
            continue
        seen.add(href)
        abs_url = urllib.parse.urljoin(base_url, href)
        is_dir = href.endswith("/")
        links.append((name.rstrip("/"), abs_url, is_dir))
    return links


def crawl_directory(root_url: str, max_depth: int = 5, workers: int = 20) -> list:
    """
    Crawl an HTTP directory listing concurrently (BFS).

    Uses ThreadPoolExecutor for concurrent HTTP requests,
    matching dircrawl's 20-worker concurrency model.

    Args:
        root_url (str): Root URL to start crawling.
        max_depth (int): Maximum directory depth.
        workers (int): Number of concurrent HTTP workers.

    Returns:
        list: Sorted list of (kind, path, url) tuples.
              kind is "d" for directory, "f" for file.

    """
    if not root_url.endswith("/"):
        root_url += "/"

    results = []
    current_level = [(root_url, 0, "")]

    with ThreadPoolExecutor(max_workers=workers) as executor:
        while current_level:
            future_map = {}
            for url, depth, prefix in current_level:
                if depth >= max_depth:
                    continue
                future = executor.submit(_fetch_page, url)
                future_map[future] = (url, depth, prefix)

            if not future_map:
                break

            next_level = []
            for future in as_completed(future_map):
                url, depth, prefix = future_map[future]
                try:
                    html = future.result()
                except (urllib.error.URLError, OSError):
                    continue

                for name, link_url, is_dir in _parse_links(html, url):
                    path = prefix + name + ("/" if is_dir else "")
                    results.append(("d" if is_dir else "f", path, link_url))
                    if is_dir:
                        next_level.append(
                            (link_url, depth + 1, prefix + name + "/")
                        )

            current_level = next_level

    results.sort(key=lambda x: x[1])
    return results


def _run_crawl(args: list) -> None:
    """
    Handle 'crawl' subcommand: crawl a directory listing.

    Args:
        args (list): Arguments after 'crawl', e.g. ['-d', '4', '<url>'].

    """
    max_depth = 5
    url = ""
    i = 0
    while i < len(args):
        if args[i] == "-d" and i + 1 < len(args):
            max_depth = int(args[i + 1])
            i += 2
            continue
        url = args[i]
        i += 1

    if not url:
        print(
            "Usage: rp_cli.py crawl [-d DEPTH] <url>", file=sys.stderr
        )
        sys.exit(1)

    for kind, path, _ in crawl_directory(url, max_depth=max_depth):
        print(f"{kind} {path}")


def _run_query(rp_url: str) -> None:
    """
    Handle RP query: fetch test info and print JSON.

    Args:
        rp_url (str): Full Report Portal URL.

    """
    try:
        api_key = read_token()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    base_url = extract_base_url(rp_url)
    launch_id, test_item_id = extract_ids(rp_url)
    launch_info = fetch_launch_info(base_url, launch_id, api_key)
    test_info = fetch_test_info(base_url, test_item_id, api_key)

    result = {
        "launch_id": launch_id,
        "test_item_id": test_item_id,
        **test_info,
        **launch_info,
    }

    print(json.dumps(result, indent=2))


def compute_traceback_hash(traceback: str) -> str:
    """
    Compute a normalized SHA-256 hash of a traceback for deduplication.

    Extracts last 5 non-empty lines, strips timestamps, UUIDs,
    and pod name suffixes before hashing.

    Args:
        traceback (str): Full traceback string.

    Returns:
        str: Hex digest of SHA-256 hash.

    """
    lines = [line for line in traceback.splitlines() if line.strip()][-5:]
    normalized = []
    for line in lines:
        line = line.strip()
        line = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.\d]*", "", line)
        line = re.sub(
            r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
            "",
            line,
        )
        line = re.sub(r"-[a-z0-9]{5,10}$", "", line)
        normalized.append(line)
    return hashlib.sha256("\n".join(normalized).encode()).hexdigest()


def _run_hash() -> None:
    """Handle 'hash' subcommand: read traceback from stdin, print hash."""
    traceback = sys.stdin.read()
    if not traceback.strip():
        print("Error: pipe traceback via stdin", file=sys.stderr)
        sys.exit(1)
    print(compute_traceback_hash(traceback))


def _run_decide(args: list) -> None:
    """
    Handle 'decide' subcommand: set defect type on an RP test item.

    Args:
        args (list): [item_id, issue_type, --comment "...", --link-url "...", --link-id "..."]

    """
    if len(args) < 3:
        print(
            "Usage: rp_cli.py decide <base_url> <item_id> <issue_type> "
            "[--comment '...'] [--link-url '...'] [--link-id '...']"
            "\n  issue_type: product_bug | automation_bug | system_issue",
            file=sys.stderr,
        )
        sys.exit(1)

    base_url = args[0]
    item_id = args[1]
    issue_type_key = args[2]

    if issue_type_key not in ISSUE_TYPE_MAP:
        print(
            f"Error: unknown issue_type '{issue_type_key}'. "
            f"Choose from: {', '.join(ISSUE_TYPE_MAP)}",
            file=sys.stderr,
        )
        sys.exit(1)

    comment = ""
    link_url = ""
    link_id = ""
    i = 3
    while i < len(args):
        if args[i] == "--comment" and i + 1 < len(args):
            comment = args[i + 1]
            i += 2
        elif args[i] == "--link-url" and i + 1 < len(args):
            link_url = args[i + 1]
            i += 2
        elif args[i] == "--link-id" and i + 1 < len(args):
            link_id = args[i + 1]
            i += 2
        else:
            print(f"Error: unknown flag '{args[i]}'", file=sys.stderr)
            sys.exit(1)

    try:
        api_key = read_token()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    result = update_defect(
        base_url, item_id, api_key,
        ISSUE_TYPE_MAP[issue_type_key], comment,
        external_url=link_url, ticket_id=link_id,
    )
    print(json.dumps(result, indent=2))


def main() -> None:
    """Route to query, crawl, hash, or decide subcommand."""
    if len(sys.argv) < 2:
        print(
            "Usage:\n"
            "  rp_cli.py <report_portal_url>       # Query RP\n"
            "  rp_cli.py crawl [-d DEPTH] <url>     # Crawl directory\n"
            "  rp_cli.py hash                        # Hash traceback (stdin)\n"
            "  rp_cli.py decide <base_url> <item_id> <issue_type> [opts]  # Set defect",
            file=sys.stderr,
        )
        sys.exit(1)

    if sys.argv[1] == "crawl":
        _run_crawl(sys.argv[2:])
        return

    if sys.argv[1] == "hash":
        _run_hash()
        return

    if sys.argv[1] == "decide":
        _run_decide(sys.argv[2:])
        return

    _run_query(sys.argv[1])


if __name__ == "__main__":
    main()
