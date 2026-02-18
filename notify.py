"""Claude Code voice notification hook.

Speaks aloud when Claude finishes a task or needs user input.
Uses edge-tts (Microsoft neural voices) for TTS and Windows MCI for playback.
"""

import asyncio
import json
import os
import sys
import tempfile
from ctypes import create_unicode_buffer, windll, wintypes
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
MAX_MESSAGE_LENGTH = 200

# --- MCI audio playback (Windows) ---

windll.winmm.mciSendStringW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.UINT, wintypes.HANDLE
]
windll.winmm.mciSendStringW.restype = wintypes.DWORD


def _mci(command: str) -> str:
    buf = create_unicode_buffer(600)
    err = windll.winmm.mciSendStringW(command, buf, 599, 0)
    if err:
        err_buf = create_unicode_buffer(600)
        windll.winmm.mciGetErrorStringW(err, err_buf, 599)
        raise RuntimeError(f"MCI error {err}: {err_buf.value}")
    return buf.value


def play_mp3(path: str) -> None:
    _mci("Close All")
    _mci(f'Open "{path}" Type MPEGVideo Alias voice_notif')
    _mci("Play voice_notif Wait")
    _mci("Close voice_notif")


# --- Config ---

DEFAULT_CONFIG = {
    "enabled": True,
    "voice": "en-US-GuyNeural",
    "rate": "+0%",
    "volume": "+0%",
    "pitch": "+0Hz",
    "messages": {
        "prompt_submit": "On it.",
        "stop": "Done. {summary}",
        "notification_permission_prompt": "Need your permission. {message}",
        "notification_idle_prompt": "Waiting for your input.",
        "notification_default": "{message}",
    },
}


def load_config() -> dict:
    config_path = SCRIPT_DIR / "config.json"
    try:
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Config error, using defaults: {e}", file=sys.stderr)
        return DEFAULT_CONFIG


# --- Message resolution ---


def _truncate(text: str, max_len: int = MAX_MESSAGE_LENGTH) -> str:
    if len(text) <= max_len:
        return text
    # Cut at last sentence boundary within limit, or just truncate
    cut = text[:max_len]
    last_period = cut.rfind(".")
    if last_period > max_len // 2:
        return cut[: last_period + 1]
    return cut.rstrip() + "."


def _extract_summary(transcript_path: str) -> str:
    """Read the last assistant text message from the transcript JSONL."""
    try:
        last_text = ""
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                # Look for assistant messages with text content
                if entry.get("type") == "assistant":
                    message = entry.get("message", {})
                    for block in message.get("content", []):
                        if block.get("type") == "text":
                            last_text = block["text"]
        if not last_text:
            return ""
        # Get the last non-empty line — that's where the ask/conclusion lives
        lines = [l.strip() for l in last_text.strip().splitlines() if l.strip()]
        snippet = lines[-1] if lines else last_text.strip()
        # Trim to first sentence if still long
        period = snippet.find(".")
        if 0 < period < 150:
            return snippet[: period + 1]
        return snippet[:150]
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return ""


def resolve_message(event: dict, config: dict) -> str | None:
    hook_event = event.get("hook_event_name", "")
    messages = config.get("messages", DEFAULT_CONFIG["messages"])

    if hook_event == "UserPromptSubmit":
        return messages.get("prompt_submit", "On it.")

    if hook_event == "Stop":
        if event.get("stop_hook_active", False):
            return None
        template = messages.get("stop", "Done. {summary}")
        summary = event.get("transcript_summary", "")
        if not summary:
            transcript_path = event.get("transcript_path", "")
            if transcript_path:
                summary = _extract_summary(transcript_path)
        if not summary:
            text = template.replace("{summary}", "").strip()
        else:
            text = template.replace("{summary}", summary)
        return _truncate(text)

    if hook_event == "Notification":
        notif_type = event.get("notification_type", "")
        key = f"notification_{notif_type}"
        template = messages.get(key, messages.get("notification_default", "{message}"))
        raw_message = event.get("message", "Notification")
        text = template.replace("{message}", raw_message)
        return _truncate(text)

    return None


# --- TTS ---


async def _generate_and_play(text: str, config: dict) -> None:
    import edge_tts

    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        comm = edge_tts.Communicate(
            text,
            voice=config.get("voice", "en-US-GuyNeural"),
            rate=config.get("rate", "+0%"),
            volume=config.get("volume", "+0%"),
            pitch=config.get("pitch", "+0Hz"),
        )
        await comm.save(tmp_path)
        play_mp3(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def speak(text: str, config: dict) -> None:
    asyncio.run(_generate_and_play(text, config))


# --- Entry point ---


def _debug_log(event: dict) -> None:
    log_path = SCRIPT_DIR / "debug.log"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, indent=2, default=str) + "\n---\n")
    except OSError:
        pass


def main() -> None:
    raw = sys.stdin.read()
    event = json.loads(raw) if raw.strip() else {}

    config = load_config()

    if config.get("debug", False):
        _debug_log(event)

    if not config.get("enabled", True):
        return

    message = resolve_message(event, config)
    if message:
        speak(message, config)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"claudevoice error: {e}", file=sys.stderr)
        sys.exit(0)
