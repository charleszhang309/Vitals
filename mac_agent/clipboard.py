"""One-shot commands for the Mac pasteboard and Vitals scratchpad."""

import argparse
from pathlib import Path
import subprocess
import sys

import httpx

if __package__ in ("", None):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mac_agent.api import CONFIG_PATH, Config
from mac_agent.macos import apple, native


def pasteboard_content(pasteboard=None):
    from AppKit import NSBitmapImageFileTypePNG, NSBitmapImageRep, NSPasteboard
    from AppKit import NSPasteboardTypePNG, NSPasteboardTypeString, NSPasteboardTypeTIFF

    if pasteboard is None:
        pasteboard = NSPasteboard.generalPasteboard()
    image = pasteboard.dataForType_(NSPasteboardTypePNG)
    if image is not None:
        return "image", bytes(image)
    image_type = pasteboard.availableTypeFromArray_([
        NSPasteboardTypeTIFF, "public.jpeg", "public.heic", "public.heif",
        "com.compuserve.gif", "public.webp", "com.microsoft.bmp",
    ])
    if image_type is not None:
        image = pasteboard.dataForType_(image_type)
        representation = NSBitmapImageRep.imageRepWithData_(image) if image is not None else None
        png = representation.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}) if representation else None
        if png is None:
            raise ValueError("剪贴板图片无法转换为 PNG")
        return "image", bytes(png)
    text = pasteboard.stringForType_(NSPasteboardTypeString)
    if not text or not text.strip():
        raise ValueError("剪贴板中没有可发送的图片或文字")
    return "text", text


def latest_text(items: list) -> str:
    entries = [item for item in items if item["kind"] == "text" and isinstance(item.get("text"), str)]
    if not entries:
        raise ValueError("中转站暂无文字")
    return max(entries, key=lambda item: (item["created_at"], item["id"]))["text"]


def push(client: httpx.Client):
    kind, payload = pasteboard_content()
    if kind == "image":
        response = client.post("/api/scratchpad/file", files={"file": ("clipboard.png", payload, "image/png")})
    else:
        response = client.post("/api/scratchpad/text", json={"text": payload})
    response.raise_for_status()


def pull(client: httpx.Client):
    response = client.get("/api/scratchpad")
    response.raise_for_status()
    native("/usr/bin/pbcopy", input_text=latest_text(response.json()))


def notify(action: str):
    message = "已发送到 Vitals 中转站" if action == "push" else "已复制中转站最新文字"
    try:
        apple(f'display notification "{message}" with title "Vitals"')
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(f"剪贴板操作已成功，通知发送失败：{error}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("action", choices=["push", "pull"])
    args = parser.parse_args()
    try:
        config = Config.load(args.config, require_token=False)
        with config.client(timeout=30) as client:
            (push if args.action == "push" else pull)(client)
    except (httpx.HTTPError, OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(f"Vitals 剪贴板：{error}", file=sys.stderr)
        return 1
    notify(args.action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
