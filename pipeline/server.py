"""
pipeline/server.py

GitHub App Webhook Server。

基于 Flask，接收 GitHub webhook 事件，验证签名，
路由到对应 handler，返回结果。

用法：
    from pipeline.server import create_app
    app = create_app(config, auth)
    app.run(host="0.0.0.0", port=8000)

或通过 CLI：
    repoforge-pipe serve --port 8000

GitHub App 配置要求：
    - Webhook URL: https://your-server.com/webhook
    - Webhook secret: 任意随机字符串
    - Permissions:
        - Issues: Read & Write
        - Pull requests: Read & Write
        - Checks: Read only
        - Contents: Read & Write
    - Events:
        - Issues
        - Pull request
        - Check run
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sys
from functools import wraps
from pathlib import Path

# 确保项目根在 sys.path
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from flask import Flask, Response, abort, request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 事件路由表
# ---------------------------------------------------------------------------

def _build_routes(auth, config):
    """构建事件路由表（使用 EventRouter）。"""
    from pipeline.event_registry import build_default_router
    return build_default_router()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _extract_event_info(event_type: str, payload: dict) -> str:
    """从 payload 中提取人类可读的摘要信息。"""
    if event_type == "issues":
        action = payload.get("action", "?")
        number = payload.get("issue", {}).get("number", "?")
        repo = payload.get("repository", {}).get("full_name", "?")
        return f"issue #{number} {action} ({repo})"

    if event_type == "pull_request":
        action = payload.get("action", "?")
        number = payload.get("pull_request", {}).get("number", "?")
        repo = payload.get("repository", {}).get("full_name", "?")
        return f"PR #{number} {action} ({repo})"

    if event_type == "pull_request_review":
        action = payload.get("action", "?")
        pr_number = payload.get("pull_request", {}).get("number", "?")
        state = payload.get("review", {}).get("state", "?")
        repo = payload.get("repository", {}).get("full_name", "?")
        return f"PR review #{pr_number} {action} ({state}) ({repo})"

    if event_type == "check_run":
        action = payload.get("action", "?")
        name = payload.get("check_run", {}).get("name", "?")
        repo = payload.get("repository", {}).get("full_name", "?")
        return f"check_run '{name}' {action} ({repo})"

    return f"{event_type} / {payload.get('action', '?')}"


def _action_matches(event_type: str, action: str) -> bool:
    """判断事件 + action 组合是否需要处理。"""
    wanted = {
        "issues": {"opened", "labeled", "closed"},
        "issue_comment": {"created"},
        "pull_request": {"opened", "synchronize", "closed", "labeled"},
        "pull_request_review": {"submitted"},
        "check_run": {"completed"},
        "push": {"*"},           # tag push = any action (ref check in handler)
        "dependabot_alert": {"created"},
        "installation": {"created"},
    }
    if event_type == "push":
        return True  # handle_push_tag checks ref internally
    return action in wanted.get(event_type, set())


def _check_run_is_failure(payload: dict) -> bool:
    """检查 check_run 完成时是否为失败状态。"""
    check_run = payload.get("check_run", {})
    return check_run.get("conclusion") in ("failure", "cancelled", "timed_out")


# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------

def create_app(auth, config, webhook_secret: str = ""):
    """
    创建配置好的 Flask application。

    Args:
        auth: GitHubAppAuth 实例
        config: AppConfig 实例
        webhook_secret: GitHub App webhook secret（用于 HMAC 签名验证）

    Returns:
        Flask app
    """
    app = Flask(__name__)

    import logging as _logging
    flask_log = _logging.getLogger("werkzeug")
    flask_log.setLevel(_logging.WARNING)

    routes = _build_routes(auth, config)

    # ------------------------------------------------------------------
    # 签名验证
    # ------------------------------------------------------------------

    def verify_signature(request_body: bytes) -> bool:
        """验证 HMAC-SHA256 webhook 签名。"""
        if not webhook_secret:
            logger.warning("No webhook secret configured — skipping signature check")
            return True

        signature = request.headers.get("X-Hub-Signature-256", "")
        if not signature:
            logger.warning("Missing X-Hub-Signature-256 header")
            return False

        expected = (
            "sha256="
            + hmac.new(
                webhook_secret.encode("utf-8"),
                request_body,
                hashlib.sha256,
            ).hexdigest()
        )
        return hmac.compare_digest(signature, expected)

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.route("/", methods=["GET"])
    def health():
        """健康检查。"""
        return {
            "status": "ok",
            "model": f"{config.llm.provider}/{config.llm.model}",
            "handlers": list({r["event"] for r in routes.list_routes()}),
        }

    @app.route("/webhook", methods=["POST"])
    def webhook():
        """GitHub webhook 端点。"""
        event_type = request.headers.get("X-GitHub-Event", "")
        delivery_id = request.headers.get("X-GitHub-Delivery", "?")

        # 验证签名
        if not verify_signature(request.get_data()):
            logger.warning("Invalid signature for delivery %s", delivery_id)
            abort(403)

        # 解析 payload
        payload = request.get_json(silent=True)
        if not payload:
            abort(400)

        event_info = _extract_event_info(event_type, payload)
        action = payload.get("action", "?")

        logger.info("[%s] %s", delivery_id[:8], event_info)

        # 记录 TTR 接收时间
        try:
            from pipeline.metrics import TTRTracker
            _repo = payload.get("repository", {}).get("full_name", "")
            _num = (payload.get("issue", {}) or payload.get("pull_request", {}) or {}).get("number", 0)
            if _repo and _num:
                TTRTracker.record_receipt(_repo, _num, event_type)
        except Exception:
            pass

        # 过滤不关心的事件类型
        if event_type not in routes:
            logger.debug("No handler for event type: %s", event_type)
            return Response(
                json.dumps({"status": "ignored", "reason": f"no handler for {event_type}"}),
                status=200, mimetype="application/json",
            )

        # 过滤不关心的 action
        if not _action_matches(event_type, action):
            logger.debug("Ignored: %s / %s", event_type, action)
            return Response(
                json.dumps({"status": "ignored", "reason": f"action '{action}' not handled"}),
                status=200, mimetype="application/json",
            )

        # check_run 特殊处理
        if event_type == "check_run" and not _check_run_is_failure(payload):
            conclusion = payload.get("check_run", {}).get("conclusion", "?")
            logger.debug("Check run completed with conclusion=%s, ignored", conclusion)
            return Response(
                json.dumps({"status": "ignored", "reason": f"check_run conclusion='{conclusion}'"}),
                status=200, mimetype="application/json",
            )

        # 路由到 handler
        try:
            handler = routes.resolve(event_type, action)
            if handler is None:
                logger.debug("No handler for %s / %s", event_type, action)
                return Response(
                    json.dumps({"status": "ignored", "reason": f"no handler for {event_type}/{action}"}),
                    status=200, mimetype="application/json",
                )
            result = handler(payload, auth, config)
            logger.info("[%s] Handler result: %s", delivery_id[:8], result)
            return Response(
                json.dumps({"status": "dispatched", "result": result}),
                status=200, mimetype="application/json",
            )
        except Exception:
            logger.exception("[%s] Handler failed", delivery_id[:8])
            return Response(
                json.dumps({"status": "error", "error": "Internal handler error"}),
                status=500, mimetype="application/json",
            )

    from pipeline.dashboard import register_dashboard
    register_dashboard(app, config)

    @app.route("/stats", methods=["GET"])
    def stats():
        """查看当前管线状态。"""
        return {
            "model": f"{config.llm.provider}/{config.llm.model}",
            "max_steps": config.agent.max_steps,
            "handlers": list({r["event"] for r in routes.list_routes()}),
        }

    return app
