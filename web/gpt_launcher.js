export const GPT_DIRECTOR_URLS = Object.freeze({
    storyboard: "https://chatgpt.com/g/g-6a7c2ee4c49c81919ff52e70d0e6f58a-storyboard-director",
    prompts: "https://chatgpt.com/g/g-6a7dd81828588191955a0ef1ac196da7-prompt-director",
});

export function gptLaunchPath(director) {
    return Object.hasOwn(GPT_DIRECTOR_URLS, director)
        ? `/music-video-builder/gpt/${director}/open`
        : null;
}

export function isSuccessfulGptLaunch(result, director) {
    return result !== null
        && typeof result === "object"
        && result.opened === true
        && result.director === director
        && result.url === GPT_DIRECTOR_URLS[director];
}
