import json
import logging

from .scenes import SceneConstructionError, build_project_scenes
from .projects import (
    ProjectNotFoundError,
    ProjectPersistenceError,
    ProjectStorage,
    ProjectValidationError,
    validate_project_id,
)
from .source import (
    AudioProbeError,
    MediaProbeUnavailableError,
    SourceImportError,
    SrtValidationError,
    import_lyrics_srt,
    import_master_audio,
    parse_srt,
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

    async def read_file_part(request):
        try:
            reader = await request.multipart()
            part = await reader.next()
        except (ValueError, RuntimeError, UnicodeDecodeError):
            return None
        if part is None or part.name != "file" or not part.filename:
            return None
        return part

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

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/source/audio")
    async def music_video_builder_import_audio(request):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)

        part = await read_file_part(request)
        if part is None:
            return api_error("Multipart request must contain a file field.", 400)
        try:
            project = await import_master_audio(PROJECT_STORAGE, project_id, part.filename, part)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except MediaProbeUnavailableError:
            LOGGER.exception("No usable local media probe is available.")
            return api_error("Audio duration probing is unavailable.", 503)
        except (AudioProbeError, SourceImportError, ProjectValidationError) as error:
            LOGGER.warning("Master audio import was rejected: %s", error)
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist imported master audio project.")
            return api_error("Master audio could not be saved.", 500)
        return web.json_response(project)

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/source/srt")
    async def music_video_builder_import_srt(request):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)

        part = await read_file_part(request)
        if part is None:
            return api_error("Multipart request must contain a file field.", 400)
        try:
            project = await import_lyrics_srt(PROJECT_STORAGE, project_id, part.filename, part)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except (SrtValidationError, SourceImportError, ProjectValidationError) as error:
            LOGGER.warning("Lyrics SRT import was rejected: %s", error)
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist imported lyrics SRT project.")
            return api_error("Lyrics SRT could not be saved.", 500)
        return web.json_response(project)

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/scenes/build")
    async def music_video_builder_build_scenes(request):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)

        try:
            project = build_project_scenes(PROJECT_STORAGE, project_id, parse_srt)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except SceneConstructionError as error:
            LOGGER.warning("Scene construction was rejected: %s", error)
            return api_error(str(error), 422)
        except ProjectValidationError as error:
            LOGGER.warning("Project data prevented scene construction: %s", error)
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist constructed Music Video Builder scenes.")
            return api_error("Scenes could not be saved.", 500)
        return web.json_response(project)

    _routes_registered = True
