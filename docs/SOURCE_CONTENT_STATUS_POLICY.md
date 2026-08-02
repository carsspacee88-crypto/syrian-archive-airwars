# Source-content status policy

Each external-source entity receives exactly one primary status:

- `REFERENCE_ONLY`: an occurrence is known but no usable URL exists.
- `URL_PRESERVED`: a raw/normalized URL exists, without stronger content or failure evidence.
- `METADATA_ONLY`: title, author, date, publisher, caption, or description exists, but no source text.
- `PARTIAL_TEXT`: non-empty text exists, but completeness is not established or it is an access/navigation/error shell.
- `FULL_TEXT_DIRECT`: validated complete main text retrieved through a public direct/feed/embed path.
- `FULL_TEXT_ARCHIVED`: validated complete main text whose selected-text provenance is an archived public copy.
- `FULL_TEXT_LOCAL_SNAPSHOT`: validated complete main text preserved only in an identified local/historical snapshot.
- `BLOCKED`: no text and explicit 401/403/429/451 or block evidence.
- `DEAD`: no text and HTTP 404/410 evidence.
- `MALFORMED`: no text and the raw URL cannot normalize to HTTP(S).
- `RESTRICTED`: lawful access requires permission not available to the collector.
- `NEEDS_MANUAL_REVIEW`: evidence is insufficient for a stronger primary decision.

Primary statuses reconcile exactly to the external-source entity total. Secondary flags—duplicate content, manual review, malformed observed alternative, or repeated relationship—may overlap and never change that equation.

`FULL_TEXT_*` requires non-empty `text_original`, accepted legacy quality, completeness `full`, no known shell marker, and an accepted extraction provenance/method. Merely generating a local HTML page, recording HTTP 200, storing metadata, or finding an archive URL does not qualify.
