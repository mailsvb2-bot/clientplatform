# Protected audio access policy

ClientPlatform fallback listening URLs are bearer capabilities. The HTTP request does not contain a reliable authenticated Telegram, MAX or VK user identity, so the runtime must not pretend that IP addresses, cookies or user-agent strings prove ownership.

The protection model is intentionally bounded and fail-closed:

- `AUDIO_ACCESS_TOKEN_TTL_HOURS` controls absolute lifetime. Default: `6`; accepted runtime range: `1..12`.
- `AUDIO_ACCESS_TOKEN_MAX_REQUESTS` controls the total protected-file request budget. Default: `32`; accepted runtime range: `4..256`.
- request counting is atomic in the database, so concurrent requests cannot exceed the configured budget;
- an expired or exhausted pending token is not reused; the next legitimate bot interaction issues a fresh token;
- protected responses send `Cache-Control: private, no-store`, `Pragma: no-cache`, `Referrer-Policy: no-referrer` and `X-Content-Type-Options: nosniff`;
- fetching the URL never marks the audio as completed because messenger previews, antivirus scanners and proxies may fetch links automatically. Completion remains an explicit user action.

Recommended production settings:

```text
AUDIO_ACCESS_TOKEN_TTL_HOURS=6
AUDIO_ACCESS_TOKEN_MAX_REQUESTS=32
```

Do not increase the limits merely to hide delivery problems. If a legitimate user exhausts a token, investigate provider previews/range requests and let the bot rotate the pending token through the normal interaction flow.
