"""Flask Web 层：路由 + 静态页面。"""
from __future__ import annotations

import os

from .api import ApiCore, ApiError
from .constants import DEFAULT_BLOCK_COUNT
from .disk import DiskImage, create_image


def create_app(image_path: str | None = None, block_count: int = DEFAULT_BLOCK_COUNT):
    from flask import Flask, jsonify, request, send_from_directory

    image_path = image_path or os.environ.get("IMAGE_PATH", "/data/disk.raw")
    block_count = int(os.environ.get("BLOCK_COUNT", str(block_count)))

    disk = create_image(image_path, block_count)
    core = ApiCore(disk)

    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    app = Flask(__name__, static_folder=static_dir, static_url_path="/static")
    app.core = core  # 便于测试与 gunicorn 复用同一实例

    @app.errorhandler(ApiError)
    def _handle_api_error(exc: ApiError):
        return jsonify({"error": exc.message}), exc.status

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception):
        if isinstance(exc, ApiError):
            return _handle_api_error(exc)
        app.logger.exception("unhandled error")
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    @app.get("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    @app.get("/api/status")
    def status():
        return jsonify(core.status())

    @app.get("/api/blocks/<int:index>")
    def get_block(index: int):
        return jsonify(core.get_block(index))

    @app.post("/api/blocks/<int:index>/read")
    def read_block(index: int):
        # 读操作不改变盘上任何状态，仅返回当前 4KiB 块值
        return jsonify(core.read_block(index))

    @app.post("/api/blocks/<int:index>/write")
    def write_block(index: int):
        return jsonify(core.write_block(index, request.get_json(silent=True) or {}))

    @app.post("/api/blocks/<int:index>/write-pattern/<kind>")
    def write_pattern(index: int, kind: str):
        return jsonify(core.write_pattern(index, kind))

    @app.post("/api/reclaim/start")
    def reclaim_start():
        return jsonify(core.reclaim_start())

    @app.post("/api/reclaim/cancel")
    def reclaim_cancel():
        return jsonify(core.reclaim_cancel())

    return app
