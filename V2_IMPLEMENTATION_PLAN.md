# V2.0/V2.1 Implementation Plan

## Status, scope, and release mapping

This document is a planning document. It does not implement features and does not authorize source changes.

`V2_ARCHITECTURE_PROPOSAL.md` remains the architectural source of truth. This plan covers only the requested implementation slices:

- **V2.0:** current lifecycle fixes, truthful status, recovery, and live qualification of the existing production core.
- **V2.1:** full-song draft assembly, timeline validation, assembly manifests, and CSV Resolve metadata export.

The architecture proposal places broader storyboard, prompt, candidate-generation, review, and technical-production work around these systems. This plan does not implement those systems. V2.1 consumes the current Final Scene and Production Scene records and leaves a reserved seam for future review decisions.

The Builder remains a focused scene-production tool. The plan does not add a video editor, Resolve automation, LUTs, colour tools, film grain, effects, creative finishing, local LLMs, transcription, Seed Hunter, storyboard expansion, prompt-system expansion, or new enhancement workflows.

## 1. Existing contracts that this plan must preserve

### 1.1 Source and timeline

- The artist/editor-provided SRT remains the timing and lyric authority.
- `backend/scenes.py` remains the owner of contiguous scene construction and exact millisecond boundaries.
- Scene IDs and canonical scene order come from the project scene list.
- Assembly does not infer timing from filenames, generated audio, waveform analysis, or media timestamps.
- Assembly cannot drop, stretch, reorder, or silently fill a scene.

### 1.2 H3 rendering

- `keyframe_i2v` and `reference2video` remain the only Visuals generation methods.
- `backend/workflows.py` remains the only H3 workflow manifest registry.
- `backend/render.py` remains a dry preparation boundary.
- `backend/render_jobs.py` remains the authority for ComfyUI queue/history reconciliation, cancellation, retry, output discovery, and durable H3 job state.
- `backend/render_finalize.py` remains the authority for Final Scene media validation and authoritative scene-audio remux.
- `backend/render_production.py` remains the authority for the existing technical Production Scene records.

### 1.3 Artifact ownership

```text
Raw H3 output
    -> Final Scene
    -> current Production Scene when selected
    -> draft assembly
    -> Resolve metadata
```

Preview or raw files cannot enter the draft assembly. A failed replacement cannot remove the previous valid Final Scene or Production Scene.

### 1.4 Frontend ownership

`web/extension.js` presents durable backend state. It does not own a scheduler, advance a worker, decide which scene output is current, or assemble a timeline in browser memory.

## 2. Release exit criteria

### V2.0 exit criteria

V2.0 can ship only when:

- ComfyUI-reported success without a usable output displays as an output-association failure and cannot finalize.
- Post-process start and retry execute a synchronous worker target through the background boundary.
- Post-process cancellation calls the correct ComfyUI client operation and records a terminal state.
- Malformed JSON cannot become an empty batch selection.
- Batch recovery cannot recurse without a bound or leave a `RUNNING` record without a live backend runner.
- Render and post-process status endpoints reconcile durable state before reporting terminal readiness.
- The Renders UI distinguishes active, terminal, output-missing, stale, cancelled, and failed states.
- Unit, route, frontend-contract, and restart-recovery tests pass in the intended environment.
- Live qualification records the current H3, Final Scene, and existing Production Scene behavior or records a concrete environment blocker.

### V2.1 exit criteria

V2.1 can ship only when:

- The Builder resolves one current Final Scene or Production Scene per scene by scene ID.
- Assembly preflight rejects missing outputs, stale outputs, gaps, overlaps, foreign paths, incompatible media, and master-audio mismatches.
- A durable assembly manifest records the exact scene order, timing, artifact identities, source identities, output policy, and validation result.
- The Builder creates a project-owned draft music video with the project master audio attached once.
- A changed scene, source, output policy, or selected production output stales the draft.
- The Builder exports deterministic `scenes.csv` metadata.
- The UI can preflight, start, observe, cancel, inspect, play, and download a draft without exposing editor controls.
- The documented CSV handoff supports a tested manual Resolve fixture, or the release records the exact external blocker.

## 3. Workstream order

| Order | Release | Workstream | Depends on |
| --- | --- | --- | --- |
| 0 | V2.0 | Freeze contracts, add regression fixtures, and reproduce known lifecycle defects. | Current source and test inventory. |
| 1 | V2.0 | Correct raw-output association and terminal render truth. | Existing `render_jobs.py` and `render_finalize.py`. |
| 2 | V2.0 | Correct post-process worker dispatch, cancellation, and reconciliation. | Existing production job store and ComfyUI client. |
| 3 | V2.0 | Correct batch request parsing, recovery bounds, and restart status. | Existing `render_batch.py` and route helpers. |
| 4 | V2.0 | Repair lifecycle presentation and polling. | Workstreams 1 through 3. |
| 5 | V2.0 | Complete live lifecycle qualification and publish release evidence. | V2.0 workstreams 1 through 4. |
| 6 | V2.1 | Add the assembly pointer migration and manifest storage contract. | V2.0 lifecycle truth. |
| 7 | V2.1 | Add the artifact resolver and timeline preflight. | Assembly schema and current Final/Production summaries. |
| 8 | V2.1 | Add the durable assembly runner and draft media output. | Passing preflight and media adapter tests. |
| 9 | V2.1 | Add CSV Resolve metadata export. | Successful immutable assembly manifest. |
| 10 | V2.1 | Add frontend assembly controls, route tests, live assembly qualification, and release documentation. | All preceding workstreams. |

The implementation should keep each workstream independently reviewable. Do not start V2.1 assembly while V2.0 can still report false success or lose output association.

# V2.0: Current lifecycle fixes

## 4. Feature V2.0-1: Render completion and output-association truth

### Affected files and modules

- `backend/render_jobs.py`
  - `JOB_SCHEMA_VERSION` and durable record validation.
  - `JobStore` load/save/list behavior.
  - `reconcile_project_jobs()` and raw output discovery.
  - `discover_raw_output()` and output identity checks.
  - job summary and currentness helpers.
- `backend/render_finalize.py`
  - `finalize_render_job()` admission checks.
  - `summarize_render_job_finalization()` and Final Scene currentness.
  - `MediaProbeAdapter` and final media validation seams.
- `backend/routes.py`
  - `GET /music-video-builder/projects/{project_id}/render/jobs`.
  - `POST /music-video-builder/projects/{project_id}/render/jobs/{job_id}/finalize`.
  - shared `render_job_error()` response mapping.
- `web/extension.js`
  - `renderJobIsActive()`.
  - `renderFinalizationLabel()`.
  - `loadRenderJobs()`.
  - `renderRenderState()` and job card presentation.
- Existing tests:
  - `tests/test_phase8b.py`.
  - `tests/test_phase8c.py` and relevant `tests/test_phase8c*.py`.
  - `tests/test_phase8c5.py` and `tests/test_phase8c8.py` for status presentation.
- New tests:
  - `tests/test_v20_render_lifecycle.py`.
  - `tests/test_v20_render_routes.py`.

### Required schema changes

Keep the project schema unchanged for this feature.

The current job schema already stores `output`, `output_discovery`, finalization information, and failure information. Prefer derived readiness over a new top-level job state:

```text
ComfyUI state = SUCCEEDED
output_discovery state = FAILED
usable raw output = false
finalization eligibility = blocked
```

Add a durable `output_association` object only if existing fields cannot express the distinction without ambiguity:

```json
{
  "state": "AVAILABLE|MISSING|AMBIGUOUS|STALE|UNREADABLE",
  "relative_path": null,
  "identity": null,
  "checked_at": "...",
  "failure": null
}
```

If the new object becomes necessary, bump the job record schema from `1` to `2`, accept version 1 on read, normalize missing `output_association` to `UNKNOWN`, and preserve historical records without claiming they are reproducible or current.

Do not add a project-level `current_job` pointer. Finalization and production records remain the currentness authorities for their artifact layers.

### New services and classes

Prefer small pure helpers over a second lifecycle service:

- `evaluate_raw_output_association(job, project_root)` in `backend/render_jobs.py`.
- `is_raw_output_usable(job)` in `backend/render_jobs.py`.
- `render_completion_summary(job)` in `backend/render_jobs.py`.
- An explicit `RawOutputUnavailable` or existing `RenderOutputDiscoveryError` response path in `backend/render_finalize.py`.

The helpers must distinguish:

- ComfyUI execution failure;
- ComfyUI cancellation or interruption;
- successful history with no Builder-owned output;
- output found but outside the project/output ownership policy;
- output found but unreadable or stale;
- usable raw output.

Do not create a new scheduler or copy queue/history reconciliation into finalization.

### API routes

Keep route paths stable.

`GET /music-video-builder/projects/{project_id}/render/jobs` should return a derived completion summary for each job. The response should make output association visible without exposing arbitrary filesystem paths.

`POST /music-video-builder/projects/{project_id}/render/jobs/{job_id}/finalize` must reject a successful ComfyUI history record when the Builder cannot prove a usable raw output. Return an actionable error code and details containing the job ID, scene ID, output-association state, and retry action.

Preserve existing HTTP classes:

- `400` for invalid IDs or malformed request shape;
- `404` for missing project/job;
- `409` for stale or not-current preparation;
- `422` for invalid render state;
- `502` for ComfyUI or media-tool failures.

### Frontend changes

Update the existing Renders surface rather than adding a new page.

- Show `Raw H3 output ready` only when a usable Builder-owned output exists.
- Show `ComfyUI succeeded, output unavailable` when history succeeded but discovery failed.
- Disable Finalize for output-missing, ambiguous, stale, or unreadable output.
- Show the output-association failure and the available retry or reconcile action.
- Keep raw ComfyUI prompt IDs and filesystem paths out of normal user copy.
- Keep historical successful records visible as historical, not current.
- Ensure polling does not stop merely because the ComfyUI state says `SUCCEEDED` when output discovery remains unresolved.

### Tests required

Backend unit tests:

- history reports `SUCCEEDED` with no output and finalization blocks;
- output discovery returns a project-owned readable file and finalization proceeds;
- output path traversal, symlink escape, foreign-project output, duplicate output, and stale identity fail;
- output association remains deterministic across repeated reconciliation;
- historical version-1 job records remain readable;
- finalization preserves the prior valid Final Scene when association or validation fails.

Route tests:

- `GET render/jobs` returns derived association state;
- `POST finalize` returns the correct status and error code;
- malformed IDs and missing jobs preserve existing error contracts.

Frontend contract tests:

- the Renders card uses output association rather than raw ComfyUI success;
- the Finalize action disables for unavailable output;
- the UI keeps one shared polling path.

Live qualification:

- submit a real H3 job;
- verify queue/history discovery;
- verify a real Final Scene promotion;
- remove or make the output unavailable and confirm the UI reports the blocker instead of false success.

### Migration concerns

- Do not guess output paths for old jobs.
- Treat old records without association metadata as `UNKNOWN` until reconciliation proves a current, owned, readable output.
- Do not invalidate existing Raw H3 files or Final Scenes during migration.
- Keep historical jobs inspectable even when their output cannot be recovered.
- Reconciliation must not overwrite a prior valid Final Scene with an unresolved result.

### Implementation order

1. Add pure association and completion helpers.
2. Add tests for success-without-output and unsafe output paths.
3. Gate finalization on the helper.
4. Add route response fields and error mapping.
5. Update Renders presentation and polling.
6. Run live queue/history/output qualification.

## 5. Feature V2.0-2: Post-process lifecycle, worker dispatch, and cancellation

### Affected files and modules

- `backend/routes.py`
  - post-process start handler around `/render/postprocess/start`.
  - post-process retry handler around `/render/postprocess/retry`.
  - cancellation handler around `/render/postprocess/cancel`.
  - status handler around `/render/postprocess`.
- `backend/render_production.py`
  - `PostprocessJobStore`.
  - `submit_postprocess_job()`.
  - `cancel_postprocess_job()`.
  - `retry_postprocess_job()`.
  - `reconcile_postprocess_job()`.
  - post-process state transitions and output promotion.
- `backend/render_jobs.py`
  - `ComfyUIClient.delete_queued()`.
  - `ComfyUIClient.interrupt_running()`.
  - cancellation capability semantics.
- `backend/render_finalize.py`
  - shared media-probe and authoritative-audio validation seams used during production promotion.
- `web/extension.js`
  - post-process status polling, cancel, retry, and production labels.
- Existing tests:
  - `tests/test_phase8e.py`.
  - `tests/test_phase8e1.py` through `tests/test_phase8e4.py`.
  - `tests/test_phase8e2a_capability_enforcement.py`.
  - `tests/test_phase8e2b_blocker_ux_and_seedvr2.py`.
- New tests:
  - `tests/test_v20_postprocess_lifecycle.py`.
  - `tests/test_v20_postprocess_routes.py`.

### Required schema changes

Keep project production settings unchanged. Production remains limited to the existing `upscale_method` field.

Keep the existing post-process record schema if the fix can use current state and failure fields. If reconciliation requires a durable worker marker, add optional fields such as:

```json
{
  "worker_state": "NOT_STARTED|RUNNING|STOP_REQUESTED|STOPPED|RECONCILING",
  "last_reconciled_at": "...",
  "completion": {
    "state": "PENDING|AVAILABLE|MISSING|INVALID",
    "failure": null
  }
}
```

Bump the post-process record schema only when a new persisted field is necessary. Normalize old records to `worker_state: UNKNOWN` and reconcile before reporting them as terminally usable.

Do not add look, LUT, colour, grain, or effects fields.

### New services and classes

Use the existing production service and add narrow seams:

- `PostprocessDispatch` or a synchronous internal dispatch helper that the route can pass to `asyncio.to_thread`.
- `PostprocessLifecycleSummary` as a pure status projection if the current summary functions cannot express worker and output association clearly.
- `PostprocessJobStore.reconcile_scene()` if reconciliation currently remains split between route and production code.

Correct the worker boundary instead of adding another production scheduler. The route must pass a synchronous callable to `asyncio.to_thread` or call the async boundary directly when the operation is genuinely asynchronous.

Use the existing `ComfyUIClient` methods with the correct operation:

- queued prompt: `delete_queued(prompt_id)`;
- running prompt: `interrupt_running(prompt_id)`.

### API routes

Keep these paths stable:

- `POST /music-video-builder/projects/{project_id}/scenes/{scene_id}/render/postprocess/start`;
- `POST /music-video-builder/projects/{project_id}/scenes/{scene_id}/render/postprocess/cancel`;
- `POST /music-video-builder/projects/{project_id}/scenes/{scene_id}/render/postprocess/retry`;
- `GET /music-video-builder/projects/{project_id}/scenes/{scene_id}/render/postprocess`;
- `GET /music-video-builder/projects/{project_id}/production/status`.

Start and retry must return a durable post-process job record, not a coroutine object or an unpersisted placeholder. Cancellation must return the terminal or current durable record after the targeted ComfyUI operation.

The status route should reconcile queue/history and local output state before it labels a job `SUCCEEDED` or a Production Scene `ready`.

### Frontend changes

- Keep the current scene-level Production Scene controls.
- Show `Starting`, `Submitted`, `Active`, `Succeeded`, `Failed`, `Cancelled`, `Output unavailable`, and `Stale` as separate states.
- Disable start while an active post-process job exists.
- Enable retry only when the backend reports a retryable failure and the Final Scene remains current.
- Show cancellation as a transition followed by reconciliation, not as an immediate browser-only state.
- Preserve the current Final Scene when production fails or cancels.
- Keep Production Scene controls limited to the existing technical methods.

### Tests required

Backend tests:

- start route persists a real post-process record and does not create an un-awaited coroutine;
- retry route persists a new or linked post-process attempt with correct source fingerprint;
- queued cancellation calls `delete_queued`;
- running cancellation calls `interrupt_running`;
- cancellation persists `CANCELLED` even when ComfyUI interruption reports an error, with the error retained as diagnostic data;
- post-process reconciliation handles missing queue/history records without leaving an active job forever;
- output promotion preserves the current Production Scene on failure;
- a stale Final Scene blocks start and retry;
- method `none` rejects post-process start.

Route tests:

- start/retry/cancel/status return durable JSON and correct HTTP statuses;
- invalid scene/project IDs remain `400`;
- missing project remains `404`;
- unavailable or unqualified methods remain actionable `422` responses.

Frontend tests:

- post-process polling observes backend status;
- cancellation does not remove the previous successful production output;
- output-unavailable state blocks the ready label;
- no browser-side scheduler advances post-process state.

Live qualification:

- start an approved production method;
- observe queue/history and local post-process records;
- cancel a queued operation;
- cancel a running operation where the target runtime supports it;
- verify a failed or cancelled operation leaves Final Scene playable and current.

### Migration concerns

- Do not reinterpret historical `SUCCEEDED` post-process records without output verification.
- Keep old post-process records readable and mark unknown worker state for reconciliation.
- Preserve existing Production Scene files and current pointers.
- Do not rerun old post-process jobs during migration.
- Do not change the meaning of `none`.

### Implementation order

1. Add fake-client cancellation tests.
2. Correct `interrupt_running` and queued deletion dispatch.
3. Correct route worker callables and retry dispatch.
4. Add reconciliation before status reporting.
5. Repair output-promotion failure isolation.
6. Update frontend labels and polling.
7. Run live qualification on the intended ComfyUI runtime.

## 6. Feature V2.0-3: Batch request validation and bounded recovery

### Affected files and modules

- `backend/routes.py`
  - `read_json()`.
  - `read_batch_selection()`.
  - `run_batch_operation()`.
  - batch preview/start/control handlers.
- `backend/render_batch.py`
  - `BatchStore` validation and persistence.
  - `BatchRunner.tick()`.
  - `_select_and_dispatch_next()`.
  - restart recovery and emergency pause.
  - transitions that currently call `self.tick()` recursively.
- `web/extension.js`
  - batch request helpers.
  - `loadRenderBatch()`.
  - batch pause, resume, end, and retry controls.
- Existing tests:
  - `tests/test_phase8d.py` and `tests/test_phase8d*.py`.
  - `tests/test_phase8e2a_capability_enforcement.py` for batch production gates.
- New tests:
  - `tests/test_v20_batch_boundaries.py`.
  - `tests/test_v20_batch_routes.py`.

### Required schema changes

Do not change project schema.

The current batch record already carries state, items, current index, attention, recovery, and timestamps. Add no field unless a test demonstrates that the bound needs durable evidence. If needed, add optional fields under a new batch schema version:

```json
{
  "runner_contract_version": 1,
  "last_transition_at": "...",
  "transition_count": 0
}
```

The batch validator must tolerate old records and derive missing values. It must reject an invalid transition count rather than looping through it.

### New services and classes

No new scheduler or runner class is required.

Add pure helpers if useful:

- `parse_json_body_or_error()` in `backend/routes.py` or a small route utility.
- `bounded_recovery_step()` in `backend/render_batch.py`.
- `batch_transition_budget(record)` for tests and diagnostics.

Refactor recursive `BatchRunner.tick()` transitions into a bounded loop inside one tick. The backend-owned runner remains the only scheduler.

### API routes

Keep the existing paths:

- `POST /music-video-builder/projects/{project_id}/render/batch/preview`;
- `POST /music-video-builder/projects/{project_id}/render/batch/start`;
- `GET /music-video-builder/projects/{project_id}/render/batch`;
- `POST /music-video-builder/projects/{project_id}/render/batch/pause`;
- `POST /music-video-builder/projects/{project_id}/render/batch/resume`;
- `POST /music-video-builder/projects/{project_id}/render/batch/end`;
- `POST /music-video-builder/projects/{project_id}/render/batch/retry-failed`.

Malformed JSON must return `400`. A malformed body cannot become `{}` and select every ready scene.

Batch controls that do not accept a body must reject malformed or non-empty request fields consistently. Batch status must report a paused-for-recovery state when no live backend runner owns a `RUNNING` record.

### Frontend changes

- Show malformed request errors as actionable input or transport errors.
- Keep the batch selection and control UI unchanged in scope.
- Show `Paused after restart` when the backend records recovery attention.
- Do not let frontend callbacks advance the current item.
- Stop polling when the backend reaches a terminal or attention state.
- Preserve the existing one-poll-path rule.

### Tests required

Backend tests:

- malformed JSON returns `400` for preview and start;
- valid empty body selects the documented default behavior;
- invalid body fields remain `400`;
- a batch with a missing active runner pauses for attention;
- a restart recovers an active item once and does not dispatch duplicate work;
- recursive transitions become bounded loop steps;
- a batch with many completed items cannot exceed the per-tick transition budget;
- unexpected runner errors persist a pause state;
- batch state survives process restart fixtures.

Frontend tests:

- batch controls call the existing routes;
- restart attention appears in the panel;
- no frontend recursion or scheduler exists;
- malformed-body errors do not clear the selection state as if the request succeeded.

### Migration concerns

- Preserve existing batch records and item dispositions.
- Do not reinterpret a historical `RUNNING` record as complete.
- On startup, mark a record with no live runner as paused for recovery rather than silently resuming.
- Do not change scene order or production profile semantics.
- Do not make assembly a batch item in V2.1; assembly remains its own service.

### Implementation order

1. Add malformed-body route tests.
2. Introduce a JSON parse error sentinel or equivalent route boundary.
3. Add the bounded transition loop.
4. Add restart and no-runner recovery tests.
5. Update status and frontend attention presentation.
6. Run the existing Phase 8D suite and live batch smoke tests.

## 7. Feature V2.0-4: Lifecycle presentation and qualification gate

### Affected files and modules

- `web/extension.js`
  - `builderState` render/job/post-process/batch state.
  - render and production status helpers.
  - shared polling functions.
  - Renders panel and scene card status.
- `web/builder.css`
  - status, blocker, stale, recovery, and output-association presentation.
- `backend/routes.py`
  - response shape consistency for render, post-process, production, and batch status.
- `backend/render_telemetry.py`
  - only if volatile telemetry currently masks durable lifecycle state.
- Existing tests:
  - `tests/test_phase8c5.py`.
  - `tests/test_phase8c8.py`.
  - `tests/test_phase8c9.py`.
  - `tests/test_phase8c10.py`.
  - relevant `tests/test_phase8d*.py` and `tests/test_phase8e2b.py`.
- New tests:
  - `tests/test_v20_lifecycle_frontend.py`.

### Required schema changes

No schema changes.

The frontend should consume derived backend status. It must not persist a second lifecycle record in browser storage or project JSON.

### New services and classes

No new backend service.

Add or consolidate pure presentation helpers in `web/extension.js`, such as:

- `renderLifecycleStatus(job)`;
- `renderOutputAssociationStatus(job)`;
- `renderRecoveryAttention(record)`;
- `isAssemblyEligibleScene(scene)`, reserved for V2.1 assembly readiness.

Do not move lifecycle decisions into those helpers. They should format backend decisions only.

### API routes

No new routes.

Ensure existing status payloads use stable fields and consistent error objects across:

- render jobs;
- Finalization;
- post-process jobs;
- Production Scene summary;
- batches.

### Frontend changes

- Use a single `builderState` lifecycle model.
- Preserve active scene and scroll position during polling.
- Show exact blocker text without raw Python tracebacks or internal prompt IDs.
- Show the difference between `current`, `historical`, `stale`, `blocked`, and `unavailable`.
- Keep production controls limited to technical methods.
- Add no assembly controls until V2.1 backend preflight exists.

### Tests required

- Source-contract tests assert required status labels and stable route paths.
- Frontend tests assert durable status drives actions.
- Polling tests assert one shared timer and no duplicate request loop.
- CSS tests assert blocker and recovery styles exist without exposing debug text.
- Manual checklist covers refresh during active H3, post-process, and batch work.

### Migration concerns

- Existing browser state may lack new derived fields; use safe defaults.
- Do not treat browser state as authority during a refresh.
- Do not clear valid current outputs when presentation fields are missing.

### Implementation order

1. Stabilize backend response semantics in V2.0-1 through V2.0-3.
2. Update frontend state normalization.
3. Update labels, actions, and polling.
4. Run frontend contract tests.
5. Perform GUI smoke tests against the existing ComfyUI page.

## 8. Feature V2.0-5: V2.0 live qualification and release evidence

### Affected files and modules

- No production source changes beyond V2.0-1 through V2.0-4.
- `tests/` for deterministic qualification helpers and fake adapters.
- `docs/` for a V2.0 qualification checklist or release note.
- Existing runtime records only as evidence; do not commit them as fixtures.

### Required schema changes

None.

Qualification results should live in an external report or relay artifact. Do not store machine-specific qualification claims in `project.json`.

### New services and classes

No new service.

Use existing fakes and seams:

- `ComfyUIClient` transport seams in `render_jobs.py`;
- media adapters in `render_finalize.py` and `phase8e_base.py`;
- `BatchRunner` seams in `phase8d_base.py`;
- post-process fakes in `phase8e_base.py`.

### API routes

No new routes.

Qualify existing route contracts through real HTTP requests against the intended PromptServer runtime where possible.

### Frontend changes

No product feature changes beyond V2.0 lifecycle presentation.

Record a manual checklist for:

- setup to scene construction;
- one H3 render;
- raw output discovery;
- Final Scene finalization;
- Production Scene none/upscale status where qualified;
- cancellation;
- batch restart;
- page refresh during each active state.

### Tests required

- Run targeted V2.0 tests in the intended ComfyUI Python environment.
- Run the full deterministic suite after target dependencies are available.
- Record missing Pillow, FFmpeg, FFprobe, GPU, or ComfyUI runtime dependencies as blockers rather than converting them into passing claims.
- Capture real `/queue` and `/history/{prompt_id}` behavior.
- Capture real output association and Final Scene promotion.

### Migration concerns

- Runtime evidence must not modify committed project fixtures.
- Do not infer live readiness from static tests or historical relay artifacts.
- Preserve the existing target-hardware qualification boundary for technical Production Scene methods.

### Implementation order

1. Complete V2.0-1 through V2.0-4.
2. Run deterministic tests.
3. Run live single-scene qualification.
4. Run live cancellation and restart checks.
5. Publish the release evidence and decide whether V2.1 assembly can begin.

# V2.1: Full-song draft assembly and Resolve metadata

## 9. Feature V2.1-1: Assembly project pointer and manifest storage contract

### Affected files and modules

- `backend/projects.py`
  - schema constants and field sets;
  - schema-v7 normalization into the new current schema;
  - project creation defaults;
  - source and scene replacement invalidation;
  - `ProjectStorage.save_project()` validation.
- New `backend/assembly.py`
  - assembly constants, validators, errors, pointer helpers, and stores.
- `backend/routes.py`
  - assembly status and preflight imports after the store exists.
- `web/extension.js`
  - assembly state defaults after project load and project switching.
- Existing tests:
  - `tests/test_phase1.py` for fixed directory ownership;
  - `tests/test_phase2.py` for source and scene replacement;
  - `tests/test_phase7c6.py` for schema-v7 additive boundaries;
  - `tests/test_phase8e.py` for production setting persistence.
- New tests:
  - `tests/test_v21_assembly_schema.py`.
  - `tests/test_v21_assembly_store.py`.

### Required schema changes

Add an additive project schema version for the assembly pointer. This plan does not add Seed Hunter, storyboard-v2, prompt-v2, or review-state fields.

Proposed fields:

```json
{
  "assembly": {
    "schema_version": 1,
    "current_assembly_id": null,
    "current_assembly_fingerprint": null
  }
}
```

Implementation changes in `backend/projects.py` should include:

- advance `SCHEMA_VERSION` from `7` to `8` when the migration is approved;
- define `ASSEMBLY_POINTER_FIELDS`;
- add `assembly` to the current project field set;
- validate a nullable canonical assembly UUID and a nullable 64-character fingerprint;
- normalize schema-v7 projects with an empty assembly pointer;
- initialize the pointer for new projects;
- clear or stale the pointer when source replacement or scene replacement changes the timeline;
- preserve the pointer when unrelated project fields save;
- retain the assembly manifest files even when the current pointer becomes stale.

The pointer identifies the latest assembly request. The assembly manifest remains the authority for state and output. A pointer does not make a failed or stale assembly current.

Do not add `review_decision_id` as a required project field in V2.1. The assembly manifest may reserve a nullable field for later review integration.

### New services and classes

In `backend/assembly.py` add:

- `AssemblyError` base class;
- `AssemblyValidationError` for invalid manifest or selection data;
- `AssemblyNotFoundError`;
- `AssemblyConflictError` for an active assembly;
- `AssemblyManifestStore` for atomic project-owned manifest records;
- `AssemblyPointerService` or small pointer helpers that update the project through `ProjectStorage`.

`AssemblyManifestStore` must validate project ownership, assembly ID, state, scene records, source identity, output policy, and path safety. It must not write the full manifest into `project.json`.

### API routes

Reserve these routes after the store exists:

- `GET /music-video-builder/projects/{project_id}/assembly` for current/latest assembly summary;
- `GET /music-video-builder/projects/{project_id}/assembly/{assembly_id}` for one manifest summary;
- `GET /music-video-builder/projects/{project_id}/assembly/{assembly_id}/manifest` for the validated manifest.

Register static paths such as `/assembly/preflight` before the dynamic `{assembly_id}` route.

### Frontend changes

- Add `assembly`, `assemblyLoading`, `assemblyBusy`, `assemblyPreflight`, and `assemblyPollTimer` to `builderState`.
- Reset assembly state when the active project changes.
- Preserve the latest manifest summary across harmless render polling.
- Do not treat an assembly pointer as a successful draft until the manifest state and output identity prove success.

### Tests required

- schema-v7 project loads into schema-v8 with an empty assembly pointer;
- new projects receive the pointer with null values;
- malformed assembly IDs and fingerprints fail strict validation;
- source replacement clears or stales the pointer while preserving prior manifests;
- scene replacement clears or stales the pointer;
- unrelated project saves preserve the pointer;
- atomic manifest writes survive replacement failure without losing the prior manifest;
- manifest ownership rejects a foreign project or scene;
- symlinked assembly directories and files fail path validation.

### Migration concerns

- Do not rewrite every existing project at install time. Normalize on load and write schema-v8 on the next valid project save, or use an explicit migration command later.
- Do not create assembly manifests for existing scene outputs automatically.
- Do not infer review approval from existing Final Scene or Production Scene files.
- Do not invalidate Final Scene, Production Scene, job, or batch records.
- Existing `export/` directories remain valid; create `renders/assemblies/` lazily.
- Existing schema-v7 tests must remain valid through the migration boundary.

### Implementation order

1. Define the assembly pointer and manifest validation contract.
2. Add schema-v7 normalization and schema-v8 tests.
3. Add `AssemblyManifestStore` with atomic writes and path checks.
4. Add pointer update/invalidation helpers.
5. Add read-only assembly status routes.
6. Add frontend state normalization.

## 10. Feature V2.1-2: Assembly artifact resolver

### Affected files and modules

- New `backend/assembly.py`.
- `backend/render_finalize.py`
  - public Final Scene summary/currentness functions;
  - final output identity and media metadata.
- `backend/render_production.py`
  - `summarize_production_scene()`;
  - production currentness and output identity;
  - selected technical method status.
- `backend/projects.py`
  - canonical scene order and source duration through `ProjectStorage.load_project()`.
- `backend/routes.py`
  - assembly preflight and start handlers.
- New tests:
  - `tests/test_v21_assembly_resolver.py`.

### Required schema changes

No additional project schema fields beyond the assembly pointer.

The manifest scene record must reserve these fields:

```json
{
  "order": 1,
  "scene_id": "...",
  "timeline_start_ms": 0,
  "timeline_end_ms": 6679,
  "artifact_kind": "production_scene|final_scene",
  "artifact_identity": {},
  "final_job_id": "...",
  "production_job_id": null,
  "review_decision_id": null,
  "scene_fingerprint": "..."
}
```

`review_decision_id` remains nullable because the full review service is outside this plan. V2.1 uses an explicit assembly source policy and current artifact checks.

### New services and classes

Add these pure, injectable components to `backend/assembly.py`:

- `AssemblySourcePolicy` with named policies such as:
  - `production_preferred_final_fallback`;
  - `final_only`.
- `AssemblyArtifactResolver`.
- `ResolvedAssemblyScene` value object or normalized mapping.
- `AssemblyResolutionError` with scene-specific details.

The resolver must:

1. read the canonical project scene list;
2. query current Final Scene and Production Scene summaries by scene ID;
3. apply the explicit source policy;
4. reject Raw H3, incomplete finalization, preview media, stale files, failed jobs, missing files, and ambiguous current outputs;
5. return one selected artifact per scene in project order;
6. include relative project-owned paths and file identities, never arbitrary external paths.

The resolver must not choose by filename, modification time, newest job, or folder sort.

### API routes

Add:

```text
GET /music-video-builder/projects/{project_id}/assembly/preflight
```

Accepted query values should be named and validated:

- `source_policy=production_preferred_final_fallback`;
- `source_policy=final_only`;
- `output_policy=draft_h264_aac_v1`.

The route returns:

- project and source duration;
- canonical scene order;
- selected artifact per scene when available;
- readiness and currentness;
- media summaries;
- blockers with scene ID, expected range, artifact kind, and next action;
- `can_assemble`.

The server must recompute all values when `/assembly/start` receives a request. The frontend cannot submit a preflight result as authority.

### Frontend changes

- Add source-policy and output-policy selectors with named options only.
- Show a scene-by-scene resolver table with selected artifact kind, duration, currentness, and blocker.
- Show whether the service selected Production Scene or Final Scene.
- Prevent `Build Draft` until `can_assemble` is true.
- Show the exact scene ID for every unresolved or incompatible output.
- Do not expose output filesystem paths or filename sorting controls.

### Tests required

Resolver unit tests:

- production-preferred policy selects current Production Scene;
- final fallback selects current Final Scene only when explicitly allowed;
- final-only policy rejects a production-only or missing final selection as defined by the policy;
- raw H3 output and preview paths are rejected;
- stale Final Scene and stale Production Scene are rejected;
- missing, duplicate, foreign-project, symlinked, and ambiguous outputs are rejected;
- canonical project order wins over filename order;
- every resolved scene retains exact source start and end values;
- nullable future review decision fields do not block V2.1.

Route tests:

- valid preflight returns `can_assemble` and scene blockers;
- invalid policy values return `400`;
- missing project returns `404`;
- invalid project or output state returns the correct product error.

### Migration concerns

- Existing currentness summaries must remain authoritative; do not create a second Final Scene resolver.
- Existing projects with no Production Scene must work under explicit Final Scene fallback.
- A scene with a historical output but no current record remains blocked.
- Do not convert filename order into scene order for legacy outputs.

### Implementation order

1. Define source and output policy enums.
2. Add resolver adapters over Finalization and Production summaries.
3. Add scene-specific blocker contracts.
4. Add preflight route.
5. Add frontend resolver table.
6. Run resolver and route tests before writing any draft media.

## 11. Feature V2.1-3: Timeline validation and assembly preflight

### Affected files and modules

- New `backend/assembly.py`.
- `backend/scenes.py` as a read-only source of the existing scene validation rules.
- `backend/source.py` for authoritative master-audio metadata and duration.
- `backend/render_finalize.py` for shared media-probe semantics.
- New `backend/assembly_media.py` for injectable media probing.
- `backend/routes.py` for preflight response mapping.
- New tests:
  - `tests/test_v21_timeline_validation.py`.
  - `tests/test_v21_media_contract.py`.

### Required schema changes

No project schema changes beyond the assembly pointer.

Add a manifest `timeline_validation` object:

```json
{
  "state": "PASS|BLOCKED",
  "source_duration_ms": 322840,
  "covered_duration_ms": 322840,
  "duration_tolerance_ms": 50,
  "scene_count": 80,
  "blockers": [],
  "checked_at": "..."
}
```

The duration tolerance must be a named assembly contract value, not a route-specific number. The initial value requires qualification against FFprobe and the selected draft output policy. Tests must use the same declared value.

### New services and classes

Add:

- `TimelineValidationError`;
- `AssemblyTimelineValidator`;
- `MediaContract` or `AssemblyOutputPolicy` value object;
- `AssemblyMediaProbe` protocol/interface;
- `FfprobeAssemblyMediaProbe` implementation in `backend/assembly_media.py`.

`AssemblyTimelineValidator` must validate:

- unique scene IDs;
- canonical scene order;
- first start at zero;
- no gap between adjacent expected scene ranges;
- no overlap between adjacent expected scene ranges;
- final end equals master-audio duration;
- one resolved artifact per scene;
- selected artifact media duration within the declared per-scene tolerance;
- compatible dimensions, FPS, video codec, pixel format, and audio policy;
- master-audio file identity and duration;
- project ownership and safe relative paths.

The validator reports all independent blockers in one preflight response. It must not stop after the first missing scene.

### API routes

Use the V2.1 preflight route:

```text
GET /music-video-builder/projects/{project_id}/assembly/preflight
```

A later `POST /assembly/preflight` is unnecessary unless the policy payload becomes too large for query parameters. Do not add a route that accepts arbitrary file paths or a client-supplied timeline.

### Frontend changes

- Render a timeline coverage summary with source duration, selected coverage, and tolerance.
- Render gap, overlap, missing, duration, media, and audio blockers by scene ID.
- Keep the Build button disabled until validation passes.
- Show a stale warning when a scene changes after preflight.
- Refresh preflight after render finalization, Production Scene promotion, source replacement, or project reload.
- Do not offer Close Gaps, Trim, Split, Overlay, or timeline-edit actions.

### Tests required

- contiguous project scenes pass validation;
- duplicate scene IDs fail;
- reordered resolved records fail or normalize to project order before validation;
- missing first scene fails start-at-zero coverage;
- gaps fail with both adjacent scene IDs;
- overlaps fail with both adjacent scene IDs;
- final scene ending before or after master duration fails;
- per-scene media duration below expected range fails;
- media duration within declared tolerance passes;
- mismatched dimensions, FPS, codecs, pixel format, or audio streams fail;
- master-audio identity mismatch fails;
- probe failure reports the scene and media path role without exposing unsafe paths;
- validator reports multiple blockers together;
- fake FFprobe output supports deterministic tests without FFmpeg installation.

### Migration concerns

- Existing project scene validation remains the first source-timeline gate.
- Assembly validation must not repair a project with gaps or overlaps.
- Do not change SRT or scene times to fit generated media.
- Do not use audio peaks, beat detection, or generated scene-audio lengths as replacement timing.
- Do not treat a legacy output with unknown FPS or duration as assembly-ready.

### Implementation order

1. Define media policy and tolerance constants.
2. Add fake probe fixtures and validator unit tests.
3. Add FFprobe adapter with safe path ownership.
4. Integrate resolver output into the validator.
5. Return preflight blockers through the route.
6. Add the frontend coverage and blocker presentation.

## 12. Feature V2.1-4: Assembly manifest and draft music video

### Affected files and modules

- New `backend/assembly.py`.
- New `backend/assembly_media.py`.
- `backend/projects.py` for the current assembly pointer.
- `backend/source.py` for project master-audio resolution.
- `backend/render_finalize.py` and `backend/render_production.py` for current artifact summaries only.
- `backend/routes.py` for start, status, cancel, and media routes.
- `web/extension.js` and `web/builder.css` for the assembly panel.
- New tests:
  - `tests/test_v21_assembly_service.py`.
  - `tests/test_v21_assembly_routes.py`.
  - `tests/test_v21_assembly_media.py`.

### Required schema changes

Add the assembly manifest schema outside `project.json`:

```text
projects/<project_id>/renders/assemblies/<assembly_id>/
├── assembly.json
├── draft_music_video.mp4
└── logs/
```

The manifest should contain:

```json
{
  "assembly_schema_version": 1,
  "assembly_id": "...",
  "project_id": "...",
  "state": "PREFLIGHT|READY|RUNNING|SUCCEEDED|FAILED|CANCELLED|STALE",
  "created_at": "...",
  "updated_at": "...",
  "completed_at": null,
  "source_policy_id": "production_preferred_final_fallback",
  "output_policy_id": "draft_h264_aac_v1",
  "source_audio": {
    "relative_path": "source/master_audio.wav",
    "identity": {},
    "duration_ms": 322840
  },
  "timeline_fingerprint": "...",
  "scenes": [],
  "timeline_validation": {},
  "output": null,
  "failure": null,
  "review_decision_id": null
}
```

The manifest is mutable only through validated state transitions. A successful manifest and its output identity become immutable. A new request creates a new assembly ID. A prior valid draft remains available when a new assembly fails or is cancelled.

The initial `draft_h264_aac_v1` policy should require compatible selected scene streams and attach the project master audio once. It must not hide scaling, cropping, frame-rate conversion, colour processing, or effects. If future output normalization becomes necessary, it requires a separately qualified named delivery policy outside this plan.

### New services and classes

Add to `backend/assembly.py`:

- `AssemblyRunner` for one backend-owned draft operation per project;
- `AssemblyService` for preflight, manifest creation, media execution, final validation, and promotion;
- `AssemblyStateTransition` validation;
- `AssemblyCancellation` or a runner cancellation event;
- `AssemblyArtifactResolver` integration.

Add to `backend/assembly_media.py`:

- `AssemblyMediaAdapter` protocol;
- `FfmpegAssemblyMediaAdapter`;
- `AssemblyMediaProcess` for a cancellable FFmpeg process;
- `AssemblyOutputValidator`.

The runner must not become an editor scheduler. It performs one bounded assembly operation, records state, and exits. The backend owns the worker so the browser cannot advance scene order.

The media adapter should:

1. receive the validated, ordered scene records;
2. create a project-owned temporary concat input or equivalent safe FFmpeg input;
3. concatenate the selected picture streams in scene-ID order;
4. remove scene audio from the concatenation path;
5. attach the project-owned master audio once;
6. write a temporary draft output;
7. probe and validate the temporary output;
8. atomically promote `draft_music_video.mp4` and output identity;
9. preserve the prior successful draft when any step fails.

Use project-owned temporary files and clean them after terminal success or failure. Preserve logs under the assembly directory without exposing them as the normal media route.

### API routes

Add these routes:

```text
GET  /music-video-builder/projects/{project_id}/assembly
GET  /music-video-builder/projects/{project_id}/assembly/preflight
POST /music-video-builder/projects/{project_id}/assembly/start
GET  /music-video-builder/projects/{project_id}/assembly/{assembly_id}
POST /music-video-builder/projects/{project_id}/assembly/{assembly_id}/cancel
GET  /music-video-builder/projects/{project_id}/assembly/{assembly_id}/manifest
GET  /music-video-builder/projects/{project_id}/assembly/{assembly_id}/media
```

Route behavior:

- `POST /assembly/start` reruns server-side preflight, creates a manifest, and returns `202` with the assembly ID.
- A second active assembly for the same project returns `409`.
- `GET /assembly` returns the latest manifest summary and current pointer status.
- `GET /assembly/{assembly_id}` returns normalized status, validation, output summary, and blockers.
- `POST /cancel` requests cancellation through the backend runner and returns the durable state.
- `GET /manifest` returns only a validated project-owned manifest.
- `GET /media` serves the successful draft through a record-resolved safe path. It must reject failed, stale, or foreign assembly IDs.

Do not accept client-provided scene paths, audio paths, concat lists, FFmpeg arguments, or arbitrary output names.

### Frontend changes

Add a compact Draft Assembly panel to the existing Renders view:

- source policy selector;
- output policy label;
- preflight action;
- coverage and blocker summary;
- ordered scene readiness list;
- Build Draft action;
- active assembly status and progress stage;
- Cancel action;
- completed draft video preview;
- Download Draft action;
- Open or download assembly manifest;
- Export `scenes.csv` action from the successful manifest.

The panel must:

- poll the backend assembly status through one timer;
- stop polling on terminal state;
- retain the prior successful draft while a replacement draft runs;
- show stale status after a source, scene, final output, production output, or output-policy change;
- show no timeline editing controls;
- never sort or select clips by filename in the browser.

### Tests required

Service tests:

- start creates a durable manifest only after passing preflight;
- active assembly conflicts are deterministic;
- scene records preserve canonical project order and exact timing;
- master audio identity is recorded once;
- successful media output promotes atomically;
- failed media assembly preserves the prior successful draft;
- cancellation records `CANCELLED` and removes only temporary work;
- restart recovery marks an interrupted runner for an explicit new assembly request;
- stale inputs block execution before FFmpeg starts;
- output validation rejects incomplete or incompatible draft media;
- successful manifests become immutable;
- a new assembly request creates new lineage.

Media adapter tests with fakes:

- concat input order follows scene order, not filename order;
- scene audio is not used as song audio;
- master audio is attached once;
- temporary output is not promoted before probe validation;
- FFmpeg failure persists a bounded failure record;
- cancellation stops the owned process and preserves the prior draft;
- path traversal and symlink checks fail before process launch.

Route tests:

- preflight blockers return `200` with `can_assemble: false`;
- start returns `202` and an assembly ID;
- active conflict returns `409`;
- status, manifest, cancel, and media routes enforce project and assembly ownership;
- media route rejects failed, stale, and missing outputs;
- malformed request bodies return `400`;
- download response has the expected content type and safe filename.

Frontend tests:

- assembly panel uses backend status and does not implement progression;
- build action requires a successful preflight;
- previous draft remains visible during replacement;
- terminal failures do not clear the prior successful draft;
- no editor, trim, split, overlay, transition, or audio-mixer controls appear.

### Migration concerns

- Existing completed scene outputs are not automatically assembled.
- Existing `export/` files are not treated as manifests.
- Existing Final Scene and Production Scene records remain untouched.
- A process restart during an assembly cannot leave a manifest reporting `RUNNING` without an owned worker. Mark it `FAILED` or `STALE` with an explicit recovery reason.
- Do not overwrite `draft_music_video.mp4` until the replacement passes final media validation.
- Clean only temporary files owned by the assembly ID.
- Retain failed manifests and logs for diagnostics, subject to a later documented cleanup policy.

### Implementation order

1. Implement manifest state transitions and store tests.
2. Implement resolver and timeline preflight integration.
3. Implement fake media adapter and atomic output promotion.
4. Implement the backend-owned assembly runner.
5. Add start/status/cancel/media routes.
6. Add frontend panel and polling.
7. Run small multi-scene assembly fixtures.
8. Run full-song assembly qualification on a completed project.

## 13. Feature V2.1-5: Resolve CSV metadata export

### Affected files and modules

- New `backend/assembly_export.py`.
- `backend/assembly.py` for manifest loading and export eligibility.
- `backend/routes.py` for the CSV response route.
- `web/extension.js` and `web/builder.css` for the export control and status.
- Existing `backend/projects.py` export path policy.
- New tests:
  - `tests/test_v21_assembly_export.py`.
  - `tests/test_v21_resolve_csv_routes.py`.
  - `tests/test_v21_resolve_csv_frontend.py`.

### Required schema changes

No additional project schema fields.

The CSV exporter reads the immutable assembly manifest. The manifest must retain enough data for stable export:

```text
scene_number
scene_id
start_ms
end_ms
source_path
final_path
production_path
selected_path
artifact_kind
generation_method
production_method
final_job_id
production_job_id
review_decision_id
status
scene_fingerprint
```

The exporter should also write an export metadata record beside the CSV:

```json
{
  "export_schema_version": 1,
  "format": "scenes_csv",
  "assembly_id": "...",
  "assembly_fingerprint": "...",
  "created_at": "...",
  "source_audio_identity": {},
  "row_count": 80,
  "path_policy": "project_relative"
}
```

Do not store generated CSV text in `project.json`.

### New services and classes

Add to `backend/assembly_export.py`:

- `AssemblyExportError`;
- `ResolveMetadataExporter` interface;
- `CsvTimelineExporter`;
- `AssemblyExportStore` or safe export-path helpers;
- `ExportEligibility` validator.

`CsvTimelineExporter` must:

- consume only a successful, current assembly manifest;
- preserve canonical scene order;
- use explicit UTF-8 and newline behavior;
- quote and escape values through the standard CSV library;
- emit project-relative paths with forward slashes;
- omit raw ComfyUI paths and machine-specific absolute paths;
- include stable IDs and exact millisecond values;
- record the assembly and manifest fingerprints;
- write the CSV and metadata atomically.

The exporter must not infer timeline data from project files after the manifest succeeds. The manifest is the export snapshot.

### API routes

Add:

```text
GET /music-video-builder/projects/{project_id}/assembly/{assembly_id}/export/scenes.csv
```

The route must:

- validate project and assembly IDs;
- load and validate the manifest;
- reject non-successful or stale assemblies;
- generate or load the project-owned CSV;
- return `text/csv; charset=utf-8` with a safe download name;
- return a normalized error when the export is stale or unavailable.

Do not add FCPXML, EDL, or Resolve automation routes in V2.1.

### Frontend changes

- Enable `Export scenes.csv` only for a successful current assembly.
- Show the assembly ID and export timestamp after download.
- Keep the manifest download available for inspection.
- Explain that CSV supplies metadata to Resolve and does not operate Resolve.
- Do not add automatic import, relink, launch, or project-mutation actions.

### Tests required

Exporter unit tests:

- rows follow canonical scene order;
- exact `start_ms` and `end_ms` values survive;
- IDs, paths, methods, job IDs, and status values come from the manifest;
- commas, quotes, Unicode, and newline characters in names are escaped correctly;
- project-relative paths use the declared separator policy;
- output is deterministic for the same manifest;
- stale or failed manifests cannot export;
- missing optional production or review fields serialize predictably;
- metadata fingerprint matches the source manifest;
- atomic replacement preserves a prior export when a write fails.

Route tests:

- successful export returns CSV content type and deterministic rows;
- stale, failed, missing, or foreign assemblies return the correct error;
- invalid IDs return `400`;
- missing project or assembly returns `404`;
- absolute-path leakage does not appear in the response.

Frontend tests:

- export action is disabled when assembly is not current and successful;
- the UI uses the assembly route rather than building CSV in JavaScript;
- no Resolve automation call exists;
- manifest and CSV controls remain separate.

Resolve qualification:

- open the CSV in a spreadsheet or text tool;
- verify scene order, IDs, exact ranges, and project-relative paths;
- test a documented manual Resolve relink workflow against one completed draft;
- record whether CSV supplies enough information for the intended editorial handoff.

### Migration concerns

- Existing projects have no CSV contract; do not generate one until a V2.1 assembly succeeds.
- Existing export files may have unknown provenance and must not be advertised as current metadata.
- A changed assembly manifest requires a new export.
- Do not promise FCPXML, EDL, or Resolve XML compatibility from CSV tests.
- Keep the format version in the metadata record so a later exporter can coexist with V2.1 output.

### Implementation order

1. Freeze the assembly manifest fields.
2. Add exporter and escaping tests.
3. Add safe export path and atomic write.
4. Add the CSV route.
5. Add the frontend download action.
6. Run manual Resolve handoff qualification.
7. Document CSV limitations and defer richer formats to a separate decision.

## 14. Feature V2.1-6: Assembly frontend integration and release gate

### Affected files and modules

- `web/extension.js`.
- `web/builder.css`.
- `backend/routes.py` response contracts.
- New frontend contract test file `tests/test_v21_assembly_frontend.py`.
- New route integration test file `tests/test_v21_assembly_integration.py`.
- Documentation under `docs/` for manual draft assembly and CSV handoff.

### Required schema changes

None beyond the assembly pointer and manifest schemas defined above.

Frontend state must remain ephemeral. It may cache the latest response for rendering, but the backend remains authoritative after every refresh.

### New services and classes

No new backend service.

Use the existing `fetchJson()` and shared operation guards. Add no browser worker, browser queue, or client-side manifest builder.

### API routes

The frontend uses only the V2.1 routes:

- assembly summary;
- assembly preflight;
- assembly start;
- assembly status;
- assembly cancel;
- assembly manifest;
- assembly media;
- CSV export.

The frontend must not call FFmpeg, read local project directories, or construct concat lists.

### Frontend changes

Integrate the assembly panel into the current Renders view instead of creating a second application.

Required states:

```text
No assembly
Preflight required
Blocked
Ready to build
Queued
Running
Succeeded
Failed
Cancelled
Stale
```

Required actions:

- Refresh Preflight;
- Build Draft;
- Cancel Draft;
- Open Draft;
- Download Draft;
- Open Manifest;
- Export scenes.csv.

The panel should display:

- source duration;
- selected source policy;
- selected output policy;
- scene count;
- covered duration;
- blocker count;
- current assembly ID;
- draft state;
- stale reason;
- export status.

The UI must retain useful render content when assembly status refreshes. It must not move, split, trim, or reorder scenes.

### Tests required

- render view includes assembly panel and controls;
- controls use exact route paths;
- Build Draft stays disabled until preflight passes;
- active status polls through one timer;
- failure and stale states retain prior draft information;
- CSV export is disabled for non-successful assemblies;
- the extension contains no local concat, FFmpeg, Resolve-launch, or editor timeline implementation.

### Migration concerns

- Projects without an assembly pointer show `No assembly`.
- Existing browser state does not create a phantom assembly.
- Refreshing the page reloads status from the backend.
- Existing render and Production Scene panels retain their current behavior.

### Implementation order

1. Add route helper functions and state defaults.
2. Add preflight panel and blocker rendering.
3. Add start/status/cancel actions.
4. Add draft media and manifest controls.
5. Add CSV export control.
6. Run frontend and HTTP integration tests.
7. Verify the existing ComfyUI GUI after refresh.

## 15. Cross-cutting migration and compatibility plan

### 15.1 Project schema migration

Use an additive schema migration:

```text
schema-v7 project
    -> normalize assembly pointer in memory
    -> validate current source/scenes/entities/storyboard/Visuals/prompts/production
    -> save as schema-v8 only on an explicit valid project write
```

The migration must not add Seed Hunter, storyboard expansion, prompt history, review decisions, or enhancement fields. Those systems remain deferred.

### 15.2 Render and production records

- Preserve existing H3 job records and finalization records.
- Add only the minimum optional lifecycle fields required by V2.0.
- Read old records without guessing missing output identities or technical provenance.
- Keep post-process records separate from H3 Render Jobs.
- Do not migrate raw output files into assembly directories.

### 15.3 Assembly records

- Create `renders/assemblies/` lazily.
- Give every manifest a UUID and schema version.
- Keep failed and cancelled manifests for diagnostics.
- Make successful manifests immutable.
- Treat a changed source, scene list, scene artifact, output policy, or master audio identity as a new assembly basis.
- Update the project assembly pointer atomically.

### 15.4 Path and security policy

All new routes must:

- validate canonical project and assembly IDs;
- resolve paths from project-owned records;
- reject traversal and symlinks;
- reject files outside the project root;
- recheck file identity before media serving or export;
- avoid raw filesystem paths in normal API responses;
- use safe response filenames.

### 15.5 Testing environment

The repository has no dependency manifest. Unit tests should use injectable project roots, fake ComfyUI transports, fake media probes, and fake FFmpeg adapters. Live checks must run in the intended ComfyUI environment with FFmpeg, FFprobe, Pillow, and the target hardware where required.

A missing dependency or unavailable GPU is a qualification blocker. It is not evidence of a passing implementation.

## 16. Complete implementation sequence

### V2.0

1. Freeze current route and record contracts.
2. Add V2.0 regression fixtures before changing behavior.
3. Correct raw output association and finalization gating.
4. Correct post-process worker dispatch and cancellation.
5. Correct malformed JSON handling and batch recovery bounds.
6. Normalize lifecycle status presentation and polling.
7. Run deterministic V2.0 tests.
8. Run live H3, Final Scene, Production Scene, cancellation, and restart qualification.
9. Approve the V2.1 assembly gate only after lifecycle truth passes.

### V2.1

1. Add schema-v8 assembly pointer normalization.
2. Add manifest store and state validation.
3. Add current Final/Production artifact resolver.
4. Add timeline and media preflight.
5. Add assembly runner and FFmpeg adapter.
6. Add draft output promotion and stale detection.
7. Add assembly routes and backend integration tests.
8. Add Renders assembly panel and polling.
9. Add immutable manifest loading and CSV exporter.
10. Add CSV route and frontend download.
11. Run full-song draft qualification.
12. Run manual Resolve metadata handoff qualification.
13. Publish limitations and defer FCPXML, EDL, and Resolve XML to a separate format-qualification decision.

## 17. Deferred work

This plan does not include:

- Seed Hunter or candidate-generation attempts;
- storyboard scene-card expansion or shot planning;
- prompt system changes, style bibles, prompt history, or continuity memory;
- visual candidate-generation systems;
- review and approval state machines beyond the assembly readiness boundary;
- new upscaling or enhancement workflows;
- LUTs, colour, grain, effects, or creative finishing;
- FCPXML, EDL, or Resolve XML implementation;
- Resolve automation or editor functionality.

The V2.1 assembly manifest leaves nullable provenance fields for future review integration, but it must not invent or implement that system in this plan.

## Sources and architectural basis

- `V2_ARCHITECTURE_PROPOSAL.md`.
- `PRODUCT_ROADMAP_ASSESSMENT.md`.
- `VIDEO_GENERATION_PIPELINE_ASSESSMENT.md`.
- `PROJECT_CONTEXT.md`.
- `MEMORY.md`.
- The existing ComfyUI Music Video Builder development plan under `docs/`.
- `backend/projects.py`.
- `backend/source.py`.
- `backend/scenes.py`.
- `backend/render.py`.
- `backend/render_jobs.py`.
- `backend/render_finalize.py`.
- `backend/render_production.py`.
- `backend/render_batch.py`.
- `backend/routes.py`.
- `web/extension.js`.
- existing Phase 8B, 8C, 8D, and 8E tests.

This plan changes no source code and does not replace the architectural source of truth.
