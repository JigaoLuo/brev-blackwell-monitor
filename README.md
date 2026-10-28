# Brev Blackwell Monitor

Checks the Brev GPU catalog on a schedule (a Cloudflare Cron Trigger, with
GitHub's own schedule as a backup) and sends alerts through a GitHub
Issue in this repository. Optional [auto-start](#auto-start), off by default,
keeps **one** B200/B300 single-GPU instance running while switched on, which
**incurs GPU rental charges** until you stop it.

This README is for human users. Coding agents should read [AGENTS.md](AGENTS.md)
before making changes.

## GitHub setup

1. Open **Settings > Secrets and variables > Actions > Secrets > New repository secret**. Add `BREV_API_KEY` with a Brev API key: **Read** is enough for notifications; auto-start needs **Read & Write**. Never put the key in files or chat.
2. Under **Variables > New repository variable**, add `BREV_API_KEY_EXPIRES_ON` with the expiration date selected when creating the key, in `YYYY-MM-DD` format. Without it, GPU checks still run, but the monitor warns that expiry reminders are not enabled.
3. Make sure **Settings > General > Features > Issues** is enabled. Alerts mention the repository owner by default. To notify another user with repository access, add a `NOTIFY_USER` variable containing their username without `@`.
4. Open **Actions > Blackwell monitor > Run workflow**, select `main`, and run it. The initial code push also triggers a check. If that run started before you configured the key, add the secret and rerun the workflow; no new push is needed.
5. Click **Subscribe** on the automatically created **Brev Blackwell monitor** Issue. Enable email notifications for participation and mentions in [GitHub notification settings](https://github.com/settings/notifications), and verify your email address. The first run mentions you so you can confirm the notification channel.

GitHub Actions supplies `GITHUB_TOKEN` automatically; no personal GitHub token is
needed. You do not need `ORG_ID`: the script discovers the organization associated
with the Brev API key. An existing `ORG_ID` variable is ignored.
The workflow requests only `contents: read` and `issues: write`.

Issue alerts cannot be delivered if the GitHub API is unavailable or the workflow
fails before the script runs. Enable Actions failure notifications as well, and
check the Actions page.

## Reading workflow results

Use the **Live Brev organization check** section of the run summary, or the
**Check Brev and notify** step, for actual results. The summary includes the check
time, data source, and one explicit outcome:

- **Result: MATCHES FOUND**: matching GPU configurations are listed with their base prices and API availability.
- **Result: NO MATCHES**: the live query succeeded, but no monitored GPU configurations were found in the API key's organization catalog. This is not a platform-wide result.
- **Result: CHECK FAILED**: the query or processing failed, so no inventory conclusion can be drawn. This must not be interpreted as zero matching GPUs.

The **Run unit tests** step uses simulated data and is not a GPU inventory check.
Test fixtures such as provider `example`, location `region-a`, and price
`USD 4.00/h` are not real offers. Tests must not write their results to the
live monitoring summary.

## What is monitored

- The monitor uses the same official service interfaces as the [Brev CLI](https://github.com/brevdev/brev-cli/blob/4d93d635e09f76f6198c2f5a9f26fe7992817a3b/pkg/store/instancetypes.go), scoped to the API key's organization. It does not scrape HTML and may not exactly reproduce every web-console filter.
- GPU names matched: **B200 and B300 only**, and only **single-GPU (1x) configurations**; multi-GPU listings such as `B300x8` are ignored. Other Blackwell models (B100, GB10, GB200, GB300) and names that only say `Blackwell` are not matched. **All RTX models are excluded**, including RTX PRO, Server, and GeForce variants, even when their names contain `Blackwell`. A6000 and H100/H200 are also not matched.
- Alerts are sent for **initial detections, new configurations, reappearances after absence, and API availability changing to available**. Blackwell listings already present on the first run also trigger an alert. Queries include unavailable catalog entries; notifications distinguish listing presence from availability.
- Notifications include the base prices reported by the API at that time. Price-only changes do not trigger separate alerts.
- Tables include GPU count, instance type, provider, locations, and base hourly prices. `unknown` means the API did not explicitly report availability. Even `available` does not guarantee that provisioning will succeed. Prices may vary by region, and storage or other charges may be additional.
- State is stored as hidden JSON in the same Issue. Unchanged results update the state without repeated comments. The monitor does not depend on an expiring Actions cache, commit state files, or store credentials in its state.
- The Issue body previews the first 20 configurations. Notification comments include all configurations newly detected or becoming available, together with their base prices. Large notifications are split into multiple comments.
- Authentication failures, API errors, and unexpected response formats fail the workflow and trigger an Issue alert. Failures preserve the previous successful result instead of treating an error as "no Blackwell GPUs."
- The monitor sends one reminder within seven days before the configured key expiration date and another after that date has passed, using UTC dates. When replacing a key, update both the secret and the date variable. The script does not extend or rotate keys automatically.

Do not delete the state Issue or edit its title or hidden JSON. Closing the Issue
does not stop monitoring; the next run reopens it. To stop, use
**Actions > Blackwell monitor > ... > Disable workflow**.

The monitor reads the state Issue directly by number: Issue `#1` by default, or
the number in the optional repository variable `STATE_ISSUE`. GitHub's issue
listings have occasionally omitted it, which used to create duplicate
**Brev Blackwell monitor** Issues with empty state. Duplicates can be closed or
deleted; only the state Issue is used.

State is acknowledged only after notification comments are delivered. If GitHub
fails after delivery but before state persistence, the next run may repeat an
alert rather than risk losing it.

## Auto-start

`BREV_AUTOSTART` is a switch set as an Actions repository variable. It is
**off** when the variable is missing. Auto-start requires a **Read & Write**
`BREV_API_KEY`. Monitoring and notifications always cover both B200 and B300;
the switch only decides what may be started.

| Value | Starts |
| --- | --- |
| `off` (default) | nothing |
| `b200` | only a single-GPU B200 |
| `b300` | only a single-GPU B300 |
| `on` | the cheapest available single-GPU B200 or B300 |

```bash
gh variable set BREV_AUTOSTART --body b300 -R JigaoLuo/brev-blackwell-monitor
gh variable set BREV_AUTOSTART --body off  -R JigaoLuo/brev-blackwell-monitor
```

`on` also accepts `1`, `true`, `yes`, `enabled`; `off` also accepts `0`,
`false`, `no`, `disabled`. Any other value is reported and treated as `off`.

Whenever the switch is not `off`, the monitor keeps exactly one auto-started
instance. On each check, if no instance started by the monitor is live and an
allowed configuration is listed as `available`, it creates one from the cheapest
such listing. There is **no price cap**. You are mentioned in an
**Instance started** comment with the instance ID, type, and base price.

The monitor recognizes its own instances by the `blackwell-auto-` name prefix and
picks the names itself. Only an instance that is running, deploying, starting,
or stopping blocks a second one. A failed provisioning (`FAILURE`), a stopped or
deleted instance, or any unrecognized status frees the slot, so a start that
never came up does not block the next one. An instance still deploying or
starting 60 minutes after it was created is treated as failed. Instances you
create yourself are ignored. While the switch is not `off`, each run's summary
lists the auto-started instances and their statuses.

**When you are done, set the switch to `off` first, then stop or delete the
instance.** While it is not `off`, stopping or deleting the instance lets the next
check start a new one, and billing starts again.

- The check uses Brev's live instance list, so reruns, crashes, or lost Issue
  state cannot start a second instance.
- A failed start (for example, capacity disappeared) is reported once and tried
  again on the next check. Creation requests are never retried within a run.
- The monitor never stops or deletes instances. Billing continues until you do.
- `--dry-run` never starts instances.

## Schedule and usage

The UTC cron expression is `17,47 * * * *`: runs start at minutes 17 and 47 of
each hour, avoiding the busiest top-of-hour period. GitHub schedules are best
effort: under load, scheduled runs are delayed or skipped entirely, and gaps of
several hours have been observed. They are not guaranteed to catch short-lived
capacity.

For reliable timing, the Cloudflare Worker in [`cloudflare/`](cloudflare/)
triggers the workflow through `workflow_dispatch` at minutes 7 and 37 (UTC).
To set it up:

1. Create a GitHub **fine-grained personal access token** limited to this
   repository, with **Actions: Read and write** permission only.
2. In the Cloudflare dashboard, create a Worker named `brev-blackwell-dispatch`
   and replace its code with [`cloudflare/dispatch-worker.js`](cloudflare/dispatch-worker.js).
3. Under the Worker's **Settings > Variables and Secrets**, add a **Secret**
   named `GITHUB_TOKEN` containing the token.
4. Under **Settings > Trigger events**, add a **Cron Trigger** `7,37 * * * *`.

Any cadence works, down to every minute (`* * * * *`); the Cron Trigger in the
Cloudflare dashboard is authoritative, not `wrangler.toml`.

Alternatively, deploy with `npx wrangler deploy` from `cloudflare/` and set the
secret with `npx wrangler secret put GITHUB_TOKEN`. The Worker has no HTTP
endpoint, so its URL cannot trigger runs. Runs started by the Worker appear in
Actions with the `workflow_dispatch` event. The GitHub schedule stays as a
backup; runs are serialized, so overlapping triggers are safe. When the token
expires, dispatched runs stop silently; the Cloudflare dashboard shows the
failed cron invocations.
The push trigger is configured for the `main` branch.

There is one Linux job, with no third-party Python dependencies, browser
installation, or Brev CLI installation. Each job has a five-minute timeout.
Brev's catalog occasionally stalls for tens of seconds shortly after the hour,
so read requests get three attempts (20-second timeout, then 5- and 15-second
pauses) before a check is reported as failed.
At one billed minute per run, usage is approximately 1,440 minutes over 30 days
or 1,488 minutes over 31 days for the 30-minute cadence. Two billed minutes per
run doubles those estimates. GitHub's backup schedule adds its own runs on top
of the Cloudflare triggers.
Actual GitHub usage is authoritative, and other private repositories share the
account's allowance.

## Local development

Requires Python 3.10+ and uses only the standard library.

```bash
python3 -m unittest discover -s tests -v
```

Preview the public catalog without credentials or GitHub writes:

```bash
python3 scripts/monitor.py --dry-run --public-catalog
```

A public-catalog preview does not prove that your organization can rent the same
instances. The production workflow always uses the organization catalog and
never silently falls back to public data when authentication fails.

If `BREV_API_KEY` is already supplied securely through your environment, preview
the organization catalog without GitHub writes:

```bash
python3 scripts/monitor.py --dry-run
```

The script uses fixed service endpoints, does not print credentials or raw error
responses, and does not follow redirects that could forward authentication data.
