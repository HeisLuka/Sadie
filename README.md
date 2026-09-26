# Sadie Source Recovery

GitHub Actions recovery bench for **Sadie Research HQ**.

The repository is intentionally small: Notion remains the research authority, while GitHub Actions is used as a disposable acquisition/extraction environment for public sources that were previously transport-blocked.

## What the pipeline does

- fetches public HTTP(S) objects with redirects and full response metadata;
- uses `yt-dlp` for supported public video/audio pages and tries to recover captions/subtitles and metadata;
- inspects files with `file`, hashes them, and records size/MIME;
- extracts readable text from HTML/PDF and OCRs images;
- inventories archives and extracts only non-encrypted archives;
- records media metadata with `ffprobe`;
- probes Wayback/Common Crawl for selected dead URLs/patterns;
- uploads the original bytes, derived text, metadata and logs as an Actions artifact.

## Boundary

This pipeline does **not** bypass logins, CAPTCHAs, paywalls, DRM, or crack encrypted archives. If a source requires credentials or a decryption key, the run records that boundary instead of attempting to defeat it.

## Queue

`queue/recovery_queue.json` is the first Actions recovery batch derived from unresolved/blocked Sadie Research HQ items. Every item carries its Notion provenance so results can be written back without losing source identity.
