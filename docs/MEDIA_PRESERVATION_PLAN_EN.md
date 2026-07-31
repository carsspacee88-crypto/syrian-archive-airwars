# Future media-preservation plan — not executed

This is documentation only. The current phase must not download or commit image, video, or audio binaries.

The legacy snapshot contains 22,119 media records, not necessarily 22,119 unique files. At an illustrative average of 1–5 MB per original, pre-deduplication storage could be roughly 22–111 GB. Thumbnails averaging 200 KB would add about 4.4 GB. These are scenarios, not measured totals.

The proposed later workflow is to inventory unique URLs and attribution, measure MIME type and size with bounded requests, quarantine downloads outside Git, compute SHA-256 and BLAKE3, deduplicate by content hash while retaining every incident/source relationship, store originals in versioned object or archival storage, and publish only reviewed small WebP/AVIF thumbnails.

Every manifest entry should retain original URL, incident, publisher, author, publication date, caption, sensitivity flag, rights/licence notes, retrieval time, MIME type, byte size, and hashes. Storage should be outside the main Git repository, with a second integrity copy and a documented takedown path.

Sensitive media must remain covered by default and require explicit user action. Public availability must not be treated as permission to republish; legal and rights review is required before local mirroring.

Execution should begin only after measuring unique file counts and sizes, approving a storage budget, defining copyright/takedown policy, and receiving explicit project-owner approval.
