# Repository Memory

## Product boundary

- This repository is a private local ComfyUI custom-node extension for music-video scene production.
- The no-op `MusicVideoBuilder` node is only a ComfyUI launcher anchor. The web extension and `PromptServer` routes implement the product.
- Keep the stack plain: Python, plain JavaScript ES modules, plain CSS, ComfyUI APIs, local FFmpeg/FFprobe, and packages already present in the target ComfyUI environment.
- Do not add React, TypeScript, npm, a separate server, a database, cloud inference, OpenAI API calls, automatic model downloads, automatic node installation, generic T2V, frame interpolation, final song stitching, or Resolve automation.
- Do not modify ComfyUI core.

## Canonical source of truth

- `docs/ComfyUI Music Video Builder — Development Plan.md` defines the product constraints and phase contracts.
- `backend/workflows.py` is the only workflow manifest registry. Keep node IDs, patch points, model declarations, and graph invariants there.
- `backend/projects.py` owns schema-v7 project validation, normalization, path policy, and atomic JSON writes.
- Runtime projects and render records under `projects/` are ignored by Git. Treat them as evidence, not source fixtures, unless a test explicitly copies them.
- There is no dependency manifest. The target ComfyUI environment, not this repository, supplies ComfyUI, aiohttp, Pillow, websockets, and hardware packages.

## Project data contract

- A project uses schema version 7 and stores source metadata, scenes, characters, locations, Story Direction, storyboard, Visuals, prompts, and production settings in `project.json`.
- Project-owned directories are `source`, `references`, `keyframes`, `scene_audio`, `renders`, and `export`.
- Render jobs, finalization records, postprocess jobs, and batches have separate validated JSON records under `renders`.
- Writes should remain atomic. Preserve prior valid artifacts if replacement or post-processing fails.
- Source replacement resets scenes, storyboard, Visuals, and prompts.
- Keep entity/reference deletion guards and required Storyboard-to-Visuals reference synchronization intact.

## Pipeline invariants

1. Audio and SRT setup precede scene construction.
2. Scenes cover the source timeline contiguously. The policy is 2 to 10 seconds per scene.
3. Supported visual methods are exactly `keyframe_i2v` and `reference2video`.
4. REF2VA references remain ordered and capped at nine stills.
5. Storyboard and Prompt Director relay responses require current fingerprints and strict schemas.
6. The Prompt Director owns bounded creative prose only. Machine-owned timing, lyrics, tags, references, retention, and method structure stay in `prompt_service.py`.
7. Render preparation is dry. It may extract audio, copy assets, compile, validate, and persist a package, but it must not call `/prompt`.
8. H3 runs at 24 FPS on the `5 + 17n` frame grid. Exact source audio remains authoritative through Final Scene and Production Scene creation.
9. Raw H3, Final Scene, and Production Scene are separate artifacts. A production failure must not invalidate upstream files.
10. Durable `/queue` and `/history/{prompt_id}` reconciliation owns job state. WebSocket telemetry only decorates that state.
11. BatchRunner owns sequential dispatch and persists its state. The browser must not implement its own scheduler.
12. `none`, `rtx_vsr_fast`, and `seedvr2_quality` are the production methods. RTX VSR and SeedVR2 require runtime capability checks; SeedVR2 remains unqualified until live target-NVIDIA evidence exists.

## Key implementation map

- `backend/source.py`: upload limits, safe names, audio probing, SRT parsing, source replacement.
- `backend/scenes.py`: deterministic lyric/instrumental scene construction and exact timing.
- `backend/entities.py`: character/location/reference CRUD and image persistence.
- `backend/storyboard.py`: Story Direction modes, request fingerprints, strict relay validation.
- `backend/visuals.py`: keyframes, REF2VA selections, required-reference reconciliation, readiness.
- `backend/prompt_service.py`: deterministic H3 prompt compilation, relay request/response validation, stale/current state.
- `backend/gpt_launcher.py`: two allowlisted browser launches only.
- `backend/requirements.py`: manifest-driven node/model/tool scan with process-local successful-result caching.
- `backend/render.py`: preflight, H3 timing plan, input materialization, preparation fingerprints, template patching.
- `backend/render_jobs.py`: loopback-only `ComfyUIClient`, `JobStore`, submission, queue/history reconciliation, output ownership, cancellation, retry.
- `backend/render_telemetry.py`: volatile local WebSocket progress adapter.
- `backend/render_finalize.py`: raw output/media checks, authoritative-audio remux, Final Scene promotion.
- `backend/render_production.py`: postprocess manifests, production jobs, validation, promotion, failure isolation.
- `backend/render_batch.py`: `BatchStore`, `BatchRunner`, preview/start/pause/resume/end/retry/recovery.
- `backend/routes.py`: the 61-handler HTTP contract under `/music-video-builder/`.
- `web/extension.js`: one modal overlay, module-global state, REST/multipart calls, autosave, render and batch presentation.

## ComfyUI execution rules

- Only loopback ComfyUI API URLs are accepted. Allowed paths are `/prompt`, `/queue`, `/interrupt`, `/history`, and bounded `/history/{id}` paths.
- Queued cancellation deletes the owned prompt. Running cancellation uses `ComfyUIClient.interrupt_running(prompt_id)`, which targets the global interrupt endpoint with the owned prompt ID.
- Never use a caller-supplied prompt ID as a Builder job identity. Builder job IDs own output prefixes and persisted records.
- Never claim success from telemetry. Reconcile history and discover a job-owned output.
- Keep `preparation.json` and `workflow.json` as audit artifacts. A preparation record with `queue_submitted: false` has not rendered.
- Live object-info compatibility and runtime requirements remain separate checks from frozen-template validation.

## Frontend rules

- Use ComfyUI `api.fetchApi`; do not create a second server or use browser storage.
- Keep prompt drafts in memory and preserve them during refreshes and project transitions.
- Keep prompt save separate from general project autosave. A project Save must not mark a relay response current.
- Preserve user-facing diagnostic sanitization. Do not expose UUIDs, prompt IDs, node IDs, node types, raw tracebacks, or arbitrary server dictionaries in visible copy.
- Verify the existing ComfyUI web runtime after shell or plain-package changes. Client-plugin HMR only works when the existing `pnpm run dev:web` watcher is running from the DSH checkout; do not start a replacement server for this repository.

## Testing practice

- Tests use `unittest`, temporary project roots, fake ComfyUI clients, and fake media adapters.
- There are 48 test modules and 841 test methods by source count. That count is not a passing result.
- There is no route-level aiohttp test client, browser E2E suite, live H3 render proof, live RTX VSR proof, live SeedVR2 proof, or complete audio-to-Production-Scene proof.
- Current audit environment lacks `PIL`; the full discovery command ran 162 tests and stopped with 53 import errors. Run the target ComfyUI interpreter after restoring dependencies.
- Treat `.relay/result.json` and `.relay/test-output.txt` as historical evidence. Their 841-test and 728-test summaries conflict with each other and with the current environment.
- Add tests beside the phase that owns the contract. Prefer behavior tests over source-string assertions and test route handlers with an actual aiohttp request seam before trusting route fixes.

## Verified defects to address before relying on the UI

1. In `backend/routes.py`, postprocess `_do_submit` and `_do_retry` are `async def` functions passed to `asyncio.to_thread`. Make the worker callable synchronous or await it directly, then add handler-level tests.
2. In `backend/render_production.py`, replace the nonexistent `ComfyUIClient.interrupt` call with the real `interrupt_running` contract and remove the test masking that creates a fake nonexistent method.
3. Distinguish malformed JSON, wrong content type, missing body, and valid empty selection. Do not let malformed batch JSON mean “all ready scenes.”
4. Explicitly reject boolean Prompt Director response versions before integer comparison.
5. Make `_binary_status` inspect the subprocess return code before reporting a tool as available.
6. Give postprocess reconciliation the same bounded absent-prompt/orphan policy as render jobs.
7. Validate production promotion paths against the resolved project root and recompute stored output identities when checking currentness.
8. Scope batch systemic readiness checks to selected work where the contract allows it, and replace recursive immediate transitions with bounded iteration.
9. Cover unexpected source-save exceptions with rollback and decide whether cleanup failures should create recoverable orphan records.
10. Treat `SUCCEEDED` plus failed output discovery as unusable for finalization and production, or change the lifecycle state model so the ambiguity cannot escape.
11. Fix frontend status visibility, postprocess/batch polling, responsive render-heading layout, and the stale Phase 4 header.

## Current capability gaps

- No Resolve export or final song assembly exists. The `export` directory is only reserved storage.
- Live H3 qualification is blocked in recorded relay artifacts: the observed host was AMD Radeon RX 7900 XT, I2V hit Windows paging-file failure, and REF2VA hit HIP out-of-memory. Those results do not qualify or disqualify a target NVIDIA host.
- RTX VSR and SeedVR2 still need target-NVIDIA runtime and media validation.
- The persisted `RUNNING` job in the active project has no output record. Do not assume it is live without querying ComfyUI.

## Safe change sequence

1. Read the relevant phase section and module contract before editing.
2. Change the manifest and frozen workflow contract together when graph inputs or node IDs change.
3. Keep project storage injectable and use temporary roots in tests.
4. Test stale fingerprints, source-audio authority, failure isolation, path ownership, and malformed inputs.
5. Run targeted tests in the intended ComfyUI Python environment, then run the full suite.
6. Validate JavaScript syntax and inspect the existing ComfyUI GUI after frontend changes.
7. Use explicit live qualification for GPU, FFmpeg, browser relay, and export work. Do not infer live readiness from static tests or relay summaries.
