# Menubar Terminal

A lightweight macOS menu bar terminal app. Click **⌨** in the menu bar to open a floating, always-on-top terminal with tabs. Every tab runs inside a tmux session, so sessions survive app crashes — and now survive reboots too.

## Features

- **Floating terminal** — pops up above full-screen apps
- **Tabs** — ⌘T new, ⌘W close, ⌘1–9 switch
- **tmux-backed** — sessions stay alive when you close the popover
- **Sessions panel** — view, attach, or kill any tmux session (click ⊞)
- **Claude shortcut** — dedicated button to open a new `claude` tab
- **Restart persistence** — sessions are saved via [tmux-resurrect](https://github.com/tmux-plugins/tmux-resurrect) and automatically restored after a reboot, including scrollback and working directories

## Setup

### Requirements

- macOS (tested on Sequoia / Sonnet)
- Python 3 (system `/usr/bin/python3` works)
- [tmux](https://github.com/tmux/tmux) — `brew install tmux`
- Python packages are auto-installed on first run (`pyobjc`, `websockets`)

### Install

```bash
# 1. Clone
git clone https://github.com/nondevmendel/menubar-terminal.git ~/menubar-terminal
cd ~/menubar-terminal

# 2. Install tmux-resurrect (for session persistence across reboots)
mkdir -p ~/.tmux/plugins
git clone --depth=1 https://github.com/tmux-plugins/tmux-resurrect \
    ~/.tmux/plugins/tmux-resurrect

# 3. Copy the tmux config (enables scrollback saving)
cp ~/.tmux.conf ~/.tmux.conf.bak 2>/dev/null; true
cat >> ~/.tmux.conf <<'EOF'
run-shell ~/.tmux/plugins/tmux-resurrect/resurrect.tmux
set -g @resurrect-capture-pane-contents 'on'
set -g @resurrect-pane-contents-area 'full'
set -g @resurrect-processes 'ssh python3 node vim nvim'
EOF

# 4. Register the LaunchAgent so it starts at login
#    Edit the plist first — replace YOUR_USERNAME with your actual username:
sed -i '' "s/YOUR_USERNAME/$(whoami)/g" com.user.menubar-terminal.plist
cp com.user.menubar-terminal.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.user.menubar-terminal.plist
```

### Run manually (without LaunchAgent)

```bash
python3 ~/menubar-terminal/menubar_terminal.py &
```

## How persistence works

| Event | What happens |
|---|---|
| App crash / kill | tmux server stays up; sessions reattach normally |
| Every 5 minutes | `tmux-resurrect` saves session layout + scrollback to `~/.tmux/resurrect/last` |
| After a session kill | Immediate save |
| System reboot | On next launch, resurrect restores all sessions in their last directories; sessions panel opens automatically so you can reattach |

Session data is stored in `~/.tmux/resurrect/`. The simple JSON fallback (`~/.menubar_terminal/saved_sessions.json`) is used if tmux-resurrect is not installed.

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
