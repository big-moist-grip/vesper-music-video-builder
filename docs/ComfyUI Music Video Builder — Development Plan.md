# ComfyUI Music Video Builder — Development Plan

## 0. Relay authority

This project uses three participants.

### GPT-5.6 Sol — reviewer/controller

Sol owns:

- Architecture.
- Product scope.
- Phase boundaries.
- Implementation prompts.
- Code/diff review.
- Failure diagnosis.
- Remediation instructions.
- Approval to advance.

Sol is the technical decision-maker.

### GPT-5.6 Luna — coding agent

Luna owns:

- Repository inspection.
- Implementation of the current approved phase.
- Automated tests.
- Running available tests.
- Producing review artefacts.
- Reporting concrete blockers or risks to Sol.
- Stopping when the current phase is complete.

Luna does **not** own:

- Scope.
- Architecture.
- Feature additions.
- Phase progression.
- Product-management decisions.

If a technical decision cannot safely be resolved from the current instructions or repository evidence, Luna reports it under `reviewer_attention`. Luna does not ask the user to choose.

### User — relay/manual test operator

The user:

- Transfers Sol's prompts to Luna.
- Returns Luna's results to Sol.
- Performs explicitly requested manual ComfyUI tests.
- Reports exact observed behaviour/errors/screenshots.

The user is not expected to:

- Review code.
- Diagnose failures.
- Choose implementation strategies.
- Compare architectural options.
- Translate technical findings.

Never shift those responsibilities onto the user.

---

# 1. Product definition

Create a private, local-only ComfyUI extension:

`ComfyUI-MusicVideoBuilder`

Purpose:

Turn a master audio track and SRT lyrics into an organised storyboard and a queue of local MiniMax H3 scene generations that can later be assembled in DaVinci Resolve.

Target machine:

- Windows.
- NVIDIA RTX 4080 SUPER 16 GB.
- 32 GB system RAM.
- Local ComfyUI.
- Local MiniMax H3 inference.

No external inference API is required.

---

# 2. User workflow

## Stage A — Setup

User supplies:

- Master audio track.
- SRT lyrics.

Builder:

1. Validates local requirements.
2. Reads master-audio duration.
3. Parses SRT.
4. Creates one initial scene per lyric cue.
5. Creates scenes for instrumental gaps.
6. Splits any cue or instrumental region exceeding 10 seconds.
7. Preserves exact source timing separately from H3 render-frame length.
8. Allows scene timing to be reviewed.

The 10-second split is a builder/editorial policy, not an assumed H3 technical maximum.

---

## Stage B — Characters

User defines any number of characters.

Each character contains:

- Stable ID.
- Name.
- Role:
  - performer
  - band_member
  - extra
- Appearance/outfit description.
- One or more local reference images.

References are copied into the project rather than depending on their original external file paths.

---

## Stage C — Locations

Each location contains:

- Stable ID.
- Name.
- Description.
- One or more local reference images.

---

## Stage D — Story direction

Optional:

- Short music-video story brief.
- Overall visual/directional notes.

Builder produces a strict storyboard-request JSON payload.

The user manually:

1. Copies JSON.
2. Pastes it into the dedicated ChatGPT/custom GPT.
3. Receives structured JSON.
4. Pastes that JSON back into the builder.

No OpenAI API integration.

---

## Stage E — Storyboard import

Returned JSON allocates:

- Characters.
- Locations.
- Scene type.
- Scene action.
- Keyframe instructions.
- Camera direction.
- Motion direction.
- Continuity notes.
- References required to create the keyframe.

Importer must reject before mutation:

- Invalid JSON.
- Wrong schema version.
- Unknown IDs.
- Duplicate IDs.
- Missing required scenes.
- Invalid enum values.

No partial application after validation failure.

---

## Stage F — Keyframes

Each scene card shows:

- Scene ID.
- Timeline start/end.
- Duration.
- SRT lyric or instrumental status.
- Assigned characters.
- Assigned location.
- Required reference images.
- Keyframe-generation instructions.
- Copyable image prompt.

The user generates keyframe images externally.

The user then assigns the accepted image to the scene.

Store separately:

- `keyframe_generation_prompt`
- `intended_keyframe_description`
- `actual_keyframe_description`

The accepted image itself is authoritative.

---

## Stage G — H3 prompt generation

Final H3 prompts are built using:

- Actual keyframe description.
- Character continuity information.
- Location information.
- Scene action.
- Camera direction.
- Motion direction.
- Scene duration.
- Relevant audio/performance requirements.

### Prompt-helper architecture

Prompt generation may use a local text LLM through Ollama.

Ollama is:

- Localhost only.
- Optional.
- Separate from the H3 render workflow.
- Run before video generation.

The H3 render graph always receives a completed prompt string.

Do not run an LLM inside every H3 render.

If Ollama is disabled/unavailable, builder must still support deterministic template-generated prompts and manual editing.

The final editable prompt stored in the scene is authoritative.

---

# 3. H3 production architecture

## Primary generation mode

Default MVP generation path:

`accepted keyframe -> H3 I2V/FL2VA -> output video`

Do not build a general H3 interface.

Do not expose T2V in the Music Video Builder.

REF2VA is not an MVP requirement unless workflow qualification demonstrates a concrete need that cannot be satisfied by the accepted keyframe.

The generated storyboard keyframe is the primary visual-conditioning input.

---

## Source-audio conditioning

The master song is authoritative.

For each scene:

1. Extract the exact corresponding audio segment from the master track.
2. Feed that audio into the H3 generation path using the source-audio latent replacement strategy qualified from the Advanced v16 donor.
3. Generate visuals conditioned by that source audio.
4. Preserve the original extracted source audio as authoritative.
5. Final encoded scene should use the original scene-audio segment rather than accepting newly generated H3 audio as authoritative.

This is particularly important for:

- Vocal performance.
- Lip movement.
- Instrument/performance timing.
- Rhythmically driven body motion.

The exact implementation must be validated against the installed H3 nodes during the workflow-integration phase.

---

# 4. Community workflow donor policy

Four supplied workflows are reference donors.

They are not production workflows.

Do not embed them intact.

Do not reproduce their UI groups, notes, toggles or convenience infrastructure.

## Donor A — primary generation core

`minimaxH3T2VI2VREF2VAdvanced_v16`

Use as the primary reference for:

- H3 FL2VA/I2V generation.
- Optional REF2VA mechanics if later required.
- Source-audio latent replacement.
- Turbo LoRA.
- Sage Attention.
- Sol Attention.
- H3 sampling.
- H3 resolution/frame calculations.
- 24 fps output.
- VRAM cleanup strategy where useful.

Potential features in this donor that are not automatically adopted:

- Spectrum.
- EasyCache.
- RIFE.
- T2V.
- REF2VA.
- Generic LoRA controls.

Qualification determines whether an optimization provides enough measurable benefit to retain.

---

## Donor B — fast upscale and memory optimisation

`minimaxH3EZTurboOptimalRTXUpscale_v35REMADE`

Reference for:

- NVIDIA RTX Video Super Resolution.
- VRAM chunking.
- Model loading suitable for constrained VRAM.
- Additional H3 speed optimisations.
- H3 frame calculation.
- Optional acceleration techniques.

Do not adopt:

- Full EZ workflow UI.
- Generic mode switching.
- LTX refinement by default.
- RIFE by default.
- Its entire dependency set without evidence.

---

## Donor C — quality upscale and local prompt helper

`minimaxH3WithSEEDVR2Upscaler_v4`

Reference for:

- SeedVR2 video upscaling.
- Model unloading between H3 and upscale stages.
- 16 GB-class VRAM handling.
- Local Ollama prompt-helper behaviour.
- H3 sigma-shift relationships if applicable.

Do not execute Ollama as part of the render graph.

Do not retain seamless-loop or interpolation functionality unless explicitly added by Sol later.

---

## Donor D — research/reference only

`minimaxH3Ref2vaAllInputsTurboMode_beta`

Reference for:

- Safe H3 resolution calculations.
- Half-resolution generation.
- NVIDIA VSR integration.
- LTX 2× upscale behaviour.
- Large REF2VA input handling.
- Audio/video/image reference conventions.

It is deliberately too broad to be the production graph.

Do not use it as the builder workflow.

---

# 5. Render quality modes

Target builder UI:

### Generation mode

- `Draft`
- `Final`

Do not expose every sampler parameter.

Exact mappings are not frozen until workflow qualification.

Expected intent:

### Draft

Optimised for:

- Fast storyboard validation.
- Motion testing.
- Composition testing.

May use:

- Turbo LoRA.
- Lower H3 resolution.
- No upscale.

### Final

Optimised for:

- Accepted music-video takes.

May use:

- Qualified high-quality H3 settings.
- Optional output upscale.

---

# 6. Upscaling

Target UI:

`Upscale`

- None
- RTX VSR — Fast
- SeedVR2 — Quality

## RTX VSR

Primary fast upscale candidate.

Reasons:

- Runs well on RTX hardware.
- Low conceptual complexity.
- Suitable for batch generation.
- Already demonstrated in supplied donor workflows.

## SeedVR2

Optional quality upscale candidate.

Use only if Phase 7 qualification confirms:

- Fits RTX 4080 SUPER 16 GB.
- Fits 32 GB system RAM.
- Does not destabilise sequential rendering.
- Quality improvement is visibly worthwhile.

H3 must be unloaded/released before SeedVR2 where necessary.

## Excluded

Do not include LTX 2.3 refinement in MVP.

It adds:

- Additional large models.
- Additional text encoders/VAEs.
- Additional generative behaviour.
- Additional failure modes.

If SeedVR2 proves unusable, RTX VSR remains sufficient for MVP.

---

# 7. Frame interpolation

Do not include interpolation in MVP.

H3's native 24 fps output is sufficient for Resolve.

Interpolation can be done later in Resolve or added only if there is a demonstrated need.

Do not import:

- RIFE.
- FILM.
- Other interpolation dependencies.

---

# 8. Dependency philosophy

The finished extension should depend only on nodes actually required by the stripped production graph.

Do not inherit every custom-node dependency from donor workflows.

Requirement scanning must be generated from the final production workflow manifest.

Potential final dependencies are expected to include only the minimum required subset of:

- ComfyUI core MiniMax H3 nodes.
- Video Helper Suite for encoding where still required.
- H3 acceleration node(s) that survive benchmarking.
- NVIDIA RTX nodes if RTX VSR is enabled.
- SeedVR2 nodes only if SeedVR2 survives qualification.
- Ollama availability only when prompt assistance is enabled.

Avoid rgthree/EasyUse/KJ convenience nodes in the production graph when the same operation can be implemented directly or through core nodes.

A node used only to make a manually operated workflow more convenient is not automatically useful in a programmatically patched workflow.

---

# 9. Workflow manifest

Production workflow-specific identifiers live in one manifest.

Example concept:

```json
{
  "workflow_id": "h3_music_video_i2v_v1",
  "workflow_file": "h3_music_video_i2v_api.json",
  "required_node_types": [],
  "required_models": [],
  "inputs": {
    "keyframe": {},
    "audio": {},
    "prompt": {},
    "seed": {},
    "width": {},
    "height": {},
    "frame_count": {},
    "filename_prefix": {}
  }
}
```

Never scatter node IDs through Python source.

Every render:

1. Loads the immutable production template.
2. Deep-copies it.
3. Patches declared manifest inputs.
4. Validates it.
5. Queues the fresh copy.

Never mutate the stored workflow template.

---

# 10. Batch render model

Scene render states:

- `not_ready`
- `ready`
- `queued`
- `running`
- `completed`
- `failed`
- `cancelled`

Batch generation is sequential.

Algorithm:

1. Find next ready scene.
2. Save state as queued.
3. Build fresh workflow.
4. Queue it locally.
5. Save running state.
6. Monitor completion/error.
7. Verify expected output exists.
8. Attach output metadata.
9. Save completed/failed state atomically.
10. Continue.

Restart recovery:

- Completed scenes remain completed.
- Failed scenes remain failed.
- Interrupted queued/running scenes return to a recoverable state.
- Resume never regenerates completed scenes unless explicitly requested.

---

# 11. Project persistence

Use JSON.

No database.

Every entity has a stable ID.

Project state must include:

- schema_version
- project metadata
- master audio
- SRT
- lyric cues
- render scenes
- characters
- locations
- reference assets
- storyboard allocation
- keyframes
- prompts
- render settings
- render status
- output metadata

Save atomically:

`temporary file -> flush -> replace valid project`

Do not build a generic schema-migration framework.

A simple integer schema version and narrowly scoped migration functions are enough if a migration ever becomes necessary.

---

# 12. Project files

Suggested runtime structure:

```text
Project/
├── project.json
├── source/
│   ├── master_audio.*
│   └── lyrics.srt
├── references/
│   ├── characters/
│   └── locations/
├── keyframes/
├── scene_audio/
├── renders/
└── export/
    └── scenes.csv
```

Do not create human documentation inside project folders.

---

# 13. Resolve handoff

Final output remains intentionally simple.

Export:

- Scene MP4s.
- Original master audio.
- Original SRT.
- `scenes.csv`.

CSV fields:

- scene_id
- timeline_start
- timeline_end
- exact_duration
- render_duration
- lyric
- scene_type
- character_ids
- location_id
- output_file
- take
- status

No:

- Resolve project generation.
- FCPXML.
- EDL generation.
- automatic timeline construction.

Those are outside MVP.

---

# 14. Explicit non-goals

Do not implement:

- Cloud MiniMax.
- OpenAI API.
- Embedded ChatGPT.
- Browser automation.
- Cloud storage.
- Accounts.
- Authentication.
- Telemetry.
- Analytics.
- Multi-user features.
- Mobile UI.
- Generic H3 playground.
- Generic ComfyUI workflow editor.
- T2V UI.
- Built-in image generation.
- Built-in image editing.
- Vision model.
- Automatic image description.
- Automatic model downloads.
- Automatic custom-node installation.
- LTX refinement.
- Frame interpolation.
- Final music-video stitching.
- Resolve automation.
- Colour grading.
- Audio mixing/mastering.
- LoRA training.
- Plugin systems.
- Extensibility frameworks.
- Theme systems.
- Internationalisation.
- Auto-update infrastructure.
- Registry publishing.
- CI/CD.
- Release pipelines.
- Human-facing README.
- User guide.
- Tutorials.
- Architecture documents.
- Changelog.

This is a personal tool. Build only what is required to make it work.

---

# 15. Engineering constraints

Backend:

- Python.

Frontend:

- Plain JavaScript ES modules.
- Plain CSS.
- Browser DOM APIs.

Do not use:

- React.
- Vue.
- Svelte.
- Preact.
- TypeScript.
- npm.
- frontend compilation.
- separate web server.
- database.
- Docker.

Use ComfyUI's supported extension interfaces.

Do not modify ComfyUI core.

Do not monkey-patch internals where a supported interface exists.

Inspect installed ComfyUI code before relying on version-sensitive APIs.

Prefer standard-library Python.

Do not add Python dependencies without reviewer approval unless they are already available inside the target ComfyUI environment.

---

# 16. Testing philosophy

Automate deterministic behaviour.

Required automated coverage eventually includes:

- SRT parser.
- Multiline SRT.
- malformed timing.
- overlapping cues.
- instrumental gaps.
- >10 s splitting.
- equalised long-scene splitting.
- exact scene timing.
- stable IDs.
- JSON schema validation.
- save/load.
- failed atomic write handling.
- GPT import validation.
- prompt construction.
- frame calculation.
- manifest patching.
- workflow-template immutability.
- output filenames.
- queue state transitions.
- restart recovery.
- Resolve CSV.

Manual testing is reserved for:

- ComfyUI startup.
- frontend appearance.
- file pickers.
- reference previews.
- real H3 rendering.
- GPU/VRAM behaviour.
- speed/quality comparison.
- RTX VSR.
- SeedVR2.
- Resolve import.

Use `unittest` unless the repository already has another test framework.

---

# 17. Relay artefacts

Every implementation phase must create or replace:

```text
.relay/
├── result.json
├── changes.patch
└── test-output.txt
```

These are machine-review artefacts, not documentation.

## result.json

```json
{
  "phase": "PHASE_ID",
  "status": "PASS | PARTIAL | BLOCKED",
  "summary": [],
  "files_changed": [],
  "tests_run": [
    {
      "command": "exact command",
      "result": "PASS | FAIL",
      "details": "concise factual details"
    }
  ],
  "manual_tests_required": [
    {
      "id": "MT-01",
      "steps": [],
      "expected": "exact expected result"
    }
  ],
  "known_issues": [],
  "reviewer_attention": []
}
```

## changes.patch

Must contain complete current-phase source/test changes.

Exclude:

- `.relay`
- caches
- model files
- generated video
- media
- virtual environments
- temporary files

## test-output.txt

Contains:

- Exact commands.
- Exact stdout.
- Exact stderr.
- Exit codes where possible.

Never paraphrase failures.

---

# 18. Development phases

## PHASE 0 — Extension shell

Implement only:

- Custom-node registration.
- `Music Video Builder` launcher node.
- Frontend extension.
- `Open Builder` button.
- Full-screen builder overlay.
- Namespaced health route.
- Connection status.
- Minimal CSS.
- Basic tests.

No H3 implementation.

---

## PHASE 1 — Project persistence

Implement:

- Create project.
- Open project.
- Save.
- Autosave.
- Atomic writes.
- Stable IDs.
- Runtime folder layout.
- Project JSON.

---

## PHASE 2 — Audio/SRT/scene construction

Implement:

- Audio import.
- Audio-duration detection.
- SRT import/parser.
- Cue creation.
- Instrumental-gap detection.
- Render-scene creation.
- >10 s splitting.
- Scene timing review.

Do not touch H3.

---

## PHASE 3 — Character/location references

Implement:

- Character CRUD.
- Character roles.
- Character reference images.
- Character descriptions.
- Location CRUD.
- Location references.
- Location descriptions.
- Project-local asset copying.

---

## PHASE 4 — ChatGPT storyboard relay

Implement:

- Story brief.
- Storyboard request JSON.
- Copy.
- Response paste/import.
- Strict schema validation.
- Preview.
- Apply.
- Persistence.

No API.

---

## PHASE 5 — Keyframe workflow

Implement:

- Scene cards.
- Reference instructions.
- Keyframe prompt.
- Copy controls.
- Accepted-image assignment.
- Actual keyframe description.
- Scene readiness.

---

## PHASE 6 — H3 donor qualification

This phase uses the four supplied workflow JSONs.

Luna must inspect actual graphs, not their Civitai descriptions.

Primary donor:

`Advanced v16`

Secondary donors:

- `EZ v3.5`
- `SeedVR2 v4`
- `All Inputs beta`

### 6A — Extract minimum H3 core

Identify exact nodes required for:

- FL2VA/I2V.
- Prompt.
- First-frame image.
- H3 model.
- text encoder.
- video VAE.
- audio VAE.
- duration/frame count.
- seed.
- sampler.
- decode.
- source-audio latent replacement.
- output encoding.

Strip all graph UI/QoL infrastructure.

### 6B — Acceleration qualification

Measure only realistic combinations on RTX 4080 SUPER:

- Base.
- Turbo.
- Turbo + Sage/Sol if compatible.
- Other donor acceleration only when it has credible value.

Record:

- completion/failure
- generation time
- peak VRAM where observable
- peak RAM where observable
- qualitative user result

Do not benchmark every theoretical permutation.

Sol selects final production settings after reviewing results.

### 6C — Upscale qualification

Test:

- RTX VSR.
- SeedVR2.

Do not test LTX refine unless Sol explicitly reopens it.

### 6D — Production graph

Create the smallest stable production API-format workflow from the winning pieces.

Create workflow manifest.

Freeze node mappings after approval.

---

## PHASE 7 — Requirement checker + H3 prompt service

Implement:

- Production-workflow dependency scan.
- Required model scan.
- FFmpeg scan.
- Optional RTX VSR scan.
- Optional SeedVR2 scan.
- Optional Ollama scan.
- Local Ollama H3 prompt helper.
- Deterministic fallback prompt builder.
- Editable final prompt.

Do not report dependencies inherited from unused donor branches.

---

## PHASE 8 — Single-scene rendering

Implement:

- Exact master-audio segment extraction.
- H3 render-frame calculation.
- Production-workflow patching.
- Source-audio conditioning.
- Local queue submission.
- Progress.
- failure reporting.
- output detection.
- exact-duration trim if needed.
- authoritative source audio in final scene output.
- optional selected upscale.
- output association.

One real H3 scene must pass before batch work begins.

---

## PHASE 9 — Persistent batch rendering

Implement:

- Sequential scene queue.
- Persistent states.
- Stop after current.
- retry.
- failure handling.
- resume.
- output verification.
- restart recovery.

Test an actual short multi-scene batch.

---

## PHASE 10 — Resolve export/final reduction

Implement:

- deterministic filenames.
- scenes.csv.
- source audio/SRT retention.
- final regression tests.
- removal of dead code.
- removal of unused controls.
- removal of abandoned workflow branches.

No new features.

---

# 19. Phase progression rule

Every phase follows:

```text
Sol issues prompt
→
Luna implements only that phase
→
Luna creates relay artefacts
→
User performs requested manual tests
→
User transfers results to Sol
→
Sol reviews actual patch/results
→
Sol issues remediation OR approves
→
only then next phase begins
```

Luna must never advance itself.

A manual failure remains part of the current phase until Sol closes it.

---

# 20. Core design rule

The Music Video Builder replaces the complexity of manually operating ComfyUI graphs.

Therefore:

**Do not recreate workflow complexity in the Builder UI.**

A complex donor graph may contain dozens of switches because it serves many users and use cases.

Our production graph serves exactly one workflow.

Expose creative choices.

Hide implementation details.