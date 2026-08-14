import subprocess
import unittest
from pathlib import Path

from backend.gpt_launcher import (
    GPT_DIRECTOR_URLS,
    GptDirectorNotFoundError,
    GptLaunchError,
    launch_gpt_director,
)


ROOT = Path(__file__).parents[1]
EXTENSION = ROOT.joinpath("web", "extension.js").read_text(encoding="utf-8")
LAUNCHER_PATH = ROOT.joinpath("web", "gpt_launcher.js")
ROUTES = ROOT.joinpath("backend", "routes.py").read_text(encoding="utf-8")


class Phase7C5BackendLaunchTestCase(unittest.TestCase):
    def test_each_allowlisted_director_opens_exactly_once(self):
        for director, expected_url in GPT_DIRECTOR_URLS.items():
            calls = []

            def opener(url):
                calls.append(url)
                return True

            result = launch_gpt_director(director, opener=opener)

            self.assertEqual(calls, [expected_url])
            self.assertEqual(
                result,
                {"director": director, "url": expected_url, "opened": True},
            )

    def test_failed_system_launch_is_reported_truthfully(self):
        with self.assertRaises(GptLaunchError):
            launch_gpt_director("storyboard", opener=lambda _url: False)

        def failing_opener(_url):
            raise OSError("test browser failure")

        with self.assertRaises(GptLaunchError):
            launch_gpt_director("prompts", opener=failing_opener)

    def test_invalid_director_never_attempts_a_launch(self):
        calls = []
        with self.assertRaises(GptDirectorNotFoundError):
            launch_gpt_director("unknown", opener=lambda url: calls.append(url) or True)
        self.assertEqual(calls, [])


class Phase7C5FrontendContractTestCase(unittest.TestCase):
    @staticmethod
    def function_body(name, next_name):
        start = EXTENSION.index(name)
        return EXTENSION[start:EXTENSION.index(next_name, start)]

    def test_launcher_result_contract_and_exact_urls(self):
        script = f"""
import {{ pathToFileURL }} from 'node:url';
const launcher = await import(pathToFileURL({str(LAUNCHER_PATH)!r}).href);
const expected = {{
  storyboard: "https://chatgpt.com/g/g-6a7c2ee4c49c81919ff52e70d0e6f58a-storyboard-director",
  prompts: "https://chatgpt.com/g/g-6a7dd81828588191955a0ef1ac196da7-prompt-director",
}};
for (const [director, url] of Object.entries(expected)) {{
  if (launcher.gptLaunchPath(director) !== `/music-video-builder/gpt/${{director}}/open`) process.exit(1);
  if (!launcher.isSuccessfulGptLaunch({{ director, url, opened: true }}, director)) process.exit(2);
  if (launcher.isSuccessfulGptLaunch({{ director, url, opened: false }}, director)) process.exit(3);
}}
if (launcher.gptLaunchPath("unknown") !== null) process.exit(4);
"""
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_frontend_uses_one_authoritative_launch_request(self):
        helper = self.function_body("async function openDedicatedGpt(root, director)", "async function closeBuilder")
        self.assertEqual(helper.count("fetchJson(path"), 1)
        self.assertIn("builderState.gptLaunchInFlight", helper)
        self.assertIn("isSuccessfulGptLaunch(result, director)", helper)
        self.assertNotIn("window.open", helper)
        self.assertNotIn("document.createElement", helper)
        self.assertNotIn("allow pop-ups", helper)
        self.assertNotIn("setCurrentProject", helper)
        self.assertNotIn("storyboardRequest", helper)
        self.assertNotIn("promptDraft", helper)

    def test_success_path_does_not_create_an_error_status(self):
        helper = self.function_body("async function openDedicatedGpt(root, director)", "async function closeBuilder")
        success_tail = helper[helper.index("if (!isSuccessfulGptLaunch"):helper.index("} catch (error)")]
        self.assertNotIn("setStoryboardRelayMessage", success_tail)
        self.assertNotIn("promptMessage =", success_tail)
        self.assertNotIn("promptMessageState =", success_tail)
        self.assertNotIn("GPT_LAUNCH_FAILURE_MESSAGE", success_tail.split("throw new Error", 1)[0])

    def test_new_attempt_and_stage_switch_clear_stale_launcher_errors(self):
        clear = self.function_body("function clearGptLauncherErrors()", "async function openDedicatedGpt")
        launch = self.function_body("async function openDedicatedGpt(root, director)", "async function closeBuilder")
        switch = self.function_body("function switchView(root, view)", "function renderProjectState")
        self.assertIn("storyboardRelayMessage === GPT_LAUNCH_FAILURE_MESSAGE", clear)
        self.assertIn("promptMessage === GPT_LAUNCH_FAILURE_MESSAGE", clear)
        self.assertLess(launch.index("clearGptLauncherErrors();"), launch.index("fetchJson(path"))
        self.assertIn("clearGptLauncherErrors();", switch)

    def test_route_uses_backend_result_as_single_authority(self):
        self.assertIn('@PromptServer.instance.routes.post("/music-video-builder/gpt/{director}/open")', ROUTES)
        self.assertIn("await asyncio.to_thread(launch_gpt_director", ROUTES)
        self.assertIn('api_error("Could not open the GPT. Open it manually or retry.", 502)', ROUTES)
        self.assertNotIn("allow pop-ups", ROUTES)


if __name__ == "__main__":
    unittest.main()
