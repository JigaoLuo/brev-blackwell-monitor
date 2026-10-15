# Brev Blackwell Monitor

Checks the Brev GPU catalog every 30 minutes and sends alerts through a GitHub
Issue in this private repository. [Auto-start](#auto-start) is **on by default**:
it starts **one** B200/B300 single-GPU instance when one becomes available, which
**incurs GPU rental charges** until you stop it. Set `BREV_AUTOSTART` to `0` to
only receive notifications.

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

State is acknowledged only after notification comments are delivered. If GitHub
fails after delivery but before state persistence, the next run may repeat an
alert rather than risk losing it.

## Auto-start

On by default and requires a **Read & Write** `BREV_API_KEY`. Without a
`BREV_AUTOSTART` repository variable, the workflow uses the arm label `1`. To
use another label, set `BREV_AUTOSTART` to 1-20 lowercase letters, digits, or
hyphens.

On the next successful check where a monitored configuration (B200/B300, single
GPU) is listed with API availability `available`, the monitor creates one
instance named `blackwell-auto-<label>` using the cheapest such listing. There is
**no price cap**. It uses the same defaults as `brev create`: VM mode with
Jupyter and a 120 GiB disk. You are mentioned in an **Instance started** comment
with the instance ID, type, and base price.

- **One instance per label.** After a start, that label is used up; to allow
  another start, change `BREV_AUTOSTART` to a new value such as `2`.
- **Duplicate protection.** Before creating, the monitor checks whether an
  instance named `blackwell-auto-<label>` already exists. Reruns, crashes, or
  failed notifications therefore cannot start a second instance for the same label.
- **Failures.** A failed start (for example, capacity disappeared) is reported
  once and retried on the next run. Creation requests are never retried within a
  run. If a request timed out, check the Brev console; the next run detects the
  instance by name.
- **Billing.** The monitor never stops or deletes instances. Stop or delete the
  instance in the Brev console when you are done.
- To disable, add the repository variable `BREV_AUTOSTART` with value `0`
  (`false`, `off`, or `no` also work). Deleting the variable turns auto-start
  back on with label `1`. `--dry-run` never starts instances.

## Schedule and usage

The UTC cron expression is `17,47 * * * *`: runs start at minutes 17 and 47 of
each hour, avoiding the busiest top-of-hour period. GitHub schedules are best
effort: under load, scheduled runs are delayed or skipped entirely, and gaps of
several hours have been observed. They are not guaranteed to catch short-lived
capacity. For tighter timing, trigger the workflow through `workflow_dispatch`
from an external scheduler.
The push trigger is configured for the `main` branch.

There is one Linux job, with no third-party Python dependencies, browser
installation, or Brev CLI installation. Each job has a three-minute timeout.
At one billed minute per run, usage is approximately 1,440 minutes over 30 days
or 1,488 minutes over 31 days. Two billed minutes per run doubles those estimates.
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
