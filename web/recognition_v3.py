"""Administrative foundation endpoints; recognition task APIs follow in S19."""

from functools import wraps

from flask import Blueprint, request
from flask_login import current_user


def register_recognition_foundation(app, same_origin):
    routes = Blueprint("recognition_v3_foundation", __name__)

    def administrator(method):
        @wraps(method)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated or str(current_user.get_id()) != "0":
                return {"code": 403, "msg": "仅管理员可以管理媒体识别"}, 403
            if request.method != "GET" and not same_origin():
                return {"code": 403, "msg": "媒体识别管理请求缺少有效的同源来源信息"}, 403
            if request.content_length and request.content_length > 128 * 1024:
                return {"code": 413, "msg": "请求过大"}, 413
            from app.media.recognition.cleanup import CleanupBusy
            from app.media.recognition.configuration import RecognitionConfigurationConflict
            try:
                return method(*args, **kwargs)
            except (CleanupBusy, RecognitionConfigurationConflict) as error:
                return {"code": 409, "msg": str(error)}, 409
            except ValueError as error:
                return {"code": 400, "msg": str(error)}, 400
        return wrapped

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value

    @routes.route("/recognition-records/cleanup/preview", methods=["POST"])
    @administrator
    def preview():
        from app.media.recognition.cleanup import CleanupService
        from config import Config
        data = body()
        scope = data.get("scope", "expired")
        policy = ((Config().get_config("recognition") or {}).get("records") or {}).get("cleanup") if scope == "expired" else None
        return {"code": 0, **CleanupService().preview(current_user.get_id(), scope, policy)}

    @routes.route("/recognition-records/cleanup", methods=["POST"])
    @administrator
    def submit():
        from app.media.recognition.cleanup import CleanupService
        data = body()
        result = CleanupService().submit(data.get("confirmation_token"), current_user.get_id(),
            data.get("idempotency_key"), scope=data.get("scope", "expired"))
        return {"code": 0, **result}, 202

    @routes.route("/recognition-records/cleanup/jobs/<job_id>", methods=["GET"])
    @administrator
    def job(job_id):
        from app.media.recognition.repository import RecognitionRepository
        result = RecognitionRepository().get_task(job_id, current_user.get_id())
        return ({"code": 0, **result}, 200) if result else ({"code": 404, "msg": "清理任务不存在"}, 404)

    @routes.route("/recognition-settings", methods=["GET", "PUT"])
    @administrator
    def settings():
        from app.media.recognition.settings_service import get_settings, save_settings
        if request.method == "GET":
            result = get_settings()
        else:
            data = body()
            result = save_settings(data.get("patch", {}), data.get("expected_version"))
        return {"code": 0, **result}

    @routes.route("/recognition-records/cleanup/jobs/<job_id>/resume", methods=["POST"])
    @administrator
    def resume(job_id):
        from app.media.recognition.cleanup import CleanupService
        return {"code": 0, **CleanupService().resume(job_id, current_user.get_id())}, 202

    @routes.route("/recognition-settings/openai/credential", methods=["PUT"])
    @administrator
    def credential():
        from app.media.recognition.settings_service import save_openai_secret
        data = body()
        return {"code": 0, **save_openai_secret(data.get("api_key"), data.get("clear_api_key", False), data.get("expected_version"))}

    @routes.route("/recognition-settings/openai/test", methods=["POST"])
    @administrator
    def test_openai():
        from app.media.recognition.settings_service import test_openai_connection
        return {"code": 0, **test_openai_connection()}

    app.register_blueprint(routes, url_prefix="/api/v1")
