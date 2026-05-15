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

> **Important:** the project MUST live at `/Applications/Claude Applications/menubar-terminal/`. The LaunchAgent plist (`com.user.menubar-terminal.plist`), `session.py`'s reference to `DtachLauncher.app`, and `CLAUDE.md` all hardcode this path. Cloning to `~/Desktop/...` or anywhere else will break the LaunchAgent and re-trigger the macOS TCC permission prompts that the App Management whitelist is meant to suppress (launchd-spawned `python3` is blocked from `~/Desktop/` by Files & Folders permissions on modern macOS).

```bash
# 1. Clone — path matters (see note above)
sudo mkdir -p "/Applications/Claude Applications"
sudo chown "$(whoami)" "/Applications/Claude Applications"
git clone https://github.com/nondevmendel/menubar-terminal.git "/Applications/Claude Applications/menubar-terminal"
cd "/Applications/Claude Applications/menubar-terminal"

# 2. Install dtach
brew install dtach

# 3. Register the LaunchAgent so it starts at login
#    Edit the plist first — replace YOUR_USERNAME with your actual username:
sed -i '' "s/YOUR_USERNAME/$(whoami)/g" com.user.menubar-terminal.plist
cp com.user.menubar-terminal.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.user.menubar-terminal.plist
```

### Whitelist DtachLauncher.app (one-time)

Inside dtach-attached shells, any process that uses AppleScript (`osascript`) trips macOS's *App Management* permission. The TCC system attributes the prompt to dtach (its responsible-process ancestor). To dismiss it permanently:

1. Open **System Settings → Privacy & Security → App Management**.
2. Click **+**, navigate to `/Applications/Claude Applications/menubar-terminal/DtachLauncher.app`, and add it.
3. Toggle it **on**.

`DtachLauncher.app` is a thin wrapper bundle (ad-hoc signed, `com.user.menubar-terminal.dtach`) around a copy of the real dtach binary. System Settings's `+` button refuses raw CLI binaries, which is why the wrapper exists. If you ever upgrade dtach via Homebrew, refresh the copy:

```bash
cp /opt/homebrew/bin/dtach "/Applications/Claude Applications/menubar-terminal/DtachLauncher.app/Contents/MacOS/dtach"
codesign --force --deep --sign - --identifier com.user.menubar-terminal.dtach \
  "/Applications/Claude Applications/menubar-terminal/DtachLauncher.app"
```

### Run manually (without LaunchAgent)

```bash
python3 "/Applications/Claude Applications/menubar-terminal/menubar_terminal.py" &
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
