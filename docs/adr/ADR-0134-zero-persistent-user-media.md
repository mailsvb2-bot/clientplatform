# ADR-0134 — Zero-persistent user media lifecycle

**Status:** Accepted by explicit owner direction, implementation starts in this slice  
**Date:** 2026-09-28

## Context

ClientPlatform currently persists generated visual provider output under the production provider data mount and persists advertising image/video payloads under the application asset directory. At scale this makes production disk usage proportional to user-generated media volume and creates an unnecessary long-lived copy of user media.

The owner requirement is stricter: ClientPlatform must not be a permanent storage provider for user images, video or audio. Users must still be able to download/save generated media, send it to their chosen channel, upload their own image for advertising, and reuse previously generated media when a durable external provider reference is available.

## Decision

1. User media bytes are transient processing data, not durable ClientPlatform business state.
2. Production generation output must live only in RAM/tmpfs and must have a bounded TTL/cleanup policy.
3. Persistent ClientPlatform storage may keep metadata, checksums, lifecycle state and provider references, but not image/video/audio payloads.
4. Provider content type must be derived from the actual media bytes when possible; declared metadata must not silently override a conflicting signature.
5. Production must fail closed if generated user media is configured to a persistent output directory.
6. After delivery/upload, the durable reusable handle is the target provider reference where supported (for example Telegram file_id, VK/MAX media ID, Yandex Direct AdImageHash/creative ID), not a local filesystem path.
7. Advertising UX must converge on three owner choices: generate media, add own media, or reuse earlier media when a provider reference remains resolvable.
8. A provider reference becoming unavailable does not justify a hidden ClientPlatform backup. The owner must reselect or regenerate the media.

## Consequences

- A provider/app restart may invalidate an undelivered transient asset. Recovery must therefore prefer immediate delivery/provider upload and durable external references instead of extending local retention.
- Retry semantics must distinguish paid generation from media delivery so a delivery retry never silently starts another paid generation.
- Advertising media selection/upload now transfers bytes directly to the advertising provider and persists only provider identifiers plus non-payload metadata; the advertising asset model and new schema contain no local storage-path field at all.
- Existing program/course media that is explicitly sourced from external cloud storage is unaffected by this generated-media slice; those references remain external rather than being copied into ClientPlatform.

## First implementation slice

This PR:
- moves production visual generation output from the persistent /data mount to /tmp tmpfs;
- enforces the transient-output requirement fail closed;
- bounds transient assets by TTL and cleanup batch size;
- makes asset_ready reflect actual file presence;
- corrects MIME metadata from actual image/video signatures;
- adds regression tests preventing a return to persistent generated visual output.

## Follow-up slices

1. Persist provider media references after owner-channel delivery and expose «Скачать / сохранить» without a ClientPlatform media archive.
2. Add «Использовать ранее созданную» to advertising setup over durable external provider references.
3. Generalize the same no-persistent-media contract to remaining video/audio owner flows and omnichannel delivery.

## Advertising provider-reference slice

Advertising image/video bytes are normalized or validated only in memory/transient input, uploaded immediately to the selected advertising provider, and then discarded. Durable ClientPlatform state contains checksum/size/type metadata and Yandex Direct provider identifiers only. Worker publication attaches those identifiers and never reopens a local media file.
