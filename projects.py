"""Project bookmarks for the sessions sidebar.

Lists, persists, and auto-discovers project folders (from ~/.claude/projects).
Originally lived in tmux.py; extracted for clarity.
"""

import os, json

_CACHE_DIR = os.path.expanduser("~/.menubar_terminal")
os.makedirs(_CACHE_DIR, exist_ok=True)

_PROJECTS_PATH       = os.path.join(_CACHE_DIR, "projects.json")
_SKIP_PATH           = os.path.join(_CACHE_DIR, "projects_skip.json")
_CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
_HOME                = os.path.expanduser("~")


def _load_skip() -> set:
    try:
        with open(_SKIP_PATH) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_skip(paths) -> None:
    try:
        with open(_SKIP_PATH, "w") as f:
            json.dump(sorted(paths), f)
    except Exception as e:
        print(f"[menubar-terminal] save-skip error: {e}", flush=True)


def load() -> list:
    try:
        with open(_PROJECTS_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def save(projects: list) -> None:
    try:
        with open(_PROJECTS_PATH, "w") as f:
            json.dump(projects, f)
    except Exception as e:
        print(f"[menubar-terminal] save-projects error: {e}", flush=True)


def add(path: str) -> None:
    path = os.path.expanduser(path.strip())
    projects = load()
    if not any(p["path"] == path for p in projects):
        projects.append({"path": path, "name": os.path.basename(path) or path})
        save(projects)


def remove(path: str) -> None:
    """Remove from the active list AND remember it so the auto-scan won't re-add."""
    save([p for p in load() if p["path"] != path])
    skip = _load_skip()
    skip.add(path)
    _save_skip(skip)


def _decode_claude_dir(dirname: str) -> str:
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


def sync_claude_projects() -> None:
    """Auto-add any projects Claude knows about that aren't in our list yet."""
    if not os.path.isdir(_CLAUDE_PROJECTS_DIR):
        return
    projects = load()
    existing = {p["path"] for p in projects}
    added = 0
    for dirname in os.listdir(_CLAUDE_PROJECTS_DIR):
        if not dirname.startswith("-"):
            continue
        path = _decode_claude_dir(dirname)
        if not path or path == "/" or path == _HOME:
            continue
        if path not in existing:
            projects.append({"path": path, "name": os.path.basename(path) or path})
            existing.add(path)
            added += 1
    if added:
        save(projects)
        print(f"[menubar-terminal] auto-added {added} project(s) from Claude", flush=True)


# Locations scanned for git repos. ~/ is included so home-root projects
# like ~/.rickrubin or ~/menubar-terminal are picked up; standard macOS
# system folders inside ~ are skipped to avoid noise.
_GIT_SCAN_ROOTS = [
    os.path.expanduser("~/Desktop/claude"),
    os.path.expanduser("~/Documents"),
    os.path.expanduser("~/Sites"),
    _HOME,
]
_HOME_SKIP = {
    "Desktop", "Documents", "Sites", "Library", "Music", "Movies",
    "Pictures", "Public", "Downloads", "Applications",
    # very common dotfile-tool dirs that aren't user projects
    ".Trash", ".cache", ".config", ".cargo", ".rustup", ".npm",
    ".local", ".oh-my-zsh", ".vscode", ".cursor", ".docker",
    ".gradle", ".m2", ".gem", ".bundle", ".pyenv", ".nvm",
    ".rbenv", ".tmux", ".vim", ".ssh", ".gnupg", ".aws",
}


def sync_git_repos() -> None:
    """Auto-add any directory containing .git that we haven't seen yet.

    Scans the canonical user-project roots plus ~/ top-level (so newly-created
    repos like ~/.rickrubin show up without having to start a Claude session
    inside them first). Also prunes entries whose directory no longer exists.
    Paths the user has explicitly Remove'd are remembered in projects_skip.json
    and are never re-added.
    """
    projects = load()
    skip = _load_skip()
    # Prune entries whose paths are gone (moved/deleted projects)
    kept = [p for p in projects if os.path.isdir(p.get("path", ""))]
    removed = len(projects) - len(kept)
    projects = kept
    existing = {p["path"] for p in projects}
    added = 0
    for root in _GIT_SCAN_ROOTS:
        if not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if root == _HOME and name in _HOME_SKIP:
                continue
            full = os.path.join(root, name)
            if not os.path.isdir(full):
                continue
            # .git can be a dir (normal) or a file (submodules / worktrees)
            if not os.path.exists(os.path.join(full, ".git")):
                continue
            if full in existing or full in skip:
                continue
            projects.append({"path": full, "name": name})
            existing.add(full)
            added += 1
    if added or removed:
        save(projects)
        msg = []
        if added:   msg.append(f"added {added} git repo(s)")
        if removed: msg.append(f"pruned {removed} stale entry(ies)")
        print(f"[menubar-terminal] projects: {', '.join(msg)}", flush=True)
