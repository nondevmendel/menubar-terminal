import json, shlex, subprocess, threading

import objc
from Foundation import (
    NSObject, NSURL, NSMakeSize, NSURLRequest, NSMutableAttributedString,
)
from AppKit import (
    NSApplication, NSStatusBar, NSPopover, NSViewController, NSView,
    NSColor, NSMakeRect, NSPasteboard, NSPasteboardTypeString,
    NSVariableStatusItemLength, NSFont,
    NSForegroundColorAttributeName, NSFontAttributeName,
    NSFilenamesPboardType, NSDragOperationCopy, NSDragOperationNone,
    NSEventModifierFlagCommand, NSMenu, NSMenuItem,
)
from WebKit import WKWebView, WKWebViewConfiguration, WKUserContentController

import server
import tmux
import stats as _stats

NSPopoverBehaviorApplicationDefined = 0
NSRectEdgeMinY = 1

_app_delegate_ref = None


class _ClipboardBridge(NSObject):
    def setWebView_(self, wv):
        self._wv = wv

    def _paste_via_tmux(self, session_name, text):
        try:
            r = subprocess.run(
                [tmux._TMUX_BIN] + tmux._TMUX_FLAGS + ["load-buffer", "-b", "paste-menubar", "-"],
                input=text.encode("utf-8"), capture_output=True, timeout=3
            )
            if r.returncode == 0:
                subprocess.run(
                    [tmux._TMUX_BIN] + tmux._TMUX_FLAGS +
                    ["paste-buffer", "-b", "paste-menubar", "-p", "-t", session_name],
                    capture_output=True, timeout=3
                )
        except Exception as e:
            print(f"[paste] error: {e}", flush=True)

    def _tmux_scroll(self, session_name, direction, lines):
        try:
            tmux._tmux("copy-mode", "-t", session_name)
            key = "scroll-up" if direction == "up" else "scroll-down"
            tmux._tmux("send-keys", "-t", session_name, "-X", "-N", str(lines), key)
        except Exception as e:
            print(f"[scroll] error: {e}", flush=True)

    def userContentController_didReceiveScriptMessage_(self, _ucc, msg):
        name = str(msg.name())
        if name == "paste":
            try:
                data = json.loads(str(msg.body() or ""))
                session_name = data.get("name", "")
                text = data.get("text", "")
            except Exception:
                return
            if text and session_name:
                threading.Thread(
                    target=self._paste_via_tmux, args=(session_name, text), daemon=True
                ).start()
            elif text:
                js = "window._termPaste(" + json.dumps(text) + ")"
                self._wv.evaluateJavaScript_completionHandler_(js, None)
        elif name == "copy":
            text = str(msg.body() or "")
            if text:
                try:
                    subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                                   capture_output=True, timeout=3)
                except Exception as e:
                    print(f"[copy] error: {e}", flush=True)
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
        elif name == "scroll":
            try:
                data = json.loads(str(msg.body() or ""))
                session_name = data.get("name", "")
                direction = data.get("dir", "up")
                lines = max(1, int(data.get("lines", 3)))
            except Exception:
                return
            if session_name:
                threading.Thread(
                    target=self._tmux_scroll, args=(session_name, direction, lines), daemon=True
                ).start()
        elif name == "openUrl":
            url = str(msg.body() or "")
            if url.startswith(("http://", "https://")):
                import subprocess
                subprocess.run(["open", url])
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

    def acceptsFirstMouse_(self, event):
        return True

    def scrollWheel_(self, event):
        # Skip trackpad momentum (inertia after finger lift)
        if event.momentumPhase() != 0:
            return
        dy = event.scrollingDeltaY()
        if abs(dy) < 0.5:
            return
        direction = 'up' if dy > 0 else 'down'
        if event.hasPreciseScrollingDeltas():
            lines = max(1, int(abs(dy) / 20))
        else:
            lines = max(1, int(abs(dy)))
        js = (
            "(function(){"
            "var t=tabs&&tabs.find(function(t){return t.id===act;});"
            "if(t&&t.name){"
            "try{"
            "window.webkit.messageHandlers.scroll.postMessage("
            "JSON.stringify({name:t.name,dir:'" + direction + "',lines:" + str(lines) + "})"
            ");"
            "}catch(ex){}"
            "}"
            "})()"
        )
        self.evaluateJavaScript_completionHandler_(js, None)
        if self.window():
            self.window().makeFirstResponder_(self)

    def _jsFocus(self):
        self.evaluateJavaScript_completionHandler_(
            "(function(){var t=tabs&&tabs.find&&tabs.find(function(t){return t.id===act;});"
            "if(t&&t.term)t.term.focus();})()", None)

    def mouseDown_(self, event):
        if self.window() and not self.window().isKeyWindow():
            self.window().makeKeyWindow()
        objc.super(TerminalWKWebView, self).mouseDown_(event)
        self._jsFocus()

    def otherMouseDown_(self, event):
        # Extra mouse buttons (gaming mice side buttons) fire this, not mouseDown_.
        if self.window() and not self.window().isKeyWindow():
            self.window().makeKeyWindow()
        objc.super(TerminalWKWebView, self).otherMouseDown_(event)
        self._jsFocus()

    def performKeyEquivalent_(self, event):
        if event.modifierFlags() & NSEventModifierFlagCommand:
            key = event.charactersIgnoringModifiers() or ''
            if key == 'v':
                # Let WKWebView handle paste natively — it reads clipboard with
                # user-gesture permission (no Automation dialog). The DOM paste
                # event fires with e.clipboardData; our JS listener intercepts it
                # and routes to Python without any NSPasteboard read.
                return objc.super(TerminalWKWebView, self).performKeyEquivalent_(event)
            if key == 'c':
                self.evaluateJavaScript_completionHandler_(
                    "(function(){var t=tabs&&tabs.find(function(t){return t.id===act;});"
                    "if(t&&t.term&&t.term.hasSelection())"
                    "{window.webkit.messageHandlers.copy.postMessage(t.term.getSelection());}})()",
                    None)
                return True
        return objc.super(TerminalWKWebView, self).performKeyEquivalent_(event)

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
        ucc.addScriptMessageHandler_name_(self._bridge, "status")
        ucc.addScriptMessageHandler_name_(self._bridge, "browse")
        ucc.addScriptMessageHandler_name_(self._bridge, "openUrl")
        ucc.addScriptMessageHandler_name_(self._bridge, "scroll")

        cfg = WKWebViewConfiguration.alloc().init()
        cfg.setUserContentController_(ucc)
        try: cfg.preferences().setValue_forKey_(True, "developerExtrasEnabled")
        except: pass

        wv = TerminalWKWebView.alloc().initWithFrame_configuration_(frame, cfg)
        wv.registerForDraggedTypes_([NSFilenamesPboardType])
        wv.setAutoresizingMask_(18)
        url = NSURL.URLWithString_(f"http://127.0.0.1:{server.HTTP_PORT}/")
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
        btn.sendActionOn_(0x02 | 0x08)  # NSLeftMouseUpMask | NSRightMouseUpMask

    def toggle_(self, sender):
        event = NSApplication.sharedApplication().currentEvent()
        # Right-click → show context menu
        if event and event.type() == 3:  # NSEventTypeRightMouseUp
            self._show_context_menu()
            return
        if self._popover and self._popover.isShown():
            self._popover.close()
            _stats.popover_closed()
        else:
            self._open()

    def _show_context_menu(self):
        menu = NSMenu.alloc().init()
        restart_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Restart", "restartApp:", "")
        restart_item.setTarget_(self)
        menu.addItem_(restart_item)
        self._item.popUpStatusItemMenu_(menu)

    def restartApp_(self, _sender):
        import subprocess, sys, os
        tmux._save_sessions()
        subprocess.Popen([sys.executable] + sys.argv)
        os._exit(0)

    def _open(self):
        if not self._popover:
            self._build_popover()
        btn = self._item.button()
        self._popover.showRelativeToRect_ofView_preferredEdge_(
            btn.bounds(), btn, NSRectEdgeMinY)
        # Allow popover to appear on all spaces, including full-screen spaces
        win = self._vc.view().window()
        if win:
            win.setCollectionBehavior_(
                (1 << 0) |  # NSWindowCollectionBehaviorCanJoinAllSpaces
                (1 << 8)    # NSWindowCollectionBehaviorFullScreenAuxiliary
            )
        _stats.popover_opened()
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
