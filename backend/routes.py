import asyncio
import json
import logging
import uuid

from .gpt_launcher import GptDirectorNotFoundError, GptLaunchError, launch_gpt_director
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
    update_resource_draft,
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
from .prompt_service import (
    PromptConflictError,
    build_prompt_relay_request,
    build_prompt_list,
    compile_scene_prompt,
    save_scene_prompt,
    validate_prompt_relay_response,
)
from .requirements import build_requirements_report, get_requirements_snapshot
from .render import (
    RenderPreparationBlocked,
    RenderPreparationError,
    build_render_preflight,
    prepare_render_scene,
)
from .render_batch import (
    RenderBatchError,
    batch_status,
    end_batch,
    manual_action_blocker,
    pause_batch_after_current,
    preview_batch,
    retry_failed_batch,
    resume_batch,
    start_batch,
)
from .render_finalize import (
    RenderFinalizationError,
    enrich_render_jobs_with_finalization,
    finalization_error_details,
    finalize_render_job,
)
from .render_production import (
    POSTPROCESS_METHOD_NONE,
    PostprocessJobConflict,
    ProductionMethodUnavailable,
    ProductionNotReady,
    RenderProductionError,
    cancel_postprocess_job,
    get_project_production_status,
    materialize_final_scene_for_postprocess,
    reconcile_scene_postprocess_jobs,
    resolve_project_production_method,
    retry_postprocess_job,
    set_project_production_method,
    submit_postprocess_job,
    summarize_production_scene,
    validate_production_method,
)
from .render_jobs import (
    JobStore,
    RenderJobError,
    RenderJobNotFound,
    cancel_render_job,
    reconcile_project_jobs,
    retry_render_job,
    submit_render_job,
)
from .visuals import (
    KeyframeNotFoundError,
    MAX_REF2VA_STILL_REFERENCES,
    VisualSceneNotFoundError,
    assign_keyframe,
    generate_and_save_keyframe_prompt,
    get_keyframe_path,
    remove_keyframe,
    save_keyframe_details,
    save_reference_selection,
    set_all_generation_method,
    set_generation_method,
    synchronize_storyboard_required_references,
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


def _request_has_body(request) -> bool:
    """Distinguish an absent/empty request body from malformed non-empty JSON."""

    content_length = getattr(request, "content_length", None)
    if isinstance(content_length, int):
        return content_length > 0
    can_read_body = getattr(request, "can_read_body", None)
    if isinstance(can_read_body, bool):
        return can_read_body
    headers = getattr(request, "headers", {})
    if hasattr(headers, "get"):
        length = headers.get("Content-Length")
        if length is not None:
            try:
                return int(length) > 0
            except (TypeError, ValueError):
                return True
    # An object that cannot expose any body metadata is safest to treat as
    # containing a body when JSON parsing failed. Real aiohttp requests expose
    # content_length or can_read_body, so empty bodies retain the all-ready path.
    return True


def parse_batch_selection_payload(payload: object, *, body_present: bool) -> list[str] | None:
    """Validate the batch body while preserving empty-body all-ready semantics."""

    if payload is None:
        if body_present:
            raise ValueError("Batch request body must contain valid JSON.")
        return None
    if not isinstance(payload, dict):
        raise ValueError("Batch requests must be a JSON object.")
    if not payload:
        return None
    if set(payload) != {"scene_ids"}:
        raise ValueError("Batch selection accepts only scene_ids.")
    scene_ids = payload["scene_ids"]
    if not isinstance(scene_ids, list) or not scene_ids or any(not isinstance(item, str) or not item for item in scene_ids):
        raise ValueError("scene_ids must be a non-empty list of scene identifiers.")
    return scene_ids


def register_routes():
    global _routes_registered
    if _routes_registered:
        return

    from aiohttp import ContentTypeError, web
    from server import PromptServer

    @PromptServer.instance.routes.get("/music-video-builder/health")
    async def music_video_builder_health(_request):
        return web.json_response(health_payload())

    @PromptServer.instance.routes.get("/music-video-builder/requirements")
    async def music_video_builder_requirements(_request):
        try:
            report = await asyncio.to_thread(build_requirements_report)
            return web.json_response(report)
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            LOGGER.exception("Could not scan Music Video Builder requirements.")
            return api_error("Requirements could not be scanned.", 500)

    def api_error(message, status):
        return web.json_response({"error": message}, status=status)

    async def read_json(request):
        try:
            return await request.json()
        except (ContentTypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None

    def render_error(error, status=422, *, preflight=None):
        payload = {
            "error_code": error.code,
            "error": error.message,
        }
        if preflight is not None:
            payload["preflight"] = preflight
        return web.json_response(payload, status=status)

    def render_job_error(error, *, extra_details=None):
        status = getattr(error, "status", 422)
        payload = {
            "error_code": error.code,
            "error": error.message,
        }
        details = getattr(error, "details", None)
        if isinstance(details, dict):
            payload.update(details)
        if isinstance(extra_details, dict):
            payload.update(extra_details)
            payload["details"] = dict(extra_details)
        return web.json_response(payload, status=status)

    @PromptServer.instance.routes.get("/music-video-builder/projects/{project_id}/render/preflight")
    async def music_video_builder_render_preflight(request):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)

        try:
            project = PROJECT_STORAGE.load_project(project_id)
            force_requirements_refresh = request.query.get("force") in {"1", "true", "yes"}
            requirements_snapshot = await asyncio.to_thread(
                get_requirements_snapshot,
                force_refresh=force_requirements_refresh,
            )
            preflight = await asyncio.to_thread(
                build_render_preflight,
                project,
                PROJECT_STORAGE,
                requirements_snapshot=requirements_snapshot,
            )
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except ProjectValidationError:
            LOGGER.warning("Invalid project data encountered during render preflight.")
            return api_error("Project data is invalid.", 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not load project for render preflight.")
            return api_error("Render preflight could not load the project.", 500)
        return web.json_response(preflight)

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/render/scenes/{scene_id}/prepare")
    async def music_video_builder_prepare_render_scene(request):
        project_id = request.match_info["project_id"]
        scene_id = request.match_info["scene_id"]
        try:
            validate_project_id(project_id)
            validate_entity_id(scene_id, "Scene ID")
        except ProjectValidationError:
            return api_error("Invalid project or scene ID.", 400)

        payload = await read_json(request)
        if payload is not None and (not isinstance(payload, dict) or payload):
            return api_error("Render preparation does not accept filesystem paths or request fields.", 400)

        blocker = await asyncio.to_thread(manual_action_blocker, PROJECT_STORAGE, project_id)
        if blocker is not None:
            return api_error(blocker, 409)

        try:
            result = await asyncio.to_thread(prepare_render_scene, PROJECT_STORAGE, project_id, scene_id)
        except RenderPreparationBlocked as error:
            return render_error(error, preflight=error.preflight)
        except ProjectNotFoundError:
            return api_error("Project or scene was not found.", 404)
        except ProjectValidationError:
            LOGGER.warning("Invalid project data encountered during render preparation.")
            return api_error("Project data is invalid.", 422)
        except RenderPreparationError as error:
            return render_error(error)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist render preparation artifacts.")
            return api_error("Render preparation artifacts could not be persisted.", 500)
        return web.json_response(result)

    @PromptServer.instance.routes.get("/music-video-builder/projects/{project_id}/render/jobs")
    async def music_video_builder_render_jobs(request):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
            result = await asyncio.to_thread(reconcile_project_jobs, PROJECT_STORAGE, project_id)
            result["jobs"] = await asyncio.to_thread(
                enrich_render_jobs_with_finalization,
                PROJECT_STORAGE,
                project_id,
                result.get("jobs", []),
            )
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except RenderJobError as error:
            return render_job_error(error)
        except RenderFinalizationError as error:
            return render_job_error(error)
        except ProjectPersistenceError:
            LOGGER.exception("Could not load durable render jobs.")
            return api_error("Render jobs could not be loaded.", 500)
        return web.json_response(result)

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/render/scenes/{scene_id}/submit")
    async def music_video_builder_submit_render_job(request):
        project_id = request.match_info["project_id"]
        scene_id = request.match_info["scene_id"]
        try:
            validate_project_id(project_id)
            validate_entity_id(scene_id, "Scene ID")
        except ProjectValidationError:
            return api_error("Invalid project or scene ID.", 400)
        payload = await read_json(request)
        if payload is not None and (not isinstance(payload, dict) or payload):
            return api_error("Render submission does not accept workflows, prompts, paths, hosts, or request fields.", 400)
        blocker = await asyncio.to_thread(manual_action_blocker, PROJECT_STORAGE, project_id, check_upscaler=True)
        if blocker is not None:
            return api_error(blocker, 409)
        try:
            result = await asyncio.to_thread(submit_render_job, PROJECT_STORAGE, project_id, scene_id)
        except ProjectNotFoundError:
            return api_error("Project or scene was not found.", 404)
        except ProjectValidationError:
            return api_error("Project data is invalid.", 422)
        except RenderJobError as error:
            return render_job_error(error)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist render job state.")
            return api_error("Render job state could not be persisted.", 500)
        return web.json_response(result, status=202)

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/render/jobs/{job_id}/cancel")
    async def music_video_builder_cancel_render_job(request):
        project_id = request.match_info["project_id"]
        job_id = request.match_info["job_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)
        payload = await read_json(request)
        if payload is not None and (not isinstance(payload, dict) or payload):
            return api_error("Render cancellation does not accept prompt IDs or request fields.", 400)
        try:
            result = await asyncio.to_thread(cancel_render_job, PROJECT_STORAGE, project_id, job_id)
        except ProjectValidationError:
            return api_error("Invalid render job ID.", 400)
        except (ProjectNotFoundError, RenderJobNotFound):
            return api_error("Render job was not found.", 404)
        except RenderJobError as error:
            return render_job_error(error)
        return web.json_response(result)

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/render/jobs/{job_id}/retry")
    async def music_video_builder_retry_render_job(request):
        project_id = request.match_info["project_id"]
        job_id = request.match_info["job_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)
        payload = await read_json(request)
        if payload is not None and (not isinstance(payload, dict) or payload):
            return api_error("Render retry does not accept prompt IDs, workflows, or request fields.", 400)
        blocker = await asyncio.to_thread(manual_action_blocker, PROJECT_STORAGE, project_id, check_upscaler=True)
        if blocker is not None:
            return api_error(blocker, 409)
        try:
            result = await asyncio.to_thread(retry_render_job, PROJECT_STORAGE, project_id, job_id)
        except ProjectValidationError:
            return api_error("Invalid render job ID.", 400)
        except (ProjectNotFoundError, RenderJobNotFound):
            return api_error("Render job was not found.", 404)
        except RenderJobError as error:
            return render_job_error(error)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist retried render job state.")
            return api_error("Render job state could not be persisted.", 500)
        return web.json_response(result, status=202)

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/render/jobs/{job_id}/finalize")
    async def music_video_builder_finalize_render_job(request):
        project_id = request.match_info["project_id"]
        job_id = request.match_info["job_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)
        payload = await read_json(request)
        if payload is None and _request_has_body(request):
            return api_error("Finalization request body must contain valid JSON.", 400)
        if payload is not None and (not isinstance(payload, dict) or payload):
            return api_error("Finalization does not accept raw paths, audio paths, or request fields.", 400)
        blocker = await asyncio.to_thread(manual_action_blocker, PROJECT_STORAGE, project_id)
        if blocker is not None:
            return api_error(blocker, 409)
        try:
            result = await asyncio.to_thread(finalize_render_job, PROJECT_STORAGE, project_id, job_id)
        except ProjectValidationError:
            return api_error("Invalid render job ID.", 400)
        except (ProjectNotFoundError, RenderJobNotFound):
            return api_error("Render job was not found.", 404)
        except RenderJobError as error:
            return render_job_error(error)
        except RenderFinalizationError as error:
            job_record = None
            try:
                job_record = await asyncio.to_thread(JobStore(PROJECT_STORAGE).find, project_id, job_id)
            except (ProjectNotFoundError, ProjectValidationError, RenderJobError):
                pass
            if job_record is not None:
                details = finalization_error_details(job_record, error)
            else:
                details = {
                    "job_id": job_id,
                    "scene_id": None,
                    "association_state": "UNKNOWN",
                    "output_association": {"state": "UNKNOWN", "usable": False, "failure": None},
                    "failure_reason": {"code": error.code, "message": error.message, "details": dict(error.details)},
                    "retry_available": False,
                    "retry_action": None,
                }
            return render_job_error(error, extra_details=details)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist finalization state.")
            return api_error("Finalization state could not be persisted.", 500)
        return web.json_response(result)

    @PromptServer.instance.routes.get("/music-video-builder/projects/{project_id}/production/status")
    async def music_video_builder_production_status(request):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)
        try:
            status = await asyncio.to_thread(get_project_production_status, PROJECT_STORAGE, project_id)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except RenderProductionError as error:
            return render_job_error(error)
        return web.json_response(status)

    @PromptServer.instance.routes.put("/music-video-builder/projects/{project_id}/production/upscale-method")
    async def music_video_builder_set_upscale_method(request):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)
        payload = await read_json(request)
        if not isinstance(payload, dict) or "upscale_method" not in payload:
            return api_error("Payload must include upscale_method.", 400)
        method = payload["upscale_method"]
        try:
            validate_production_method(method)
        except ProjectValidationError as error:
            return api_error(str(error), 400)
        try:
            result = await asyncio.to_thread(set_project_production_method, PROJECT_STORAGE, project_id, method)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except RenderProductionError as error:
            return render_job_error(error)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist project production settings.")
            return api_error("Could not persist production settings.", 500)
        return web.json_response(result)

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/scenes/{scene_id}/render/postprocess/start")
    async def music_video_builder_start_scene_postprocess(request):
        project_id = request.match_info["project_id"]
        scene_id = request.match_info["scene_id"]
        try:
            validate_project_id(project_id)
            validate_entity_id(scene_id, "Scene ID")
        except ProjectValidationError:
            return api_error("Invalid project or scene ID.", 400)
        payload = await read_json(request)
        if payload is not None and (not isinstance(payload, dict) or payload):
            return api_error("Postprocess start does not accept custom workflows or paths.", 400)
        blocker = await asyncio.to_thread(lambda: manual_action_blocker(PROJECT_STORAGE, project_id))
        if blocker is not None:
            return api_error(blocker, 409)

        def _do_submit():
            project = PROJECT_STORAGE.load_project(project_id)
            method = resolve_project_production_method(project)
            if method == POSTPROCESS_METHOD_NONE:
                raise RenderProductionError("POSTPROCESS_JOB_NOT_APPLICABLE", "The None method performs no post-processing.")
            selector, _ = materialize_final_scene_for_postprocess(PROJECT_STORAGE, project_id, scene_id)
            output_prefix = f"mvb_{project_id}_{scene_id}_{uuid.uuid4().hex[:8]}"
            return submit_postprocess_job(
                PROJECT_STORAGE,
                project_id,
                scene_id,
                method=method,
                final_scene_selector=selector,
                output_prefix=output_prefix,
            )

        try:
            result = await asyncio.to_thread(_do_submit)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except ProductionMethodUnavailable as error:
            return render_job_error(error)
        except PostprocessJobConflict as error:
            return render_job_error(error)
        except ProductionNotReady as error:
            return render_job_error(error)
        except RenderProductionError as error:
            return render_job_error(error)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist postprocess job state.")
            return api_error("Postprocess job state could not be persisted.", 500)
        return web.json_response(result, status=202)

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/scenes/{scene_id}/render/postprocess/cancel")
    async def music_video_builder_cancel_scene_postprocess(request):
        project_id = request.match_info["project_id"]
        scene_id = request.match_info["scene_id"]
        try:
            validate_project_id(project_id)
            validate_entity_id(scene_id, "Scene ID")
        except ProjectValidationError:
            return api_error("Invalid project or scene ID.", 400)
        try:
            result = await asyncio.to_thread(cancel_postprocess_job, PROJECT_STORAGE, project_id, scene_id)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except RenderProductionError as error:
            return render_job_error(error)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist cancelled postprocess job state.")
            return api_error("Postprocess job state could not be persisted.", 500)
        return web.json_response(result)

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/scenes/{scene_id}/render/postprocess/retry")
    async def music_video_builder_retry_scene_postprocess(request):
        project_id = request.match_info["project_id"]
        scene_id = request.match_info["scene_id"]
        try:
            validate_project_id(project_id)
            validate_entity_id(scene_id, "Scene ID")
        except ProjectValidationError:
            return api_error("Invalid project or scene ID.", 400)
        blocker = await asyncio.to_thread(lambda: manual_action_blocker(PROJECT_STORAGE, project_id))
        if blocker is not None:
            return api_error(blocker, 409)

        def _do_retry():
            selector, _ = materialize_final_scene_for_postprocess(PROJECT_STORAGE, project_id, scene_id)
            output_prefix = f"mvb_{project_id}_{scene_id}_{uuid.uuid4().hex[:8]}"
            return retry_postprocess_job(
                PROJECT_STORAGE,
                project_id,
                scene_id,
                final_scene_selector=selector,
                output_prefix=output_prefix,
            )

        try:
            result = await asyncio.to_thread(_do_retry)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except ProductionMethodUnavailable as error:
            return render_job_error(error)
        except PostprocessJobConflict as error:
            return render_job_error(error)
        except ProductionNotReady as error:
            return render_job_error(error)
        except RenderProductionError as error:
            return render_job_error(error)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist retried postprocess job state.")
            return api_error("Postprocess job state could not be persisted.", 500)
        return web.json_response(result, status=202)

    @PromptServer.instance.routes.get("/music-video-builder/projects/{project_id}/scenes/{scene_id}/render/postprocess")
    async def music_video_builder_scene_postprocess_status(request):
        project_id = request.match_info["project_id"]
        scene_id = request.match_info["scene_id"]
        try:
            validate_project_id(project_id)
            validate_entity_id(scene_id, "Scene ID")
        except ProjectValidationError:
            return api_error("Invalid project or scene ID.", 400)
        def _do_status():
            project = PROJECT_STORAGE.load_project(project_id)
            method = resolve_project_production_method(project)
            jobs = reconcile_scene_postprocess_jobs(PROJECT_STORAGE, project_id, scene_id)
            summary = summarize_production_scene(PROJECT_STORAGE, project_id, scene_id, method=method)
            return {"summary": summary, "jobs": jobs, "method": method}

        try:
            result = await asyncio.to_thread(_do_status)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except RenderProductionError as error:
            return render_job_error(error)
        except ProjectPersistenceError:
            LOGGER.exception("Could not reconcile scene postprocess status.")
            return api_error("Scene postprocess status could not be loaded.", 500)
        return web.json_response(result)

    async def read_batch_selection(request):
        """Accept either no body/{} (all ready) or exactly {scene_ids: [...]}."""

        payload = await read_json(request)
        try:
            scene_ids = parse_batch_selection_payload(payload, body_present=_request_has_body(request))
        except ValueError as error:
            return None, api_error(str(error), 400)
        return scene_ids, None

    async def run_batch_operation(request, operation, *, read_selection=False):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)
        scene_ids = None
        if read_selection:
            scene_ids, error_response = await read_batch_selection(request)
            if error_response is not None:
                return error_response
        else:
            payload = await read_json(request)
            if payload is None and _request_has_body(request):
                return api_error("Batch request body must contain valid JSON.", 400)
            if payload is not None and (not isinstance(payload, dict) or payload):
                return api_error("Batch controls do not accept batch IDs, scene objects, or request fields.", 400)
        try:
            if scene_ids is None:
                result = await asyncio.to_thread(operation, PROJECT_STORAGE, project_id)
            else:
                result = await asyncio.to_thread(operation, PROJECT_STORAGE, project_id, scene_ids=scene_ids)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except ProjectValidationError:
            return api_error("Project or scene selection is invalid.", 400)
        except RenderBatchError as error:
            return render_job_error(error)
        except RenderJobError as error:
            return render_job_error(error)
        except RenderFinalizationError as error:
            return render_job_error(error)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist batch state.")
            return api_error("Batch state could not be persisted.", 500)
        return result

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/render/batch/preview")
    async def music_video_builder_batch_preview(request):
        response = await run_batch_operation(request, preview_batch, read_selection=True)
        if isinstance(response, dict):
            return web.json_response(response)
        return response

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/render/batch/start")
    async def music_video_builder_batch_start(request):
        response = await run_batch_operation(request, start_batch, read_selection=True)
        if isinstance(response, dict):
            return web.json_response(response, status=202)
        return response

    @PromptServer.instance.routes.get("/music-video-builder/projects/{project_id}/render/batch")
    async def music_video_builder_batch_status(request):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)
        try:
            result = await asyncio.to_thread(batch_status, PROJECT_STORAGE, project_id)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except RenderBatchError as error:
            return render_job_error(error)
        except RenderJobError as error:
            return render_job_error(error)
        except ProjectPersistenceError:
            LOGGER.exception("Could not load batch state.")
            return api_error("Batch state could not be loaded.", 500)
        return web.json_response(result)

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/render/batch/pause")
    async def music_video_builder_batch_pause(request):
        response = await run_batch_operation(request, pause_batch_after_current)
        return web.json_response(response) if isinstance(response, dict) else response

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/render/batch/resume")
    async def music_video_builder_batch_resume(request):
        response = await run_batch_operation(request, resume_batch)
        return web.json_response(response) if isinstance(response, dict) else response

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/render/batch/end")
    async def music_video_builder_batch_end(request):
        response = await run_batch_operation(request, end_batch)
        return web.json_response(response) if isinstance(response, dict) else response

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/render/batch/retry-failed")
    async def music_video_builder_batch_retry_failed(request):
        response = await run_batch_operation(request, retry_failed_batch)
        if isinstance(response, dict):
            return web.json_response(response, status=202)
        return response

    @PromptServer.instance.routes.post("/music-video-builder/gpt/{director}/open")
    async def music_video_builder_open_gpt(request):
        payload = await read_json(request)
        if payload is not None and (not isinstance(payload, dict) or payload):
            return api_error("GPT launch does not accept request fields.", 400)
        try:
            result = await asyncio.to_thread(launch_gpt_director, request.match_info["director"])
        except GptDirectorNotFoundError:
            return api_error("GPT Director target was not found.", 404)
        except GptLaunchError:
            LOGGER.warning("The system browser could not open the requested Vesper GPT.")
            return api_error("Could not open the GPT. Open it manually or retry.", 502)
        return web.json_response(result)

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
            synchronized_project = synchronize_storyboard_required_references(
                {**project, "storyboard": storyboard}
            )
            over_capacity_scene_ids = {
                visual_scene["scene_id"]
                for visual_scene in synchronized_project["visuals"]["scenes"]
                if len(visual_scene["reference2video"]["selected_references"]) > MAX_REF2VA_STILL_REFERENCES
            }
            saved_project = PROJECT_STORAGE.save_project(
                project_id,
                synchronized_project,
                allow_storyboard_change=True,
                allow_visuals_change=True,
                allow_visual_over_capacity_scene_ids=over_capacity_scene_ids,
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

    def validate_visual_route_ids(request):
        project_id = request.match_info["project_id"]
        scene_id = request.match_info["scene_id"]
        validate_project_id(project_id)
        validate_entity_id(scene_id, "Scene ID")
        return project_id, scene_id

    def validate_visual_project_id(request):
        project_id = request.match_info["project_id"]
        validate_project_id(project_id)
        return project_id

    async def run_visual_json_mutation(request, operation, fields):
        try:
            project_id, scene_id = validate_visual_route_ids(request)
        except ProjectValidationError:
            return api_error("The project or scene ID is invalid.", 400)
        payload = await read_json(request)
        if not isinstance(payload, dict) or set(payload) != set(fields):
            return api_error("Request body has unsupported or missing Visuals fields.", 400)
        try:
            project = operation(PROJECT_STORAGE, project_id, scene_id, payload)
        except ProjectNotFoundError:
            return api_error("Project or scene was not found.", 404)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist Music Video Builder Visuals mutation.")
            return api_error("Visuals state could not be saved.", 500)
        return web.json_response(project)

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/scenes/{scene_id}/prompt/preview")
    async def music_video_builder_prompt_preview(request):
        try:
            project_id, scene_id = validate_visual_route_ids(request)
        except ProjectValidationError:
            return api_error("The project or scene ID is invalid.", 400)
        payload = await read_json(request)
        if payload is not None and (not isinstance(payload, dict) or payload):
            return api_error("Prompt preview does not accept request fields.", 400)
        try:
            project = PROJECT_STORAGE.load_project(project_id)
            preview = compile_scene_prompt(project, scene_id)
        except ProjectNotFoundError:
            return api_error("Project or scene was not found.", 404)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not load the project for prompt preview.")
            return api_error("Prompt preview could not be built.", 500)
        return web.json_response(preview)

    @PromptServer.instance.routes.get("/music-video-builder/projects/{project_id}/prompts")
    async def music_video_builder_prompt_list(request):
        project_id = request.match_info["project_id"]
        try:
            validate_project_id(project_id)
        except ProjectValidationError:
            return api_error("Invalid project ID.", 400)
        try:
            project = PROJECT_STORAGE.load_project(project_id)
            prompts = build_prompt_list(project, PROJECT_STORAGE)
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not load the project for prompt listing.")
            return api_error("Prompts could not be loaded.", 500)
        return web.json_response(prompts)

    @PromptServer.instance.routes.put("/music-video-builder/projects/{project_id}/scenes/{scene_id}/prompt")
    async def music_video_builder_save_prompt(request):
        try:
            project_id, scene_id = validate_visual_route_ids(request)
        except ProjectValidationError:
            return api_error("The project or scene ID is invalid.", 400)
        payload = await read_json(request)
        if not isinstance(payload, dict):
            return api_error("Request body must be a prompt save object.", 400)
        try:
            project = save_scene_prompt(PROJECT_STORAGE, project_id, scene_id, payload)
        except ProjectNotFoundError:
            return api_error("Project or scene was not found.", 404)
        except PromptConflictError as error:
            return api_error(str(error), 409)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist Music Video Builder final prompt.")
            return api_error("Prompt could not be saved.", 500)
        return web.json_response(project)

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/scenes/{scene_id}/prompt/relay-request")
    async def music_video_builder_prompt_relay_request(request):
        try:
            project_id, scene_id = validate_visual_route_ids(request)
        except ProjectValidationError:
            return api_error("The project or scene ID is invalid.", 400)
        payload = await read_json(request)
        if payload is not None and (not isinstance(payload, dict) or payload):
            return api_error("Prompt relay request does not accept request fields.", 400)
        try:
            project = PROJECT_STORAGE.load_project(project_id)
            relay_request = build_prompt_relay_request(project, scene_id)
        except ProjectNotFoundError:
            return api_error("Project or scene was not found.", 404)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not load the project for a prompt relay request.")
            return api_error("Prompt relay request could not be prepared.", 500)
        return web.json_response(relay_request)

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/scenes/{scene_id}/prompt/relay-response")
    async def music_video_builder_prompt_relay_response(request):
        try:
            project_id, scene_id = validate_visual_route_ids(request)
        except ProjectValidationError:
            return api_error("The project or scene ID is invalid.", 400)
        payload = await read_json(request)
        if not isinstance(payload, dict):
            return api_error("Request body must be a Custom GPT response object.", 400)
        try:
            project = PROJECT_STORAGE.load_project(project_id)
            compiled = compile_scene_prompt(project, scene_id)
            relay_response = validate_prompt_relay_response(compiled, payload)
        except ProjectNotFoundError:
            return api_error("Project or scene was not found.", 404)
        except PromptConflictError as error:
            return api_error(str(error), 409)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not load the project for Custom GPT response validation.")
            return api_error("Custom GPT response could not be validated.", 500)
        return web.json_response(relay_response)

    @PromptServer.instance.routes.put("/music-video-builder/projects/{project_id}/visuals/scenes/{scene_id}/generation-method")
    async def music_video_builder_set_generation_method(request):
        try:
            project_id, scene_id = validate_visual_route_ids(request)
        except ProjectValidationError:
            return api_error("The project or scene ID is invalid.", 400)
        payload = await read_json(request)
        if not isinstance(payload, dict) or set(payload) != {"generation_method"}:
            return api_error("Request body must contain only generation_method.", 400)
        try:
            project = set_generation_method(
                PROJECT_STORAGE,
                project_id,
                scene_id,
                payload["generation_method"],
            )
        except ProjectNotFoundError:
            return api_error("Project or scene was not found.", 404)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist Music Video Builder generation method.")
            return api_error("Generation method could not be saved.", 500)
        return web.json_response(project)

    @PromptServer.instance.routes.put("/music-video-builder/projects/{project_id}/visuals/generation-method")
    async def music_video_builder_set_all_generation_method(request):
        try:
            project_id = validate_visual_project_id(request)
        except ProjectValidationError:
            return api_error("The project ID is invalid.", 400)
        payload = await read_json(request)
        if not isinstance(payload, dict) or set(payload) != {"generation_method"}:
            return api_error("Request body must contain only generation_method.", 400)
        try:
            project = set_all_generation_method(
                PROJECT_STORAGE,
                project_id,
                payload["generation_method"],
            )
        except ProjectNotFoundError:
            return api_error("Project was not found.", 404)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist Music Video Builder bulk generation method.")
            return api_error("Generation method could not be saved.", 500)
        return web.json_response(project)

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/visuals/scenes/{scene_id}/keyframe-prompt")
    async def music_video_builder_generate_keyframe_prompt(request):
        try:
            project_id, scene_id = validate_visual_route_ids(request)
        except ProjectValidationError:
            return api_error("The project or scene ID is invalid.", 400)
        try:
            project = generate_and_save_keyframe_prompt(PROJECT_STORAGE, project_id, scene_id)
        except ProjectNotFoundError:
            return api_error("Project or scene was not found.", 404)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist Music Video Builder keyframe prompt.")
            return api_error("Keyframe prompt could not be saved.", 500)
        return web.json_response(project)

    @PromptServer.instance.routes.put("/music-video-builder/projects/{project_id}/visuals/scenes/{scene_id}/keyframe-details")
    async def music_video_builder_save_keyframe_details(request):
        return await run_visual_json_mutation(
            request,
            save_keyframe_details,
            ("keyframe_generation_prompt", "intended_keyframe_description", "actual_keyframe_description"),
        )

    @PromptServer.instance.routes.post("/music-video-builder/projects/{project_id}/visuals/scenes/{scene_id}/keyframe")
    async def music_video_builder_assign_keyframe(request):
        try:
            project_id, scene_id = validate_visual_route_ids(request)
        except ProjectValidationError:
            return api_error("The project or scene ID is invalid.", 400)
        part = await read_file_part(request)
        if part is None:
            return api_error("Multipart request must contain a file field.", 400)
        try:
            project = await assign_keyframe(
                PROJECT_STORAGE,
                project_id,
                scene_id,
                part.filename,
                part,
            )
        except ProjectNotFoundError:
            return api_error("Project or scene was not found.", 404)
        except ProjectValidationError as error:
            LOGGER.warning("Accepted keyframe upload was rejected: %s", error)
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist Music Video Builder keyframe.")
            return api_error("Keyframe could not be saved.", 500)
        return web.json_response(project)

    @PromptServer.instance.routes.delete("/music-video-builder/projects/{project_id}/visuals/scenes/{scene_id}/keyframe")
    async def music_video_builder_remove_keyframe(request):
        try:
            project_id, scene_id = validate_visual_route_ids(request)
        except ProjectValidationError:
            return api_error("The project or scene ID is invalid.", 400)
        try:
            project = remove_keyframe(PROJECT_STORAGE, project_id, scene_id)
        except (KeyframeNotFoundError, VisualSceneNotFoundError):
            return api_error("Accepted keyframe was not found.", 404)
        except ProjectValidationError as error:
            return api_error(str(error), 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not persist Music Video Builder keyframe removal.")
            return api_error("Keyframe could not be removed.", 500)
        return web.json_response(project)

    @PromptServer.instance.routes.get("/music-video-builder/projects/{project_id}/visuals/scenes/{scene_id}/keyframe/image")
    async def music_video_builder_get_keyframe_image(request):
        try:
            project_id, scene_id = validate_visual_route_ids(request)
        except ProjectValidationError:
            return api_error("The project or scene ID is invalid.", 400)
        try:
            path = get_keyframe_path(PROJECT_STORAGE, project_id, scene_id)
        except KeyframeNotFoundError:
            return api_error("Accepted keyframe was not found.", 404)
        except VisualSceneNotFoundError:
            return api_error("Project or scene was not found.", 404)
        except ProjectNotFoundError:
            return api_error("Project or scene was not found.", 404)
        except ProjectValidationError:
            LOGGER.warning("Unsafe keyframe path rejected.")
            return api_error("Project data is invalid.", 422)
        except ProjectPersistenceError:
            LOGGER.exception("Could not load Music Video Builder keyframe.")
            return api_error("Keyframe could not be loaded.", 500)
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    @PromptServer.instance.routes.put("/music-video-builder/projects/{project_id}/visuals/scenes/{scene_id}/reference2video")
    async def music_video_builder_save_reference_selection(request):
        return await run_visual_json_mutation(
            request,
            save_reference_selection,
            ("selected_references",),
        )

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
            lambda storage, current_project_id, payload: (
                update_resource_draft(storage, current_project_id, "characters", character_id, payload)
                if isinstance(payload, dict) and "reference_ids" in payload
                else update_character(storage, current_project_id, character_id, payload)
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
            lambda storage, current_project_id, payload: (
                update_resource_draft(storage, current_project_id, "locations", location_id, payload)
                if isinstance(payload, dict) and "reference_ids" in payload
                else update_location(storage, current_project_id, location_id, payload)
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
