import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
EXTENSION = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
STYLES = (ROOT / "web" / "builder.css").read_text(encoding="utf-8")


class Phase7C7PromptWorkflowTestCase(unittest.TestCase):
    @staticmethod
    def _function_body(source, name, next_name):
        start = source.index(name)
        end = source.index(next_name, start)
        return source[start:end]

    def _render_body(self):
        return self._function_body(
            EXTENSION,
            "function renderPromptState(root, options = {})",
            "async function loadPromptCards(root, silent = false, options = {})",
        )

    def test_needs_gpt_hides_final_but_current_or_stale_keeps_it_visible(self):
        render = self._render_body()
        visibility = self._function_body(
            EXTENSION,
            "function promptFinalIsVisible(item, draft)",
            "function promptFinalInstruction(item, draft)",
        )
        self.assertIn("promptDraftHasCurrentRelay(item, draft)", visibility)
        self.assertIn('item.status === "current"', visibility)
        self.assertIn('item.status === "stale"', visibility)
        self.assertIn("if (promptFinalIsVisible(item, draft))", render)
        final_block = render[render.index("if (promptFinalIsVisible(item, draft))"):render.index("const promptContextSection", render.index("if (promptFinalIsVisible(item, draft))"))]
        self.assertIn('finalSection.dataset.mvbPromptFinal = "";', final_block)
        self.assertIn('makeVisualButton("Save Prompt"', final_block)

    def test_prompt_director_is_linear_and_has_one_prominent_instruction(self):
        render = self._render_body()
        relay = render[render.index('const relayDetails = document.createElement("section")'):render.index("body.append(relayDetails);", render.index('const relayDetails = document.createElement("section")'))]
        instruction = relay.index('relayInstruction.className = "mvb-prompt-instruction"')
        paste = relay.index('relayResponseLabel.textContent = "Paste GPT Response";')
        generate = relay.index('makeVisualButton("Generate GPT Request"')
        copy = relay.index('makeVisualButton("Copy Request JSON"')
        open_gpt = relay.index('makeVisualButton("Open GPT"')
        apply = relay.index('makeVisualButton("Apply GPT Response"')
        self.assertLess(instruction, paste)
        self.assertLess(paste, generate)
        self.assertLess(generate, copy)
        self.assertLess(copy, open_gpt)
        self.assertLess(open_gpt, apply)
        self.assertNotIn("Prompt Director · Vesper H3 Prompt Director", EXTENSION)
        self.assertIn('relayInstruction.textContent = "Generate and copy the scene request, open the Prompt Director, then paste and apply its response.";', relay)

    def test_final_prompt_instruction_is_above_editor_and_status_copy_save_remain(self):
        render = self._render_body()
        start = render.index("if (promptFinalIsVisible(item, draft))")
        end = render.index("const promptContextSection", start)
        final_block = render[start:end]
        self.assertLess(final_block.index("finalSection.append(finalInstruction);"), final_block.index("const editorField"))
        self.assertLess(final_block.index("const editorField"), final_block.index('makeVisualButton("Copy"'))
        self.assertLess(final_block.index('makeVisualButton("Copy"'), final_block.index('makeVisualButton("Save Prompt"'))
        self.assertIn("This saved prompt is stale because the scene changed.", EXTENSION)
        self.assertIn("Run the Prompt Director again before saving a current version.", EXTENSION)
        self.assertNotIn("mvb-prompt-required", EXTENSION)

    def test_prompt_context_contains_only_collapsed_support_disclosures(self):
        render = self._render_body()
        context_start = render.index("const promptContextSection")
        context = render[context_start:render.index("body.append(promptContextSection);", context_start)]
        self.assertIn('promptContextSection.dataset.mvbPromptContext = "";', context)
        self.assertIn('mapping = document.createElement("details")', context)
        self.assertIn('mappingSummary.textContent = "Reference Mapping";', context)
        self.assertIn('relayRequestSummary.textContent = "View Request JSON";', context)
        self.assertIn('deterministicSummary.textContent = "View Deterministic Prompt";', context)
        self.assertLess(context.index('mappingSummary.textContent = "Reference Mapping";'), context.index('relayRequestSummary.textContent = "View Request JSON";'))
        self.assertLess(context.index('relayRequestSummary.textContent = "View Request JSON";'), context.index('deterministicSummary.textContent = "View Deterministic Prompt";'))
        self.assertIn("mapping.open = Boolean", context)
        self.assertIn("relayRequestDisclosure.open = Boolean", context)
        self.assertNotIn("deterministic.open = true", context)

    def test_context_follows_relay_when_final_prompt_is_hidden_and_follows_final_when_visible(self):
        render = self._render_body()
        relay_end = render.index("body.append(relayDetails);")
        final_start = render.index("if (promptFinalIsVisible(item, draft))")
        context_start = render.index("const promptContextSection")
        self.assertLess(relay_end, final_start)
        self.assertLess(final_start, context_start)
        self.assertIn("body.append(relayDetails);", render)
        self.assertIn("body.append(finalSection);", render)
        self.assertIn("body.append(promptContextSection);", render)

    def test_prompt_workflow_uses_readable_instruction_and_compact_disclosure_styles(self):
        self.assertIn(".mvb-prompt-instruction {", STYLES)
        self.assertIn("font-size: 14px;", STYLES)
        self.assertIn("color: var(--mvb-text);", STYLES)
        self.assertIn(".mvb-prompt-context-disclosure > summary {", STYLES)
        self.assertIn(".mvb-prompt-context-disclosure[open] > summary {", STYLES)
        self.assertIn(".mvb-prompt-relay-body {", STYLES)
        self.assertIn("padding-top: 0;", STYLES)

    def test_prompt_contract_and_approved_launcher_survive_ui_refinement(self):
        self.assertIn('payload.schema_version === 7', EXTENSION)
        self.assertIn('relay_fingerprint: draft.relayFingerprint', EXTENSION)
        self.assertIn('async function openDedicatedGpt(root, director)', EXTENSION)
        self.assertIn('isSuccessfulGptLaunch(result, director)', EXTENSION)
        self.assertIn('function capturePromptViewport(root, anchorSceneId = null)', EXTENSION)
        self.assertIn('window.requestAnimationFrame(apply);', EXTENSION)
        self.assertNotIn('localStorage', EXTENSION)
        self.assertNotIn('sessionStorage', EXTENSION)
        self.assertIn('data-mvb-view="render"', EXTENSION)


if __name__ == "__main__":
    unittest.main()
