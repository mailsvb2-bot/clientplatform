# ADR-0009: Bot-independent program media

## Status

Accepted.

## Context

The ClientPlatform control bot receives lesson files while a business may deliver
those lessons through a different managed Telegram bot. Telegram `file_id` values
belong to the individual bot that received the file and cannot be transferred to
another bot. Persisting a control-bot `file_id` therefore creates a program that
looks valid in PostgreSQL but can fail when dispatch resolves a business-bot
token.

The production deployment already owns a private, versioned S3-compatible bucket,
a signed media gateway and a dispatch resolver for `s3://` references.

## Decision

Program lesson media follows one canonical boundary:

1. Text remains inline and does not require object storage.
2. Audio, voice, video, image and document messages are downloaded by the control
   bot into an owner-only temporary file.
3. The file is rejected before download when Telegram reports a size above the
   configured limit. The downloaded size is checked again.
4. The application uploads the file to the dedicated private bucket with bounded
   memory, a non-identifying object key, SHA-256 metadata and a subsequent HEAD
   verification.
5. Only after successful verification may a program or lesson mutation persist
   the resulting `s3://bucket/key` reference.
6. Temporary plaintext is deleted on every exit path.
7. At dispatch time the `s3://` reference becomes a short-lived signed gateway
   URL. The Telegram client fetches only URLs matching its configured gateway
   origin/path and reuploads the object as multipart data through the selected
   business-bot token.
8. Arbitrary public HTTPS references keep Telegram's normal URL-send path and are
   never fetched server-side by this feature.
9. Raw bot-local media references are rejected in gateway mode. They are not
   retried as if they were portable media.

## Limits

`CLIENTPLATFORM_PROGRAM_MEDIA_MAX_BYTES` is capped at 20,000,000 bytes because the
control bot must first obtain the file through Telegram `getFile`. The same limit
bounds gateway fetch and multipart upload buffering.

## Failure semantics

- Disabled or incomplete storage configuration rejects media but keeps text
  lessons available.
- Telegram download, S3 upload or HEAD verification failure leaves the draft
  unchanged.
- Lesson replacement preserves the previous reference until the new object is
  verified.
- Dispatch fetches only the owned gateway URL; redirects are disabled.
- Errors persisted by dispatch are sanitized and contain no bot token, S3 key,
  signed URL or original filename.

## Legacy data

Any pre-existing media lesson whose `content_ref` is a raw Telegram `file_id` is
not portable. Gateway mode rejects it with
`media_bot_local_reference_not_portable`. Operators must reopen the draft and
replace that material so it is ingested into private storage. Published legacy
programs require a future version/copy workflow rather than in-place mutation.

## Consequences

Program media becomes independent of the bot used for creation or delivery. The
private bucket is the durable source of truth, while Telegram receives only a
bounded multipart copy through the correct business bot.
