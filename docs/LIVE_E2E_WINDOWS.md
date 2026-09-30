# ClientPlatform Windows 11 live E2E runner

## Purpose

This runner is a dedicated synthetic-user workstation for ClientPlatform. It is
not a developer PC and must contain no personal messenger sessions or production
credentials.

## Machine contract

Use a clean Windows 11 x64 VM with one dedicated local user. Install:

- GitHub Actions runner;
- Python 3.12 (the workflow also pins it through `setup-python`);
- Telegram Desktop;
- MAX Desktop;
- Microsoft Edge;
- Google Chrome.

Log Telegram, VK and MAX into dedicated test accounts once. Keep those profiles
inside the VM user profile. Do not copy session files into the repository,
GitHub Secrets or Actions artifacts.

The GitHub runner must have labels:

`self-hosted, Windows, X64, clientplatform-live-e2e`

Do **not** install the runner as a Windows service. UI Automation needs an
interactive desktop. Start `run.cmd` after logon under the dedicated test user
(for example with Task Scheduler). Lock down remote access, disk encryption and
the VM console according to the host policy.

## Public-repository runner isolation

This repository is public, so the Windows VM must be treated as an exposed test
worker, not as a production administration host. Keep only synthetic messenger
identities and staging data on it. Do not place production SSH keys, reusable
GitHub PATs, production cloud credentials, personal browser profiles or other
product credentials in that Windows user profile.

Keep GitHub Actions configured to require approval for workflows from outside
collaborators before they can run. The dedicated label is routing, not a security
boundary by itself. If the repository is later moved to an organization, prefer
a runner group restricted to the exact live-E2E workflow.

The `clientplatform_live_e2e` Environment must allow only `main`. The workflow
also checks out `main` explicitly and has a read-only `GITHUB_TOKEN`, but the
machine-level isolation remains mandatory because desktop session material lives
on the VM rather than in GitHub Secrets.

## Repository configuration

Create GitHub Environment `clientplatform_live_e2e` and restrict deployments
for that environment to the `main` branch. Configure the application/session
paths and test-chat locations as repository or environment variables:

- `CLIENTPLATFORM_E2E_TELEGRAM_EXE`
- `CLIENTPLATFORM_E2E_TELEGRAM_PROCESS` (usually `Telegram`)
- `CLIENTPLATFORM_E2E_TELEGRAM_CHAT`
- `CLIENTPLATFORM_E2E_MAX_EXE`
- `CLIENTPLATFORM_E2E_MAX_PROCESS`
- `CLIENTPLATFORM_E2E_MAX_CHAT`
- `CLIENTPLATFORM_E2E_VK_BROWSER` = `edge` or `chrome`
- `CLIENTPLATFORM_E2E_VK_CHAT_URL`
- `CLIENTPLATFORM_E2E_COCKPIT_URL`
- `CLIENTPLATFORM_E2E_EDGE_EXE`
- `CLIENTPLATFORM_E2E_CHROME_EXE`

After the VM and sessions are verified, set the **repository-level** variable
`CLIENTPLATFORM_LIVE_E2E_ENABLED=1`. It must be repository-level because the
job-level `if` is evaluated before environment-scoped variables are injected.
Until then the scheduled job is skipped rather than accumulating queued/false-red
runs.

## Local validation on the VM

From a clean checkout of trusted `main`:

```powershell
$env:APP_ENV = "staging"
$env:CLIENTPLATFORM_LIVE_E2E = "1"
$env:CLIENTPLATFORM_LIVE_E2E_TEST_ACCOUNTS = "1"
$env:CLIENTPLATFORM_LIVE_E2E_PRODUCTION_CREDENTIALS = "0"
$env:CLIENTPLATFORM_LIVE_E2E_REAL_MONEY = "0"

python scripts/check_live_e2e_manifest.py
python scripts/all_user_scenario_gate.py
python scripts/clientplatform_live_e2e.py --preflight
python scripts/clientplatform_live_e2e.py --execute
```

The first live run should be observed at the VM console. If a client changes its
accessibility tree, fix the repository driver/selector rather than adding sleeps
until the run becomes green.

## Account hygiene

Test accounts must not share phone numbers, recovery email, cookies or 2FA
secrets with personal accounts where the provider allows separation. The test
tenant must contain synthetic names/data only. Disable unrelated desktop
notifications so screenshots cannot capture unrelated information.


## Reading the result

Do not treat semantic coverage and live execution as the same metric.
`semantic_contract.native_actions` is the exhaustive repository contract.
`summary.asserted_live_probes` is the number of provider/client steps that
actually produced a newly visible expected result in this run.

A changing screen is not enough for PASS. The desktop driver requires both a
changed accessibility-tree fingerprint and a newly appearing expected text.

Mutation-heavy journeys stay non-live until a deterministic staging fixture and
reset path exist. Do not turn them green by accepting error/help responses or
by spending real money.
