from __future__ import annotations

import json
import mimetypes
import os
import threading
import webbrowser
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from qtrade.config import AppConfig
from qtrade.ui.application import (
    OverviewRepository,
    PipelineTaskManager,
    SubprocessPipelineRunner,
    WatchlistEditor,
)

MAX_BODY_BYTES = 64 * 1024


class UiApplication:
    def __init__(self, config: AppConfig, config_path: Path, assets_root: Path) -> None:
        self.config = config
        self.config_path = Path(config_path)
        self.assets_root = Path(assets_root)
        self.repository = OverviewRepository(config.paths.reports)
        self.watchlist = WatchlistEditor(config_path)
        self.tasks = PipelineTaskManager(
            SubprocessPipelineRunner(
                config_path=config_path,
                working_directory=Path.cwd(),
                curated_root=config.paths.curated,
                provider=config.provider.name,
            )
        )

    def meta(self) -> dict[str, Any]:
        dates = self.repository.available_dates()
        return {
            "dates": dates,
            "latest_date": dates[0] if dates else None,
            "watchlist": self.watchlist.read(),
            "provider": self.config.provider.name,
            "token_configured": bool(
                os.getenv(self.config.provider.token_env, "").strip()
            ),
            "api_url_configured": bool(self.config.provider.api_url()),
        }


def make_handler(application: UiApplication) -> type[BaseHTTPRequestHandler]:
    class QTradeHandler(BaseHTTPRequestHandler):
        server_version = "QTradeUI/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/meta":
                self._json(application.meta())
                return
            if parsed.path == "/api/task":
                self._json(application.tasks.snapshot())
                return
            if parsed.path == "/api/overview":
                try:
                    as_of_date = self._query_date(parsed.query)
                except ValueError as exc:
                    self._error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                self._json(application.repository.overview(as_of_date))
                return
            self._asset(parsed.path)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/api/run":
                self._error(HTTPStatus.NOT_FOUND, "接口不存在。")
                return
            try:
                body = self._body()
                as_of_date = date.fromisoformat(str(body.get("date", "")))
                mode = body.get("mode")
                if mode not in {"update", "existing"}:
                    raise ValueError("运行模式必须是 update 或 existing。")
                snapshot = application.tasks.start(
                    as_of_date,
                    skip_data=mode == "existing",
                )
            except (ValueError, RuntimeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._json(snapshot, status=HTTPStatus.ACCEPTED)

        def do_PUT(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/api/watchlist":
                self._error(HTTPStatus.NOT_FOUND, "接口不存在。")
                return
            try:
                body = self._body()
                symbols = body.get("symbols")
                if not isinstance(symbols, list) or not all(
                    isinstance(value, str) for value in symbols
                ):
                    raise ValueError("自选股必须是字符串数组。")
                values = application.watchlist.write(symbols)
            except (OSError, ValueError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._json({"watchlist": values})

        def _query_date(self, query: str) -> date:
            value = parse_qs(query).get("date", [""])[0]
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("日期必须使用 YYYY-MM-DD 格式。") from exc

        def _body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("请求长度无效。") from exc
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("请求内容为空或过大。")
            try:
                value = json.loads(self.rfile.read(length))
            except json.JSONDecodeError as exc:
                raise ValueError("请求不是有效 JSON。") from exc
            if not isinstance(value, dict):
                raise ValueError("请求 JSON 必须是对象。")
            return value

        def _asset(self, request_path: str) -> None:
            relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
            target = (application.assets_root / relative).resolve()
            root = application.assets_root.resolve()
            if root not in target.parents and target != root:
                self._error(HTTPStatus.NOT_FOUND, "文件不存在。")
                return
            if not target.is_file():
                self._error(HTTPStatus.NOT_FOUND, "文件不存在。")
                return
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            content = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; img-src 'self' data:;",
            )
            self.end_headers()
            self.wfile.write(content)

        def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            content = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def _error(self, status: HTTPStatus, message: str) -> None:
            self._json({"error": message}, status=status)

    return QTradeHandler


def serve_ui(
    config: AppConfig,
    config_path: Path,
    host: str,
    port: int,
    open_browser: bool,
) -> None:
    assets_root = Path(__file__).with_name("assets")
    application = UiApplication(config, config_path, assets_root)
    server = ThreadingHTTPServer((host, port), make_handler(application))
    url = f"http://{host}:{server.server_port}"
    print(f"QTrade UI: {url}")
    print("按 Ctrl+C 停止。")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
