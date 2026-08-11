def health_payload():
    return {
        "ok": True,
        "service": "music-video-builder",
        "phase": 0,
    }


def register_routes():
    from aiohttp import web
    from server import PromptServer

    @PromptServer.instance.routes.get("/music-video-builder/health")
    async def music_video_builder_health(_request):
        return web.json_response(health_payload())
