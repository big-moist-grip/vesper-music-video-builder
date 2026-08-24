# Current Implementation Baseline

## Report purpose

This report records the repository state before V2.0 implementation begins.

The report captures the source tree, Git state, test status, current schema, current architecture, documented issues, and the gap between the approved planning documents and the source code.

The report itself is a documentation artifact. It does not modify source code or claim that planned V2 features exist.

## Capture context

- Working directory: `D:\User Folders\Documents\Projects\vesper-music-video-builder`
- Final capture timestamp: `2026-08-25T00:08:43+09:30`
- Initial pre-report capture timestamp: `2026-08-24T23:55:27+09:30`
- Python executable: `C:\Python314\python.exe`
- Python version: `3.14.6`
- The baseline describes the repository before V2.0 implementation. It records both the initial pre-report snapshot and the final Git state after the repository documentation commit described below.

# 1. Git status

## Current branch and commit

At the final capture:

- Branch: `main`
- Upstream relation: `main...origin/main` with no ahead or behind count;
- HEAD: `822655a9063802e63d026bd6923f0d2c0115a1b5`;
- Short HEAD: `822655a`;
- HEAD commit date: `2026-08-24T23:55:33+09:30`;
- HEAD author: `Vesper Builder Relay`;
- HEAD subject: `Update project files`.

## Current working tree

At the final capture:

- staged tracked changes: none;
- unstaged tracked changes: none;
- tracked diff names: none;
- untracked files: `CURRENT_IMPLEMENTATION_BASELINE.md` only.

## Pre-report Git observation

The initial observation before this report was written recorded:

- HEAD `5f8e895a8268d50d8a4de24beb21e4b85c81ca8c`;
- branch `main...origin/main [ahead 6]`;
- no staged or unstaged tracked changes;
- 11 untracked planning, memory, assessment, and relay files.

An intervening commit, `822655a` with subject `Update project files`, added those 11 files and moved `origin/main` to the same commit. Its file list contains documentation and `.relay.7z`; it contains no backend, frontend, or test source changes.

This report remains the baseline before V2.0 implementation. Future agents must distinguish the committed planning documents from the untracked baseline report itself.

# 2. Existing tests and status

## Test inventory

The repository contains 48 `tests/test*.py` modules. The current inventory covers:

```text
test_phase0.py
test_phase1.py
test_phase2.py
test_phase3.py
test_phase4.py
test_phase5.py
test_phase6.py
test_phase7.py
test_phase7b.py
test_phase7c3.py
test_phase7c4.py
test_phase7c5.py
test_phase7c6.py
test_phase7c7.py
test_phase7c8.py
test_phase7c9.py
test_phase8.py
test_phase8a1.py
test_phase8a4.py
test_phase8b.py
test_phase8c.py
test_phase8c1.py
test_phase8c2.py
test_phase8c3.py
test_phase8c4.py
test_phase8c5.py
test_phase8c6.py
test_phase8c7.py
test_phase8c8.py
test_phase8c9.py
test_phase8c10.py
test_phase8d.py
test_phase8d1.py
test_phase8d2.py
test_phase8d3.py
test_phase8d4.py
test_phase8d5.py
test_phase8d9.py
test_phase8d10.py
test_phase8e.py
test_phase8e1.py
test_phase8e2.py
test_phase8e2_integration.py
test_phase8e2a_capability_enforcement.py
test_phase8e2b_blocker_ux_and_seedvr2.py
test_phase8e2c_legacy_diagnostic_sanitization.py
test_phase8e3.py
test_phase8e4.py
```

Shared fixtures also exist in `tests/phase8d_base.py` and `tests/phase8e_base.py`.

The test inventory covers project storage, source and scene construction, Storyboard, Visuals, prompt relay contracts, render preparation, Render Jobs, Finalization, telemetry, Production Scene, and batch orchestration.

It contains no V2.1 assembly, timeline preflight, assembly manifest, CSV export, or Resolve fixture tests.

## Discovery run

Command:

```text
python -B -m unittest discover -s tests -p "test*.py"
```

Observed result:

```text
Ran 162 tests in 0.098s
FAILED (errors=53)
process exit: 1
```

The primary environment blocker was:

```text
ModuleNotFoundError: No module named 'PIL'
```

The import failure begins at `backend/visuals.py:12` and prevents multiple phase modules from loading. The run also emitted Windows `WinError 5` permission errors while `TemporaryDirectory` attempted cleanup after failed tests.

The run reported no assertion failures, but the suite is not passing. The result is an environment-blocked baseline, not a green test baseline.

## Dependency determination and environment repair

Pillow is an expected dependency for this project. `PROJECT_CONTEXT.md` names Pillow in the Python backend/runtime dependency description and image-validation requirements. `MEMORY.md` states that the target ComfyUI environment supplies Pillow alongside ComfyUI, aiohttp, websockets, and hardware packages. The development plan and V2 implementation plan use the same target-environment model.

The repository has no `requirements.txt`, `pyproject.toml`, lockfile, Dockerfile, or environment file. `backend/requirements.py` discovers ComfyUI nodes, models, and binary tools for runtime readiness. It is not a Python package installer or package manifest.

The correct local fix was:

1. Install Pillow into the interpreter used by the existing test command, `C:\Python314\python.exe`, using its user site because the system site-packages directory is not writable.
2. Verify `import PIL` succeeds. The installed version is Pillow `12.3.0` at `C:\Users\HighStreet\AppData\Roaming\Python\Python314\site-packages`.
3. Run the unchanged unittest discovery command with `TEMP` and `TMP` pointing to a writable user test directory. The harness's default `C:\Users\HighStreet\AppData\Local\Temp` produced `WinError 5` during temporary project creation and cleanup. This temp-directory adjustment is a test-process environment setting, not a project source change.

The environment fix does not add a dependency manifest or modify Python, JavaScript, CSS, or test source.

## Post-environment discovery run

Command:

```text
python -B -m unittest discover -s tests -p "test*.py"
```

The command ran with Pillow installed and a writable per-process temporary directory.

Observed result:

```text
Ran 841 tests in 41.359s
OK
process exit: 0
```

The run emitted expected diagnostic logging from simulated failure and unavailable-runtime tests, including ComfyUI and optional telemetry messages. Those messages did not fail the suite. No project source files changed during the environment repair or retest.

## Coverage limits recorded by the repository

The current test setup does not provide:

- a route-level aiohttp test client for the complete HTTP surface;
- browser end-to-end coverage;
- live H3 completion proof;
- live RTX VSR proof;
- live SeedVR2 proof;
- complete live audio-to-Production-Scene proof;
- live Resolve import or relink qualification.

The intended target ComfyUI environment supplies runtime packages and hardware. This checkout has no dependency manifest that can establish those capabilities by itself.

# 3. Current schema and persistence

## Project schema

Actual source in `backend/projects.py` reports:

```python
SCHEMA_VERSION = 7
```

Legacy normalization paths cover schema versions 1 through 6. The current project document contains:

- project identity and timestamps;
- `source.master_audio` and `source.lyrics_srt`;
- SRT-derived `scenes`;
- characters and locations;
- Story Direction;
- storyboard;
- Visuals;
- prompts;
- optional Production Scene settings.

The actual current project field set has no `assembly` pointer, `current_assembly_id`, or `current_assembly_fingerprint`.

## Existing external record schemas

The current source defines version 1 records for the existing lifecycle layers, including:

- Render Jobs in `backend/render_jobs.py`;
- Finalization records in `backend/render_finalize.py`;
- Production records in `backend/render_production.py`;
- Batch records in `backend/render_batch.py`.

The actual source contains no `assembly_schema_version`, assembly manifest store, assembly media adapter, generation-attempt record, candidate-set record, candidate record, or review decision record.

## Current project-owned layout

The actual project layout includes:

```text
projects/<project_id>/
├── project.json
├── source/
├── references/
├── keyframes/
├── scene_audio/
├── renders/
│   ├── <scene_id>/
│   │   ├── inputs/
│   │   ├── jobs/
│   │   ├── final/
│   │   └── production/
│   └── batches/
└── export/
```

The actual source has no `renders/assemblies/<assembly_id>/` tree.

Project, job, finalization, production, and batch records use atomic project-owned persistence. There is no database.

# 4. Current backend architecture

The backend contains 17 Python files. The main ownership boundaries are:

- `backend/projects.py`: schema-v7 validation, normalization, project directories, path policy, atomic project JSON writes;
- `backend/source.py`: master-audio import/probing and supplied SRT parsing;
- `backend/scenes.py`: deterministic SRT-derived scene construction and contiguous timing validation;
- `backend/storyboard.py`: Story Direction and storyboard relay validation;
- `backend/visuals.py`: keyframe and REF2VA state, references, readiness;
- `backend/prompt_service.py`: deterministic H3 prompt structure, relay validation, freshness and fingerprints;
- `backend/workflows.py`: immutable H3 workflow manifest registry;
- `backend/render.py`: dry preparation, H3 timing plan, project-owned inputs, workflow compilation, preparation fingerprint;
- `backend/render_jobs.py`: ComfyUI client, submission, queue/history reconciliation, durable jobs, output discovery, retry, and cancellation;
- `backend/render_telemetry.py`: volatile WebSocket telemetry presentation;
- `backend/render_finalize.py`: Raw H3 validation, authoritative scene-audio remux, Final Scene promotion;
- `backend/render_production.py`: technical Production Scene methods and post-process lifecycle;
- `backend/render_batch.py`: durable sequential batch planning, execution, recovery, and controls;
- `backend/routes.py`: PromptServer HTTP contract and domain-error mapping;
- `backend/requirements.py`: manifest-driven runtime requirement discovery;
- `backend/entities.py`: character, location, and reference persistence;
- `backend/gpt_launcher.py`: the two allowlisted browser launches for the manual relay.

The current backend pipeline is:

```text
source/SRT
    -> scenes
    -> storyboard and Visuals
    -> prompt compilation
    -> dry H3 preparation
    -> ComfyUI Render Job
    -> Raw output discovery
    -> Final Scene
    -> Production Scene
    -> batch orchestration where selected
```

The backend owns durable progression. The browser observes and requests transitions.

# 5. Current frontend architecture

The `web/` directory contains four files:

- `extension.js`;
- `prompt_state.js`;
- `gpt_launcher.js`;
- `builder.css`.

`web/extension.js` owns one module-global `builderState` object. The state includes:

- active project and project lists;
- current view and operation flags;
- Visuals drafts;
- prompt drafts and relay payloads;
- Render Jobs;
- post-process jobs;
- batch state;
- polling timers and UI messages.

The frontend uses ComfyUI `api.fetchApi` for HTTP requests and multipart uploads. It does not use browser storage, a second server, or a client-owned durable scheduler.

The current Renders UI presents scene render, Final Scene, Production Scene, and batch state. It has no assembly state, assembly preflight panel, draft media route, assembly manifest view, CSV export action, or Resolve handoff control.

# 6. Current API surface

`backend/routes.py` currently registers 61 PromptServer route handlers under `/music-video-builder/`.

The current surface covers:

- health and runtime requirements;
- project lifecycle;
- source audio and SRT upload;
- scene construction;
- character, location, and reference management;
- Storyboard relay and application;
- prompt preview, save, and relay;
- Visuals method, keyframe, and REF2VA operations;
- render preflight, preparation, submit, list, cancel, retry, and finalize;
- Production Scene status, method selection, post-process start, cancel, retry, and status;
- batch preview, start, status, pause, resume, end, and retry-failed;
- the two allowlisted GPT browser-launch routes.

No current route contains `/assembly`, `scenes.csv`, FCPXML, EDL, Resolve XML, or Resolve automation behavior.

# 7. Documented known issues and capability gaps

The following items come from `MEMORY.md`, `V2_ARCHITECTURE_PROPOSAL.md`, `PROJECT_CONTEXT.md`, `PRODUCT_ROADMAP_ASSESSMENT.md`, and `VIDEO_GENERATION_PIPELINE_ASSESSMENT.md`. This section records documentation claims. It does not reclassify them as fixed or independently verified by this report.

## Lifecycle issues

Documentation identifies these current production-core concerns:

- post-process worker functions passed incorrectly through `asyncio.to_thread`;
- post-process cancellation using the wrong ComfyUI client method;
- malformed batch JSON treated as an empty selection;
- post-process jobs without queue/history records remaining active;
- successful history with failed output discovery appearing usable;
- production path and currentness validation gaps;
- frontend render, post-process, and batch status visibility or polling gaps;
- recursive batch transitions without a clear bound;
- boolean Prompt Director response versions needing explicit rejection;
- binary-tool status checking subprocess return codes;
- source-save rollback and cleanup failure handling requiring coverage;
- batch readiness checks requiring selected-work scoping where the contract permits.

V2.0 implementation must establish the actual state of each item through source changes and tests. This baseline does not claim any item has been fixed.

## Missing V2.1 systems

Documentation records these systems as absent from the current implementation:

- full-song review draft assembly;
- scene-ID artifact resolver;
- assembly timeline and media preflight;
- external immutable assembly manifests;
- stale assembly detection;
- `scenes.csv` writer;
- Resolve metadata handoff;
- target Resolve fixture qualification;
- candidate generation and selection;
- formal review and approval records;
- multiple generation-attempt records above Render Jobs.

## Runtime and qualification gaps

Documentation records these unqualified or incomplete runtime paths:

- live H3 output completion;
- complete single-scene audio-to-Final-Scene execution;
- complete audio-to-Production-Scene execution;
- target-NVIDIA RTX VSR qualification;
- target-NVIDIA SeedVR2 qualification;
- mixed-method live batch execution;
- browser end-to-end qualification;
- Resolve handoff qualification.

## Media compatibility risk

The architecture identifies mixed dimensions, FPS, codecs, audio policies, stale identities, path traversal, symlink escape, foreign-project paths, ambiguous outputs, and raw ComfyUI path exposure as assembly risks.

The future assembly service must reject incompatible inputs or use a named, qualified media policy. It must not hide scaling, cropping, frame-rate conversion, audio changes, or creative finishing inside assembly.

# 8. Divergence analysis

## Summary

`PROJECT_MEMORY.md` and `V2_SCHEMA_DECISIONS.md` describe the approved target architecture. The actual source remains the pre-V2 schema-v7 and pre-assembly implementation.

The documents do not contradict each other on the locked schema decisions. Their target fields do not exist in source code yet.

## Comparison table

| Area | `PROJECT_MEMORY.md` | `V2_SCHEMA_DECISIONS.md` | Actual source | Baseline finding |
| --- | --- | --- | --- | --- |
| Project schema | Planned additive v8 pointer; current baseline called out as v7. | Assembly pointer belongs to the project pointer schema. | `backend/projects.py` has `SCHEMA_VERSION = 7`; no assembly field. | Planned, not implemented. |
| `current_assembly_id` | Latest successful valid assembly. | Latest successful valid assembly, never latest request. | Field absent. | Decision is consistent; source has no implementation. |
| Latest requested assembly | External manifest store. | No `latest_requested_assembly_id` in project JSON. | No assembly store. | Future design only. |
| Assembly manifest | External immutable versioned manifest under `renders/assemblies`. | Version 1 with explicit migration readers. | No assembly module or manifest files. | Planned, not implemented. |
| Future lineage refs | Nullable attempt, candidate-set, and candidate references. | Present nullable fields in every V2.1 scene record. | No such fields or records. | Decision is consistent; source has no implementation. |
| Job IDs | Selected lineage references only. | Selected lineage references only, not history. | Render Job and post-process records exist, but no assembly scene records consume them. | Existing job architecture supports the rule; assembly mapping is absent. |
| Fingerprints | Scene, timeline, assembly, attempt, candidate, review, and artifact meanings stay separate. | Exact SHA-256 basis and migration rules. | Existing per-layer preparation, prompt, finalization, production, and file identities exist; no assembly fingerprints. | Existing foundation; assembly fingerprint contract is absent. |
| Review | Future eligibility provider; V2.1 review reference remains null. | Nullable opaque `review_decision_id`. | No review service or approval state. | Expected pre-V2 state. |
| Candidate attempts | Future layers above Render Jobs. | Nullable references only in V2.1. | No candidate or attempt layer. | Expected pre-V2 state. |
| ComfyUI authority | ComfyUI executes; Builder owns durable truth. | Runtime integration must not become product state. | `render_jobs.py` reconciles queue/history; telemetry remains separate. | Current source aligns with the boundary. |
| Resolve | Resolve remains editor and finishing environment. | CSV first; richer formats require qualification. | No assembly or export routes. | Product boundary aligns; handoff is absent. |

## Important target-versus-source distinction

The following statements in the memory and decision documents describe approved future contracts, not current source behavior:

- schema-v8 assembly pointer;
- `assembly_schema_version`;
- `current_assembly_id`;
- `assembly_fingerprint`;
- nullable future lineage references;
- assembly scene fingerprints;
- external assembly manifests;
- assembly routes and frontend state;
- CSV export.

V2.0 coding must first preserve the actual schema-v7 and existing lifecycle ownership. V2.1 coding can add the planned assembly contracts through explicit migration and new external records.

# 9. Baseline conclusion

Implementation begins from this state:

- Git has no staged or tracked working-tree changes.
- The branch `main` matches `origin/main` at `822655a`.
- `CURRENT_IMPLEMENTATION_BASELINE.md` is the only untracked file at final capture; the preceding 11 planning and evidence files were added by the intervening documentation commit.
- The repository contains 48 test modules. The initial environment run failed with 53 errors because Pillow was absent; after the environment-only repair, the unchanged discovery command ran 841 tests and passed.
- The source uses project schema-v7 and has no assembly implementation.
- The backend owns the existing source, prompt, render, Final Scene, Production Scene, and batch lifecycle.
- The frontend presents the existing scene-production workflow and has no assembly surface.
- V2.0 lifecycle concerns and V2.1 assembly systems remain documented target work.
- `PROJECT_MEMORY.md` and `V2_SCHEMA_DECISIONS.md` describe future contracts that currently diverge from source by design, not through an undocumented conflict.

No code was modified while capturing this baseline.
