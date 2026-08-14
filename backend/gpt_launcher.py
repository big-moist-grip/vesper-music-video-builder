"""Allowlisted external launch for the two dedicated Vesper GPTs."""

from __future__ import annotations

import webbrowser
from collections.abc import Callable


GPT_DIRECTOR_URLS = {
    "storyboard": "https://chatgpt.com/g/g-6a7c2ee4c49c81919ff52e70d0e6f58a-storyboard-director",
    "prompts": "https://chatgpt.com/g/g-6a7dd81828588191955a0ef1ac196da7-prompt-director",
}


class GptLaunchError(RuntimeError):
    """The allowlisted GPT could not be opened by the system browser."""


class GptDirectorNotFoundError(ValueError):
    """The requested GPT Director key is not allowlisted."""


def launch_gpt_director(
    director: object,
    opener: Callable[[str], bool] | None = None,
) -> dict[str, object]:
    if not isinstance(director, str) or director not in GPT_DIRECTOR_URLS:
        raise GptDirectorNotFoundError("GPT Director target is invalid.")
    url = GPT_DIRECTOR_URLS[director]
    launch = opener or webbrowser.open_new_tab
    try:
        opened = launch(url)
    except (OSError, webbrowser.Error) as error:
        raise GptLaunchError("The system browser could not open the GPT.") from error
    if not opened:
        raise GptLaunchError("The system browser did not accept the GPT launch.")
    return {"director": director, "url": url, "opened": True}
