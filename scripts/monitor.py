"""Read Brev's GPU catalog and deliver deduplicated GitHub Issue alerts."""

import argparse
import copy
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import hashlib
import html
import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


BREV_ORGS_URL = (
    "https://brevapi.us-west-2-prod.control-plane.brev.dev/api/organizations"
)
BREV_INSTANCE_URL = "https://api.brev.dev/devplaneapi.v1.InstanceService/"
CONSOLE_URL = "https://brev.nvidia.com/environment/new"
ISSUE_TITLE = "Brev Blackwell monitor"
STATE_START = "<!-- brev-blackwell-monitor-state\n"
STATE_END = "\n-->"
GPU_PATTERN = re.compile(
    r"\bblackwell\b"
    r"|\b(?:B100|B200|B300|GB10|GB200|GB300)\b",
    re.IGNORECASE,
)


class MonitorError(Exception):
    """An actionable error that is safe to include in logs and notifications."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward authentication headers to a redirect destination.
        return None


def request_json(method, url, *, label, token="", body=None, retry=False,
                 connect=False):
    headers = {
        "Accept": "application/json",
        "User-Agent": "brev-blackwell-monitor",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if connect:
        headers["Connect-Protocol-Version"] = "1"
    if url.startswith("https://api.github.com/"):
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    opener = build_opener(NoRedirects())
    for attempt in range(2 if retry else 1):
        try:
            request = Request(url, data=data, headers=headers, method=method)
            with opener.open(request, timeout=15) as response:
                payload = response.read()
            return json.loads(payload)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if retry and attempt == 0 and status in (429, 500, 502, 503, 504):
                time.sleep(2)
                continue
            hint = ""
            if status in (401, 403):
                hint = " Check the credential, its expiration, and permissions."
            raise MonitorError(f"{label}: HTTP {status}.{hint}") from None
        except (URLError, TimeoutError):
            if retry and attempt == 0:
                time.sleep(2)
                continue
            raise MonitorError(f"{label}: connection failed or timed out.") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise MonitorError(f"{label}: response was not valid JSON.") from None
    raise MonitorError(f"{label}: request did not complete.")


def fetch_catalog(api_key, *, public=False):
    options = {
        "includeCpu": False,
        "includeUnavailable": True,
        "includePreemptible": False,
        "uniqueInstanceType": True,
        "skipAccessFilter": False,
    }
    body = {"options": options}
    if public:
        method = "ListPublicInstanceType"
        token = ""
    else:
        if not api_key:
            raise MonitorError("Missing BREV_API_KEY repository secret.")
        if not api_key.startswith("bak-"):
            raise MonitorError("BREV_API_KEY must be a Brev API key, starting with bak-.")
        orgs = request_json(
            "GET", BREV_ORGS_URL, label="Brev organization lookup",
            token=api_key, retry=True,
        )
        if (not isinstance(orgs, list) or len(orgs) != 1
                or not isinstance(orgs[0], dict)
                or not isinstance(orgs[0].get("id"), str)
                or not orgs[0]["id"]):
            raise MonitorError("Brev API key must resolve to exactly one organization.")
        method = "ListOrganizationAvailableInstanceTypes"
        body["organizationId"] = orgs[0]["id"]
        token = api_key
    response = request_json(
        "POST", BREV_INSTANCE_URL + method, label="Brev GPU catalog",
        token=token, body=body, retry=True, connect=True,
    )
    if not isinstance(response, dict) or (response and "items" not in response):
        raise MonitorError("Brev GPU catalog: unexpected response schema.")
    if response.get("nextPageToken"):
        raise MonitorError("Brev GPU catalog: unexpected pagination; update the monitor.")
    # Protobuf JSON omits repeated fields when the response is legitimately empty.
    items = response.get("items", [])
    if not isinstance(items, list):
        raise MonitorError("Brev GPU catalog: items must be a list.")
    return items


def text_field(value, name, *, required=False):
    if not isinstance(value, str) or (required and not value.strip()):
        raise MonitorError(f"Brev GPU catalog: invalid {name}.")
    return value.strip()


@dataclass
class Match:
    key: str
    gpu: str
    instance_type: str
    provider: str
    count: int
    statuses: set = field(default_factory=set)
    locations: set = field(default_factory=set)
    prices: set = field(default_factory=set)

    @property
    def availability(self):
        if "available" in self.statuses:
            return "available"
        if "unknown" in self.statuses:
            return "unknown"
        return "unavailable"


def find_blackwell(items):
    matches = {}
    saw_gpu = False
    for item in items:
        if not isinstance(item, dict):
            raise MonitorError("Brev GPU catalog: invalid instance entry.")
        instance_type = text_field(item.get("type"), "instance type", required=True)
        gpus = item.get("supportedGpus", [])
        if not isinstance(gpus, list):
            raise MonitorError("Brev GPU catalog: invalid supportedGpus.")
        for gpu in gpus:
            if not isinstance(gpu, dict):
                raise MonitorError("Brev GPU catalog: invalid GPU entry.")
            name = text_field(gpu.get("name"), "GPU name", required=True)
            saw_gpu = True
            if "rtx" in name.casefold() or not GPU_PATTERN.search(name):
                continue
            count = gpu.get("count")
            if type(count) is not int or count < 1:
                raise MonitorError("Brev GPU catalog: invalid GPU count.")
            provider = text_field(item.get("provider", ""), "provider") or "unknown"
            identity = [provider.casefold(), instance_type.casefold(),
                        re.sub(r"[\s_-]+", "", name.casefold()), count]
            key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
            if key not in matches:
                matches[key] = Match(key, name, instance_type, provider, count)
            match = matches[key]
            available = item.get("isAvailable")
            if available is not None and type(available) is not bool:
                raise MonitorError("Brev GPU catalog: invalid availability.")
            match.statuses.add(
                "unknown" if available is None else
                "available" if available else "unavailable"
            )
            locations = item.get("availableLocations", [])
            if not isinstance(locations, list):
                raise MonitorError("Brev GPU catalog: invalid availableLocations.")
            for location in locations:
                match.locations.add(text_field(location, "location", required=True))
            if not locations and item.get("location"):
                match.locations.add(text_field(item["location"], "location"))
            price = item.get("basePrice", {})
            if not isinstance(price, dict):
                raise MonitorError("Brev GPU catalog: invalid basePrice.")
            if price:
                currency = text_field(price.get("currency", ""), "price currency")
                amount = text_field(price.get("amount", ""), "price amount")
                if currency and amount:
                    match.prices.add(f"{currency} {amount}/h")
    if items and not saw_gpu:
        raise MonitorError("Brev GPU catalog: nonempty GPU catalog has no GPU metadata.")
    return sorted(matches.values(), key=lambda m: (m.gpu, m.provider, m.instance_type))


def safe_text(value):
    return html.escape(str(value)).replace("|", "\\|").replace("@", "&#64;").replace(
        "\n", " "
    ).replace("\r", " ")


def match_table(matches):
    if not matches:
        return "No matching Blackwell GPU listings."
    lines = [
        "| GPU | Count | Instance | Provider | API availability | Base price | Locations |",
        "| --- | ---: | --- | --- | --- | --- | --- |",
    ]
    for match in matches[:20]:
        columns = (
            match.gpu, match.count, match.instance_type, match.provider,
            match.availability, ", ".join(sorted(match.prices)) or "not reported",
            ", ".join(sorted(match.locations)) or "not reported",
        )
        lines.append("| " + " | ".join(safe_text(v) for v in columns) + " |")
    if len(matches) > 20:
        lines.append(f"\nShowing 20 of {len(matches)} matching configurations.")
    return "\n".join(lines)


def initial_state():
    return {
        "version": 1, "matches": {}, "error": "", "expiry_notice": "",
        "last_success": "",
    }


def listing_alerts(title, matches):
    return [
        f"## {title}\n\n" + match_table(matches[start:start + 20])
        for start in range(0, len(matches), 20)
    ]


def notification_comments(alerts, notify_user, run_url):
    prefix = f"@{notify_user}\n\n"
    suffix = (
        f"\n\n[Brev console]({CONSOLE_URL}) | [Workflow run]({run_url})"
        "\n\nCatalog presence is not a reservation or a guarantee of capacity."
    )
    comments = []
    current = ""
    for alert in alerts:
        if len(prefix + alert + suffix) > 60000:
            raise MonitorError("A notification is too large for GitHub; update the monitor.")
        candidate = current + ("\n\n" if current else "") + alert
        if len(prefix + candidate + suffix) > 60000:
            comments.append(prefix + current + suffix)
            current = alert
        else:
            current = candidate
    if current:
        comments.append(prefix + current + suffix)
    return comments


def decode_state(body):
    if (not isinstance(body, str) or body.count(STATE_START) != 1
            or STATE_END not in body.split(STATE_START, 1)[1]):
        raise MonitorError("Monitor issue state is missing or damaged; do not edit its marker.")
    raw = body.split(STATE_START, 1)[1].split(STATE_END, 1)[0]
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        raise MonitorError("Monitor issue state is not valid JSON.") from None
    if (not isinstance(state, dict) or state.get("version") != 1
            or not isinstance(state.get("matches"), dict)
            or not all(isinstance(state.get(k), str)
                       for k in ("error", "expiry_notice", "last_success"))
            or not all(re.fullmatch(r"[a-f0-9]{32}", k)
                       and v in ("available", "unavailable", "unknown")
                       for k, v in state["matches"].items())):
        raise MonitorError("Monitor issue state has an unsupported schema.")
    return state


def expiry_notice(value, today):
    if not value:
        return ("missing", "Set repository variable `BREV_API_KEY_EXPIRES_ON` "
                "to `YYYY-MM-DD` to enable expiry reminders. GPU checks continue.")
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        expires = date.fromisoformat(value)
    except ValueError:
        return ("invalid", "`BREV_API_KEY_EXPIRES_ON` must be a valid `YYYY-MM-DD` "
                "date. Expiry reminders are unavailable; GPU checks continue.")
    days = (expires - today).days
    if days > 7:
        return "", ""
    if days < 0:
        return (f"{value}:expired", f"The configured Brev API key expiry date "
                f"({value}) has passed. Replace the secret and update the date variable.")
    return (f"{value}:due", f"The Brev API key expires on {value} "
            f"(in {days} day(s)). Create a replacement, update `BREV_API_KEY`, "
            "and update `BREV_API_KEY_EXPIRES_ON` before it expires.")


def evaluate(previous, *, now, expires_on, matches=None, error=""):
    state = copy.deepcopy(previous)
    alerts = []
    expiry_key, expiry_message = expiry_notice(expires_on, now.date())
    if expiry_key and expiry_key != previous["expiry_notice"]:
        alerts.append("## API key configuration\n\n" + expiry_message)
    state["expiry_notice"] = expiry_key
    if error:
        if error != previous["error"]:
            alerts.append("## Monitoring failed\n\n" + safe_text(error)
                          + "\n\nPrevious successful GPU observations have been preserved.")
        state["error"] = error
        return state, alerts
    if matches is None:
        raise MonitorError("Internal error: a successful check needs a catalog result.")
    if previous["error"]:
        alerts.append("## Monitoring recovered\n\nThe Brev catalog is readable again.")
    new = [m for m in matches if m.key not in previous["matches"]]
    restocked = [
        m for m in matches if m.key in previous["matches"]
        and previous["matches"][m.key] != "available" and m.availability == "available"
    ]
    if new:
        alerts.extend(listing_alerts("Blackwell listings detected", new))
    if restocked:
        alerts.extend(listing_alerts("Blackwell availability changed to available", restocked))
    state["matches"] = {m.key: m.availability for m in matches}
    state["error"] = ""
    state["last_success"] = now.isoformat(timespec="seconds")
    return state, alerts


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise MonitorError("GITHUB_REPOSITORY must be owner/repository.")
        if not token:
            raise MonitorError("GITHUB_TOKEN is missing; run this through GitHub Actions.")
        self.base = f"https://api.github.com/repos/{repository}"
        self.token = token

    def call(self, method, path, body=None):
        return request_json(
            method, self.base + path, label="GitHub Issues",
            token=self.token, body=body, retry=method == "GET",
        )

    def find_issue(self):
        page = 1
        while True:
            issues = self.call(
                "GET", f"/issues?state=all&creator=github-actions%5Bbot%5D"
                f"&sort=created&direction=asc&per_page=100&page={page}",
            )
            if not isinstance(issues, list):
                raise MonitorError("GitHub returned an invalid issue list.")
            for issue in issues:
                if issue.get("title") == ISSUE_TITLE and "pull_request" not in issue:
                    return issue
            if len(issues) < 100:
                return None
            page += 1

    def create_issue(self, body):
        return self.call("POST", "/issues", {"title": ISSUE_TITLE, "body": body})

    def comment(self, number, body):
        self.call("POST", f"/issues/{number}/comments", {"body": body})

    def update_issue(self, number, body):
        self.call("PATCH", f"/issues/{number}", {"body": body, "state": "open"})


def issue_body(state, summary, run_url):
    body = (
        "# Brev Blackwell monitor\n\n"
        "Checks every 30 minutes. Subscribe to this issue for alerts.\n\n"
        f"{summary}\n\n"
        f"Last successful check (UTC): {state['last_success'] or 'not yet'}\n\n"
        f"[Brev console]({CONSOLE_URL}) | [Workflow run]({run_url})\n\n"
        "A catalog listing or API availability flag does not guarantee that an "
        "instance can be rented. Prices are base prices and may vary by region; "
        "storage and other charges may be additional. No instances are created.\n\n"
        "Do not delete this issue, change its title, or edit its hidden state. "
        "To stop monitoring, disable the workflow, rather than closing the issue.\n\n"
        + STATE_START + json.dumps(state, sort_keys=True) + STATE_END
    )
    if len(body) > 60000:
        raise MonitorError("Monitor state is too large for an issue; update the monitor.")
    return body


def run_monitor(github, *, api_key, expires_on, notify_user, run_url, now):
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", notify_user):
        raise MonitorError("NOTIFY_USER must be a GitHub username without @.")
    issue = github.find_issue()
    previous = decode_state(issue["body"]) if issue else initial_state()
    matches = None
    catalog_count = None
    error = ""
    try:
        items = fetch_catalog(api_key)
        catalog_count = len(items)
        matches = find_blackwell(items)
    except MonitorError as exc:
        error = str(exc)
    state, alerts = evaluate(
        previous, now=now, expires_on=expires_on, matches=matches, error=error,
    )
    if error:
        summary = (
            "**Result: CHECK FAILED**\n\n" + safe_text(error)
            + "\n\nNo inventory conclusion was reached. This is not a no-match result."
        )
    elif matches:
        summary = (
            "**Result: MATCHES FOUND**\n\n"
            f"Catalog entries scanned: {catalog_count}. "
            f"Matching GPU configurations: {len(matches)}.\n\n"
            + match_table(matches)
            + "\n\nA catalog match does not guarantee rentable capacity."
        )
    else:
        summary = (
            "**Result: NO MATCHES**\n\n"
            f"Live query succeeded. Catalog entries scanned: {catalog_count}.\n\n"
            "No GPU configurations matched the current non-RTX Blackwell filters "
            "in your Brev organization's catalog. This is not a platform-wide result."
        )
    final_body = issue_body(state, summary, run_url)
    if not issue:
        # Save an unacknowledged state until the alert comment has been delivered.
        issue = github.create_issue(issue_body(previous, "Monitor is starting.", run_url))
        alerts.insert(0, "## Monitor initialized\n\nChecks run every 30 minutes. "
                      "This notification also confirms the GitHub delivery channel.")
    for comment in notification_comments(alerts, notify_user, run_url):
        github.comment(issue["number"], comment)
    github.update_issue(issue["number"], final_body)
    live_summary = (
        "## Live Brev organization check\n\n"
        f"Check time (UTC): {now.isoformat(timespec='seconds')}\n\n"
        "Source: Brev catalog for the API key's organization (RTX excluded).\n\n"
        + summary + "\n"
    )
    print(live_summary)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(live_summary)
    if error:
        raise MonitorError(error)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Read and display the catalog without GitHub writes.")
    parser.add_argument("--public-catalog", action="store_true",
                        help="Use the unauthenticated catalog (dry-run only).")
    args = parser.parse_args()
    if args.public_catalog and not args.dry_run:
        parser.error("--public-catalog requires --dry-run")
    api_key = os.environ.get("BREV_API_KEY", "").strip()
    try:
        if args.dry_run:
            items = fetch_catalog(api_key, public=args.public_catalog)
            matches = find_blackwell(items)
            print("Catalog scope:", "public" if args.public_catalog else "API key organization")
            print(f"Read {len(items)} instance entries; found {len(matches)} Blackwell configurations.")
            print(match_table(matches))
        else:
            repository = os.environ.get("GITHUB_REPOSITORY", "")
            github = GitHub(repository, os.environ.get("GITHUB_TOKEN", ""))
            run_monitor(
                github, api_key=api_key,
                expires_on=os.environ.get("BREV_API_KEY_EXPIRES_ON", "").strip(),
                notify_user=os.environ.get("NOTIFY_USER", repository.split("/")[0]),
                run_url=f"https://github.com/{repository}/actions/runs/"
                        + os.environ.get("GITHUB_RUN_ID", ""),
                now=datetime.now(timezone.utc),
            )
    except MonitorError as exc:
        print(f"Monitor failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
