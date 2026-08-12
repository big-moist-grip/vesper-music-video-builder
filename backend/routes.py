import json
import logging

from .scenes import SceneConstructionError, build_project_scenes
from .entities import (
    EntityNotFoundError,
    ReferenceImportError,
    ReferenceNotFoundError,
    add_reference,
    create_character,
    create_location,
    delete_character,
    delete_location,
    get_reference_path,
    remove_reference,
    update_character,
    update_location,
)
from .projects import (
    ProjectNotFoundError,
    ProjectPersistenceError,
    ProjectStorage,
    ProjectValidationError,
    validate_project_id,
    validate_entity_id,
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
from .storyboard import (
    build_storyboard_request,
    empty_storyboard,
    validate_storyboard_response,
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

    @PromptServer.instance.routes.post("/music-video-builder/projects/invalid/ignore")
    async def music_video_builder_ignore_invalid_project(request):
        payload = await read_json(request)
        if not isinstance(payload, dict) or set(payload) != {"folder_name", "signature"}:
            return api_error("Request body must contain the invalid project folder and signature.", 400)

        try:
            result = PROJECT_STORAGE.ignore_invalid_project(payload["folder_name"], payload["signature"])
        except ProjectNotFoundError:
            return api_error("Invalid project entry was not found.", 404)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not save ignored invalid-project state.")
            return api_error("Invalid project entry could not be ignored.", 500)
        return web.json_response(result)

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

    @PromptServer.instance.routes.get("/music-video-builder/projects/{project_id}/storyboard/request")
    async def music_video_builder_storyboard_request(request):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)

        try:
            project = PROJECT_STORAGE.load_project(project_id)
            storyboard_request = build_storyboard_request(project)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not build the Music Video Builder storyboard request.")
            return api_error("Storyboard request could not be built.", 500)
        return web.json_response(storyboard_request)

    async def read_storyboard_response(request):
        payload = await read_json(request)
        if not isinstance(payload, dict):
            return None, api_error("Request body must be a storyboard response JSON object.", 400)
        return payload, None

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/storyboard/validate")
    async def music_video_builder_validate_storyboard(request):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)

        payload, error_response = await read_storyboard_response(request)
        if error_response is not None:
            return error_response
        try:
            project = PROJECT_STORAGE.load_project(project_id)
            preview = validate_storyboard_response(project, payload)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not load the project for storyboard validation.")
            return api_error("Storyboard response could not be validated.", 500)
        return web.json_response(preview)

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/storyboard/apply")
    async def music_video_builder_apply_storyboard(request):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)

        payload, error_response = await read_storyboard_response(request)
        if error_response is not None:
            return error_response
        try:
            project = PROJECT_STORAGE.load_project(project_id)
            storyboard = validate_storyboard_response(project, payload)
            saved_project = PROJECT_STORAGE.save_project(
                project_id,
                {**project, "storyboard": storyboard},
                allow_storyboard_change=True,
            )
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not apply the Music Video Builder storyboard.")
            return api_error("Storyboard could not be applied.", 500)
        return web.json_response(saved_project)

    @PromptServer.instance.routes.delete("/music-video-builder/projects/{project_id}/storyboard")
    async def music_video_builder_clear_storyboard(request):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)

        try:
            project = PROJECT_STORAGE.load_project(project_id)
            saved_project = PROJECT_STORAGE.save_project(
                project_id,
                {**project, "storyboard": empty_storyboard()},
                allow_storyboard_change=True,
            )
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not clear the Music Video Builder storyboard.")
            return api_error("Storyboard could not be cleared.", 500)
        return web.json_response(saved_project)

    @PromptServer.instance.routes.delete("/music-video-builder/projects/{project_id}")
    async def music_video_builder_delete_project(request):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)

        try:
            result = PROJECT_STORAGE.delete_project(project_id)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except ProjectValidationError:
            LOGGER.warning("Unsafe or invalid project deletion request rejected.")
            return api_error("Project could not be deleted.", 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not delete Music Video Builder project.")
            return api_error("Project could not be deleted.", 500)
        return web.json_response(result)

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

    async def run_entity_json_mutation(request, operation):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)

        payload = await read_json(request)
        if not isinstance(payload, dict):
            return api_error("Request body must be a JSON object.", 400)
        try:
            project = operation(PROJECT_STORAGE, project_id, payload)
        except EntityNotFoundError:
            return api_error("The requested entity was not found.", 404)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist Music Video Builder entity mutation.")
            return api_error("The entity could not be saved.", 500)
        return web.json_response(project)

    async def run_entity_delete(request, operation):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)
        try:
            project = operation(PROJECT_STORAGE, project_id, request.match_info)
        except EntityNotFoundError:
            return api_error("The requested entity was not found.", 404)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist Music Video Builder entity deletion.")
            return api_error("The entity could not be deleted.", 500)
        return web.json_response(project)

    async def run_reference_upload(request, kind):
        project_id = request.match_info["project_id"]
        entity_id = request.match_info[f"{kind[:-1]}_id"]
        try:
            validate_project_id(project_id)
            validate_entity_id(entity_id, f"{kind[:-1].capitalize()} ID")
        except ProjectValidationError:
            return api_error("The project or entity ID is invalid.", 400)
        part = await read_file_part(request)
        if part is None:
            return api_error("Multipart request must contain a file field.", 400)
        try:
            project = await add_reference(
                PROJECT_STORAGE,
                project_id,
                kind,
                entity_id,
                part.filename,
                part,
            )
        except EntityNotFoundError:
            return api_error("The requested entity was not found.", 404)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except ReferenceImportError as error:
            LOGGER.warning("Reference image upload was rejected: %s", error)
            return api_error(str(error), 422)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist Music Video Builder reference image.")
            return api_error("Reference image could not be saved.", 500)
        return web.json_response(project)

    async def serve_reference(request, kind):
        project_id = request.match_info["project_id"]
        entity_id = request.match_info[f"{kind[:-1]}_id"]
        reference_id = request.match_info["reference_id"]
        try:
            validate_project_id(project_id)
            validate_entity_id(entity_id, f"{kind[:-1].capitalize()} ID")
            validate_entity_id(reference_id, "Reference ID")
        except ProjectValidationError:
            return api_error("The project, entity, or reference ID is invalid.", 400)
        try:
            reference_path = get_reference_path(
                PROJECT_STORAGE,
                project_id,
                kind,
                entity_id,
                reference_id,
            )
        except ReferenceNotFoundError:
            return api_error("Reference file was not found.", 404)
        except EntityNotFoundError:
            return api_error("The requested entity was not found.", 404)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except ProjectValidationError:
            LOGGER.warning("Invalid project data encountered while serving a reference.")
            return api_error("Project data is invalid.", 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not load Music Video Builder reference image.")
            return api_error("Reference file could not be loaded.", 500)
        return web.FileResponse(reference_path)

    async def run_reference_delete(request, kind):
        project_id = request.match_info["project_id"]
        entity_id = request.match_info[f"{kind[:-1]}_id"]
        reference_id = request.match_info["reference_id"]
        try:
            validate_project_id(project_id)
            validate_entity_id(entity_id, f"{kind[:-1].capitalize()} ID")
            validate_entity_id(reference_id, "Reference ID")
        except ProjectValidationError:
            return api_error("The project, entity, or reference ID is invalid.", 400)
        try:
            project = remove_reference(
                PROJECT_STORAGE,
                project_id,
                kind,
                entity_id,
                reference_id,
            )
        except ReferenceNotFoundError:
            return api_error("The requested reference was not found.", 404)
        except EntityNotFoundError:
            return api_error("The requested entity was not found.", 404)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist Music Video Builder reference deletion.")
            return api_error("Reference could not be removed.", 500)
        return web.json_response(project)

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/characters")
    async def music_video_builder_create_character(request):
        return await run_entity_json_mutation(request, create_character)

    @PromptServer.instance.routes.put("/music-video-builder/projects/{project_id}/characters/{character_id}")
    async def music_video_builder_update_character(request):
        project_id = request.match_info["project_id"]
        character_id = request.match_info["character_id"]
        return await run_entity_json_mutation(
            request,
            lambda storage, current_project_id, payload: update_character(
                storage,
                current_project_id,
                character_id,
                payload,
            ),
        )

    @PromptServer.instance.routes.delete("/music-video-builder/projects/{project_id}/characters/{character_id}")
    async def music_video_builder_delete_character(request):
        return await run_entity_delete(
            request,
            lambda storage, current_project_id, match_info: delete_character(
                storage,
                current_project_id,
                match_info["character_id"],
            ),
        )

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/characters/{character_id}/references")
    async def music_video_builder_add_character_reference(request):
        return await run_reference_upload(request, "characters")

    @PromptServer.instance.routes.get("/music-video-builder/projects/{project_id}/characters/{character_id}/references/{reference_id}")
    async def music_video_builder_get_character_reference(request):
        return await serve_reference(request, "characters")

    @PromptServer.instance.routes.delete("/music-video-builder/projects/{project_id}/characters/{character_id}/references/{reference_id}")
    async def music_video_builder_delete_character_reference(request):
        return await run_reference_delete(request, "characters")

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/locations")
    async def music_video_builder_create_location(request):
        return await run_entity_json_mutation(request, create_location)

    @PromptServer.instance.routes.put("/music-video-builder/projects/{project_id}/locations/{location_id}")
    async def music_video_builder_update_location(request):
        location_id = request.match_info["location_id"]
        return await run_entity_json_mutation(
            request,
            lambda storage, current_project_id, payload: update_location(
                storage,
                current_project_id,
                location_id,
                payload,
            ),
        )

    @PromptServer.instance.routes.delete("/music-video-builder/projects/{project_id}/locations/{location_id}")
    async def music_video_builder_delete_location(request):
        return await run_entity_delete(
            request,
            lambda storage, current_project_id, match_info: delete_location(
                storage,
                current_project_id,
                match_info["location_id"],
            ),
        )

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/locations/{location_id}/references")
    async def music_video_builder_add_location_reference(request):
        return await run_reference_upload(request, "locations")

    @PromptServer.instance.routes.get("/music-video-builder/projects/{project_id}/locations/{location_id}/references/{reference_id}")
    async def music_video_builder_get_location_reference(request):
        return await serve_reference(request, "locations")

    @PromptServer.instance.routes.delete("/music-video-builder/projects/{project_id}/locations/{location_id}/references/{reference_id}")
    async def music_video_builder_delete_location_reference(request):
        return await run_reference_delete(request, "locations")

    _routes_registered = True
