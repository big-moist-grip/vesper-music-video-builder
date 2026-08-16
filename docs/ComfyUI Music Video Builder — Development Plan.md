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

Supported development and execution machines:

- Windows.
- AMD Radeon RX 7900 XT — primary development and functional-test machine.
- NVIDIA RTX 4080 SUPER 16 GB — secondary, faster production/performance machine.
- 32 GB system RAM.
- Local ComfyUI.
- Local MiniMax H3 inference.

No external inference API is required.

### Development host and secondary performance host

The primary development host is:

- AMD Radeon RX 7900 XT.

The secondary production/performance host is:

- NVIDIA RTX 4080 SUPER 16 GB.
- 32 GB system RAM.

Both GPUs are valid H3 execution platforms. The AMD workstation is the primary
machine for Builder development, automated testing, and functional H3 testing;
the RTX workstation is available for faster production runs and performance
comparison. RTX throughput preference is informational and is not an execution
or implementation prerequisite.

The same quality-first production workflow and settings apply on both GPUs.
Actual runtime, model, node, content, workflow, and preparation failures remain
truthful blockers on either machine. Do not alter Windows paging, download
lower-quality substitutes, or promote the W4A8 REF2VA candidate to Final to
work around a runtime failure.

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

Persisted Story Direction contains:

- `storyboard_mode`:
  - `loose` — default. Lyrics provide emotional information, broad narrative/theme information, song structure, and intensity/pacing cues. The Director may construct a coherent song-level concept that is not a literal scene-by-scene lyric illustration; mundane lyrical nouns do not automatically require matching props or locations.
  - `strict` — lyrics have close visual influence. Concrete lyrical events, objects, environments, and changes should materially inform corresponding scenes where practical while preserving coherence and continuity.
  - `band_performance` — the storyboard has no narrative storyline. Every scene allocation is `performance`, focused on vocal/instrumental performance, performer or band-member coverage, shot scale, camera movement, physical intensity, staging, lighting, atmosphere, and musical pacing. Lyrics may guide performance intensity, expression, emphasis, and pacing but do not create narrative objects, events, symbolic reenactments, or unrelated story scenes.
- `story_brief`.
- `visual_notes`.

Default:

- `storyboard_mode = loose`.

The mode is creative storyboard direction and participates in the deterministic storyboard-request fingerprint. It is not the later per-scene generation-method choice. Phase 5 independently selects `keyframe_i2v` or `reference2video` for each scene.

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

When `storyboard_mode = band_performance`, response validation requires every scene allocation to use `scene_type = performance`.

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

The installed `MiniMaxH3ReferenceToVideo` contract currently exposes nine
finite still-image inputs, `ref_image_0` through `ref_image_8`. The provisional
production contract therefore limits a scene to nine ordered still references.
Phase 5 enforces this limit without truncating stored data, and scenes that
retain an over-capacity legacy selection remain not-ready until corrected.

---

## Stage G — H3 prompt generation

Final prompts branch by the scene's `generation_method`.

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

### Prompt-relay architecture

Prompt generation uses the following local, deterministic pipeline:

`deterministic method-specific compiler`
`-> optional manual Custom GPT relay`
`-> strict response validation`
`-> machine reassembly`
`-> editable final prompt`

The Builder never executes a language model, automates browser interaction, or
calls a cloud LLM API.  The optional Custom GPT relay is a user-operated copy
and paste workflow using a versioned request/response contract.  The H3 render
graph always receives a completed prompt string.

The deterministic compiler remains available as internal diagnostic context,
but a current validated Prompt Director response is required before a Final
Prompt can be saved as current.

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

The supplied and reviewed workflows below are reference donors.

They are not production workflows.

Do not embed them intact.

Do not reproduce their UI groups, notes, toggles or convenience infrastructure.

## Donor A — latest primary generation research donor

`minimaxH3T2VI2VREF2VAdvanced_v18`

Advanced v18 is the latest primary H3 research donor. Use it to refresh
qualification evidence for FL2VA/I2V, REF2VA, source-audio latent replacement,
sampling, resolution/frame calculations, and optional NVIDIA optimisation.

The current stripped Base production topology was derived from Advanced v16
and installed-node contracts. Advanced v16 remains historical Base-contract
derivation evidence. Newer optional nodes in v18 do not rewrite the hardened
Base manifests or templates.

Deferred target-NVIDIA candidates observed in v18 are:

- `minimax_h3_video_vae_int8_convrot.safetensors`, compared with the current
  provisional FP16 video VAE for visible quality, decode behaviour, VRAM,
  stability, and speed.
- `MiniMaxH3SigmaShift`, with observed video shift 12 and audio shift 6; no
  shift value is frozen until target qualification resolves differing donor
  evidence.
- `minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy_resized_avg_rank_21_bf16.safetensors`
  at observed strength 0.7 as the current preferred I2V Draft Turbo candidate.
- Additional REF2VA audio-reference inputs as research evidence only; MVP
  continues to expose one authoritative scene-audio source from the master
  song.

Do not adopt `LoadImageCrop` or `ModelPreviewOverrideKJ` into the Base graph.
Do not add Sigma Shift, the INT8 ConvRot video VAE, or the newer Turbo model as
current production requirements.

---

## Donor A historical evidence — Base-contract derivation

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
- `MiniMaxChunkFeedForward` as a deferred memory-management candidate. The
  observed donor values are 2 and 4096; compare Base with Base plus this node
  on the target NVIDIA host for completion, time, memory, and visible output
  equivalence. Do not add it to the Base template without qualification.

Do not adopt:

- Full EZ workflow UI.
- Generic mode switching.
- LTX refinement by default.
- RIFE by default.
- Its entire dependency set without evidence.

W8A8/INT4 diffusion remains fallback/research evidence only and is not Final.

---

## Donor C — quality upscale and memory research

`minimaxH3WithSEEDVR2Upscaler_v4`

Reference for:

- SeedVR2 video upscaling.
- Model unloading between H3 and upscale stages.
- 16 GB-class VRAM handling.
- H3 sigma-shift relationships if applicable.

Do not execute a language model as part of the render graph.

Do not retain seamless-loop or interpolation functionality unless explicitly added by Sol later.

`minimaxH3WithSEEDVR2Upscaler_v4` remains the SeedVR2 v4 upscale/memory
research donor. SeedVR2 is not part of the Base H3 generation templates and
remains conditional on target qualification.

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

`minimaxh3Auto_v5` is a prompt-grammar/research donor only. Do not inherit its
ComfyUI prompt-generation graph, Qwen3.6 GGUF or Qwen3.5 9B requirements, three-image helper
limitation, TextGenerate render nodes, rgthree switching, Pixaroma,
video-reference support, additional audio-reference UI, concept-expansion UI,
or EasyUse dependencies. Its useful contribution is structured H3 prompt
grammar and conventions, not a production workflow.

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
- Manual Custom GPT relay operation is outside the local runtime dependency scan.

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

- Persisted Story Direction with `storyboard_mode` values `loose`, `strict`, and `band_performance`; default `loose`.
- Story brief.
- Visual/directional notes.
- Storyboard request JSON.
- Copy.
- Response paste/import.
- Strict schema validation.
- Preview.
- Apply.
- Persistence.
- Visual/reference instructions that remain usable by either later generation method.

`storyboard_mode` participates in the request fingerprint and communicates the creative direction to the dedicated Storyboard Director. `band_performance` remains storyboard-level creative direction and requires performance-only scene allocations; it is not `generation_method`.

The ChatGPT storyboard relay does not own the final generation-method choice. Phase 5 independently chooses `keyframe_i2v` or `reference2video` per scene.

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

The per-scene Generation Method remains authoritative. Visuals also provides a convenience `Apply to All Scenes` action for setting one allowed generation method across the current scene list in one mutation. Bulk method changes are non-destructive: inactive keyframe and Reference-to-Video branch data remains stored.

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
- Deterministic planned mapping: `<Picture N>` follows selected-reference order; `<Subject N>` follows first occurrence of each distinct owning Character or Location, and multiple references from one owner share its Subject tag.
- Reference-to-Video readiness without requiring a keyframe.

Exact production mapping remains subject to Phase 6 qualification.

No H3 rendering yet.

Do not expose T2V or video-reference/motion-transfer UI.

---

## PHASE 6 — H3 donor qualification

This phase uses the reviewed donor set and installed-node contracts. The raw
v18, EZ v35, and Auto-Prompter v5 JSON files may not be present in every
development checkout; when unavailable, reviewer-audited findings are recorded
as research evidence and are not treated as a replacement for direct graph
inspection.

Luna must inspect actual graphs, not their Civitai descriptions.

Latest primary research donor:

`Advanced v18`

Historical Base-contract derivation evidence:

`Advanced v16`

Qualification and research donors:

- `EZ v3.5`
- `SeedVR2 v4`
- `All Inputs beta`
- `Auto-Prompter v5` — prompt grammar only; not a production H3 workflow donor.

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

#### Deferred target-NVIDIA acceptance gate

Real H3 generation and quality acceptance may be deferred while the target
NVIDIA host is unavailable. Development may continue on the AMD workstation
using:

- Structural workflow validation.
- Mocked or unit-tested queue behaviour where appropriate.
- Deterministic manifest patching tests.
- Backend and frontend tests.
- Non-generative ComfyUI API validation where safe.

Do not mark deferred runtime tests as passed. The following remain required
before Final Project Approval and must run on the target RTX 4080 SUPER:

- Real `keyframe_i2v` H3 generation.
- Real `reference2video` H3 generation.
- Final quality and acceleration selection.
- RTX VSR qualification if retained.
- Real single-scene render validation.
- Real mixed-generation-method batch validation.

The quality-first production candidates remain provisional until those target
NVIDIA gates are complete. Keep production graph settings manifest/config
driven. Graph structure and deterministic node mappings may be implemented
before the gate when donor evidence and installed-node contracts support them,
but AMD resource behaviour must not be used to downgrade the selected INT8
models or promote a lower-memory fallback.

#### Focused target-NVIDIA qualification shortlist

Do not benchmark every cross-product permutation. Sol selects final settings
after user quality review.

Final-quality and memory order:

1. Current Base INT8 with FP16 video VAE.
2. Compare FP16 video VAE with the v18 INT8 ConvRot video VAE candidate.
3. Compare Base with Base plus `MiniMaxChunkFeedForward`.
4. Compare Base with Base plus `MiniMaxH3SigmaShift`.
5. Combine only individually successful and valuable optimisations if warranted.

Draft order:

6. Qualify the newer v18 resized-average Rank-21 LightX2V I2V Turbo candidate.
7. Qualify the appropriate REF2VA Turbo candidate.
8. Qualify Sage only if it remains credible and compatible at test time.

Upscale order:

9. RTX VSR.
10. SeedVR2 only if it becomes available and remains worthwhile.

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

The Phase 6D Base contract may establish the stripped I2V and REF2VA topology,
deterministic node mappings, ordered REF2VA picture capacity, source-audio
replacement, decode, and output mechanics before target-NVIDIA qualification.
This establishes the structural Base contract; it does not freeze target-NVIDIA
quality or acceleration topology. Final Base settings, Turbo Draft, Sage/Sol or
other acceleration, any alternative acceleration template, final resolution,
and RTX VSR remain deferred and manifest/config driven. If target qualification
requires acceleration, add a separately approved workflow/template rather than
altering the stripped Base contract.

The 6D.3 donor refresh does not change the Base production topology, manifest
registry, or workflow templates. Advanced v18, EZ v3.5, SeedVR2 v4, All Inputs
beta, and Auto-Prompter v5 contribute deferred qualification or research
evidence only.

---

## PHASE 7 — Requirement checker + H3 prompt service

Implement:

- Production-workflow dependency scan across both approved generation-method manifests.
- Required model scan for both supported methods.
- FFmpeg scan.
- Optional RTX VSR scan.
- Optional SeedVR2 scan.
- Keep the requirements checker as an internal backend service rather than a
  permanent Setup dashboard. Later render preflight may consume it to report
  actionable missing-node, missing-model, and tool diagnostics at the point
  they matter.
- A deterministic method-specific H3 prompt compiler as the authoritative
  machine-fact layer and diagnostic preview.
- A mandatory manual Prompt Director relay after deterministic compilation,
  with strict response validation and machine-owned structural reassembly.
- A user-editable Final Prompt derived from a current validated Prompt Director
  response. The deterministic preview is not a user-selectable final-prompt
  path.
- The Prompts workspace is the Phase 7 user-facing prompt-relay surface; the
  requirements checker remains an internal backend service for future render
  preflight and actionable diagnostics.
- Schema version 7 prompt persistence, with method-specific prompt state for
  every current scene: `keyframe_i2v` and `reference2video` each store exactly
  `final_prompt`, `source_fingerprint`, and `relay_fingerprint`.
- Computed `NEEDS GPT`, `UNSAVED`, `CURRENT`, and `STALE` prompt status.
  A Final Prompt is render-ready only when the saved source and relay
  fingerprints both match the current deterministic source fingerprint and no
  unsaved local edit exists. Readiness remains derived rather than persisted.
- Narrow schema-v6 normalization preserves its existing prompt text and source
  fingerprint while initializing `relay_fingerprint` to empty. It does not
  infer Prompt Director provenance or mutate a project merely by listing or
  loading it. Existing v1-v5 compatibility continues to normalize through the
  established empty/current prompt-state rules.

Do not report dependencies inherited from unused donor branches.

The Phase 7 prompt pipeline is:

`deterministic method-specific H3 prompt compiler`
`-> mandatory Prompt Director relay`
`-> strict response validation`
`-> machine reassembly`
`-> user-editable final authoritative prompt`

The saved Final Prompt is authoritative only after a current Prompt Director
response has been applied and the user explicitly saves it. Applying a valid
response records its request fingerprint as relay provenance and produces an
`UNSAVED` draft, including when its text happens to equal previously saved
text but the prior record lacked provenance. Saving cannot acknowledge a stale
prompt: when authoritative scene inputs change, a fresh request and matching
Prompt Director response are required before the Final Prompt can be saved as
`CURRENT`. Prompt state is saved through a method-scoped,
current-fingerprint-checked mutation; it is not inferred from deterministic
previews.

The Prompt Director receives a lean, deterministic JSON payload of
creative context and selected owner facts. It does not receive the assembled
deterministic prompt or the completed six-section/I2VA wrapper. For
`reference2video`, the relay may use exact supplied `<Subject N>` tags naturally
in bounded creative prose, but the Builder owns Subject numbering, Picture
ownership, retention, Audio, section headings, `[Shot 1]`, and final machine
reassembly. Exact supplied lyric text is also machine-owned: the relay may use
the lyric as read-only performance context and describe delivery, breath,
expression, and movement, but it must not repeat or quote the lyric in
`enhanced_description`. The accepted response shape is strict bounded response
JSON, not a copy of the request; embedded quotation marks in JSON strings must
be escaped. Applying a response does not save a Final Prompt by itself.

The deterministic compiler must supply for both generation methods:

- Exact scene duration.
- Generation method.
- Storyboard action.
- Camera direction and motion direction.
- Continuity context.
- The authoritative source-audio relationship.
- Reference ownership and numbering where applicable.
- Timestamps only where the storyboard establishes a structurally warranted
  timed change.

For `reference2video`, the compiler must additionally supply ordered
`<Picture N>` and `<Subject N>` relationships, Character/Location ownership,
stable Subject reuse for multiple Pictures from one owner, and the
`<Audio 1>` relationship. The Prompt Director may improve bounded wording
and detail but must not invent, renumber, or reassign these structural facts.

The canonical REF2VA Final Prompt contains exactly these six sections in this
order, with each heading on its own line and exactly one blank line between
sections:

- `subject_definitions` — one natural machine-generated definition per Subject,
  combining its Character or Location identity, ordered Picture ownership, and
  relevant continuity context. Its Audio definition states that `<Audio 1>` is
  the authoritative master-song scene-audio segment supplied for the target
  video; `fully_copy` belongs only in retention analysis.
- `summary` — concise generation intent beginning with the canonical
  `[reference generation + audio reuse]` task prefix.
- `retention_analysis` — explicit preservation relationships for supplied
  reference assets using canonical entries such as
  `<Subject N> (appears in [Shot 1]): fully_preserved - ...` and
  `<Audio 1>: fully_copy - ...`.
- `detailed_description` — a Builder-owned grounded visual/style opening,
  followed by a blank line and one Builder-owned `[Shot 1]` wrapper. The
  validated Prompt Director creative prose is inserted intact after that
  wrapper. One scene remains one continuous shot unless authoritative project
  data explicitly establishes another shot.
- `overall_soundscape` — only relevant diegetic/environmental sound
  requirements.
- `non_diegetic_music` — the supplied authoritative scene-audio relationship.

For `keyframe_i2v`, the Builder uses I2VA semantics because it supplies one
authoritative accepted first frame. The final assembled prompt begins with:

`For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.`

After exactly one blank line it uses
`integrated_multimodal_description: [Shot 1] ...`, then
`overall_soundscape:` and `non_diegetic_music:` in that order, with exactly one
blank line between the three fields. `<Picture 1>` is the accepted keyframe;
its actual description owns the visible opening state. The Builder owns the
opening sentence, field labels, `[Shot 1]` wrapper, duration, opening authority,
soundscape, and music/audio relationship. Validated Prompt Director prose is
inserted intact only into the integrated description after the machine-owned
opening-state and continuity boundaries. The prompt does not add REF2VA
Subject/Audio labels.

For a visible performer singing supplied lyrics, the compiler may include the
exact supplied lyric in the performance context using a stable speaker ID and
MiniMax dialogue syntax such as `Vesper (S1) sings: <d>[English] ...</d>`.
Narrative lyrics are not turned into dialogue, and instrumental scenes receive
no invented vocal content. In REF2VA `retention_analysis`, Character and
Location Subjects use the machine-owned `fully_preserved` relationship and
the authoritative `<Audio 1>` uses `fully_copy`.

The master song remains authoritative. The prompt system must not invent
lyrics, dialogue, vocal words, soundtrack, music genre, or an extra score
unless those facts are present in authoritative project data. For ordinary
Vesper music-video scenes, `non_diegetic_music` references the supplied scene
audio rather than hallucinating replacement music; final audio replacement or
remux remains Phase 8.

One Builder scene is one continuous H3 shot by default. Do not invent multiple
shots to fill a prompt template. A cut, transition, or timed state change may
justify timestamps only when storyboard data explicitly establishes it; small
camera-position changes remain camera movement.

The Prompts workspace presents each scene as a compact linear relay. A readable
instruction precedes the Paste GPT Response field, followed immediately by one
ordered, wrapping action row: Generate GPT Request, Copy Request JSON, Open GPT,
and Apply GPT Response. For a `NEEDS GPT` scene, `Final Prompt` remains hidden
until a current response is applied. It is then user-editable and explicitly
saveable; current and stale saved prompts remain visible with their appropriate
status guidance. `Prompt Context` follows at the bottom with collapsed
Reference Mapping, Request JSON, and deterministic-prompt disclosures.

Project persistence is a durability contract, not a prompt or render-readiness
gate. Schema-v7 projects may be saved and reopened with `NEEDS GPT`, empty prompt
records, stale prompts, incomplete Visuals, or other valid work-in-progress
state. Only the method-scoped Save Prompt mutation requires a non-empty canonical
Final Prompt and current relay provenance. Relevant saved Storyboard, resource,
or Visuals edits may therefore make a previously current prompt stale while
preserving its text; render preflight remains responsible for rejecting
incomplete or stale work before rendering.

The UI must not describe the Prompt Director as optional, expose a
`Use Deterministic` action, or label the editor `Final H3 Prompt`.

Prompt freshness has exactly one authoritative source. Per scene and generation
method, the deterministic source fingerprint is computed once from the
method-specific authoritative basis (exact scene source/duration, scene type,
action, visual instructions, camera, motion, continuity, lyric/performance
context, relevant Character and Location facts, and — per method — the accepted
keyframe identity plus its actual description, or the ordered selected
references with Picture-to-Subject ownership). Saved-prompt status, generated
request freshness, pasted-response rejection, Save Prompt eligibility, inline
stale notices, and render readiness all derive from that one fingerprint and
the saved `source_fingerprint`/`relay_fingerprint` provenance pair, graded as
current, stale, or missing. Only scenes whose relevant inputs changed become
stale; unrelated scenes and unrelated entity edits do not.

Internally, source/relay validity is kept separate from editor state. The
single visible badge uses the precedence `STALE` over `UNSAVED` over `CURRENT`
over `NEEDS GPT` whenever a Final Prompt or draft exists, because a stale
unsaved draft cannot be saved against changed scene inputs. A stale unsaved
draft is retained non-destructively, its Save Prompt control stays disabled,
and a fresh Prompt Director relay is required. An unsaved draft that carries a
current relay provenance against the current source remains `UNSAVED` and
saveable; that is the recovery path after an upstream edit. Ordinary Save
Project never stamps prompt provenance: only the explicit method-scoped Save
Prompt mutation persists current fingerprints. Transition flushes never block
on a stale draft that can no longer be saved.

The production I2VA Final Prompt reads as natural audiovisual generation prose.
Its integrated description opens with a concise accepted-first-frame anchor
derived from the actual keyframe description, continues only with useful
identity, wardrobe, and environment continuity, then carries the exact
machine-owned vocal line where appropriate and the validated Prompt Director
prose. Internal compiler invariants — opening-authority rules, exact duration,
keyframe reference restatements, continuity-context labels, forward-generation
intent, lyric-handling guidance, fingerprints, and other diagnostic prose —
stay inside the compiler, relay request, and validators; they must not appear
in the production prompt. Removing that prose does not weaken authority: the
accepted keyframe and its actual description remain authoritative for frame
zero, and when storyboard or entity context conflicts with the accepted image,
the accepted image wins.

The Prompts workspace renders flicker-free. State refreshes update the existing
scene cards narrowly — badge, button states, notices, request JSON state, Final
Prompt visibility, editor value, and Save state — instead of rebuilding the
stage. Cards, textareas, disclosures, focus, caret, internal scroll, page
scroll, and disclosure state survive Generate, Apply, Save, and status
refreshes. A legitimate Final Prompt reveal inserts its section atomically;
silent refreshes keep visible cards on screen until fresh data lands;
same-project saves never render a blank intermediate prompt list.

---

## PHASE 8 — Single-scene rendering

### Phase 8A — Render preflight, media preparation, and workflow compilation

Phase 8A starts from the accepted Phase 7 checkpoint SHA
`1a3593c6be52e8f1abb99f90443cc34e36e9e00e`.

This structural round adds the real Render stage and its single authoritative,
read-only render-preflight contract. Preflight reports project identity,
requirements, per-scene method and exact master-audio boundaries, prompt
freshness, Visuals readiness, source-audio readiness, workflow-contract
readiness, runtime requirements, preparation readiness, stable blockers, and
warnings. Scene readiness is independent: an incomplete scene does not gate an
otherwise eligible scene. Phase 7 prompt freshness remains authoritative,
including `CURRENT`, `NEEDS GPT`, `UNSAVED`, and `STALE` with stale precedence.

Phase 8A prepares project-owned scene audio, accepted keyframe or ordered
REF2VA references, timing metadata, immutable manifest-compiled workflows, and
durable preparation fingerprints. The H3 timing contract is 24 fps with the
approved `5 + 17n` frame grid; exact source-audio duration remains authoritative
and any generated-duration mismatch is explicit. Preparation is a dry boundary:
it validates prerequisites and writes local inputs/metadata, but never submits a
ComfyUI queue, calls `/prompt`, invokes H3, downloads models, or exposes a fake
render-execution control. The project remains schema v7 and uses the existing
project-owned `scene_audio`, `renders`, and `export` directories.

Content readiness, workflow readiness, runtime requirements, and preparation
freshness are reported separately from an informational multi-GPU execution
policy. Scene execution eligibility depends only on the real current scene
dimensions and active-job protections. AMD RX 7900 XT and RTX 4080 SUPER 16 GB
are both valid H3 execution targets; RTX is preferred only when faster
throughput is useful.

Manual contracts accepted for the current Phase 8A implementation:

- MT-52 — Render Preflight / Scene Readiness — PASS
- MT-53 — Scene Media Preparation — PASS
- MT-54 — Production Workflow Dry Compilation — PASS
- MT-55 — Preparation Invalidation / AMD-Safe Boundary — PASS

### Phase 8A.1 — Required reference synchronization and Visuals guardrails

When a Storyboard is applied, requiredness is derived at the owner/resource
level from the current required Character and Location owners. Every current
image under each required owner is automatically synchronized into the
affected Visuals scene as the authoritative locked baseline. Required images
retain Storyboard owner order and entity reference order, are visibly
identified, and cannot be removed or reordered through normal Visuals actions.
Optional Reference-to-Video references remain supported after the complete
required group, retain their relative order, and remain subject to the
existing nine-reference cap.
Re-apply is non-destructive: references that are no longer required remain
selected as optional references. The synchronized selections use the existing
schema-v7 Visuals persistence model and do not make the freshly applied
Storyboard stale. Keyframe / Image-to-Video continues to use the accepted
keyframe as the sole H3 Picture 1 / first-frame authority; synchronized
Storyboard references provide context only and are not additional H3 inputs.

Manual contract:

- MT-56 — Required Storyboard Reference Synchronization — PASS

### Phase 8A.3 — Authoritative required-reference reconciliation

Required Visuals selection is canonical derived state, not a second user decision.
The authoritative scene reconciler derives the locked required block from the
current applied Storyboard's first-seen owner order, then expands each required
owner in its current entity-reference order. It self-heals missing, late,
misordered, and duplicate selections on Storyboard Apply, project load/reopen,
Visuals initialization, resource changes, re-apply, and migration without adding
per-image required schema state. The complete required block always precedes the
optional block; optional references retain their relative order and remain the
only references users may add, remove, or reorder within the existing nine-image
Reference-to-Video limit.

Normal Visuals validation therefore reports integrity failures only: unresolved
owners, resources, assets, corrupt selectors, an impossible required set, or a
truthful over-capacity blocker. Ordinary UI actions cannot create missing-required
selection or required-order errors. Reconciliation is idempotent, leaves an
already-applied Storyboard current, and only changes downstream Prompt and Render
preparation freshness when the canonical Visuals mapping actually changes. The
accepted keyframe remains the sole H3 Picture 1 / first-frame authority for
Keyframe / Image-to-Video.

Manual contract:

- MT-56 — Required Storyboard Reference Synchronization — PASS

### Phase 8A.4 — Preflight performance and runtime requirements cache

Render preflight separates dynamic scene correctness from stable runtime
requirements discovery. Prompt freshness, Visuals selection/readiness, source
file existence, and preparation fingerprints are evaluated on every refresh;
the authoritative runtime requirements scanner remains fail-closed but its
successful process-local snapshot is reused while its versioned manifest and
runtime identity remain unchanged.

Normal refreshes use the warm snapshot. A visible Rescan Runtime action forces
a fresh discovery pass, process restart clears the cache, and runtime
manifest/version changes invalidate it. Failed scans are not retained as
successful cache entries, and identical concurrent scans coalesce so a slow
runtime inspection does not multiply. File identity hashing is reused only
when the file size, modification time, and inode signature are unchanged;
dynamic readiness and preparation correctness are never replaced by a full
preflight cache. No project schema or persisted cache state is introduced.

The Render UI prevents duplicate refresh requests, distinguishes runtime
inspection from scene-readiness refresh, reuses stable scene cards, and
preserves content scroll position.

Manual contract:

- MT-57 — Preflight Performance and Requirements Cache — PASS

### Phase 8B — Queue job lifecycle, progress, recovery, and output discovery

Phase 8B adds the real local execution boundary above the accepted Phase 8A
preparation package. A centralized local ComfyUI adapter uses the installed
core `/prompt`, `/queue`, `/interrupt`, and `/history` API contract. Builder
job IDs are distinct from ComfyUI prompt IDs; job records are atomically
persisted under the project-owned `renders/<scene>/jobs` workspace and do not
change schema v7.

The durable job state machine distinguishes READY_TO_SUBMIT, SUBMITTING,
QUEUED, RUNNING, SUCCEEDED, FAILED, CANCEL_REQUESTED, CANCELLED,
INTERRUPTED, and UNKNOWN / reconciliation required. Illegal transitions and
terminal mutation are rejected. A retry preserves the old record and creates
both a new Builder job ID and a new ComfyUI prompt ID. Submission failures are
separate from execution failures. Queue/history reconciliation is one
coordinator per project refresh, uses bounded local requests, preserves the
last known state across transient polling failures, and never fabricates a
numeric progress value. Restart recovery treats a missing prompt ID or a
prompt absent from both queue and history as UNKNOWN rather than success.

The execution eligibility gate is checked before any `/prompt` request. It
admits a scene only when content, workflow, runtime requirements, and current
preparation are ready; GPU vendor is not an admission condition. AMD and RTX
therefore use the same production workflow and quality settings. Queued
cancellation uses the owned prompt ID for queue deletion. Running cancellation
uses the installed global engine interrupt API targeted by that prompt ID and
is represented with that scope explicitly.

Successful output association is derived only from the prompt-owned ComfyUI
history entry, the manifest-declared production output node, and the expected
deterministic filename prefix. Missing, ambiguous, unsafe, or wrong-type
outputs fail closed. The associated file is the raw H3 output; trimming,
remuxing, authoritative-audio replacement, upscaling, final-duration
correction, export assembly, and batch lifecycle remain later work.

The Render card exposes compact lifecycle state, guarded cancel/retry actions,
reconciliation-required status, raw output association, and a non-blocking
hardware performance note without scroll jumps or fake deferred controls.

Manual contracts for this structural round:

- MT-58 — Render Execution Gate / AMD Safety — SUPERSEDED_PRODUCT_DECISION
- MT-59 — ComfyUI Queue Submission Contract — FAIL / PENDING_RETEST
- MT-60 — Render Job Lifecycle / Recovery — PASS_STRUCTURAL
- MT-61 — Failure / Retry / Cancellation — PASS_STRUCTURAL
- MT-62 — Raw Output Discovery / Association — BLOCKED_BY_MT59 / PENDING_LIVE_RENDER

### Phase 8C — Raw output finalization, exact timing, and authoritative audio

Phase 8C consumes only a successful Phase 8B raw H3 output. It keeps the raw
artifact immutable and creates a separate project-owned final scene artifact
under `renders/<scene>/final/<job_id>.mp4`, with durable finalization metadata
beside it. Finalization does not mutate the project document, Prompt provenance,
preparation metadata, or the raw Builder job record.

The installed H3 node contract was audited directly: its `temporal_shape()`
uses `frame_count / 24` for generated video duration and its
`align_frame_count()` rounds upward until `frame_count % 17 == 5`. The accepted
frame planner therefore chooses the smallest valid `5 + 17n` count whose
nominal generated duration is at least the exact scene duration. The previous
6679 ms example's 158 frames represented 6583.333 ms and was unsafe; the
corrected plan is 175 frames representing 7291.666667 ms, followed by a
precise finalization trim. The planner retains the 24 FPS, minimum 5, step 17,
and maximum 3600 contract and fails closed when the valid grid cannot cover the
target.

Raw media is inspected with bounded JSON `ffprobe` calls. A valid raw result
must contain a 24 FPS video stream with usable dimensions and duration;
container duration, frame count, time base, codec, and any raw audio streams
are recorded. Raw audio is never authoritative. A video shortfall fails with
`RAW_VIDEO_TOO_SHORT` rather than speed modification, interpolation, or freeze
padding. The explicit timing tolerances are one 24 FPS frame for video and
container boundaries, one source audio sample for prepared PCM, and two 1024
sample AAC frames for final encoded-audio priming/padding.

Final scene media is encoded deterministically with installed FFmpeg using
libx264, medium preset, CRF 18, yuv420p, 24 FPS, AAC-LC at 256 kbps, and no
filters, scaling, interpolation, normalization, gain, or tempo changes. The
video is mapped from the raw H3 input and the audio is mapped from the exact
Phase 8A prepared scene-audio artifact. The candidate is ffprobe-validated
before atomic promotion; a failed replacement retains any prior valid final
artifact. Finalization fingerprints include the raw job/output identity,
preparation fingerprint, exact target duration, authoritative audio identity,
and encode policy. Finalization states are NOT_AVAILABLE, RAW_READY,
FINALIZING, FINALIZED, STALE, and FAILED. Re-finalization is idempotent for
unchanged inputs, while raw, preparation, audio, or policy changes make the
old final stale.

The Render card distinguishes historical/current RAW H3 OUTPUT from FINAL
SCENE state. `Finalize Scene` is available for a current successful raw job
without requiring a different GPU vendor. Synthetic FFmpeg media qualifies the
post-processing path without running H3.

Manual contracts for this round:

- MT-63 — Post-Processing Eligibility / UI — PASS
- MT-64 — Exact Scene Finalization / Authoritative Audio — PASS_STRUCTURAL
- MT-65 — Finalization Invalidation / Idempotence — PASS_STRUCTURAL
- MT-66 — Frame-Plan Coverage — PASS

MT-59 remains pending retest and MT-62 remains blocked by MT-59 pending live-render evidence. Upscale, Resolve package
and export, and complete-video assembly remain later work.

### Phase 8C.1 — Multi-GPU execution eligibility and frame-plan UX

The AMD RX 7900 XT is the primary and fully valid development and functional H3
execution machine. The RTX 4080 SUPER 16 GB is a second fully valid execution
machine that is expected to provide faster throughput. GPU vendor is never a
hard Render or queue gate, and no quality downgrade or alternate production
workflow is selected for AMD. Actual content, workflow, runtime, preparation,
duplicate-job, stale-input, and ComfyUI runtime failures remain blocking and
truthful on either platform.

The Render preflight exposes one per-scene `execution_eligibility` decision
based on content readiness, workflow readiness, runtime requirements, and
current preparation. The top-level execution policy is informational only.
Eligible scenes expose `Render Scene` and may reach the existing Phase 8B queue
adapter. The Render card presents the H3 plan concisely in seconds plus any
required finalization trim while preserving the exact internal 24 FPS `5 + 17n`
timing contract and authoritative audio duration.

MT-58 is superseded by this product decision. MT-59 and MT-62 are pending live
render evidence on the AMD development machine; MT-60 and MT-61 remain
structurally accepted. MT-63 is PASS, MT-64 and MT-65 are PASS_STRUCTURAL, and
MT-66 is PASS. The new cross-hardware eligibility contract is:

- MT-67 — Cross-Hardware Render Eligibility — PASS

This round does not run H3, submit a live queue job, begin Phase 8D, or change
the Phase 8B lifecycle and Phase 8C finalization boundaries.

### Phase 8C.2 — Live ComfyUI submission validation diagnostics

Live ComfyUI prompt validation is authoritative over the Builder's static
template/manifest contract. The submitted payload was confirmed to be the
correct API shape, `{ "prompt": <API-format node graph> }`, without a UI
workflow, manifest, or preparation wrapper. The failed REF2VA package was also
confirmed to contain API nodes with `class_type` and `inputs`; no production
model, quality, timing, or graph setting required correction.

The exact live rejection was file materialization. ComfyUI's validation log
identified LoadAudio node 7 input `audio` and LoadImage nodes 50 through 53
input `image`. Their submitted values were the deterministic
`music_video_builder/<project>/<scene>/scene_audio.wav` and
`picture_01.png` through `picture_04.png` selectors. Installed LoadAudio and
LoadImage validation resolves those selectors only below ComfyUI's
authoritative input root. Phase 8A had copied the files into the project-owned
render workspace but had not staged matching copies below that runtime input
root, so all five selectors were invalid before queue admission.

Preparation version 3 retains the project-owned artifacts and atomically
stages audio and visual inputs into the fixed
`ComfyUI/input/music_video_builder/<project>/<scene>/` namespace. Runtime asset
size/hash records make missing or changed staging copies stale, requiring
re-preparation; required assets are never truncated or substituted. The
existing API workflow selectors become valid without changing the immutable
Phase 6 templates, manifest mappings, production model choices, or shared AMD
and RTX quality policy.

Before queue submission, Builder now performs read-only, bounded, short-lived
cached `/object_info/<node_class>` inspection. It verifies API graph shape,
registered classes, input names, required inputs, detectable scalar types, and
current enum/file/model selector values. This inspection never uses `/prompt`
as a validation surrogate. Non-2xx ComfyUI responses preserve bounded parsed
`error` and `node_errors` data. HTTP 400 validation rejection is persisted as
`submission_failed_validation`, with no ComfyUI prompt ID, queue/running state,
polling, or raw-output lookup. The main UI shows a concise affected
node/input/value summary and provides a bounded diagnostic disclosure; later
execution failures remain a distinct category.

Manual contracts after this structural remediation:

- MT-59 — ComfyUI Queue Submission Contract — PENDING_RETEST
- MT-60 — Render Job Lifecycle / Recovery — PASS_STRUCTURAL
- MT-61 — Failure / Retry / Cancellation — PASS_STRUCTURAL
- MT-62 — Raw Output Discovery / Association — BLOCKED_BY_MT59 / PENDING_LIVE_RENDER
- MT-63 — Post-Processing Eligibility / UI — PASS
- MT-64 — Exact Scene Finalization / Authoritative Audio — PASS_STRUCTURAL
- MT-65 — Finalization Invalidation / Idempotence — PASS_STRUCTURAL
- MT-66 — Frame-Plan Coverage — PASS
- MT-67 — Cross-Hardware Render Eligibility — PASS
- MT-68 — ComfyUI Validation Error Diagnostics — PASS_STRUCTURAL

The implementation round performs no live `/prompt` request, H3 execution, or
Phase 8D work. MT-59 requires one manual re-prepare and Render Scene retry;
MT-62 remains blocked until that retry yields a successful raw H3 output.

### Phase 8C.3 — Dynamic ComfyUI node-input compatibility

The Phase 8C.2 live validator's flat input-name comparison produced a false
incompatibility for prepared VHS_VideoCombine node 21. The production node
selects `format = video/h264-mp4`; the installed VideoHelperSuite runtime
defines format-dependent widgets through nested metadata on the static
`format` option rather than as flat `required` or `optional` inputs. For the
selected production format, the resolved dynamic contract is:

- `pix_fmt`: `yuv420p` or `yuv420p10le`;
- `crf`: integer from 0 through 100;
- `save_metadata`: boolean;
- `trim_to_audio`: boolean.

The prepared production values remain valid: `pix_fmt = yuv420p`, `crf = 16`,
`save_metadata = false`, and `trim_to_audio = false`. The immutable Phase 6
workflow, manifest, frame rate, format, and quality settings therefore remain
unchanged. This is a validator-only remediation and preparation version 3
remains current; users do not need to re-prepare solely for this fix.

Live compatibility resolution now combines static required, optional, and
hidden inputs with dynamic widgets resolved from the node's actual selected
option. The resolver is generic over nested selected-option metadata and is
computed per node configuration, so two nodes of the same class may expose
different accepted input sets. The short-lived raw object-info response may
still be cached per class; selected-option resolution is never cached as one
global class-wide input set.

Input assessment has three outcomes: `SUPPORTED`, `UNSUPPORTED`, and
`NOT_DETERMINABLE`. Only a definitive `UNSUPPORTED` result blocks submission.
Malformed or insufficient dynamic metadata does not turn absence from flat
object-info keys into a false rejection; ComfyUI `/prompt` remains the final
authoritative validation boundary. Definitive selected-format mismatches, such
as an unsupported pixel-format value or a widget belonging only to another
format, still fail before queue submission with the exact node and input.

MT-59 remains PENDING_RETEST. Refresh Preflight, retain the current preparation,
and click Render Scene once; a valid graph should now reach `/prompt`, while any
new ComfyUI validation rejection remains separately actionable. MT-62 remains
BLOCKED_BY_MT59 until a real H3 job produces discoverable raw output. This
round performs no live `/prompt` request, queue submission, H3 execution, or
Phase 8D work.

### Phase 8C.4 — LoadImage input materialization compatibility

Execution-media paths and node-widget selectors are separate contracts. Phase
8 preparation owns a source-preserving execution copy beneath the active
ComfyUI input root and compiles an input-root-relative selector into each
LoadImage node. The active Comfy Desktop root for the reported node-50 failure
was resolved through the runtime as
`C:\Users\HighStreet\AppData\Local\Comfy-Desktop\ComfyUI-Shared\input`.
All four prepared pictures and scene audio existed there beneath the
deterministic project/scene namespace, and their sizes and SHA-256 identities
matched preparation metadata exactly.

The installed LoadImage contract is intentionally asymmetric. `INPUT_TYPES()`
uses non-recursive `os.listdir(input_dir)`, so nested selectors do not appear in
its root-level image-discovery combo. `load_image()` resolves the submitted
selector with `folder_paths.get_annotated_filepath()`, while
`VALIDATE_INPUTS()` accepts it when `exists_annotated_filepath()` succeeds. The
installed ComfyUI prompt validator skips ordinary combo membership for inputs
owned by a custom `VALIDATE_INPUTS` argument and then calls that custom
validator. The observed nested selector was absent from the discovery list but
returned `True` from the installed LoadImage `VALIDATE_INPUTS`; it is therefore
runtime-valid and must not be flattened.

Live compatibility now treats an option list carrying explicit upload-widget
metadata such as `image_upload = true` as an open discovery snapshot rather
than a definitive closed enum. A submitted value present in the list is
`SUPPORTED`; a value outside it is `NOT_DETERMINABLE` and reaches ComfyUI's
authoritative `/prompt` validation. Closed enums, including model selectors,
remain fail-closed and still produce `UNSUPPORTED` for values outside their
active options. This removes the node-50 false positive without globally
weakening enum validation or introducing a LoadImage/node-ID allowlist.

No execution materialization or workflow value changed. Preparation version 3,
its fingerprint, deterministic nested project/scene selectors, atomic copies,
path-containment checks, REF2VA Picture order and Subject ownership, and the I2V
accepted-keyframe Picture 1 contract remain current. Audio retains its existing
LoadAudio selector contract. A cross-version root-level fallback is therefore
not activated for this installed runtime; it remains the required policy only
if a future active LoadImage contract definitively rejects safe existing nested
selectors.

MT-59 remains PENDING_RETEST. Reload if required, Refresh Preflight, retain the
current preparation, and click Render Scene once. MT-62 remains BLOCKED_BY_MT59
until a real H3 job produces discoverable raw output. This round performs no
live `/prompt` request, queue submission, H3 execution, or Phase 8D work.

Before batch work begins, one real H3 scene must pass in each supported generation method:

- Keyframe / Image-to-Video.
- Reference-to-Video.

---

### Phase 8C.5 — Live ComfyUI progress telemetry

Render jobs retain the HTTP `/queue` and `/history` contract as the durable
authority for queued, running, terminal, recovery, cancellation, and output
states. A single shared Builder-owned local WebSocket session is used only for
volatile prompt-correlated telemetry. Its generated session ID is submitted as
the installed ComfyUI top-level `client_id`, and event messages are accepted
only for prompt IDs owned by Builder jobs. Foreign prompts, malformed frames,
binary preview data, and prompt-less `status` messages cannot alter a job.

The adapter consumes the installed `execution_start`, `execution_cached`,
`executing`, `progress`, `progress_state`, `executed`, `execution_error`,
`execution_interrupted`, and `execution_success` messages. It normalizes only a
finite node-local `value/max` pair with a positive maximum, clamps the visible
fraction to 0–100%, and never labels node-local progress as whole-workflow
completion. The current executing node is mapped to a compact manifest/class
purpose stage such as Loading inputs, Generating video, Decoding output, or
Encoding output. A new node clears the previous node's numeric display until a
new reliable pair arrives, so resets remain truthful RUNNING state.

The active installed runtime evidence was inspected directly: `ComfyUI/main.py`
sends `progress` with `value`, `max`, `prompt_id`, and `node`;
`ComfyUI/comfy_execution/progress.py` sends `progress_state` with a prompt ID
and per-node value/max/state records; `ComfyUI/execution.py` sends the
execution-start/cache/current-node/executed/error/interrupted/success events;
and `ComfyUI/server.py` copies the `/prompt` top-level `client_id` into the
execution session that receives those events. `progress_state` is therefore
treated as node-local installed-runtime state, not as an aggregate graph
percentage.

The connection reconnects after transient disconnects and server restarts;
disconnect is not a failure and retained telemetry resumes when the same owned
prompt session is available. Success, failure, interruption, and Builder
cancellation clear the volatile snapshot. Telemetry ticks are not persisted in
project or job JSON. HTTP reconciliation overlays the latest snapshot in the
Render response without mutating durable job records, and the UI updates only a
stable telemetry subtree so narrow cards and scroll position remain steady.

MT-69 — Live Render Progress Telemetry — PENDING. This round adds exact
installed-event fixtures, prompt isolation, normalized node-local progress,
stage mapping, reset and terminal handling, reconnect behavior, shared-session
identity, volatile-overlay, and narrow Render DOM regressions. It does not
submit another H3 job, execute H3, or begin Phase 8D.

---

### Phase 8C.6 — Job reconciliation and telemetry-deadlock remediation

The durable render lifecycle is reconciled through one HTTP-authoritative path:
ComfyUI `/queue` establishes queued/running evidence and `/history/{prompt_id}`
establishes terminal success, failure, or interruption. History terminal results
take precedence over stale WebSocket observations; a confirmed queue-running or
queue-pending prompt remains truthful even when history is temporarily absent or
no telemetry event has arrived. A prompt absent from both queue and history
becomes `UNKNOWN` / `RECONCILIATION_REQUIRED` only after those durable sources
actually provide absence, and a history transport failure preserves the last
durable state for bounded retry.

WebSocket telemetry remains supplementary, prompt-correlated, volatile display
data. Re-registering an existing prompt does not manufacture an observed
`RUNNING` snapshot, telemetry freshness prevents stale numeric progress from
being displayed, and terminal HTTP reconciliation clears live telemetry. The
frontend keeps one bounded HTTP polling coordinator active for queued, running,
or unreconciled jobs; it does not wait for WebSocket replay or create a second
poll loop. Durable job state is never `WAITING_FOR_TELEMETRY`, and the UI no
longer presents `UNKNOWN` / reconciliation-required beside a telemetry-only
`RUNNING` panel. Render submission remains disabled for any possible active or
unreconciled job, while terminal recovery updates the controls without a new
H3 submission.

Historical ComfyUI success automatically retries the existing raw-output
association during reconciliation. If output discovery fails, the job remains
terminal `SUCCEEDED` at the ComfyUI execution layer and records a separate
`output_discovery` failure; it is never regressed to `RUNNING` or misreported as
an execution failure. No prompt, queue, H3, preparation, frame-plan, or
workflow-quality change is made by this remediation.

MT-62 — Existing Render Job Recovery — PENDING. Reload if required, open Render,
and Refresh Preflight once; recover the existing prompt from queue/history and
discover its raw H3 output without rerendering if the historical execution
succeeded. MT-69 — Live Render Progress Telemetry — PENDING_RETEST. This round
does not submit another H3 job, execute H3, or begin Phase 8D.

---

### Phase 8C.7 — Orphaned-job recovery and deterministic output fallback

The persisted job `9c4e6fad-49b2-41aa-8805-ffce8648d174` for scene
`002fc648-3024-472e-8753-895dec21c1c5` was inspected without modifying the
project, job record, prepared workflow, or ComfyUI output. Its durable state was
`UNKNOWN` with `RECONCILIATION_REQUIRED`, and the installed ComfyUI log recorded
an explicit cancellation followed by `Processing interrupted` for prompt
`267dcce2-b1fe-4109-a2ad-23d84de8e755`. The queue and prompt-history endpoints
were unavailable during inspection (`127.0.0.1:8188` refused both requests),
and no output matched the persisted project/scene namespace. A Comfy Desktop
restart snapshot exists, but the installed runtime exposes no reliable
per-process or per-instance identity; recovery therefore relies on durable
queue/history/output evidence and does not invent a `comfy_instance_id`.

`RECONCILIATION_REQUIRED` is now transient. A reachable queue with no prompt,
an absent history entry, and no recoverable deterministic output must be
confirmed twice before the job becomes terminal `ORPHANED`. Queue or history
transport failures do not count as absence. An orphaned job is retained as an
immutable historical record, is no longer active, and may be retried without a
user delete action or permanent UI deadlock.

Before declaring a prompt lost, reconciliation scans only the job-owned
deterministic output prefix. It accepts exactly one direct supported-video
candidate, rejects traversal/symlink/outside-root paths, rejects ambiguity,
and validates stable non-empty media through the existing ffprobe adapter.
Successful fallback records the output as history-independent and
`deterministic_job_output_prefix`; unresolved, ambiguous, corrupt, or partial
media fails closed and eventually produces the truthful orphaned blocker.

New submissions use a deep execution copy of the prepared workflow and append
the Builder `job_id` to the manifest-owned output prefix. The prepared workflow
file, preparation fingerprint, frame plan, prompt provenance, and quality
settings remain unchanged. Retries therefore receive distinct prompt and
output namespaces while the old job and any old raw output remain preserved.
The Render UI labels the terminal state `ORPHANED`, explains that the previous
ComfyUI job is unavailable, re-enables Render Scene when current inputs are
ready, and exposes Retry. The top badge says `INPUTS READY` to distinguish
input readiness from queue state.

MT-62 — Existing Render Job Recovery — PENDING pending live queue/history and
raw-output evidence. MT-69 — Live Render Progress Telemetry — PENDING_RETEST.
MT-70 — Orphaned Job Recovery / Output Fallback — PASS. This round adds no
H3 execution, no second automatic H3 submission, no queue lifecycle expansion,
and no Phase 8D work.

---

### Phase 8C.8 — Live progress repair and Renders UI cleanup

The real telemetry failure was at the first transport boundary, before prompt
correlation or frontend delivery. The active ComfyUI Python environment has
`websockets 16.1.1` and `websockets.sync.client.connect`, but does not have the
`websocket-client` package imported by the Builder's former default factory.
The adapter caught that `ModuleNotFoundError` in an unlogged reconnect loop, so
the WebSocket remained disconnected, no installed ComfyUI event entered the
Builder, the volatile snapshot remained unobserved, and the frontend hid its
telemetry panel. Read-only inspection found the local API unavailable and the
installed ComfyUI log recorded the then-current prompt starting and later being
cancelled; no Builder WebSocket existed from which that job's live event traffic
could be recovered. The job was not interrupted or mutated by this round.

Installed ComfyUI source confirms that `/ws?clientId=<id>` owns the socket
session, `/prompt` copies the top-level `client_id` into execution `extra_data`,
and execution/progress events are directed to that same client identity. The
Builder already generated one non-caller-controlled ID for both paths, but its
worker waited for an owned `prompt_id` before connecting while ownership was not
known until `/prompt` returned. Future submissions now request and briefly wait
for the shared `websockets.sync` session before POSTing with the same ID. A
connection failure is logged and never gates HTTP submission; queue/history
remain the durable lifecycle authority and the UI presents truthful
indeterminate activity when numeric telemetry is unavailable.

The installed event wrappers retained by the adapter are `execution_start`,
`execution_cached`, `executing`, node-local `progress` (`value`, `max`,
`prompt_id`, `node`), `progress_state` (`prompt_id`, `nodes`), `executed`,
`execution_error`, `execution_interrupted`, `execution_success`, and the
uncorrelated `status` wrapper. Owned prompt IDs are required before an event can
update the latest in-memory snapshot. Active Render/job responses now include
the snapshot even before the first event, with `available`, connection state,
friendly current stage, numeric value/max/percent when present, and update time.
No progress tick is written to project or job persistence. Structured logs keep
the WebSocket URL, client ID, prompt ID, Builder job and scene IDs, event and node
identity, connection/reconnect failures, submission result, and compatibility
diagnostics outside the product UI.

The user-facing stage is named **Renders**. Redundant Render Preparation and
Scene readiness headings, the explanatory intro, scene UUID title, persistent
submission-acceptance banner, numerical-progress debug sentence, repeated RTX
commentary, filesystem output paths, and primary-card node/input diagnostics
have been removed. Product state tiles and concise actionable errors remain.
Runtime rescan is retained as a direct control beside Refresh Preflight. Render
and preparation actions are disabled while a job is active, and Cancel remains
available under the existing capability contract.

Every active job has a compact antique-gold activity region. Without a current
authoritative value/max pair it is indeterminate and claims no percentage. With
installed ComfyUI node-local progress it becomes determinate and labels the
number as friendly stage progress, such as `Generating video · 42%`. Stage
changes clear the prior number, terminal states remove activity, reduced-motion
preferences suppress animation, and telemetry-only polls update the existing
DOM subtree without replacing cards or changing scroll position.

MT-69 — Live Render Progress Telemetry — PENDING_RETEST on the next real H3
submission loaded after this repair; the previous job cannot gain telemetry
retrospectively. MT-71 — Renders UI Production Cleanup — PENDING. MT-62 remains
PENDING until a successful current or subsequent job produces and associates
raw H3 output. This round submits no H3 work and does not begin Phase 8D.

### Phase 8C.9 — Renders header, status, and progress-component remediation

The Renders workspace uses one responsive header row: the visible **Renders**
title at the left, a concise `ready · not ready · active` aggregate toward the
right, and direct **Refresh Preflight** and **Rescan Runtime** controls. Ordinary
scene incompleteness is neutral informational state; red remains reserved for
actual failures and actionable errors. The Advanced disclosure, redundant
active prose, and `STAGE PROGRESS` label are absent.

The previous indeterminate presentation assigned its bar a literal 32% width.
That width could look like a frozen determinate value, and its reduced-motion
fallback made the false partial fill static. The corrected component has two
mutually exclusive modes. Indeterminate mode uses a full neutral host with a
moving antique-gold segment and no number; reduced-motion uses a non-positional
opacity pulse. Determinate mode is selected only for a current authoritative
ComfyUI `value/max` pair with `max > 0`, and displays that exact stage-local
percentage. It never infers whole-render completion.

Every RUNNING job therefore retains visible truthful activity. Numeric-to-
nonnumeric stage changes, stale or unavailable telemetry, and telemetry loss
clear the old inline width and percentage and return in place to indeterminate
activity. Terminal states clear the progress region. Narrow telemetry refreshes
continue to update the existing component without replacing the scene card.

For retryable terminal jobs the UI exposes the existing **Retry** action only;
that route creates a new job with historical retry linkage, while hiding the
semantically duplicate normal Render Scene action. Backend job history and
orphan recovery are unchanged.

MT-69 — Live Render Progress Telemetry — PENDING_RETEST. MT-71 — Renders UI
Production Cleanup — PENDING_RETEST. MT-62 remains PENDING. This round submits
no H3 work and does not begin Phase 8D.

### Phase 8C.10 — Renders title and internal-identifier cleanup

The Renders workspace retains the accepted single responsive header, neutral
aggregate summary, and direct Refresh Preflight and Rescan Runtime controls.
Its sole **Renders** heading now has an explicit page-title hierarchy above scene
labels, status text, and fact-tile headings while remaining within the existing
application type scale.

Normal Renders presentation is scene-numbered and product-facing. Scene, job,
prompt, preparation, and fingerprint identifiers remain available to backend
records, reconciliation, route bindings, structured logs, and relay evidence,
but are not rendered as card or header copy. Actionable card errors retain their
Scene N context without exposing those implementation identifiers.

Successful Prepare Render Inputs no longer leaves a persistent technical banner
containing a scene UUID, duplicated timing/frame details, or `workflow
validated` prose. The refreshed scene card's **Preparation: PREPARED** tile is
the durable success authority alongside its existing Duration and H3 Plan tiles.
Preparation and execution failures remain visible and actionable.

MT-69 — Live Render Progress Telemetry — PASS from real RUNNING H3 evidence; the
accepted indeterminate/determinate component and narrow update architecture are
unchanged. MT-71 — Renders UI Production Cleanup — PENDING_RETEST. MT-62 —
Existing Render Job Recovery — PENDING_LIVE_COMPLETION and does not require a
new long AMD render solely for this UI remediation. This round submits no H3
work and does not begin Phase 8D.

### Phase 8C final acceptance

Phase 8C completes structural, automated, and practical manual acceptance. The
accepted scope retains the corrected covering H3 temporal plan
(`minimax_h3_24fps_5_plus_17n_covering_v2`: 24 FPS, valid frame counts
`5 + 17n`, smallest valid count whose generated coverage is at or above the
authoritative target scene duration; 6679 ms plans to 175 frames, nominal
7291.666… ms, trim ≈ 613 ms), raw H3 output as immutable execution output
distinct from the post-processed final scene MP4, authoritative Phase 8A
prepared scene audio (AAC 256 kbps, no processing) over H3-generated audio,
ffprobe validation with one-video-frame / one-source-sample / two-AAC-frame
tolerances, fail-closed short raw coverage, deterministic high-quality encode
(libx264, medium, CRF 18, yuv420p, 24 FPS), and atomic final candidate
promotion preserving the previous valid final after a failed replacement.

Runtime compatibility validation resolves live node contracts against the
installed runtime with SUPPORTED / UNSUPPORTED / NOT_DETERMINABLE semantics;
only definitive incompatibility blocks pre-submit, and ComfyUI `/prompt`
remains the final authoritative validation. VHS_VideoCombine format-dependent
widgets resolve against the selected format's dynamic contract, and LoadImage
`VALIDATE_INPUTS` accepts the installed nested input-root-relative selector for
existing files. The AMD Radeon RX 7900 XT development machine and the RTX 4080
SUPER 16 GB production machine are both valid H3 execution platforms with one
shared quality-first workflow and no GPU-vendor execution gate. Submission uses
the API-format workflow through the top-level `prompt` key against
`/prompt`, `/queue`, `/history/{prompt_id}`, and `/interrupt`, with bounded
structured HTTP 400 diagnostics in the UI and full detail in logs.

HTTP queue/history remains the durable job lifecycle authority; WebSocket
telemetry (installed `websockets.sync` client, shared connection, client/session
and prompt_id correlation, real value/max only, friendly stage labels, HTTP
reconciliation fallback) is supplementary and never gates job release.
Confirmed historical jobs absent from queue/history resolve through transient
RECONCILIATION_REQUIRED to safe terminal orphan semantics, and raw-output
recovery uses only deterministic job/workflow-owned output evidence validated
by path safety, ffprobe, supported video, unique association, and completeness;
ambiguity fails closed. The Renders workspace is the production-facing UI:
neutral aggregate summary, Refresh Preflight and Rescan Runtime controls,
scene-numbered cards with the accepted fact tiles, truthful
indeterminate/determinate stage progress, and no internal identifiers or debug
prose.

Final manual test statuses:

- MT-58 — Render Execution Gate / AMD Safety — SUPERSEDED_PRODUCT_DECISION
- MT-59 — ComfyUI Queue Submission Contract — PASS
- MT-60 — Render Job Lifecycle / Recovery — PASS_STRUCTURAL
- MT-61 — Failure / Retry / Cancellation — PASS_STRUCTURAL
- MT-62 — Raw Output Discovery / Association — PENDING_LIVE_COMPLETION
- MT-63 — Post-Processing Eligibility / UI — PASS
- MT-64 — Exact Scene Finalization / Authoritative Audio — PASS_STRUCTURAL
- MT-65 — Finalization Invalidation / Idempotence — PASS_STRUCTURAL
- MT-66 — Frame-Plan Coverage — PASS
- MT-67 — Cross-Hardware Render Eligibility — PASS
- MT-68 — ComfyUI Validation Error Diagnostics — PASS_STRUCTURAL
- MT-69 — Live Render Progress Telemetry — PASS
- MT-70 — Orphaned Job Recovery / Output Fallback — PASS
- MT-71 — Renders UI Production Cleanup — PASS

MT-62 remains intentionally open: it requires a naturally completed real H3
render to validate automatic raw-output association end-to-end, and is carried
forward without forcing an extended AMD render solely for this evidence. When a
real production render completes naturally, perform the MT-62 live validation.
Phase 8D is not started.

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
