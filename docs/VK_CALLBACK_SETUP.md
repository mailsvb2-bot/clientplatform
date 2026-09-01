# VK Callback API production contract

ClientPlatform uses VK community messages through Callback API and VK API `5.199`.

## Required environment

```env
VK_WEBHOOK_ENABLED=1
VK_GROUP_ID=238191212
VK_GROUP_TOKEN=...
VK_CONFIRMATION_TOKEN=...
VK_SECRET=...
VK_API_VERSION=5.199
VK_CALLBACK_SNACKBAR_ENABLED=1
VK_AUDIO_UPLOAD_RETRIES=3
VK_AUDIO_UPLOAD_RETRY_BACKOFF_SEC=0.5
MESSENGER_PUBLIC_BASE_URL=https://clientplatform-bot.clientplatform.ru
```

The public Callback API URL is:

```text
https://clientplatform-bot.clientplatform.ru/webhooks/vk
```

The server validates both the Callback API `secret` and the callback `group_id`.
Events from another community are rejected before business processing.

## VK community settings

The matching Callback API server must have status `ok`. Enable at least:

- `message_new` — normal incoming messages and text keyboard buttons;
- `message_event` — inline callback buttons.

The configured API version must match `VK_API_VERSION`.

## Live production audit

Load the production environment and run:

```bash
set -a
. /etc/clientplatform/clientplatform.env
set +a
python scripts/vk_provider_audit.py
```

The audit calls only official VK methods:

- `groups.getById`;
- `groups.getCallbackConfirmationCode`;
- `groups.getCallbackServers`;
- `groups.getCallbackSettings`.

A healthy result has this shape:

```text
status=ok stage=callback_settings group=... code=200 error=NONE api=5.199 webhook=present server=ok secret=match confirmation=match message_new=1 message_event=1
```

The audit never prints the community token, Callback API secret or confirmation code.

## Outbound messages and media

Text uses `messages.send` with a unique `random_id`. Callback acknowledgements use
`messages.sendMessageEventAnswer`. ClientPlatform interaction buttons carry only
canonical tenant-scoped commands or validated HTTPS links.

Images use the VK message-photo upload flow. Audio uses
`docs.getMessagesUploadServer(type=audio_message)` and `docs.save`. Existing Ogg or
Opus files are uploaded directly. Other audio is converted to a deterministic Opus
file with an explicit Opus muxer. If VK still rejects the attachment, the user gets
a signed expiring listening link and can complete the same progress flow in VK.
