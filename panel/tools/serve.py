#!/usr/bin/env python3
"""工单进度视图：把两个库算成「每张在途工单一张卡」（PRD §4.11）。

    python tools/serve.py            # 默认端口 8790
    python tools/serve.py --port 9000

- GET /              返回实时页面（tools/live.html）
- GET /api/overview  返回 JSON，要 Authorization: Bearer <token>；没有或不对都是 401
- GET /window        返回 420×700 的浮窗样张（docs/样张/sample.html）；?source=export 时它读 /api/feed
- GET /api/feed      返回样张吃的 FEED（读 progress-export.json），同样要 token；
                     带 ?since=<fingerprint> 且文件没变时只回一个 unchanged，不重读文件
- 只绑 127.0.0.1，不监听其他网卡
- token 启动时生成并打印在终端；页面第一次打开时粘一次，存浏览器 localStorage。
  token 不进 cookie、不进 URL、不写日志。
- 每次 /api/overview 都重新读两个库（overview.collect_overview），没有缓存

除了这几个 GET 没有别的接口；页面与接口都只有读路径，没有任何写操作。
/api/feed 读的是导出物（默认与中心库同目录的 progress-export.json），只 stat + 读字节，不写盘。
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

_TOOLS = Path(__file__).resolve().parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import datalayer  # noqa: E402
import exportfeed  # noqa: E402
import overview  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8790
PAGE_PATH = _TOOLS / "live.html"
SAMPLE_PATH = _TOOLS.parent / "docs" / "样张" / "sample.html"
TOKEN_PREFIX = "Bearer "


def make_token():
    """每次启动生成一个新 token；只打印在终端，不写盘。"""
    return secrets.token_urlsafe(24)


def banner(token, host, port):
    return (
        "工单进度 · 本地只读服务\n"
        f"地址  http://{host}:{port}/    （只绑本机）\n"
        f"token {token}\n"
        "页面第一次打开时把 token 粘进去，存在浏览器 localStorage，之后不再问。\n"
        "Ctrl+C 停止。"
    )


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, *, token, center_path, cindy_dir, export_path):
        super().__init__(address, handler)
        self.token = token
        self.center_path = center_path
        self.cindy_dir = cindy_dir
        #: 导出物路径（只读）。样张的 /api/feed 就靠它 + mtime 判断「变没变」
        self.export_path = Path(export_path)
        #: 上一次解析结果：{fingerprint, feed}。文件没变就直接交出来，不重读、不重算
        self.feed_cache = None


class OverviewHandler(BaseHTTPRequestHandler):
    server_version = "Dashboard/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------------- 路由

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            self._send_page()
        elif path == "/window":
            self._send_window()
        elif path == "/api/overview":
            self._send_overview()
        elif path == "/api/feed":
            self._send_feed()
        else:
            self._send_error(HTTPStatus.NOT_FOUND, "没有这个地址")

    def do_HEAD(self):
        self._method_not_allowed()

    def do_POST(self):
        self._method_not_allowed()

    def do_PUT(self):
        self._method_not_allowed()

    def do_PATCH(self):
        self._method_not_allowed()

    def do_DELETE(self):
        self._method_not_allowed()

    # ---------------------------------------------------------------- 处理

    def _authorized(self):
        header = self.headers.get("Authorization") or ""
        if not header.startswith(TOKEN_PREFIX):
            return False
        supplied = header[len(TOKEN_PREFIX):].strip()
        if not supplied:
            return False
        try:
            return secrets.compare_digest(supplied, self.server.token)
        except TypeError:
            # 非 ASCII 的请求头不可能等于生成出来的 token，按不对处理
            return False

    def _send_page(self):
        try:
            body = PAGE_PATH.read_bytes()
        except OSError as exc:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"页面读不到：{exc}")
            return
        self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")

    def _send_window(self):
        """浮窗样张：与 / 一样是公开 HTML，token 只在它去拉 /api/feed 时才需要。"""
        try:
            body = SAMPLE_PATH.read_bytes()
        except OSError as exc:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"样张读不到：{exc}")
            return
        self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")

    def _send_feed(self):
        """导出物 → 样张模型。按 mtime 判断变没变：没变只回一个 unchanged。"""
        if not self._authorized():
            self._send_json(
                {"error": "unauthorized"},
                HTTPStatus.UNAUTHORIZED,
                extra_headers={"WWW-Authenticate": 'Bearer realm="dashboard"'},
            )
            return
        try:
            current = exportfeed.fingerprint(self.server.export_path)
        except OSError as exc:
            self._send_json({"ok": False, "error": f"读不到导出物：{exc}"}, HTTPStatus.OK)
            return
        since = self._query().get("since", [""])[0]
        if since and since == current and self.server.feed_cache is not None:
            self._send_json({"ok": True, "unchanged": True, "fingerprint": current}, HTTPStatus.OK)
            return
        cached = self.server.feed_cache
        if cached is not None and cached["fingerprint"] == current:
            feed = cached["feed"]
        else:
            try:
                payload = exportfeed.read_payload(self.server.export_path)
                feed = exportfeed.normalize(payload, int(time.time() * 1000))
            except exportfeed.ExportError as exc:
                # 解析失败不拿旧数据顶替、也不清屏：把原因交回去，页面自己保留上一份
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.OK)
                return
            self.server.feed_cache = {"fingerprint": current, "feed": feed}
        self._send_json({"ok": True, "unchanged": False, "fingerprint": current, "feed": feed},
                        HTTPStatus.OK)

    def _query(self):
        return parse_qs(urlsplit(self.path).query)

    def _send_overview(self):
        if not self._authorized():
            self._send_json(
                {"error": "unauthorized"},
                HTTPStatus.UNAUTHORIZED,
                extra_headers={"WWW-Authenticate": 'Bearer realm="dashboard"'},
            )
            return
        try:
            # 每次请求重新读库：这里不查缓存、不留上一次的结果
            payload = overview.collect_overview(
                center_path=self.server.center_path,
                cindy_dir=self.server.cindy_dir,
            )
        except Exception as exc:  # noqa: BLE001 - 读库异常原样交给页面，不拿旧数据顶替
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(payload, HTTPStatus.OK)

    # ---------------------------------------------------------------- 输出

    def _send(self, status, body, content_type, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload, status=HTTPStatus.OK, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra_headers)

    def _send_error(self, status, message):
        self._send_json({"error": message}, status)

    def _method_not_allowed(self):
        self._send(
            HTTPStatus.METHOD_NOT_ALLOWED,
            b'{"error":"read-only: only GET is allowed"}',
            "application/json; charset=utf-8",
            extra_headers={"Allow": "GET"},
        )

    def log_request(self, code="-", size="-"):
        # 只记方法、路径与状态码；不记查询串与请求头，token 不会进日志
        sys.stderr.write("[dashboard] %s %s %s\n" % (self.command, urlsplit(self.path).path, code))

    def log_error(self, fmt, *args):
        sys.stderr.write("[dashboard] %s\n" % (fmt % args))


def make_server(port=DEFAULT_PORT, token=None, center_path=datalayer.CENTER_DB, cindy_dir=None,
                export_path=None):
    """建一个只绑 127.0.0.1 的服务。port=0 让系统分配（测试用）。host 不开放配置。

    export_path 默认取中心库同目录的 progress-export.json：导出物与库在同一处是这套工具本来的约定，
    不另写死一个 D:/ 开头的路径。
    """
    token = make_token() if token is None else token
    center = Path(center_path)
    if export_path is None:
        export_path = center.parent / "progress-export.json"
    return DashboardServer(
        (HOST, int(port)),
        OverviewHandler,
        token=token,
        center_path=center,
        cindy_dir=None if cindy_dir is None else Path(cindy_dir),
        export_path=Path(export_path),
    )


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="工单进度：本地只读服务")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口，默认 {DEFAULT_PORT}")
    parser.add_argument("--center-db", default=str(datalayer.CENTER_DB), help="中心库路径（只读）")
    parser.add_argument("--cindy-dir", default=None, help="Cindy 主库所在目录（只读）")
    parser.add_argument("--export", default=None,
                        help="进度导出物路径（只读）；默认取中心库同目录的 progress-export.json")
    args = parser.parse_args(argv)

    token = make_token()
    server = make_server(
        port=args.port,
        token=token,
        center_path=args.center_db,
        cindy_dir=args.cindy_dir,
        export_path=args.export,
    )
    print(banner(token, HOST, server.server_address[1]), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
