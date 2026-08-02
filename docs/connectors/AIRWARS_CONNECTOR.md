# Airwars connector

All Airwars-specific behavior is isolated under `archive_engine/connectors/airwars/`.

It owns the civilian-casualty URL pattern, Airwars IDs/codes and labels, historical package reconciliation, incident field mapping, coordinate display policy, direct-attempt interpretation, source occurrence recovery, source content/reachability classification, Airwars HTML extraction rules, Airwars release pages, and Airwars-specific blocking gates.

The connector discovers the stable local incident set and keeps direct verification separate from record origin. `source_references()` preserves all 50,326 historical occurrences and adds only relationships recoverable from explicit normalized provenance that are absent from those rows. It never merges away two incident relationships because URLs normalize alike.

Known failure modes include Airwars endpoint/live HTTP 403, host circuit-open evidence after repeated 403, malformed historical coordinates, incomplete historical detail sections, external social-login shells, deleted posts, dead URLs, and unavailable archive endpoints. Each becomes evidence and an explicit status rather than fabricated content.

Sanitized connector fixtures test parsing and status semantics. The generic core contains no Airwars selector or URL pattern.
