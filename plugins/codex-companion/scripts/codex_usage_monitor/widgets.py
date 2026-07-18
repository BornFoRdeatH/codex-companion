from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


PLACEMENTS = {"right_dock", "left_dock", "bottom_dock", "floating", "message_footer", "modal"}
CONTENT_TYPES = {"markdown", "html", "javascript"}


class WidgetError(ValueError):
    pass


def load_widgets(directories: list[str], scripts_enabled: bool = True) -> list[dict[str, Any]]:
    widgets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for directory in directories:
        root = Path(directory).resolve()
        if not root.is_dir():
            continue
        for manifest_path in root.glob("*/manifest.json"):
            try:
                widget = validate_manifest(manifest_path, scripts_enabled)
            except (OSError, json.JSONDecodeError, WidgetError):
                continue
            if widget["id"] not in seen:
                seen.add(widget["id"])
                widgets.append(widget)
    return sorted(widgets, key=lambda value: (value["order"], value["id"]))


def validate_manifest(path: Path, scripts_enabled: bool = True) -> dict[str, Any]:
    root = path.parent.resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    required = {"id", "name", "entry", "content_type", "placements", "default_placement"}
    if not isinstance(data, dict) or not required.issubset(data):
        raise WidgetError("Missing widget manifest fields")
    if data.get("schema_version", 1) != 1:
        raise WidgetError("Unsupported widget schema")
    content_type = data["content_type"]
    if content_type not in CONTENT_TYPES or (content_type == "javascript" and not scripts_enabled):
        raise WidgetError("Unsupported widget content type")
    placements = data["placements"]
    if not isinstance(placements, list) or not placements or not set(placements) <= PLACEMENTS:
        raise WidgetError("Invalid widget placements")
    if data["default_placement"] not in placements:
        raise WidgetError("Default placement is not allowed")
    entry = (root / str(data["entry"])).resolve()
    try:
        entry.relative_to(root)
    except ValueError as exc:
        raise WidgetError("Widget entry escapes its directory") from exc
    if not entry.is_file():
        raise WidgetError("Widget entry does not exist")
    if "message_footer" in placements and content_type == "javascript":
        raise WidgetError("Message footer widgets must be declarative")
    result = {
        "schema_version": 1,
        "id": str(data["id"]),
        "name": str(data["name"]),
        "content_type": content_type,
        "placements": placements,
        "default_placement": data["default_placement"],
        "permissions": [value for value in data.get("permissions", []) if value in {"telemetry", "theme", "resize", "settings"}],
        "order": int(data.get("order", 100)),
        "size": data.get("size", {"width": 320, "height": 180}),
        "source": entry.read_text(encoding="utf-8"),
        "base_dir": str(root),
    }
    return result


class _Sanitizer(HTMLParser):
    allowed = {"div", "span", "p", "strong", "em", "code", "pre", "ul", "ol", "li", "br", "small", "section", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self.blocked = 0
        self.in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "iframe", "object", "embed", "form", "svg", "math", "link", "meta"}:
            self.blocked += 1
            return
        if self.blocked or tag not in self.allowed:
            return
        if tag == "style":
            self.in_style = True
        safe_attrs = []
        for name, value in attrs:
            if name in {"class", "title", "aria-label"} and value is not None:
                safe_attrs.append(f' {name}="{html.escape(value, quote=True)}"')
        self.output.append(f"<{tag}{''.join(safe_attrs)}>")

    def handle_endtag(self, tag: str) -> None:
        if self.blocked:
            if tag in {"script", "iframe", "object", "embed", "form", "svg", "math", "link", "meta"}:
                self.blocked -= 1
            return
        if tag in self.allowed and tag != "br":
            self.output.append(f"</{tag}>")
        if tag == "style":
            self.in_style = False

    def handle_data(self, data: str) -> None:
        if not self.blocked:
            self.output.append(_sanitize_css(data) if self.in_style else html.escape(data))


def sanitize_html(value: str) -> str:
    sanitizer = _Sanitizer()
    sanitizer.feed(value)
    return "".join(sanitizer.output)


def _sanitize_css(value: str) -> str:
    value = re.sub(r"@import\b[^;]*;?", "", value, flags=re.IGNORECASE)
    value = re.sub(r"url\s*\([^)]*\)", "", value, flags=re.IGNORECASE)
    value = re.sub(r"(?:expression|behavior|-moz-binding)\s*:[^;}]*(?:;|(?=}))", "", value, flags=re.IGNORECASE)
    return value.replace("</style", "<\\/style")


def markdown_to_html(value: str) -> str:
    # Deliberately small declarative subset: headings, lists, emphasis and code stay text-only.
    lines = []
    for raw in value.splitlines():
        line = html.escape(raw)
        if line.startswith("### "):
            line = f"<strong>{line[4:]}</strong>"
        elif line.startswith("## "):
            line = f"<strong>{line[3:]}</strong>"
        elif line.startswith("# "):
            line = f"<strong>{line[2:]}</strong>"
        elif line.startswith("- "):
            line = f"<span>• {line[2:]}</span>"
        lines.append(line)
    return "<br>".join(lines)
