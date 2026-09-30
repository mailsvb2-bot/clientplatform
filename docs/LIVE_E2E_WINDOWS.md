# ClientPlatform Windows 10 + Windows 11 live E2E runners

## Purpose

The live contour uses two dedicated synthetic-user workstations so Windows 10
and Windows 11 are independently proven. A pass on one operating system never
counts as a pass on the other.

These are test machines, not developer PCs, and must contain no personal
messenger sessions or production credentials.

## Machine contract

Provision two clean x64 VMs:

- **Windows 10 22H2 x64**, build 19045 or newer in the Windows 10 line;
- **Windows 11 x64**, build 22000 or newer.

Each VM has one dedicated local test user. Install on both:

- GitHub Actions runner;
- Python 3.12 (the workflow also pins it through `setup-python`);
- Telegram Desktop;
- MAX Desktop;
- Microsoft Edge;
- Google Chrome.

Log Telegram, VK and MAX into dedicated test accounts once on each VM. Keep
those profiles inside that VM user's profile. Do not copy session files into the
repository, GitHub Secrets or Actions artifacts.

Both runners carry the common labels:

`self-hosted, Windows, X64, clientplatform-live-e2e`

and exactly one OS-specific label:

- Windows 10: `clientplatform-windows-10`
- Windows 11: `clientplatform-windows-11`

The workflow uses a two-leg matrix and requires both legs. Evidence artifacts
are named separately for `windows-10` and `windows-11`, so one machine cannot
silently substitute for the other.

Do **not** install either runner as a Windows service. UI Automation needs an
interactive desktop. Start `run.cmd` after logon under the dedicated test user
(for example with Task Scheduler). Lock down remote access, disk encryption and
the VM console according to the host policy.

Windows 10 is used here strictly as an isolated compatibility-test worker. It
must not become a production administration host or hold production credentials.

## Public-repository runner isolation

This repository is public, so both Windows VMs must be treated as exposed test
workers, not as production administration hosts. Keep only synthetic messenger
identities and staging data on them. Do not place production SSH keys, reusable
GitHub PATs, production cloud credentials, personal browser profiles or other
product credentials in either Windows user profile.

Keep GitHub Actions configured to require approval for workflows from outside
collaborators before they can run. The dedicated labels are routing, not a
security boundary by themselves. If the repository is later moved to an
organization, prefer a runner group restricted to the exact live-E2E workflow.

The `clientplatform_live_e2e` Environment must allow only `main`. The workflow
also checks out `main` explicitly and has a read-only `GITHUB_TOKEN`, but
machine-level isolation remains mandatory because desktop session material lives
on the VMs rather than in GitHub Secrets.

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

After **both** VMs and sessions are verified, set the repository-level variable
`CLIENTPLATFORM_LIVE_E2E_ENABLED=1`. It must be repository-level because the
job-level `if` is evaluated before environment-scoped variables are injected.
Until then the scheduled job is skipped rather than accumulating queued or
false-red runs.

## Local validation on either VM

From a clean checkout of trusted `main`, set the OS identity to the machine
being tested:

```powershell
$env:APP_ENV = "staging"
$env:CLIENTPLATFORM_LIVE_E2E = "1"
$env:CLIENTPLATFORM_LIVE_E2E_TEST_ACCOUNTS = "1"
$env:CLIENTPLATFORM_LIVE_E2E_PRODUCTION_CREDENTIALS = "0"
$env:CLIENTPLATFORM_LIVE_E2E_REAL_MONEY = "0"
$env:CLIENTPLATFORM_E2E_EXPECTED_WINDOWS = "windows-10" # use windows-11 on the Win11 VM
$env:CLIENTPLATFORM_E2E_RUNNER_IDENTITY = "$env:TEMP\clientplatform-runner-identity.json"

.\scripts\live_e2e\windows\validate_runner.ps1
python scripts/check_live_e2e_manifest.py
python scripts/all_user_scenario_gate.py
python scripts/clientplatform_live_e2e.py --preflight
python scripts/clientplatform_live_e2e.py --execute
```

The PowerShell validator reads the OS contract from
`config/live_e2e_manifest.json`, checks the real Windows caption/build/x64
architecture and interactive runner session, and writes a non-secret runner
identity file. Python refuses to execute live probes unless that validated OS
identity matches the matrix leg.

The first live run on each OS should be observed at the VM console. If a client
changes its accessibility tree, fix the repository driver/selector rather than
adding sleeps until the run becomes green.

## Account hygiene

Test accounts must not share phone numbers, recovery email, cookies or 2FA
secrets with personal accounts where the provider allows separation. The test
tenant must contain synthetic names/data only. Disable unrelated desktop
notifications so screenshots cannot capture unrelated information.

## Reading the result

Do not treat semantic coverage and live execution as the same metric.
`semantic_contract.native_actions` is the exhaustive repository contract.
`summary.asserted_live_probes` is the number of provider/client steps that
actually produced a newly visible expected result in that OS run.

A changing screen is not enough for PASS. The desktop driver requires both a
changed accessibility-tree fingerprint and a newly appearing expected text.

Overall Windows compatibility is green only when **both** the `windows-10` and
`windows-11` matrix legs pass on their correctly validated machines.

Mutation-heavy journeys stay non-live until a deterministic staging fixture and
reset path exist. Do not turn them green by accepting error/help responses or
by spending real money.
