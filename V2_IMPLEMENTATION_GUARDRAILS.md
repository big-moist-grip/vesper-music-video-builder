# V2 Implementation Guardrails

## Purpose

The approved `V2_IMPLEMENTATION_PLAN.md` remains the implementation direction. This document performs the final architectural compatibility check before coding begins.

These guardrails do not add V2.2 or V2.3 features. They keep the V2.0 and V2.1 contracts open to later candidate generation, review, approval, and attempt lineage.

No source code changes belong in this pass.

## Compatibility decision

The V2.0/V2.1 direction is compatible with the architecture proposal if implementation follows the ownership and identity rules below.

The assembly manifest must describe one selected, validated snapshot for one assembly. It must not become the database for every generation attempt, candidate, review action, or ComfyUI observation.

The project document should keep pointers and fingerprints. Detailed histories should remain in project-owned records outside `project.json`.

# 1. Future V2.2/V2.3 extension points

## 1.1 Seed Hunter candidate generation

**Confirmed:** V2.1 can leave room for Seed Hunter without implementing candidate generation.

V2.1 should treat the selected assembly artifact as a reference to a broader provenance chain. The assembly service does not need to know how the selected artifact was produced.

Use these future-compatible concepts:

- `candidate_set_id`: an opaque reference to one future candidate set;
- `candidate_id`: an opaque reference to the selected future candidate;
- `generation_attempt_id`: an opaque reference to the attempt that produced the selected artifact;
- `artifact_identity`: the project-relative media identity and content/media fingerprint used for assembly;
- `final_job_id` and `production_job_id`: selected lineage references, not the complete history of the scene.

The V2.1 assembly manifest may carry nullable `candidate_set_id`, `candidate_id`, and `generation_attempt_id` fields. V2.1 must leave them null and must not create candidate records.

A future candidate service should own records similar to:

```text
renders/<scene_id>/attempts/<attempt_id>/attempt.json
renders/<scene_id>/candidates/<candidate_set_id>.json
renders/<scene_id>/candidates/<candidate_id>.json
```

The exact future directory names may change during V2.2 design. The ownership rule must remain stable:

- the candidate service owns candidate grouping, candidate state, selection, and retention;
- the generation-package service owns immutable per-attempt preparation inputs;
- `render_jobs.py` owns queue/history, cancellation, retry, and raw output discovery;
- `render_finalize.py` owns Final Scene validation and promotion;
- assembly reads the selected current artifact and records its references.

Seed Hunter must not require assembly to enumerate every candidate. Assembly needs the selected candidate reference and its validated artifact identity. Candidate comparison remains outside assembly.

## 1.2 Review and approval states

**Confirmed:** V2.1 can leave room for review and approval without implementing a review state machine.

The V2.1 manifest already reserves `review_decision_id`. Treat that field as an opaque immutable reference, not as an approval boolean.

V2.1 must not add or infer any of the following:

- `approved: true` on a scene or file;
- approval state from a successful Render Job;
- approval state from the presence of a Final Scene;
- approval state from the presence of a Production Scene;
- browser-only approval state;
- a review note embedded in a prompt or render job record.

When review arrives, a separate review service should own records such as:

```text
renders/<scene_id>/review/
├── decisions/<decision_id>.json
└── current.json
```

A future decision record can carry:

- decision state;
- approved artifact kind and artifact identity;
- candidate or attempt reference;
- reviewer note;
- replacement/remake lineage;
- decision fingerprint;
- prior decision reference.

The assembly service should consume a future `AssemblyEligibilityProvider` or equivalent adapter. That adapter can require a current review decision when V2.2/V2.3 enables review. V2.1 can use its explicit current Final Scene or Production Scene policy while leaving the approval hook available.

The assembly manifest should record the review decision reference and fingerprint when one exists. It should record `null` in V2.1. It must not describe an unreviewed V2.1 artifact as approved.

## 1.3 Multiple generation attempts per scene

**Confirmed:** V2.1 can support multiple attempts per scene without implementing them.

The current Render Job model already gives each job its own durable identity. A future attempt layer should group jobs rather than replace the job lifecycle.

The future relationship should look like this:

```text
Scene
  -> Candidate Set, optional
       -> Attempt 1
            -> Render Job(s)
            -> Raw output identity
            -> Preview or Final artifact
       -> Attempt 2
            -> Render Job(s)
            -> Raw output identity
            -> Preview or Final artifact
  -> Review decision, optional
  -> selected current artifact
  -> Assembly scene record
```

A retry must not overwrite the previous Render Job record or reuse a different attempt's output identity. The future attempt service can decide whether a retry retains an attempt ID or creates a new attempt, but that decision must remain above `render_jobs.py` and must preserve job-level history.

The V2.1 assembly record represents the selected artifact for one immutable assembly snapshot. It does not claim that the scene has only one attempt or one historical output.

## 1.4 V2.1 manifest compatibility shape

Before implementation freezes strict manifest validation, the scene record must carry selected-artifact data and future lineage references as separate concepts.

The compatible shape is:

```json
{
  "order": 1,
  "scene_id": "...",
  "timeline_start_ms": 0,
  "timeline_end_ms": 6679,
  "artifact_kind": "final_scene",
  "artifact_identity": {
    "relative_path": "renders/<scene_id>/final/final_scene.mp4",
    "content_fingerprint": "...",
    "media_fingerprint": "..."
  },
  "final_job_id": "...",
  "production_job_id": null,
  "generation_attempt_id": null,
  "candidate_set_id": null,
  "candidate_id": null,
  "review_decision_id": null,
  "scene_fingerprint": "..."
}
```

The nullable references do not activate future features. They prevent V2.1 from using the absence of a field to define a permanently one-dimensional artifact model.

The manifest should preserve the distinction between:

- the selected artifact;
- the attempt that produced it;
- the candidate that led to the selection;
- the review decision that approved it;
- the Render Job that executed it;
- the current Final Scene or Production Scene record.

If the implementation keeps the current flat field set without adding the nullable future references, it must document a versioned migration path before releasing `assembly_schema_version: 1`. It must not overload `final_job_id` later to carry candidate-set or attempt identity.

# 2. Required identity and state invariants

## 2.1 One scene does not mean one render attempt

**Confirmed:** The V2.0/V2.1 changes must not create this assumption.

Implementation rules:

- A scene ID identifies the editorial timeline scene, not one Render Job.
- A Render Job ID identifies one durable queue execution record.
- A future generation-attempt ID groups one generation intent and its lineage.
- A retry must preserve historical job records and output identities.
- Assembly selects one current validated artifact for its snapshot; it does not delete or hide other attempts.
- Project JSON must not store a single `scene_id -> job_id` map as the complete history.

Tests must submit or simulate two jobs for one scene and verify that the second job does not overwrite the first job record. The assembly resolver should select by currentness and explicit policy, not by job creation order.

## 2.2 One scene does not mean one permanent output

**Confirmed:** The existing artifact layers already provide the correct direction.

The implementation must preserve these distinct records:

```text
Raw H3 output
Final Scene
Production Scene
Assembly snapshot
```

Each layer needs its own identity, state, fingerprint, and failure isolation. A current pointer can select one artifact for a layer, but the pointer does not erase prior valid artifacts or job lineage.

The assembly manifest records the selected artifact identity at assembly time. If a later Final Scene, Production Scene, candidate selection, review decision, source change, or policy change alters the basis, the existing assembly becomes stale and a new assembly ID records the replacement snapshot.

Do not use a single permanent path as the only identity for all versions. A canonical current path may support the current artifact layer, but the durable record must retain an immutable content identity and lineage reference.

## 2.3 Filenames do not define ordering

**Confirmed:** The approved plan preserves this invariant.

The implementation must derive order from:

1. the project scene list;
2. the scene IDs in that list;
3. the exact SRT-derived start and end values;
4. the manifest `order` and timeline fields.

FFmpeg input lists, assembly scene records, CSV rows, media responses, and UI tables must use this order. They must not sort by:

- filename;
- filesystem directory order;
- modification time;
- Render Job ID;
- ComfyUI prompt ID;
- candidate number;
- output discovery order.

A filename remains a storage locator. A filename never becomes a timeline key.

## 2.4 ComfyUI state does not define Builder state

**Confirmed:** The V2.0 lifecycle plan preserves this boundary.

The Builder should derive readiness from its own durable state and validation sequence:

```text
ComfyUI queue/history observation
    -> Render Job reconciliation
    -> Builder-owned output association
    -> media probe and currentness checks
    -> Final Scene or Production Scene state
    -> assembly eligibility
```

ComfyUI can report execution success while the Builder still lacks:

- a Builder-owned output;
- an unambiguous output association;
- a readable media file;
- a current preparation fingerprint;
- valid media coverage;
- a valid Final Scene or Production Scene record.

The implementation must keep ComfyUI prompt IDs and history records as runtime evidence. They cannot make a Final Scene, Production Scene, or assembly ready by themselves.

Volatile telemetry can improve presentation. It cannot manufacture a durable transition.

# 3. ComfyUI MCP compatibility

**Confirmed:** A future ComfyUI MCP integration can fit around the current architecture as a read-oriented validation, diagnostics, and environment-health layer.

## 3.1 Compatible placement

Keep the current ownership chain:

```text
Builder services
    -> ComfyUI client and transport boundary
        -> PromptServer HTTP and queue/history APIs
        -> optional future MCP inspection adapter
```

The MCP adapter should implement a narrow protocol beside the existing ComfyUI client. It should not create a second Render Job service.

A future interface could expose operations such as:

- inspect runtime capabilities;
- inspect queue and history observations;
- validate node, model, workflow, and output contracts;
- collect environment diagnostics;
- report health with a timestamp and observation source.

The exact interface belongs to a future implementation. V2.0/V2.1 should keep the existing client seams injectable so a second observation adapter can be added later.

## 3.2 Validation layer

MCP could validate Builder-owned expectations against the active ComfyUI environment:

- required node classes exist;
- the selected workflow manifest matches the installed node contract;
- the configured model or profile is available;
- output nodes expose the expected role;
- queue/history responses contain the expected prompt identity;
- a discovered output satisfies the Builder output-prefix and media contract.

The validation result should enrich preflight or diagnostics. `render_jobs.py`, `render_finalize.py`, and `render_production.py` must keep the final state decision.

MCP validation must not:

- replace the Render Job store;
- approve a scene;
- promote a Final Scene or Production Scene;
- mark an assembly current;
- alter SRT timing;
- write project JSON.

## 3.3 Diagnostics layer

MCP could collect a bounded diagnostic snapshot when a job fails, stalls, or loses output association:

- queue observation;
- history observation;
- runtime capability response;
- node or model availability;
- relevant timestamps;
- a redacted error category.

The Builder should store diagnostics with the owning job or qualification report. It should retain the Builder's error code and state as the user-facing authority.

MCP diagnostics must not expose raw ComfyUI paths, credentials, or internal graph details in normal UI copy. The existing redaction and error-boundary rules remain in force.

## 3.4 Environment-health layer

MCP could answer whether the active environment is ready for a requested operation:

- PromptServer is reachable;
- required nodes and models are available;
- the selected production capability is present;
- the runtime reports sufficient disk, VRAM, or other declared resources;
- FFmpeg and FFprobe are available where the environment adapter can verify them.

Health is an observation with a timestamp and a capability scope. It expires. It does not become a permanent project fact.

A future health route or panel may display MCP observations, but the backend must still recheck capability and currentness at the operation boundary.

## 3.5 MCP boundary rules

Do not place MCP-specific IDs, URLs, prompts, or response shapes in `project.json`, the assembly manifest's required core fields, or the scene timeline contract.

Do not allow MCP to add:

- a second scheduler;
- a second cancellation authority;
- a second output-currentness model;
- a second project store;
- a browser-side state machine;
- an implicit fallback from failed Builder validation to MCP success.

If a future MCP adapter submits or cancels work, it must do so through the existing Render Job service. V2.0/V2.1 does not need that capability.

# 4. Schema compatibility review

## 4.1 Decisions that support future V2.2/V2.3 work

The following approved decisions support later features:

- `project.json` stores a compact assembly pointer rather than full assembly history;
- assembly history lives under `renders/assemblies/<assembly_id>/`;
- successful manifests become immutable snapshots;
- scene IDs and exact source timing remain independent from generated media duration;
- artifact identity includes project ownership and content/media fingerprints;
- source and output policies use named, versionable IDs;
- `review_decision_id` remains nullable in V2.1;
- Render Jobs, Final Scene, Production Scene, and assembly keep separate owners;
- the frontend does not own progression or durable state;
- CSV uses manifest data instead of rebuilding timeline data from filenames or current project files.

These choices align with the architecture proposal's generation-package, candidate-set, review, Render Job, and assembly ownership boundaries.

## 4.2 Decisions that would make future features difficult

### A. Treating flat job fields as complete scene history

`final_job_id` and `production_job_id` must mean selected lineage for this assembly snapshot. They must not mean the only job or attempt that ever existed for the scene.

Guardrail: reserve nullable attempt and candidate references, and keep complete histories in their owning records.

### B. Storing attempts, candidates, or review histories in `project.json`

A growing per-scene history would make project saves large, migrations coupled, and concurrent lifecycle writes fragile.

Guardrail: keep project JSON pointers and fingerprints only. Store generation packages, attempts, candidate sets, review decisions, jobs, and assembly manifests in project-owned records with their own schemas.

### C. Encoding approval in artifact kind or output state

`final_scene`, `production_scene`, `SUCCEEDED`, and `READY` describe technical artifact layers. They do not describe human approval.

Guardrail: keep artifact kind, technical state, and review decision as separate fields owned by separate services.

### D. Treating a nullable review ID as a boolean approval model

A single nullable ID can reference a future current decision, but it cannot carry the state machine or decision history.

Guardrail: keep `review_decision_id` opaque in V2.1. A future review service owns decision state, lineage, notes, and fingerprints. Assembly consumes an eligibility result.

### E. Using one pointer for both latest request and current successful assembly

The V2.1 pointer needs an explicit meaning before implementation. A failed replacement and a prior valid draft can coexist.

Preferred rule:

- `current_assembly_id` identifies the latest successful assembly whose basis matches the current project and selected artifact policy;
- an active or latest requested assembly remains discoverable through the manifest store or a separately named `latest_assembly_id` pointer;
- a stale current pointer reports stale status rather than silently selecting a different draft.

If the product chooses to make `current_assembly_id` mean latest request instead, it must name that meaning in the schema contract and expose the latest successful assembly separately. Do not overload one field with both meanings.

### F. Making `scene_fingerprint` the only future fingerprint

A single ambiguous fingerprint cannot explain whether a scene changed because of source timing, prompt input, a generation attempt, candidate selection, review approval, Final Scene, or Production Scene.

Guardrail: define V2.1 `scene_fingerprint` as the assembly input basis for the selected scene. Keep future fields available for:

- `scene_input_fingerprint`;
- `generation_attempt_fingerprint`;
- `candidate_selection_fingerprint`;
- `review_decision_fingerprint`;
- `artifact_identity`;
- `assembly_timeline_fingerprint`.

A future feature should add a new field or schema version instead of changing the meaning of an existing fingerprint.

### G. Making policy IDs unversioned feature flags

A string such as `draft_h264_aac_v1` must identify an immutable media contract. It must not become a bag of hidden options.

Guardrail: register policy IDs with explicit versions and persist the policy basis in the manifest. A future normalization or candidate-preview policy gets a new ID and qualification record.

### H. Making artifact paths permanent identities

A stable current path can support the current Final Scene or Production Scene layer, but a path alone cannot distinguish replacement, stale content, or multiple attempts.

Guardrail: use UUID lineage references and file/content/media identities. Recheck path ownership and identity before assembly and export.

### I. Closing manifest validation without migration seams

The repository uses strict validators. A future field added without a manifest migration will make old assembly records unreadable.

Guardrail:

- keep `assembly_schema_version` explicit;
- add migration readers before adding fields;
- preserve old successful manifests;
- validate future extension objects with their own version and schema;
- reject unknown required semantics rather than guessing them.

An optional extension namespace can carry future references only if the implementation validates its shape and version. Do not add an unbounded unvalidated dictionary as a shortcut.

### J. Hardcoding resolver logic to current artifact kinds

V2.1 needs `final_scene` and `production_scene`. Future V2.2/V2.3 may add candidate and approved-artifact references without making preview media assembly-ready.

Guardrail: keep the resolver behind an artifact and eligibility interface. Add future artifact kinds through versioned policy and review checks. Do not make the assembly service inspect candidate folders or ComfyUI output names.

### K. Treating the assembly manifest as a candidate or review database

The manifest should explain why one artifact entered one draft. It should not copy every candidate, note, or decision.

Guardrail: store selected references and fingerprints in the manifest. Let candidate and review stores retain full histories.

### L. Persisting ComfyUI or MCP state as product state

Prompt IDs, history payloads, MCP observations, and runtime health change outside the Builder.

Guardrail: store bounded diagnostics and observation metadata with lifecycle records when useful. Reconcile them into Builder-owned states through explicit adapters. Do not make external runtime state a project invariant.

## 4.3 Required schema decisions before code freeze

The implementation team must settle these points before writing strict manifest validators:

1. Add nullable `generation_attempt_id`, `candidate_set_id`, and `candidate_id` references to the selected scene record, or define a versioned equivalent that preserves the same semantics.
2. Define `final_job_id` and `production_job_id` as selected lineage references, not complete history fields.
3. Define the exact meaning of `current_assembly_id` and separate it from a latest-request pointer if both are needed.
4. Define `scene_fingerprint` and keep future basis fingerprints separate.
5. Define an `AssemblyEligibilityProvider` seam so future review approval can gate assembly without replacing the resolver.
6. Keep policy IDs versioned and immutable.
7. Keep assembly manifest migrations explicit and test old manifests before adding fields.
8. Keep external runtime identifiers out of required project and assembly identity fields.

These decisions do not implement V2.2 or V2.3. They prevent V2.1 from closing the extension points those releases require.

# 5. Implementation gates

Before V2.0 coding begins:

- `render_jobs.py` remains the only Render Job lifecycle owner;
- ComfyUI success cannot bypass Builder output discovery;
- retries preserve durable job and output lineage;
- browser and telemetry state cannot advance durable transitions.

Before V2.1 schema coding begins:

- the manifest treats selected artifact data as a snapshot;
- nullable future lineage references or an equivalent versioned extension are defined;
- `review_decision_id` stays nullable and opaque;
- the current assembly pointer has one documented meaning;
- the manifest stores no candidate list, review history, or ComfyUI observation dump.

Before V2.1 resolver coding begins:

- the resolver accepts a future eligibility provider;
- source policy and output policy IDs remain versioned;
- scene IDs and project order drive every input list and export;
- currentness comes from Builder records and fingerprints.

Before any future MCP work begins:

- the MCP adapter has read-only validation, diagnostics, or health scope;
- the existing Render Job and artifact services remain authorities;
- MCP observations have timestamps, source labels, and bounded lifetime;
- no MCP-specific schema becomes required for a project or assembly.

# 6. Final confirmation

1. **Future Seed Hunter candidates, review/approval states, and multiple attempts:** confirmed. V2.1 records selected-artifact references and leaves nullable lineage and review seams. Future services own their full histories.
2. **One attempt, one permanent output, filename ordering, and ComfyUI authority assumptions:** confirmed absent. The implementation must preserve the identity, ordering, reconciliation, and currentness rules in this document.
3. **Future ComfyUI MCP:** confirmed compatible as a validation, diagnostics, and environment-health layer behind the existing client and service boundaries. It must not become a competing lifecycle authority.
4. **Future schema risks:** identified. The implementation must resolve pointer semantics, attempt references, fingerprint meanings, policy versioning, eligibility seams, and manifest migrations before strict schema freeze.

No code was modified while creating this guardrails document.

## Architectural sources

- `V2_ARCHITECTURE_PROPOSAL.md`.
- `V2_IMPLEMENTATION_PLAN.md`.
- `MEMORY.md`.
- `backend/render_jobs.py`.
- `backend/render_finalize.py`.
- `backend/render_production.py`.
- `backend/projects.py`.
- `backend/routes.py`.
- `web/extension.js`.
