# Agent guide

Read this file before changing the project. `README.md` is the human-facing
setup and operating guide. Keep both guides in English and in sync with behavior
changes.

## Purpose and boundaries

This is a notification-only Blackwell GPU catalog monitor for NVIDIA Brev.
It must never create, rent, reserve, purchase, or modify GPU instances, including
as a way to test availability. A Read-only Brev API key is sufficient.

The production source is the organization associated with the API key, not a
scraped browser page. Catalog presence and an API availability flag are not
guarantees of rentable capacity. Display that distinction in notifications.
Report the API's base hourly price; do not describe it as a complete quote.

## Repository map

- `scripts/monitor.py`: HTTP clients, catalog parsing, model detection, state
  transitions, GitHub Issue delivery, and CLI entry point.
- `.github/workflows/monitor.yml`: one Linux job, every 30 minutes, plus manual
  dispatch and relevant pushes to `main`. UTC cron is `17,47 * * * *`.
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
| `BREV_API_KEY` | Actions repository secret | Brev Read API key, `bak-` prefix |
| `BREV_API_KEY_EXPIRES_ON` | Actions repository variable | `YYYY-MM-DD` expiry date |
| `NOTIFY_USER` | Optional Actions repository variable | Username without `@`; defaults to owner |
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

If the API changes, verify the official source and update fixtures and parsing.
Do not silently substitute a public catalog for an authentication failure.
Public catalog access is explicitly limited to `--dry-run --public-catalog`.

## State and notification invariants

- `GPU_PATTERN` matches Blackwell model names, including Brev's compact
  `RTXPro6000` alias. Exclude RTX6000Ada, A6000, H100/H200, and RTX4090.
  Add positive and negative tests when changing matching rules.
- Merge duplicate instance entries by provider, type, normalized GPU name, and
  GPU count. Aggregate regions, prices, and availability; response order must
  not produce spurious alerts.
- Missing `isAvailable` is reported as `unknown`, not a promise of stock.
- State lives in the hidden JSON marker in the bot-created
  `Brev Blackwell monitor` Issue. Search includes closed issues and paginates;
  closing the issue does not disable monitoring.
- State schema version 1 contains `matches`, `error`, `expiry_notice`, and
  `last_success`. A damaged or unsupported state is an error, not an empty
  baseline. Schema changes require migration or an explicit recovery procedure.
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

## Failure handling and credential safety

Use `MonitorError` for sanitized, actionable errors. Never print authentication
headers, credential values, or raw server error bodies. Reject redirects so an
Authorization header cannot be forwarded elsewhere.

Only retry reads/read-only RPCs, with bounded timeouts and backoff. Do not blindly
retry Issue-creation or notification POSTs: the server may have accepted a timed
out request. Do not treat malformed data or missing authentication as zero GPUs.

Keep workflow permissions at `contents: read` and `issues: write`, keep checkout
pinned to a reviewed commit, and do not persist checkout credentials. Do not add
pull-request triggers that expose secrets to untrusted code.

## Before finishing a change

Run the unit suite. Cover any changes to aliases, payload schema, deduplication,
failure recovery, expiry boundaries, and notification delivery. Use the public
dry-run when catalog parsing changes, but confirm production separately through
an authenticated workflow run. Confirm notifications include price information
and no provisioning calls exist.

Update the human README when setup, matching, schedule, or alert semantics change.
Do not claim a deployment is working based only on local tests or a successful
push. Confirm the Actions result, or explicitly state that it is still unknown.
