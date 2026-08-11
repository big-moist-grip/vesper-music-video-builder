import json
import logging

from .projects import (
    ProjectNotFoundError,
    ProjectPersistenceError,
    ProjectStorage,
    ProjectValidationError,
    validate_project_id,
)


LOGGER = logging.getLogger(__name__)
PROJECT_STORAGE = ProjectStorage()
_routes_registered = False


def health_payload():
    return {
        "ok": True,
        "service": "music-video-builder",
        "phase": 0,
    }


def register_routes():
    global _routes_registered
    if _routes_registered:
        return

    from aiohttp import ContentTypeError, web
    from server import PromptServer

    @PromptServer.instance.routes.get("/music-video-builder/health")
    async def music_video_builder_health(_request):
        return web.json_response(health_payload())

    def api_error(message, status):
        return web.json_response({"error": message}, status=status)

    async def read_json(request):
        try:
            return await request.json()
        except (ContentTypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None

    @PromptServer.instance.routes.get("/music-video-builder/projects")
    async def music_video_builder_list_projects(_request):
        try:
            return web.json_response(PROJECT_STORAGE.list_projects())
        except ProjectPersistenceError:
            LOGGER.exception("Could not list Music Video Builder projects.")
            return api_error("Projects could not be listed.", 500)

    @PromptServer.instance.routes.post("/music-video-builder/projects")
    async def music_video_builder_create_project(request):
        payload = await read_json(request)
        if not isinstance(payload, dict) or set(payload) != {"name"}:
            return api_error("Request body must contain only a project name.", 400)

        try:
            project = PROJECT_STORAGE.create_project(payload["name"])
        except ProjectValidationError as error:
            return api_error(str(error), 400)
        except ProjectPersistenceError:
            LOGGER.exception("Could not create Music Video Builder project.")
            return api_error("Project could not be created.", 500)
        return web.json_response(project, status=201)

    @PromptServer.instance.routes.get("/music-video-builder/projects/{project_id}")
    async def music_video_builder_load_project(request):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)

        try:
            project = PROJECT_STORAGE.load_project(project_id)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except ProjectValidationError:
            LOGGER.warning("Invalid project data encountered while loading project.")
            return api_error("Project data is invalid.", 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not load Music Video Builder project.")
            return api_error("Project could not be loaded.", 500)
        return web.json_response(project)

    @PromptServer.instance.routes.put("/music-video-builder/projects/{project_id}")
    async def music_video_builder_save_project(request):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)

        payload = await read_json(request)
        if not isinstance(payload, dict):
            return api_error("Request body must be a project JSON object.", 400)

        try:
            project = PROJECT_STORAGE.save_project(project_id, payload)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not save Music Video Builder project.")
            return api_error("Project could not be saved.", 500)
        return web.json_response(project)

    _routes_registered = True
