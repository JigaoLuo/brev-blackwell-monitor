# Agent guide

Read this file before changing the project. `README.md` is the human-facing
setup and operating guide. Keep both guides in English and in sync with behavior
changes.

## Purpose and boundaries

This is a Blackwell GPU catalog monitor for NVIDIA Brev with auto-start. At the
owner's request, the workflow arms auto-start by default with label `1` when the
`BREV_AUTOSTART` repository variable is unset; `0`/`false`/`off`/`no` disables it.
It may create **at most one** instance per arm label (see "Auto-start invariants"). It must never create instances in any
other path, never stop, delete, or modify instances, and never create one to
test availability. Auto-start needs a Read & Write Brev API key; with it
disarmed, a Read-only key is sufficient.

The production source is the organization associated with the API key, not a
scraped browser page. Catalog presence and an API availability flag are not
guarantees of rentable capacity. Display that distinction in notifications.
Report the API's base hourly price; do not describe it as a complete quote.

## Repository map

- `scripts/monitor.py`: HTTP clients, catalog parsing, model detection, state
  transitions, auto-start, GitHub Issue delivery, and CLI entry point.
- `.github/workflows/monitor.yml`: one Linux job, every 30 minutes, plus manual
  dispatch and relevant pushes to `main`. UTC cron is `17,47 * * * *`.
- `cloudflare/`: a Cloudflare Worker whose Cron Trigger (`7,37 * * * *` UTC)
  calls `workflow_dispatch`, because GitHub's `schedule` often skips runs. It
  has no `fetch` handler; keep it that way so the public URL cannot trigger runs.
  Its `GITHUB_TOKEN` secret is a fine-grained PAT with Actions write access to
  this repository only. Deployment happens in Cloudflare, not through this repo.
- `tests/test_monitor.py`: standard-library `unittest` tests; no live credentials.
- `README.md`: setup, notifications, monitoring semantics, limits, and local use.
- `AGENTS.md`: development instructions and invariants for future agents.

Python 3.10+ and its standard library are the only runtime requirements.
Do not add browsers, Brev CLI installation, or third-party dependencies without
a concrete need. Keep scheduled jobs short because this private repository
consumes the owner's GitHub Actions minute allowance.

## Commands

Run from the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 scripts/monitor.py --dry-run --public-catalog
git diff --check
```

The second command makes read-only public network queries. It needs no secret,
does not write GitHub Issues, and does not prove the organization-scoped query
will work. Do not run it in a tight loop.

For authenticated validation, use the existing GitHub Actions secret and inspect
the workflow result. A local `--dry-run` is also possible if the user has already
provided the key through a secure environment variable. Never ask the user to
paste a key into chat, command history, source files, or test fixtures.

## Configuration

| Name | Location | Meaning |
| --- | --- | --- |
| `BREV_API_KEY` | Actions repository secret | Brev API key, `bak-` prefix; Read & Write for auto-start |
| `BREV_API_KEY_EXPIRES_ON` | Actions repository variable | `YYYY-MM-DD` expiry date |
| `NOTIFY_USER` | Optional Actions repository variable | Username without `@`; defaults to owner |
| `BREV_AUTOSTART` | Optional Actions repository variable | Arm label; the workflow defaults it to `1`. `0`/`false`/`off`/`no` disables. The script treats an empty value as disabled |
| `GITHUB_TOKEN` | Automatically supplied by Actions | Issue reads/writes in this repository |

`ORG_ID` is not read. `fetch_catalog` resolves the key's single organization using
`GET https://brevapi.us-west-2-prod.control-plane.brev.dev/api/organizations`.
It then calls the read-only Connect RPC:

```text
POST https://api.brev.dev/devplaneapi.v1.InstanceService/ListOrganizationAvailableInstanceTypes
Authorization: Bearer <key>
Content-Type: application/json
Connect-Protocol-Version: 1
```

The JSON request uses `organizationId` and `options`. Keep
`includeUnavailable: true` to detect listed models even before capacity appears,
`includeCpu: false`, and `skipAccessFilter: false`. The response uses camelCase
fields: `items`, `supportedGpus`, `isAvailable`, `basePrice`, and
`availableLocations`. An empty protobuf response can be `{}`.

These interfaces were traced from the official CLI:

- [Catalog queries](https://github.com/brevdev/brev-cli/blob/4d93d635e09f76f6198c2f5a9f26fe7992817a3b/pkg/store/instancetypes.go)
- [Organization discovery](https://github.com/brevdev/brev-cli/blob/4d93d635e09f76f6198c2f5a9f26fe7992817a3b/pkg/store/organization.go)
- [Instance list and creation](https://github.com/brevdev/brev-cli/blob/45bd8d47e9dcd10186797763f53c74fd3fb15d2a/pkg/store/workspace.go)
  and [`brev create` defaults](https://github.com/brevdev/brev-cli/blob/45bd8d47e9dcd10186797763f53c74fd3fb15d2a/pkg/cmd/gpucreate/gpucreate.go)

Auto-start lists instances with
`GET https://brevapi.us-west-2-prod.control-plane.brev.dev/api/organizations/<org>/workspaces`
and creates one with `POST` to the same path. The body mirrors brev-cli
`NewCreateWorkspacesOptions` plus `gpucreate` for API-key auth: VM mode with
Jupyter, workspace class `2x8`, template `4nbb4lg2s`, disk `120Gi`, and the
listing's `cloudCredId`. `brev create` does not send a location for a plain
instance type, so neither does the monitor.

If the API changes, verify the official source and update fixtures and parsing.
Do not silently substitute a public catalog for an authentication failure.
Public catalog access is explicitly limited to `--dry-run --public-catalog`.

## State and notification invariants

- `GPU_PATTERN` matches only B200 and B300 as whole words. B100, GB10, GB200,
  GB300, and names that only say `Blackwell` are intentionally not matched.
  Only GPU count `GPU_COUNT` (1) is accepted; multi-GPU entries are skipped
  after the count is validated, so an invalid count is still an error.
  `find_blackwell` excludes every name containing `RTX`, case-insensitively,
  before accepting a match. This exclusion also applies to RTX PRO, Server,
  compact aliases such as `RTXPro6000`, and names containing `Blackwell`.
  Exclude A6000 and H100/H200 as well. Add positive and negative tests when
  changing matching rules.
- Merge duplicate instance entries by provider, type, normalized GPU name, and
  GPU count. Aggregate regions, prices, and availability; response order must
  not produce spurious alerts.
- Missing `isAvailable` is reported as `unknown`, not a promise of stock.
- State lives in the hidden JSON marker in the bot-created
  `Brev Blackwell monitor` Issue. Search includes closed issues and paginates;
  closing the issue does not disable monitoring.
- State schema version 3 contains `matches`, `error`, `expiry_notice`,
  `last_success`, `autostart` (the consumed arm label), `autostart_error`,
  `autostart_failures` (label to count of instances that never ran), and
  `autostart_ran` (whether the current instance reached a running status).
  Versions 1 and 2 are migrated in `decode_state`. A damaged
  or unsupported state is an error, not an empty baseline. Schema changes
  require migration or an explicit recovery procedure.
- Alert on a newly observed identity, reappearance after a successful absent
  observation, or a transition to `available`. Price-only changes do not alert.
- An unsuccessful catalog query preserves the last successful GPU state and
  timestamp. Notify once per distinct failure; notify again on recovery.
- Expiry reminders are once per configured date/stage: within seven days, then
  after expiration. Missing/invalid expiry configuration is explicitly reported
  but does not disable GPU checks. Reminders use UTC dates.
- Notify via an Issue comment mentioning `NOTIFY_USER`. Include all newly
  detected/restocked configurations and base prices, splitting comments when
  necessary. The Issue body is only a 20-row preview.
- Deliver alert comments before acknowledging the new state. A notification
  failure must not mark events as delivered. A crash after delivery but before
  state persistence can cause a duplicate: delivery is at-least-once.
- Serialize workflow runs with a single concurrency group and do not cancel an
  in-progress state update. Do not replace Issue state with an expiring cache.

## Auto-start invariants

- Auto-start runs only in the scheduled/dispatched workflow path, never in
  `--dry-run`, and only after a successful catalog query. A catalog failure,
  invalid `BREV_AUTOSTART`, or missing `cloudCredId` never creates anything.
- Candidates are monitored matches (same B200/B300 single-GPU rules) whose
  listing has `isAvailable: true`, sorted cheapest base price first. There is
  intentionally no price cap; the owner chose this.
- At most one instance per arm label that actually runs. The first instance is
  `blackwell-auto-<label>`; after `n` failures it is `blackwell-auto-<label>-<n+1>`.
  Before creating, list the organization's instances; an existing instance with
  the current name consumes the label without creating another. This name check
  is what makes reruns, crashes after creation, and failed notifications safe.
- While a label is used, every run looks up its current instance. `RUNNING`,
  `STOPPING`, or `STOPPED` sets `autostart_ran`; from then on the label stays
  used forever, even if the instance is deleted or later fails. If the instance
  never ran and is `FAILURE`, `DELETING`, or gone, increment the label's failure
  count, clear `autostart`, alert, and select a replacement in the same run.
  Any other status (such as `DEPLOYING`) waits. A lookup error never creates.
- Never retry the creation `POST`. A failure leaves the label armed and alerts
  once per distinct error; the next run rechecks by name before trying again.
- After a start, record the label in state. The owner re-arms by setting
  `BREV_AUTOSTART` to a new value. The monitor never stops or deletes instances;
  notifications must say that billing continues until the owner stops it.

## Failure handling and credential safety

Use `MonitorError` for sanitized, actionable errors. Never print authentication
headers, credential values, or raw server error bodies. Reject redirects so an
Authorization header cannot be forwarded elsewhere.

Only retry reads/read-only RPCs, with bounded timeouts and backoff. Do not blindly
retry Issue-creation, notification, or instance-creation POSTs: the server may
have accepted a timed out request. Do not treat malformed data or missing authentication as zero GPUs.

Keep workflow permissions at `contents: read` and `issues: write`, keep checkout
pinned to a reviewed commit, and do not persist checkout credentials. Do not add
pull-request triggers that expose secrets to untrusted code.

## Before finishing a change

Run the unit suite. Cover any changes to aliases, payload schema, deduplication,
failure recovery, expiry boundaries, and notification delivery. Use the public
dry-run when catalog parsing changes, but confirm production separately through
an authenticated workflow run. Confirm notifications include price information
and that the auto-start creation call remains the only provisioning call. Tests
must mock every Brev request; never exercise auto-start against the live API.

Tests invoking `run_monitor` must isolate `GITHUB_STEP_SUMMARY` from the CI
environment. Disable it for mocked delivery checks, or point it to a temporary
file when explicitly testing summary output. Never publish simulated inventory
or expected test failures into the live Actions summary.

Update the human README when setup, matching, schedule, or alert semantics change.
Do not claim a deployment is working based only on local tests or a successful
push. Confirm the Actions result, or explicitly state that it is still unknown.
