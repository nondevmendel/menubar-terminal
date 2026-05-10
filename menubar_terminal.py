#!/usr/bin/env python3
"""
Menubar Terminal for macOS
──────────────────────────
• Click ⌨ in the menu bar to open a floating terminal
• Floats above full-screen apps
• Every tab auto-starts inside tmux (sessions survive crashes)
• Sessions panel: view / connect / detach / kill all tmux sessions
• ⌘T = new tab  ⌘W = close tab  ⌘1-9 = switch tab

Run:       python3 ~/menubar_terminal.py &
Auto-start: load ~/Library/LaunchAgents/com.user.menubar-terminal.plist
"""

import sys, os, pty, asyncio, threading, json, shutil, urllib.request, urllib.parse, shlex
import select, struct, fcntl, termios, time, socket, signal, subprocess

# ── single-instance lock ──────────────────────────────────────────────────────
_LOCK_SOCK = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    _LOCK_SOCK.bind(("127.0.0.1", 57230))
except OSError:
    print("[menubar-terminal] Already running — exiting.")
    sys.exit(0)

# ── dependency bootstrap ──────────────────────────────────────────────────────
def _pip(*pkgs):
    print(f"[menubar-terminal] Installing: {' '.join(pkgs)} …")
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", *pkgs], check=True)

import subprocess
try:    import objc
except: _pip("pyobjc-core","pyobjc-framework-Cocoa","pyobjc-framework-WebKit"); import objc
try:    from AppKit import NSApplication  # noqa: F401
except: _pip("pyobjc-framework-Cocoa")
try:    from WebKit import WKWebView      # noqa: F401
except: _pip("pyobjc-framework-WebKit")
try:    import websockets
except: _pip("websockets"); import websockets

from http.server import HTTPServer, BaseHTTPRequestHandler
from Foundation import NSObject, NSURL, NSMakeSize, NSURLRequest
from AppKit import (
    NSApplication, NSStatusBar, NSPopover, NSViewController, NSView,
    NSColor, NSMakeRect, NSPasteboard, NSPasteboardTypeString,
    NSApplicationActivationPolicyAccessory, NSVariableStatusItemLength,
    NSFont, NSForegroundColorAttributeName, NSFontAttributeName,
    NSFilenamesPboardType, NSDragOperationCopy, NSDragOperationNone,
    NSEventModifierFlagCommand,
)
from Foundation import NSMutableAttributedString
from WebKit import WKWebView, WKWebViewConfiguration, WKUserContentController

NSPopoverBehaviorApplicationDefined = 0
NSRectEdgeMinY = 1

# ── tmux ──────────────────────────────────────────────────────────────────────
TMUX = shutil.which("tmux") or "/opt/homebrew/bin/tmux"

def _tmux(*args) -> str:
    """Run a tmux command and return stdout (empty string on error)."""
    try:
        r = subprocess.run([TMUX] + list(args), capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except Exception:
        return ""

def _list_sessions():
    out = _tmux("ls", "-F",
                "#{session_name}|#{session_windows}|#{session_attached}")
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

    # Fetch the active pane title for each session
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

# ── session persistence ───────────────────────────────────────────────────────

_CACHE_DIR = os.path.expanduser("~/.menubar_terminal")
os.makedirs(_CACHE_DIR, exist_ok=True)

_SAVED_SESSIONS_PATH = os.path.join(_CACHE_DIR, "saved_sessions.json")

_RESURRECT_SCRIPTS = os.path.expanduser("~/.tmux/plugins/tmux-resurrect/scripts")
_RESURRECT_SAVE_SH  = os.path.join(_RESURRECT_SCRIPTS, "save.sh")
_RESURRECT_RESTORE_SH = os.path.join(_RESURRECT_SCRIPTS, "restore.sh")
_RESURRECT_LAST = os.path.expanduser("~/.tmux/resurrect/last")

_sessions_were_restored: bool = False   # frontend reads this to open the panel

# ── project shortcuts ─────────────────────────────────────────────────────────

_PROJECTS_PATH = os.path.join(_CACHE_DIR, "projects.json")

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


def _resurrect_available() -> bool:
    return False  # disabled: resurrect save.sh uses AppleScript, triggers macOS permission dialogs


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
    if _resurrect_available() and _list_sessions():
        try:
            env = {**os.environ, "HOME": os.path.expanduser("~")}
            subprocess.run(["bash", _RESURRECT_SAVE_SH],
                           capture_output=True, timeout=15, env=env)
            print("[menubar-terminal] tmux-resurrect: saved", flush=True)
            return
        except Exception as e:
            print(f"[menubar-terminal] resurrect save error: {e}", flush=True)
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
    # If sessions already exist (server survived a crash), just sync the counter.
    if _list_sessions():
        _sync_session_counter()
        return
    if _resurrect_available() and os.path.exists(_RESURRECT_LAST):
        try:
            _tmux("start-server")
            time.sleep(0.2)
            env = {**os.environ, "HOME": os.path.expanduser("~")}
            subprocess.run(["bash", _RESURRECT_RESTORE_SH],
                           capture_output=True, timeout=20, env=env)
            time.sleep(0.6)
            restored = _list_sessions()
            if restored:
                print(f"[menubar-terminal] tmux-resurrect: restored "
                      f"{len(restored)} session(s)", flush=True)
                _sessions_were_restored = True
                _sync_session_counter()
                return
        except Exception as e:
            print(f"[menubar-terminal] resurrect restore error: {e}", flush=True)
    # Fallback: simple JSON-based restore
    before = {s["name"] for s in _list_sessions()}
    _restore_sessions_simple()
    after  = {s["name"] for s in _list_sessions()}
    if after - before:
        _sessions_were_restored = True
    _sync_session_counter()

# ── port helpers ──────────────────────────────────────────────────────────────
def _free_port(start=57231):
    for p in range(start, start + 200):
        with socket.socket() as s:
            try:   s.bind(("127.0.0.1", p)); return p
            except OSError: pass
    raise RuntimeError("No free port")

WS_PORT   = _free_port(57231)
HTTP_PORT = _free_port(57331)

# ── local asset cache (xterm.js served from disk, no CDN in WKWebView) ────────

_CDN = {
    "xterm.js":     "https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js",
    "xterm-fit.js": "https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js",
    "xterm.css":    "https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css",
}
_assets: dict = {}   # filename → bytes

def _load_assets():
    for name, url in _CDN.items():
        path = os.path.join(_CACHE_DIR, name)
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

# ── HTML / front-end ──────────────────────────────────────────────────────────
HTML = f"""\
<!DOCTYPE html><html>
<head><meta charset="UTF-8">
<link rel="stylesheet" href="/xterm.css">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{height:100%;background:#0d1117;display:flex;flex-direction:column;overflow:hidden}}
::-webkit-scrollbar{{width:6px;height:6px}}
::-webkit-scrollbar-track{{background:transparent}}
::-webkit-scrollbar-thumb{{background:#484f58;border-radius:3px}}
::-webkit-scrollbar-thumb:hover{{background:#6e7681}}

/* ── tab bar ── */
#bar{{display:flex;align-items:center;background:#161b22;border-bottom:1px solid #30363d;
     height:36px;padding:0 6px;gap:2px;flex-shrink:0}}
.tab{{display:flex;align-items:center;gap:5px;padding:0 10px;height:28px;border-radius:5px;
     cursor:pointer;font:12px -apple-system,sans-serif;color:#8b949e;user-select:none;max-width:200px}}
.tlabel{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}}
.tab:hover{{background:#21262d;color:#c9d1d9}}
.tab.on{{background:#0d1117;color:#e6edf3;font-weight:500}}
.x{{opacity:0;font-size:10px;margin-left:2px;flex-shrink:0}}
.tab:hover .x{{opacity:.6}}
.x:hover{{opacity:1!important;color:#f85149}}
.tdot{{display:inline-block;width:6px;height:6px;border-radius:50%;flex-shrink:0;margin-right:3px;background:#484f58}}
.tdot.running{{background:#3fb950}}
.tdot.attention{{background:#f0883e}}
#bar-right{{margin-left:auto;display:flex;align-items:center;gap:2px}}
.bar-btn{{background:none;border:none;color:#8b949e;cursor:pointer;
          padding:2px 7px;border-radius:4px;font-family:-apple-system,sans-serif;
          font-size:18px;line-height:1}}
.bar-btn:hover{{background:#21262d;color:#e6edf3}}
.bar-btn.active{{color:#58a6ff;background:#21262d}}

/* ── main area (terminal + sessions panel) ── */
#main{{flex:1;display:flex;overflow:hidden}}
#terms{{flex:1;position:relative;overflow:hidden}}
.tw{{position:absolute;inset:0;display:none}}
.tw.on{{display:block}}
.xterm,.xterm-screen{{height:100%!important}}
.xterm-viewport::-webkit-scrollbar{{display:none}}
.tw>.xterm{{position:absolute;left:0;top:0;bottom:0;right:6px}}
.tscroller{{position:absolute;right:0;top:0;bottom:0;width:6px;z-index:10}}
.tthumb{{position:absolute;right:0;width:6px;background:#484f58;border-radius:3px;cursor:ns-resize}}

/* ── sessions panel (right drawer) ── */
#sp{{width:0;overflow:hidden;transition:width .2s ease;
    background:#161b22;border-left:1px solid #30363d;
    display:flex;flex-direction:column;flex-shrink:0}}
#sp.open{{width:260px}}
#sp-head{{padding:12px 14px 8px;font:600 12px -apple-system,sans-serif;
          color:#8b949e;letter-spacing:.05em;text-transform:uppercase}}
#sp-list{{flex:1;overflow-y:auto;padding:0 8px 8px}}
.srow{{border-radius:6px;padding:8px 10px;margin-bottom:4px;
      background:#0d1117;border:1px solid #21262d;cursor:default}}
.srow:hover{{border-color:#30363d}}
.sname{{font:600 13px -apple-system,sans-serif;color:#e6edf3;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:4px}}
.smeta{{font:11px -apple-system,sans-serif;color:#6e7681;margin-bottom:7px}}
.sdot{{display:inline-block;width:7px;height:7px;border-radius:50%;
      margin-right:5px;background:#484f58;vertical-align:middle}}
.sdot.running{{background:#3fb950}}
.sdot.attention{{background:#f0883e}}
.sp-sec{{padding:8px 10px 3px;font:600 10px -apple-system,sans-serif;
         color:#484f58;letter-spacing:.08em;text-transform:uppercase}}
.sbtns{{display:flex;gap:5px}}
.sbtn{{flex:1;padding:3px 0;border-radius:4px;border:1px solid #30363d;
      background:#21262d;color:#8b949e;font:11px -apple-system,sans-serif;cursor:pointer;
      text-align:center}}
.sbtn:hover{{border-color:#58a6ff;color:#58a6ff}}
.sbtn.kill:hover{{border-color:#f85149;color:#f85149}}
#sp-foot{{padding:8px}}
#sp-new{{width:100%;padding:7px;border-radius:6px;border:1px solid #30363d;
         background:#21262d;color:#8b949e;font:12px -apple-system,sans-serif;cursor:pointer}}
#sp-new:hover{{border-color:#3fb950;color:#3fb950}}
#sp-empty{{padding:20px 14px;font:12px -apple-system,sans-serif;color:#484f58;text-align:center}}

/* ── projects panel (right drawer) ── */
#pp{{width:0;overflow:hidden;transition:width .2s ease;
    background:#161b22;border-left:1px solid #30363d;
    display:flex;flex-direction:column;flex-shrink:0}}
#pp.open{{width:260px}}
#pp-head{{padding:12px 14px 8px;font:600 12px -apple-system,sans-serif;
          color:#8b949e;letter-spacing:.05em;text-transform:uppercase}}
#pp-list{{flex:1;overflow-y:auto;padding:0 8px 8px}}
.prow{{border-radius:6px;padding:8px 10px;margin-bottom:4px;
      background:#0d1117;border:1px solid #21262d}}
.prow:hover{{border-color:#30363d}}
.pname{{font:600 13px -apple-system,sans-serif;color:#e6edf3;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:3px}}
.ppath{{font:10px -apple-system,sans-serif;color:#6e7681;margin-bottom:7px;
       white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
#pp-foot{{padding:8px;display:flex;gap:5px}}
#pp-input{{flex:1;min-width:0;padding:6px 8px;border-radius:5px;border:1px solid #30363d;
          background:#21262d;color:#c9d1d9;font:12px -apple-system,sans-serif;outline:none}}
#pp-input::placeholder{{color:#484f58}}
#pp-input:focus{{border-color:#58a6ff}}
#pp-browse,#pp-add-btn{{padding:6px 9px;border-radius:5px;border:1px solid #30363d;
  background:#21262d;color:#8b949e;font:13px -apple-system,sans-serif;cursor:pointer;flex-shrink:0}}
#pp-browse:hover,#pp-add-btn:hover{{border-color:#3fb950;color:#3fb950}}
#pp-empty{{padding:20px 14px;font:12px -apple-system,sans-serif;color:#484f58;text-align:center}}
</style>
</head>
<body>
<div id="bar">
  <button class="bar-btn" id="plus" onclick="nt()" title="New tab  ⌘T">+</button>
  <button class="bar-btn" id="claude-btn" onclick="ntClaude()" title="New Claude session"><svg width="17" height="17" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="10" cy="13" rx="5" ry="3.5"/><path d="M5 12C2 11 1 8 2.5 7C3.5 6 4.5 7 5 10"/><path d="M15 12C18 11 19 8 17.5 7C16.5 6 15.5 7 15 10"/><line x1="6" y1="16" x2="4" y2="19"/><line x1="8.5" y1="16.5" x2="7.5" y2="19"/><line x1="11.5" y1="16.5" x2="12.5" y2="19"/><line x1="14" y1="16" x2="16" y2="19"/><circle cx="7.5" cy="9.5" r="1" fill="currentColor" stroke="none"/><circle cx="12.5" cy="9.5" r="1" fill="currentColor" stroke="none"/></svg></button>
  <div id="bar-right">
    <button class="bar-btn" id="proj-btn" onclick="toggleProjects()" title="Projects">⊟</button>
    <button class="bar-btn" id="sess-btn" onclick="toggleSessions()" title="Sessions">⊞</button>
  </div>
</div>
<!-- close-tab menu -->
<div id="cm" style="display:none;position:fixed;z-index:9999;background:#161b22;border:1px solid #30363d;border-radius:7px;padding:5px;box-shadow:0 6px 20px rgba(0,0,0,.6);min-width:160px">
  <div style="font:11px -apple-system;color:#6e7681;padding:3px 8px 6px">Close tab</div>
  <button id="cm-pause" style="display:block;width:100%;text-align:left;padding:6px 10px;background:none;border:none;border-radius:5px;color:#c9d1d9;font:13px -apple-system;cursor:pointer" onmouseover="this.style.background='#21262d'" onmouseout="this.style.background='none'">Hide  <span style="color:#6e7681;font-size:11px">(keep running in background)</span></button>
  <button id="cm-kill" style="display:block;width:100%;text-align:left;padding:6px 10px;background:none;border:none;border-radius:5px;color:#f85149;font:13px -apple-system;cursor:pointer" onmouseover="this.style.background='#21262d'" onmouseout="this.style.background='none'">Kill session</button>
</div>
<div id="main">
  <div id="terms"></div>
  <div id="pp">
    <div id="pp-head">Projects</div>
    <div id="pp-list"><div id="pp-empty">No projects saved</div></div>
    <div id="pp-foot">
      <input id="pp-input" type="text" placeholder="/path/to/project">
      <button id="pp-browse" onclick="browseProject()" title="Browse folders">…</button>
      <button id="pp-add-btn" onclick="addProjectInput()" title="Add">+</button>
    </div>
  </div>
  <div id="sp">
    <div id="sp-head">Sessions</div>
    <div id="sp-list"><div id="sp-empty">No sessions</div></div>
    <div id="sp-foot">
      <button id="sp-new" onclick="newSession()">+ New Session</button>
    </div>
  </div>
</div>
<script src="/xterm.js"></script>
<script src="/xterm-fit.js"></script>
<script>
var tabs=[],act=null,n=0,spOpen=false,spTimer=null,ppOpen=false;

function setTabState(obj,state){{
  obj.state=state;
  obj.dotEl.className='tdot '+state;
  updateMenuBarState();
}}
function updateMenuBarState(){{
  var s='idle';
  for(var i=0;i<tabs.length;i++){{
    if(tabs[i].state==='attention'){{s='attention';break;}}
    if(tabs[i].state==='running')s='running';
  }}
  try{{
    window.webkit.messageHandlers.status.postMessage(s);
  }}catch(ex){{
    try{{window.webkit.messageHandlers.log.postMessage('status-err:'+ex);}}catch(e2){{}}
  }}
}}

/* ── native clipboard bridge ── */
window._termPaste=function(text){{
  var t=tabs.find(function(t){{return t.id===act;}});
  if(t)t.term.paste(text);
}};

/* ── close-tab menu ── */
var cm=document.getElementById('cm');
var cmTarget=null;
function showCloseMenu(e,id){{
  e.stopPropagation();
  cmTarget=id;
  cm.style.left=(e.clientX-10)+'px';
  cm.style.top=(e.clientY+4)+'px';
  cm.style.display='block';
}}
document.addEventListener('click',function(){{cm.style.display='none';cmTarget=null;}});
document.getElementById('cm-pause').onclick=function(e){{
  e.stopPropagation();cm.style.display='none';
  if(cmTarget!==null)ct(cmTarget,false);cmTarget=null;
}};
document.getElementById('cm-kill').onclick=function(e){{
  e.stopPropagation();cm.style.display='none';
  if(cmTarget!==null)ct(cmTarget,true);cmTarget=null;
}};

/* ── control WebSocket (session management) ── */
var ctrl=null;
function ctrlSend(obj,cb){{
  if(!ctrl||ctrl.readyState!==1){{
    ctrl=new WebSocket('ws://127.0.0.1:{WS_PORT}/control');
    ctrl.onmessage=function(e){{
      var d=JSON.parse(e.data);
      if(d.type==='sessions')renderSessions(d.data);
      if(cb){{cb();cb=null;}}
    }};
    ctrl.onopen=function(){{ctrl.send(JSON.stringify(obj));}};
  }}else{{
    ctrl.send(JSON.stringify(obj));
    if(cb)setTimeout(cb,300);
  }}
}}
function refreshSessions(){{ctrlSend({{action:'list'}});}}

/* ── sessions panel ── */
function _resizeActive(){{
  var t=tabs.find(function(t){{return t.id===act;}});
  if(t)setTimeout(function(){{t.fa.fit();rsz(t.ws,t.term);}},250);
}}
function toggleSessions(){{
  spOpen=!spOpen;
  document.getElementById('sp').classList.toggle('open',spOpen);
  document.getElementById('sess-btn').classList.toggle('active',spOpen);
  if(spOpen){{refreshSessions();spTimer=setInterval(refreshSessions,3000);}}
  else{{clearInterval(spTimer);spTimer=null;}}
  _resizeActive();
}}
function toggleProjects(){{
  ppOpen=!ppOpen;
  document.getElementById('pp').classList.toggle('open',ppOpen);
  document.getElementById('proj-btn').classList.toggle('active',ppOpen);
  if(ppOpen)refreshProjects();
  _resizeActive();
}}
function refreshProjects(){{
  fetch('/api/projects').then(function(r){{return r.json();}}).then(renderProjects).catch(function(){{}});
}}
function renderProjects(list){{
  var el=document.getElementById('pp-list');
  if(!list||!list.length){{el.innerHTML='<div id="pp-empty">No projects saved.<br><span style="font:10px -apple-system;color:#484f58">Add a path below or use … to browse.</span></div>';return;}}
  el.innerHTML='';
  list.forEach(function(p){{
    var d=document.createElement('div');d.className='prow';
    d.innerHTML='<div class="pname">'+esc(p.name)+'</div>'+
      '<div class="ppath" title="'+esc(p.path)+'">'+esc(p.path)+'</div>'+
      '<div class="pbtns">'+
      '<button class="sbtn" data-path="'+esc(p.path)+'" onclick="openProject(this.dataset.path)">Open</button>'+
      '<button class="sbtn kill" data-path="'+esc(p.path)+'" onclick="removeProject(this.dataset.path)">Remove</button>'+
      '</div>';
    el.appendChild(d);
  }});
}}
function openProject(path){{nt(null,null,path);}}
function removeProject(path){{
  fetch('/api/projects',{{method:'DELETE',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{path:path}})}}).then(refreshProjects);
}}
function addProjectInput(){{
  var inp=document.getElementById('pp-input');
  var path=inp.value.trim();if(!path)return;
  addProject(path);inp.value='';
}}
function addProject(path){{
  if(!path)return;
  fetch('/api/projects',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{path:path}})}}).then(refreshProjects);
}}
function browseProject(){{
  try{{window.webkit.messageHandlers.browse.postMessage(null);}}catch(e){{}}
}}
document.getElementById('pp-input').addEventListener('keydown',function(e){{if(e.key==='Enter')addProjectInput();}});

function renderSessions(list){{
  var el=document.getElementById('sp-list');
  if(!list||!list.length){{el.innerHTML='<div id="sp-empty">No tmux sessions</div>';return;}}
  el.innerHTML='';

  function makeRow(s,tab){{
    var display=s.title||s.name;
    var dotState=tab?tab.state:'idle';
    var primaryBtn=tab
      ?'<button class="sbtn" data-n="'+esc(s.name)+'" onclick="hideTab(this.dataset.n)">Hide</button>'
      :'<button class="sbtn" data-n="'+esc(s.name)+'" onclick="ntAttach(this.dataset.n)">Open in New Tab</button>';
    var d=document.createElement('div');d.className='srow';
    d.innerHTML='<div class="sname"><span class="sdot '+dotState+'"></span>'+esc(display)+'</div>'+
      '<div class="smeta">'+esc(s.name)+' · '+s.windows+' window'+(s.windows!==1?'s':'')+'</div>'+
      '<div class="sbtns">'+primaryBtn+
      '<button class="sbtn kill" data-n="'+esc(s.name)+'" onclick="killSession(this.dataset.n)">Kill</button>'+
      '</div>';
    return d;
  }}

  var open=[],hidden=[];
  list.forEach(function(s){{
    var tab=tabs.find(function(t){{return t.name===s.name;}});
    if(tab)open.push({{s:s,tab:tab}});else hidden.push({{s:s,tab:null}});
  }});

  if(open.length){{
    var h=document.createElement('div');h.className='sp-sec';h.textContent='Open';el.appendChild(h);
    open.forEach(function(x){{el.appendChild(makeRow(x.s,x.tab));}});
  }}
  if(hidden.length){{
    var h=document.createElement('div');h.className='sp-sec';h.textContent='Hidden';el.appendChild(h);
    hidden.forEach(function(x){{el.appendChild(makeRow(x.s,x.tab));}});
  }}
}}
function hideTab(name){{
  var t=tabs.find(function(t){{return t.name===name;}});
  if(t)ct(t.id,false);
}}
function esc(s){{return s.replace(/&/g,'&amp;').replace(/</g,'&lt;');}}
function killSession(name){{
  ctrlSend({{action:'kill',name:name}},function(){{refreshSessions();}});
  /* also close any tab attached to this session */
  var t=tabs.find(function(t){{return t.name===name;}});
  if(t){{try{{t.ws.close();}}catch(ex){{}}t.tw.remove();t.te.remove();tabs=tabs.filter(function(x){{return x.id!==t.id;}});if(!tabs.length)nt();else if(act===t.id)sw(tabs[0].id);}}
}}
function newSession(){{nt();}}

/* ── terminal tabs ── */
/* ── reconnect a frozen tab ── */
function reconnect(obj){{
  var wsUrl=obj.name
    ?'ws://127.0.0.1:{WS_PORT}/attach/'+encodeURIComponent(obj.name)
    :'ws://127.0.0.1:{WS_PORT}/new';
  var ws=new WebSocket(wsUrl);
  ws.binaryType='arraybuffer';
  obj.ws=ws;
  obj.term.writeln('\\r\\n\\x1b[33m[reconnecting…]\\x1b[0m');
  ws.onopen=function(){{obj.fa.fit();rsz(ws,obj.term);}};
  ws.onmessage=function(e){{
    clearTimeout(obj.runTimer);
    setTabState(obj,'running');
    obj.runTimer=setTimeout(function(){{setTabState(obj,'idle');}},1500);
    obj.term.write(e.data instanceof ArrayBuffer?new Uint8Array(e.data):e.data);
  }};
  ws.onclose=function(){{if(tabs.some(function(t){{return t.id===obj.id;}}))ct(obj.id,false);}};
}}

function ntClaude(){{nt(null,'claude\\n');}}
function nt(attachTo,initCmd,projectCwd){{
  var id=++n;
  var wsUrl=attachTo
    ?'ws://127.0.0.1:{WS_PORT}/attach/'+encodeURIComponent(attachTo)
    :(projectCwd
      ?'ws://127.0.0.1:{WS_PORT}/new?cwd='+encodeURIComponent(projectCwd)
      :'ws://127.0.0.1:{WS_PORT}/new');

  var te=document.createElement('div');
  te.className='tab';te.dataset.id=id;
  var tnew=document.createElement('span');tnew.className='tdot';
  te.appendChild(tnew);
  var tlabel=document.createElement('span');tlabel.className='tlabel';
  tlabel.textContent=attachTo||('Tab '+n);
  te.appendChild(tlabel);
  var tx=document.createElement('span');tx.className='x';tx.textContent='✕';
  tx.onclick=function(e){{showCloseMenu(e,id);}};
  te.appendChild(tx);
  te.onclick=function(){{sw(id);}};
  var bar=document.getElementById('bar');
  bar.insertBefore(te,document.getElementById('plus'));

  var tw=document.createElement('div');
  tw.className='tw';tw.id='tw'+id;
  document.getElementById('terms').appendChild(tw);

  tw.style.display='block';
  var term,fa;
  try{{
    term=new Terminal({{
      cursorBlink:true,fontSize:13,lineHeight:1.25,
      fontFamily:'Menlo,"SF Mono",Monaco,"Courier New",monospace',
      theme:{{background:'#0d1117',foreground:'#c9d1d9',cursor:'#58a6ff',
        selectionBackground:'rgba(56,139,253,.15)',
        black:'#484f58',red:'#ff7b72',green:'#3fb950',yellow:'#d29922',
        blue:'#58a6ff',magenta:'#bc8cff',cyan:'#39c5cf',white:'#b1bac4',
        brightBlack:'#6e7681',brightRed:'#ffa198',brightGreen:'#56d364',
        brightYellow:'#e3b341',brightBlue:'#79c0ff',brightMagenta:'#d2a8ff',
        brightCyan:'#56d4dd',brightWhite:'#f0f6fc'}}
    }});
    fa=new (FitAddon.FitAddon||FitAddon)();
    term.loadAddon(fa);
    term.open(tw);
  }}catch(e){{tw.style.display='';return;}}
  tw.style.display='';

  var scrollerEl=document.createElement('div');scrollerEl.className='tscroller';
  var thumbEl=document.createElement('div');thumbEl.className='tthumb';
  scrollerEl.appendChild(thumbEl);tw.appendChild(scrollerEl);

  var obj={{id:id,name:attachTo||'',term:term,fa:fa,ws:null,tw:tw,te:te,dotEl:tnew,state:'idle',runTimer:null,scrollPos:0}};

  var SCROLL_MAX=2000;
  function updateScrollThumb(){{
    var h=scrollerEl.clientHeight||400;
    var thumbH=Math.max(28,Math.round(h*0.15));
    var trackH=h-thumbH;
    thumbEl.style.height=thumbH+'px';
    thumbEl.style.top=Math.round(((SCROLL_MAX-obj.scrollPos)/SCROLL_MAX)*trackH)+'px';
  }}
  updateScrollThumb();
  var _dragY=null,_dragStart=null;
  thumbEl.addEventListener('mousedown',function(e){{e.preventDefault();_dragY=e.clientY;_dragStart=obj.scrollPos;}});
  document.addEventListener('mousemove',function(e){{
    if(_dragY===null)return;
    var h=scrollerEl.clientHeight||400;
    var thumbH=Math.max(28,Math.round(h*0.15));
    var trackH=h-thumbH;if(!trackH)return;
    var newPos=Math.max(0,Math.min(SCROLL_MAX,Math.round(_dragStart-(e.clientY-_dragY)/trackH*SCROLL_MAX)));
    var delta=obj.scrollPos-newPos;
    if(Math.abs(delta)>=1){{
      var seq=new TextEncoder().encode('\x1b[<'+(delta>0?65:64)+';1;1M');
      var n=Math.min(Math.abs(delta),40);
      if(obj.ws&&obj.ws.readyState===1)for(var i=0;i<n;i++)obj.ws.send(seq);
      obj.scrollPos=newPos;updateScrollThumb();
    }}
  }});
  document.addEventListener('mouseup',function(){{_dragY=null;_dragStart=null;}});

  var ws=new WebSocket(wsUrl);
  ws.binaryType='arraybuffer';
  obj.ws=ws;
  var wsReady=false;
  ws.onopen=function(){{wsReady=true;fa.fit();rsz(ws,term);if(initCmd)setTimeout(function(){{if(ws.readyState===1)ws.send(new TextEncoder().encode(initCmd));}},400);}};
  ws.onmessage=function(e){{
    if(!(e.data instanceof ArrayBuffer)){{
      try{{
        var msg=JSON.parse(e.data);
        if(msg.type==='init'){{
          obj.name=msg.name;
          tlabel.textContent=msg.name;
          te.title=msg.name;
          return;
        }}
        if(msg.type==='title'){{
          tlabel.textContent=msg.title;
          te.title=msg.title;
          return;
        }}
      }}catch(ex){{}}
    }}
    if(obj.scrollPos===0){{
      clearTimeout(obj.runTimer);
      setTabState(obj,'running');
      obj.runTimer=setTimeout(function(){{setTabState(obj,'idle');}},1500);
    }}
    term.write(e.data instanceof ArrayBuffer?new Uint8Array(e.data):e.data);
  }};
  ws.onclose=function(){{
    if(wsReady&&obj.ws===ws&&tabs.some(function(t){{return t.id===id;}}))ct(id,false);
  }};
  term.onData(function(d){{if(obj.ws.readyState===1)obj.ws.send(new TextEncoder().encode(d));else if(obj.ws.readyState>1)reconnect(obj);}});
  term.onResize(function(){{rsz(obj.ws,term);}});
  term.onTitleChange(function(title){{if(!title)return;tlabel.textContent=title;te.title=title;}});
  term.attachCustomKeyEventHandler(function(e){{
    if(e.type!=='keydown')return true;
    if(e.metaKey&&e.key==='c'){{
      if(term.hasSelection()){{window.webkit.messageHandlers.copy.postMessage(term.getSelection());return false;}}
      return true;
    }}
    if(e.metaKey&&e.key==='v'){{window.webkit.messageHandlers.paste.postMessage(null);return false;}}
    return true;
  }});
  new ResizeObserver(function(){{if(act===id){{fa.fit();rsz(obj.ws,term);}}}}).observe(tw);

  var _scrollPoll=null;
  function syncScrollPos(){{
    if(!obj.name)return;
    fetch('/api/scroll-info/'+encodeURIComponent(obj.name))
      .then(function(r){{return r.json();}})
      .then(function(d){{
        if(d.size>0)SCROLL_MAX=d.size;
        obj.scrollPos=d.pos;
        updateScrollThumb();
        if(d.pos===0){{clearInterval(_scrollPoll);_scrollPoll=null;}}
      }}).catch(function(){{}});
  }}
  function startScrollSync(){{
    if(_scrollPoll||!obj.name)return;
    _scrollPoll=setInterval(syncScrollPos,250);
  }}

  tw.addEventListener('wheel',function(e){{
    e.preventDefault();e.stopPropagation();
    var goUp=e.deltaY<0;
    var headroom=goUp?(SCROLL_MAX-obj.scrollPos):obj.scrollPos;
    if(headroom<=0)return;
    var steps=Math.min(Math.max(1,Math.round(Math.abs(e.deltaY)/20)),8);
    var seq=new TextEncoder().encode('\x1b[<'+(goUp?64:65)+';1;1M');
    if(obj.ws&&obj.ws.readyState===1)for(var i=0;i<steps;i++)obj.ws.send(seq);
    obj.scrollPos=Math.max(0,Math.min(SCROLL_MAX,obj.scrollPos+(goUp?steps:-steps)));
    updateScrollThumb();
    startScrollSync();
  }},{{passive:false,capture:true}});
  tabs.push(obj);
  sw(id);
  if(spOpen)setTimeout(refreshSessions,800);
}}

function ntAttach(name){{nt(name);}}

function rsz(ws,term){{
  if(ws.readyState===1)
    ws.send(JSON.stringify({{type:'resize',rows:term.rows,cols:term.cols}}));
}}

function sw(id){{
  act=id;
  tabs.forEach(function(t){{
    var on=t.id===id;
    t.tw.classList.toggle('on',on);
    t.te.classList.toggle('on',on);
    if(on){{t.fa.fit();t.term.focus();if(t.state==='attention')setTabState(t,'idle');}}
  }});
}}

function ct(id,doKill){{
  var i=tabs.findIndex(function(t){{return t.id===id;}});
  if(i<0)return;
  var t=tabs[i];
  if(doKill&&t.name)ctrlSend({{action:'kill',name:t.name}},function(){{if(spOpen)refreshSessions();}});
  else if(spOpen)setTimeout(refreshSessions,500);
  clearTimeout(t.runTimer);
  try{{t.ws.close();}}catch(e){{}}
  t.tw.remove();t.te.remove();tabs.splice(i,1);
  if(!tabs.length)nt();else sw(tabs[Math.max(0,i-1)].id);
}}

document.addEventListener('keydown',function(e){{
  if(e.metaKey&&e.key==='t'){{e.preventDefault();nt();}}
  if(e.metaKey&&e.key==='w'){{e.preventDefault();if(act!==null)showCloseMenu({{clientX:0,clientY:36,stopPropagation:function(){{}}}},act);}}
  var k=parseInt(e.key);
  if(e.metaKey&&!isNaN(k)&&k>=1&&k<=9){{var t=tabs[k-1];if(t)sw(t.id);}}
}});

fetch('/api/status').then(function(r){{return r.json();}}).then(function(s){{
  if(s.restored){{toggleSessions();}}else{{nt();}}
}}).catch(function(){{nt();}});
setTimeout(function(){{
  try{{window.webkit.messageHandlers.log.postMessage('bridge-ok');}}catch(e){{}}
  updateMenuBarState();
}}, 1000);
</script>
</body></html>
"""

# ── PTY session ───────────────────────────────────────────────────────────────

_tmux_session_counter = 0


def _sync_session_counter() -> None:
    """Advance the counter past any existing tab-N sessions to avoid collisions."""
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
    """Enable automatic window renaming so #{window_name} stays current."""
    _tmux("set-option", "-g", "allow-rename",     "on")
    _tmux("set-option", "-g", "automatic-rename", "on")
    _tmux("set-option", "-g", "mouse",            "on")

class PTYSession:
    """One pseudo-terminal. Runs tmux if available, bare shell as fallback."""

    def __init__(self, attach_to: str = None, cwd: str = None) -> None:
        global _tmux_session_counter
        self.clients: set = set()
        self._loop_running = False
        env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}

        if TMUX and os.path.exists(TMUX):
            if attach_to:
                self.name = attach_to
                cmd = [TMUX, "attach-session", "-t", attach_to]
            else:
                _tmux_session_counter += 1
                self.name = f"tab-{_tmux_session_counter}"
                start_dir = cwd if (cwd and os.path.isdir(cwd)) else os.path.expanduser("~")
                cmd = [TMUX, "new-session", "-s", self.name, "-c", start_dir]
            exe = TMUX
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
        if TMUX and os.path.exists(TMUX):
            time.sleep(0.15)   # let tmux server start before configuring
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

# ── title polling ─────────────────────────────────────────────────────────────

async def _title_watcher(ws, session_name: str) -> None:
    """Push tab title updates by polling tmux directly — no OSC 2 passthrough needed."""
    last = ""
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(1)
        try:
            # Prefer the pane title (set by apps like Claude Code via OSC 2);
            # fall back to the window name (kept current by automatic-rename).
            title = await loop.run_in_executor(
                None, lambda: _tmux(
                    "display-message", "-p", "-t", session_name,
                    "#{?#{==:#{pane_title},},#{window_name},#{pane_title}}"))
            if title and title != last:
                last = title
                await ws.send(json.dumps({"type": "title", "title": title}))
        except Exception:
            return

# ── WebSocket handlers ────────────────────────────────────────────────────────

_sessions: dict = {}

def _ws_path(ws) -> str:
    if hasattr(ws, "request") and hasattr(ws.request, "path"):
        return ws.request.path
    return getattr(ws, "path", "/new")


async def pty_handler(ws, *_) -> None:
    """Handles /new and /attach/<name> — PTY I/O."""
    raw = _ws_path(ws)
    parsed = urllib.parse.urlparse(raw)
    path = parsed.path.strip("/")           # e.g. "new" or "attach/my-session"
    qs = urllib.parse.parse_qs(parsed.query)
    cwd = urllib.parse.unquote(qs.get("cwd", [""])[0]) or None

    if path.startswith("attach/"):
        attach_to = path[len("attach/"):]
        session = PTYSession(attach_to=attach_to)
        key = f"attach-{id(session)}"
        _sessions[key] = session
    else:
        key = str(len(_sessions) + 1)
        _sessions[key] = PTYSession(cwd=cwd)

    session = _sessions[key]
    session.clients.add(ws)
    session.start_loop()
    await ws.send(json.dumps({"type": "init", "name": session.name}))

    watcher = asyncio.ensure_future(_title_watcher(ws, session.name))
    try:
        async for msg in ws:
            if isinstance(msg, bytes):
                session.write(msg)
            else:
                try:
                    cmd = json.loads(msg)
                    if cmd.get("type") == "resize":
                        session.resize(int(cmd["rows"]), int(cmd["cols"]))
                except Exception:
                    session.write(msg.encode() if isinstance(msg, str) else msg)
    except Exception:
        pass
    finally:
        watcher.cancel()
        session.clients.discard(ws)


async def control_handler(ws, *_) -> None:
    """Handles /control — session management commands."""
    try:
        async for msg in ws:
            try:
                cmd = json.loads(msg)
                action = cmd.get("action")
                if action == "list":
                    sessions = await asyncio.get_running_loop().run_in_executor(
                        None, _list_sessions)
                    await ws.send(json.dumps({"type": "sessions", "data": sessions}))
                elif action == "kill":
                    name = cmd.get("name", "")
                    await asyncio.get_running_loop().run_in_executor(
                        None, lambda: _tmux("kill-session", "-t", name))
                    await asyncio.get_running_loop().run_in_executor(None, _save_sessions)
                elif action == "detach":
                    name = cmd.get("name", "")
                    await asyncio.get_running_loop().run_in_executor(
                        None, lambda: _tmux("detach-client", "-s", name))
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

# ── macOS UI ──────────────────────────────────────────────────────────────────

_app_delegate_ref = None   # set in applicationDidFinishLaunching_


class _ClipboardBridge(NSObject):
    def setWebView_(self, wv):
        self._wv = wv

    def userContentController_didReceiveScriptMessage_(self, _ucc, msg):
        name = str(msg.name())
        if name == "paste":
            pb = NSPasteboard.generalPasteboard()
            text = pb.stringForType_(NSPasteboardTypeString) or ""
            js = "window._termPaste(" + json.dumps(text) + ")"
            self._wv.evaluateJavaScript_completionHandler_(js, None)
        elif name == "copy":
            text = str(msg.body() or "")
            pb = NSPasteboard.generalPasteboard()
            pb.clearContents()
            pb.setString_forType_(text, NSPasteboardTypeString)
        elif name == "browse":
            from AppKit import NSOpenPanel
            panel = NSOpenPanel.openPanel()
            panel.setCanChooseDirectories_(True)
            panel.setCanChooseFiles_(False)
            panel.setAllowsMultipleSelection_(False)
            panel.setPrompt_("Add Project")
            if panel.runModal() == 1:
                url = panel.URL()
                if url:
                    path = str(url.path())
                    js = f"addProject({json.dumps(path)})"
                    self._wv.evaluateJavaScript_completionHandler_(js, None)
        elif name == "log":
            text = str(msg.body() or "")
            _diag_log.append({"msg": text, "t": time.time()})
            print(f"[DIAG] {text}", flush=True)
        elif name == "status":
            state = str(msg.body() or "idle")
            try:
                color = {
                    "running":   NSColor.colorWithSRGBRed_green_blue_alpha_(0.18, 0.80, 0.44, 1.0),
                    "attention": NSColor.colorWithSRGBRed_green_blue_alpha_(1.00, 0.58, 0.00, 1.0),
                }.get(state, NSColor.colorWithSRGBRed_green_blue_alpha_(0.55, 0.57, 0.60, 1.0))
                astr = NSMutableAttributedString.alloc().initWithString_("⌨")
                rng = (0, astr.length())
                astr.addAttribute_value_range_(NSForegroundColorAttributeName, color, rng)
                astr.addAttribute_value_range_(NSFontAttributeName, NSFont.menuBarFontOfSize_(14), rng)
                if _app_delegate_ref is not None:
                    _app_delegate_ref._item.button().setAttributedTitle_(astr)
            except Exception as e:
                print(f"[STATUS] error: {e}", flush=True)


class TerminalWKWebView(WKWebView):
    """WKWebView subclass that intercepts ⌘C/⌘V and accepts file drops."""

    # ── keyboard ──────────────────────────────────────────────────────────────

    def performKeyEquivalent_(self, event):
        if event.modifierFlags() & NSEventModifierFlagCommand:
            key = event.charactersIgnoringModifiers() or ''
            if key == 'v':
                self.evaluateJavaScript_completionHandler_(
                    "window.webkit.messageHandlers.paste.postMessage(null);", None)
                return True
            if key == 'c':
                self.evaluateJavaScript_completionHandler_(
                    "(function(){var t=tabs&&tabs.find(function(t){return t.id===act;});"
                    "if(t&&t.term&&t.term.hasSelection())"
                    "{window.webkit.messageHandlers.copy.postMessage(t.term.getSelection());}})()",
                    None)
                return True
        return objc.super(TerminalWKWebView, self).performKeyEquivalent_(event)

    # ── drag & drop ───────────────────────────────────────────────────────────

    def draggingEntered_(self, sender):
        if NSFilenamesPboardType in (sender.draggingPasteboard().types() or []):
            return NSDragOperationCopy
        return objc.super(TerminalWKWebView, self).draggingEntered_(sender)

    def draggingUpdated_(self, sender):
        if NSFilenamesPboardType in (sender.draggingPasteboard().types() or []):
            return NSDragOperationCopy
        return objc.super(TerminalWKWebView, self).draggingUpdated_(sender)

    def prepareForDragOperation_(self, sender):
        if NSFilenamesPboardType in (sender.draggingPasteboard().types() or []):
            return True
        return objc.super(TerminalWKWebView, self).prepareForDragOperation_(sender)

    def performDragOperation_(self, sender):
        files = sender.draggingPasteboard().propertyListForType_(NSFilenamesPboardType)
        if files:
            text = ' '.join(shlex.quote(str(f)) for f in files) + ' '
            js = f"if(window._termPaste)window._termPaste({json.dumps(text)})"
            self.evaluateJavaScript_completionHandler_(js, None)
            return True
        return objc.super(TerminalWKWebView, self).performDragOperation_(sender)


class TerminalViewController(NSViewController):
    def loadView(self):
        frame = NSMakeRect(0, 0, 960, 620)
        view = NSView.alloc().initWithFrame_(frame)
        view.setWantsLayer_(True)

        ucc = WKUserContentController.alloc().init()
        self._bridge = _ClipboardBridge.alloc().init()
        ucc.addScriptMessageHandler_name_(self._bridge, "paste")
        ucc.addScriptMessageHandler_name_(self._bridge, "copy")
        ucc.addScriptMessageHandler_name_(self._bridge, "log")
        ucc.addScriptMessageHandler_name_(self._bridge, "status")
        ucc.addScriptMessageHandler_name_(self._bridge, "browse")

        cfg = WKWebViewConfiguration.alloc().init()
        cfg.setUserContentController_(ucc)
        try: cfg.preferences().setValue_forKey_(True, "developerExtrasEnabled")
        except: pass

        wv = TerminalWKWebView.alloc().initWithFrame_configuration_(frame, cfg)
        wv.registerForDraggedTypes_([NSFilenamesPboardType])
        wv.setAutoresizingMask_(18)
        url = NSURL.URLWithString_(f"http://127.0.0.1:{HTTP_PORT}/")
        wv.loadRequest_(NSURLRequest.requestWithURL_(url))

        self._bridge.setWebView_(wv)
        view.addSubview_(wv)
        self.setView_(view)
        self._wv = wv


class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, _notif):
        global _app_delegate_ref
        _app_delegate_ref = self
        self._popover = None
        sb = NSStatusBar.systemStatusBar()
        self._item = sb.statusItemWithLength_(NSVariableStatusItemLength)
        btn = self._item.button()
        btn.setTitle_("⌨")
        btn.setToolTip_("Menubar Terminal  —  click to toggle")
        btn.setTarget_(self)
        btn.setAction_("toggle:")

    def toggle_(self, _sender):
        if self._popover and self._popover.isShown():
            self._popover.close()
        else:
            self._open()

    def _open(self):
        if not self._popover:
            self._build_popover()
        btn = self._item.button()
        self._popover.showRelativeToRect_ofView_preferredEdge_(
            btn.bounds(), btn, NSRectEdgeMinY)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        wv = self._vc._wv
        if wv and wv.window():
            wv.window().makeFirstResponder_(wv)

    def _build_popover(self):
        vc = TerminalViewController.alloc().init()
        popover = NSPopover.alloc().init()
        popover.setContentSize_(NSMakeSize(960, 620))
        popover.setBehavior_(NSPopoverBehaviorApplicationDefined)
        popover.setAnimates_(True)
        popover.setContentViewController_(vc)
        self._popover = popover
        self._vc = vc

# ── HTTP server (serves the HTML so CDN scripts load from a real origin) ─────

_diag_log: list = []

class _HTMLHandler(BaseHTTPRequestHandler):
    _types = {".js": "text/javascript", ".css": "text/css"}

    def do_GET(self):
        if self.path == "/diag":
            body = json.dumps(_diag_log).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/status":
            body = json.dumps({"restored": _sessions_were_restored}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/scroll-info/"):
            session = urllib.parse.unquote(self.path[len("/api/scroll-info/"):])
            try:
                pos  = int(_tmux("display-message", "-p", "-t", session, "#{scroll_position}").strip() or "0")
                size = int(_tmux("display-message", "-p", "-t", session, "#{history_size}").strip() or "0")
            except ValueError:
                pos, size = 0, 0
            body = json.dumps({"pos": pos, "size": size}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/projects":
            body = json.dumps(_load_projects()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        name = self.path.lstrip("/")
        if name in _assets:
            body = _assets[name]
            ct   = self._types.get(os.path.splitext(name)[1], "application/octet-stream")
        else:
            body = HTML.encode("utf-8")
            ct   = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        data = self._read_json_body()
        if self.path == "/api/projects":
            path = data.get("path", "").strip()
            if path:
                _add_project(path)
        else:
            _diag_log.append(data)
            print(f"[DIAG] {data.get('msg','')}", flush=True)
        self._ok()

    def do_DELETE(self):
        data = self._read_json_body()
        if self.path == "/api/projects":
            path = data.get("path", "").strip()
            if path:
                _remove_project(path)
        self._ok()

    def log_message(self, *_): pass

# ── server thread ─────────────────────────────────────────────────────────────

def _run_ws_server() -> None:
    async def _periodic_save():
        while True:
            await asyncio.sleep(300)  # every 5 minutes
            await asyncio.get_running_loop().run_in_executor(None, _save_sessions)

    async def _main():
        _restore_sessions()
        http = HTTPServer(("127.0.0.1", HTTP_PORT), _HTMLHandler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        async with websockets.serve(ws_router, "127.0.0.1", WS_PORT):
            print(f"[menubar-terminal] http://127.0.0.1:{HTTP_PORT}  ws://127.0.0.1:{WS_PORT}")
            asyncio.ensure_future(_periodic_save())
            await asyncio.Future()
    asyncio.run(_main())

# ── entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    def _on_sigterm(_sig, _frame):
        _save_sessions()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _on_sigterm)
    threading.Thread(target=_run_ws_server, daemon=True).start()
    time.sleep(0.4)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()

# ─────────────────────────────────────────────────────────────────────────────
# AUTO-START — load once after saving:
#   launchctl load ~/Library/LaunchAgents/com.user.menubar-terminal.plist
# ─────────────────────────────────────────────────────────────────────────────
