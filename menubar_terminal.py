#!/usr/bin/env python3
"""
Menubar Terminal for macOS
──────────────────────────
• Click ⌨ in the menu bar to open a floating terminal
• Floats above full-screen apps
• Multiple tabs (⌘T = new, ⌘W = close, ⌘1-9 = switch)
• Full PTY — runs your real shell, supports colour, vim, htop, etc.

Run:       python3 ~/menubar_terminal.py &
Auto-start: see the launchd plist at the bottom of this file
"""

import sys, os, pty, asyncio, threading, json
import select, struct, fcntl, termios, time, socket, signal, subprocess

# ── single-instance lock (exit immediately if already running) ────────────────
_LOCK_SOCK = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    _LOCK_SOCK.bind(("127.0.0.1", 57230))
except OSError:
    print("[menubar-terminal] Already running — exiting.")
    sys.exit(0)

# ── dependency bootstrap ─────────────────────────────────────────────────────

def _pip(*pkgs):
    print(f"[menubar-terminal] Installing: {' '.join(pkgs)} …")
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", *pkgs], check=True)

try:
    import objc                          # noqa: F401
except ImportError:
    _pip("pyobjc-core", "pyobjc-framework-Cocoa", "pyobjc-framework-WebKit")
    import objc                          # noqa: F401

try:
    from AppKit import NSApplication     # noqa: F401
except ImportError:
    _pip("pyobjc-framework-Cocoa")

try:
    from WebKit import WKWebView         # noqa: F401
except ImportError:
    _pip("pyobjc-framework-WebKit")

try:
    import websockets
except ImportError:
    _pip("websockets")
    import websockets

from Foundation import NSObject, NSURL, NSMakeSize
from AppKit import (
    NSApplication, NSStatusBar, NSPopover, NSViewController, NSView,
    NSColor, NSMakeRect,
    NSApplicationActivationPolicyAccessory,
    NSVariableStatusItemLength,
)
from WebKit import WKWebView, WKWebViewConfiguration

NSPopoverBehaviorApplicationDefined = 0  # stays open until we close it
NSRectEdgeMinY = 1                       # popover drops down from button

# ── port helpers ──────────────────────────────────────────────────────────────

def _free_port(start: int = 57231) -> int:
    for p in range(start, start + 200):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                pass
    raise RuntimeError("No free port found")

WS_PORT = _free_port(57231)

# ── front-end HTML (xterm.js via CDN + WebSocket PTY) ────────────────────────

HTML = f"""\
<!DOCTYPE html><html>
<head><meta charset="UTF-8">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{height:100%;background:#0d1117;display:flex;flex-direction:column;overflow:hidden}}
#bar{{display:flex;align-items:center;background:#161b22;border-bottom:1px solid #30363d;
      height:36px;padding:0 6px;gap:2px;flex-shrink:0}}
.tab{{display:flex;align-items:center;gap:5px;padding:0 10px;height:28px;border-radius:5px;
      cursor:pointer;font:12px -apple-system,sans-serif;color:#8b949e;user-select:none;max-width:220px}}
.tlabel{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}}
.tab:hover{{background:#21262d;color:#c9d1d9}}
.tab.on{{background:#0d1117;color:#e6edf3;font-weight:500}}
.x{{opacity:0;font-size:10px;margin-left:2px}}
.tab:hover .x{{opacity:.7}}
.x:hover{{opacity:1!important;color:#f85149}}
#plus{{background:none;border:none;color:#8b949e;font-size:20px;cursor:pointer;
       padding:2px 6px;border-radius:4px;font-family:-apple-system,sans-serif;line-height:1}}
#plus:hover{{background:#21262d;color:#e6edf3}}
#terms{{flex:1;position:relative}}
.tw{{position:absolute;inset:0;display:none}}
.tw.on{{display:block}}
.xterm,.xterm-screen,.xterm-viewport{{height:100%!important}}
</style>
</head>
<body>
<div id="bar"><button id="plus" onclick="nt()" title="New tab  ⌘T">+</button></div>
<div id="terms"></div>
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
<script>
var tabs=[],act=null,n=0;

function nt(){{
  var id=++n;
  /* tab button */
  var te=document.createElement('div');
  te.className='tab';te.dataset.id=id;
  var tlabel=document.createElement('span');
  tlabel.className='tlabel';tlabel.textContent='Terminal '+n;
  te.appendChild(tlabel);
  var tx=document.createElement('span');
  tx.className='x';tx.textContent='✕';
  tx.onclick=function(e){{e.stopPropagation();ct(id);}};
  te.appendChild(tx);
  te.onclick=function(){{sw(id);}};
  document.getElementById('bar').insertBefore(te,document.getElementById('plus'));
  /* terminal container */
  var tw=document.createElement('div');
  tw.className='tw';tw.id='tw'+id;
  document.getElementById('terms').appendChild(tw);
  /* xterm */
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
  /* WebSocket → PTY */
  var ws=new WebSocket('ws://127.0.0.1:{WS_PORT}/new');
  ws.binaryType='arraybuffer';
  ws.onopen=function(){{fa.fit();rsz(ws,term);}};
  ws.onmessage=function(e){{
    term.write(e.data instanceof ArrayBuffer?new Uint8Array(e.data):e.data);
  }};
  ws.onclose=function(){{
    if(tabs.some(function(t){{return t.id===id;}}))ct(id);
  }};
  term.onData(function(d){{
    if(ws.readyState===1)ws.send(new TextEncoder().encode(d));
  }});
  term.onResize(function(){{rsz(ws,term);}});
  /* update tab label from OSC title sequences (set by the shell) */
  term.onTitleChange(function(title){{
    if(!title)return;
    tlabel.textContent=title;
    te.title=title;  /* full title on hover */
  }});
  /* ⌘C = copy selection (fall through to send ^C if nothing selected)
     ⌘V = paste clipboard into terminal */
  term.attachCustomKeyEventHandler(function(e){{
    if(e.type!=='keydown')return true;
    if(e.metaKey&&e.key==='c'){{
      if(term.hasSelection()){{
        navigator.clipboard.writeText(term.getSelection()).catch(function(){{}});
        return false;
      }}
      return true; /* let xterm send ^C to the PTY */
    }}
    if(e.metaKey&&e.key==='v'){{
      navigator.clipboard.readText().then(function(t){{term.paste(t);}}).catch(function(){{}});
      return false;
    }}
    return true;
  }});
  /* auto-resize when container resizes */
  new ResizeObserver(function(){{
    if(act===id){{fa.fit();rsz(ws,term);}}
  }}).observe(tw);
  tabs.push({{id:id,term:term,fa:fa,ws:ws,tw:tw,te:te}});
  sw(id);
}}

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

class PTYSession:
    """One pseudo-terminal attached to the user's shell."""

    def __init__(self) -> None:
        self.clients: set = set()
        self._loop_running = False
        shell = os.environ.get("SHELL", "/bin/zsh")
        env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
        self.pid, self.fd = pty.fork()
        if self.pid == 0:           # child: exec the shell
            os.execve(shell, [shell, "-l"], env)
        # parent: make fd non-blocking
        fl = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    def resize(self, rows: int, cols: int) -> None:
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def write(self, data: bytes) -> None:
        try:
            os.write(self.fd, data)
        except OSError:
            pass

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
                        try:
                            await ws.send(data)
                        except Exception:
                            dead.add(ws)
                    self.clients -= dead
            except (OSError, ValueError):
                break
            except Exception:
                await asyncio.sleep(0.05)

    def start_loop(self) -> None:
        if not self._loop_running:
            self._loop_running = True
            asyncio.ensure_future(self._read_loop())

# ── WebSocket handler ─────────────────────────────────────────────────────────

_sessions: dict = {}


def _ws_path(ws) -> str:
    """Extract request path regardless of websockets library version."""
    if hasattr(ws, "request") and hasattr(ws.request, "path"):
        return ws.request.path
    return getattr(ws, "path", "/new")


async def ws_handler(ws, *_ignored) -> None:
    """
    Path convention: /new  → spawn a new PTY session
                     /<id> → re-attach to existing session (not yet used by UI)
    """
    path = _ws_path(ws).strip("/")
    key = path.split("/")[-1] if "/" in path else path

    if key == "new" or key not in _sessions:
        key = str(len(_sessions) + 1)
        _sessions[key] = PTYSession()

    session = _sessions[key]
    session.clients.add(ws)
    session.start_loop()          # no-op if already running

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

# ── macOS UI ──────────────────────────────────────────────────────────────────

class TerminalViewController(NSViewController):
    """View controller that hosts the WKWebView inside the NSPopover."""

    def loadView(self):
        w, h = 960, 620
        frame = NSMakeRect(0, 0, w, h)
        view = NSView.alloc().initWithFrame_(frame)
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.05, 0.07, 0.09, 1.0).CGColor())

        cfg = WKWebViewConfiguration.alloc().init()
        try:
            cfg.preferences().setValue_forKey_(True, "developerExtrasEnabled")
        except Exception:
            pass

        wv = WKWebView.alloc().initWithFrame_configuration_(frame, cfg)
        wv.setAutoresizingMask_(18)           # width + height flexible
        wv.loadHTMLString_baseURL_(
            HTML, NSURL.URLWithString_("http://127.0.0.1/"))

        view.addSubview_(wv)
        self.setView_(view)
        self._wv = wv


class AppDelegate(NSObject):

    def applicationDidFinishLaunching_(self, _notif):
        self._popover = None

        # Menu-bar icon
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
        # Activate so the webview captures ⌘C / ⌘V / Ctrl+C etc.
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def _build_popover(self):
        vc = TerminalViewController.alloc().init()

        popover = NSPopover.alloc().init()
        popover.setContentSize_(NSMakeSize(960, 620))
        popover.setBehavior_(NSPopoverBehaviorApplicationDefined)
        popover.setAnimates_(True)
        popover.setContentViewController_(vc)

        self._popover = popover
        self._vc = vc

# ── WebSocket server thread ───────────────────────────────────────────────────

def _run_ws_server() -> None:
    async def _main():
        async with websockets.serve(ws_handler, "127.0.0.1", WS_PORT):
            print(f"[menubar-terminal] WebSocket server on ws://127.0.0.1:{WS_PORT}")
            await asyncio.Future()          # run forever

    asyncio.run(_main())

# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_DFL)   # allow Ctrl-C to kill cleanly

    threading.Thread(target=_run_ws_server, daemon=True).start()
    time.sleep(0.4)                                 # give server a moment to bind

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # no Dock icon

    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()

# ─────────────────────────────────────────────────────────────────────────────
# AUTO-START via launchd
# Save the plist below to ~/Library/LaunchAgents/com.user.menubar-terminal.plist
# then run:
#   launchctl load ~/Library/LaunchAgents/com.user.menubar-terminal.plist
#
# <?xml version="1.0" encoding="UTF-8"?>
# <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
#   "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
# <plist version="1.0">
# <dict>
#   <key>Label</key>
#   <string>com.user.menubar-terminal</string>
#   <key>ProgramArguments</key>
#   <array>
#     <string>/usr/bin/python3</string>
#     <string>/Users/YOUR_USERNAME/menubar_terminal.py</string>
#   </array>
#   <key>RunAtLoad</key>
#   <true/>
#   <key>KeepAlive</key>
#   <true/>
#   <key>StandardErrorPath</key>
#   <string>/tmp/menubar-terminal.log</string>
# </dict>
# </plist>
# ─────────────────────────────────────────────────────────────────────────────
