# Yandex Direct rollout for ClientPlatform

This runbook activates only the personal advertising-account connection and read-only verification path. It does not authorize campaign launch, impressions, budget changes or advertising spend.

## Current external contract

- OAuth application type: API/debug application.
- Immutable Redirect URI: `https://oauth.yandex.ru/verification_code`.
- Production outbound IPv4 allowed in Yandex Direct: `185.104.114.163/32`.
- Telegram control bot: `@clientplatform_bot`.
- Advertising connections stay disabled until Yandex approves the Direct API access request.
- Spend mutations remain disabled after connections are enabled.

Never put the OAuth client secret, access token, refresh token or age identity in GitHub, Telegram, shell history, screenshots or support messages.

## 1. Preconditions after Yandex approval

Verify in the Yandex Direct API console that the application has full API access. Do not continue while the application is pending, rejected or blocked.

On the production server:

```bash
cd /opt/clientplatform
git fetch --prune origin
git checkout main
git pull --ff-only origin main
git status --short
```

`git status --short` must be empty.

## 2. Provision the encrypted advertising credential identity

Run the repository-owned provisioning script as root:

```bash
cd /opt/clientplatform
sudo CLIENTPLATFORM_AD_CREDENTIAL_HOST_DIR=/var/lib/clientplatform/ad-secrets \
  bash deploy/clientplatform/configure-ad-credential-age.sh
```

The command must end with:

```text
CLIENTPLATFORM_AD_CREDENTIAL_AGE_OK:/var/lib/clientplatform/ad-secrets/identity.txt
```

Do not print or copy the identity file. Verify only metadata:

```bash
sudo stat -c '%a %U:%G %n' \
  /var/lib/clientplatform/ad-secrets \
  /var/lib/clientplatform/ad-secrets/identity.txt
```

The directory must be `0700`; the identity file must be `0600`.

## 3. Put OAuth credentials into the protected production environment

The environment file is:

```text
/opt/clientplatform/deploy/clientplatform/clientplatform.env
```

It must be a regular file with mode `0600`.

Use an interactive root editor so the secret does not enter shell history:

```bash
sudoedit /opt/clientplatform/deploy/clientplatform/clientplatform.env
```

Set the following values:

```dotenv
CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID=3bf66b0ded6b407fbdeaf7db0d5888e3
CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_SECRET=<REAL_SECRET_FROM_YANDEX_OAUTH>
CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI=https://oauth.yandex.ru/verification_code
CLIENTPLATFORM_AD_CREDENTIAL_IDENTITY_FILE=/run/secrets/clientplatform-ad/identity.txt
CLIENTPLATFORM_AD_CREDENTIAL_HOST_DIR=/var/lib/clientplatform/ad-secrets
CLIENTPLATFORM_YANDEX_DIRECT_REPORT_TIMEZONE=Europe/Moscow
CLIENTPLATFORM_AD_CONNECTIONS_ENABLED=1
CLIENTPLATFORM_AD_SPEND_MUTATIONS_ENABLED=0
```

Do not enable spend mutations during the first rollout.

Verify names and non-secret values without printing the secret:

```bash
cd /opt/clientplatform
sudo python3 - <<'PY'
from pathlib import Path

path = Path('deploy/clientplatform/clientplatform.env')
values = {}
for raw in path.read_text(encoding='utf-8').splitlines():
    if raw and not raw.lstrip().startswith('#') and '=' in raw:
        key, value = raw.split('=', 1)
        values[key.strip()] = value.strip()

required = {
    'CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID',
    'CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_SECRET',
    'CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI',
    'CLIENTPLATFORM_AD_CREDENTIAL_IDENTITY_FILE',
    'CLIENTPLATFORM_AD_CREDENTIAL_HOST_DIR',
    'CLIENTPLATFORM_YANDEX_DIRECT_REPORT_TIMEZONE',
}
missing = sorted(key for key in required if not values.get(key))
if missing:
    raise SystemExit('missing: ' + ', '.join(missing))
if values['CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI'] != 'https://oauth.yandex.ru/verification_code':
    raise SystemExit('wrong OAuth redirect')
if values.get('CLIENTPLATFORM_AD_CONNECTIONS_ENABLED') != '1':
    raise SystemExit('ad connections are not enabled')
if values.get('CLIENTPLATFORM_AD_SPEND_MUTATIONS_ENABLED') != '0':
    raise SystemExit('spend mutations must remain disabled')
print('CLIENTPLATFORM_YANDEX_READ_ONLY_ENV_OK')
PY
```

## 4. Run fail-closed checks and deploy

```bash
cd /opt/clientplatform
sudo python3 scripts/clientplatform_prepare_production_env.py \
  deploy/clientplatform/clientplatform.env
sudo python3 scripts/clientplatform_production_preflight.py \
  --env-file deploy/clientplatform/clientplatform.env
sudo python3 scripts/clientplatform_production_deploy.py \
  --timeout-seconds 240
```

Required success markers:

```text
CLIENTPLATFORM_PRODUCTION_PREFLIGHT_OK
CLIENTPLATFORM_PRODUCTION_DEPLOY_OK:<evidence-path>
```

## 5. Complete the first OAuth connection in Telegram

1. Open `@clientplatform_bot` as the business owner.
2. Open **Get clients** → **Personal advertising accounts**.
3. Press **Connect Yandex Direct**.
4. Open the official Yandex authorization page from the bot.
5. Select the intended Direct account and approve access.
6. Copy the one-time confirmation code displayed by Yandex exactly as shown. Treat it as an opaque value: it may be alphanumeric and must not be assumed to have a fixed digit count.
7. Send the code to the bot within ten minutes.
8. Confirm that the bot displays the connected Direct login.

The code is one-time. An expired, mistyped or already-used code requires starting the connection again.

## 6. Read-only smoke test

The first production verification must only:

- resolve the connected Direct account identity;
- list accessible campaigns;
- read campaign state/status;
- obtain a statistics/report response when the report path is enabled;
- record a sanitized audit result.

Do not confirm a publication job, launch a campaign, resume a campaign, change a strategy, change a bid or change a budget during this smoke test.

Check application logs without printing environment variables or credential files:

```bash
cd /opt/clientplatform/deploy/clientplatform
COMPOSE=(sudo docker compose)
if [[ -f .env ]]; then
  COMPOSE+=(--env-file .env)
fi
COMPOSE+=(--env-file clientplatform.env -f compose.production.yml)
"${COMPOSE[@]}" ps
"${COMPOSE[@]}" logs --tail=200 app
```

## 7. Immediate rollback of the integration

To disable new advertising connections without changing the rest of ClientPlatform:

```bash
sudoedit /opt/clientplatform/deploy/clientplatform/clientplatform.env
```

Set:

```dotenv
CLIENTPLATFORM_AD_CONNECTIONS_ENABLED=0
CLIENTPLATFORM_AD_SPEND_MUTATIONS_ENABLED=0
```

Then redeploy through the normal production procedure. Existing encrypted credentials remain inaccessible to user interfaces while the feature is disabled. A connected account should also be revoked through the Telegram disconnect flow or the Yandex OAuth account page when permanent removal is required.

## Prohibited shortcuts

- do not paste the Client secret or OAuth token into Telegram;
- do not use a debug token as the production connection;
- do not change the Redirect URI back to the ClientPlatform callback;
- do not set `CLIENTPLATFORM_AD_SPEND_MUTATIONS_ENABLED=1` for the first rollout;
- do not remove the production IP restriction in Yandex Direct;
- do not bypass the age identity permission checks;
- do not perform production deployment from a dirty working tree;
- do not claim that advertising is live until a separately approved real-money test is completed and evidenced.
