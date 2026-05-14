"""Session manager using dtach for PTY persistence.

dtach is a minimal (~500 LOC C) attach/detach tool. Unlike tmux it does
no terminal multiplexing, no clipboard handling, no mouse interception —
it is a pure passthrough between the client PTY and the master shell PTY.

Architecture per session:
    dtach -N <socket> -E -z <shell> -l    ← master (foreground, runs shell)
    dtach -a <socket> -E                  ← client (one per WebSocket attach)

Persistence model (same as the previous tmux setup): on save we record
{name, cwd} pairs in saved_sessions.json; on next launch we recreate empty
detached masters at those cwds. Running state is not preserved across
reboots (this was the actual behavior with tmux too; tmux-resurrect was
mentioned in the README but never wired up in code).
"""

import os, json, subprocess, shutil, time, asyncio, pty, select, struct, fcntl, termios, signal
import socket as _socket
import stats as _stats

_DTACH_BIN = shutil.which("dtach") or "/opt/homebrew/bin/dtach"

_CACHE_DIR = os.path.expanduser("~/.menubar_terminal")
_SOCK_DIR  = os.path.join(_CACHE_DIR, "sockets")
os.makedirs(_SOCK_DIR, exist_ok=True)

_SAVED_SESSIONS_PATH = os.path.join(_CACHE_DIR, "saved_sessions.json")

_sessions_were_restored: bool = False
_session_counter: int = 0
_master_pids: dict = {}  # name -> pid (in-memory cache, repopulated from .pid sidecar)
_tracked_pids: set = set()  # dtach masters we Popen'd; reaped on demand to prevent zombies


def _reap_tracked() -> None:
    """Non-blocking reap of any tracked dtach masters that have exited.

    Without this, killed dtach processes accumulate as <defunct> children of
    the Python process — invisible to users but slowly leaking PIDs.
    """
    for pid in list(_tracked_pids):
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done != 0:
                _tracked_pids.discard(pid)
        except ChildProcessError:
            _tracked_pids.discard(pid)
        except OSError:
            pass


def _sock_path(name: str) -> str:
    return os.path.join(_SOCK_DIR, f"{name}.sock")


def _pid_path(name: str) -> str:
    return os.path.join(_SOCK_DIR, f"{name}.pid")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _is_alive(sock_path: str) -> bool:
    if not os.path.exists(sock_path):
        return False
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.settimeout(0.1)
    try:
        s.connect(sock_path)
        s.close()
        return True
    except OSError:
        try: os.unlink(sock_path)
        except OSError: pass
        return False


def _master_pid(name: str):
    pid = _master_pids.get(name)
    if pid and _pid_alive(pid):
        return pid
    try:
        with open(_pid_path(name)) as f:
            pid = int(f.read().strip())
        if _pid_alive(pid):
            _master_pids[name] = pid
            return pid
    except Exception:
        pass
    return None


def _foreground_command(name: str) -> str:
    """Return the name of the foreground process on this session's PTY (e.g. 'vim').

    Walks: dtach master → its shell child → that shell's tty → foreground proc on tty.
    """
    pid = _master_pid(name)
    if not pid:
        return ""
    try:
        r = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=2)
        child = (r.stdout.strip().split("\n") or [""])[0]
        if not child:
            return ""
        r = subprocess.run(["ps", "-o", "tty=", "-p", child], capture_output=True, text=True, timeout=2)
        tty = r.stdout.strip()
        if not tty:
            return ""
        r = subprocess.run(["ps", "-t", tty, "-o", "stat=,comm="], capture_output=True, text=True, timeout=2)
        for line in r.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and "+" in parts[0]:
                return parts[1].split("/")[-1].lstrip("-")
        return ""
    except Exception:
        return ""


def _session_cwd(name: str) -> str:
    """Return the cwd of the session's shell (for save_sessions)."""
    pid = _master_pid(name)
    if not pid:
        return os.path.expanduser("~")
    try:
        r = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=1)
        child = (r.stdout.strip().split("\n") or [""])[0]
        if not child:
            return os.path.expanduser("~")
        r = subprocess.run(["lsof", "-a", "-p", child, "-d", "cwd", "-Fn"],
                           capture_output=True, text=True, timeout=2)
        for line in r.stdout.splitlines():
            if line.startswith("n"):
                return line[1:]
    except Exception:
        pass
    return os.path.expanduser("~")


def _list_sessions() -> list:
    _reap_tracked()
    sessions = []
    if not os.path.isdir(_SOCK_DIR):
        return sessions
    for f in sorted(os.listdir(_SOCK_DIR)):
        if not f.endswith(".sock"):
            continue
        name = f[:-5]
        if not _is_alive(_sock_path(name)):
            try: os.unlink(_pid_path(name))
            except OSError: pass
            _master_pids.pop(name, None)
            continue
        sessions.append({
            "name":     name,
            "windows":  1,
            "attached": False,
            "title":    _foreground_command(name),
        })
    return sessions


def _sync_session_counter() -> None:
    global _session_counter
    for s in _list_sessions():
        name = s["name"]
        if name.startswith("tab-"):
            try:
                n = int(name[4:])
                if n > _session_counter:
                    _session_counter = n
            except ValueError:
                pass


def _start_master(name: str, cwd: str = None) -> int:
    """Start a dtach master in the background. Returns its PID."""
    sock = _sock_path(name)
    if _is_alive(sock):
        return _master_pid(name)
    shell = os.environ.get("SHELL", "/bin/zsh")
    start_dir = cwd if (cwd and os.path.isdir(cwd)) else os.path.expanduser("~")
    env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
    # remove stale socket
    try: os.unlink(sock)
    except OSError: pass
    # -N keeps dtach as the foreground process (no second fork), so Popen.pid IS the master pid
    # -E disables the detach key (^\) since we drive attach/detach from the app
    # -z disables suspend-key handling
    proc = subprocess.Popen(
        [_DTACH_BIN, "-N", sock, "-E", "-z", shell, "-l"],
        cwd=start_dir, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # wait briefly for socket to appear (dtach creates it after fork)
    for _ in range(100):
        if os.path.exists(sock):
            break
        time.sleep(0.02)
    _master_pids[name] = proc.pid
    _tracked_pids.add(proc.pid)
    try:
        with open(_pid_path(name), "w") as f:
            f.write(str(proc.pid))
    except OSError:
        pass
    return proc.pid


def _kill_session(name: str) -> None:
    pid = _master_pid(name)
    if pid:
        try: os.kill(pid, signal.SIGKILL)
        except OSError: pass
    try: os.unlink(_sock_path(name))
    except OSError: pass
    try: os.unlink(_pid_path(name))
    except OSError: pass
    _master_pids.pop(name, None)
    # Give the kernel a moment to finalize the exit, then reap any zombies
    # this kill (or any earlier kill) produced.
    time.sleep(0.05)
    _reap_tracked()


def _save_sessions() -> None:
    _reap_tracked()
    sessions = _list_sessions()
    if not sessions:
        return
    payload = [{"name": s["name"], "cwd": _session_cwd(s["name"])} for s in sessions]
    try:
        with open(_SAVED_SESSIONS_PATH, "w") as f:
            json.dump(payload, f)
    except Exception as e:
        print(f"[menubar-terminal] save-sessions error: {e}", flush=True)


def _migrate_from_tmux() -> None:
    """Kill the legacy tmux server from the previous tmux-based implementation.

    Saved-sessions JSON is forward-compatible (same {name, cwd} schema), so the
    next call to _restore_sessions will recreate the sessions as dtach masters.
    """
    # LaunchAgent runs with a stripped PATH that omits /opt/homebrew/bin, so
    # shutil.which("tmux") returns None even when tmux is installed. Try both.
    tmux_bin = shutil.which("tmux") or "/opt/homebrew/bin/tmux"
    if not os.path.exists(tmux_bin):
        return
    try:
        subprocess.run([tmux_bin, "-L", "menubar_terminal", "kill-server"],
                       capture_output=True, timeout=2)
    except Exception:
        pass


def _restore_sessions() -> None:
    global _sessions_were_restored
    _migrate_from_tmux()
    # already live? (e.g. server restarted but dtach masters survived)
    if _list_sessions():
        _sync_session_counter()
        return
    if not os.path.exists(_SAVED_SESSIONS_PATH):
        return
    try:
        with open(_SAVED_SESSIONS_PATH) as f:
            saved = json.load(f)
    except Exception:
        return
    restored = 0
    for s in saved:
        name = s.get("name", "")
        cwd  = s.get("cwd", os.path.expanduser("~"))
        if not name or _is_alive(_sock_path(name)):
            continue
        _start_master(name, cwd)
        restored += 1
    if restored:
        _sessions_were_restored = True
        print(f"[menubar-terminal] restored {restored} session(s)", flush=True)
    _sync_session_counter()


class PTYSession:
    """One pseudo-terminal wrapping a `dtach -a` client attached to a master."""

    def __init__(self, attach_to: str = None, cwd: str = None) -> None:
        global _session_counter
        self.clients: set = set()
        self._loop_running = False
        env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}

        if attach_to:
            self.name = attach_to
            if not _is_alive(_sock_path(attach_to)):
                _start_master(attach_to, cwd)
        else:
            _session_counter += 1
            self.name = f"tab-{_session_counter}"
            _start_master(self.name, cwd)

        sock = _sock_path(self.name)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.execve(_DTACH_BIN, [_DTACH_BIN, "-a", sock, "-E"], env)
            except Exception:
                os._exit(1)
        # Track the dtach -a client too — it exits on socket-EOF when the master
        # is killed, and would zombie otherwise.
        _tracked_pids.add(self.pid)
        fl = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

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
            title = await loop.run_in_executor(None, lambda: _foreground_command(session_name))
            if title and title != last:
                last = title
                await ws.send(json.dumps({"type": "title", "title": title}))
        except Exception:
            return
