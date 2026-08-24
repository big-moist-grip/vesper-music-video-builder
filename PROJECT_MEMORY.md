# Project Memory

## Document role

This file gives future coding agents the durable architectural context for the Vesper Music Video Builder.

It records product boundaries, ownership, invariants, approved V2 decisions, and extension rules. It does not track temporary tasks, bug queues, test counts, or implementation progress.

The architecture documents remain authoritative for detail:

- `V2_ARCHITECTURE_PROPOSAL.md` defines the system boundaries and long-term architecture.
- `V2_IMPLEMENTATION_PLAN.md` defines the approved V2.0/V2.1 scope and release direction.
- `V2_IMPLEMENTATION_GUARDRAILS.md` defines compatibility rules for future candidate, review, attempt, and ComfyUI integrations.
- `V2_SCHEMA_DECISIONS.md` defines the locked assembly schema meanings and migration rules.
- `MEMORY.md` contains the earlier repository baseline and existing phase contracts.

The current repository baseline uses project schema-v7. The approved V2 plan proposes an additive schema-v8 assembly pointer and external assembly manifests. Do not treat planned V2 fields as implemented until the source and migration code prove it.

# Product identity

Vesper Music Video Builder is a local ComfyUI custom-node extension for deliberate music-video scene production.

The no-op `MusicVideoBuilder` node provides a ComfyUI launcher anchor. The product lives in the web extension, PromptServer routes, Python services, project-owned records, and local media tools.

The product uses:

- Python backend services;
- plain JavaScript ES modules and plain CSS;
- ComfyUI PromptServer APIs;
- local FFmpeg and FFprobe;
- project-owned JSON and media directories;
- manifest-driven H3 workflows;
- manual ChatGPT relay for bounded creative prose;
- MiniMax H3 for scene video generation;
- DaVinci Resolve for editorial work and finishing.

The product does not modify ComfyUI core or expose a general ComfyUI graph editor.

# Product boundaries

Resolve remains the editorial and finishing environment. The Builder may create a review draft and export timeline metadata. It must not become an NLE or automate Resolve project mutation.

The Builder does not own:

- a visual timeline editor;
- clip trimming, splitting, transitions, overlays, or audio mixing;
- Resolve launch, import automation, relinking automation, or project mutation;
- LUTs, colour matching, grading, film grain, VFX, or creative finishing;
- a general workflow browser or node editor;
- cloud inference or OpenAI API calls;
- local LLM execution;
- automatic model downloads or node installation;
- generic text-to-video;
- frame interpolation;
- transcription or inferred lyric timing;
- a second server, database, React application, or TypeScript frontend.

Production Scene remains a technical layer. It covers the existing resolution enhancement, approved upscaling, and delivery preparation boundary. It does not become a creative look-processing layer.

Seed Hunter, candidate generation, storyboard expansion, prompt-system expansion, and formal review workflows remain future extensions. Their future compatibility rules appear below. Their absence from the current product is intentional.

# Canonical pipeline

The current production path is:

```text
Master audio + artist/editor SRT
    -> SRT-derived scenes
    -> Visuals and deterministic prompt state
    -> dry H3 preparation
    -> durable Render Job
    -> Raw H3 output
    -> Final Scene
    -> Production Scene, when selected
```

The approved V2.1 assembly path is:

```text
Current valid Final Scene or Production Scene per scene
    -> timeline and media preflight
    -> immutable assembly manifest
    -> controlled video concatenation
    -> original project master audio attached once
    -> project-owned draft music video
    -> CSV timeline metadata
    -> manual Resolve handoff
```

The assembly service does not create a new source timeline. It consumes the project scene contract.

# Source and timeline invariants

## Source authority

The artist/editor-provided SRT supplies the editorial timing contract. The project master audio supplies the authoritative song duration and master-audio identity.

The Builder must not infer or replace source timing through:

- waveform analysis;
- beat detection;
- generated scene audio;
- media container timestamps;
- filenames;
- candidate durations;
- ComfyUI history order.

## Scene contract

An SRT-derived scene owns:

- stable `scene_id`;
- canonical scene order;
- exact `start_ms` and `end_ms` values;
- lyric or instrumental provenance;
- scene audio derived from the authoritative master audio;
- Visuals and prompt inputs;
- render and artifact lineage.

Scenes must cover the source timeline contiguously. The first scene begins at zero. The final scene ends at the master-audio duration. Assembly validates these facts again before it writes a draft.

A generated H3 frame plan serves the 24 FPS and `5 + 17n` generation contract. It does not replace the exact SRT-derived scene range.

## Ordering

Scene IDs and the project scene list define order everywhere:

- render selection;
- assembly records;
- FFmpeg input lists;
- CSV rows;
- UI tables;
- media responses.

Filenames, filesystem order, modification time, Render Job IDs, ComfyUI prompt IDs, candidate numbers, and output discovery order never define timeline order.

# Artifact model and currentness

The Builder keeps these artifacts distinct:

```text
Raw H3 output
    -> Final Scene
    -> Production Scene
    -> Assembly snapshot
```

Each layer owns its own record, state, identity, fingerprint, and failure isolation.

## Raw H3 output

Raw H3 output belongs to a durable Render Job. Builder job IDs own output prefixes and persisted records. ComfyUI prompt IDs remain runtime references and never become Builder job identities.

## Final Scene

Final Scene contains validated final-quality media with authoritative scene audio. Finalization must prove output ownership, media validity, duration coverage, and current preparation basis before promotion.

## Production Scene

Production Scene points at or promotes a technical derivative of the current Final Scene. A production failure or cancellation preserves upstream Raw H3 and Final Scene artifacts.

## Assembly snapshot

An assembly selects one current valid artifact per scene under an explicit source policy. It records the selected artifact identity and lineage at one point in time. A later source, scene, artifact, review, policy, or master-audio change makes that snapshot stale and requires a new assembly ID.

A selected assembly artifact does not imply that the scene had one render attempt or one historical output.

# Lifecycle authority

Builder state comes from durable records and validation. The authority chain is:

```text
ComfyUI queue/history observation
    -> Render Job reconciliation
    -> Builder-owned output association
    -> media probe and currentness validation
    -> Final Scene or Production Scene state
    -> assembly eligibility
```

ComfyUI can report execution success while the Builder lacks a usable owned output. Telemetry can decorate a durable status but cannot create one.

`backend/render_jobs.py` owns:

- ComfyUI submission;
- queue/history reconciliation;
- retry and cancellation;
- output discovery and ownership;
- durable H3 job state.

`backend/render_finalize.py` owns Final Scene validation, authoritative-audio remux, and promotion.

`backend/render_production.py` owns Production Scene state, technical post-process jobs, currentness, capability checks, and failure isolation.

`backend/render_batch.py` owns backend-controlled sequential scene dispatch. The browser does not advance a batch or render job.

Any future assembly worker must remain a bounded backend operation. It must not become a general editor scheduler.

# Ownership map

| Area | Owner | Durable responsibility | Boundary |
| --- | --- | --- | --- |
| Source and scenes | `backend/source.py`, `backend/scenes.py`, `backend/projects.py` | Master audio, SRT, scene IDs, exact timing, contiguous coverage, project schema. | No transcription or inferred timeline. |
| Visuals | `backend/visuals.py` | Generation method, accepted keyframe, ordered references, visual readiness. | No queue lifecycle or final approval. |
| Prompt compiler | `backend/prompt_service.py` | Deterministic H3 structure, relay validation, fingerprints, currentness. | No local LLM, timing inference, or reference invention. |
| Workflow registry | `backend/workflows.py` | Immutable workflow manifests, patch points, node and model contracts, output roles. | No arbitrary graph editing. |
| Render Jobs | `backend/render_jobs.py` | ComfyUI lifecycle, retries, cancellation, output discovery, job records. | No candidate selection or browser scheduling. |
| Final Scene | `backend/render_finalize.py` | Validated final media, authoritative scene audio, promotion. | No preview approval or editorial decisions. |
| Production Scene | `backend/render_production.py` | Technical upscale and delivery preparation, currentness, failure isolation. | No creative finishing. |
| Batch orchestration | `backend/render_batch.py` | Durable sequential scene orchestration and recovery. | No assembly scheduling or frontend progression. |
| Assembly | Planned `backend/assembly.py` and media adapter | Timeline validation, artifact resolution, master-audio attachment, draft MP4, assembly manifest. | No editing, candidate database, review state machine, or Resolve control. |
| Resolve export | Planned assembly export service | CSV first and later qualified interchange formats. | No Resolve automation or project mutation. |
| Browser presentation | `web/extension.js` | Durable status presentation, controls, polling, draft preservation. | No scheduler, source of truth, or hidden browser database. |

Routes should remain thin wrappers around backend services. `backend/routes.py` must not become the feature layer.

# Persistence rules

`project.json` stays compact. It stores source, scenes, current project settings, and current pointers. It does not store full job, attempt, candidate, review, or assembly histories.

Project-owned writes remain atomic. A failed replacement must preserve the prior valid artifact and record the failure separately.

Detailed records use their own versioned schemas and project-owned paths. Records must validate project ownership, IDs, fingerprints, media identities, and symlink/traversal safety.

Runtime projects and render records under `projects/` provide evidence. They do not become source fixtures unless a test explicitly copies them into a temporary root.

# V2 schema decisions

## Project pointer

The approved V2 migration adds an assembly pointer to the project document. The pointer remains separate from the external manifests:

```json
{
  "assembly": {
    "schema_version": 1,
    "current_assembly_id": null,
    "current_assembly_fingerprint": null
  }
}
```

`current_assembly_id` means the latest successful assembly whose basis still matches the current project, selected artifacts, policies, and source audio. It never means the latest requested assembly.

A failed or active replacement does not replace a prior valid current pointer. A stale current assembly clears the pointer while preserving its manifest and draft for history. The latest active request remains in the external assembly manifest store.

## Assembly manifest

V2.1 keeps detailed manifests under:

```text
renders/assemblies/<assembly_id>/
├── assembly.json
├── draft_music_video.mp4
├── scenes.csv
└── logs/
```

Version 1 manifests include:

- `assembly_schema_version`;
- `assembly_id` and `project_id`;
- state;
- source-audio identity;
- `timeline_fingerprint`;
- `assembly_fingerprint`;
- versioned source and output policy IDs;
- ordered scene records;
- timeline validation;
- output identity and failure data.

Each scene record includes:

- order and exact timeline range;
- selected artifact kind and identity;
- selected `final_job_id` and `production_job_id` lineage;
- nullable `generation_attempt_id`, `candidate_set_id`, and `candidate_id`;
- nullable `review_decision_id`;
- `scene_fingerprint`.

V2.1 writes the future lineage and review references as `null`. Those fields do not activate future features.

## Job reference semantics

`final_job_id` and `production_job_id` identify the jobs associated with the selected artifact in that assembly snapshot. They do not contain all retries or attempts and do not establish one job per scene.

Render Job, post-process, generation-attempt, candidate, and review records retain their own history.

## Fingerprints

`scene_fingerprint` is the SHA-256 digest of the canonical selected-scene assembly basis:

- scene ID;
- exact timeline start and end;
- selected artifact kind;
- normalized artifact identity and content/media fingerprints;
- selected job and future lineage references;
- review reference when one exists.

`timeline_fingerprint` covers ordered scene IDs and exact SRT-derived timing. It excludes generated media and generation provenance.

`assembly_fingerprint` covers the whole assembly basis, including timeline fingerprint, source-audio identity, source/output policy IDs, and ordered scene fingerprints.

Future `generation_attempt_fingerprint`, `candidate_selection_fingerprint`, and `review_decision_fingerprint` retain separate meanings and owners. A future schema version may add them to the selected basis when their records can change without immutable IDs changing.

## Manifest migration

Every `assembly.json` carries `assembly_schema_version`. V2.1 writes version 1.

Readers migrate supported historical manifests in memory. Successful historical manifests remain immutable and keep their original schema version and bytes. New writes use the current version. Core field, state, identity, or fingerprint meaning changes require a version bump and explicit migration reader.

Migration never guesses missing artifact, attempt, review, or ComfyUI identity. Unsupported or ambiguous records fail closed for assembly and export.

Project schema migration and manifest migration remain separate. The project v7 to v8 migration adds a compact pointer. It does not rewrite external assembly history.

# Future extension rules

## Candidate generation and attempts

Future candidate generation must add an attempt layer above Render Jobs. A scene may own many attempts and many Render Jobs across retries.

Candidate sets own grouping, candidate state, selection, and retention. Generation packages own immutable per-attempt inputs. Render Jobs continue to own queue/history and raw output lifecycle.

Assembly records only the selected candidate or attempt references. Assembly does not enumerate candidates, compare previews, or promote preview media.

## Review and approval

A future review service owns decision state, approval, replacement/remake lineage, notes, and decision fingerprints. V2.1 must not infer approval from Render Job success or the existence of a Final Scene or Production Scene.

Assembly should consume a future eligibility provider. The provider can add an approval gate without replacing the resolver or changing source timing.

`review_decision_id` remains an opaque nullable reference in V2.1.

## ComfyUI MCP

A future ComfyUI MCP adapter may sit beside the existing ComfyUI client as a read-oriented layer for:

- workflow and runtime validation;
- queue/history diagnostics;
- node, model, tool, disk, or hardware health observations.

MCP observations carry a source and timestamp. They can enrich diagnostics and preflight. They cannot replace Render Job reconciliation, promote artifacts, approve scenes, mark assemblies current, alter SRT timing, or write required project state.

MCP must not create a second scheduler, cancellation authority, project store, or output-currentness model. MCP-specific IDs, URLs, and response payloads do not belong in required project or assembly identity fields.

## Schema evolution

Future features add versioned records and pointers. They do not place growing histories into `project.json` or overload current fields with new meanings.

Future changes must preserve:

- stable UUID references;
- explicit artifact identity;
- separate current pointers and historical records;
- separate technical state and human review state;
- separate source timing and generated media timing;
- explicit migration readers;
- project ownership and path safety.

A future feature must add a field or schema version when it introduces a new basis. It must not reinterpret an existing fingerprint, pointer, job ID, artifact kind, or state enum.

# Non-negotiable constraints

1. The supplied SRT and master audio define the editorial timeline.
2. Scene IDs, not filenames, define order.
3. Raw H3, Final Scene, Production Scene, and assembly output remain separate artifact layers.
4. Builder durable state, not ComfyUI telemetry, defines lifecycle readiness.
5. ComfyUI queue/history observations must reconcile through the Render Job service.
6. Final Scene and Production Scene preserve authoritative audio and failure isolation.
7. Assembly attaches the original master audio once and does not use per-scene audio as the song master.
8. Assembly fails closed on missing, stale, ambiguous, incompatible, or foreign media.
9. Project JSON stores compact current pointers. Detailed histories stay in external project-owned records.
10. Successful manifests are immutable snapshots. Changed inputs create new lineage.
11. Backend services own progression. The browser presents state and never becomes a scheduler.
12. Resolve remains the editor and finishing environment.
13. Production Scene remains technical. Creative finishing stays out of the Builder.
14. New integrations must preserve existing owners and must not create competing currentness models.

# Document maintenance rule

Update this file only when a durable architecture, product boundary, ownership rule, invariant, or approved schema decision changes.

Keep release tasks, bug reports, test results, environment observations, and implementation details in their appropriate planning, issue, qualification, or source documents.
