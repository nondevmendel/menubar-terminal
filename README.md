# Menubar Terminal

A lightweight macOS menu bar terminal app. Click **⌨** in the menu bar to open a floating, always-on-top terminal with tabs. Every tab runs inside a [dtach](https://dtach.sourceforge.net/) session, so sessions survive app crashes and stay alive when you close the popover.

## Features

- **Floating terminal** — pops up above full-screen apps
- **Tabs** — ⌘T new, ⌘W close, ⌘1–9 switch
- **dtach-backed** — sessions stay alive when you close the popover. dtach is a pure passthrough (no terminal multiplexing, no clipboard interception), so paste from web "copy" buttons works correctly
- **Sessions panel** — view, attach, or kill any session (click ⊞)
- **Claude shortcut** — dedicated button to open a new `claude` tab
- **Reboot persistence** — session names + working directories are saved to `~/.menubar_terminal/saved_sessions.json` and recreated on next launch (empty shells, in the same directories)

## Setup

### Requirements

- macOS (tested on Sequoia / Sonoma)
- Python 3 (system `/usr/bin/python3` works)
- [dtach](https://dtach.sourceforge.net/) — `brew install dtach`
- Python packages are auto-installed on first run (`pyobjc`, `websockets`)

### Install

```bash
# 1. Clone
git clone https://github.com/nondevmendel/menubar-terminal.git ~/Desktop/Claude/Current/menubar-terminal
cd ~/Desktop/Claude/Current/menubar-terminal

# 2. Install dtach
brew install dtach

# 3. Register the LaunchAgent so it starts at login
#    Edit the plist first — replace YOUR_USERNAME with your actual username:
sed -i '' "s/YOUR_USERNAME/$(whoami)/g" com.user.menubar-terminal.plist
cp com.user.menubar-terminal.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.user.menubar-terminal.plist
```

### Run manually (without LaunchAgent)

```bash
python3 ~/Desktop/Claude/Current/menubar-terminal/menubar_terminal.py &
```

## How persistence works

| Event | What happens |
|---|---|
| Popover closed | dtach master keeps the shell running; reattach restores live state |
| App crash / kill | dtach masters survive in the background; reattach normally on next launch |
| Every 5 minutes | Session names + cwds saved to `~/.menubar_terminal/saved_sessions.json` |
| After a session kill | Immediate save |
| System reboot | dtach masters die with the kernel; on next launch the app recreates each saved session as a fresh shell in its saved cwd |

Session sockets live in `~/.menubar_terminal/sockets/`. Each session also has a `.pid` sidecar pointing at its dtach master, so the app can find and clean up sessions after a server restart.

## Why dtach, not tmux

Previous versions used tmux as the persistence layer. dtach replaces it because:

- **Clipboard works.** dtach is a pure byte-passthrough — paste from a "copy" button on a webpage lands in the shell exactly as-is. tmux's paste-buffer detour was corrupting certain inputs.
- **Native scrollback.** xterm.js handles its own scrollback; wheel/trackpad scroll Just Works without escape-sequence translation.
- **Same persistence story.** Sessions survive popover-close, app-restart, and crashes. (Neither tmux nor dtach survives a reboot with running state — both recreate empty shells.)

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| ⌘T | New tab |
| ⌘W | Close tab (shows hide/kill menu) |
| ⌘1–9 | Switch to tab N |
| ⌘C | Copy selection |
| ⌘V | Paste |

## Logs

```bash
tail -f /tmp/menubar-terminal.log
```
