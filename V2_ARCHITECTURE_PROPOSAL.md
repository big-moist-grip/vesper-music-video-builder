# V2 Architecture Proposal

## Status and decision

This document defines a future architecture. It does not implement features and does not authorize source changes.

V2 should deepen the Builder's production workflow without broadening its identity:

```text
Song
  -> SRT-derived Scenes
  -> Storyboard and Shot Planning
  -> Visual Planning
  -> Deterministic Prompt Compilation
  -> Optional Seed Hunter Candidates
  -> Final MiniMax H3 Generation
  -> Review and Approval
  -> Technical Production Scene
  -> Draft Assembly
  -> DaVinci Resolve
```

The central decision is to add planning, selection, provenance, review, and assembly layers around the existing H3 pipeline. V2 should not replace the current source, Visuals, Prompt, Render Job, Final Scene, or Production Scene contracts.

## 1. Product identity and non-negotiable boundaries

The Builder is a focused AI music-video production environment. It turns an intentional song timeline into reproducible, reviewable H3 scene outputs.

The Builder owns:

- project and source preparation;
- SRT-derived scene timing;
- storyboard direction and visual planning;
- project-local characters, locations, and references;
- deterministic H3 prompt structure;
- MiniMax H3 generation through approved manifests;
- candidate generation and selection when the user enables it;
- scene review and approval records;
- technical Production Scene preparation;
- draft assembly and timeline metadata for Resolve.

DaVinci Resolve owns:

- editing;
- colour grading;
- effects;
- transitions;
- final finishing;
- audio mixing;
- editorial changes after the Builder exports its reviewed material.

V2 will not add:

- a general ComfyUI workflow collection;
- arbitrary node or model controls;
- local LLM runners or cloud LLM APIs;
- automatic transcription;
- a video-editor replacement;
- LUT management;
- colour matching or grading;
- film grain;
- visual effects;
- creative finishing controls;
- Resolve project automation or Resolve replacement functionality.

Production Scene remains a technical preparation layer. Approved profiles may perform resolution enhancement, approved upscaling, codec/container preparation, frame-rate validation, and authoritative-audio delivery preparation. They must not make creative look decisions.

## 2. Current architecture strengths

### 2.1 The Builder owns a workflow instead of exposing a graph editor

The no-op ComfyUI node gives the frontend an entry point. `web/extension.js`, `backend/routes.py`, the backend services, project persistence, manifest registry, and job lifecycle own the production workflow. ComfyUI executes approved graphs; it does not define the product surface.

The existing UI already provides a focused sequence:

```text
Setup -> Storyboard -> Visuals -> Prompts -> Renders
```

V2 can add candidate review and assembly status to this surface without exposing arbitrary graph plumbing.

### 2.2 Source audio and SRT provide one editorial contract

`backend/source.py` validates the supplied master audio and SRT. `backend/scenes.py` creates lyric and instrumental scenes that cover the source timeline contiguously. Each scene retains exact millisecond boundaries and lyric cue provenance. The Builder keeps the SRT as the timing and lyric authority.

The H3 frame plan serves a different purpose. It satisfies the 24 FPS and `5 + 17n` generation contract while the scene start and end values remain editorial facts. V2 must preserve that separation through candidate generation and assembly.

### 2.3 The relay architecture gives creative control a safe boundary

The prompt pipeline follows this shape:

```text
deterministic source compiler
    -> manual ChatGPT relay
    -> strict response validation
    -> machine-owned prompt reassembly
    -> editable Final Prompt
```

The relay can provide creative prose. The backend continues to own timing, lyrics, references, tags, method structure, continuity facts, output identity, and render metadata.

### 2.4 Fingerprints expose stale work

Storyboard requests, Visuals readiness, prompts, preparation, finalization, and Production Scene records already use fingerprints or file identities. V2 can extend the same pattern to:

- shot plans;
- continuity states;
- candidate sets;
- candidate selections;
- Final Scene approval;
- assembly manifests;
- Resolve export packages.

V2 should not create parallel boolean flags that can disagree with the existing currentness model.

### 2.5 Manifest-driven execution limits workflow sprawl

`backend/workflows.py` centralizes workflow files, method identities, node types, models, patch points, graph links, output contracts, and forbidden donor content. The existing registry supports two visual-conditioning methods:

- `keyframe_i2v`;
- `reference2video`.

Seed Hunter adds a profile dimension to those methods. It does not add a third Visuals method.

### 2.6 Durable jobs and artifact separation already exist

The current lifecycle separates:

```text
Raw H3 -> Final Scene -> Production Scene
```

`render_jobs.py` owns durable job IDs, queue/history reconciliation, cancellation, retry lineage, output ownership, and restart recovery. `render_finalize.py` validates raw output and remuxes authoritative scene audio. `render_production.py` isolates technical post-processing from upstream artifacts.

V2 should place candidate attempts and review decisions beside these services. It should not replace them with a frontend scheduler or a second output-currentness model.

### 2.7 Project-owned persistence supports review and handoff

Schema-v7 `project.json` stores source metadata, scenes, entities, Story Direction, storyboard, Visuals, prompts, and production settings. Project-owned directories store source media, references, keyframes, scene audio, render jobs, finalization records, production records, and batches. Atomic writes and prior-valid-output preservation provide a sound base for candidate, review, and assembly records.

### 2.8 The current gaps define the V2 entry points

The current architecture still lacks:

- a user-facing generation-attempt group;
- explicit seed and profile provenance;
- a candidate gallery and selected-final pointer;
- a formal shot plan and continuity state model;
- prompt version comparison and reusable creative context;
- a review and approval record above individual artifacts;
- full-song assembly;
- Resolve timeline metadata export;
- live qualification of the H3, production, and complete audio-to-output paths.

V2 should address these gaps in dependency order. It should resolve current lifecycle defects and live qualification gaps before it adds more render volume.

## 3. Future architecture vision

### 3.1 Artifact and ownership flow

```text
Project source contract
    | master audio + supplied SRT
    v
SRT-derived scene contract
    | scene IDs + exact source timing + cue provenance
    v
Storyboard contract
    | scene cards + shot planning + continuity + references
    v
Visuals and Prompt contracts
    | accepted keyframe/references + current Final Prompt
    v
Render strategy
    | direct final generation OR optional Seed Hunter candidate set
    v
Generation attempts
    | candidate attempts and final-generation attempt, each backed by Render Job
    v
Raw H3 output
    | job-owned output discovery
    v
Final Scene
    | authoritative scene audio + media validation
    v
Review and approval
    | candidate/final decision + replacement/remake lineage
    v
Production Scene
    | approved technical upscale or delivery preparation only
    v
Assembly manifest
    | scene-ID order + coverage + selected artifact identities + master audio
    v
Draft music video + Resolve metadata
```

### 3.2 One scene remains the primary render unit

An SRT-derived scene remains the unit that owns:

- source timing;
- lyric or instrumental provenance;
- exact scene audio;
- Visuals method;
- prompt state;
- generation attempts;
- Final Scene;
- Production Scene;
- review decision;
- assembly membership.

A storyboard shot is a planning object inside that scene. A candidate attempt is an execution object associated with that scene. Neither object changes the scene timeline.

### 3.3 Optional generation strategy, fixed visual methods

The user may choose either:

```text
Direct final render
    -> final-generation Render Job
```

or:

```text
Preview Candidates
    -> Candidate Set
    -> Candidate Comparison
    -> Selection
    -> final-generation Render Job
```

Both paths use the scene's existing `keyframe_i2v` or `reference2video` method. Seed Hunter changes render intent and workflow profile, not the Visuals method.

### 3.4 Currentness flows forward through every layer

A change to an authoritative input invalidates downstream records:

```text
source/SRT change
    -> scene identity or timing change
    -> storyboard and Visuals currentness change
    -> prompt currentness change
    -> candidate selection stale
    -> Final Scene stale
    -> Production Scene stale
    -> assembly stale
    -> Resolve export stale
```

A change to a technical Production Scene profile invalidates Production Scene and assembly outputs. It does not invalidate source timing, storyboard direction, prompt history, or candidate selection.

### 3.5 The Builder fails closed on incomplete timelines

The Builder must not silently drop a missing scene, stretch a scene to fill a gap, infer new timing, or replace master audio. A blocked preflight should show the scene ID, expected timing, missing artifact, and next action.

## 4. System 1: Seed Hunter and candidate generation

### 4.1 User flow

The first Seed Hunter release should work per scene:

1. The user opens a scene whose source, Visuals, prompt, workflow, and runtime checks are current.
2. The user selects `Preview Candidates`.
3. The backend freezes the input basis for one candidate set.
4. The backend derives three explicit seeds and creates three candidate attempt records.
5. The runner prepares and submits candidates sequentially.
6. Existing Render Job reconciliation discovers each usable raw output.
7. The backend materializes a project-owned preview artifact or a validated safe media reference.
8. The UI shows candidate cards as attempts complete.
9. The user selects one candidate and can add an optional review note.
10. The backend writes a selection pointer and selection fingerprint.
11. The user selects `Generate Final`.
12. The backend creates a new final-generation package using the selected candidate's seed and current inputs.
13. The final job runs through the existing Final Scene finalization path.
14. The existing Production Scene path resolves the selected Final Scene.

A user can keep the current one-shot render path. Seed Hunter remains opt-in.

### 4.2 Generation attempt abstraction

The current system treats one Render Job as one attempt. V2 adds a generation-attempt layer that groups related jobs without duplicating job lifecycle logic.

```text
CandidateSet 1 --- many CandidateAttempt
CandidateAttempt 1 --- 1 RenderJob
CandidateAttempt 1 --- 1 preview artifact
CandidateSet 1 --- 0..1 selected CandidateAttempt
Selected CandidateAttempt --- 1 FinalGenerationAttempt
FinalGenerationAttempt 1 --- 1 RenderJob
FinalGenerationAttempt --- 1 Final Scene
```

The attempt abstraction owns:

- intent: `preview_candidate` or `final_generation`;
- candidate set membership;
- candidate index;
- explicit seed;
- workflow profile;
- input and preparation fingerprints;
- preview artifact identity;
- selection provenance;
- candidate-specific state and failure.

`RenderJob` remains the authority for queue, ComfyUI prompt identity, output prefix, cancellation, retry, reconciliation, and raw output discovery.

A retry of a failed attempt keeps the same attempt ID and seed. A new visual hunt creates a new candidate set, new attempts, and a new hunt nonce. Retry must not become an accidental new candidate.

### 4.3 Workflow profiles

The current method-only identity becomes:

```text
(generation_method, workflow_profile_id)
```

Planning examples:

```text
keyframe_i2v + preview_draft_v1
keyframe_i2v + final_quality_v1
reference2video + preview_draft_v1
reference2video + final_quality_v1
```

Each profile must declare:

- profile ID and version;
- purpose: preview or final generation;
- generation method;
- immutable workflow template or approved scalar patch policy;
- required node types and models;
- declared seed patch point;
- timing and audio policy;
- dimensions and FPS;
- output role and encoding contract;
- runtime requirements;
- manifest and template identity;
- qualification status.

The first preview profile should preserve the visual facts that matter for comparison:

- the same H3 conditioning method;
- the same source audio segment;
- the same prompt structure;
- the same keyframe or reference mapping;
- the same scene timing coverage;
- the same 24 FPS contract.

A lower-cost profile may change only a qualified setting such as sampler steps or another manifest-owned quality parameter. V2 must not assume that fewer steps, lower resolution, altered scheduler settings, or shorter audio creates a valid preview. Live qualification must establish the profile.

### 4.4 Seed policy

The backend should derive reproducible seeds from a stable basis:

```text
seed_basis =
    project_id
    + scene_id
    + prompt_source_fingerprint
    + visual_input_fingerprint
    + timing_plan_fingerprint
    + preview_profile_id
    + hunt_nonce
    + candidate_index

seed = stable_uint64(SHA-256(seed_basis))
```

The exact integer range must match the installed `RandomNoise` node. The backend persists the seed before submission and includes it in the generation-package and attempt fingerprints.

The UI may show `Candidate 1`, `Candidate 2`, and a quality label. It should not expose a general seed playground, raw node controls, or ComfyUI prompt IDs.

Same-seed retry reuses the seed. A new hunt changes the nonce. Historical jobs without an explicit seed must remain readable without guessed reproducibility claims.

### 4.5 Candidate storage and media ownership

Keep detailed candidate records out of the main project document:

```text
projects/<project_id>/
├── project.json
├── keyframes/
├── renders/
│   └── <scene_id>/
│       ├── candidate_sets/
│       │   └── <candidate_set_id>.json
│       ├── candidates/
│       │   └── <candidate_id>.json
│       ├── previews/
│       │   └── <candidate_id>.mp4
│       ├── generation_packages/
│       │   └── <package_id>/
│       │       ├── package.json
│       │       └── workflow.json
│       ├── jobs/
│       ├── final/
│       └── production/
```

The candidate set record stores the frozen input basis, candidate IDs, selected candidate pointer, selection fingerprint, state, and retention information. Each candidate record stores seed, profile, package, Render Job, raw output identity, preview identity, state, timestamps, and failure details.

A candidate media route must resolve a candidate record, verify project ownership, recheck file identity, prevent path traversal and symlink escape, and return only a Builder-owned preview artifact. The route must not expose arbitrary ComfyUI output paths.

### 4.6 Relationship to existing Render Jobs

Seed Hunter should reuse the current Render Job service:

- the candidate runner owns sequencing, not queue truth;
- `JobStore` owns durable lifecycle state;
- queue/history reconciliation remains authoritative;
- WebSocket telemetry remains presentation data;
- output prefixes remain Builder-job-owned;
- finalization accepts only final-generation jobs;
- Production Scene resolves only a validated current Final Scene.

The candidate runner needs a scene lock and a durable cursor. It should submit one candidate at a time, reconcile it, persist the result, then advance. A three-branch ComfyUI graph would increase VRAM pressure, complicate output association, and make recovery harder.

The normal batch runner should not generate candidates in its first release. A future explicit batch mode can process `preview_candidates` and `final_selected_candidates` after the per-scene flow proves stable.

### 4.7 Seed Hunter UI

Add a candidate panel to the Renders or Review workspace:

- `Preview Candidates` eligibility and blockers;
- three candidate cards with stable numbering;
- queued, running, succeeded, failed, cancelled, and stale states;
- project-owned playback;
- candidate duration and timing status;
- selection marker and optional note;
- retry same candidate;
- cancel active hunt while retaining completed candidates;
- start a new hunt;
- stale-selection explanation;
- `Generate Final` enabled only for a current selection;
- separate final-generation progress;
- clear distinction between Preview Candidate, Raw H3, Final Scene, and Production Scene.

The frontend must retain candidate records by candidate ID. It must not reduce all jobs to one object per scene.

## 5. System 2: Expanded storyboard without shot render units

### 5.1 Scene card model

Keep the existing SRT-derived scene as the parent. Extend each storyboard scene with structured planning data:

```json
{
  "scene_id": "...",
  "scene_type": "performance",
  "story_beat": "...",
  "performance_direction": "...",
  "camera_intent": "...",
  "movement_direction": "...",
  "references": [
    {
      "entity_type": "character",
      "entity_id": "...",
      "reference_id": "...",
      "role": "identity",
      "order": 1
    }
  ],
  "continuity": {
    "carry_in": "...",
    "carry_out": "...",
    "state_version": 1,
    "facts": []
  },
  "shot_plan": {
    "version": 1,
    "shots": []
  }
}
```

The exact field set requires schema design and migration review. The planning model must preserve current strict response validation and stable entity/reference IDs.

### 5.2 Shot planning data

A shot plan describes how one H3 scene should behave. It does not create render units.

A planning shot may include:

- `shot_id`;
- order;
- relative start and end time within the parent scene;
- framing or shot scale;
- composition anchor;
- camera direction;
- movement direction and speed intent;
- lighting or environment direction used for generation;
- visible subject IDs;
- location and reference IDs;
- action or performance beat;
- continuity in and continuity out;
- optional cut instruction.

The backend validates that relative intervals remain inside the parent scene. It rejects a shot plan that changes the parent scene start or end. It should allow a plan to cover only an intended portion when the remaining scene uses an explicit hold or continuous state. It should not invent cuts to fill missing planning data.

### 5.3 Camera intent and movement

Separate camera intent from character movement:

- camera intent: framing, camera path, lens or distance language, and planned reframe;
- movement direction: subject action, body movement, prop interaction, and performance energy;
- performance direction: singing, silent performance, instrumental presence, or narrative action;
- story beat: why the scene changes or holds.

The prompt compiler can assemble these fields in a stable order. The relay can add creative prose inside the declared sections. The backend retains the shot order, timing, subject ownership, and camera constraints.

### 5.4 Performance and narrative classification

The storyboard should distinguish the purpose of the scene without creating a second timing model. Proposed classifications:

- `performance`;
- `narrative`;
- `instrumental`;
- `b_roll`;
- `speaking` when the project explicitly uses dialogue.

The classification must coexist with the source scene's `lyric` or `instrumental` provenance. An instrumental source region can still receive a narrative or B-roll visual treatment. A performance classification cannot invent lyric text or singer ownership.

Singer or speaker mapping remains editor-supplied structured context. V2 does not add transcription.

### 5.5 Continuity state

Continuity facts should capture machine-checkable state, not create an LLM memory system. Candidate facts include:

- character identity, appearance, wardrobe, and visible state;
- location and time-of-day state;
- lighting state used for generation direction;
- prop or performance state;
- preceding-scene carry-in;
- next-scene carry-out;
- entity and reference IDs;
- the source storyboard version that established the fact.

The relay may suggest prose. The backend owns IDs, state version, inheritance rules, and fingerprints. A changed continuity state stales dependent prompts and candidates.

### 5.6 Reference planning

Storyboard reference planning should identify purpose and order without copying the reference storage model from another product:

- identity reference;
- location reference;
- prop or object reference when the active H3 method supports it;
- exact-start image when the selected method supports it;
- optional inspiration reference that does not enter the H3 graph.

`backend/entities.py` remains the owner of reference files. `backend/visuals.py` remains the owner of active method selection and renderable reference mapping. Storyboard required references synchronize into Visuals through the existing ownership checks.

### 5.7 Execution boundary

The first release keeps this relationship:

```text
Scene
  -> Shot Planning Data
  -> One H3 Generation
```

A shot plan may direct explicit camera changes or cuts in the prompt. It does not produce multiple H3 jobs. V2 should consider child render units only after review data shows that one H3 clip per scene cannot meet the desired output. That later decision would require a separate audio policy, render-unit identity, candidate selection model, continuity policy, and parent-scene assembly step.

## 6. System 3: Review and approval workflow

### 6.1 Review layers

Review should cover three artifact classes:

1. **Visual candidates:** generated or imported stills that may become the accepted keyframe.
2. **H3 candidates:** preview videos produced by Seed Hunter.
3. **Final scene outputs:** validated final-quality scene media before and after technical Production Scene preparation.

The Review workspace consumes records owned by Visuals, generation attempts, Finalization, and Production. It does not become a second render scheduler.

### 6.2 Visual still review

The current `visuals.scenes[].keyframe_i2v.accepted_keyframe` remains the render input pointer. V2 may add a visual candidate set under the project-owned keyframe area:

```text
keyframes/<scene_id>/
├── candidates/<visual_candidate_id>.<ext>
├── candidates/<visual_candidate_id>.json
└── selection.json
```

A visual candidate record should include:

- candidate ID and scene ID;
- source operation or import identity;
- image identity and dimensions;
- prompt version or user note when available;
- reference/input identities;
- created timestamp;
- currentness fingerprint;
- selection state.

Selecting a still updates the accepted keyframe through the existing Visuals service. It does not place an unvalidated candidate into H3 inputs. Replacing an accepted keyframe stales prompts, candidates, and downstream final outputs through the existing fingerprint flow.

### 6.3 H3 candidate comparison

Candidate comparison should show the user the information needed to choose motion and composition:

- candidate media;
- candidate number;
- generation profile label;
- timing and duration validation;
- selected seed label or reproducibility indicator;
- candidate status;
- optional user note.

The UI should hide node IDs, raw filesystem paths, and ComfyUI diagnostics. Candidate selection should not edit the prompt or silently change references. A user who wants a different creative direction edits the storyboard or prompt, which stales the candidate set and starts a new basis.

### 6.4 Scene approval

Scene approval is a durable decision record above media artifacts. It should include:

- scene ID;
- decision ID and schema version;
- approved artifact kind: `final_scene` or `production_scene`;
- approved artifact identity;
- candidate and final-generation provenance;
- review basis fingerprint;
- decision state;
- user note;
- created and updated timestamps;
- prior decision identity when replacing an approval.

Proposed states:

```text
UNREVIEWED
CANDIDATES_READY
SELECTION_REQUIRED
FINAL_GENERATING
FINAL_READY
APPROVED
REMAKE_REQUESTED
STALE
BLOCKED
SUPERSEDED
```

The state machine must derive technical readiness from durable records. A browser-only flag cannot make a scene assembly-ready.

### 6.5 Scene replacement and remake requests

A remake request creates new lineage:

```text
Approved Final Scene
    -> remake request with reason and source fingerprint
    -> new candidate set or direct final-generation attempt
    -> new Final Scene
    -> new review decision
    -> old approved artifact retained as superseded history
```

The Builder must not overwrite the previous successful media before the replacement passes validation and promotion. A failed remake leaves the previous approved scene available. A replacement becomes current only after finalization and an atomic pointer update.

A user may request a remake because of:

- motion or composition;
- continuity;
- performance direction;
- reference mismatch;
- prompt revision;
- technical output failure.

The request reason helps review and provenance. It does not become machine-owned prompt text without an explicit prompt edit.

### 6.6 Interaction with existing pipeline artifacts

| Artifact or service | V2 review responsibility |
| --- | --- |
| Visuals | Own accepted keyframe, active H3 method, ordered references, and visual readiness. |
| Render Jobs | Own queue lifecycle, retry, cancellation, output discovery, and attempt execution. |
| Candidate Set | Own preview-attempt grouping, candidate selection, and stale-selection state. |
| Final Scene | Own validated final-quality scene media and authoritative scene audio. |
| Production Scene | Own approved technical upscale or delivery output only. |
| Review record | Own scene decision, approval, remake lineage, and current approved artifact pointer. |
| Assembly | Resolve one approved/current scene output per timeline scene and record its identity. |

The Review workspace reads these records. It does not copy them into a second state tree.

## 7. System 4: Full-song draft assembly

### 7.1 Desired pipeline

```text
Completed approved scene outputs
    -> timeline validation
    -> assembly manifest
    -> controlled concatenation + master-audio attachment
    -> project-owned draft music video
    -> Resolve timeline metadata
    -> DaVinci Resolve
```

The Builder produces a review draft. Resolve remains responsible for editorial decisions and final finishing.

### 7.2 Output resolver

The assembly service resolves one current artifact per SRT-derived scene:

1. Prefer an approved current Production Scene when the user selected technical production.
2. Permit an explicit fallback to an approved current Final Scene when no Production Scene exists.
3. Reject Raw H3 output, preview candidates, stale artifacts, failed jobs, and ambiguous output identities.
4. Require one selected artifact for every scene in canonical scene order.
5. Reject mixed dimensions, FPS, codec, or audio policies unless a named, qualified delivery profile permits them.

The resolver must not choose by filename, newest filesystem timestamp, or newest successful Render Job alone.

### 7.3 Timeline validation

The preflight reads scene order and exact timing from the project scene contract. It verifies:

- scene IDs are unique;
- scene order matches the project;
- each selected output belongs to the expected project and scene;
- each output passes media probing;
- output duration covers the expected scene range within a declared technical tolerance;
- scene ranges contain no gaps or overlaps;
- the first scene starts at zero;
- the final scene ends at the master-audio duration;
- all selected outputs meet one declared output policy;
- the selected master audio identity matches the project source;
- stale scene, Final Scene, Production Scene, or review fingerprints block assembly.

The service must report the exact scene ID and timeline range for each blocker.

### 7.4 Assembly manifest

Keep detailed assembly history outside `project.json`:

```text
renders/assemblies/<assembly_id>/
├── assembly.json
├── draft_music_video.mp4
├── scenes.csv
└── logs/
```

The assembly manifest should include:

```json
{
  "assembly_schema_version": 1,
  "assembly_id": "...",
  "project_id": "...",
  "source_audio": {
    "relative_path": "source/master_audio.ext",
    "identity": {}
  },
  "timeline_fingerprint": "...",
  "output_policy_id": "draft_h264_aac_v1",
  "scenes": [
    {
      "order": 1,
      "scene_id": "...",
      "timeline_start_ms": 0,
      "timeline_end_ms": 6679,
      "artifact_kind": "production_scene",
      "artifact_identity": {},
      "final_job_id": "...",
      "production_job_id": "...",
      "review_decision_id": "...",
      "scene_fingerprint": "..."
    }
  ],
  "state": "READY"
}
```

The manifest becomes immutable after successful assembly. A new scene output, source change, output policy change, or review decision creates a new assembly ID and marks the prior draft stale.

### 7.5 Audio and media handling

The assembly service attaches the original project master audio. It does not reuse generated per-scene audio as the song master and does not infer a new timeline from audio analysis.

The controlled assembly path should:

- remove or ignore scene audio during video concatenation;
- attach the project-owned master audio once;
- preserve the master audio's authority;
- validate final container duration against the source duration;
- record codec and stream metadata;
- preserve the scene-level provenance in `assembly.json`.

The Builder may use FFmpeg for this bounded operation. It should not expose an editor timeline, clip trimming, overlays, transitions, or audio mixing controls.

### 7.6 Resolve export options

| Format | V2 value | Decision |
| --- | --- | --- |
| CSV timeline metadata | Simple, inspectable, easy to test, and sufficient for scene IDs, exact ranges, source paths, output paths, methods, review decisions, and profile IDs. | Build first. |
| FCPXML | Potentially useful for Resolve clip placement, markers, and richer metadata. It requires target Resolve-version testing, path rules, frame-rate mapping, and relink validation. | Evaluate after CSV works. |
| EDL | Useful for a limited one-track editorial handoff. It cannot carry the full provenance and reference metadata needed by the Builder. | Optional compatibility export, not the primary format. |
| Resolve XML | The term covers Resolve-readable XML variants and requires a concrete target version and import test. It may overlap with an FCPXML path. | Do not commit before a target Resolve fixture exists. |

V2 should export metadata and open media locations for the user. It should not launch Resolve, mutate a Resolve project, or promise automatic relinking without live qualification.

## 8. System 5: Prompt improvements

### 8.1 Style bible as generation context

Add a versioned, project-owned style-bible context for generation direction. It may describe:

- visual language;
- narrative tone;
- subject and location conventions;
- composition vocabulary;
- camera vocabulary;
- motion vocabulary;
- performance conventions;
- generation-oriented palette or atmosphere guidance.

The style bible directs generated imagery and motion. It does not grade, colour-match, or finish rendered media. It does not contain machine-owned timing, reference tags, or output settings.

The project should store the current style-bible pointer and fingerprint in `project.json`; detailed versions belong in a project-owned prompt context directory.

### 8.2 Reusable creative context

A reusable context record can hold approved creative facts shared across scenes:

- story premise;
- subject identity descriptions;
- location descriptions;
- recurring props;
- performance conventions;
- continuity facts;
- user-authored exclusions or constraints.

The user edits this context through the Builder. The manual ChatGPT relay receives a bounded snapshot. The backend controls which fields enter the prompt and records the context fingerprint.

A context change should stale affected storyboard, prompt, candidate, and review records. The Builder should not maintain hidden LLM memory.

### 8.3 Prompt version history and comparison

Keep the current render-ready Final Prompt in the existing prompt record. Add immutable prompt versions under a prompt-owned project directory:

```text
prompts/<scene_id>/versions/<prompt_version_id>.json
```

Each version should include:

- prompt version ID;
- generation method;
- compiler version;
- source fingerprint;
- storyboard and shot-plan fingerprints;
- style/context fingerprint;
- relay response fingerprint;
- final prompt text;
- created timestamp;
- user note;
- superseded/current state.

The UI can show a diff between two versions and explain which structured inputs changed. A version becomes render-ready only when the current source and relay validation rules pass and the user explicitly saves it.

### 8.4 Continuity memory

Continuity memory should use structured facts and explicit inheritance:

```text
previous scene carry-out
    -> next scene carry-in
    -> prompt compiler context
    -> shot and reference validation
```

The Builder should show the inherited facts and let the user revise them. The relay can propose prose around those facts. It cannot create a new entity, reference, scene duration, or ownership relationship through prose alone.

### 8.5 Prompt lint

The prompt compiler should add lint checks for:

- unknown subject or reference names;
- unsupported reference tags;
- lyric text not matching the source scene;
- unsupported timing claims;
- shot order contradictions;
- continuity facts that conflict with the current state;
- camera or movement instructions that violate the shot plan;
- generated dialogue or lyrics in an instrumental or B-roll scene;
- method-incompatible prompt sections;
- stale style/context or storyboard fingerprints.

The lint result should explain the blocker and preserve the user's draft. It should not silently rewrite the creative prose.

### 8.6 Prompt features that remain out of scope

V2 will not add:

- local Gemma or other local prompt runners;
- LM Studio integration;
- LLM API calls;
- automatic transcription or lyric repair;
- freeform prompt ownership by the relay;
- automatic browser AI prompt generation;
- a generic prompt-enhancement button that can alter machine-owned structure.

## 9. Proposed data model changes

### 9.1 Schema strategy

The current project uses schema-v7. V2 should use an additive migration, likely schema-v8 for current pointers and planning fields. Detailed histories remain in separate project-owned JSON records.

Migration rules must:

- keep schema-v1 through v7 projects readable;
- normalize missing V2 sections to empty records;
- avoid guessing seeds or candidate selections for historical jobs;
- preserve existing Final Scene and Production Scene artifacts;
- preserve current prompt and Visuals semantics;
- reject malformed new records through strict validators.

### 9.2 Project document additions

Keep `project.json` compact. Add current pointers and fingerprints, not full attempt history:

```json
{
  "schema_version": 8,
  "generation": {
    "schema_version": 1,
    "scenes": [
      {
        "scene_id": "...",
        "candidate_set_id": null,
        "selected_candidate_id": null,
        "selected_final_job_id": null,
        "selection_fingerprint": null
      }
    ]
  },
  "review": {
    "schema_version": 1,
    "scenes": [
      {
        "scene_id": "...",
        "state": "UNREVIEWED",
        "current_decision_id": null,
        "approved_artifact_kind": null,
        "approved_artifact_id": null,
        "review_fingerprint": null
      }
    ]
  },
  "assembly": {
    "schema_version": 1,
    "current_assembly_id": null,
    "current_assembly_fingerprint": null
  }
}
```

The current project continues to own source, scenes, entities, Story Direction, storyboard, Visuals, prompts, and production settings. The new sections contain pointers only.

### 9.3 Storyboard additions

Extend each current storyboard scene with:

- `scene_type` or performance/narrative classification;
- `shot_plan` and version;
- camera intent;
- movement direction;
- performance direction;
- continuity in/out state;
- structured continuity facts;
- ordered reference plan;
- shot-plan fingerprint.

The storyboard response contract must retain exact scene order, stable entity IDs, reference ownership, mode validation, and request fingerprints.

### 9.4 Generation package and attempt records

A generation package is the immutable preparation input for one attempt:

```json
{
  "package_schema_version": 1,
  "package_id": "...",
  "project_id": "...",
  "scene_id": "...",
  "intent": "preview_candidate",
  "candidate_set_id": "...",
  "candidate_id": "...",
  "generation_method": "reference2video",
  "workflow_profile_id": "preview_draft_v1",
  "seed": 123,
  "prompt_source_fingerprint": "...",
  "visual_input_fingerprint": "...",
  "storyboard_fingerprint": "...",
  "timing_plan": {},
  "source_audio_identity": {},
  "workflow_identity": {},
  "output_contract": {},
  "queue_submitted": false
}
```

The Render Job record references the package. It does not duplicate the package's full workflow map.

### 9.5 Review records

Store review decisions under a project-owned review tree:

```text
renders/<scene_id>/review/
├── decisions/<decision_id>.json
└── current.json
```

The current pointer should identify the approved artifact and the fingerprint basis. A decision record should preserve prior decision lineage and remake reason.

### 9.6 Production data restrictions

Production settings remain narrow:

```json
{
  "production": {
    "upscale_method": "none"
  }
}
```

Future fields may identify an approved upscaling or delivery profile. V2 must not add look, LUT, colour, grain, VFX, or creative-finishing fields to Production Scene.

### 9.7 Assembly records

Assembly details live under `renders/assemblies/<assembly_id>/`. The manifest records scene order, exact timing, chosen artifact identity, Final Scene and Production Scene lineage, review decision, master audio identity, output policy, validation, and stale basis.

The `export/` directory stores generated CSV and future interchange files. It does not become a second project database.

## 10. Major systems and ownership boundaries

| System | Primary owner | V2 responsibility | It must not own |
| --- | --- | --- | --- |
| Source and scenes | `backend/source.py`, `backend/scenes.py`, project storage | Master audio, supplied SRT, scene IDs, exact timing, cue provenance, contiguous coverage. | Transcription, inferred lyric timing, candidate state. |
| Storyboard | `backend/storyboard.py`, Storyboard workspace | Story direction, scene cards, shot planning, performance classification, continuity, required references. | H3 jobs, scene timeline mutation, arbitrary reference files. |
| Visuals | `backend/visuals.py`, Visuals workspace | Active generation method, accepted keyframe, ordered references, visual readiness, visual candidate selection. | Render queue, prompt ownership, final approval. |
| Prompt compiler | `backend/prompt_service.py`, Prompt workspace | Machine-owned H3 structure, prompt fingerprints, relay validation, version history, lint. | Local LLM execution, timing inference, reference invention. |
| Workflow profiles | `backend/workflows.py`, immutable templates | Method/profile identity, patch points, node/model contracts, seed policy, output role. | User-entered graph editing, arbitrary model selection. |
| Generation packages | New generation-package service | Immutable per-attempt workflow and input package. | Queue lifecycle and final approval. |
| Candidate sets | New candidate service/store | Candidate grouping, sequential cursor, selection, stale state, safe preview artifacts. | Duplicate Render Job state or Final Scene promotion. |
| Render Jobs | `backend/render_jobs.py` | ComfyUI submission, queue/history truth, retry, cancellation, output discovery. | Candidate selection or browser scheduling. |
| Final Scene | `backend/render_finalize.py` | Validated final-quality scene media and authoritative scene audio. | Preview candidates or editorial approval state. |
| Review | New review service and Review workspace | Candidate comparison, scene approval, replacement/remake lineage, current decision. | Rendering, prompt compilation, project source truth. |
| Production Scene | `backend/render_production.py` | Approved technical upscale and delivery preparation with failure isolation. | LUTs, colour, grain, VFX, creative finishing. |
| Assembly | New assembly service | Timeline preflight, artifact resolution, master-audio attachment, draft MP4, assembly manifest. | Editing, transitions, overlays, audio mixing, Resolve control. |
| Resolve export | New assembly/export service | CSV first, later qualified interchange formats. | Resolve automation or project mutation. |
| Browser presentation | `web/extension.js` and existing helpers | Present durable state, preserve drafts, expose review and assembly controls. | Independent scheduler, durable lifecycle, hidden browser storage. |

Keep new logic in these owners. Avoid turning `backend/routes.py` into a feature layer or letting the frontend coordinate candidate or assembly progression.

## 11. Feature dependencies

```text
Current lifecycle hardening and live qualification
    |
    +--> Schema/fingerprint extension
    |        |
    |        +--> Storyboard shot plan and continuity state
    |        |        |
    |        |        +--> Prompt compiler and relay contract v2
    |        |                 |
    |        |                 +--> Candidate profile and seed contracts
    |        |                          |
    |        |                          +--> Candidate Set and selection
    |        |                                   |
    |        |                                   +--> Final-generation pointer
    |        |                                            |
    |        |                                            +--> Review approval and remake lineage
    |        |                                                     |
    |        |                                                     +--> Assembly resolver
    |        |                                                              |
    |        |                                                              +--> CSV export
    |        |                                                              +--> FCPXML/EDL evaluation
    |
    +--> Technical Production Scene qualification
             |
             +--> Assembly output-policy compatibility
```

Dependency rules:

- Seed Hunter needs a stable prompt, Visuals input, timing plan, workflow profile, and output lifecycle.
- Final-generation promotion needs candidate selection and a current input basis.
- Review approval needs a validated Final Scene or Production Scene identity.
- Assembly needs approved current scene outputs and one output policy.
- Resolve export needs a successful assembly manifest and stable media identities.
- Shot-aware execution remains gated behind review evidence.

Work can proceed in parallel on contract design, route tests, live qualification, schema migration design, prompt lint design, candidate media policy, assembly manifest design, and CSV format design. The repository must not grow independent schedulers or competing currentness models.

## 12. Development phases

### Phase V2.0: Stabilize the production core

Scope:

- fix verified route, cancellation, polling, stale-state, and output-discovery defects;
- add handler-level HTTP coverage;
- qualify current H3 methods where target hardware permits;
- qualify current technical Production Scene profiles;
- define release evidence for complete audio-to-Final Scene and audio-to-Production Scene paths;
- document the supported runtime and dependency gate.

Exit condition: the current two-method path reports truthful lifecycle, output, and live qualification status.

### Phase V2.1: Storyboard and prompt planning

Scope:

- add versioned storyboard planning fields;
- add shot planning inside each parent scene;
- add performance/narrative classification;
- add continuity facts and carry-in/carry-out state;
- add structured reference planning;
- add versioned style and creative context;
- extend deterministic prompt compilation;
- add prompt lint, diff, and provenance display.

Exit condition: a user can revise shot intent and continuity while the Builder preserves SRT timing, reference ownership, and machine-owned prompt structure.

### Phase V2.2: Candidate contracts and storage

Scope:

- generalize manifest identity to method plus profile;
- qualify one preview profile per H3 method;
- add explicit seed policy and seed validation;
- add generation-package schema;
- add Render Job intent and candidate metadata;
- add schema-v8 current pointers;
- add candidate-set records and safe preview storage.

Exit condition: the backend can dry-compile and persist one preview attempt and one final attempt without changing the existing direct-final path.

### Phase V2.3: Seed Hunter execution and selection

Scope:

- run three candidate attempts sequentially;
- reconcile each through existing Render Job services;
- materialize safe preview media;
- add candidate selection and stale checks;
- add same-candidate retry and new-hunt behavior;
- add restart and cancellation recovery;
- add candidate comparison UI.

Exit condition: a user can refresh, compare, select, and retain a current candidate without promoting preview media to Final Scene.

### Phase V2.4: Review and approval

Scope:

- add visual still candidate review;
- add scene approval records;
- add replacement and remake requests;
- add previous-successful-output retention;
- add Final Scene and Production Scene review status;
- add review provenance to assembly readiness.

Exit condition: the user can replace a scene without losing the previous valid output, and the Builder can explain which approved artifact will enter assembly.

### Phase V2.5: Draft assembly

Scope:

- add current scene-output resolver;
- add scene-ID timeline preflight;
- add coverage and overlap validation;
- add one-output-policy validation;
- attach original master audio;
- write project-owned draft MP4 and assembly manifest;
- add stale assembly detection and retry lineage.

Exit condition: the Builder produces a draft only when approved scene outputs cover the SRT-derived song timeline and satisfy the declared media policy.

### Phase V2.6: Resolve metadata handoff

Scope:

- export `scenes.csv` and assembly manifest;
- preserve source, scene, Final Scene, Production Scene, job, and review identities;
- document Resolve relink and import steps;
- evaluate FCPXML against a target Resolve version;
- add EDL only if a demonstrated one-track handoff needs it.

Exit condition: Resolve receives stable scene timing and media metadata without Builder-side editing or project automation.

### Phase V2.7: Conditional shot-aware execution gate

Do not implement this phase by default. Open it only if review evidence proves that one H3 clip per scene cannot meet the desired output.

A gate review must show:

- repeated failures caused by within-scene shot requirements;
- a stable shot plan and continuity model;
- a defined child audio policy;
- a parent-scene assembly strategy;
- measurable value over better prompt planning and candidate selection.

## 13. Risks and things that should not be built

### 13.1 H3 preview quality risk

The repository has not established that changing the H3 seed produces useful variation for either method. It also lacks a qualified lower-cost preview profile. V2 must not ship Seed Hunter from structural tests alone.

A preview can select motion or composition that final quality does not reproduce. The final-quality rerender remains the source of truth. Qualification must measure same-seed repeatability, seed-to-seed variation, preview-to-final similarity, duration, audio, VRAM, disk, and elapsed time.

### 13.2 Lifecycle multiplication

Three candidates multiply queue time, disk usage, ComfyUI history dependence, and recovery states. A large project can create hundreds of preview attempts before final rendering. Per-scene Seed Hunter comes first. Batch candidate generation requires a separate product decision, storage policy, and operational limit.

### 13.3 Stale selection and provenance risk

A prompt edit, storyboard change, continuity change, reference replacement, keyframe replacement, source replacement, method switch, profile change, or timing change must stale the candidate set. A final job must record both selected-candidate provenance and current input fingerprints.

Historical jobs without explicit seed metadata must not receive guessed seeds during migration.

### 13.4 Output and path risk

Candidate and assembly media need the same project-ownership rules as references and Final Scene outputs. Routes must block path traversal, symlink escape, foreign project access, ambiguous files, stale file identities, and raw ComfyUI path exposure.

### 13.5 Assembly compatibility risk

Scenes may have different dimensions or technical outputs if users mix native and upscaled artifacts. The assembly service must reject incompatible inputs or require a named, qualified output policy. It must not hide scaling, cropping, frame-rate conversion, or audio changes inside a convenience action.

### 13.6 Current repository defects

V2 work depends on resolving known lifecycle defects before adding more state:

- asynchronous post-process workers passed incorrectly to `asyncio.to_thread`;
- post-process cancellation calling the wrong client method;
- malformed batch JSON treated as an empty selection;
- post-process jobs without queue/history records remaining active;
- successful history with failed output discovery appearing usable;
- production path and currentness validation gaps;
- frontend polling and status visibility gaps;
- recursive batch transitions without a clear bound.

### 13.7 Scope limits

Do not build:

- LUT browsers, LUT import, colour matching, grading, or look presets;
- film-grain, texture, VFX, interpolation, denoise, or creative finishing systems;
- a multi-track timeline, clip editor, trim editor, overlay editor, transition editor, or audio mixer;
- Resolve automation, Resolve project mutation, or a Resolve replacement;
- a general workflow browser or donor-graph library;
- local LLM, LM Studio, Browser AI, API runner, transcription, or lyric alignment stack;
- arbitrary model, LoRA, sampler, node, seed, or workflow controls;
- multi-user accounts, cloud storage, collaboration, analytics, or remote queues;
- separate H3 render units for every storyboard shot without the V2.7 gate.

## 14. VRGDG comparison and final disposition

The completed `VRGDG_FEATURE_COMPARISON.md` provides the source-based feature inventory. This proposal applies the product boundary stated here, which supersedes any earlier suggestion to adapt look-processing features. LUTs, colour adjustments, film grain, and creative finishing receive `Ignore` even when VRGDG provides usable implementation patterns.

| VRGDG feature | Recommendation | Reason |
| --- | --- | --- |
| Project folder with persisted session and working artifacts | Adapt | Use the visible artifact inventory and recovery ideas, while retaining the Builder's schema, atomic stores, and durable lifecycle. |
| Project audio and per-scene audio | Adapt | Use advisory waveform and beat metadata for review. Keep master audio and supplied SRT authoritative. |
| SRT import and transcript repair | Ignore | The Builder accepts editor-provided SRT and must not add transcription or a competing lyric timeline. |
| Gap-aware scene segmentation | Adapt | Use gap and vocal-tail diagnostics without rewriting SRT-derived scene boundaries. |
| Approved stills and media history | Adapt | Add candidate and replacement history around the existing accepted-keyframe and Final Scene contracts. |
| Portable project package | Adopt | A self-contained, identity-preserving archive fits local production, backup, and handoff. |
| Wizard-to-builder handoff | Adapt | Add a focused setup checklist or draft state without adding a second LLM or workflow runner. |
| Persisted storyboard scene cards | Adapt | Expand the current storyboard with structured cards, shot intent, continuity, and references. |
| Camera-flow presets and duration-aware cut planning | Adapt | Use planning-only camera and cut data inside one H3 scene. Do not create shot render units. |
| First/last-frame continuity fields | Adapt | Convert useful in/out and carry-forward facts into the Builder's structured continuity model. |
| Subject and location reference catalog | Ignore | The Builder already owns stable entity IDs, references, required selectors, and method-specific synchronization. |
| Still approval and preview history | Adapt | Add visual candidate comparison and selection while keeping `accepted_keyframe` as the render input pointer. |
| Clip review and remake selection | Adapt | Add captured-frame review, notes, remake lineage, and replacement decisions on top of durable jobs. |
| Formal approval state machine | Ignore | VRGDG does not provide the required durable state model. Build a Builder-specific review record tied to Final Scene and Production Scene identities. |
| Broad prompt context bundle | Adapt | Add versioned style bible, creative context, motion notes, and continuity context without changing machine-owned structure. |
| JSON concept prompt generation and repair | Ignore | The Builder's deterministic compiler and strict relay validation provide a safer contract than a freeform concept generator. |
| Freeform final prompt paragraphs | Ignore | The relay must not own timing, references, tags, method structure, or output controls. |
| Editable prompts, drafts, instructions, and presets | Adapt | Add prompt version history, diff, and user notes. Do not add arbitrary runner instructions that bypass validation. |
| Local, LM Studio, API, and Browser AI runners | Ignore | The product keeps manual ChatGPT relay architecture and avoids local or cloud LLM dependencies. |
| Vision-assisted prompt handoff | Adapt | Allow manual visual descriptions or review annotations without introducing a hidden local vision runner. |
| Lyric, singer, instrumental, and B-roll mapping | Adapt | Preserve editor-supplied performance and vocal-status context while keeping SRT text and timing authoritative. |
| Standalone 2K, 3K, and 4K enhancement | Adapt | Use fixed technical resolution and delivery ideas only. Exclude its sharpen and film-grain effects and prefer qualified Builder upscalers. |
| Project-safe `.cube` LUT application | Ignore | LUT management and look processing belong in Resolve. |
| Colour and finishing adjustments | Ignore | V2 does not build colour grading or creative finishing tools. |
| Film grain as a controlled effect | Ignore | Film grain belongs in finishing, not Production Scene. |
| Preview before full post-processing | Ignore | The direct VRGDG feature previews creative finishing. V2 may preview technical eligibility, but it will not add a look-preview system. |
| Audio and media contract during finishing | Ignore | Retain the Builder's stricter authoritative-audio and media validation contract rather than copying VRGDG's implementation. |
| Visual timeline and beat-aware review | Adapt | Use timing-aware draft review without building an editor or replacing Resolve. |
| Audio-derived frame duration metadata | Adapt | Use exact compiled scene timing and frame metadata for assembly. Do not infer a new source timeline from audio analysis. |
| Folder-based full-video concatenation | Adapt | Implement explicit scene-ID manifest ordering and coverage checks instead of filename sorting. |
| Rerun and output-version handling | Adapt | Reuse immutable output lineage, retry state, and prior-valid-output preservation for assembly drafts. |
| Final soundtrack reattachment | Adapt | Attach the project-owned master audio once at song level and validate the result. |
| Resolve or editorial timeline metadata export | Adapt | Build CSV first, then evaluate FCPXML or limited EDL against a target Resolve version. |
| Explicit scene coverage and ordering validation | Adopt | Promote the Builder's existing contiguous scene validation into an assembly preflight. |

## 15. Final recommendation

Build V2 in this order:

```text
Harden current lifecycle and qualify live media paths
    -> add storyboard shot planning and continuity
    -> add prompt context, history, lint, and diff
    -> add Seed Hunter profile and attempt contracts
    -> add candidate comparison and scene approval
    -> add technical Production Scene delivery profiles only
    -> add scene-ID draft assembly
    -> add CSV and qualified Resolve metadata export
```

The Builder should become better at making deliberate music-video decisions, not broader at performing unrelated post-production work. Preserve SRT timing, the manual ChatGPT relay, MiniMax H3, two Visuals methods, manifest-owned graphs, durable jobs, authoritative audio, artifact provenance, and Resolve handoff. Decline features that increase surface area without improving scene selection, review confidence, reproducibility, or editorial handoff.

## Sources and planning basis

This proposal builds on:

- `PRODUCT_ROADMAP_ASSESSMENT.md`;
- `VIDEO_GENERATION_PIPELINE_ASSESSMENT.md`;
- `VRGDG_FEATURE_COMPARISON.md`;
- `PROJECT_CONTEXT.md`;
- `MEMORY.md`;
- `docs/ComfyUI Music Video Builder — Development Plan.md`;
- the current backend, workflow manifests, frontend extension, and tests described by those assessments.

The proposal is planning-only. No source code or existing planning document should change as a result of this document alone.
