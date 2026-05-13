import os, json, subprocess, shutil, time, asyncio, pty, select, struct, fcntl, termios
import stats as _stats

_TMUX_BIN  = shutil.which("tmux") or "/opt/homebrew/bin/tmux"

_CACHE_DIR = os.path.expanduser("~/.menubar_terminal")
os.makedirs(_CACHE_DIR, exist_ok=True)

# Private tmux socket + config — isolates us from the user's ~/.tmux.conf
# (avoids tmux-resurrect triggering macOS Automation dialogs on every new session)
_TMUX_SOCKET = "menubar_terminal"
_TMUX_CONF   = os.path.join(_CACHE_DIR, "tmux.conf")
_TMUX_FLAGS  = ["-L", _TMUX_SOCKET, "-f", _TMUX_CONF]

_SAVED_SESSIONS_PATH = os.path.join(_CACHE_DIR, "saved_sessions.json")
_PROJECTS_PATH       = os.path.join(_CACHE_DIR, "projects.json")

_sessions_were_restored: bool = False
_tmux_session_counter: int = 0


def _tmux(*args) -> str:
    try:
        r = subprocess.run([_TMUX_BIN] + _TMUX_FLAGS + list(args),
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except Exception:
        return ""


def _list_sessions():
    out = _tmux("ls", "-F", "#{session_name}|#{session_windows}|#{session_attached}")
    sessions = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) == 3:
            sessions.append({
                "name":     parts[0],
                "windows":  int(parts[1]),
                "attached": parts[2] == "1",
                "title":    "",
            })
    panes_out = _tmux("list-panes", "-a", "-F",
                      "#{session_name}|#{pane_active}|#{window_active}|#{pane_title}")
    titles: dict = {}
    for line in panes_out.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4 and parts[1] == "1" and parts[2] == "1":
            titles[parts[0]] = parts[3]
    for s in sessions:
        s["title"] = titles.get(s["name"], "")
    return sessions


def _sync_session_counter() -> None:
    global _tmux_session_counter
    for s in _list_sessions():
        name = s["name"]
        if name.startswith("tab-"):
            try:
                n = int(name[4:])
                if n > _tmux_session_counter:
                    _tmux_session_counter = n
            except ValueError:
                pass


def _ensure_tmux_titles() -> None:
    _tmux("set-option", "-g", "allow-rename",     "on")
    _tmux("set-option", "-g", "automatic-rename", "on")
    # Prevent tmux from converting scroll events to arrow keys
    _tmux("set-option", "-g", "mouse", "off")


def _get_session_cwds() -> dict:
    out = _tmux("list-panes", "-a", "-F",
                "#{session_name}|#{pane_active}|#{window_active}|#{pane_current_path}")
    cwds: dict = {}
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4 and parts[1] == "1" and parts[2] == "1":
            cwds[parts[0]] = parts[3]
    return cwds


def _save_sessions_simple() -> None:
    sessions = _list_sessions()
    if not sessions:
        return
    cwds = _get_session_cwds()
    payload = [
        {"name": s["name"], "cwd": cwds.get(s["name"], os.path.expanduser("~"))}
        for s in sessions
    ]
    try:
        with open(_SAVED_SESSIONS_PATH, "w") as f:
            json.dump(payload, f)
    except Exception as e:
        print(f"[menubar-terminal] save-sessions error: {e}", flush=True)


def _save_sessions() -> None:
    _save_sessions_simple()


def _restore_sessions_simple() -> None:
    if not os.path.exists(_SAVED_SESSIONS_PATH):
        return
    try:
        with open(_SAVED_SESSIONS_PATH) as f:
            saved = json.load(f)
    except Exception:
        return
    existing = {s["name"] for s in _list_sessions()}
    restored = 0
    for s in saved:
        name = s.get("name", "")
        cwd  = s.get("cwd", os.path.expanduser("~"))
        if name and name not in existing:
            if os.path.isdir(cwd):
                _tmux("new-session", "-d", "-s", name, "-c", cwd)
            else:
                _tmux("new-session", "-d", "-s", name)
            restored += 1
    if restored:
        print(f"[menubar-terminal] restored {restored} session(s) (simple)", flush=True)


def _restore_sessions() -> None:
    global _sessions_were_restored
    _sync_claude_projects()
    if _list_sessions():
        _sync_session_counter()
        return
    before = {s["name"] for s in _list_sessions()}
    _restore_sessions_simple()
    after  = {s["name"] for s in _list_sessions()}
    if after - before:
        _sessions_were_restored = True
    _sync_session_counter()


def _load_projects() -> list:
    try:
        with open(_PROJECTS_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def _save_projects(projects: list) -> None:
    try:
        with open(_PROJECTS_PATH, "w") as f:
            json.dump(projects, f)
    except Exception as e:
        print(f"[menubar-terminal] save-projects error: {e}", flush=True)


def _add_project(path: str) -> None:
    path = os.path.expanduser(path.strip())
    projects = _load_projects()
    if not any(p["path"] == path for p in projects):
        projects.append({"path": path, "name": os.path.basename(path) or path})
        _save_projects(projects)


def _remove_project(path: str) -> None:
    projects = [p for p in _load_projects() if p["path"] != path]
    _save_projects(projects)


def _decode_claude_project_dir(dirname: str) -> str:
    """Decode a ~/.claude/projects/ directory name to an absolute path.

    Claude encodes paths by replacing '/' with '-', so directory names containing
    hyphens are ambiguous. Resolve by testing which segment groupings exist on disk.
    """
    if not dirname.startswith("-"):
        return ""
    parts = dirname[1:].split("-")

    def _resolve(parts: list, current: str) -> str:
        if not parts:
            return current if os.path.isdir(current) else ""
        for n in range(1, len(parts) + 1):
            segment = "-".join(parts[:n])
            candidate = os.path.join(current, segment)
            if os.path.isdir(candidate):
                result = _resolve(parts[n:], candidate)
                if result:
                    return result
        return ""

    return _resolve(parts, "/")


_CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
_HOME = os.path.expanduser("~")


def _sync_claude_projects() -> None:
    """Auto-add any projects Claude knows about that aren't in our list yet."""
    if not os.path.isdir(_CLAUDE_PROJECTS_DIR):
        return
    projects = _load_projects()
    existing = {p["path"] for p in projects}
    added = 0
    for dirname in os.listdir(_CLAUDE_PROJECTS_DIR):
        if not dirname.startswith("-"):
            continue
        path = _decode_claude_project_dir(dirname)
        if not path or path == "/" or path == _HOME:
            continue
        if path not in existing:
            projects.append({"path": path, "name": os.path.basename(path) or path})
            existing.add(path)
            added += 1
    if added:
        _save_projects(projects)
        print(f"[menubar-terminal] auto-added {added} project(s) from Claude", flush=True)


class PTYSession:
    """One pseudo-terminal backed by a tmux session (or bare shell as fallback)."""

    def __init__(self, attach_to: str = None, cwd: str = None) -> None:
        global _tmux_session_counter
        self.clients: set = set()
        self._loop_running = False
        env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}

        if _TMUX_BIN and os.path.exists(_TMUX_BIN):
            if attach_to:
                self.name = attach_to
                cmd = [_TMUX_BIN] + _TMUX_FLAGS + ["attach-session", "-t", attach_to]
            else:
                _tmux_session_counter += 1
                self.name = f"tab-{_tmux_session_counter}"
                start_dir = cwd if (cwd and os.path.isdir(cwd)) else os.path.expanduser("~")
                cmd = [_TMUX_BIN] + _TMUX_FLAGS + ["new-session", "-s", self.name, "-c", start_dir]
            exe = _TMUX_BIN
        else:
            shell = os.environ.get("SHELL", "/bin/zsh")
            self.name = "shell"
            cmd = [shell, "-l"]
            exe = shell

        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execve(exe, cmd, env)
        fl = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        if _TMUX_BIN and os.path.exists(_TMUX_BIN):
            time.sleep(0.15)
            _ensure_tmux_titles()

    def resize(self, rows: int, cols: int) -> None:
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def write(self, data: bytes) -> None:
        try:   os.write(self.fd, data)
        except OSError: pass

    async def _read_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                r, _, _ = await loop.run_in_executor(
                    None, select.select, [self.fd], [], [], 0.05)
                if r:
                    data = os.read(self.fd, 65536)
                    _stats.scan_output(data)
                    dead: set = set()
                    for ws in list(self.clients):
                        try:    await ws.send(data)
                        except: dead.add(ws)
                    self.clients -= dead
            except (OSError, ValueError): break
            except: await asyncio.sleep(0.05)

    def start_loop(self) -> None:
        if not self._loop_running:
            self._loop_running = True
            asyncio.ensure_future(self._read_loop())


async def _title_watcher(ws, session_name: str) -> None:
    last = ""
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(1)
        try:
            title = await loop.run_in_executor(
                None, lambda: _tmux(
                    "display-message", "-p", "-t", session_name,
                    "#{?#{==:#{pane_title},},#{window_name},#{pane_title}}"))
            if title and title != last:
                last = title
                await ws.send(json.dumps({"type": "title", "title": title}))
        except Exception:
            return
