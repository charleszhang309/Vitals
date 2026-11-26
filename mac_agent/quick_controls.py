"""Run short desktop scripts in-process on the agent's main thread."""

from .core_audio import AudioPropertyError, CoreAudio, fourcc

PLAYERS = (("com.apple.Music", "Apple Music", "music"), ("com.spotify.client", "Spotify", "spotify"))
MEDIA_EVENTS = {"media_play_pause": "PlPs", "media_next": "Next", "media_previous": "Prev"}
WAIT_WITHOUT_INTERACTION = 0x03 | 0x10


class ScriptError(RuntimeError):
    def __init__(self, error):
        super().__init__(str(error.get("NSAppleScriptErrorMessage", "AppleScript 执行失败")))
        self.number = error.get("NSAppleScriptErrorNumber")


class QuickControls:
    def __init__(self):
        from AppKit import NSRunningApplication
        from Foundation import NSAppleEventDescriptor, NSAppleScript
        from objc import autorelease_pool

        self.running = NSRunningApplication.runningApplicationsWithBundleIdentifier_
        self.script = NSAppleScript
        self.descriptor = NSAppleEventDescriptor
        self.audio = CoreAudio()
        self.pool = autorelease_pool

    def run(self, source):
        with self.pool():
            script = self.script.alloc().initWithSource_(source)
            result, error = script.executeAndReturnError_(None)
            if result is None:
                raise ScriptError(error or {})
            return result

    def volume(self, *, percent=None, muted=None):
        try:
            return self.audio.volume(percent=percent, muted=muted)
        except AudioPropertyError:
            setting = ""
            if percent is not None:
                setting = f"set volume output volume {percent}"
            if muted is not None:
                setting = "set volume with output muted" if muted else "set volume without output muted"
            return self.apple_volume(setting)

    def apple_volume(self, setting=""):
        result = self.run(setting + """
set settings to get volume settings
return {output volume of settings, output muted of settings}
""")
        level = result.descriptorAtIndex_(1)
        mute = result.descriptorAtIndex_(2)
        missing = int.from_bytes(b"msng", "big")
        return {"percent": level.int32Value() if level.descriptorType() != missing else None,
                "muted": bool(mute.booleanValue()) if mute.descriptorType() != missing else None}

    def player_state(self, bundle_id):
        return self.run(f'tell application id "{bundle_id}" to return player state as text').stringValue()

    def media(self, action):
        players = [player for player in PLAYERS if self.running(player[0])]
        if not players:
            raise RuntimeError("Music / Spotify is not available")
        if len(players) == 1:
            target = players[0]
        else:
            states = [(player, self.player_state(player[0])) for player in players]
            target = max(states, key=lambda entry: {"playing": 2, "paused": 1}.get(entry[1], 0))[0]
        bundle_id, player, source = target
        running = self.running(bundle_id)
        if not running:
            raise RuntimeError("Music / Spotify is not available")
        self.send_media_event(running[0].processIdentifier(), source, MEDIA_EVENTS[action])

    def send_media_event(self, pid, source, event):
        with self.pool():
            target = self.descriptor.descriptorWithProcessIdentifier_(pid)
            event_class = fourcc("hook" if source == "music" else "spfy")
            message = self.descriptor.appleEventWithEventClass_eventID_targetDescriptor_returnID_transactionID_(
                event_class, fourcc(event), target, -1, 0)
            reply, error = message.sendEventWithOptions_timeout_error_(WAIT_WITHOUT_INTERACTION, 2.0, None)
            code = reply.descriptorForKeyword_(fourcc("errn")) if reply is not None else None
            if error is not None:
                raise ScriptError({"NSAppleScriptErrorNumber": error.code(),
                                   "NSAppleScriptErrorMessage": error.localizedDescription()})
            if code is not None and code.int32Value():
                raise ScriptError({"NSAppleScriptErrorNumber": code.int32Value()})
