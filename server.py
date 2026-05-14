import asyncio, json, os, socket, threading, urllib.parse, urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

import websockets
import session
import projects
import stats as _stats

_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "terminal.html")

# ── port helpers ──────────────────────────────────────────────────────────────

def _free_port(start: int) -> int:
    for p in range(start, start + 200):
        with socket.socket() as s:
            try:   s.bind(("127.0.0.1", p)); return p
            except OSError: pass
    raise RuntimeError("No free port")

WS_PORT   = _free_port(57231)
HTTP_PORT = _free_port(57331)

# ── local asset cache ─────────────────────────────────────────────────────────

_CDN = {
    "xterm.js":           "https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js",
    "xterm-fit.js":       "https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js",
    "xterm.css":          "https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css",
    "xterm-web-links.js": "https://cdn.jsdelivr.net/npm/xterm-addon-web-links@0.9.0/lib/xterm-addon-web-links.js",
}
_assets: dict = {}

def _load_assets() -> None:
    for name, url in _CDN.items():
        path = os.path.join(session._CACHE_DIR, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                _assets[name] = f.read()
        else:
            print(f"[menubar-terminal] Downloading {name} …")
            try:
                with urllib.request.urlopen(url, timeout=15) as r:
                    data = r.read()
                with open(path, "wb") as f:
                    f.write(data)
                _assets[name] = data
            except Exception as e:
                print(f"[menubar-terminal] WARNING: could not fetch {name}: {e}")

_load_assets()

# ── state ─────────────────────────────────────────────────────────────────────

_sessions: dict = {}

# ── WebSocket handlers ────────────────────────────────────────────────────────

def _ws_path(ws) -> str:
    if hasattr(ws, "request") and hasattr(ws.request, "path"):
        return ws.request.path
    return getattr(ws, "path", "/new")


async def pty_handler(ws, *_) -> None:
    raw = _ws_path(ws)
    parsed = urllib.parse.urlparse(raw)
    path = parsed.path.strip("/")
    qs = urllib.parse.parse_qs(parsed.query)
    cwd = urllib.parse.unquote(qs.get("cwd", [""])[0]) or None

    if path.startswith("attach/"):
        attach_to = path[len("attach/"):]
        s = session.PTYSession(attach_to=attach_to)
        key = f"attach-{id(s)}"
        _sessions[key] = s
    else:
        key = str(len(_sessions) + 1)
        _sessions[key] = session.PTYSession(cwd=cwd)

    s = _sessions[key]
    s.clients.add(ws)
    s.start_loop()
    await ws.send(json.dumps({"type": "init", "name": s.name}))

    watcher = asyncio.ensure_future(session._title_watcher(ws, s.name))
    try:
        async for msg in ws:
            if isinstance(msg, bytes):
                s.write(msg)
            else:
                try:
                    cmd = json.loads(msg)
                    if cmd.get("type") == "resize":
                        s.resize(int(cmd["rows"]), int(cmd["cols"]))
                except Exception:
                    s.write(msg.encode() if isinstance(msg, str) else msg)
    except Exception:
        pass
    finally:
        watcher.cancel()
        s.clients.discard(ws)


async def control_handler(ws, *_) -> None:
    try:
        async for msg in ws:
            try:
                cmd = json.loads(msg)
                action = cmd.get("action")
                if action == "list":
                    sessions_list = await asyncio.get_running_loop().run_in_executor(
                        None, session._list_sessions)
                    await ws.send(json.dumps({"type": "sessions", "data": sessions_list}))
                elif action == "kill":
                    name = cmd.get("name", "")
                    await asyncio.get_running_loop().run_in_executor(
                        None, lambda: session._kill_session(name))
                    await asyncio.get_running_loop().run_in_executor(None, session._save_sessions)
            except Exception:
                pass
    except Exception:
        pass


async def ws_router(ws, *args) -> None:
    path = _ws_path(ws)
    if path.startswith("/control"):
        await control_handler(ws, *args)
    else:
        await pty_handler(ws, *args)

# ── HTTP server ───────────────────────────────────────────────────────────────

class _HTMLHandler(BaseHTTPRequestHandler):
    _types = {".js": "text/javascript", ".css": "text/css"}

    def do_GET(self):
        if self.path == "/api/status":
            body = json.dumps({"restored": session._sessions_were_restored}).encode()
            self._send(200, "application/json", body)
            return
        if self.path == "/api/projects":
            projects.sync_claude_projects()
            body = json.dumps(projects.load()).encode()
            self._send(200, "application/json", body)
            return
        if self.path == "/api/stats":
            body = json.dumps(_stats.get()).encode()
            self._send(200, "application/json", body)
            return
        name = self.path.lstrip("/")
        if name in _assets:
            body = _assets[name]
            ct   = self._types.get(os.path.splitext(name)[1], "application/octet-stream")
            self._send(200, ct, body)
        else:
            try:
                with open(_HTML_PATH, "rb") as f:
                    body = f.read().replace(b"%%WS_PORT%%", str(WS_PORT).encode())
            except OSError as e:
                body = f"terminal.html not found: {e}".encode()
            self._send(200, "text/html; charset=utf-8", body, no_cache=True)

    def _send(self, code, ct, body, no_cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        if no_cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

    def do_POST(self):
        data = self._read_json_body()
        if self.path == "/api/stats/reset-tokens":
            _stats.reset_tokens()
            self._send(200, "text/plain", b"")
            return
        if self.path == "/api/projects":
            path = data.get("path", "").strip()
            if path:
                projects.add(path)
        self._send(200, "text/plain", b"")

    def do_DELETE(self):
        data = self._read_json_body()
        if self.path == "/api/projects":
            path = data.get("path", "").strip()
            if path:
                projects.remove(path)
        self._send(200, "text/plain", b"")

    def log_message(self, *_): pass

# ── server thread ─────────────────────────────────────────────────────────────

def _run_ws_server() -> None:
    async def _periodic_save():
        while True:
            await asyncio.sleep(300)
            await asyncio.get_running_loop().run_in_executor(None, session._save_sessions)

    async def _main():
        session._restore_sessions()
        http = HTTPServer(("127.0.0.1", HTTP_PORT), _HTMLHandler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        async with websockets.serve(ws_router, "127.0.0.1", WS_PORT):
            print(f"[menubar-terminal] http://127.0.0.1:{HTTP_PORT}  ws://127.0.0.1:{WS_PORT}")
            asyncio.ensure_future(_periodic_save())
            await asyncio.Future()

    asyncio.run(_main())
