# ADR-0003 — Git as definition source of truth
Status: Accepted as target design (2026-11-21)

Decision: Store agents, instruction files, skill manifests and immutable tool/endpoint references in Git. Index validated revisions into a searchable DB. Run manifests retain actual commit SHA, content digests and dependency version references. UI edits create commits/PRs after review; do not silently rewrite published versions.

Consequences: Auditability and reviewability; more implementation work around drafts, permissions, merge conflicts and external repositories. Runtime artefacts, secret values, transient events and large outputs are stored outside Git.
