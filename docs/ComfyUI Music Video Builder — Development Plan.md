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

The MVP supports two first-class per-scene H3 visual-conditioning methods:

- `keyframe_i2v` — Keyframe / Image-to-Video. This is the default and primary workflow.
- `reference2video` — Reference-to-Video using selected project-local character/location still-image references through H3 REF2VA.

Generic Text-to-Video is not part of the Music Video Builder MVP.

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
- Visual-generation instructions.
- Camera direction.
- Motion direction.
- Continuity notes.
- References relevant to the scene.
- Keyframe instructions where useful for the default keyframe workflow.

Importer must reject before mutation:

- Invalid JSON.
- Wrong schema version.
- Unknown IDs.
- Duplicate IDs.
- Missing required scenes.
- Invalid enum values.

No partial application after validation failure.

Storyboard import does not make the final per-scene generation-method choice. The Visuals stage defaults every scene to `keyframe_i2v`; the user may override an individual scene to `reference2video`.

---

## Stage F — Visuals

Visuals owns the authoritative per-scene generation method.

Each scene stores:

- `generation_method`
  - `keyframe_i2v`
  - `reference2video`

Default:

`keyframe_i2v`

Changing generation method must not destructively delete assets or metadata belonging to the inactive method.

Each scene card shows:

- Scene ID.
- Timeline start/end.
- Duration.
- SRT lyric or instrumental status.
- Assigned characters.
- Assigned location.
- Generation Method.
- Relevant project-local reference images.
- Visual-generation instructions.

### Keyframe / Image-to-Video

When `generation_method = keyframe_i2v`:

- Show keyframe-generation instructions.
- Show a copyable external-image prompt.
- The user generates the keyframe image externally.
- The user assigns the accepted image to the scene.
- The accepted keyframe is the primary H3 visual-conditioning input.

Store separately:

- `keyframe_generation_prompt`
- `intended_keyframe_description`
- `actual_keyframe_description`

The accepted image itself is authoritative.

### Reference-to-Video

When `generation_method = reference2video`:

- An accepted keyframe is not required.
- The user selects one or more existing project-local Character and/or Location reference images.
- Store an ordered reference selection including the owning entity ID and reference ID.
- Map selected still images deterministically to H3 `<Picture N>` inputs.
- Build deterministic `<Subject N>` / location definitions from the selected project resources and storyboard context.
- The final H3 prompt may use those picture/subject tags directly.

Reference-to-Video is a first-class MVP mode, not an experimental hidden branch.

Do not expose generic T2V.

Do not expose H3 video-reference or motion-transfer inputs in MVP merely because REF2VA can technically accept them.

---

## Stage G — H3 prompt generation

Final H3 prompts branch by the scene's `generation_method`.

For `keyframe_i2v`, prompt construction uses:

- Actual keyframe description.
- Character continuity information.
- Location information.
- Scene action.
- Camera direction.
- Motion direction.
- Scene duration.
- Relevant audio/performance requirements.

For `reference2video`, prompt construction uses:

- Ordered selected project-local references.
- Deterministic `<Picture N>` / `<Subject N>` mapping.
- Character continuity information.
- Location information.
- Scene action.
- Camera direction.
- Motion direction.
- Scene duration.
- Relevant audio/performance requirements.

Both paths produce one final editable H3 prompt string before render.

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

## MVP builder tabs

The user-facing production sequence is:

`Setup -> Storyboard -> Visuals -> Prompts -> Render`

Responsibilities:

- Setup — master audio, SRT, scene timing/provenance.
- Storyboard — characters, locations, references, story direction, ChatGPT relay/import.
- Visuals — per-scene Generation Method plus Keyframe or Reference-to-Video visual inputs.
- Prompts — final editable H3 prompt review and generation settings.
- Render — single-scene and batch H3 execution, takes, status, upscale and Resolve handoff.

Only functional tabs are shown. Do not add future-stage placeholder navigation.

---

# 3. H3 production architecture

## Scene generation methods

The MVP has two first-class per-scene generation methods.

### `keyframe_i2v` — Keyframe / Image-to-Video

Default and primary path:

`accepted keyframe -> H3 I2V/FL2VA -> output video`

The accepted keyframe is the primary visual-conditioning input.

### `reference2video` — Reference-to-Video

Alternate path:

`selected project-local still references -> H3 REF2VA -> output video`

This mode does not require an accepted keyframe.

It uses ordered Character and/or Location reference images selected in Visuals and maps them deterministically into H3 picture/subject references.

### Shared rules

- Generation method is selected per scene.
- New scenes default to `keyframe_i2v`.
- Switching methods must not destructively delete inactive-method assets or metadata.
- Both methods use the exact scene audio extracted from the master song as authoritative source audio.
- Both methods share the same storyboard action/camera/motion/continuity context.
- Both methods produce a final editable prompt before rendering.
- Both methods produce the same take/status/output model.

Do not build a general H3 interface.

Do not expose T2V in the Music Video Builder.

Do not expose H3 video-reference/motion-transfer inputs in MVP.

Production integration must qualify both FL2VA/I2V and REF2VA. If their minimum stable graphs differ materially, prefer separate stripped production templates/manifests rather than retaining a large generic multi-mode donor graph.

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
- H3 REF2VA generation.
- Source-audio latent replacement for both supported generation methods where compatible.
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
- REF2VA still-image input handling.
- Ordered `<Picture N>` / `<Subject N>` reference conventions.
- Audio/video/image reference mechanics as research evidence.

It is deliberately too broad to be the production graph.

Do not use it as the builder workflow.

MVP Reference-to-Video adopts only the qualified still-image REF2VA subset plus the authoritative scene-audio path. Video-reference/motion-transfer UI remains excluded.

---

# 5. Render quality modes

Target builder UI:

### Quality mode

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

Production workflow-specific identifiers live in a small manifest registry keyed by supported scene generation method.

Expected production entries:

- `keyframe_i2v`
- `reference2video`

Conceptual I2V entry:

```json
{
  "generation_method": "keyframe_i2v",
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

Conceptual REF2VA entry:

```json
{
  "generation_method": "reference2video",
  "workflow_id": "h3_music_video_ref2va_v1",
  "workflow_file": "h3_music_video_ref2va_api.json",
  "required_node_types": [],
  "required_models": [],
  "inputs": {
    "pictures": {},
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

Exact REF2VA picture-slot mappings are frozen only after donor qualification.

Never scatter node IDs through Python source.

Every render:

1. Selects the manifest from the scene's `generation_method`.
2. Loads that immutable production template.
3. Deep-copies it.
4. Patches only declared manifest inputs.
5. Validates it.
6. Queues the fresh copy.

Never mutate a stored workflow template.

Do not force both supported methods into one large generic donor graph if separate stripped graphs are smaller or more stable.

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
- per-scene `generation_method`
- keyframe assets/metadata
- ordered Reference-to-Video selections/mappings
- prompts
- render settings
- render status
- output metadata

Generation-method switching is non-destructive: inactive keyframe or Reference-to-Video data remains stored unless the user explicitly removes it.

Save atomically:

`temporary file -> flush -> replace valid project`

Do not build a generic schema-migration framework.

A simple integer schema version and narrowly scoped migration functions are enough if a migration ever becomes necessary.

### Project-management safety

The no-project landing may delete a valid project only through:

- an explicit in-app confirmation
- validated project ID
- backend-confined deletion of that project's directory

Never accept an arbitrary filesystem path from the frontend.

Unreadable-project warnings may be persistently ignored by storing an application-local invalid-entry signature outside project JSON. Ignoring a warning must not delete or modify the malformed project. If the invalid condition changes, the new failure may surface again.

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
- H3 video-reference/motion-transfer UI.
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
- prompt construction for both supported generation methods.
- generation-method default/enum validation.
- non-destructive generation-method switching.
- deterministic Reference-to-Video picture/subject mapping.
- frame calculation.
- generation-method manifest selection.
- manifest patching for I2V and REF2VA.
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
- real H3 rendering in Keyframe / Image-to-Video mode.
- real H3 rendering in Reference-to-Video mode.
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
- Visual/reference instructions that remain usable by either later generation method.

The ChatGPT storyboard relay does not own the final generation-method choice.

Visuals defaults scenes to `keyframe_i2v`; the user can later override any scene to `reference2video`.

No API.

---

## PHASE 5 — Visuals workflow

Implement:

- Visuals tab.
- Scene cards.
- Per-scene Generation Method selector.
- Default `keyframe_i2v`.
- Alternate `reference2video`.
- Non-destructive method switching.

For `keyframe_i2v`:

- Reference instructions.
- Keyframe prompt.
- Copy controls.
- Accepted-image assignment.
- Actual keyframe description.
- Keyframe readiness.

For `reference2video`:

- Select one or more existing project-local Character/Location still references.
- Preserve ordered owning-entity/reference IDs.
- Preview selected references.
- Deterministic planned `<Picture N>` / `<Subject N>` mapping.
- Reference-to-Video readiness without requiring a keyframe.

No H3 rendering yet.

Do not expose T2V or video-reference/motion-transfer UI.

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

### 6A — Extract minimum H3 cores

Identify exact common and method-specific nodes required for both supported production paths.

Keyframe / Image-to-Video:

- FL2VA/I2V.
- First-frame/keyframe image input.

Reference-to-Video:

- REF2VA.
- Ordered still-image reference inputs.
- Picture/subject prompt mapping.

Common:

- Prompt.
- H3 model(s).
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

Do not qualify T2V as a product mode.

Do not add video-reference/motion-transfer product inputs.

### 6B — Generation and acceleration qualification

Both `keyframe_i2v` and `reference2video` must successfully execute on the target RTX 4080 SUPER before production mappings are frozen.

For each supported method, measure only realistic combinations:

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

### 6D — Production graphs

Create the smallest stable production API-format workflow(s) from the winning pieces.

The finished production integration must support:

- `keyframe_i2v`
- `reference2video`

Prefer separate stripped templates when that is smaller/safer than a generic multi-mode graph.

Create the manifest registry and one approved manifest entry per supported method.

Freeze node mappings after approval.

---

## PHASE 7 — Requirement checker + H3 prompt service

Implement:

- Production-workflow dependency scan across both approved generation-method manifests.
- Required model scan for both supported methods.
- FFmpeg scan.
- Optional RTX VSR scan.
- Optional SeedVR2 scan.
- Optional Ollama scan.
- Local Ollama H3 prompt helper.
- Deterministic fallback prompt builder for `keyframe_i2v`.
- Deterministic fallback prompt builder for `reference2video`, including stable picture/subject tags.
- Editable final prompt.

Do not report dependencies inherited from unused donor branches.

---

## PHASE 8 — Single-scene rendering

Implement:

- Exact master-audio segment extraction.
- H3 render-frame calculation.
- Production workflow/manifest selection from `generation_method`.
- Keyframe input patching for `keyframe_i2v`.
- Ordered REF2VA picture input patching for `reference2video`.
- Source-audio conditioning.
- Local queue submission.
- Progress.
- failure reporting.
- output detection.
- exact-duration trim if needed.
- authoritative source audio in final scene output.
- optional selected upscale.
- output association.

Before batch work begins, one real H3 scene must pass in each supported generation method:

- Keyframe / Image-to-Video.
- Reference-to-Video.

---

## PHASE 9 — Persistent batch rendering

Implement:

- Sequential scene queue supporting mixed `keyframe_i2v` and `reference2video` scenes in the same project.
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