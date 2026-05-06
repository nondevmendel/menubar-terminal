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

import sys, os, pty, asyncio, threading, json, shutil
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
try:    import objc                       # noqa: F401
except: _pip("pyobjc-core","pyobjc-framework-Cocoa","pyobjc-framework-WebKit"); import objc
try:    from AppKit import NSApplication  # noqa: F401
except: _pip("pyobjc-framework-Cocoa")
try:    from WebKit import WKWebView      # noqa: F401
except: _pip("pyobjc-framework-WebKit")
try:    import websockets
except: _pip("websockets"); import websockets

from Foundation import NSObject, NSURL, NSMakeSize
from AppKit import (
    NSApplication, NSStatusBar, NSPopover, NSViewController, NSView,
    NSColor, NSMakeRect, NSPasteboard, NSPasteboardTypeString,
    NSApplicationActivationPolicyAccessory, NSVariableStatusItemLength,
)
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
                "#{session_name}|#{session_windows}|#{session_attached}|#{session_created}")
    sessions = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) == 4:
            sessions.append({
                "name":     parts[0],
                "windows":  int(parts[1]),
                "attached": parts[2] == "1",
            })
    return sessions

# ── port helpers ──────────────────────────────────────────────────────────────
def _free_port(start=57231):
    for p in range(start, start + 200):
        with socket.socket() as s:
            try:   s.bind(("127.0.0.1", p)); return p
            except OSError: pass
    raise RuntimeError("No free port")

WS_PORT = _free_port(57231)

# ── HTML / front-end ──────────────────────────────────────────────────────────
HTML = f"""\
<!DOCTYPE html><html>
<head><meta charset="UTF-8">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{height:100%;background:#0d1117;display:flex;flex-direction:column;overflow:hidden}}

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
.xterm,.xterm-screen,.xterm-viewport{{height:100%!important}}

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
.sdot.on{{background:#3fb950}}
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
</style>
</head>
<body>
<div id="bar">
  <div id="bar-right">
    <button class="bar-btn" id="plus" onclick="nt()" title="New tab  ⌘T">+</button>
    <button class="bar-btn" id="sess-btn" onclick="toggleSessions()" title="Sessions">⊞</button>
  </div>
</div>
<div id="main">
  <div id="terms"></div>
  <div id="sp">
    <div id="sp-head">Sessions</div>
    <div id="sp-list"><div id="sp-empty">No sessions</div></div>
    <div id="sp-foot">
      <button id="sp-new" onclick="newSession()">+ New Session</button>
    </div>
  </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
<script>
var tabs=[],act=null,n=0,spOpen=false,spTimer=null;

/* ── native clipboard bridge ── */
window._termPaste=function(text){{
  var t=tabs.find(function(t){{return t.id===act;}});
  if(t)t.term.paste(text);
}};

/* ── control WebSocket (session management) ── */
var ctrl=null;
function ctrlSend(obj){{
  if(!ctrl||ctrl.readyState!==1){{
    ctrl=new WebSocket('ws://127.0.0.1:{WS_PORT}/control');
    ctrl.onmessage=function(e){{
      var d=JSON.parse(e.data);
      if(d.type==='sessions')renderSessions(d.data);
    }};
    ctrl.onopen=function(){{ctrl.send(JSON.stringify(obj));}};
  }}else{{
    ctrl.send(JSON.stringify(obj));
  }}
}}
function refreshSessions(){{ctrlSend({{action:'list'}});}}

/* ── sessions panel ── */
function toggleSessions(){{
  spOpen=!spOpen;
  document.getElementById('sp').classList.toggle('open',spOpen);
  document.getElementById('sess-btn').classList.toggle('active',spOpen);
  if(spOpen){{
    refreshSessions();
    spTimer=setInterval(refreshSessions,3000);
  }}else{{
    clearInterval(spTimer);spTimer=null;
  }}
  /* refit active terminal */
  var t=tabs.find(function(t){{return t.id===act;}});
  if(t)setTimeout(function(){{t.fa.fit();rsz(t.ws,t.term);}},250);
}}

function renderSessions(list){{
  var el=document.getElementById('sp-list');
  if(!list||!list.length){{
    el.innerHTML='<div id="sp-empty">No tmux sessions</div>';return;
  }}
  el.innerHTML='';
  list.forEach(function(s){{
    var d=document.createElement('div');
    d.className='srow';
    d.innerHTML=
      '<div class="sname"><span class="sdot'+(s.attached?' on':'')+'"></span>'+esc(s.name)+'</div>'+
      '<div class="smeta">'+s.windows+' window'+(s.windows!==1?'s':'')+
      (s.attached?' · active':'')+'</div>'+
      '<div class="sbtns">'+
      '<button class="sbtn" onclick="connectSession(\''+esc(s.name)+'\')">Connect</button>'+
      '<button class="sbtn" onclick="detachSession(\''+esc(s.name)+'\')">Pause</button>'+
      '<button class="sbtn kill" onclick="killSession(\''+esc(s.name)+'\')">Kill</button>'+
      '</div>';
    el.appendChild(d);
  }});
}}

function esc(s){{return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/'/g,"\\'");}}

function connectSession(name){{ntAttach(name);}}
function detachSession(name){{ctrlSend({{action:'detach',name:name}});setTimeout(refreshSessions,400);}}
function killSession(name){{
  if(!confirm('Kill session "'+name+'"? Running processes will be lost.'))return;
  ctrlSend({{action:'kill',name:name}});setTimeout(refreshSessions,400);
}}
function newSession(){{nt();}}

/* ── terminal tabs ── */
function nt(attachTo){{
  var id=++n;
  var wsUrl=attachTo
    ?'ws://127.0.0.1:{WS_PORT}/attach/'+encodeURIComponent(attachTo)
    :'ws://127.0.0.1:{WS_PORT}/new';

  /* tab button */
  var te=document.createElement('div');
  te.className='tab';te.dataset.id=id;
  var tlabel=document.createElement('span');
  tlabel.className='tlabel';
  tlabel.textContent=attachTo?attachTo:'Terminal '+n;
  te.appendChild(tlabel);
  var tx=document.createElement('span');
  tx.className='x';tx.textContent='✕';
  tx.onclick=function(e){{e.stopPropagation();ct(id);}};
  te.appendChild(tx);
  te.onclick=function(){{sw(id);}};
  /* insert tabs before the right-side button group */
  var bar=document.getElementById('bar');
  var right=document.getElementById('bar-right');
  bar.insertBefore(te,right);

  /* terminal container */
  var tw=document.createElement('div');
  tw.className='tw';tw.id='tw'+id;
  document.getElementById('terms').appendChild(tw);

  var term=new Terminal({{
    cursorBlink:true,fontSize:13,lineHeight:1.25,
    fontFamily:'Menlo,"SF Mono",Monaco,"Courier New",monospace',
    theme:{{
      background:'#0d1117',foreground:'#c9d1d9',cursor:'#58a6ff',
      selectionBackground:'rgba(56,139,253,.15)',
      black:'#484f58',red:'#ff7b72',green:'#3fb950',yellow:'#d29922',
      blue:'#58a6ff',magenta:'#bc8cff',cyan:'#39c5cf',white:'#b1bac4',
      brightBlack:'#6e7681',brightRed:'#ffa198',brightGreen:'#56d364',
      brightYellow:'#e3b341',brightBlue:'#79c0ff',brightMagenta:'#d2a8ff',
      brightCyan:'#56d4dd',brightWhite:'#f0f6fc'
    }}}});
  var fa=new FitAddon.FitAddon();
  term.loadAddon(fa);term.open(tw);

  var ws=new WebSocket(wsUrl);
  ws.binaryType='arraybuffer';
  ws.onopen=function(){{fa.fit();rsz(ws,term);}};
  ws.onmessage=function(e){{
    term.write(e.data instanceof ArrayBuffer?new Uint8Array(e.data):e.data);
  }};
  ws.onclose=function(){{if(tabs.some(function(t){{return t.id===id;}}))ct(id);}};
  term.onData(function(d){{if(ws.readyState===1)ws.send(new TextEncoder().encode(d));}});
  term.onResize(function(){{rsz(ws,term);}});
  term.onTitleChange(function(title){{
    if(!title)return;
    tlabel.textContent=title;te.title=title;
  }});
  term.attachCustomKeyEventHandler(function(e){{
    if(e.type!=='keydown')return true;
    if(e.metaKey&&e.key==='c'){{
      if(term.hasSelection()){{
        window.webkit.messageHandlers.copy.postMessage(term.getSelection());
        return false;
      }}
      return true;
    }}
    if(e.metaKey&&e.key==='v'){{
      window.webkit.messageHandlers.paste.postMessage(null);
      return false;
    }}
    return true;
  }});
  new ResizeObserver(function(){{
    if(act===id){{fa.fit();rsz(ws,term);}}
  }}).observe(tw);
  tabs.push({{id:id,term:term,fa:fa,ws:ws,tw:tw,te:te}});
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
    if(on){{t.fa.fit();t.term.focus();}}
  }});
}}

function ct(id){{
  var i=tabs.findIndex(function(t){{return t.id===id;}});
  if(i<0)return;
  var t=tabs[i];
  try{{t.ws.close();}}catch(e){{}}
  t.tw.remove();t.te.remove();tabs.splice(i,1);
  if(!tabs.length)nt();else sw(tabs[Math.max(0,i-1)].id);
}}

document.addEventListener('keydown',function(e){{
  if(e.metaKey&&e.key==='t'){{e.preventDefault();nt();}}
  if(e.metaKey&&e.key==='w'){{e.preventDefault();if(act!==null)ct(act);}}
  var k=parseInt(e.key);
  if(e.metaKey&&!isNaN(k)&&k>=1&&k<=9){{var t=tabs[k-1];if(t)sw(t.id);}}
}});

nt();
</script>
</body></html>
"""

# ── PTY session ───────────────────────────────────────────────────────────────

_tmux_session_counter = 0

class PTYSession:
    """One pseudo-terminal. Runs tmux if available, bare shell as fallback."""

    def __init__(self, attach_to: str = None) -> None:
        global _tmux_session_counter
        self.clients: set = set()
        self._loop_running = False
        env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}

        if TMUX and os.path.exists(TMUX):
            if attach_to:
                cmd = [TMUX, "attach-session", "-t", attach_to]
            else:
                _tmux_session_counter += 1
                sname = f"tab-{_tmux_session_counter}"
                # -A: attach if name exists, else create new
                cmd = [TMUX, "new-session", "-A", "-s", sname]
            exe = TMUX
        else:
            shell = os.environ.get("SHELL", "/bin/zsh")
            cmd = [shell, "-l"]
            exe = shell

        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execve(exe, cmd, env)
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

# ── WebSocket handlers ────────────────────────────────────────────────────────

_sessions: dict = {}

def _ws_path(ws) -> str:
    if hasattr(ws, "request") and hasattr(ws.request, "path"):
        return ws.request.path
    return getattr(ws, "path", "/new")


async def pty_handler(ws, *_) -> None:
    """Handles /new and /attach/<name> — PTY I/O."""
    path = _ws_path(ws).strip("/")          # e.g. "new" or "attach/my-session"

    if path.startswith("attach/"):
        attach_to = path[len("attach/"):]
        session = PTYSession(attach_to=attach_to)
        key = f"attach-{id(session)}"
        _sessions[key] = session
    else:
        key = str(len(_sessions) + 1)
        _sessions[key] = PTYSession()

    session = _sessions[key]
    session.clients.add(ws)
    session.start_loop()

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


class TerminalViewController(NSViewController):
    def loadView(self):
        frame = NSMakeRect(0, 0, 960, 620)
        view = NSView.alloc().initWithFrame_(frame)
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.05, 0.07, 0.09, 1.0).CGColor())

        ucc = WKUserContentController.alloc().init()
        self._bridge = _ClipboardBridge.alloc().init()
        ucc.addScriptMessageHandler_name_(self._bridge, "paste")
        ucc.addScriptMessageHandler_name_(self._bridge, "copy")

        cfg = WKWebViewConfiguration.alloc().init()
        cfg.setUserContentController_(ucc)
        try: cfg.preferences().setValue_forKey_(True, "developerExtrasEnabled")
        except: pass

        wv = WKWebView.alloc().initWithFrame_configuration_(frame, cfg)
        wv.setAutoresizingMask_(18)
        wv.loadHTMLString_baseURL_(HTML, NSURL.URLWithString_("http://127.0.0.1/"))

        self._bridge.setWebView_(wv)
        view.addSubview_(wv)
        self.setView_(view)
        self._wv = wv


class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, _notif):
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

# ── server thread ─────────────────────────────────────────────────────────────

def _run_ws_server() -> None:
    async def _main():
        async with websockets.serve(ws_router, "127.0.0.1", WS_PORT):
            print(f"[menubar-terminal] ws://127.0.0.1:{WS_PORT}")
            await asyncio.Future()
    asyncio.run(_main())

# ── entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_DFL)
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
