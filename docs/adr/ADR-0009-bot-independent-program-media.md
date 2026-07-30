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

Telegram voice notes add a second transport constraint: OGG/Opus voice messages
must use `sendVoice`; they are not valid MP3/M4A inputs for `sendAudio`.

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
7. Private OGG program objects are an explicit portable voice representation.
   They remain `ContentKind.AUDIO` for schema compatibility, but dispatch selects
   `sendVoice`. MP3/M4A audio continues through `sendAudio`.
8. At dispatch time the `s3://` reference becomes a short-lived signed gateway
   URL. The Telegram client fetches only URLs matching its configured gateway
   origin/path and reuploads the object as multipart data through the selected
   business-bot token.
9. Arbitrary public HTTPS references keep Telegram's normal URL-send path and are
   never fetched server-side by this feature.
10. Raw bot-local media references are rejected in gateway mode. They are not
    retried as if they were portable media.
11. Every newly uploaded object receives a delayed cleanup intent before the
    program mutation. A successful mutation cancels that intent. If cancellation
    is interrupted, the cleanup worker checks the `lessons` table and never
    deletes a still-referenced object.
12. Replaced material and failed mutations are placed in an idempotent cleanup
    queue. Jobs use leases, stale-lock recovery, exponential retry and a terminal
    state. S3 DELETE is idempotent, signed and restricted to the configured
    `program-media/` scope.

## Limits

`CLIENTPLATFORM_PROGRAM_MEDIA_MAX_BYTES` is capped at 20,000,000 bytes because the
control bot must first obtain the file through Telegram `getFile`. The same limit
bounds gateway fetch and multipart upload buffering.

## Failure semantics

- Disabled or incomplete storage configuration rejects media but keeps text
  lessons available outside production. Production preflight requires media
  ingest and the private gateway.
- Telegram download, S3 upload or HEAD verification failure leaves the draft
  unchanged.
- Lesson replacement preserves the previous reference until the new object is
  verified and the database mutation commits.
- A failed mutation accelerates the new object's cleanup intent.
- A successful replacement queues the superseded reference only after commit.
- Cleanup checks live database references immediately before DELETE. A referenced
  object is retained and its stale intent is removed.
- A process crash after claiming or deleting a cleanup object is safe: the lease
  becomes stale, DELETE can be repeated, and S3 404 is treated as success.
- Dispatch fetches only the owned gateway URL; redirects are disabled.
- Errors persisted by dispatch or cleanup are sanitized and contain no bot token,
  S3 key, signed URL, business UUID or original filename.

## Legacy data

Any pre-existing media lesson whose `content_ref` is a raw Telegram `file_id` is
not portable. Gateway mode rejects it with
`media_bot_local_reference_not_portable`. Operators must reopen the draft and
replace that material so it is ingested into private storage. Published legacy
programs require a future version/copy workflow rather than in-place mutation.

## Consequences

Program media becomes independent of the bot used for creation or delivery. The
private bucket is the durable source of truth, Telegram receives a bounded
multipart copy through the correct business bot, voice notes preserve their
native delivery semantics, and superseded objects have a durable reclamation
path instead of accumulating indefinitely.
