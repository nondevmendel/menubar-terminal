# Menubar Terminal

macOS menu bar terminal with tabs. Click ⌨ in the menu bar → floating xterm.js terminal. Sessions persist across popover-close and app crashes via dtach; empty shells are recreated on reboot.

## Files
- `menubar_terminal.py` — entry point; single-instance lock on 127.0.0.1:57230
- `ui.py` — NSStatusBar item, NSPopover, WKWebView, clipboard bridge
- `server.py` — HTTP (port 57331+) serves `terminal.html`; WebSocket (port 57231+) bridges xterm.js ↔ PTY
- `session.py` — dtach session lifecycle: create/attach/kill/persist
- `projects.py` — project bookmarks sidebar (reads `~/.claude/projects`)
- `stats.py` — daily usage stats; token count parser scraping xterm output
- `terminal.html` — xterm.js frontend

## Key details
- dtach replaced tmux — pure byte passthrough, no clipboard interception, paste-from-web works correctly
- Sessions: sockets in `~/.menubar_terminal/sockets/`, saved state in `~/.menubar_terminal/saved_sessions.json`
- LaunchAgent: `~/Library/LaunchAgents/com.user.menubar-terminal.plist`
- Logs: `/tmp/menubar-terminal.log`

## Running
```bash
python3 ~/Desktop/Claude/Current/menubar-terminal/menubar_terminal.py &
# Auto-start via LaunchAgent:
launchctl load ~/Library/LaunchAgents/com.user.menubar-terminal.plist
```

## Location
Lives at `~/Desktop/Claude/Current/menubar-terminal/`. All active projects live under `~/Desktop/Claude/Current/`; the project sidebar scans there.
