"""无第三方依赖的 WSGI 回退入口（本地无 Flask 时自测用）。

容器内仍以 Flask + gunicorn 为准；本入口复用与 Flask 完全相同的 ApiCore，
仅手工实现路由分发与静态文件响应，保证行为一致。

启动方式：python -m app.server_stdlib
"""
from __future__ import annotations

import json
import os
import re
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

from .api import ApiCore, ApiError
from .constants import DEFAULT_BLOCK_COUNT
from .disk import create_image

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8"}


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """多线程 WSGI 服务器：刷盘在等宿主盘写入时，块读写请求照样被处理。"""

    daemon_threads = True

_BLOCK_RE = re.compile(r"^/api/blocks/(\d+)(/read|/write|/write-pattern/(zero|data))?$")


def create_wsgi_app(core: ApiCore):
    def application(environ, start_response):
        method = environ["REQUEST_METHOD"]
        path = environ["PATH_INFO"]
        try:
            body, status_code, ctype = _route(environ, method, path, core)
        except ApiError as exc:
            body, status_code, ctype = (
                json.dumps({"error": exc.message}).encode(),
                exc.status,
                "application/json",
            )
        start_response(
            f"{status_code} {_reason(status_code)}",
            [("Content-Type", ctype), ("Content-Length", str(len(body)))],
        )
        return [body]

    return application


def _route(environ, method: str, path: str, core: ApiCore):
    if path == "/" and method == "GET":
        return _serve_static("index.html"), 200, _CONTENT_TYPES[".html"]
    if path.startswith("/static/"):
        name = path.removeprefix("/static/")
        if "/" in name or ".." in name:
            raise ApiError(404, "not found")
        return _serve_static(name), 200, _CONTENT_TYPES.get(
            os.path.splitext(name)[1], "application/octet-stream"
        )
    if path == "/api/status" and method == "GET":
        return _json(core.status()), 200, "application/json"
    if path == "/api/flush" and method == "POST":
        return _json(core.flush()), 200, "application/json"
    if path == "/api/reclaim/start" and method == "POST":
        return _json(core.reclaim_start()), 200, "application/json"
    if path == "/api/reclaim/cancel" and method == "POST":
        return _json(core.reclaim_cancel()), 200, "application/json"

    match = _BLOCK_RE.match(path)
    if match:
        index = int(match.group(1))
        suffix = match.group(2) or ""
        if suffix == "" and method == "GET":
            return _json(core.get_block(index)), 200, "application/json"
        if suffix == "/read" and method == "POST":
            return _json(core.read_block(index)), 200, "application/json"
        if suffix == "/write" and method == "POST":
            payload = _read_json(environ)
            return _json(core.write_block(index, payload)), 200, "application/json"
        if suffix.startswith("/write-pattern/") and method == "POST":
            return _json(core.write_pattern(index, match.group(3))), 200, "application/json"

    raise ApiError(404, f"无此路由: {method} {path}")


def _read_json(environ) -> dict:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        raw = environ["wsgi.input"].read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "请求体不是合法 JSON")


def _json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _serve_static(name: str) -> bytes:
    full = os.path.join(_STATIC_DIR, name)
    if not os.path.isfile(full):
        raise ApiError(404, "not found")
    with open(full, "rb") as fh:
        return fh.read()


def _reason(code: int) -> str:
    return {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}.get(code, "OK")


def main() -> None:
    image_path = os.environ.get("IMAGE_PATH", "/data/disk.raw")
    block_count = int(os.environ.get("BLOCK_COUNT", str(DEFAULT_BLOCK_COUNT)))
    flush_delay_ms = int(os.environ.get("FLUSH_DELAY_MS", "0"))
    port = int(os.environ.get("PORT", "8000"))
    disk = create_image(image_path, block_count)
    application = create_wsgi_app(ApiCore(disk, flush_delay_ms=flush_delay_ms))
    print(f"[stdlib] serving on 0.0.0.0:{port}, image={image_path}")
    with make_server("0.0.0.0", port, application, server_class=_ThreadingWSGIServer) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
