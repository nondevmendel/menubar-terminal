#!/usr/bin/env python3
"""
Menubar Terminal for macOS
──────────────────────────
• Click ⌨ in the menu bar to open a floating terminal
• Right-click ⌨ for a menu (Restart)
• Floats above full-screen apps
• Every tab auto-starts inside dtach (sessions survive popover-close)
• Sessions panel: view / attach / kill any session
• ⌘T = new tab  ⌘W = close tab  ⌘1-9 = switch tab

Run:        python3 "/Applications/Claude Applications/menubar-terminal/menubar_terminal.py" &
Auto-start: load ~/Library/LaunchAgents/com.user.menubar-terminal.plist
"""

import sys, os, socket, subprocess, signal, threading, time

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

try:    import objc
except: _pip("pyobjc-core", "pyobjc-framework-Cocoa", "pyobjc-framework-WebKit"); import objc
try:    from AppKit import NSApplication  # noqa: F401
except: _pip("pyobjc-framework-Cocoa")
try:    from WebKit import WKWebView      # noqa: F401
except: _pip("pyobjc-framework-WebKit")
try:    import websockets
except: _pip("websockets"); import websockets

# ── deferred imports (packages guaranteed installed above) ────────────────────
from server import _run_ws_server
from ui import AppDelegate
import session

from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

# ── entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    def _on_sigterm(_sig, _frame):
        # Use os._exit, not sys.exit: sys.exit raises SystemExit, which — if
        # the signal lands while Python is mid-callback inside a PyObjC bridge
        # (e.g. pbcopy/pbpaste inside the WKWebView script-message handler) —
        # propagates out as an uncaught NSException and crashes the app.
        try:
            session._save_sessions()
        finally:
            os._exit(0)
    signal.signal(signal.SIGTERM, _on_sigterm)

    threading.Thread(target=_run_ws_server, daemon=True).start()
    time.sleep(0.4)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()
