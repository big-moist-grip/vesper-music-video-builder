# Product Roadmap Assessment

## Planning position

The Builder should remain a focused music-video production environment. Its job is to turn intentional song timing into reviewed, reproducible H3 scene production:

```text
Song -> Scenes -> Storyboard -> Visual Planning -> MiniMax H3 Generation -> Review -> Final Production
```

The Builder should not become a general AI video workstation, workflow browser, model playground, or replacement for DaVinci Resolve.

This direction preserves the current product decisions:

- ChatGPT remains a manual creative relay. The Builder does not run a local LLM or call an LLM API.
- Artist/editor-provided SRT remains the authoritative lyric and timing source. The Builder does not add transcription.
- MiniMax H3 remains the focused generation engine.
- `keyframe_i2v` and `reference2video` remain visual-conditioning methods. Future candidate generation adds an attempt layer, not another Visuals method.
- Machine-owned prompt structure, scene timing, reference tags, continuity facts, fingerprints, and output metadata remain under Builder control.
- Final Scene and Production Scene remain distinct artifacts.
- Resolve remains the editing and finishing environment. The Builder may produce a review draft and timeline metadata without replacing Resolve.

The current source code remains unchanged. This document records planning guidance only.

## Evidence boundary

The assessment uses the repository source, tests, runtime contracts, `PROJECT_CONTEXT.md`, `MEMORY.md`, `VIDEO_GENERATION_PIPELINE_ASSESSMENT.md`, and the development plan.

The checkout contains no VRGDG source, documentation, or copied workflow. A web search attempt could not run because the configured search service lacked an API key. The VRGDG section therefore compares design patterns at a conceptual level. It does not claim a source-verified inventory of VRGDG features. A VRGDG URL or source package would be needed for a factual feature-by-feature comparison.

## 1. Current capabilities

### 1.1 Product workflow

The Builder already implements the core production spine:

1. **Setup**
   - Creates schema-v7 projects.
   - Imports a master audio file.
   - Imports and validates an SRT file.
   - Probes source duration with FFprobe.
   - Stores project-owned source files.

2. **Scene construction**
   - Uses SRT cues and source duration to create lyric and instrumental scenes.
   - Preserves contiguous source timing.
   - Splits long regions under the 2 to 10 second scene policy.
   - Keeps cue provenance and exact millisecond boundaries.

3. **Characters, locations, and references**
   - Stores project-local entities with stable IDs.
   - Validates and copies local still-image references.
   - Protects references used by the storyboard or Visuals selections.

4. **Storyboard**
   - Supports `loose`, `strict`, and `band_performance` Story Direction modes.
   - Generates a structured Storyboard Director request.
   - Uses a manual ChatGPT relay.
   - Validates pasted JSON, scene order, IDs, references, modes, and fingerprints.
   - Synchronizes required storyboard references into Visuals.

5. **Visual planning**
   - Supports `keyframe_i2v` and `reference2video`.
   - Stores accepted keyframes and descriptions.
   - Stores ordered reference selectors with a nine-image REF2VA limit.
   - Derives per-scene readiness and blockers.

6. **Prompt planning**
   - Compiles deterministic method-specific H3 prompt structure.
   - Keeps timing, lyrics, tags, references, retention, and method structure machine-owned.
   - Uses a manual Prompt Director relay for bounded creative prose.
   - Validates relay fingerprints, response fields, forbidden markup, lyric ownership, and stale state.
   - Saves only a current validated Final Prompt as render-ready.

7. **H3 generation**
   - Uses immutable API-format I2V and REF2VA templates.
   - Selects a manifest from the scene's Visuals method.
   - Prepares exact scene audio and project-owned visual inputs.
   - Applies the 24 FPS, `5 + 17n` frame-count contract.
   - Writes a compiled workflow and preparation record before submission.
   - Submits only to local loopback ComfyUI.
   - Tracks durable jobs through queue/history reconciliation and volatile WebSocket telemetry.

8. **Review and finalization**
   - Associates raw output with a Builder job, prompt ID, output node, and owned prefix.
   - Rejects ambiguous or unsafe output files.
   - Creates a project-owned Final Scene with exact source audio.
   - Validates media, timing, streams, and output identity.

9. **Production**
   - Supports `none`, `rtx_vsr_fast`, and `seedvr2_quality` as controlled Production Scene choices.
   - Keeps production jobs and current output pointers separate from Raw H3 and Final Scene.
   - Preserves upstream artifacts when post-processing fails.

10. **Batch**
    - Plans minimum work per scene.
    - Runs scenes sequentially through preparation, H3, finalization, and production.
    - Persists state, pause/resume/end/retry behavior, and restart recovery.

### 1.2 Capabilities that remain incomplete

- Seed Hunter candidate generation and selection do not exist.
- The H3 seed is fixed in both current templates. No per-attempt seed metadata exists.
- The frontend shows one job per scene and has no candidate gallery or render-media route.
- Storyboard records scene-level camera, motion, visual, and continuity notes, but no formal shot plan or continuity state model exists.
- Prompt generation has strong structural controls but no shot-level prompt sections, prompt version comparison, or structured style-bible layer.
- Production has upscale profiles, not a general controlled enhancement/look profile system.
- Full-length review assembly does not exist.
- Resolve timeline metadata export does not exist.
- Live H3 completion, target-NVIDIA profile qualification, and complete audio-to-Production-Scene execution remain unverified.

## 2. Current architectural strengths

### 2.1 The Builder owns the workflow, not the graph editor

The no-op ComfyUI node only opens the Builder. The product UI, backend services, manifests, persistence, and lifecycle logic own the music-video workflow. This keeps ComfyUI as the execution host instead of turning the Builder into a second workflow editor.

The current UI already follows a focused stage sequence:

```text
Setup -> Storyboard -> Visuals -> Prompts -> Renders
```

That structure provides a good home for candidate review, shot planning, production profiles, and assembly status without exposing arbitrary node graphs.

### 2.2 SRT and source audio provide a stable editorial contract

`source.py` and `scenes.py` make the artist/editor-provided SRT the source of truth for lyric ownership and scene timing. The Builder does not infer a competing timeline from audio. That choice has several benefits:

- scene boundaries remain intentional;
- lyric cues retain provenance;
- instrumental gaps remain explicit;
- H3 timing can use a separate frame plan without changing editorial timing;
- Final Scene and assembly services can reason from one contiguous timeline;
- review and Resolve metadata can share the same scene start/end values.

Future shot planning must stay subordinate to this scene timeline. A shot plan may subdivide a scene for visual direction. It must not silently rewrite the SRT-derived scene boundaries.

### 2.3 Relay architecture keeps creative direction bounded

The current prompt pipeline separates creative prose from machine-owned structure:

```text
deterministic compiler
    -> manual ChatGPT relay
    -> strict response validation
    -> machine reassembly
    -> editable Final Prompt
```

This gives the user creative control without allowing a relay response to author:

- scene duration;
- lyric text;
- source timing;
- reference ownership;
- `<Picture N>` and `<Subject N>` relationships;
- generation method;
- render seed;
- output paths;
- hidden workflow controls.

Future prompt improvements should expand the structured context and relay contract, not replace this division with a local model or a free-form prompt generator.

### 2.4 Fingerprints make stale work visible

Storyboard requests, Visuals readiness, prompts, preparation, finalization, and Production Scene records use fingerprints or file identities. These records give future features a basis for invalidation:

- a changed shot plan can stale the prompt;
- a changed prompt can stale candidate selection;
- a changed reference can stale generation packages;
- a changed Final Scene can stale Production Scene output;
- a changed scene output can stale a review assembly.

The next roadmap stages should extend this provenance model instead of adding parallel flags that can disagree.

### 2.5 Manifest-driven execution limits workflow sprawl

`backend/workflows.py` centralizes:

- workflow files;
- method identities;
- required node types;
- required models;
- declared patch points;
- graph links;
- output contracts;
- forbidden donor branches;
- profile settings.

This design can support future H3 preview profiles and controlled Production Scene profiles without exposing arbitrary ComfyUI graphs. It also provides one place to validate model/node readiness.

### 2.6 Durable lifecycle and failure isolation already exist

The Builder separates:

```text
Raw H3 -> Final Scene -> Production Scene
```

The job store owns queue/history reconciliation, retries, cancellation, output discovery, and restart recovery. Finalization owns media validation and authoritative source-audio remuxing. Production promotion preserves upstream artifacts. Assembly can build on these boundaries instead of taking ownership of individual ComfyUI node execution.

### 2.7 Project-owned persistence is a strong base for review

Schema-v7 JSON and project-owned directories give the Builder stable places for:

- source media;
- scene audio;
- references;
- keyframes;
- prepared workflows;
- jobs;
- Final Scenes;
- Production Scenes;
- batch records.

A future candidate set, review draft, and timeline manifest can follow the same ownership and atomic-write rules.

## 3. Missing capabilities

### 3.1 Candidate generation and selection

The pipeline has no generation-attempt abstraction beyond individual jobs. It needs:

- preview and final workflow profiles;
- explicit per-attempt seeds;
- candidate-set records;
- three sequential lower-cost H3 attempts;
- project-owned preview media or safe media references;
- candidate selection and stale-selection rules;
- final re-render from selected metadata;
- explicit selected-final resolution for Final Scene and Production Scene.

This feature belongs between Prompt/Visual readiness and Final Scene finalization. It should not add another Visuals method.

### 3.2 Shot-level planning

The storyboard already stores scene-level fields for action, camera, motion, visual instructions, and continuity notes. It does not model:

- shot IDs and shot order;
- relative shot timing within a scene;
- shot scale or framing;
- composition anchors;
- lighting state;
- movement vocabulary;
- continuity in/out state;
- planned transitions;
- shot-to-reference ownership;
- shot-level prompt provenance.

The first shot-level version should serve planning and prompt compilation. It should not force the H3 graph to render multiple cuts inside one scene. H3 generation can continue to produce one clip per scene while the shot plan directs camera, composition, movement, lighting, and continuity.

If later testing shows that a scene needs separate generated shots, the Builder can map shots to child render units. That decision should come after the shot-plan model and review experience prove the need.

### 3.3 Prompt generation depth

Current prompt generation handles method-specific structure well. Future improvements could add:

- versioned scene style-bible context;
- structured lighting and composition sections;
- shot-plan context;
- continuity state and previous/next scene context;
- prompt diff and provenance review;
- deterministic lint for camera, motion, duration, reference, and lyric ownership;
- separate relay fields for creative direction, continuity notes, and visual mood;
- prompt compilation profiles for preview and final generation.

The Builder should keep the relay output bounded to creative prose or explicitly declared creative fields. The backend should continue to reassemble the final H3 prompt.

### 3.4 Controlled Production Scene profiles

The current Production Scene registry covers native pass-through and two upscalers. A future controlled enhancement system could add named profiles for:

- upscale;
- image/detail enhancement;
- colour processing;
- film/look processing.

The current architecture lacks:

- a profile chain model;
- profile versioning beyond workflow IDs and fingerprints;
- colour-space and output-policy metadata;
- a stable distinction between spatial enhancement and look treatment;
- a reviewable Production Scene history for chained profiles.

### 3.5 Full music-video review assembly

The Builder has the inputs required for assembly but no assembly service. It needs:

- a resolver for the current Final or Production Scene per timeline scene;
- assembly preflight for coverage, order, dimensions, FPS, codecs, and missing outputs;
- deterministic concatenation in scene order;
- original master-audio attachment;
- project-owned review-draft media and metadata;
- a `scenes.csv` or equivalent timeline manifest;
- safe handling of mixed native/upscaled output dimensions;
- stale assembly detection when a scene output changes.

Assembly should produce a review draft. It should not replace Resolve or become a full NLE.

### 3.6 Live qualification

The structure is well tested with fakes, but future release decisions need live evidence for:

- both H3 methods;
- preview and final profiles;
- seed variation and repeatability;
- exact audio/timing behavior;
- Production profiles;
- assembly on completed output sets;
- restart and output recovery against a real ComfyUI instance.

## 4. Recommended future features

### 4.1 Preserve and formalize the product contract

Keep these as non-negotiable constraints:

- ChatGPT relay remains the creative-direction interface.
- No local LLM dependency.
- No OpenAI or other cloud API integration.
- SRT remains the lyric and timing source of truth.
- No transcription workflow.
- MiniMax H3 remains the focused generation engine.
- Visual methods remain `keyframe_i2v` and `reference2video`.
- Candidate generation remains a generation-attempt layer.
- Final Scene and Production Scene remain separate.
- Resolve remains the finishing environment.
- Workflow manifests remain the only graph-selection boundary.

Add a product-direction test or documentation contract for these decisions so future feature work cannot broaden scope by accident.

### 4.2 Storyboard v2: shot planning without timeline replacement

### Proposed scene model

Keep the existing SRT-derived scene as the editorial unit. Add a storyboard-owned `shots` array inside each storyboard scene:

```json
{
  "scene_id": "...",
  "shots": [
    {
      "shot_id": "...",
      "order": 1,
      "relative_start_ms": 0,
      "relative_end_ms": 2400,
      "shot_scale": "medium",
      "composition": "...",
      "camera_direction": "...",
      "movement": "...",
      "lighting": "...",
      "subject_ids": ["..."],
      "location_id": "...",
      "reference_ids": ["..."],
      "action": "...",
      "continuity_in": "...",
      "continuity_out": "..."
    }
  ]
}
```

The example is a planning shape, not an implementation contract. The server must validate that shot intervals remain inside the parent scene, do not overlap unless the storyboard explicitly models an overlap, and cover only the intended portion of the scene.

### Continuity model

Add machine-owned continuity facts alongside the shot plan:

- character appearance and outfit state;
- location and time-of-day state;
- lighting state;
- prop or performance state;
- preceding-scene carry-in facts;
- next-scene carry-out facts;
- reference IDs and entity IDs.

The ChatGPT relay may propose continuity prose. The backend should own IDs, state transitions, timing, and reference relationships.

### Prompt integration

The deterministic compiler should consume shot and continuity fields as structured machine-owned sections. The relay should receive a bounded request and return creative prose for the selected shot or scene. The final prompt should keep:

- timing;
- lyrics;
- reference tags;
- entity names and IDs;
- continuity constraints;
- shot order;
- method-specific syntax;

under backend control.

### Execution boundary

The first Storyboard v2 release should improve planning and prompts without changing H3 graph topology. If shot-level rendering becomes necessary, create a separate render-unit model after review data proves that one H3 clip per scene cannot meet the product goal.

### 4.3 Prompt generation improvements

Recommended improvements, in order:

1. Version the deterministic prompt compiler and source fingerprint basis.
2. Add structured style-bible, lighting, composition, and continuity inputs.
3. Add shot-plan context without allowing relay output to alter scene timing.
4. Add prompt lint for unsupported camera, reference, lyric, and duration claims.
5. Add prompt diff and provenance display so the user can see what changed.
6. Allow a relay response to supply bounded creative variants only when the user explicitly requests them.
7. Store the selected creative response and its fingerprints separately from the machine-owned final prompt.
8. Let preview/final generation profiles select approved prompt sections, not arbitrary prompt text transformations.

Do not add a local prompt model, automatic browser agent, automatic GPT API, or an unvalidated free-form “enhance prompt” button.

### 4.4 Seed Hunter as a generation-attempt layer

Seed Hunter should add:

- a manifest profile dimension such as `preview_draft_v1` and `final_quality_v1`;
- explicit reproducible seeds;
- a candidate set with three sequential candidate attempts;
- project-owned candidate preview metadata and safe media;
- selection and stale-selection fingerprints;
- final re-render using selected candidate metadata;
- a selected-final pointer for Final Scene resolution.

Seed Hunter should not add:

- a new Visuals method;
- a general seed editor;
- arbitrary workflow input controls;
- a new prompt-generation system;
- a direct copy of preview media into Final Scene;
- a multi-branch H3 graph that renders all candidates in one job.

The full design appears in `VIDEO_GENERATION_PIPELINE_ASSESSMENT.md` and should remain subordinate to the Storyboard, Prompt, and SRT contracts.

### 4.5 Controlled Production enhancement profiles

### Profile registry

Extend the existing Production Scene method registry into a controlled profile registry. Each profile should declare:

- profile ID and version;
- one immutable workflow or media policy;
- input artifact role;
- required nodes, models, and tools;
- source dimensions and target dimensions;
- FPS and frame-count policy;
- colour space and pixel-format policy;
- audio policy;
- output codec/container;
- deterministic settings;
- output identity and validation rules;
- failure-isolation behavior.

Profiles should describe named outcomes such as:

```text
native_final_v1
rtx_vsr_fast_v1
seedvr2_quality_v1
enhancement_clean_detail_v1
look_cinematic_neutral_v1
```

The labels are planning examples. They are not approved profiles until the workflow and runtime qualify.

### Profile composition

Avoid an unbounded graph collection. Use one of these constrained models:

1. **Single approved profile:** one workflow produces one named Production Scene output.
2. **Short approved chain:** the backend selects an ordered list of fixed profiles from a registry, with a fingerprint for the complete chain.
3. **Project-level profile:** the user selects a named profile for the project, while the backend rejects unsupported per-scene overrides.

The first enhancement release should use single approved profiles. Add chains only when one profile cannot satisfy a demonstrated need.

### Colour and look processing

Colour processing and film/look processing must use declared, reviewable profiles. A profile may define:

- colour-space conversion;
- fixed exposure/contrast/saturation policy;
- an approved LUT or look asset with an identity hash;
- bounded grain or texture policy;
- target output format.

Do not expose arbitrary ComfyUI colour nodes, arbitrary LUT paths, unrestricted shader graphs, or user-entered chains in the Builder UI. Keep audio, timing, dimensions, and failure isolation under the same Production Scene contract.

### Currentness and promotion

Every enhancement output should record:

- base Final Scene identity;
- profile ID and version;
- profile parameters;
- input and output identities;
- validation result;
- prior valid output;
- production job lineage.

The existing `production/current.json` and atomic promotion flow provide the right model. A profile change should invalidate only Production Scene output, not Raw H3, Final Scene, candidate selection, or source timing.

### 4.6 Full-length review draft and Resolve metadata

### Review-draft pipeline

Add a post-Production assembly stage:

```text
current scene outputs in timeline order
    -> assembly preflight
    -> controlled concatenation
    -> original master-audio attachment
    -> review_draft.mp4 + assembly.json
    -> optional scenes.csv / timeline metadata
```

The assembly service should:

1. Read scene order and exact timing from SRT-derived `project.json` scenes.
2. Resolve one current output for each scene.
3. Reject missing, stale, failed, or ambiguous scene outputs.
4. Verify consistent FPS, dimensions, codec, pixel format, and audio policy.
5. Concatenate video in scene order.
6. Attach the original master audio as the authoritative full-song audio.
7. Write a project-owned review draft and assembly metadata atomically.
8. Record all scene identities, hashes, methods, job IDs, and production profile IDs.

The resolver should prefer the current Production Scene when one exists. It may fall back to the current Final Scene only under an explicit user-selected review policy. It must not silently mix incompatible native and upscaled dimensions. If mixed outputs need support, add a named assembly normalization profile rather than hidden scaling or cropping.

The assembly output should not become a new source of timing truth. Scene start/end values remain those from SRT-derived scenes. The assembly manifest should prove that concatenated scene coverage matches the master duration within the declared media tolerance.

### Resolve handoff

The Builder can export timeline metadata without automating Resolve:

```text
scenes.csv
scene_number,scene_id,start_ms,end_ms,source_path,final_path,production_path,method,profile_id,status
```

A later version may add an interchange format such as FCPXML if the workflow needs it. The Builder should not operate Resolve, mutate a Resolve project, or become an NLE.

### 4.7 Review workspace

A future Review workspace should show:

- scene order and exact timeline position;
- current Final Scene or Production Scene;
- candidate selection when Seed Hunter exists;
- prompt and storyboard provenance;
- output readiness and stale state;
- side-by-side candidate comparison;
- assembly-draft status;
- exportable timeline metadata.

The Review workspace should consume existing durable records. It should not own a second render scheduler or duplicate project state.

## 5. Features to avoid

### 5.1 General workflow browser

Avoid exposing a catalog of arbitrary ComfyUI workflows, node graphs, model loaders, sampler widgets, or donor graphs. That would shift the Builder from a music-video pipeline into a workflow collection.

Keep workflow choice inside the manifest registry. Expose named product profiles only after validation.

### 5.2 Local LLM or transcription stack

Avoid:

- local LLM downloads;
- embedded prompt models;
- automatic ChatGPT browser automation;
- OpenAI API integration;
- audio transcription and lyric alignment;
- automatic replacement of artist-provided SRT timing.

These additions would duplicate the relay and weaken the intentional editorial timing contract.

### 5.3 Unbounded generation controls

Avoid exposing arbitrary:

- node IDs;
- model filenames;
- sampler graphs;
- LoRA chains;
- workflow paths;
- video-reference branches;
- T2V modes;
- arbitrary seeds as a primary creative interface;
- undocumented quality toggles.

Seed Hunter needs explicit seed provenance, not a general seed playground.

### 5.4 Monolithic donor graphs

Avoid copying UI groups, convenience switches, refinement branches, interpolation nodes, model download helpers, or multiple incompatible execution paths into the production graph. Keep method and profile complexity in small immutable manifests and templates.

### 5.5 Uncontrolled Production Scene nodes

Avoid a node palette for upscale, enhancement, grading, film grain, interpolation, denoise, and look processing. Use named profiles with fixed inputs, validated output contracts, and atomic promotion.

Do not add temporal interpolation or arbitrary motion enhancement to Production Scene without a separate product decision and timing qualification.

### 5.6 Hidden timeline changes

Avoid:

- replacing SRT timing with model-inferred timing;
- automatic scene re-segmentation after prompt generation;
- shot plans that silently modify scene boundaries;
- assembly that drops or stretches missing scenes;
- audio normalization that changes the master track's authority.

The Builder should fail with an actionable blocker when the timeline cannot be satisfied.

### 5.7 Resolve replacement

Avoid building a complete editor, timeline UI, keyframe editor, multi-track mixer, or Resolve automation layer. The Builder should provide reviewed scene outputs, a draft assembly, and clear metadata for Resolve.

### 5.8 Multi-user platform concerns

Avoid accounts, collaboration services, remote storage, analytics, authentication, and cloud queues. They add operational scope without improving the dedicated local production workflow.

## 6. VRGDG comparison

### 6.1 Evidence status

The repository does not contain VRGDG implementation material, and the external web lookup was unavailable in this environment. The comparison below treats VRGDG as the conceptual inspiration named by the product owner, not as a source-verified specification.

### 6.2 Patterns that are useful for a dedicated music-video workflow

If the VRGDG approach provides these patterns, they are useful at the product level:

- a dedicated custom-node entry point that opens a focused workspace;
- a guided form that hides graph plumbing from the user;
- project-level controls for music, lyrics, scenes, and visual direction;
- scene-oriented review instead of raw node inspection;
- a simple path from creative inputs to generated clips;
- status and output visibility inside the same task UI;
- reusable character, location, and reference context;
- quick iteration on a scene without rebuilding a full graph by hand.

The Builder already uses the dedicated-entry and guided-stage ideas through `MusicVideoBuilder`, `web/extension.js`, and the Setup/Storyboard/Visuals/Prompts/Renders views.

### 6.3 Patterns to avoid

Do not copy a conceptual approach that introduces:

- one monolithic graph for unrelated generation modes;
- hidden coupling between UI widgets and scattered node IDs;
- automatic workflow mutation without a preparation artifact;
- arbitrary node/model controls presented as product settings;
- automatic model downloads or node installation;
- implicit audio timing or transcription;
- prompt generation that lets an LLM author machine-owned structure;
- browser state as the source of job truth;
- output files that lack project ownership and provenance;
- a UI that grows into a general workflow browser.

The Builder's manifests, fingerprints, project storage, and durable job state solve these problems through explicit backend boundaries.

### 6.4 Problems the current Builder has already solved differently

| Product concern | Dedicated Builder approach |
|---|---|
| Task entry point | No-op ComfyUI launcher node with one modal workspace. |
| Timeline | Artist/editor SRT plus master-audio duration. No transcription. |
| Creative relay | Manual ChatGPT Storyboard and Prompt Director relay with strict JSON contracts. |
| Prompt ownership | Deterministic compiler and machine-owned timing, references, tags, and continuity. |
| Visual inputs | Project-owned characters, locations, keyframes, and ordered references. |
| Workflow selection | Manifest registry with immutable API templates and declared patch points. |
| Preparation | Dry preflight/package boundary before queue submission. |
| Execution | Local ComfyUI client with durable job IDs and queue/history reconciliation. |
| Output review | Raw H3, Final Scene, and Production Scene as separate artifacts. |
| Failure handling | Atomic writes, retry lineage, output ownership, and production failure isolation. |
| Future assembly | Planned review draft and Resolve metadata, not an embedded NLE. |

### 6.5 VRGDG comparison recommendation

Use VRGDG as a source of interaction ideas only after verifying the exact reference implementation. Retain the useful task-focused surface. Keep the Builder's stronger provenance, timing, relay, manifest, persistence, and failure-isolation decisions as the architecture of record.

Do not copy VRGDG node graphs, storage models, UI state models, model assumptions, or execution shortcuts.

## 7. Dependency ordering

### 7.1 Dependency graph

```text
Product contract and current-pipeline hardening
    |
    +--> Live H3 and output qualification
    |
    v
Storyboard v2 + continuity model
    |
    v
Deterministic prompt compiler v2 + relay contract
    |
    +--> Seed Hunter profiles and generation-attempt layer
    |        |
    |        v
    |    Selected-final pointer and Final Scene resolution
    |        |
    |        +--> Controlled Production enhancement profiles
    |        |
    |        v
    |    Review assembly and master-audio attachment
    |        |
    |        v
    |    Resolve timeline metadata export
```

### 7.2 What should happen first

1. Lock the product boundary in documentation and tests.
2. Fix current route, cancellation, polling, stale-state, and output-state defects that affect lifecycle trust.
3. Complete live H3 qualification for the existing two methods where hardware permits.
4. Define versioned storyboard, prompt, generation profile, output, production, and assembly contracts.
5. Extend storyboard structure and prompt compilation before adding candidate generation.

The reason for this order is provenance. Seed Hunter candidates must reference a stable prompt and input basis. Assembly must reference stable Final/Production Scene currentness. Production profiles must reference a stable Final Scene.

### 7.3 Feature dependencies

| Future capability | Required predecessor systems |
|---|---|
| Shot-level planning | Current storyboard fingerprinting, strict response validation, SRT-derived scene model. |
| Continuity tracking | Stable entity/reference IDs, storyboard v2 schema, prompt compiler input model. |
| Prompt improvements | Storyboard v2 fields, continuity model, relay response versioning, deterministic reassembly. |
| Seed Hunter preview | Current H3 workflow validation, profile-aware manifests, explicit seed provenance, candidate stores, safe media serving. |
| Seed Hunter final rerender | Candidate selection, stale fingerprints, generation packages, selected-final pointer. |
| Production enhancement profiles | Stable Final Scene identity, profile manifest registry, currentness/failure isolation, live media qualification. |
| Full review assembly | Current Final/Production Scene resolver, consistent output specs, complete scene coverage, master-audio policy. |
| Resolve metadata | Assembly manifest, stable scene/job/profile identities, deterministic naming. |
| Shot-level H3 execution | Proven shot planning model, review evidence that one scene clip is insufficient, render-unit and audio policy design. |

### 7.4 Work that can proceed in parallel

The following work can proceed without changing the product boundary:

- live qualification of existing H3 methods;
- route-level test coverage;
- schema and fingerprint design for storyboard v2;
- prompt lint and provenance UX design;
- candidate media retention design;
- assembly manifest design;
- Resolve CSV format design;
- VRGDG source comparison if a URL or repository is supplied.

Do not implement parallel feature branches that each create an independent scheduler, prompt state, or output-currentness model.

## 8. Recommended roadmap for major versions

The existing development plan names Phase 8F, Phase 9, and Phase 10 with overlapping batch/export descriptions. The roadmap below uses product releases rather than reusing those phase names.

### R0: Stabilize the focused production core

**Goal:** Make the existing pipeline trustworthy before adding modes.

Scope:

- resolve verified route and cancellation defects;
- add handler-level HTTP tests;
- correct render polling and status visibility;
- clarify `SUCCEEDED` versus output-discovery failure;
- complete current live H3 evidence where possible;
- reconcile documentation and historical test claims;
- document target runtime dependencies and qualification gates.

Exit condition:

- existing two-method scene generation, finalization, and Production Scene lifecycle have truthful structural and live status.

### R1: Storyboard and prompt planning depth

**Goal:** Improve visual planning without replacing SRT timing or ChatGPT relay.

Scope:

- versioned storyboard v2 response;
- shot-level planning inside SRT-derived scenes;
- camera, movement, lighting, composition, and continuity fields;
- entity/reference continuity state;
- deterministic prompt compiler v2;
- bounded relay request/response updates;
- prompt diff, lint, and provenance UI.

Exit condition:

- a storyboard can describe shot intent and continuity while the backend preserves scene timing and machine-owned structure.

### R2: Seed Hunter candidate generation

**Goal:** Let the user compare three lower-cost H3 candidates and choose one for final rendering.

Scope:

- profile-aware H3 manifests;
- explicit seed policy and attempt fingerprints;
- candidate-set and generation-package schemas;
- sequential candidate runner;
- candidate preview media and selection UI;
- stale selection and restart recovery;
- final-quality rerender from selected metadata;
- selected-final pointer used by Final Scene and Production Scene.

Exit condition:

- a selected candidate can produce a new final-quality H3 output without changing Visuals method semantics or bypassing finalization.

### R3: Controlled Production profiles

**Goal:** Expand Production Scene through named enhancement profiles.

Scope:

- profile registry and versioned manifests;
- qualified upscale profiles;
- controlled enhancement profile(s);
- controlled colour/look profile(s) only where output contracts are clear;
- profile currentness and failure isolation;
- profile selection and review metadata.

Exit condition:

- each profile has a fixed graph/media policy, runtime requirements, output identity, and tested failure behavior. The UI shows named profiles, not arbitrary nodes.

### R4: Full-length review draft

**Goal:** Assemble completed scene outputs into a reviewable full-song draft.

Scope:

- current scene-output resolver;
- assembly preflight;
- deterministic scene-order concatenation;
- master-audio attachment;
- review-draft metadata and stale detection;
- controlled output normalization when required;
- scene timeline manifest.

Exit condition:

- the Builder produces a review draft only when the selected scene outputs cover the intended SRT-derived timeline and satisfy one declared media policy.

### R5: Resolve handoff metadata

**Goal:** Give Resolve a deterministic timeline package without operating Resolve.

Scope:

- `scenes.csv` and assembly manifest export;
- source and output identity retention;
- deterministic scene numbering and names;
- optional FCPXML evaluation after CSV proves insufficient;
- documentation for Resolve import and relink.

Exit condition:

- Resolve can receive stable scene timing and file metadata while the Builder remains a preparation/review tool.

### R6: Conditional shot-aware execution

**Goal:** Consider separate shot rendering only if review evidence requires it.

Scope:

- shot-to-render-unit mapping;
- child scene audio policy;
- shot-level candidate and final selection;
- continuity across generated shot outputs;
- assembly of shots back into the parent scene.

Gate:

- Do not start this release from the existence of a shot plan. Start it only if one H3 clip per SRT-derived scene cannot meet the reviewed output requirement.

## 9. Recommended ownership boundaries for future work

| Concern | Owning layer |
|---|---|
| Master audio and SRT timing | `source.py`, `scenes.py`, project schema |
| Story direction and shot intent | `storyboard.py`, Storyboard workspace |
| Creative relay prose | Manual ChatGPT relay and strict relay validators |
| Prompt structure and machine-owned facts | `prompt_service.py` |
| Keyframe/reference conditioning | `visuals.py` |
| H3 workflow profiles | `workflows.py` and immutable workflow templates |
| Render preparation | `render.py` and generation-package service |
| H3 job truth | `render_jobs.py` and ComfyUI queue/history |
| Candidate set and selection | New generation-attempt service/store |
| Final Scene media | `render_finalize.py` |
| Production profiles | `render_production.py` and production manifests |
| Full-song review draft | New assembly service |
| Resolve metadata | Assembly/export service |
| Browser presentation | `web/extension.js`, prompt state helpers, CSS |

Keep these boundaries explicit. Avoid adding cross-cutting feature logic to `routes.py` or allowing the frontend to become a workflow scheduler.

## 10. Final recommendation

The Builder has the right foundation for a focused, local music-video product. Future work should deepen planning, review, generation choice, production profiles, and assembly while preserving the current source-of-truth decisions.

Recommended order:

```text
Stabilize current contracts
    -> expand storyboard and prompt planning
    -> add Seed Hunter attempts
    -> promote selected final generation
    -> add controlled Production profiles
    -> assemble review draft
    -> export Resolve timeline metadata
```

Keep ChatGPT relay, SRT timing, MiniMax H3, project-owned artifacts, manifest-driven workflows, durable job state, and Resolve handoff at the center. Decline features that turn the Builder into a general ComfyUI interface.

## Evidence reviewed

- `docs/ComfyUI Music Video Builder — Development Plan.md`
- `PROJECT_CONTEXT.md`
- `MEMORY.md`
- `VIDEO_GENERATION_PIPELINE_ASSESSMENT.md`
- `backend/projects.py`
- `backend/source.py`
- `backend/scenes.py`
- `backend/storyboard.py`
- `backend/visuals.py`
- `backend/prompt_service.py`
- `backend/workflows.py`
- `backend/render.py`
- `backend/render_jobs.py`
- `backend/render_finalize.py`
- `backend/render_production.py`
- `backend/render_batch.py`
- `backend/routes.py`
- `web/extension.js`

No source code changed. No feature was implemented.
