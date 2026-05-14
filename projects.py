"""Project bookmarks for the sessions sidebar.

Lists, persists, and auto-discovers project folders (from ~/.claude/projects).
Originally lived in tmux.py; extracted for clarity.
"""

import os, json

_CACHE_DIR = os.path.expanduser("~/.menubar_terminal")
os.makedirs(_CACHE_DIR, exist_ok=True)

_PROJECTS_PATH       = os.path.join(_CACHE_DIR, "projects.json")
_CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
_HOME                = os.path.expanduser("~")


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
    save([p for p in load() if p["path"] != path])


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
