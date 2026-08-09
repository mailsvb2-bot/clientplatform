# ADR-0075: Yandex Direct screen-code OAuth in Telegram

**Status:** Accepted for implementation; production rollout remains disabled  
**Date:** 2026-08-06

## Context

The Yandex OAuth application registered for ClientPlatform uses the API/debug application type. Its immutable redirect URI is:

```text
https://oauth.yandex.ru/verification_code
```

The existing ClientPlatform implementation assumed a callback owned by ClientPlatform:

```text
https://clientplatform.ru/oauth/yandex-direct/callback
```

That callback cannot receive an authorization code for the registered application. Enabling the old production contract would therefore pass local checks but fail during real user authorization.

Yandex supports a confirmation-code flow for applications that cannot receive the code from a redirect URL. The user authorizes on Yandex, Yandex displays a short-lived authorization code, and the application exchanges that code for an OAuth token. PKCE remains applicable. The displayed authorization code is an opaque provider value: ClientPlatform must not depend on a fixed digit count or a numeric-only format.

## Decision

ClientPlatform uses the Yandex screen-code flow inside the Telegram owner journey:

1. The owner presses **Connect Yandex Direct**.
2. ClientPlatform creates a one-time tenant-scoped OAuth session and PKCE verifier.
3. Telegram opens the official Yandex authorization URL.
4. Yandex displays a one-time opaque confirmation code.
5. The owner sends that code to the ClientPlatform bot.
6. The bot validates the bounded opaque code and makes a best-effort attempt to delete the Telegram message containing it.
7. ClientPlatform exchanges the code server-side, resolves the Yandex account identity, revalidates current membership, encrypts the token bundle and activates the business-scoped connection.

The code is accepted only in the initiating Telegram FSM session. ClientPlatform treats it as opaque printable ASCII, rejects whitespace/control/non-ASCII input, enforces a bounded length, and tolerates the copied visual `# ` prefix supported by the shared normalizer. The code is never logged. Provider failures are returned to the user as sanitized messages.

The token exchange sends only the parameters documented for the browser-displayed screen-code flow:

```text
grant_type=authorization_code
code=<opaque one-time confirmation code>
client_id=<application id>
client_secret=<application secret>
code_verifier=<PKCE verifier>
```

The callback-only `redirect_uri` parameter is deliberately omitted from the token request.

Production preflight requires the exact immutable redirect URI:

```text
https://oauth.yandex.ru/verification_code
```

When that URI is configured, ClientPlatform does not register its legacy HTTP callback route. The Caddy path may remain in the static routing configuration temporarily, but the application returns no OAuth callback handler for the screen-code deployment.

## Security properties

- OAuth state and PKCE verifier remain one-time and tenant-scoped.
- The verifier is encrypted at rest before authorization begins.
- The confirmation code is short-lived and never persisted as a credential.
- The Telegram message containing a validly formatted code is deleted when Telegram permits deletion.
- A code submitted from a different Telegram user is rejected before any provider call.
- The OAuth token bundle remains protected by the existing age-backed credential vault.
- Current business membership and owner permission are revalidated before activation.
- Advertising connections and spend mutations remain independently disabled by default.
- No campaign mutation or advertising spend is enabled by this ADR.

## Consequences

### Positive

- The implemented flow matches the actual registered Yandex application.
- Authorization can be completed entirely through the Telegram control bot.
- ClientPlatform no longer depends on a callback URI that Yandex will not use for this application type.
- The production preflight fails closed on an incorrect redirect URI.
- The confirmation code has less exposure in Telegram chat history.
- Provider changes to the visual shape of an otherwise valid opaque authorization code do not break the client merely because the value is not seven digits.

### Trade-offs

- The owner must copy one short code from Yandex to Telegram.
- Telegram message deletion is best effort and can be denied by Telegram or chat permissions.
- An invalid or expired code requires starting a new authorization attempt because OAuth sessions are one-time.
- The legacy callback implementation remains for compatibility with a future separately registered web-service application, but it is not registered in screen-code production mode.

## Migration

Before enabling advertising connections in production:

1. Set `CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI=https://oauth.yandex.ru/verification_code`.
2. Provision the Yandex client ID and secret only in the protected production environment.
3. Provision and verify the age credential identity.
4. Keep `CLIENTPLATFORM_AD_CONNECTIONS_ENABLED=0` until Yandex approves API access.
5. After approval, enable connections only and perform a read-only account smoke test.
6. Keep `CLIENTPLATFORM_AD_SPEND_MUTATIONS_ENABLED=0` until a separate owner-approved rollout.

## Verification

Regression coverage proves:

- exact extraction of the one-time OAuth state;
- shared opaque confirmation-code normalization, including current alphanumeric provider-style values;
- rejection of whitespace, control, non-ASCII and overlong confirmation-code input;
- exact screen-code token-request shape without `redirect_uri`;
- Telegram FSM state persistence and sanitized failure handling;
- rejection of a different Telegram user before provider access;
- best-effort deletion of the message containing the code;
- successful connection completion and return to the business workspace;
- production environment rejection of the obsolete callback URI;
- absence of the legacy HTTP callback handler in screen-code mode.
