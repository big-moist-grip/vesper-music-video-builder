# Project Context

## Scope of this document

This document records the repository state observed during the ownership audit. It describes implementation that exists in the checkout. It separates verified behavior from claims in historical relay artifacts and marks unresolved facts as unknown.

## Project purpose

`ComfyUI-MusicVideoBuilder` is a private, Windows-oriented ComfyUI extension for preparing a music video as a set of timed scene generations.

The intended workflow is:

1. Import a master audio file and an SRT lyric file.
2. Build contiguous lyric and instrumental scenes with exact source timing.
3. Define project-local characters, locations, and still-image references.
4. Use a manual Storyboard Director relay to create a validated storyboard.
5. Choose a visual-conditioning method for each scene:
   - `keyframe_i2v`, the default keyframe/Image-to-Video path.
   - `reference2video`, the ordered still-reference path.
6. Prepare a method-specific prompt through a manual Prompt Director relay.
7. Run render preflight and prepare project-owned scene inputs.
8. Submit prepared API-format workflows to the local ComfyUI server.
9. Reconcile jobs, discover raw H3 output, and finalize a scene MP4 with the original scene audio.
10. Optionally create a Production Scene with no post-processing, RTX VSR, or SeedVR2.
11. Run selected scenes through the sequential batch runner.

The Builder does not assemble the complete song, automate DaVinci Resolve, call a cloud inference API, or expose a general ComfyUI workflow editor.

## Current repository snapshot

- Git branch: `main`.
- Git `HEAD`: `5f8e895` (`phase 8e2: integrate production upscale workflow`).
- Branch status at audit time: six commits ahead of `origin/main`.
- The only reported untracked item before this audit was `.relay.7z`.
- Current implementation reaches the Phase 8E.2C structural checkpoint according to the development plan and relay metadata.
- The repository has no `README` file.
- The repository has no `requirements.txt`, `pyproject.toml`, lockfile, Dockerfile, package manifest, CI configuration, or environment file.
- The current `python -m unittest discover -s tests` attempt in this environment ran 162 tests and failed with 53 errors because `PIL` was unavailable. The same command could not complete temporary-directory cleanup under the sandbox. Historical relay claims of 841 passing tests are not current verification.

## Repository structure

### Source and integration entry points

```text
__init__.py                         ComfyUI package entry point
nodes.py                            No-op launcher node
backend/routes.py                   PromptServer/aiohttp route registration
web/extension.js                    ComfyUI browser extension and Builder UI
web/prompt_state.js                 Prompt freshness/draft state helpers
web/gpt_launcher.js                 Frontend GPT URL/response helpers
web/builder.css                     Plain CSS for the modal Builder UI
workflows/*.json                    Frozen API-format production templates
docs/ComfyUI Music Video Builder — Development Plan.md
                                    Product definition, constraints, phase contracts
```

### Backend modules

```text
backend/projects.py                 Schema-v7 JSON persistence and path validation
backend/source.py                   Audio/SRT import, parsing, probing, replacement
backend/scenes.py                   Scene validation and deterministic construction
backend/entities.py                 Character/location/reference persistence
backend/storyboard.py               Story direction and storyboard relay contracts
backend/visuals.py                  Keyframe and REF2VA state, assets, readiness
backend/prompt_service.py           Deterministic H3 prompt compiler and relay validation
backend/gpt_launcher.py             Allowlisted system-browser GPT launch
backend/workflows.py                Manifest registry and workflow contract checks
backend/requirements.py              Runtime node/model/tool discovery and cache
backend/render.py                   Preflight, timing, preparation, workflow compilation
backend/render_jobs.py               ComfyUI client, job store, lifecycle, recovery
backend/render_telemetry.py         Volatile WebSocket progress overlay
backend/render_finalize.py           Raw H3 validation and Final Scene MP4 creation
backend/render_production.py         Production Scene post-processing
backend/render_batch.py              Sequential batch planning and execution
```

`backend` is a namespace package in this checkout. The root package imports its route module and node class when ComfyUI loads the extension.

### Tests and generated/runtime data

- `tests/` contains phase-oriented `unittest` modules, shared Phase 8D/8E fake adapters, and source-contract tests.
- `projects/` is ignored by Git and contains runtime project JSON, source media, references, keyframes, prepared scene inputs, render jobs, batch records, and any future final/production files.
- `state/` is ignored by Git and contains invalid-project ignore signatures.
- `.relay/` is ignored by Git and contains phase reports, qualification fixtures, workflows, scripts, logs, and runtime results.
- `.relay.7z` is a separate untracked archive and is not part of the production package.
- `__pycache__/` and `*.pyc` are ignored generated files.
- `.claude/settings.local.json` grants local development access to sibling workspaces, Downloads, the ComfyUI checkout, and the shared model directory. It is local tooling configuration, not application configuration.

The current runtime tree includes two project directories. The active schema-v7 project is `66c0cd13-2695-4199-8dea-d09655f58fbc`. The older `be245f77-6605-4d81-85a8-39ba798320d4` directory contains a schema-v2 test project and is retained as runtime evidence rather than tracked source.

## Technology stack

### Host and runtime

- Python backend using standard-library modules, Pillow, and ComfyUI-provided/runtime packages.
- Plain JavaScript ES modules and browser DOM APIs.
- Plain CSS.
- ComfyUI's supported custom-node and frontend extension interfaces.
- Windows is the documented development platform.
- Local loopback ComfyUI HTTP API, defaulting to `127.0.0.1:8188`.
- Local ComfyUI WebSocket telemetry through `websockets.sync.client` when that package is available beside ComfyUI.
- Local `ffmpeg` and `ffprobe` executables for audio preparation, media probing, and finalization.
- Pillow for image validation and image metadata handling.
- `aiohttp` and `server.PromptServer` are supplied by the ComfyUI process.

The repository does not pin versions. The presence of CPython 3.12 and 3.14 bytecode artifacts does not establish a supported Python version. The target ComfyUI environment remains the authoritative dependency environment.

### External models and node families

The production registry declares these H3 models:

- I2V diffusion model: `minimax_h3_fl2va_pruned_int8_convrot.safetensors`.
- REF2VA diffusion model: `minimax_h3_ref2va_pruned_int8_convrot.safetensors`.
- Text encoder: `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`.
- Video VAE: `minimax_h3_video_vae_fp16.safetensors`.
- Audio VAE: `minimax_h3_audio_vae_fp32.safetensors`.

The H3 graphs use MiniMax H3 nodes, ComfyUI loaders, LTX audio/video latent helper nodes, sampler nodes, and Video Helper Suite output nodes. The Builder uses only the stripped templates in `workflows/`; it does not embed the large donor graphs.

Production post-processing declares:

- RTX VSR: `RTXVideoSuperResolution`, exact 2x spatial scaling, and an NVIDIA CUDA/nvvfx runtime requirement.
- SeedVR2: `SeedVR2LoadDiTModel`, `SeedVR2LoadVAEModel`, and `SeedVR2VideoUpscaler`, with the frozen GGUF/VAE assets and CPU-offload settings in the manifest.

The manual Storyboard Director and Prompt Director are launched in the system browser through two allowlisted ChatGPT URLs. The Builder does not call OpenAI or another cloud inference API and does not send relay data programmatically.

## ComfyUI custom-node architecture

### Package load and node registration

ComfyUI imports the repository root package. `__init__.py`:

- imports `register_routes` from `backend.routes`;
- imports `MusicVideoBuilder` from `nodes.py`;
- exports `NODE_CLASS_MAPPINGS` with the key `MusicVideoBuilder`;
- exports the display label `Vesper Music Video Builder`;
- exposes `WEB_DIRECTORY = "./web"`;
- calls `register_routes()` as an import-time side effect.

`MusicVideoBuilder` is a no-op launcher node. It has no inputs, no outputs, category `Music Video`, and a `noop()` function that returns an empty tuple. It does not process audio, prompts, images, or render jobs. Its purpose is to give the frontend an identifiable ComfyUI node to decorate with an `Open Builder` button.

`backend.routes.register_routes()` uses a module-level `_routes_registered` flag and registers 61 `PromptServer.instance.routes` handlers once per Python process.

### Frontend registration

`web/extension.js` imports ComfyUI's `api` and `app` modules. It registers an extension named `music-video-builder.extension-shell`. On creation of a `MusicVideoBuilder` node it:

- applies the Builder colors;
- checks for an existing `Open Builder` widget;
- adds the widget once;
- opens the Builder modal when clicked.

The modal is one DOM overlay with Setup, Storyboard, Visuals, Prompts, and Renders tabs. CSS provides the dark responsive surface, dialogs, cards, status badges, and reduced-motion rules.

## Architecture and data flow

```text
ComfyUI loads root package
    -> no-op MusicVideoBuilder node + route registration + web directory
    -> frontend adds Open Builder widget

User audio + SRT
    -> multipart routes
    -> source.py validates names/content and probes audio with ffprobe
    -> projects/<project_id>/source/* + source metadata in project.json
    -> scenes.py parses cues, gaps, and long regions
    -> contiguous 2-10 second scene records

Characters/locations + local images
    -> entities.py validates metadata and Pillow-decodes images
    -> copied project-local reference files + entity metadata

Story Direction
    -> storyboard.py canonical request fingerprint
    -> manual Storyboard Director copy/paste
    -> strict response validation
    -> applied storyboard + synchronized required Visuals references

Visuals
    -> visual method per scene
    -> accepted keyframe OR ordered references (maximum 9 for REF2VA)
    -> readiness and reference mapping

Prompts
    -> deterministic prompt_service compiler and source fingerprint
    -> manual Prompt Director response
    -> strict JSON validation and machine-owned reassembly
    -> saved Final Prompt + relay provenance

Render preflight
    -> content, Visuals, prompt, source audio, workflow, timing, and runtime checks
    -> per-scene readiness and blockers

Render preparation
    -> exact scene audio extraction
    -> project asset copies into ComfyUI's input namespace
    -> deep copy and patch declared workflow inputs
    -> compiled workflow validation
    -> renders/<scene>/workflow.json + preparation.json + fingerprint

Render execution
    -> live object_info compatibility check
    -> durable Builder job record
    -> local ComfyUI /prompt
    -> /queue + /history reconciliation
    -> raw output discovery owned by job/prefix
    -> supplementary WebSocket telemetry

Final Scene
    -> raw media validation and frame-plan coverage check
    -> remux prepared source audio
    -> deterministic H.264/AAC MP4 and atomic promotion

Production Scene
    -> None alias OR RTX VSR/SeedVR2 postprocess workflow
    -> postprocess job/reconciliation
    -> authoritative Final Scene audio remux
    -> production validation and isolated promotion

BatchRunner
    -> canonical scene order, minimum-work action plan
    -> one active Builder batch/render path
    -> durable state, pause/resume/end/retry, restart recovery
```

### Source and scene construction

`source.py` accepts `.wav`, `.mp3`, `.flac`, `.m4a`, `.aac`, `.ogg`, and `.opus`. It strips path components from uploaded names, streams uploads through temporary files, validates SRT structure and timestamps, and uses `ffprobe` to obtain a positive duration in milliseconds. Replacing either source resets scenes, storyboard, Visuals, and prompts.

`scenes.py` creates contiguous scene ranges covering the source timeline. It records lyric cue provenance, instrumental regions, exact millisecond boundaries, split indexes, and split counts. The current policy requires each generated scene to be at least 2 seconds and at most 10 seconds. Source timing remains separate from the H3 frame plan.

### Entities and assets

`entities.py` persists characters and locations with stable UUIDs and role/description fields. PNG, JPEG, and WEBP references are copied beneath the project. Applied storyboard and Visuals selections prevent deletion of resources that still have references. Metadata is saved before later cleanup of removed files; cleanup failures are logged.

`visuals.py` keeps a schema-v7 entry for every current scene. `keyframe_i2v` requires a generation prompt, intended description, accepted keyframe, and actual image description. `reference2video` requires ordered project-local still references. Required Storyboard owner/reference selections are synchronized into Visuals, while optional references remain selectable up to nine stills.

### Storyboard and prompt relay

`storyboard.py` validates exact field sets, scene order, scene types, entity IDs, reference selectors, and the Story Direction mode. `band_performance` requires `performance` scene allocations. The route validates the complete response before mutating project state.

`prompt_service.py` assembles machine-owned structural facts and method-specific H3 sections. It keeps the lyric text, durations, reference tags, and ownership mapping machine-owned. The Prompt Director supplies only bounded creative prose. A current request fingerprint, matching relay response, and explicit user save are required before a prompt is render-ready. Prompt states are `needs_gpt`, `current`, `stale`, `unsaved`, or `error` at the UI boundary.

### Workflow manifests and graph communication

`backend/workflows.py` is the single manifest registry. Each render selects a method, loads its JSON template, deep-copies it, patches only declared manifest inputs, then validates node IDs, node types, links, models, settings, output format, and forbidden donor markers. Node connections use ComfyUI API-format references such as `["node_id", output_index]`.

The I2V template uses one accepted keyframe. The REF2VA template uses ordered `LoadImage` slots and maps selected pictures to `<Picture N>` and owning entities to `<Subject N>`. Both paths use the authoritative scene audio as the input for the source-audio latent strategy. H3-generated audio is not the final audio authority.

### Render timing and media ownership

The H3 timing contract is `minimax_h3_24fps_5_plus_17n_covering_v2`: 24 FPS and frame counts of `5 + 17n`, choosing the smallest count whose nominal duration covers the exact scene duration. For example, the prepared 6,679 ms scene uses 175 frames, about 7,291.667 ms nominal coverage, and a trim of about 612.667 ms.

`render.py` writes project-owned scene audio, ComfyUI input copies, `workflow.json`, and `preparation.json`. Preparation is a dry boundary. It never submits `/prompt`, runs H3, downloads models, or pretends that a render completed.

`render_jobs.py` owns the local HTTP client and durable lifecycle. `/queue` and `/history/<prompt_id>` remain lifecycle authority. WebSocket telemetry is volatile presentation data and cannot manufacture success or failure. Jobs use Builder-owned output prefixes and reject unsafe or ambiguous output association.

`render_finalize.py` rejects raw video that lacks sufficient coverage or valid streams, probes source and final media, remuxes the prepared scene audio, encodes a deterministic H.264/AAC MP4, and atomically promotes a Final Scene while preserving a prior valid output on replacement failure.

`render_production.py` keeps Production Scene metadata and postprocess jobs separate from Raw H3 and Final Scene. `none` points at the current Final Scene. Upscale failure or cancellation records a production failure without invalidating upstream artifacts.

### Batch orchestration

`render_batch.py` uses `BatchStore` for durable batch JSON and an active-batch pointer. `BatchRunner` processes canonical scene order sequentially and delegates scene work to the existing preparation, job, finalization, and production services. It supports preview, start, pause after current, resume, end, retry failed, and restart recovery. The frontend observes and requests transitions; it does not advance the runner itself.

## Persistence, files, and state

### Project JSON

`ProjectStorage` uses schema version 7. A normalized project contains:

- identity: `project_id`, `name`, `created_at`, `updated_at`;
- `source.master_audio` and `source.lyrics_srt` metadata;
- `scenes`;
- `characters` and `locations` with reference metadata;
- `story_direction`;
- `storyboard`;
- `visuals`;
- `prompts`;
- optional `production` settings.

Schema versions 1 through 6 have normalization paths. Validation is strict about field sets, UUIDs, timestamps, scene continuity, prompt fingerprints, references, and method enums.

Each new project receives this directory layout:

```text
projects/<project_id>/
├── project.json
├── source/
├── references/
│   ├── characters/<character_id>/
│   └── locations/<location_id>/
├── keyframes/
├── scene_audio/<scene_id>/
├── renders/
│   ├── <scene_id>/
│   │   ├── inputs/
│   │   ├── jobs/
│   │   ├── final/
│   │   └── production/
│   └── batches/
└── export/
```

`projects.py` writes JSON through a temporary file, flushes/fsyncs, retries Windows `PermissionError` briefly, and calls `os.replace`. Project roots default to hard-coded paths under `D:\User Folders\Documents\Projects\vesper-music-video-builder`; custom roots can be injected into `ProjectStorage` for tests.

Render job, finalization, production, and batch records live outside `project.json`. They are validated and written atomically by their own stores. There is no database.

### Frontend state

`web/extension.js` uses one module-global `builderState` object. It holds the active project, project lists, view, operation/transition flags, in-memory Visuals drafts, prompt drafts, relay payloads, expanded cards, render jobs, postprocess jobs, batch state, polling timers, and UI messages. The browser does not use localStorage or sessionStorage.

The UI uses ComfyUI's `api.fetchApi` for JSON requests and multipart uploads. It preserves dirty drafts during same-project refreshes, uses a 700 ms autosave for project edits, flushes saves before project transitions, and keeps prompt saves separate from general project durability. The frontend filters technical diagnostics before placing them in visible DOM text.

## HTTP/API surface

`backend/routes.py` exposes 61 handlers under `/music-video-builder/`.

- Health and runtime requirements: `/health`, `/requirements`.
- Project lifecycle: list/create/open/save/delete and invalid-project ignore.
- Source and scenes: audio upload, SRT upload, scene build.
- Resources: character/location CRUD, reference upload/read/delete.
- Storyboard: request, validate, apply, clear.
- Prompts: list, preview, save, relay request, relay response.
- Visuals: method selection, bulk method selection, keyframe prompt/details/upload/delete/image, REF2VA selection.
- Render: preflight, prepare, list jobs, submit, cancel, retry, finalize.
- Production: status, method selection, postprocess start/cancel/retry/status.
- Batch: preview, start, status, pause, resume, end, retry failed.
- GPT: `/gpt/{director}/open` for the two allowlisted browser launches.

Routes validate IDs and request shapes, call blocking backend work in worker threads in most cases, and map domain errors to product-facing HTTP responses. The route layer is the only server-to-browser contract; it does not define a separate web server.

## Feature status

### Implemented structurally

- ComfyUI extension shell and launcher node.
- Project creation, listing, loading, saving, deletion, legacy normalization, invalid-project reporting, and atomic JSON writes.
- Audio/SRT import, duration probing, cue parsing, gap detection, exact scene construction, and long-scene splitting.
- Character/location CRUD and project-local reference images.
- Story Direction and manual Storyboard Director relay.
- Visuals state for keyframe I2V and REF2VA, accepted keyframes, required-reference synchronization, and readiness diagnostics.
- Deterministic method-specific prompt compilation and manual Prompt Director relay with provenance/freshness enforcement.
- Manifest-driven H3 workflow validation for I2V and REF2VA.
- Runtime requirement discovery with process-local cache.
- Render preflight and dry media/workflow preparation.
- Local ComfyUI submission, durable render jobs, queue/history reconciliation, cancellation, retry, output discovery, and supplementary progress telemetry.
- Raw H3 finalization into a Final Scene with authoritative source audio.
- Production Scene lifecycle for `none`, RTX VSR, and SeedVR2, including failure isolation.
- Sequential batch planning, persistence, controls, retry semantics, and restart recovery.
- User-facing diagnostic sanitization at backend read/presentation boundaries and frontend presentation filters.

### Partial, deferred, or not implemented

- Live H3 output completion is not established in the current runtime artifacts.
- Live RTX VSR qualification is pending.
- Live SeedVR2 qualification is pending; the implementation marks it unqualified for batch use.
- A full successful single-scene chain from master audio through Production Scene is unknown.
- A complete mixed-method live batch is unknown.
- Resolve handoff/export is not implemented. The `export` directory exists, but there is no `scenes.csv` writer, final package builder, or Resolve automation.
- Phase 8F is named as the next step in the plan, but the plan does not define a Phase 8F contract section.
- The plan repeats persistent batch rendering as Phase 9 after Phase 8D already contains a persistent batch implementation.

### Current runtime artifacts

The active schema-v7 project `66c0cd13-2695-4199-8dea-d09655f58fbc` contains a 322,840 ms master audio file, a 78-cue SRT, and 80 scenes. The current project JSON contains storyboard, Visuals, prompt, and production state. The audit found many empty prompt records and a current REF2VA preparation for scene `002fc648-3024-472e-8753-895dec21c1c5`.

That preparation records:

- exact source audio bounds 0-6,679 ms;
- four ordered still references;
- 175 generated frames at 24 FPS;
- a valid 23-node compiled workflow from a 28-node template;
- `queue_submitted: false`, as expected for the preparation boundary.

The same scene has ten persisted render job records, including failed, orphaned, and a latest `RUNNING` record. They have no persisted raw output. The latest batch record is `ENDED` after restart recovery attention, with its first item failed as orphaned and its second item still pending. These records do not prove that the latest ComfyUI job is still live.

## Development conventions and constraints

The development plan is authoritative for product boundaries:

- Keep the tool local. Do not add cloud MiniMax, OpenAI API calls, cloud storage, accounts, authentication, analytics, or multi-user features.
- Keep the frontend as plain JavaScript ES modules, CSS, and DOM APIs. Do not add React, Vue, Svelte, Preact, TypeScript, npm, or a separate frontend server.
- Do not modify ComfyUI core. Use supported extension interfaces and inspect installed ComfyUI code before relying on version-sensitive APIs.
- Prefer standard-library Python. Do not add Python dependencies without approval unless the target ComfyUI environment already provides them.
- Keep production graphs narrow. Do not copy donor UI groups, convenience switches, LTX refinement, interpolation, LoRA, T2V, or video-reference branches into the Builder.
- Keep workflow node IDs and patch points in the manifest registry. Do not scatter them through backend code.
- Preparation must remain a dry boundary. Only the execution service may submit `/prompt`.
- Preserve exact source-audio authority through finalization and post-processing.
- Keep Raw H3, Final Scene, and Production Scene separate. Production failure must not delete or invalidate upstream artifacts.
- Keep lifecycle truth in durable HTTP queue/history reconciliation. Treat WebSocket telemetry as supplementary.
- Preserve user drafts and current/stale prompt provenance. General project Save must not stamp a prompt as render-ready.
- Use `unittest` for deterministic behavior. Reserve live ComfyUI, GPU, browser, and Resolve checks for explicit manual qualification.
- Do not treat relay summaries as current test evidence without matching the checkout and command output.

## Known issues and technical debt

The following issues were verified by source inspection or by comparing code with its tests. They are not fixed by this audit.

### Route and execution defects

1. `backend/routes.py` defines postprocess `_do_submit` and `_do_retry` as `async def` but passes them to `asyncio.to_thread`. `to_thread` invokes the function as a normal callable, so it receives a coroutine object. The handlers then pass that object to `web.json_response` instead of running the postprocess operation. No aiohttp route test catches this.
2. `render_production.cancel_postprocess_job` calls `comfy.interrupt(prompt_id)`, while the real `ComfyUIClient` exposes `interrupt_running(prompt_id)`. The broad exception handler hides the missing method and still records the postprocess as cancelled. The integration test creates a fake `interrupt` attribute with `create=True`, masking the mismatch.
3. `routes.read_json` maps missing bodies, wrong content types, malformed JSON, and decode errors to the same `None`. `read_batch_selection` treats `None` as `{}`, so malformed batch preview/start JSON can select all ready scenes.

### Validation and durability gaps

4. `_decode_prompt_relay_response` compares the response version with integer `1` without explicitly rejecting booleans. Python treats `True == 1`, so a JSON boolean can pass the version check before later validation.
5. `requirements._binary_status` reports an executable as `AVAILABLE` even when its `-version` subprocess returns a nonzero exit code.
6. Postprocess reconciliation leaves an active postprocess job active when its prompt is absent from both queue and history. Unlike the render-job path, it has no bounded absence/orphan transition.
7. `promote_production_scene` accepts a caller-supplied relative path without a direct absolute/parent traversal check. Current internal callers use fixed paths, but the public helper does not enforce the same ownership boundary. Production currentness also trusts stored output metadata instead of recomputing the file identity on every read.
8. Batch systemic checks use the project-wide preflight/runtime aggregate before dispatch. A selected-scene batch can be blocked by an unrelated unselected scene. Immediate batch transitions recurse through `tick()`, with no explicit iterative bound for large batches.
9. Source replacement restores files for declared validation, persistence, and OS failures, but an unexpected exception from `save_project` is not covered by the rollback clauses. Entity/reference/keyframe metadata saves happen before later file cleanup; cleanup failures only log warnings and can leave orphan files.
10. A successful ComfyUI history entry with no discoverable output leaves a render job in `SUCCEEDED` while `output_discovery.state` is `FAILED`. Downstream code must inspect both fields or it can treat an unusable job as complete.

### Frontend and UI fragility

11. `updateStatus` hides every non-error status. The health check sets `Checking…` and `Connected`, but the health banner remains hidden for those states.
12. Render polling schedules only while an H3 job is active. Postprocess, finalization, or batch-only activity can stop updating in the UI until a refresh.
13. Responsive CSS applies grid rules to `.mvb-render-heading`, whose base declaration is flex, so the responsive layout contract is inconsistent.
14. The modal header still displays `Phase 4 · Storyboard relay` while the UI exposes Phase 8 production and batch controls.

### Operational and process debt

15. The default project/state roots are hard-coded to this workstation's path. The `ProjectStorage` injection seam makes tests portable, but a clone or installation at another location does not automatically follow its own repository.
16. Automated tests contain no aiohttp end-to-end route execution, no browser E2E suite, and no proof of a live H3, RTX VSR, SeedVR2, or full audio-to-Production-Scene chain. Many UI tests assert source strings, and ComfyUI behavior uses fakes.
17. Test and relay evidence is contradictory: `.relay/result.json` claims 841 passing tests, `.relay/test-output.txt` contains an older 728-test summary, and the current environment lacks Pillow and cannot complete the suite.
18. Phase 8D fixtures use preparation schema v2 in places where Phase 8 and 8C contracts require v3. The mismatch can let tests exercise an obsolete artifact shape.
19. SeedVR2 implementation and tests freeze NVENC output settings while other documentation describes broader runtime semantics; live NVIDIA qualification is still pending.
20. The project plan has overlapping phase definitions for persistent batch rendering and does not define the named Phase 8F contract.

## Error handling model

- Domain modules use typed exceptions such as `ProjectValidationError`, `ProjectNotFoundError`, `ProjectPersistenceError`, `RenderPreparationError`, `RenderJobError`, `RenderFinalizationError`, and `RenderProductionError`.
- Routes convert expected exceptions into stable JSON error codes/messages and log persistence failures.
- JSON and media writes use temporary files plus atomic replacement where the module owns the boundary.
- Render and production stores validate persisted records before returning them.
- User-facing sanitization removes prompt IDs, node IDs, node types, timestamps, traceback text, raw dictionary representations, and execution internals from visible messages. The underlying forensic JSON is not rewritten by presentation sanitization.
- WebSocket disconnects and stale events do not change durable job state. HTTP queue/history reconciliation remains authoritative.

## Unknowns requiring future verification

These facts were not resolved by static inspection or the available runtime records:

- Whether the persisted `RUNNING` ComfyUI prompt is still executing.
- Current installed ComfyUI node definitions, model visibility, and runtime API behavior.
- Whether a complete I2V or REF2VA render can finish on the target NVIDIA host.
- Whether RTX VSR and SeedVR2 produce validated 1920x1088 outputs on the target host.
- Actual render quality, performance, VRAM use, and output timing on either documented GPU.
- Whether system-browser GPT launch, clipboard behavior, and manual relay quality work in the current ComfyUI browser.
- Whether FFmpeg and Pillow are installed in the intended ComfyUI runtime. Pillow is absent from the interpreter used for this audit.
- Whether the older schema-v2 runtime project is intentionally retained, ignored, or should be migrated.
- Whether the Phase 8F Resolve handoff contract exists outside this checkout.

## Evidence reviewed

- All tracked source modules, tests, workflow templates, and the development plan.
- `.gitignore`, `.claude/settings.local.json`, runtime project/state JSON, preparation/workflow/job/batch records, and selected `.relay` qualification/result artifacts.
- Git status and recent commit history.
- Current local test command and its dependency failure.

This audit created no implementation changes. The only intended new files are `PROJECT_CONTEXT.md` and `MEMORY.md`.
