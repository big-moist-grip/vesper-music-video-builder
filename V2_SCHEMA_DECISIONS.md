# V2 Schema Decisions

## Status

These decisions resolve the remaining schema questions before V2.0/V2.1 implementation. They supplement the approved `V2_IMPLEMENTATION_PLAN.md` and `V2_IMPLEMENTATION_GUARDRAILS.md`.

This document defines schema meaning only. It does not implement code, routes, migrations, candidate generation, review, or MCP integration.

## Decision summary

| Decision | Chosen contract |
| --- | --- |
| `current_assembly_id` | Latest successful assembly that remains valid for the current project and selected assembly basis. |
| Future scene lineage references | V2.1 includes nullable `generation_attempt_id`, `candidate_set_id`, and `candidate_id` in every assembly scene record. |
| `final_job_id`, `production_job_id` | Selected lineage references for the artifact in this assembly snapshot. They do not contain scene history. |
| `scene_fingerprint` | SHA-256 of the canonical selected-scene assembly basis, including timing, selected artifact identity, and selected lineage references. |
| `assembly_schema_version` | Explicit per-manifest version with read-time migrations, immutable historical manifests, and a version bump for core field or semantic changes. |

# 1. Meaning of `current_assembly_id`

## Decision

`current_assembly_id` means:

> The latest successful assembly whose recorded basis still matches the current project source, scene timeline, selected scene artifacts, source policy, and output policy.

The field does **not** mean the latest requested assembly.

## Required behavior

- A successful assembly becomes the current assembly only after its draft output and manifest pass final validation.
- A new active or failed request does not replace a prior valid `current_assembly_id`.
- A source, scene, selected artifact, source-policy, or output-policy change makes the old assembly stale.
- When the current assembly becomes stale, clear `current_assembly_id` and `current_assembly_fingerprint` in the project pointer. Keep the stale manifest and draft available for history and diagnostics.
- When a replacement assembly succeeds, atomically write its ID and fingerprint as the new current pointer.
- If no successful valid assembly exists, both pointer fields are `null`.

The latest requested or active assembly remains in the external assembly manifest store. V2.1 does not add `latest_requested_assembly_id` to `project.json`. The manifest store and its durable active-state record provide that status.

This definition prevents one project field from representing both a usable draft and an in-progress or failed request.

## Pointer shape

```json
{
  "assembly": {
    "schema_version": 1,
    "current_assembly_id": "<successful-valid-assembly-uuid-or-null>",
    "current_assembly_fingerprint": "<matching-assembly-sha256-or-null>"
  }
}
```

`current_assembly_fingerprint` must match the successful manifest's top-level `assembly_fingerprint`. The pointer does not make a manifest current when the manifest state or basis disagrees.

# 2. Nullable future lineage references in V2.1 scene records

## Decision

V2.1 assembly scene records include all three fields as present, nullable fields:

```json
{
  "generation_attempt_id": null,
  "candidate_set_id": null,
  "candidate_id": null
}
```

V2.1 writes `null` for all three. V2.1 does not create attempt, candidate-set, or candidate records.

## Field meanings

- `generation_attempt_id` identifies the future attempt that produced the selected artifact.
- `candidate_set_id` identifies the future set that grouped candidate attempts.
- `candidate_id` identifies the future selected candidate within that set.

These references remain opaque to assembly. Assembly records selected lineage. Future generation and candidate services own the referenced records and complete histories.

The fields belong in `assembly_schema_version: 1` so strict V2.1 validation establishes a stable nullable contract. Future implementations can populate them without changing the meaning of existing fields.

The fields do not imply that every scene has a candidate set. Direct final generation remains valid:

```text
candidate_set_id = null
candidate_id = null
generation_attempt_id = <direct-final-attempt-or-null-until-attempt-schema-exists>
```

If the V2.1 implementation cannot create a generation-attempt record, it leaves `generation_attempt_id` null and uses the selected Render Job references for the current snapshot.

## Scene record contract

```json
{
  "order": 1,
  "scene_id": "...",
  "timeline_start_ms": 0,
  "timeline_end_ms": 6679,
  "artifact_kind": "final_scene|production_scene",
  "artifact_identity": {},
  "final_job_id": "...",
  "production_job_id": null,
  "generation_attempt_id": null,
  "candidate_set_id": null,
  "candidate_id": null,
  "review_decision_id": null,
  "scene_fingerprint": "..."
}
```

The assembly manifest stores one selected scene record. It does not store the full attempt or candidate list.

# 3. Meaning of `final_job_id` and `production_job_id`

## Decision

`final_job_id` and `production_job_id` are selected lineage references only.

They identify the jobs that produced the selected artifact represented by that assembly scene record. They do not represent all attempts for the scene, and they do not define a one-job-per-scene model.

## Field rules

### `final_job_id`

`final_job_id` points to the H3 Render Job associated with the selected Final Scene lineage.

- It is populated when the selected artifact is a Final Scene.
- It is also populated when a selected Production Scene derives from a Final Scene.
- It does not list prior failed, cancelled, retried, or rejected Render Jobs.
- Its presence does not prove currentness. The resolver validates the Final Scene record, output identity, preparation basis, and media before assembly.

### `production_job_id`

`production_job_id` points to the post-process job associated with the selected Production Scene lineage.

- It is populated when the selected artifact is a Production Scene produced by a technical production method.
- It is `null` when the selected artifact is a Final Scene or when the production method `none` selects the Final Scene.
- It does not list prior production attempts or failed post-process jobs.
- Its presence does not prove that the Production Scene remains current. The resolver validates the production record, source Final Scene, method, output identity, and media.

## History rule

Render Job, post-process, attempt, candidate, and review stores retain history. The assembly scene record retains the selected snapshot and its references. No future feature may reinterpret either job field as a complete scene history array.

# 4. Meaning of `scene_fingerprint` and future fingerprint separation

## Decision

`scene_fingerprint` is the SHA-256 digest of the selected scene's canonical assembly basis.

It captures the exact scene input that the assembly manifest selected. It does not capture the complete history of attempts, candidates, reviews, or ComfyUI observations.

## Canonical input

Serialize the following object as UTF-8 JSON with sorted keys, compact separators, and deterministic scalar types. Hash the resulting bytes with SHA-256:

```json
{
  "scene_id": "...",
  "timeline_start_ms": 0,
  "timeline_end_ms": 6679,
  "artifact_kind": "final_scene",
  "artifact_identity": {},
  "final_job_id": "...",
  "production_job_id": null,
  "generation_attempt_id": null,
  "candidate_set_id": null,
  "candidate_id": null,
  "review_decision_id": null
}
```

The canonical input must contain normalized project-relative artifact identity and content/media fingerprints. It must not contain filesystem modification time, filename ordering, volatile telemetry, raw ComfyUI history, or machine-specific absolute paths.

A future review decision fingerprint or attempt fingerprint remains separate in its owning immutable record. If a future record can change without its ID changing, the corresponding fingerprint must enter the selected-scene canonical input through a new manifest field and schema version. That addition does not change the meaning of `scene_fingerprint`.

## Separate fingerprint meanings

| Fingerprint | Meaning | Owner or scope |
| --- | --- | --- |
| `scene_fingerprint` | Selected scene timing, artifact, and selected lineage basis for one assembly snapshot. | Assembly scene record. |
| `timeline_fingerprint` | SHA-256 of the ordered scene IDs and exact SRT-derived start/end values. | Assembly manifest. |
| `assembly_fingerprint` | SHA-256 of the assembly basis: timeline fingerprint, source-audio identity, source/output policy IDs, and ordered scene fingerprints. | Assembly manifest and project current pointer. |
| `generation_attempt_fingerprint` | Immutable preparation and workflow input basis for one future generation attempt. | Future attempt or generation-package record. |
| `candidate_selection_fingerprint` | Candidate-set basis and selected candidate reference for one future selection. | Future candidate-set record. |
| `review_decision_fingerprint` | Immutable review basis, decision, and approved-artifact reference. | Future review record. |
| artifact/content/media identity | File ownership, content bytes, and probed media properties for one artifact. | Artifact or Final/Production record. |

`timeline_fingerprint` excludes generated media and generation provenance. A changed master audio file with unchanged scene timing changes `assembly_fingerprint` through `source_audio.identity`, but it does not change `timeline_fingerprint`.

A changed selected artifact, attempt reference, candidate selection, review decision, source audio, source policy, or output policy changes `assembly_fingerprint`. The current assembly pointer then becomes stale until a new successful assembly exists.

Future schemas must add a new fingerprint field or versioned record when they introduce a new basis. They must not change the meaning of `scene_fingerprint` or `timeline_fingerprint`.

# 5. `assembly_schema_version` migration strategy

## Decision

`assembly_schema_version` is a required version on every `assembly.json`. V2.1 writes version `1`.

The assembly manifest schema uses explicit version readers and migrations:

```text
assembly.json version 1
    -> version-specific validation
    -> in-memory current representation
    -> currentness and ownership checks
    -> assembly status or export
```

## Migration rules

1. **Read old versions without rewriting them.** A successful historical manifest remains immutable, including its original schema version and bytes.
2. **Normalize in memory.** A version reader maps older fields into the current internal representation and supplies only documented defaults, such as nullable future references.
3. **Bump for core changes.** Any required-field, field-meaning, state-model, identity, or fingerprint-semantic change increments `assembly_schema_version`.
4. **Write only the current version for new records.** New assembly requests use the latest supported schema version.
5. **Preserve old outputs.** Migration never promotes an old draft, changes its currentness, or guesses missing artifact, attempt, review, or ComfyUI identities.
6. **Fail closed on unknown semantics.** An unsupported version or ambiguous legacy field blocks assembly/export with a migration error. It cannot become a usable current assembly through a guessed default.
7. **Keep history outside `project.json`.** Project schema migration updates only the compact assembly pointer. It does not copy or rewrite assembly history.
8. **Keep project and manifest versions separate.** The project schema migration from v7 to v8 normalizes an empty assembly pointer. `assembly_schema_version` governs `assembly.json` only.
9. **Test every supported reader.** Each supported manifest version needs fixtures for successful, stale, failed, and missing-output states.

V2.1 does not use an unbounded unvalidated extension dictionary as a substitute for schema versioning. Future non-core extensions require their own versioned contract. Core additions use a manifest schema version bump and an explicit migration reader.

## Version 1 core fields

V2.1 version 1 must define these fields and meanings:

- `assembly_schema_version`;
- `assembly_id`;
- `project_id`;
- assembly state;
- `source_audio` and its identity;
- `timeline_fingerprint`;
- `assembly_fingerprint`;
- source and output policy IDs;
- ordered scene records;
- `timeline_validation`;
- output identity and failure data;
- the nullable future lineage references;
- `review_decision_id`, nullable in V2.1.

A successful version 1 manifest remains a valid historical snapshot after later schema versions ship.

# Final locked contract

The following rules are resolved before implementation:

- `current_assembly_id` means the latest successful valid assembly, never the latest request.
- V2.1 scene records include nullable `generation_attempt_id`, `candidate_set_id`, and `candidate_id`.
- `final_job_id` and `production_job_id` identify selected lineage only.
- `scene_fingerprint` hashes the selected scene assembly basis; timeline, assembly, attempt, candidate, review, and artifact fingerprints keep separate meanings.
- `assembly_schema_version` uses explicit read-time migrations, immutable historical manifests, and version bumps for core semantic changes.

No code was modified. No implementation was performed.
