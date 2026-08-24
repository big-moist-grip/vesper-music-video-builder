# Video Generation Pipeline Technical Assessment

## Scope and conclusion

This assessment covers the current MiniMax H3 scene-generation pipeline and a design for a future Seed Hunter feature. It does not implement the feature or change source code.

The current architecture has strong boundaries for two fixed H3 methods:

- `keyframe_i2v`
- `reference2video`

The manifest registry, preparation fingerprints, job store, queue/history reconciliation, finalization service, and Production Scene service provide useful extension seams. The current model treats one scene preparation as one render configuration, one render job as one attempt, and the newest successful job as the current upstream render. Seed Hunter needs a durable attempt group, explicit seed metadata, profile-aware preparation, candidate selection, and a selected-final pointer.

The safest design keeps the existing H3 pipeline intact and adds a generation-attempt layer beside it. Preview candidates should run as three sequential, job-owned attempts. A selected candidate should provide seed and provenance to a new final-quality attempt. The preview file itself should not become the Final Scene.

## 1. Current MiniMax H3 generation architecture

### 1.1 Workflow files and manifest registry

The repository stores API-format ComfyUI node maps in:

```text
workflows/h3_music_video_i2v_api.json
workflows/h3_music_video_ref2va_api.json
```

The authoritative registry lives in `backend/workflows.py`:

```python
WORKFLOW_MANIFESTS = {
    "keyframe_i2v": {...},
    "reference2video": {...},
}
```

Each generation manifest declares:

- `generation_method`;
- stable `workflow_id`;
- immutable `workflow_file`;
- required ComfyUI node types;
- required model files and loader mappings;
- declared patch points under `inputs`;
- provisional dimensions and H3 settings;
- output node and output contract;
- forbidden donor branches and settings that the validator must reject.

`production_manifest_registry()` returns a deep copy. `_load_workflow()` loads a JSON template and validates its API node shape. `validate_production_workflow_contract()` validates node types, model mappings, links, settings, patch points, and forbidden donor content. The current validator expects the exact two H3 generation methods.

The same module also contains a separate post-processing manifest registry for RTX VSR and SeedVR2. Those manifests consume Final Scene media. They are not H3 generation manifests.

### 1.2 H3 graph shape

The two H3 graphs share the same production pattern:

```text
project prompt + visual input + exact scene audio
    -> MiniMax H3 conditioning
    -> source-audio latent replacement
    -> RandomNoise + sampler
    -> H3 video/audio decode
    -> VHS_VideoCombine raw video output
```

The important method-specific mappings are:

| Generation method | Visual input | H3 node | Audio input | Prompt input | Noise seed | Output node |
|---|---|---|---|---|---|---|
| `keyframe_i2v` | One accepted keyframe | `MiniMaxH3ImageToVideo`, node `7` | `LoadAudio`, node `6` | node `7` | `RandomNoise`, node `16` | `VHS_VideoCombine`, node `20` |
| `reference2video` | Ordered still-image slots | `MiniMaxH3ReferenceToVideo`, node `8` | `LoadAudio`, node `7` | node `8` | `RandomNoise`, node `17` | `VHS_VideoCombine`, node `21` |

The REF2VA compiler removes unused reference loaders and rewrites active picture links in deterministic order. The prompt compiler assigns the corresponding `<Picture N>` and `<Subject N>` tags.

### 1.3 Workflow selection

The scene's Visuals record owns the generation method:

```json
{
  "scene_id": "...",
  "generation_method": "keyframe_i2v",
  "keyframe_i2v": {...},
  "reference2video": {...}
}
```

`backend/visuals.py` currently permits only `keyframe_i2v` and `reference2video`. New scenes default to `keyframe_i2v`. Changing the method preserves inactive-method assets and metadata.

The selection path is:

```text
project.json visuals.scenes[].generation_method
    -> render.py preflight
    -> production_manifest_registry()[method]
    -> workflow identity
    -> compile_production_workflow(method, ...)
    -> workflow.json
    -> render job
```

`backend/prompt_service.py` also branches on the same method so the prompt and workflow stay aligned. `render.py` rejects a method that does not exist in `GENERATION_METHODS` or the manifest registry.

The current selection model has no quality tier, render purpose, workflow profile, candidate group, or seed selection. The method is the only generation choice persisted in project state.

### 1.4 Workflow parameter injection

Preparation is the only place that compiles a production workflow. `prepare_render_scene()`:

1. Loads the current schema-v7 project.
2. Runs render preflight.
3. Resolves the current scene method.
4. Reads the current Prompt Director state.
5. Builds the H3 timing plan.
6. Resolves the accepted keyframe or ordered REF2VA references.
7. Hashes the source audio and visual assets.
8. Hashes the workflow template and manifest.
9. Deep-copies the immutable template.
10. Patches declared inputs.
11. Validates the compiled graph.
12. Writes `workflow.json` and `preparation.json`.
13. Stops before `/prompt` submission.

The current injection surface is:

| Input | Current source | I2V mapping | REF2VA mapping |
|---|---|---|---|
| Final Prompt | `prompt_service.prompt_scene_state()` | H3 node `7` | H3 node `8` |
| Scene audio | `scene_audio/<scene>/scene_audio.wav`, copied into ComfyUI input | `LoadAudio` node `6` | `LoadAudio` node `7` |
| Accepted keyframe | project-local keyframe copied into ComfyUI input | `LoadImage` node `5` | Not used |
| Ordered references | project-local entity references copied into ComfyUI input | Not used | H3 reference slots and loader nodes `50` through `58` |
| Frame count | `build_h3_timing_plan()` | H3 node `7`, input `length` | H3 node `8`, input `length` |
| FPS | H3 timing contract | `VHS_VideoCombine` node `20` | `VHS_VideoCombine` node `21` |
| Output prefix | deterministic project/scene prefix | output node `20` | output node `21` |
| Seed | frozen template setting | `RandomNoise` node `16` | `RandomNoise` node `17` |
| Sampler settings | frozen manifest/template settings | nodes `14` and `15` | nodes `15` and `16` |

The compiled workflow validation checks the seed, sampler, scheduler, denoise, dimensions, frame count, FPS, output format, audio path, and visual links. The compiler does not patch arbitrary workflow values. This narrow patch surface prevents donor-graph controls from becoming an accidental product API.

The current H3 templates freeze native 960x544 dimensions, 24 FPS, `res_multistep`, `simple` scheduling, 20 sampler steps, denoise `1`, H.264 MP4 output, and `save_metadata: false`. These values are production-contract inputs today, although the development plan still treats several quality choices as provisional until target qualification.

At submission time, `submit_render_job()` loads the prepared package, validates its fingerprint, checks live ComfyUI `/object_info`, creates a durable job, and patches the execution copy's output prefix with the Builder job ID. The preparation artifact remains unchanged. The submit and retry routes reject request fields, so callers cannot send a seed or workflow path.

### 1.5 Current seed handling

Both H3 templates use the same fixed seed:

```text
446059383552270
```

The value appears in:

- `backend/workflows.py` provisional settings for both H3 manifests;
- `workflows/h3_music_video_i2v_api.json`, node `16`, input `noise_seed`;
- `workflows/h3_music_video_ref2va_api.json`, node `17`, input `noise_seed`.

The current seed contract has four properties:

1. The manifest declares a seed mapping.
2. The template contains a fixed value.
3. Workflow validation requires the mapped `RandomNoise` node to keep that exact value.
4. Preparation compilation never patches it.

The seed does not exist as an explicit field in:

- `project.json`;
- `preparation.json`;
- the Builder job record;
- the retry request;
- the finalization record.

Seed provenance exists only indirectly through the workflow template SHA-256, manifest SHA-256, workflow identity, and preparation fingerprint. Changing the frozen template seed changes those hashes and invalidates preparation, but the persisted record does not say which seed produced the output.

A retry creates a new Builder job and ComfyUI prompt but resubmits the same preparation. It therefore uses the same seed. Retry represents recovery from a failed attempt, not a new visual candidate.

The current VHS output nodes set `save_metadata: false`. The Builder therefore does not request embedded workflow metadata in the raw MP4. Workflow and seed provenance must come from Builder-owned JSON records and the compiled `workflow.json`.

### 1.6 Output tracking

The H3 graph writes raw output to the active ComfyUI output root. The Builder does not copy raw H3 media into the project during normal job reconciliation.

Output ownership follows this path:

```text
prepared prefix:
  music_video_builder/<project_id>/<scene_id>/render

execution prefix:
  prepared prefix + Builder job_id

ComfyUI history entry
    -> expected prompt ID
    -> manifest output node
    -> deterministic job-owned prefix
    -> exactly one supported video file
    -> output identity and probe metadata
```

`render_jobs.py` stores the relative raw association in the job record. It checks the prompt-owned history entry, expected output node, expected prefix, supported extension, path safety, and output uniqueness. If history disappears, the fallback scans the job-owned prefix and requires an unambiguous file before recovery. Repeated absence eventually produces `ORPHANED`.

The WebSocket telemetry adapter reports node progress and stage labels. It does not determine job lifecycle or output success. HTTP `/queue` and `/history/{prompt_id}` remain authoritative.

## 2. Current generation lifecycle and metadata map

### 2.1 Lifecycle diagram

```text
Scene in project.json
    |
    | Visuals method + keyframe/references
    v
Prompt state
    |
    | current source fingerprint + relay fingerprint + Final Prompt
    v
Render preflight
    |
    | content, method, prompt, asset, timing, workflow, runtime checks
    v
Workflow preparation
    |
    | scene audio and visual inputs copied to ComfyUI input namespace
    | immutable template deep-copied and patched
    | workflow.json + preparation.json persisted
    v
ComfyUI submission
    |
    | Builder job JSON created
    | execution prefix receives job ID
    | /prompt called on loopback ComfyUI
    v
Generation job
    |
    | /queue, /history, and volatile WebSocket telemetry
    | retry, cancellation, reconciliation, output discovery
    v
Raw H3 output
    |
    | external ComfyUI output file + Builder output identity
    v
Final Scene
    |
    | ffprobe validation, exact timing, authoritative source audio remux
    | renders/<scene>/final/<job_id>.mp4 + finalization JSON
    v
Production Scene
    |
    | none alias, RTX VSR, or SeedVR2
    | production job + production/current.json
    v
Current Production Scene
```

### 2.2 Metadata locations

| Pipeline point | Location | Current metadata |
|---|---|---|
| Source scene | `projects/<project>/project.json` | Scene UUID, order, exact start/end/duration, lyric/instrumental source, cue numbers, scene text. |
| Visual conditioning | `project.json` under `visuals.scenes[]` | `generation_method`, keyframe prompt/descriptions/accepted asset, ordered reference selectors. |
| Storyboard context | `project.json` under `storyboard` | Scene action, camera, motion, continuity, entity IDs, required references, request fingerprint. |
| Prompt source | `project.json` under `prompts.scenes[]` | Method-specific Final Prompt, source fingerprint, relay fingerprint. The deterministic prompt compiler also exposes a non-persisted computed result. |
| Preflight | HTTP response from `/render/preflight` and frontend `builderState.renderPreflight` | Per-scene method, prompt status, input readiness, timing plan, preparation status, blockers, workflow readiness, runtime requirements, candidate preparation fingerprint. |
| Prepared scene audio | `scene_audio/<scene>/scene_audio.wav` and `renders/<scene>/inputs/scene_audio.wav` | Project-owned exact source segment. File identity is recorded in preparation metadata. |
| Prepared visual inputs | `renders/<scene>/inputs/` and active ComfyUI input namespace | Keyframe or reference files, queue names, identities, hashes, ownership checks. |
| Prepared workflow | `renders/<scene>/workflow.json` | Complete compiled ComfyUI API node map, including prompt, audio selector, visual links, timing, output prefix, fixed seed, sampler settings, and output encoding. |
| Preparation record | `renders/<scene>/preparation.json` | Preparation schema version, project/scene, method, preparation fingerprint, source audio identity, visual inputs, runtime assets, timing plan, workflow identity and contract validation, `queue_submitted: false`. Seed is not a first-class field. |
| Submission package | In-memory request to local `/prompt` | Execution-copy workflow with job-owned output prefix. The request is not persisted as a separate Builder record. |
| Render job | `renders/<scene>/jobs/<job_id>.json` | Job schema, job ID, method, preparation fingerprint, lifecycle state, timestamps, ComfyUI prompt ID, queue number, progress, errors, cancel/reconciliation data, retry lineage, output node/prefix contract, raw output association, and output discovery status. Seed/profile/candidate identity do not exist today. |
| ComfyUI lifecycle | ComfyUI `/queue` and `/history/<prompt_id>` | Queue membership, terminal status, prompt-correlated messages, output references, and ComfyUI execution diagnostics. The Builder reads these but does not own their retention. |
| Telemetry | In-memory `ComfyUITelemetryAdapter` snapshots | Node type, stage, progress, connection state, and stale indicators. Telemetry is not durable lifecycle state. |
| Raw H3 media | ComfyUI output directory | Generated raw video. The Builder stores a relative association and identity; it normally leaves the file in the ComfyUI output root. VHS metadata saving is disabled. |
| Finalization | `renders/<scene>/final/<job_id>.json` | Finalization state, finalization fingerprint, preparation fingerprint, target duration, raw output identity/probe, authoritative audio identity/probe, timing tolerances, encode policy, output identity, validation, failure, and prior valid output. |
| Final Scene | `renders/<scene>/final/<job_id>.mp4` | Project-owned H.264/AAC scene artifact with exact source audio policy. |
| Production job | `renders/<scene>/production/postprocess_jobs/<postprocess_job_id>.json` | Production method, production fingerprint, lifecycle, ComfyUI prompt ID, output, failure, cancellation, and transitions. |
| Production pointer | `renders/<scene>/production/current.json` | Current method, production fingerprint, postprocess job, output identity, validation, prior valid output, and failure. This is the existing scene-level promotion pattern. |
| Batch orchestration | `renders/batches/<batch_id>.json` | Fixed scene membership, action/disposition, production profile, runner state, pause/recovery attention, and item lineage. |
| Browser state | Module-global `builderState` in `web/extension.js` | Preflight, one displayed job per scene, postprocess state, batch state, polling timers, and UI drafts. No browser storage exists. Current job loading collapses multiple job records to one scene entry. |

### 2.3 Current lifecycle limitations

The current lifecycle supports historical attempts, but it does not model a user-facing attempt group:

- `JobStore` allows multiple terminal jobs for one scene.
- Only one active job may run for a scene at a time.
- Retry creates lineage but keeps the same preparation and seed.
- `finalize_render_job()` is addressed by job ID, so historical raw outputs can finalize independently.
- Production selects the newest `SUCCEEDED` job rather than an explicitly selected/finalized job.
- The frontend reduces the returned job list to one entry per scene and has no candidate gallery.
- The internal finalization temporary file named `.candidate.mp4` is a media-encoding scratch file, not a selectable Seed Hunter candidate.

## 3. Workflow extension capability

### 3.1 Alternate workflows

The manifest system can support alternate workflows, but the current registry needs a second identity dimension.

Today the key is only:

```text
(generation_method)
```

The feature needs at least:

```text
(generation_method, workflow_profile_id)
```

A profile should describe purpose and quality without creating fake Visuals methods. For example:

```text
keyframe_i2v + preview_draft_v1
keyframe_i2v + final_quality_v1
reference2video + preview_draft_v1
reference2video + final_quality_v1
```

The profile should remain manifest-owned. The user should choose Seed Hunter mode, not arbitrary node IDs, model paths, or raw workflow files.

The registry can remain a Python dictionary returning deep copies. The changes belong in its contract:

- Validate every declared profile, not an exact two-method set.
- Keep `generation_method` as the visual-conditioning method.
- Add `profile_id`, `purpose`, and `quality_tier`.
- Add profile-specific required nodes, models, settings, and runtime capability declarations.
- Add a seed policy and a patchable seed mapping.
- Add timing/audio/output rules.
- Add the artifact role: `preview_candidate` or `final_generation`.
- Include profile identity and manifest hash in preparation and attempt fingerprints.

Use separate immutable JSON templates when topology or model loading differs. Use one template with manifest-declared scalar settings only when the installed node contract proves that the setting is safe to patch. Do not build one generic graph with preview/final switches copied from a donor workflow.

### 3.2 Preview workflows

The safest first preview profile keeps the properties that matter for selecting motion and composition:

- same H3 conditioning method;
- same source audio segment;
- same prompt;
- same accepted keyframe or reference mapping;
- same 24 FPS;
- same `5 + 17n` frame-count contract;
- same scene timing coverage;
- a lower-cost, manifest-declared quality setting such as fewer sampler steps or another target-qualified profile.

The repository does not establish which lower-quality setting MiniMax H3 supports on the installed nodes. It must not invent numeric preview values before live qualification. Reducing frame count or changing audio timing would make candidate comparison unreliable. Reducing resolution also needs qualification because the current H3 templates use native 960x544 output and the final-selection experience may depend on composition at native geometry.

A preview profile may use the same raw H3 output shape as final quality while lowering compute through an approved step/model profile. If a future preview profile changes resolution or encoding, the manifest must mark the output as preview-only and the final path must re-render from metadata.

### 3.3 Candidate generation

The existing one-active-job-per-scene rule can support three candidates if the runner submits them sequentially:

```text
candidate 1 -> reconcile -> store preview -> candidate 2
candidate 2 -> reconcile -> store preview -> candidate 3
candidate 3 -> reconcile -> store preview -> candidate selection
```

Sequential execution is the recommended first implementation. It preserves the current scene lock, output prefix ownership, ComfyUI cancellation model, and restart recovery. A single workflow containing three H3 branches would increase memory pressure, complicate output association, and weaken failure isolation.

Each candidate needs an explicit seed. The attempt must record:

- candidate ID;
- candidate index;
- candidate-set ID;
- explicit seed;
- workflow profile ID;
- method;
- prompt/source fingerprint;
- preparation or generation-package fingerprint;
- Builder job ID;
- ComfyUI prompt ID;
- raw output identity;
- preview artifact identity;
- state and failure.

### 3.4 Final-generation promotion

The selected preview must not become the Final Scene. Seed Hunter should promote selection metadata, not low-quality media:

```text
selected candidate
    -> selected seed + candidate provenance
    -> final-quality generation package
    -> final H3 job
    -> Raw H3 output
    -> existing Final Scene finalization
    -> existing Production Scene pipeline
```

The final package should use the selected candidate's seed by default. It must re-read the current scene, prompt, visual assets, source audio, and storyboard fingerprint before submission. If any authoritative input changed after candidate generation, the candidate set becomes stale and the UI must request a new hunt.

The final H3 attempt should reference:

- `candidate_set_id`;
- `selected_candidate_id`;
- selected seed;
- selection fingerprint;
- final profile ID;
- current prompt and input fingerprints.

The final job remains a new job with its own ComfyUI prompt ID and output prefix. The old candidate jobs remain immutable history.

## 4. Seed Hunter integration proposal

## 4.1 Preview Mode

### User flow

1. The user opens a ready scene in the Renders workspace.
2. The user chooses `Preview Candidates`.
3. The backend freezes the current scene input basis:
   - generation method;
   - current Final Prompt and prompt fingerprint;
   - keyframe or ordered references;
   - exact scene audio identity and timing;
   - preview profile ID;
   - candidate count `3`.
4. The backend derives or allocates three explicit seeds.
5. The backend creates a candidate set and three candidate attempt records.
6. The runner prepares and submits candidates one at a time.
7. The backend reconciles each job and creates a project-owned preview artifact or a safe reference to a validated raw artifact.
8. The frontend displays three candidate cards as they become available.
9. The user selects one candidate.
10. The backend records a durable selection pointer and selection fingerprint.

### Seed policy

The first implementation should use explicit, reproducible seeds rather than ComfyUI's implicit random behavior.

Recommended policy:

```text
seed_basis =
    project_id
    + scene_id
    + prompt_source_fingerprint
    + visual/input fingerprint
    + preview profile ID
    + hunt nonce
    + candidate index

seed = stable_uint64(SHA-256(seed_basis))
```

The `hunt nonce` makes a new hunt intentional. Re-running a failed submission for the same candidate reuses its seed. Starting a new hunt increments or replaces the nonce and produces a new candidate set. The exact integer conversion and accepted range must match the installed `RandomNoise` node.

This policy gives the Builder reproducible seeds without exposing arbitrary workflow controls. The backend should persist the seed before submission and include it in the attempt fingerprint. The UI may show a short candidate label; it should not expose raw ComfyUI prompt IDs or node diagnostics.

Live qualification must confirm that changing only the seed produces useful candidate variation for both H3 methods and that reusing a seed produces acceptably repeatable output on the target hardware.

### Preview media policy

Candidate previews are not Final Scenes. They need a separate artifact role and validation policy.

Recommended options, in order:

1. Copy the validated raw video to a project-owned candidate-preview path and record its file identity.
2. If the raw container cannot serve reliably in the browser, run a bounded preview-only FFmpeg remux/encode into a project-owned candidate path.
3. Preserve exact source audio in the preview where practical, or clearly mark the preview audio as non-authoritative. Final Scene audio remains governed by `render_finalize.py`.

Do not expose arbitrary ComfyUI output paths through a browser route. A candidate media route must resolve a candidate record, verify the project root and file identity, and return only a Builder-owned file.

### Candidate states

Candidate set states could include:

```text
READY
RUNNING
PARTIAL
CANDIDATES_READY
SELECTION_REQUIRED
SELECTED
STALE
CANCELLED
FAILED
```

Candidate attempt states should reuse the existing render job lifecycle, with an explicit intent field:

```text
preview_candidate
final_generation
```

Do not overload `retry` to mean a new candidate. Retry keeps the same candidate ID and seed. A new candidate gets a new candidate ID and seed.

## 4.2 Final Mode

### User flow

1. The user selects one candidate.
2. The frontend requests `Generate Final`.
3. The backend verifies:
   - candidate set exists;
   - selected candidate exists and succeeded;
   - candidate set basis matches current scene inputs;
   - selected candidate belongs to the current method;
   - preview selection is not stale;
   - final profile requirements are available.
4. The backend creates a final-quality generation package using the selected seed.
5. The backend submits a new final-generation job.
6. ComfyUI produces a new raw H3 output.
7. Existing finalization validates the raw output and remuxes authoritative scene audio.
8. Existing Production Scene planning runs against the selected Final Scene.
9. A scene-level final pointer records the selected final job, rather than relying on newest-success ordering.

### Promotion semantics

Promotion must update a durable selection record. It must not mutate the candidate job, overwrite the preview, or copy preview media into `final/`.

A final scene becomes current only after:

- the final H3 job succeeds;
- raw output discovery passes;
- finalization passes media and timing validation;
- the final output identity is persisted;
- the selected-final pointer atomically references that job.

If final generation fails, the selected candidate remains selected and usable for retry. If the scene inputs change, the selection becomes stale and final generation must stop until the user runs a new preview hunt.

## 4.3 Required backend changes

### Candidate-set service

Add a backend service/store responsible for:

- creating candidate sets;
- deriving and validating seeds;
- freezing the input basis;
- creating three candidate attempts;
- running attempts sequentially;
- reconciling each job;
- materializing safe preview artifacts;
- recording selection and stale state;
- recovering after process restart;
- cancelling the active candidate without marking other completed candidates failed.

A candidate-set store should live under the project-owned render tree, for example:

```text
renders/<scene>/candidate_sets/<candidate_set_id>.json
renders/<scene>/candidate_sets/<candidate_set_id>/
    candidates/<candidate_id>.json
    previews/<candidate_id>.mp4
```

The exact directory names can follow existing `JobStore` conventions. The important property is immutable attempt history plus one mutable candidate-set selection record.

### Generation-package service

The current `preparation.json` represents one scene package and does not carry a seed. Candidate generation needs an immutable package per attempt or a separate profile-aware package layer.

Recommended split:

```text
renders/<scene>/preparation.json
    authoritative scene inputs, timing, prompt, visual assets, base readiness

renders/<scene>/generation_packages/<package_id>/
    package.json
    workflow.json
```

`package.json` should include:

- `package_schema_version`;
- project and scene IDs;
- candidate set and candidate IDs when applicable;
- `intent`;
- method and profile ID;
- explicit seed;
- source/prompt/visual/preparation fingerprints;
- timing plan;
- workflow identity, template hash, manifest hash;
- input asset identities;
- output contract;
- `queue_submitted: false` at preparation time.

This avoids replacing one shared `workflow.json` while three historical jobs refer to different seeds or profiles.

### Render job changes

Advance the job schema and add fields such as:

```json
{
  "intent": "preview_candidate",
  "candidate_set_id": "...",
  "candidate_id": "...",
  "candidate_index": 1,
  "workflow_profile_id": "preview_draft_v1",
  "seed": 123,
  "generation_package_id": "...",
  "selection_source_candidate_id": null
}
```

For a final job:

```json
{
  "intent": "final_generation",
  "candidate_set_id": "...",
  "selection_source_candidate_id": "...",
  "workflow_profile_id": "final_quality_v1",
  "seed": 123,
  "generation_package_id": "..."
}
```

Keep old job records readable. Do not infer a seed for historical jobs from the current template because their actual seed is not persisted as a first-class value.

Rename or alias the current `production_output` raw-H3 field in the next job schema. Its current name describes later production output even though it stores the raw H3 output node and prefix contract.

### Finalization and production changes

`render_finalize.py` can remain the Final Scene authority, but it should accept the new final-job metadata and record candidate provenance in finalization metadata.

`render_production.py` needs two changes:

1. Resolve the current Final Scene through an explicit selected-final pointer, not only the newest `SUCCEEDED` job.
2. Include the selected-final job and candidate provenance in the production fingerprint basis.

The existing `production/current.json` and atomic promotion logic provide a useful model for a `final/current.json` or scene-level selected-final record.

### Route changes

The current render routes can remain for ordinary one-shot rendering. Add a separate Seed Hunter contract so the existing manual path stays stable.

Possible endpoints:

```text
POST /projects/{project_id}/scenes/{scene_id}/render/candidates/preview
GET  /projects/{project_id}/scenes/{scene_id}/render/candidates
GET  /projects/{project_id}/scenes/{scene_id}/render/candidates/{candidate_id}
POST /projects/{project_id}/scenes/{scene_id}/render/candidates/{candidate_id}/select
POST /projects/{project_id}/scenes/{scene_id}/render/final/start
POST /projects/{project_id}/scenes/{scene_id}/render/candidates/cancel
GET  /projects/{project_id}/scenes/{scene_id}/render/candidates/{candidate_id}/media
```

The existing job routes should accept and return candidate metadata when a job belongs to a candidate set. A safe media route must validate project ownership, candidate ownership, file identity, supported extension, and current selection permissions.

Route helpers must use synchronous callables with `asyncio.to_thread` or await coroutines directly. The current postprocess route helper bug should be fixed before copying that pattern into Seed Hunter.

### Batch changes

Do not add Seed Hunter to the normal project batch in the first implementation. Three candidates per scene multiplies queue time, storage, and recovery states. Add a dedicated per-scene runner first.

Later, add an explicit batch mode such as:

```text
normal_final
preview_candidates
final_selected_candidates
```

The batch record must freeze candidate membership and selection basis. A newly selected candidate must not silently change a running batch.

## 4.4 Required frontend changes

`web/extension.js` currently keeps one displayed render job per scene and renders lifecycle/finalization controls. Seed Hunter needs a separate candidate view.

Add frontend state for:

```text
candidateSetsByScene
candidateAttemptsByScene
selectedCandidateByScene
candidateSelectionState
candidateMediaState
finalGenerationState
candidatePollingTimers
```

Add UI behavior:

- `Preview Candidates` button on an eligible scene card;
- three candidate cards with stable candidate numbers;
- per-candidate queued/running/succeeded/failed state;
- playable project-owned preview media;
- select button and selected marker;
- stale selection warning when prompt, method, keyframe, references, or scene inputs change;
- `Generate Final` button enabled only after a current candidate selection;
- final job and Final Scene progress separate from preview progress;
- retry candidate uses the same candidate seed;
- new candidate hunt creates a new candidate set;
- candidate cancellation leaves completed candidates visible;
- final-generation failure leaves the selection available for retry.

The UI must retain all candidate records returned by the backend. It must not use `Object.fromEntries` keyed only by scene ID for candidate jobs.

The frontend should display creative selection information, timing, and quality label. It should hide prompt IDs, node IDs, raw filesystem paths, and ComfyUI diagnostics through the existing presentation filters.

## 4.5 Required data-schema changes

### Project schema

The current schema-v7 project document has no generation-attempt or selected-final field. Add a schema-v8 generation section rather than placing a list of jobs in `project.json`.

A compact project-level pointer could look like:

```json
"generation": {
  "schema_version": 1,
  "scenes": [
    {
      "scene_id": "...",
      "candidate_set_id": "...",
      "selected_candidate_id": "...",
      "selected_final_job_id": "...",
      "selection_fingerprint": "..."
    }
  ]
}
```

Keep detailed candidate and job history under `renders/<scene>/`. The project document should hold only current pointers and selection provenance needed to resolve current state.

Migration rules must preserve schema-v1 through v7 projects. Missing generation state should normalize to empty pointers. Existing completed Final Scenes must remain readable and should not acquire a guessed candidate selection.

### Candidate-set schema

A candidate-set record should include:

```json
{
  "candidate_set_schema_version": 1,
  "candidate_set_id": "...",
  "project_id": "...",
  "scene_id": "...",
  "generation_method": "keyframe_i2v",
  "preview_profile_id": "preview_draft_v1",
  "candidate_count": 3,
  "hunt_nonce": "...",
  "input_basis": {
    "prompt_source_fingerprint": "...",
    "preparation_fingerprint": "...",
    "source_audio_identity": {...},
    "visual_input_identities": [...],
    "timing_plan": {...}
  },
  "candidate_ids": ["...", "...", "..."],
  "selected_candidate_id": null,
  "selection_fingerprint": null,
  "state": "RUNNING"
}
```

Each candidate record should include explicit seed, profile, package, job, raw output, preview output, state, and failure fields. Store SHA-256 and size for files. Store the candidate's input basis so stale selection checks do not depend on current mutable project state alone.

### Preparation and finalization schemas

- Preparation schema must become profile/attempt aware, or a new generation-package schema must own seed/profile fields.
- Job schema must become candidate/intent aware.
- Finalization metadata should record `intent: final_generation` and selected candidate provenance.
- Preview media needs a separate metadata schema and must never be mistaken for Final Scene.
- Production fingerprints must include the selected-final identity, not candidate preview identity.

## 4.6 Required workflow-manifest changes

Extend each H3 manifest with a profile section similar to:

```json
{
  "generation_method": "keyframe_i2v",
  "profile_id": "preview_draft_v1",
  "purpose": "preview_candidate",
  "workflow_id": "h3_music_video_i2v_preview_v1",
  "workflow_file": "h3_music_video_i2v_preview_api.json",
  "seed": {
    "node_id": "16",
    "input": "noise_seed",
    "node_type": "RandomNoise",
    "patch_policy": "explicit_uint64"
  },
  "quality": {
    "width": 960,
    "height": 544,
    "fps": 24,
    "steps": "profile_declared"
  },
  "timing_policy": "same_scene_5_plus_17n",
  "audio_policy": "exact_prepared_source_audio",
  "output_role": "preview_candidate"
}
```

The exact preview settings remain an open qualification item. Do not use the illustrative values above as a production contract.

The validator should enforce:

- seed patching only at the declared `RandomNoise` input;
- integer/range validation for seeds;
- no extra noise or randomization nodes;
- profile-specific dimensions and steps;
- unchanged source-audio links;
- unchanged H3 method topology;
- valid output node and artifact role;
- timing contract compliance;
- manifest/template hashes in the generation-package fingerprint.

Final profiles should retain the current final-quality contract and should not inherit preview-only settings through mutable global state.

## 5. Existing systems that can already support the feature

The repository already provides these useful building blocks:

### Manifest and compilation

- `backend/workflows.py` already centralizes workflow files, node IDs, model declarations, patch points, graph validation, and forbidden branches.
- `render.py` already deep-copies templates, hashes template and manifest identity, patches declared inputs, validates compiled workflows, and persists preparation artifacts.
- The seed mapping already exists structurally. The feature needs to change it from frozen to explicitly patchable under a controlled policy.

### Input and provenance

- Prompt source fingerprints detect changes to scene, storyboard, Visuals, and method inputs.
- Project-local asset identity checks cover audio, keyframes, and references.
- The H3 timing plan already supplies a stable frame count and exact source-audio boundary.
- Preparation fingerprints already provide a base for candidate-set invalidation.

### Job and output lifecycle

- `ComfyUIClient` already restricts communication to loopback endpoints.
- `JobStore` already supports multiple terminal attempts and retry lineage.
- Job IDs already create unique raw output prefixes.
- Queue/history reconciliation already associates output with prompt ID, output node, and prefix.
- Output discovery already rejects ambiguous, unsafe, and foreign files.
- Finalization already accepts a specific job ID and stores per-job Final Scene artifacts.
- The existing active-scene lock already supports sequential candidate attempts.

### Promotion and failure isolation

- `production/current.json` is a scene-level current pointer.
- `promote_production_scene()` copies validated candidates through a temporary file and atomically promotes them.
- Production failures preserve Final Scene and Raw H3 artifacts.
- Batch persistence and restart recovery provide patterns for a future candidate-set runner.

### Frontend and routes

- The Renders workspace already polls preflight, jobs, finalization, production, and batch status.
- The route layer already supports safe project-owned file responses for reference images.
- The frontend already separates render lifecycle from production post-processing.
- Prompt freshness and stale-state helpers can inform candidate invalidation.

These systems reduce the amount of new infrastructure. They do not eliminate the need for candidate-specific metadata or selection semantics.

## 6. Recommended implementation phases

### Phase A: Contract and hardening

Before feature work:

- Fix the async postprocess route helper and cancellation method mismatch.
- Add handler-level HTTP tests.
- Define preview quality, timing, audio, encoding, and retention contracts.
- Decide whether preview media uses authoritative source audio or an explicitly non-authoritative track.
- Confirm target ComfyUI seed input type and range.
- Establish target-NVIDIA test fixtures and live qualification procedure.

Deliverable: a signed feature contract with no implementation changes to the existing final path.

### Phase B: Manifest profiles and seed provenance

- Generalize the H3 manifest registry from method-only to method plus profile.
- Add preview and final profile declarations.
- Add manifest-owned seed patch points and validation.
- Add explicit seed/profile fields to generation-package and job records.
- Include seed/profile in attempt fingerprints.
- Keep the current fixed-seed one-shot path compatible for old jobs during migration.

Deliverable: deterministic dry compilation for one preview seed and one final seed per H3 method.

### Phase C: Candidate-set persistence and backend runner

- Add schema-v8 project pointers.
- Add candidate-set and candidate-record stores.
- Create immutable generation packages per candidate.
- Run three candidate attempts sequentially through existing ComfyUI lifecycle services.
- Add candidate preview artifact materialization and safe media serving.
- Add stale-selection checks, cancellation, retry, and restart recovery.

Deliverable: backend-only candidate set that can complete, recover, and select without Final Scene promotion.

### Phase D: Candidate selection UI

- Add candidate state to the frontend store.
- Render three candidate cards and safe media previews.
- Add selection, stale warning, new hunt, retry, and cancellation states.
- Keep all candidate jobs visible while hiding technical IDs.
- Keep ordinary one-shot Render Scene behavior unchanged.

Deliverable: a user can select a current candidate and see durable selection state after refresh.

### Phase E: Final generation from selection

- Add final-generation package creation from selected candidate seed/provenance.
- Submit a new final job under the final profile.
- Make Finalization consume only final-generation jobs.
- Add selected-final pointer and change Production Scene resolution to use it.
- Preserve selected candidate when final generation fails.

Deliverable: selected preview seed produces a new Final Scene and then the existing Production Scene.

### Phase F: Batch and operational integration

- Add explicit preview-candidate and final-selected batch modes only after per-scene behavior stabilizes.
- Freeze candidate sets and selections in batch membership.
- Add storage quotas, cleanup, and retention policy.
- Add browser and route integration tests.
- Add restart/reconciliation tests across candidate set, final job, and production job.

Deliverable: bounded, recoverable multi-scene Seed Hunter workflow.

### Phase G: Live qualification

Run on the target H3 environment for both methods and each profile:

- same-seed repeatability;
- seed-to-seed visual variation;
- three-candidate completion rate;
- preview-to-final visual continuity;
- exact source-audio timing;
- 24 FPS and `5 + 17n` coverage;
- raw output discovery;
- finalization and Production Scene validation;
- cancellation and restart recovery;
- memory, disk, and elapsed-time measurements.

Do not mark Seed Hunter ready from unit tests or structural manifest validation alone.

## 7. Risks and unknowns

### Runtime and model risks

- No current live H3 run proves that changing `RandomNoise.noise_seed` changes the visual result for either installed H3 method.
- Same-seed repeatability across AMD and NVIDIA hardware is unknown. Floating-point behavior, node versions, attention implementations, and model loaders may produce small or material differences.
- The repository does not establish a validated lower-quality H3 profile. Fewer steps, a different model, reduced resolution, or altered scheduler behavior may affect composition and motion in different ways.
- Preview quality may select a motion or composition that final quality does not reproduce. The final rerender must remain the source of truth.
- Model and node inventories, target GPU behavior, and installed ComfyUI output conventions remain unknown until live qualification.

### Lifecycle risks

- The current `SUCCEEDED` plus failed output-discovery state is ambiguous. Candidate orchestration must require a usable raw output, not state alone.
- Postprocess reconciliation lacks the render path's bounded absent-prompt/orphan policy. Seed Hunter should not copy that gap.
- The current production resolver chooses the newest successful job. Candidate selection requires an explicit selected-final pointer.
- Three attempts multiply queue time, disk usage, ComfyUI history dependence, and recovery states. Eighty scenes would produce 240 preview attempts before final rendering.
- Raw files remain in the external ComfyUI output root. Candidate retention and safe project-owned media serving need explicit disk and cleanup policy.
- A process restart between candidate completion and selection must preserve every candidate and the candidate-set pointer.

### Provenance and staleness risks

- A prompt edit, storyboard edit, Visuals change, reference replacement, keyframe replacement, source-audio replacement, or method switch must stale the candidate set.
- A selected candidate must not silently survive a changed preparation basis.
- A final job must record both selected-candidate provenance and the current final input fingerprints. Do not trust the candidate's old workflow as current.
- Historical jobs do not contain explicit seeds. Migration must not guess their seed or claim reproducibility.

### UI and API risks

- `web/extension.js` currently collapses jobs by scene and cannot display a candidate set without a new data shape.
- No safe render-media route exists for candidate previews.
- Browser polling currently focuses on active H3 jobs and can miss postprocess or batch-only activity. Candidate polling needs an explicit lifecycle.
- The existing route layer has no handler-level aiohttp tests. New candidate endpoints need real request/response coverage.
- Candidate media routes must prevent path traversal, symlink escape, foreign project access, and stale-file mismatch.

### Schema and migration risks

- Project schema-v7 validation is strict. A new generation pointer requires a migration or a carefully introduced optional field.
- Candidate history should not inflate `project.json`; detailed records belong under `renders/<scene>/`.
- New job/preparation schemas must keep old records readable while preventing old records from being treated as Seed Hunter attempts.
- Existing `.relay` reports and fixture schemas contain contradictory historical claims. They cannot act as a feature contract.

### Product risks

- Users may interpret a preview candidate as a final-quality deliverable. UI labels and media metadata must distinguish Preview Candidate, Raw H3, Final Scene, and Production Scene.
- Candidate selection can become an editorial decision that requires notes, ranking, or side-by-side playback later. The first schema should leave room for optional user notes without storing them in the prompt.
- Batch preview generation can consume significant time before a user sees a final scene. Keep per-scene Preview Mode separate until the interaction is proven.

## 8. Recommended decision

Build Seed Hunter as a profile-aware generation-attempt layer, not as a new Visuals method and not as an extension of retry.

The first safe slice is:

1. Define and qualify one lower-cost preview profile for each H3 method.
2. Add explicit seed and profile provenance to immutable generation packages and jobs.
3. Run three candidates sequentially under one candidate set.
4. Store a durable selection pointer and safe candidate media.
5. Re-render the selected seed through the final profile.
6. Route only the final-generation job into existing Final Scene and Production Scene services.

This preserves the current workflow-manifest boundary, ComfyUI job ownership, exact source-audio policy, and failure isolation while giving future feature work a durable place for candidate generation and final promotion.

## Evidence reviewed

- `backend/workflows.py`
- `workflows/h3_music_video_i2v_api.json`
- `workflows/h3_music_video_ref2va_api.json`
- `backend/render.py`
- `backend/render_jobs.py`
- `backend/render_finalize.py`
- `backend/render_production.py`
- `backend/render_batch.py`
- `backend/prompt_service.py`
- `backend/visuals.py`
- `backend/routes.py`
- `web/extension.js`
- `docs/ComfyUI Music Video Builder — Development Plan.md`
- Existing `PROJECT_CONTEXT.md` and `MEMORY.md`

Static inspection informed this assessment. No source code changed and no new generation feature was implemented.
