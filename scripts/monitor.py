"""Read Brev's GPU catalog, deliver GitHub Issue alerts, and optionally start one GPU."""

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
BOT_LOGIN = "github-actions[bot]"
STATE_START = "<!-- brev-blackwell-monitor-state\n"
STATE_END = "\n-->"
GPU_PATTERN = re.compile(r"\b(?:B200|B300)\b", re.IGNORECASE)
# Only single-GPU configurations are monitored; multi-GPU listings are ignored.
GPU_COUNT = 1
# Auto-start mirrors the defaults of brev-cli `brev create` for API-key auth.
AUTOSTART_PREFIX = "blackwell-auto-"
AUTOSTART_OFF = ("", "0", "false", "off", "no", "disabled")
WORKSPACE_CLASS_ID = "2x8"
WORKSPACE_TEMPLATE_ID = "4nbb4lg2s"
WORKSPACE_DISK = "120Gi"
# Brev workspace statuses showing an instance actually ran (brev-cli entity.go).
RAN_STATUSES = ("RUNNING", "STOPPING", "STOPPED")


# Brev's catalog RPC sometimes stalls for tens of seconds shortly after the hour.
# Reads get three attempts spanning about 80 seconds; writes are never retried.
REQUEST_TIMEOUT = 20
READ_RETRY_DELAYS = (5, 15)


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
    delays = READ_RETRY_DELAYS if retry else ()
    for attempt in range(len(delays) + 1):
        retry_left = attempt < len(delays)
        try:
            request = Request(url, data=data, headers=headers, method=method)
            with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                payload = response.read()
            return json.loads(payload)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if retry_left and status in (429, 500, 502, 503, 504):
                time.sleep(delays[attempt])
                continue
            hint = ""
            if status in (401, 403):
                hint = " Check the credential, its expiration, and permissions."
            raise MonitorError(f"{label}: HTTP {status}.{hint}") from None
        except (URLError, TimeoutError):
            if retry_left:
                time.sleep(delays[attempt])
                continue
            raise MonitorError(f"{label}: connection failed or timed out.") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise MonitorError(f"{label}: response was not valid JSON.") from None
    raise MonitorError(f"{label}: request did not complete.")


def check_api_key(api_key):
    if not api_key:
        raise MonitorError("Missing BREV_API_KEY repository secret.")
    if not api_key.startswith("bak-"):
        raise MonitorError("BREV_API_KEY must be a Brev API key, starting with bak-.")


def resolve_org(api_key):
    check_api_key(api_key)
    orgs = request_json(
        "GET", BREV_ORGS_URL, label="Brev organization lookup",
        token=api_key, retry=True,
    )
    if (not isinstance(orgs, list) or len(orgs) != 1
            or not isinstance(orgs[0], dict)
            or not isinstance(orgs[0].get("id"), str)
            or not orgs[0]["id"]):
        raise MonitorError("Brev API key must resolve to exactly one organization.")
    return orgs[0]["id"]


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
        method = "ListOrganizationAvailableInstanceTypes"
        body["organizationId"] = resolve_org(api_key)
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
            if count != GPU_COUNT:
                continue
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


def autostart_label(value):
    label = value.strip().lower()
    if label in AUTOSTART_OFF:
        return ""
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,18}[a-z0-9])?", label):
        raise MonitorError("BREV_AUTOSTART must be 1-20 lowercase letters, digits, "
                           "or hyphens (for example `1`); auto-start is disabled.")
    return label


def autostart_candidates(items):
    """Return available monitored configurations, cheapest first."""
    candidates = []
    for item in items:
        if not item.get("supportedGpus") or item.get("isAvailable") is not True:
            continue
        for match in find_blackwell([item]):
            price = item.get("basePrice") or {}
            try:
                cost = float(price.get("amount", ""))
            except ValueError:
                cost = float("inf")
            candidates.append({
                "instance_type": match.instance_type, "gpu": match.gpu,
                "provider": match.provider, "cost": cost,
                "price": ", ".join(sorted(match.prices)) or "not reported",
                "cloud_cred_id": item.get("cloudCredId"),
            })
    return sorted(candidates, key=lambda c: (c["cost"], c["instance_type"]))


def workspaces_url(org_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", org_id):
        raise MonitorError("Brev organization ID has an unexpected format.")
    return f"{BREV_ORGS_URL}/{org_id}/workspaces"


def list_workspaces(api_key, org_id):
    workspaces = request_json(
        "GET", workspaces_url(org_id), label="Brev instance list",
        token=api_key, retry=True,
    )
    # Go encodes an empty list as null.
    if workspaces is None:
        return []
    if not isinstance(workspaces, list) or not all(isinstance(w, dict) for w in workspaces):
        raise MonitorError("Brev instance list: unexpected response schema.")
    return workspaces


def create_workspace(api_key, org_id, candidate, name):
    # Field set and defaults follow brev-cli NewCreateWorkspacesOptions + gpucreate.
    body = {
        "name": name, "cloudCredId": candidate["cloud_cred_id"],
        "workspaceClassId": WORKSPACE_CLASS_ID,
        "workspaceTemplateId": WORKSPACE_TEMPLATE_ID,
        "description": "", "isStoppable": False, "primaryApplicationId": "",
        "applications": None, "startupScript": "", "gitRepo": "", "initBranch": "",
        "startupScriptPath": "", "dotBrevPath": "", "ideConfig": None,
        "repos": None, "execs": None, "reposV1": {}, "execsV1": {},
        "instanceType": candidate["instance_type"], "diskStorage": WORKSPACE_DISK,
        "baseImage": "", "vmOnlyMode": False,
        "vmBuild": {"forceJupyterInstall": True}, "portMappings": {},
        "files": None, "labels": None, "workspaceVersion": "v1",
        "launchJupyterOnStart": False,
    }
    # Never retry: a timed-out creation may still have been accepted.
    workspace = request_json(
        "POST", workspaces_url(org_id), label="Brev instance creation",
        token=api_key, body=body,
    )
    if not isinstance(workspace, dict) or not isinstance(workspace.get("id"), str):
        raise MonitorError("Brev instance creation: unexpected response; check the console.")
    return workspace


def instance_name(label, failures):
    # The first attempt keeps the plain name so earlier instances are recognized.
    return AUTOSTART_PREFIX + label + (f"-{failures + 1}" if failures else "")


def find_instance(api_key, name, label=""):
    """Return (organization ID, the instance with this name or None).

    With `label`, any live instance from that label's earlier attempts also
    counts, so a lost or reset state cannot start a second GPU.
    """
    org_id = resolve_org(api_key)
    workspaces = list_workspaces(api_key, org_id)
    found = []
    if label:
        base = AUTOSTART_PREFIX + label
        found = [
            w for w in workspaces
            if re.fullmatch(re.escape(base) + r"(?:-\d+)?", str(w.get("name", "")))
            and w.get("status") not in ("FAILURE", "DELETING")
        ]
    found = found or [w for w in workspaces if w.get("name") == name]
    return org_id, found[0] if found else None


def autostart(api_key, label, items, state):
    """Keep at most one non-failed auto-started instance per arm label.

    Return (state changes, alerts, summary).
    """
    if not label:
        return {}, [], "Auto-start: disabled."
    changes, alerts = {}, []
    failures = state["autostart_failures"].get(label, 0)
    name = instance_name(label, failures)
    try:
        if label == state["autostart"]:
            _, started = find_instance(api_key, name)
            status = (started.get("status") or "unknown") if started else "deleted"
            if status in RAN_STATUSES:
                # The label is used for good once its instance has run.
                return ({} if state["autostart_ran"] else {"autostart_ran": True}), [], (
                    f"Auto-start: done; `{name}` reached status {safe_text(status)}. "
                    "Change `BREV_AUTOSTART` to allow another start.")
            if state["autostart_ran"] or status not in ("FAILURE", "DELETING", "deleted"):
                return {}, [], (f"Auto-start: used for label `{label}`; `{name}` has "
                                f"status {safe_text(status)}.")
            # The instance never ran, so nothing is running: start a replacement.
            failures += 1
            changes["autostart"] = ""
            changes["autostart_failures"] = {**state["autostart_failures"], label: failures}
            failed, name = name, instance_name(label, failures)
            alerts.append(
                f"## Started instance failed\n\nInstance `{failed}` ended without running "
                f"(status: {safe_text(status)}), so no GPU is running from auto-start. "
                f"Auto-start is re-armed: the next available listing starts `{name}`. "
                "A failed instance is left in Brev for you to inspect or delete."
            )
        candidates = autostart_candidates(items)
        if not candidates:
            return changes, alerts, "Auto-start: armed; no available single B200/B300 listing."
        candidate = candidates[0]
        if not isinstance(candidate["cloud_cred_id"], str) or not candidate["cloud_cred_id"]:
            raise MonitorError("Brev GPU catalog: available listing has no cloudCredId; "
                               "update the monitor.")
        org_id, existing = find_instance(api_key, name, label)
        if existing is None:
            workspace = create_workspace(api_key, org_id, candidate, name)
    except MonitorError as exc:
        error = str(exc)
        if error != state["autostart_error"]:
            alerts.append(
                "## Auto-start failed\n\n" + safe_text(error)
                + "\n\nIf the request timed out, the instance may still have been created. "
                "The next run checks for it by name before trying again."
            )
        changes["autostart_error"] = error
        return changes, alerts, "Auto-start: FAILED. " + safe_text(error)
    changes.update(autostart=label, autostart_error="", autostart_ran=False)
    if existing is not None:
        status = safe_text(existing.get("status") or "unknown")
        if existing.get("name") != name:
            # Track the live instance from an earlier attempt from now on.
            name = existing["name"]
            suffix = name[len(AUTOSTART_PREFIX + label):]
            failures = int(suffix[1:]) - 1 if suffix else 0
            tracked = {k: v for k, v in state["autostart_failures"].items() if k != label}
            changes["autostart_failures"] = {**tracked, **({label: failures} if failures else {})}
        return changes, alerts + [
            f"## Auto-start skipped\n\nInstance `{name}` already exists (status: {status}), "
            "so no second instance was created. Auto-start is now used for this label."
        ], f"Auto-start: `{name}` already exists; nothing created."
    details = (
        "| Name | ID | Instance | GPU | Provider | Base price |\n"
        "| --- | --- | --- | --- | --- | --- |\n| " + " | ".join(safe_text(v) for v in (
            name, workspace["id"], candidate["instance_type"], candidate["gpu"],
            candidate["provider"], candidate["price"],
        )) + " |"
    )
    return changes, alerts + [
        "## Instance started\n\n" + details + "\n\n**Billing continues until you stop "
        "or delete this instance in the Brev console.** Auto-start is now used; set "
        "`BREV_AUTOSTART` to a new value to allow another start. If this instance "
        "ends with status FAILURE, auto-start re-arms and tries again."
    ], "Auto-start: STARTED.\n\n" + details


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
        "version": 3, "matches": {}, "error": "", "expiry_notice": "",
        "last_success": "", "autostart": "", "autostart_error": "",
        "autostart_failures": {}, "autostart_ran": False,
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
    if isinstance(state, dict) and state.get("version") == 1:
        # Version 2 adds auto-start bookkeeping; version 1 never started anything.
        state.update(version=2, autostart="", autostart_error="")
    if isinstance(state, dict) and state.get("version") == 2:
        # Version 3 counts failed instances per label and records whether the
        # current instance ran; version 2 recorded neither.
        state.update(version=3, autostart_failures={}, autostart_ran=False)
    failures = state.get("autostart_failures") if isinstance(state, dict) else None
    if (not isinstance(state, dict) or state.get("version") != 3
            or not isinstance(state.get("matches"), dict)
            or not all(isinstance(state.get(k), str)
                       for k in ("error", "expiry_notice", "last_success",
                                 "autostart", "autostart_error"))
            or type(state.get("autostart_ran")) is not bool
            or not isinstance(failures, dict)
            or not all(isinstance(k, str) and type(v) is int and v > 0
                       for k, v in failures.items())
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
        # A filtered listing once came back without the state issue, which
        # created a duplicate with empty state. Confirm absence with an
        # unfiltered listing before reporting that no state issue exists.
        for creator in ("&creator=github-actions%5Bbot%5D", ""):
            issue = self.scan_issues(creator)
            if issue:
                return issue
        return None

    def scan_issues(self, creator):
        page = 1
        while True:
            issues = self.call(
                "GET", f"/issues?state=all{creator}"
                f"&sort=created&direction=asc&per_page=100&page={page}",
            )
            if not isinstance(issues, list):
                raise MonitorError("GitHub returned an invalid issue list.")
            for issue in issues:
                if (issue.get("title") == ISSUE_TITLE and "pull_request" not in issue
                        and (issue.get("user") or {}).get("login", BOT_LOGIN) == BOT_LOGIN):
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
        "Checks run on a schedule (see the README). Subscribe to this issue for alerts.\n\n"
        f"{summary}\n\n"
        f"Last successful check (UTC): {state['last_success'] or 'not yet'}\n\n"
        f"[Brev console]({CONSOLE_URL}) | [Workflow run]({run_url})\n\n"
        "A catalog listing or API availability flag does not guarantee that an "
        "instance can be rented. Prices are base prices and may vary by region; "
        "storage and other charges may be additional. An instance is created only "
        "when auto-start is armed with `BREV_AUTOSTART`.\n\n"
        "Do not delete this issue, change its title, or edit its hidden state. "
        "To stop monitoring, disable the workflow, rather than closing the issue.\n\n"
        + STATE_START + json.dumps(state, sort_keys=True) + STATE_END
    )
    if len(body) > 60000:
        raise MonitorError("Monitor state is too large for an issue; update the monitor.")
    return body


def run_monitor(github, *, api_key, expires_on, notify_user, run_url, now,
                autostart_value=""):
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", notify_user):
        raise MonitorError("NOTIFY_USER must be a GitHub username without @.")
    issue = github.find_issue()
    previous = decode_state(issue["body"]) if issue else initial_state()
    matches = None
    catalog_count = None
    items = []
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
    start_summary = "Auto-start: skipped because the catalog check failed."
    if not error:
        try:
            label = autostart_label(autostart_value)
        except MonitorError as exc:
            changes = {"autostart_error": str(exc)}
            start_alerts = ([] if str(exc) == state["autostart_error"]
                            else ["## Auto-start configuration\n\n" + safe_text(exc)])
            start_summary = safe_text(exc)
        else:
            changes, start_alerts, start_summary = autostart(api_key, label, items, state)
        state.update(changes)
        alerts.extend(start_alerts)
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
            "No monitored GPU configurations were found in your Brev organization's "
            "catalog. This is not a platform-wide result."
        )
    summary += "\n\n" + start_summary
    final_body = issue_body(state, summary, run_url)
    if not issue:
        # Save an unacknowledged state until the alert comment has been delivered.
        issue = github.create_issue(issue_body(previous, "Monitor is starting.", run_url))
        alerts.insert(0, "## Monitor initialized\n\nChecks run on a schedule (see the README). "
                      "This notification also confirms the GitHub delivery channel.")
    for comment in notification_comments(alerts, notify_user, run_url):
        github.comment(issue["number"], comment)
    github.update_issue(issue["number"], final_body)
    live_summary = (
        "## Live Brev organization check\n\n"
        f"Check time (UTC): {now.isoformat(timespec='seconds')}\n\n"
        "Source: Brev catalog for the API key's organization.\n\n"
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
                autostart_value=os.environ.get("BREV_AUTOSTART", ""),
            )
    except MonitorError as exc:
        print(f"Monitor failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
