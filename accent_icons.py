#!/usr/bin/env python3
"""
plasma-accent-icons
===================

Build a KDE icon theme (default: Plasma-Accent) from the active YAMIS theme
where every icon follows the current palette.

KDE's icon loader replaces the CSS classes in an SVG when the theme has
``FollowsColorScheme=true``.  Classes such as ``ColorScheme-Text`` and
``ColorScheme-Accent`` are resolved from the current KColorScheme, so the
icons change automatically whenever the palette changes.

Two color modes are available:

* ``text`` (default) uses ``ColorScheme-Text``, the light window text color
  used by panel labels such as the clock and system monitors.
* ``accent`` uses ``ColorScheme-Accent``, the darker accent/selection color.

* YAMIS icons are copied and their ``ColorScheme-*`` classes are rewritten to
  the selected class.
* Icon names missing from YAMIS are imported from plasma-monochrome-icons,
  Breeze, Adwaita, hicolor, Flatpak and Snap.
* Monochrome SVGs are patched in place.
* Colored SVGs and raster images become a small SVG mask that keeps the
  original luminance detail but is filled with ``currentColor`` and the
  selected palette class.
* A manifest inside the theme lets ``sync`` add icons for newly installed
  applications without rebuilding everything, and records the color mode.

The tool only reads the source themes.  It never modifies YAMIS, Breeze,
Adwaita or hicolor.  Run ``install`` to build and install the theme, and
``activate`` when you want to switch to it.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, OrderedDict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import numpy as np
    from PIL import Image
except Exception as exc:  # handled with a friendly message when masks are needed
    np = None
    Image = None
    _IMAGE_IMPORT_ERROR = exc
else:
    _IMAGE_IMPORT_ERROR = None


APP_NAME = "plasma-accent-icons"
VERSION = "1.0.0"
THEME_DIR_NAME = "Plasma-Accent"
THEME_DISPLAY_NAME = "Plasma Accent"
DEFAULT_BASE_THEME = "YAMIS"

# KDE's icon loader exposes a handful of palette classes.  "text" uses
# QPalette::WindowText, the same light tone used by panel labels such as the
# clock and KVitals in a Material You dark scheme.  "accent" uses the palette
# accent, which Plasma derives from the (usually darker) selection color.
COLOR_MODES = {
    "text": ("ColorScheme-Text", "#d8dee9"),
    "accent": ("ColorScheme-Accent", "#3daee9"),
}
DEFAULT_COLOR_MODE = "text"

MASK_SIZE = 256
MASK_VERSION = 4
MANIFEST_NAME = ".plasma-accent-manifest.json"
INDEX_FILE = "index.theme"


@dataclass(frozen=True)
class ColorStyle:
    mode: str
    css_class: str
    fallback: str


def resolve_color_style(mode: Optional[str]) -> ColorStyle:
    mode = (mode or DEFAULT_COLOR_MODE).lower()
    if mode not in COLOR_MODES:
        die(f"unknown color mode {mode!r}; choose one of: {', '.join(COLOR_MODES)}")
    css_class, fallback = COLOR_MODES[mode]
    return ColorStyle(mode, css_class, fallback)


DEFAULT_COLOR_STYLE = ColorStyle(
    DEFAULT_COLOR_MODE, COLOR_MODES[DEFAULT_COLOR_MODE][0], COLOR_MODES[DEFAULT_COLOR_MODE][1]
)

HOME = Path.home()
XDG_DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or (HOME / ".local/share"))
XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (HOME / ".config"))
XDG_CACHE_HOME = Path(os.environ.get("XDG_CACHE_HOME") or (HOME / ".cache"))
DEFAULT_INSTALL_DIR = XDG_DATA_HOME / "icons" / THEME_DIR_NAME
DEFAULT_BUILD_DIR = XDG_CACHE_HOME / APP_NAME / "build" / THEME_DIR_NAME
CACHE_DIR = XDG_CACHE_HOME / APP_NAME
LOCAL_BIN = HOME / ".local/bin" / APP_NAME
RESOURCE_DIR = XDG_DATA_HOME / APP_NAME
SYSTEMD_USER_DIR = XDG_CONFIG_HOME / "systemd/user"

SUPPORTED_EXT = {".svg", ".svgz", ".png", ".xpm"}
FMT_RANK = {".svg": 0, ".svgz": 1, ".png": 2, ".xpm": 3}

KNOWN_CONTEXTS = {
    "actions", "animations", "applets", "apps", "applications", "categories",
    "devices", "emblems", "emotes", "filesystems", "international", "mimetypes",
    "places", "preferences", "status", "stock",
}

CONTEXT_OUTPUT = {
    "actions": ("actions/scalable", "Actions"),
    "animations": ("animations/scalable", "Animations"),
    "applets": ("applets/scalable", "Status"),
    "apps": ("apps/scalable", "Applications"),
    "applications": ("apps/scalable", "Applications"),
    "categories": ("categories/scalable", "Categories"),
    "devices": ("devices/scalable", "Devices"),
    "emblems": ("emblems/scalable", "Emblems"),
    "emotes": ("emotes/scalable", "Emotes"),
    "filesystems": ("filesystems/scalable", "FileSystems"),
    "international": ("international/scalable", "International"),
    "mimetypes": ("mimetypes/scalable", "MimeTypes"),
    "places": ("places/scalable", "Places"),
    "preferences": ("preferences/scalable", "Applications"),
    "status": ("status/scalable", "Status"),
    "stock": ("stock/scalable", "Stock"),
}

DESKTOP_ROOTS = [
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    XDG_DATA_HOME / "applications",
    Path("/var/lib/flatpak/exports/share/applications"),
    HOME / ".local/share/flatpak/exports/share/applications",
    Path("/var/lib/snapd/desktop/applications"),
]

QUIET = False
VERBOSE = False


def log(message: str, level: str = "info") -> None:
    if QUIET and level not in ("warning", "error"):
        return
    if level == "debug" and not VERBOSE:
        return
    prefix = {"warning": "warning: ", "error": "error: ", "debug": "debug: "}.get(level, "")
    print(prefix + message, flush=True)


def die(message: str, code: int = 1) -> "NoReturn":
    log(message, "error")
    raise SystemExit(code)


def _xdg_roots() -> list[Path]:
    roots = [
        XDG_DATA_HOME / "icons",
        HOME / ".icons",
    ]
    for entry in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":"):
        if entry.strip():
            roots.append(Path(entry) / "icons")
    roots.extend(
        [
            Path("/var/lib/flatpak/exports/share/icons"),
            HOME / ".local/share/flatpak/exports/share/icons",
            Path("/var/lib/snapd/desktop/icons"),
        ]
    )
    seen: set[str] = set()
    result: list[Path] = []
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:
            key = str(root)
        if key not in seen and root.is_dir():
            seen.add(key)
            result.append(root)
    return result


def xdg_icon_roots() -> list[Path]:
    global _XDG_ROOTS_CACHE
    if "_XDG_ROOTS_CACHE" not in globals():
        _XDG_ROOTS_CACHE = _xdg_roots()
    return _XDG_ROOTS_CACHE


def find_theme_dir(name: str) -> Optional[Path]:
    for root in xdg_icon_roots():
        candidate = root / name
        if (candidate / INDEX_FILE).is_file():
            return candidate
    return None


def find_theme_dirs(name: str) -> list[Path]:
    return [root / name for root in xdg_icon_roots() if (root / name).is_dir()]


def parse_theme_index(path: Path) -> OrderedDict[str, OrderedDict[str, str]]:
    sections: OrderedDict[str, OrderedDict[str, str]] = OrderedDict()
    current: Optional[str] = None
    text = path.read_text(encoding="utf-8", errors="replace")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            sections.setdefault(current, OrderedDict())
        elif "=" in line and current is not None:
            key, value = line.split("=", 1)
            sections[current][key.strip()] = value.strip()
    return sections


def index_directories(index: OrderedDict[str, OrderedDict[str, str]]) -> list[str]:
    raw = index.get("Icon Theme", {}).get("Directories", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def icon_name(filename: str) -> Optional[str]:
    path = Path(filename)
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXT:
        return None
    name = path.name[: -len(ext)]
    if not name or name.startswith(".") or "@" in name:
        return None
    return name


def parse_context_size(rel: Path) -> tuple[str, int]:
    parts = [part.lower() for part in rel.parts[:-1]]
    context: Optional[str] = None
    index: Optional[int] = None
    for i, part in enumerate(parts):
        if part in KNOWN_CONTEXTS:
            context = part
            index = i
            break
    if context is None:
        if "legacy" in parts:
            context = "apps"
        elif "symbolic" in parts or "ui" in parts:
            context = "actions"
        else:
            context = "apps"
        size = MASK_SIZE if any(p in ("scalable", "symbolic") for p in parts) else 0
        return context, size
    size = 0
    if index is not None and index + 1 < len(parts):
        nxt = parts[index + 1]
        match = re.search(r"\d+", nxt)
        if match:
            size = int(match.group(0))
        elif nxt in ("scalable", "symbolic"):
            size = MASK_SIZE
    elif any(p in ("scalable", "symbolic") for p in parts):
        size = MASK_SIZE
    return context, size


def _iter_theme_files(theme_path: Path):
    skip_dirs = {"cursors", "cursors_scalable", ".git"}
    for dirpath, dirnames, filenames in os.walk(theme_path):
        dirnames[:] = sorted(
            d for d in dirnames if d not in skip_dirs and not d.startswith(".")
        )
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            path = Path(dirpath) / filename
            if path.suffix.lower() in SUPPORTED_EXT:
                yield path


@dataclass(frozen=True)
class Candidate:
    name: str
    path: Path
    theme: str
    priority: int
    context: str
    size: int
    fmt: str

    @property
    def score(self) -> tuple:
        return (self.priority, FMT_RANK.get(self.fmt, 9), -self.size, len(str(self.path)))


@dataclass(frozen=True)
class SourceSpec:
    label: str
    theme: str
    path: Path
    priority: int


def source_specs() -> list[SourceSpec]:
    specs: list[SourceSpec] = []
    roots = xdg_icon_roots()

    for root in roots:
        path = root / "plasma-monochrome-icons"
        if (path / INDEX_FILE).is_file():
            specs.append(SourceSpec("plasma-monochrome-icons", "plasma-monochrome-icons", path, 10))
            break

    breeze_found = False
    for root in roots:
        path = root / "breeze"
        if (path / INDEX_FILE).is_file():
            specs.append(SourceSpec("breeze", "breeze", path, 20))
            breeze_found = True
            break
    if not breeze_found:
        for root in roots:
            path = root / "breeze-dark"
            if (path / INDEX_FILE).is_file():
                specs.append(SourceSpec("breeze-dark", "breeze-dark", path, 22))
                break

    for root in roots:
        path = root / "Adwaita"
        if (path / INDEX_FILE).is_file():
            specs.append(SourceSpec("Adwaita", "Adwaita", path, 30))
            break

    for i, root in enumerate(roots):
        path = root / "hicolor"
        if path.is_dir():
            specs.append(SourceSpec(f"hicolor:{root}", "hicolor", path, 40 + i))

    return specs


def collect_catalog(specs: list[SourceSpec]) -> dict[str, list[Candidate]]:
    catalog: dict[str, list[Candidate]] = {}
    for spec in specs:
        for path in _iter_theme_files(spec.path):
            name = icon_name(path.name)
            if name is None:
                continue
            try:
                rel = path.relative_to(spec.path)
            except ValueError:
                continue
            context, size = parse_context_size(rel)
            fmt = path.suffix.lower()
            candidate = Candidate(name, path, spec.theme, spec.priority, context, size, fmt)
            catalog.setdefault(name, []).append(candidate)
    for name in catalog:
        catalog[name].sort(key=lambda c: c.score)
    return catalog


# ---------------------------------------------------------------------------
# SVG analysis and patching
# ---------------------------------------------------------------------------

STYLE_SCHEME_RE = re.compile(
    r"<style\b[^>]*\bid\s*=\s*[\"']current-color-scheme[\"'][^>]*>(.*?)</style>",
    re.I | re.S,
)
SVG_OPEN_RE = re.compile(r"<svg\b[^>]*>", re.I | re.S)
SCHEME_RE = re.compile(r"ColorScheme-[A-Za-z0-9_-]+")
PAINT_RE = re.compile(r"\b(fill|stroke)\s*[:=]\s*[\"']?\s*([^;\"'\s>]+)", re.I)
STOP_RE = re.compile(r"\bstop-color\s*[:=]\s*[\"']?\s*([^;\"'\s>]+)", re.I)
IGNORE_PAINTS = {
    "none", "currentcolor", "transparent", "inherit", "initial", "unset",
    "context-fill", "context-stroke",
}


def decode_svg(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix.lower() == ".svgz":
        data = gzip.decompress(data)
    return data.decode("utf-8", errors="replace")


def _is_color_value(value: str) -> bool:
    v = value.strip().lower()
    if not v or v in IGNORE_PAINTS or v.startswith("url("):
        return False
    if v.startswith("#"):
        return not (len(v) == 9 and v.endswith("00"))
    if v.startswith(("rgb(", "rgba(", "hsl(", "hsla(")):
        return True
    return bool(re.fullmatch(r"[a-z]+", v))


def has_fixed_colors(text: str) -> bool:
    t = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    t = re.sub(r"<metadata\b.*?</metadata>", "", t, flags=re.S | re.I)
    t = re.sub(
        r"<sodipodi:namedview\b.*?(?:/>|</sodipodi:namedview>)",
        "",
        t,
        flags=re.S | re.I,
    )
    t = STYLE_SCHEME_RE.sub("", t)
    colors: set[str] = set()
    gradient_re = re.compile(
        r"<(?:linear|radial)Gradient\b.*?</(?:linear|radial)Gradient>", re.S | re.I
    )
    for match in gradient_re.finditer(t):
        for stop in STOP_RE.finditer(match.group(0)):
            if _is_color_value(stop.group(1)):
                colors.add(stop.group(1).lower())
    t = gradient_re.sub("", t)
    for match in PAINT_RE.finditer(t):
        if _is_color_value(match.group(2)):
            colors.add(match.group(2).lower())
    return bool(colors)


def analyze_svg(text: str) -> str:
    has_scheme = bool(SCHEME_RE.search(text))
    has_current = "currentcolor" in text.lower()
    fixed = has_fixed_colors(text)
    if has_scheme and has_current:
        return "patch"
    if has_scheme and not has_current:
        return "mask" if fixed else "patch"
    if not has_scheme and has_current:
        return "mask" if fixed else "patch"
    if not has_scheme and not has_current:
        # No explicit paint at all: SVG defaults to black, so it is a
        # perfectly good monochrome icon once a currentColor fill is added.
        return "mask" if fixed else "patch"
    return "mask"


def _remove_inline_color(text: str) -> str:
    color_re = re.compile(r"(?<![-A-Za-z])color\s*:\s*[^;\"']+;?", re.I)

    def repl_double(match: re.Match) -> str:
        style = color_re.sub("", match.group(1))
        return f'style="{style}"'

    def repl_single(match: re.Match) -> str:
        style = color_re.sub("", match.group(1))
        return f"style='{style}'"

    text = re.sub(r'style\s*=\s*"([^"]*)"', repl_double, text, flags=re.I | re.S)
    text = re.sub(r"style\s*=\s*'([^']*)'", repl_single, text, flags=re.I | re.S)
    text = re.sub(r'\s+color\s*=\s*["\'][^"\']*["\']', "", text, flags=re.I)
    return text


def _ensure_scheme_style(text: str, style: ColorStyle = DEFAULT_COLOR_STYLE) -> str:
    rule = f".{style.css_class} {{ color:{style.fallback}; }}"
    matches = list(STYLE_SCHEME_RE.finditer(text))
    if matches:
        first = matches[0]
        for extra in reversed(matches[1:]):
            text = text[: extra.start()] + text[extra.end() :]
        content = first.group(1)
        content = re.sub(r"\.ColorScheme-[A-Za-z0-9_-]+\s*\{[^}]*\}", "", content, flags=re.S)
        content = content.strip()
        if content:
            content += "\n"
        content += rule + "\n"
        return text[: first.start(1)] + content + text[first.end(1) :]
    style_tag = f'<style id="current-color-scheme" type="text/css">{rule}</style>'
    if "</defs>" in text:
        return text.replace("</defs>", f"  {style_tag}\n</defs>", 1)
    open_match = SVG_OPEN_RE.search(text)
    if open_match:
        return text[: open_match.end()] + f"\n<defs>{style_tag}</defs>\n" + text[open_match.end() :]
    return text


SHAPE_TAGS = {
    "path", "rect", "circle", "ellipse", "polygon", "polyline", "line", "use",
    "text", "tspan", "textPath",
}
CONTAINER_TAGS = {
    "svg", "g", "a", "switch", "symbol", "defs", "marker", "mask", "clipPath",
    "pattern", "linearGradient", "radialGradient", "filter",
}
# Shapes below these elements are definitions, not directly painted content.
NON_RENDERED_CONTAINERS = {
    "defs", "marker", "mask", "clipPath", "pattern", "linearGradient",
    "radialGradient", "filter",
}
TAG_TOKEN_RE = re.compile(
    r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<style\b.*?</style>|<[^>]+>", re.S | re.I
)
TAG_NAME_RE = re.compile(r"<\s*/?\s*([A-Za-z_][\w:.-]*)")
ATTR_FILL_RE = re.compile(r"\bfill\s*=\s*([\"'])(.*?)\1", re.I | re.S)
ATTR_STYLE_RE = re.compile(r"\bstyle\s*=\s*([\"'])(.*?)\1", re.I | re.S)
STYLE_FILL_RE = re.compile(r"(?<![-A-Za-z])fill\s*:\s*([^;\"]+)", re.I)
STYLE_STROKE_RE = re.compile(r"(?<![-A-Za-z])stroke\s*:\s*([^;\"]+)", re.I)


PAINT_ATTR_RE = re.compile(
    r"\b(fill|stroke)\s*=\s*([\"'])([^\"']*;[^\"']*)\2", re.I | re.S
)


def _normalize_paint_attrs(tag: str) -> str:
    """Split malformed paint attributes such as fill="currentColor;fill-opacity:1"."""
    matches = list(PAINT_ATTR_RE.finditer(tag))
    if not matches:
        return tag
    extra: list[str] = []
    for match in reversed(matches):
        parts = [part.strip() for part in match.group(3).split(";") if part.strip()]
        if not parts:
            replacement = ""
        else:
            replacement = f'{match.group(1)}="{parts[0]}"'
            extra.extend(parts[1:])
        tag = tag[: match.start()] + replacement + tag[match.end() :]
    if extra:
        style_match = ATTR_STYLE_RE.search(tag)
        extra_style = ";".join(extra)
        if style_match:
            quote = style_match.group(1)
            old = style_match.group(2).strip()
            new = (old + ";" if old and not old.endswith(";") else old) + extra_style
            tag = (
                tag[: style_match.start()]
                + f"style={quote}{new}{quote}"
                + tag[style_match.end() :]
            )
        else:
            tag = _insert_attr(tag, f'style="{extra_style}"')
    return tag


def _tag_fill(tag: str) -> tuple[Optional[str], Optional[str]]:
    """Return (fill attribute, fill style) if present."""
    attr_match = ATTR_FILL_RE.search(tag)
    style_match = ATTR_STYLE_RE.search(tag)
    fill_style = None
    if style_match:
        inner = STYLE_FILL_RE.search(style_match.group(2))
        if inner:
            fill_style = inner.group(1).strip()
    return (attr_match.group(2).strip() if attr_match else None, fill_style)


def _tag_uses_current_color(tag: str) -> bool:
    lower = tag.lower()
    if "currentcolor" in lower:
        return True
    style_match = ATTR_STYLE_RE.search(tag)
    if not style_match:
        return False
    style = style_match.group(2).lower()
    return any(
        pattern.search(style)
        for pattern in (STYLE_FILL_RE, STYLE_STROKE_RE)
    ) and "currentcolor" in style


def _add_class_to_tag(tag: str, cls: str) -> str:
    class_match = re.search(r"\bclass\s*=\s*([\"'])(.*?)\1", tag, re.I | re.S)
    if class_match:
        classes = class_match.group(2).split()
        if cls in classes:
            return tag
        classes.append(cls)
        quote = class_match.group(1)
        new_class = f"class={quote}{' '.join(classes)}{quote}"
        return tag[: class_match.start()] + new_class + tag[class_match.end() :]
    return _insert_attr(tag, f'class="{cls}"')


def _inject_shape_accent(text: str, style: ColorStyle = DEFAULT_COLOR_STYLE) -> str:
    """Put the selected color class and a currentColor fill on every shape.

    QtSvg resolves currentColor against the element that carries it and does
    not always inherit the CSS color property through groups.  Adding the
    class (and the fill) directly to each shape makes monochrome icons follow
    the palette even when the original SVG had no fill attributes at all.
    """
    pieces: list[str] = []
    last = 0
    stack: list[tuple[str, bool]] = []
    for match in TAG_TOKEN_RE.finditer(text):
        token = match.group(0)
        pieces.append(text[last : match.start()])
        last = match.end()
        if token.startswith("<!--") or token.lower().startswith("<![cdata["):
            pieces.append(token)
            continue
        if token.lower().startswith("<style"):
            pieces.append(token)
            continue
        if token.startswith("</"):
            name_match = TAG_NAME_RE.match(token)
            name = name_match.group(1) if name_match else ""
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == name:
                    del stack[index:]
                    break
            pieces.append(token)
            continue
        name_match = TAG_NAME_RE.match(token)
        name = (name_match.group(1) if name_match else "").lower()
        self_closing = token.rstrip().endswith("/>")
        token = _normalize_paint_attrs(token)
        inherited_none = any(flag for _, flag in stack)
        hidden = inherited_none or any(tag in NON_RENDERED_CONTAINERS for tag, _ in stack)

        if name == "stop" and "currentcolor" in token.lower():
            token = _add_class_to_tag(token, style.css_class)

        if name in SHAPE_TAGS and not hidden:
            fill_attr, fill_style = _tag_fill(token)
            fill_value = (fill_attr or fill_style or "").strip().lower()
            needs_fill = not fill_attr and fill_style is None and not inherited_none
            if needs_fill:
                token = _insert_attr(token, 'fill="currentColor"')
            if needs_fill or fill_value == "currentcolor" or _tag_uses_current_color(token):
                token = _add_class_to_tag(token, style.css_class)

        if name in CONTAINER_TAGS and not self_closing:
            own_none = False
            if name in ("g", "svg", "a"):
                fill_attr, fill_style = _tag_fill(token)
                value = (fill_attr or fill_style or "").strip().lower()
                own_none = value == "none"
            stack.append((name, inherited_none or own_none))
        pieces.append(token)
    pieces.append(text[last:])
    return "".join(pieces)


def _add_accent_wrapper(text: str, style: ColorStyle = DEFAULT_COLOR_STYLE) -> str:
    open_match = SVG_OPEN_RE.search(text)
    if not open_match:
        return text
    close_index = text.rfind("</svg>")
    if close_index < 0:
        return text

    root_tag = open_match.group(0)
    fill_match = re.search(r"\bfill\s*=\s*([\"'])(.*?)\1", root_tag, re.I | re.S)
    root_fill = fill_match.group(2).strip().lower() if fill_match else None

    if root_fill is None:
        style_match = re.search(r"\bstyle\s*=\s*([\"'])(.*?)\1", root_tag, re.I | re.S)
        if style_match and re.search(r"(?<![-A-Za-z])fill\s*:\s*none", style_match.group(2), re.I):
            root_fill = "none"
        elif style_match and re.search(r"(?<![-A-Za-z])fill\s*:\s*currentcolor", style_match.group(2), re.I):
            root_fill = "currentcolor"

    if root_fill in (None, "currentcolor"):
        wrapper_fill = ' fill="currentColor"'
    else:
        wrapper_fill = ""

    # A currentColor fill on <svg> is resolved before the class takes effect in
    # some QtSvg versions, leaving the inherited fill black.  Remove it and let
    # the wrapper compute currentColor instead.
    if root_fill == "currentcolor" and fill_match:
        root_tag = root_tag[: fill_match.start()] + root_tag[fill_match.end() :]
        text = text[: open_match.start()] + root_tag + text[open_match.end() :]
        open_match = SVG_OPEN_RE.search(text)
        if not open_match:
            return text
        close_index = text.rfind("</svg>")
        if close_index < 0:
            return text

    wrapper = f'<g class="{style.css_class}"{wrapper_fill}>'
    return (
        text[: open_match.end()]
        + wrapper
        + text[open_match.end() : close_index]
        + "</g>"
        + text[close_index:]
    )


def _add_root_class(text: str, style: ColorStyle = DEFAULT_COLOR_STYLE) -> str:
    open_match = SVG_OPEN_RE.search(text)
    if not open_match:
        return text
    tag = open_match.group(0)
    if style.css_class in tag:
        return text
    class_match = re.search(r"class\s*=\s*([\"'])(.*?)\1", tag, flags=re.S | re.I)
    if class_match:
        quote = class_match.group(1)
        classes = class_match.group(2).strip()
        new_classes = (classes + " " if classes else "") + style.css_class
        new_tag = tag[: class_match.start()] + f"class={quote}{new_classes}{quote}" + tag[class_match.end() :]
    elif tag.rstrip().endswith("/>"):
        new_tag = tag.rstrip()[:-2] + f' class="{style.css_class}"/>'
    else:
        new_tag = tag[:-1] + f' class="{style.css_class}">'
    return text[: open_match.start()] + new_tag + text[open_match.end() :]


def _replace_attr(tag: str, name: str, value: str) -> str:
    pattern = re.compile(r"\s+" + re.escape(name) + r"\s*=\s*([\"']).*?\1", re.I | re.S)
    if pattern.search(tag):
        return pattern.sub(f' {name}="{value}"', tag, count=1)
    return _insert_attr(tag, f'{name}="{value}"')


def _insert_attr(tag: str, attr: str) -> str:
    stripped = tag.rstrip()
    if stripped.endswith("/>"):
        return stripped[:-2] + " " + attr + "/>"
    return stripped[:-1] + " " + attr + ">"


def set_svg_size(text: str, size: int = MASK_SIZE) -> str:
    open_match = SVG_OPEN_RE.search(text)
    if not open_match:
        return text
    tag = open_match.group(0)
    view = re.search(r"viewBox\s*=\s*([\"'])([^\"']+)\1", tag, re.I | re.S)
    width = re.search(r"\bwidth\s*=\s*([\"'])([0-9.]+)(?:px)?\1", tag, re.I)
    height = re.search(r"\bheight\s*=\s*([\"'])([0-9.]+)(?:px)?\1", tag, re.I)
    new_tag = tag
    if view:
        new_tag = _replace_attr(new_tag, "width", str(size))
        new_tag = _replace_attr(new_tag, "height", str(size))
    elif width and height:
        w, h = width.group(2), height.group(2)
        new_tag = _insert_attr(new_tag, f'viewBox="0 0 {w} {h}"')
        new_tag = _replace_attr(new_tag, "width", str(size))
        new_tag = _replace_attr(new_tag, "height", str(size))
    return text[: open_match.start()] + new_tag + text[open_match.end() :]


def patch_svg_text(
    text: str,
    normalize_size: bool = False,
    style: ColorStyle = DEFAULT_COLOR_STYLE,
) -> str:
    text = text.replace("context-fill-opacity", "1")
    text = text.replace("context-stroke-opacity", "1")
    text = SCHEME_RE.sub(style.css_class, text)
    text = text.replace("context-fill", "currentColor")
    text = text.replace("context-stroke", "currentColor")
    text = _remove_inline_color(text)
    text = _ensure_scheme_style(text, style)
    text = _add_root_class(text, style)
    text = _inject_shape_accent(text, style)
    text = _add_accent_wrapper(text, style)
    if normalize_size:
        text = set_svg_size(text, MASK_SIZE)
    return text


# ---------------------------------------------------------------------------
# Mask generation for colored SVGs and raster icons
# ---------------------------------------------------------------------------

def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def ensure_mask_deps() -> None:
    if _IMAGE_IMPORT_ERROR is not None or np is None or Image is None:
        die(
            "mask generation needs numpy and Pillow. Install them with:\n"
            "  python3 -m pip install --user numpy pillow"
        )
    if shutil.which("rsvg-convert") is None and shutil.which("magick") is None:
        die(
            "mask generation needs rsvg-convert or ImageMagick (magick).\n"
            "Install librsvg (rsvg-convert) or imagemagick."
        )


def _render_svg_to_png(src: Path, directory: Path) -> Path:
    input_path = src
    if src.suffix.lower() == ".svgz":
        input_path = directory / "source.svg"
        input_path.write_bytes(gzip.decompress(src.read_bytes()))
    png_path = directory / "render.png"
    errors: list[str] = []

    rsvg = shutil.which("rsvg-convert")
    if rsvg:
        command = [rsvg, "-w", str(MASK_SIZE), str(input_path), "-o", str(png_path)]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=45, check=False
            )
            if result.returncode == 0 and png_path.is_file():
                return png_path
            errors.append((result.stderr or result.stdout or "rsvg-convert failed").strip())
        except Exception as exc:
            errors.append(f"rsvg-convert: {exc}")

    magick = shutil.which("magick")
    if magick:
        command = [
            magick, "-background", "none", str(input_path),
            "-resize", f"{MASK_SIZE}x{MASK_SIZE}", str(png_path),
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=45, check=False
            )
            if result.returncode == 0 and png_path.is_file():
                return png_path
            errors.append((result.stderr or result.stdout or "magick failed").strip())
        except Exception as exc:
            errors.append(f"magick: {exc}")

    raise RuntimeError("; ".join(errors) or "could not render SVG")


def render_source_to_rgba(src: Path) -> "Image.Image":
    if src.suffix.lower() in (".svg", ".svgz"):
        with tempfile.TemporaryDirectory(prefix=f"{APP_NAME}-") as tmp:
            png_path = _render_svg_to_png(src, Path(tmp))
            with Image.open(png_path) as image:
                return image.convert("RGBA")
    with Image.open(src) as image:
        return image.convert("RGBA")


def make_mask_image(image: "Image.Image") -> "Image.Image":
    image = image.convert("RGBA")
    image.thumbnail((MASK_SIZE, MASK_SIZE), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (MASK_SIZE, MASK_SIZE), (0, 0, 0, 0))
    canvas.paste(image, ((MASK_SIZE - image.width) // 2, (MASK_SIZE - image.height) // 2), image)
    array = np.asarray(canvas, dtype=np.float32) / 255.0
    rgb = array[..., :3]
    alpha = array[..., 3]
    lum = 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
    opaque = alpha > (8.0 / 255.0)
    if not opaque.any():
        empty = np.zeros((MASK_SIZE, MASK_SIZE, 2), dtype=np.uint8)
        empty[..., 0] = 255
        return Image.fromarray(empty)

    values = lum[opaque]
    low, high = np.percentile(values, [1.0, 99.0])
    span = float(high - low)
    alpha_values = alpha[opaque]
    mean = float((values * alpha_values).sum() / max(float(alpha_values.sum()), 1e-6))
    if span >= 0.08:
        level = np.clip((lum - low) / max(span, 1e-3), 0.0, 1.0)
        if mean < 0.5:
            level = 1.0 - level
    else:
        level = np.ones_like(lum)
    out_alpha = np.clip(alpha * (0.06 + 0.94 * level), 0.0, 1.0)
    result = np.zeros((MASK_SIZE, MASK_SIZE, 2), dtype=np.uint8)
    result[..., 0] = 255
    result[..., 1] = np.rint(out_alpha * 255.0).astype(np.uint8)
    return Image.fromarray(result)


def mask_image_to_svg(
    mask: "Image.Image", style: ColorStyle = DEFAULT_COLOR_STYLE
) -> str:
    buffer = io.BytesIO()
    mask.save(buffer, format="PNG", optimize=True, compress_level=9)
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{MASK_SIZE}" height="{MASK_SIZE}" '
        f'viewBox="0 0 {MASK_SIZE} {MASK_SIZE}">\n'
        f'  <defs>\n'
        f'    <style id="current-color-scheme" type="text/css">'
        f'.{style.css_class} {{ color:{style.fallback}; }}</style>\n'
        f'    <mask id="accent-mask" maskUnits="userSpaceOnUse" x="0" y="0" '
        f'width="{MASK_SIZE}" height="{MASK_SIZE}">\n'
        f'      <image href="data:image/png;base64,{payload}" '
        f'width="{MASK_SIZE}" height="{MASK_SIZE}" preserveAspectRatio="none"/>\n'
        f'    </mask>\n'
        f'  </defs>\n'
        f'  <rect x="0" y="0" width="{MASK_SIZE}" height="{MASK_SIZE}" '
        f'fill="currentColor" class="{style.css_class}" mask="url(#accent-mask)"/>\n'
        f'</svg>\n'
    )


def _mask_cache_path(src: Path, style: ColorStyle = DEFAULT_COLOR_STYLE) -> Path:
    digest = hashlib.sha256()
    digest.update(str(MASK_VERSION).encode("ascii"))
    digest.update(style.mode.encode("ascii"))
    digest.update(src.suffix.lower().encode("ascii"))
    with src.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return CACHE_DIR / "masks" / f"{digest.hexdigest()}.svg"


def generate_mask_svg(src: Path, style: ColorStyle = DEFAULT_COLOR_STYLE) -> str:
    cache_path = _mask_cache_path(src, style)
    if cache_path.is_file():
        return cache_path.read_text(encoding="utf-8")
    ensure_mask_deps()
    image = render_source_to_rgba(src)
    mask = make_mask_image(image)
    svg = mask_image_to_svg(mask, style)
    _write_text_atomic(cache_path, svg)
    return svg


def _mask_worker(args: tuple[str, str, str]) -> tuple[str, Optional[str], Optional[str]]:
    key, src_name, color_mode = args
    try:
        return key, generate_mask_svg(Path(src_name), resolve_color_style(color_mode)), None
    except Exception as exc:
        return key, None, str(exc)


# ---------------------------------------------------------------------------
# Base theme information
# ---------------------------------------------------------------------------

@dataclass
class BaseInfo:
    path: Path
    index: OrderedDict[str, OrderedDict[str, str]]
    dirs: list[str]
    names: set[str]
    fingerprint: str


def _base_fingerprint(theme_path: Path, dirs: list[str], index_bytes: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(index_bytes)
    for dirname in dirs:
        root = theme_path / dirname
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for filename in sorted(filenames):
                path = Path(dirpath) / filename
                try:
                    stat_result = path.lstat()
                except OSError:
                    continue
                rel = path.relative_to(theme_path).as_posix()
                digest.update(rel.encode("utf-8"))
                digest.update(str(stat_result.st_mtime_ns).encode("ascii"))
                digest.update(str(stat_result.st_size).encode("ascii"))
                if path.is_symlink():
                    try:
                        digest.update(os.readlink(path).encode("utf-8"))
                    except OSError:
                        pass
    return digest.hexdigest()


def load_base(name: str) -> BaseInfo:
    path = find_theme_dir(name)
    if path is None:
        die(
            f"base icon theme {name!r} was not found.\n"
            "Install it in ~/.local/share/icons or /usr/share/icons, or pass --base."
        )
    index_path = path / INDEX_FILE
    index = parse_theme_index(index_path)
    dirs = index_directories(index)
    names: set[str] = set()
    for dirname in dirs:
        root = path / dirname
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for filename in filenames:
                if filename.startswith("."):
                    continue
                candidate_name = icon_name(filename)
                if candidate_name:
                    names.add(candidate_name)
    fingerprint = _base_fingerprint(path, dirs, index_path.read_bytes())
    return BaseInfo(path, index, dirs, names, fingerprint)


def copy_base_theme(base: BaseInfo, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(base.path, destination, symlinks=True)
    for cache in destination.rglob("icon-theme.cache"):
        try:
            cache.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Input/planning helpers
# ---------------------------------------------------------------------------

@dataclass
class MaskJob:
    key: str
    src: Path
    out: Path
    kind: str  # "base" or "generated"
    name: str
    out_rel: str = ""
    context: str = ""
    candidate: Optional[Candidate] = None
    fallback: list[Candidate] = field(default_factory=list)


def context_output_dir(
    context: str,
    base_dirs: list[str],
    added_dirs: OrderedDict[str, tuple[str, int, int, int]],
) -> str:
    context = context.lower()
    if context == "applications":
        context = "apps"
    out_dir, display_context = CONTEXT_OUTPUT.get(context, ("apps/scalable", "Applications"))
    if out_dir not in base_dirs and out_dir not in added_dirs:
        added_dirs[out_dir] = (display_context, 64, 8, 512)
    return out_dir


def _plan_candidate(
    name: str,
    candidate: Candidate,
    staging: Path,
    base_dirs: list[str],
    added_dirs: OrderedDict[str, tuple[str, int, int, int]],
    style: ColorStyle = DEFAULT_COLOR_STYLE,
) -> tuple[str, str, Path]:
    out_dir = context_output_dir(candidate.context, base_dirs, added_dirs)
    out_rel = f"{out_dir}/{name}.svg"
    out_path = staging / out_rel
    if candidate.fmt in (".svg", ".svgz"):
        text = decode_svg(candidate.path)
        action = analyze_svg(text)
        if action == "patch":
            _write_text_atomic(out_path, patch_svg_text(text, normalize_size=True, style=style))
            return "patch", out_rel, out_path
        if action == "empty":
            return "empty", out_rel, out_path
        return "mask", out_rel, out_path
    return "mask", out_rel, out_path


def _record_generated(manifest_icons: dict, name: str, out_rel: str, method: str, candidate: Candidate) -> None:
    try:
        stat_result = candidate.path.stat()
        mtime = stat_result.st_mtime_ns
        size = stat_result.st_size
    except OSError:
        mtime = 0
        size = 0
    manifest_icons[name] = {
        "src": str(candidate.path),
        "src_mtime_ns": mtime,
        "src_size": size,
        "out": out_rel,
        "method": method,
        "context": candidate.context,
    }


def _base_output_path(path: Path) -> Path:
    suffix = path.suffix.lower()
    if suffix in (".svgz", ".png", ".xpm"):
        return path.with_suffix(".svg")
    return path


def _resolve_staging_file(path: Path, staging: Path) -> Optional[Path]:
    try:
        real = path.resolve()
    except OSError:
        return None
    if not real.exists():
        return None
    try:
        real.relative_to(staging.resolve())
        return real
    except ValueError:
        # A symlink pointing outside the theme: replace it with a real copy.
        try:
            data = real.read_bytes()
            path.unlink()
            path.write_bytes(data)
            return path.resolve()
        except OSError as exc:
            log(f"cannot dereference {path}: {exc}", "warning")
            return None


def process_base(
    base: BaseInfo,
    staging: Path,
    mask_jobs: list[MaskJob],
    stats: Counter,
    style: ColorStyle = DEFAULT_COLOR_STYLE,
) -> None:
    real_paths: set[Path] = set()
    for dirname in base.dirs:
        root_dir = staging / dirname
        if not root_dir.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for filename in sorted(filenames):
                if filename.startswith("."):
                    continue
                path = Path(dirpath) / filename
                if path.suffix.lower() not in SUPPORTED_EXT:
                    continue
                real = _resolve_staging_file(path, staging)
                if real is not None:
                    real_paths.add(real)

    for real in sorted(real_paths):
        suffix = real.suffix.lower()
        if suffix in (".svg", ".svgz"):
            try:
                text = decode_svg(real)
            except Exception as exc:
                log(f"cannot read {real}: {exc}", "warning")
                stats["base_read_errors"] += 1
                continue
            action = analyze_svg(text)
            out_path = _base_output_path(real)
            if action == "patch":
                try:
                    _write_text_atomic(
                        out_path, patch_svg_text(text, normalize_size=False, style=style)
                    )
                    stats["base_patched"] += 1
                except Exception as exc:
                    log(f"cannot patch {real}: {exc}", "warning")
                    stats["base_patch_errors"] += 1
            elif action == "mask":
                mask_jobs.append(
                    MaskJob(
                        key=f"base:{real}",
                        src=real,
                        out=out_path,
                        kind="base",
                        name=real.stem,
                    )
                )
                stats["base_masks_queued"] += 1
            else:
                stats["base_empty"] += 1
        else:
            out_path = _base_output_path(real)
            mask_jobs.append(
                MaskJob(
                    key=f"base:{real}",
                    src=real,
                    out=out_path,
                    kind="base",
                    name=real.stem,
                )
            )
            stats["base_masks_queued"] += 1


def _generate_name(
    name: str,
    candidates: list[Candidate],
    staging: Path,
    base_dirs: list[str],
    added_dirs: OrderedDict[str, tuple[str, int, int, int]],
    manifest_icons: dict,
    mask_jobs: Optional[list[MaskJob]],
    unresolved: list[str],
    stats: Counter,
    style: ColorStyle = DEFAULT_COLOR_STYLE,
) -> Optional[str]:
    for index, candidate in enumerate(candidates):
        try:
            action, out_rel, out_path = _plan_candidate(
                name, candidate, staging, base_dirs, added_dirs, style
            )
        except Exception as exc:
            log(f"{name}: cannot use {candidate.path}: {exc}", "debug")
            stats["candidate_errors"] += 1
            continue
        if action == "empty":
            stats["candidate_empty"] += 1
            continue
        if action == "patch":
            _record_generated(manifest_icons, name, out_rel, "patch", candidate)
            stats["generated_patch"] += 1
            return "patch"
        if action == "mask":
            if mask_jobs is not None:
                mask_jobs.append(
                    MaskJob(
                        key=f"gen:{out_path}",
                        src=candidate.path,
                        out=out_path,
                        kind="generated",
                        name=name,
                        out_rel=out_rel,
                        context=candidate.context,
                        candidate=candidate,
                        fallback=candidates[index + 1 :],
                    )
                )
                stats["generated_masks_queued"] += 1
                return "pending"
            try:
                svg = generate_mask_svg(candidate.path, style)
            except Exception as exc:
                log(f"{name}: mask failed for {candidate.path}: {exc}", "debug")
                stats["candidate_errors"] += 1
                continue
            _write_text_atomic(out_path, svg)
            _record_generated(manifest_icons, name, out_rel, "mask", candidate)
            stats["generated_mask"] += 1
            return "mask"
    if name not in unresolved:
        unresolved.append(name)
    stats["unresolved"] += 1
    return None


def run_mask_jobs(
    jobs: list[MaskJob],
    workers: int = 0,
    style: ColorStyle = DEFAULT_COLOR_STYLE,
) -> dict[str, tuple[Optional[str], Optional[str]]]:
    if not jobs:
        return {}
    results: dict[str, tuple[Optional[str], Optional[str]]] = {}
    if workers <= 0:
        workers = min(8, os.cpu_count() or 4)
    if len(jobs) == 1 or workers <= 1:
        for job in jobs:
            results[job.key] = _mask_worker((job.key, str(job.src), style.mode))[1:]
        return results

    try:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_mask_worker, (job.key, str(job.src), style.mode)): job
                for job in jobs
            }
            completed = 0
            for future in as_completed(futures):
                job = futures[future]
                try:
                    key, svg, error = future.result()
                except Exception as exc:
                    key, svg, error = job.key, None, str(exc)
                results[key] = (svg, error)
                completed += 1
                if VERBOSE and (completed % 50 == 0 or completed == len(jobs)):
                    log(f"masks {completed}/{len(jobs)}", "debug")
    except Exception as exc:
        log(f"parallel mask generation failed ({exc}); using one process", "warning")
        for job in jobs:
            if job.key not in results:
                results[job.key] = _mask_worker((job.key, str(job.src), style.mode))[1:]
    return results


def apply_mask_results(
    jobs: list[MaskJob],
    results: dict[str, tuple[Optional[str], Optional[str]]],
    staging: Path,
    base_dirs: list[str],
    added_dirs: OrderedDict[str, tuple[str, int, int, int]],
    manifest_icons: dict,
    unresolved: list[str],
    stats: Counter,
    style: ColorStyle = DEFAULT_COLOR_STYLE,
) -> None:
    for job in jobs:
        svg, error = results.get(job.key, (None, "missing result"))
        if svg:
            try:
                _write_text_atomic(job.out, svg)
            except Exception as exc:
                error = f"cannot write {job.out}: {exc}"
                svg = None
        if svg:
            if job.kind == "base":
                stats["base_masked"] += 1
            elif job.candidate is not None:
                _record_generated(manifest_icons, job.name, job.out_rel, "mask", job.candidate)
                stats["generated_mask"] += 1
            continue

        log(f"mask failed for {job.name}: {error}", "warning")
        if job.kind == "base":
            stats["base_mask_errors"] += 1
            continue
        if job.fallback:
            _generate_name(
                job.name,
                job.fallback,
                staging,
                base_dirs,
                added_dirs,
                manifest_icons,
                None,
                unresolved,
                stats,
                style,
            )
        elif job.name not in unresolved:
            unresolved.append(job.name)
            stats["unresolved"] += 1


def write_index(
    staging: Path,
    base_index: OrderedDict[str, OrderedDict[str, str]],
    base_dirs: list[str],
    added_dirs: OrderedDict[str, tuple[str, int, int, int]],
) -> None:
    index: OrderedDict[str, OrderedDict[str, str]] = OrderedDict()
    for section, values in base_index.items():
        index[section] = OrderedDict(values)
    icon = index.setdefault("Icon Theme", OrderedDict())
    icon["Name"] = THEME_DISPLAY_NAME
    icon["Comment"] = "YAMIS-based icon theme with all icons following the Plasma accent color."
    icon["Inherits"] = "hicolor"
    icon["DisplayDepth"] = "32"
    icon["FollowsColorScheme"] = "true"
    icon["Example"] = "folder"
    icon["KDE-Extensions"] = ".svg"

    all_dirs = list(base_dirs)
    for dirname in added_dirs:
        if dirname not in all_dirs:
            all_dirs.append(dirname)
    icon["Directories"] = ",".join(all_dirs)

    for dirname in all_dirs:
        if dirname in index:
            continue
        context, size, min_size, max_size = added_dirs.get(
            dirname, ("Applications", 64, 8, 512)
        )
        index[dirname] = OrderedDict(
            [
                ("Context", context),
                ("Size", str(size)),
                ("MinSize", str(min_size)),
                ("MaxSize", str(max_size)),
                ("Type", "Scalable"),
            ]
        )

    lines: list[str] = []
    for section, values in index.items():
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key}={value}")
        lines.append("")
    _write_text_atomic(staging / INDEX_FILE, "\n".join(lines))


def scan_output_names(theme_dir: Path) -> set[str]:
    index_path = theme_dir / INDEX_FILE
    if not index_path.is_file():
        return set()
    index = parse_theme_index(index_path)
    names: set[str] = set()
    for dirname in index_directories(index):
        root = theme_dir / dirname
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for filename in filenames:
                if filename.startswith("."):
                    continue
                name = icon_name(filename)
                if name:
                    names.add(name)
    return names


def directory_size(path: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for filename in filenames:
            try:
                total += (Path(dirpath) / filename).lstat().st_size
            except OSError:
                pass
    return total


def _project_resource_dirs() -> list[Path]:
    here = Path(__file__).resolve().parent
    dirs = [here]
    if RESOURCE_DIR.resolve() != here:
        dirs.append(RESOURCE_DIR)
    return dirs


def copy_project_notices(theme_dir: Path) -> None:
    sources = _project_resource_dirs()
    for filename in ("NOTICE.md", "README.md", "LICENSE"):
        for source in sources:
            candidate = source / filename
            if candidate.is_file():
                try:
                    shutil.copy2(candidate, theme_dir / filename)
                except OSError as exc:
                    log(f"cannot copy {filename}: {exc}", "warning")
                break
    for source in sources:
        licenses_dir = source / "licenses"
        if licenses_dir.is_dir():
            try:
                shutil.copytree(licenses_dir, theme_dir / "licenses", dirs_exist_ok=True)
            except OSError as exc:
                log(f"cannot copy licenses/: {exc}", "warning")
            break


def build_theme(
    base_name: str,
    output: Path,
    workers: int = 0,
    style: ColorStyle = DEFAULT_COLOR_STYLE,
) -> dict:
    base = load_base(base_name)
    log(f"base: {base.path} ({len(base.names)} icon names)")
    copy_base_theme(base, output)
    added_dirs: OrderedDict[str, tuple[str, int, int, int]] = OrderedDict()
    mask_jobs: list[MaskJob] = []
    manifest_icons: dict = {}
    unresolved: list[str] = []
    stats: Counter = Counter()

    log("processing YAMIS icons...")
    process_base(base, output, mask_jobs, stats, style)

    log("scanning fallback icon themes...")
    catalog = collect_catalog(source_specs())
    missing = sorted(set(catalog) - base.names)
    log(f"fallback names missing from {base_name}: {len(missing)}")
    for position, name in enumerate(missing, 1):
        _generate_name(
            name,
            catalog[name],
            output,
            base.dirs,
            added_dirs,
            manifest_icons,
            mask_jobs,
            unresolved,
            stats,
            style,
        )
        if VERBOSE and position % 500 == 0:
            log(f"planned {position}/{len(missing)}", "debug")

    log(f"rendering masks: {len(mask_jobs)}")
    results = run_mask_jobs(mask_jobs, workers, style)
    apply_mask_results(
        mask_jobs,
        results,
        output,
        base.dirs,
        added_dirs,
        manifest_icons,
        unresolved,
        stats,
        style,
    )

    write_index(output, base.index, base.dirs, added_dirs)
    copy_project_notices(output)

    output_names = scan_output_names(output)
    missing_base = sorted(base.names - output_names)
    if missing_base:
        log(
            f"{len(missing_base)} base icon names are not reachable through the generated index: "
            + ", ".join(missing_base[:10]),
            "warning",
        )

    manifest = {
        "format": 1,
        "tool_version": VERSION,
        "mask_version": MASK_VERSION,
        "color_mode": style.mode,
        "color_class": style.css_class,
        "theme": THEME_DIR_NAME,
        "base": base_name,
        "base_path": str(base.path),
        "base_fingerprint": base.fingerprint,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_icon_count": len(base.names),
        "generated": manifest_icons,
        "unresolved": sorted(unresolved),
        "added_dirs": {k: list(v) for k, v in added_dirs.items()},
    }
    _write_text_atomic(
        output / MANIFEST_NAME,
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )

    return {
        "base": base,
        "manifest": manifest,
        "stats": stats,
        "output_names": output_names,
        "missing_base": missing_base,
        "size": directory_size(output),
    }


# ---------------------------------------------------------------------------
# Manifest, sync, install and status
# ---------------------------------------------------------------------------

def load_manifest(theme_dir: Path) -> Optional[dict]:
    path = theme_dir / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log(f"cannot read manifest {path}: {exc}", "warning")
        return None


def save_manifest(theme_dir: Path, manifest: dict) -> None:
    _write_text_atomic(
        theme_dir / MANIFEST_NAME,
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )


def swap_theme(staging: Path, theme_dir: Path) -> None:
    theme_dir.parent.mkdir(parents=True, exist_ok=True)
    backup = theme_dir.with_name(f".{theme_dir.name}.old-{int(time.time())}")
    had_old = theme_dir.exists()
    if had_old:
        theme_dir.rename(backup)
    try:
        shutil.move(str(staging), str(theme_dir))
    except Exception:
        if had_old and backup.exists() and not theme_dir.exists():
            backup.rename(theme_dir)
        raise
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)


def _candidate_still_valid(entry: dict, candidate: Candidate) -> bool:
    if entry.get("src") != str(candidate.path):
        return False
    try:
        stat_result = candidate.path.stat()
    except OSError:
        return False
    return (
        entry.get("src_mtime_ns") == stat_result.st_mtime_ns
        and entry.get("src_size") == stat_result.st_size
    )


def _remove_output(theme_dir: Path, out_rel: str) -> None:
    if not out_rel:
        return
    path = theme_dir / out_rel
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(f"cannot remove {path}: {exc}", "warning")


def sync_theme(
    theme_dir: Path,
    base_name: str,
    workers: int = 0,
    force: bool = False,
    dry_run: bool = False,
    style: ColorStyle = DEFAULT_COLOR_STYLE,
) -> dict:
    manifest = load_manifest(theme_dir)
    if manifest is None or not (theme_dir / INDEX_FILE).is_file():
        die(f"{theme_dir} is not an installed {THEME_DIR_NAME} theme; run install first")

    base = load_base(base_name)
    # Manifests created before color modes existed are treated as "text",
    # which is also the default for new installs.
    color_changed = manifest.get("color_mode", DEFAULT_COLOR_MODE) != style.mode
    base_changed = (
        force
        or color_changed
        or manifest.get("base") != base_name
        or manifest.get("base_fingerprint") != base.fingerprint
        or manifest.get("mask_version") != MASK_VERSION
    )

    output_names = scan_output_names(theme_dir)
    missing_base = base.names - output_names
    if missing_base:
        base_changed = True

    if base_changed:
        reason = "color mode changed" if color_changed else "base theme changed"
        log(f"{reason}; rebuilding the whole theme")
        if dry_run:
            return {"full_rebuild": True, "added": 0, "removed": 0}
        staging = theme_dir.with_name(f".{theme_dir.name}.sync-{int(time.time())}")
        if staging.exists():
            shutil.rmtree(staging)
        result = build_theme(base_name, staging, workers, style)
        swap_theme(staging, theme_dir)
        return {
            "full_rebuild": True,
            "added": len(result["manifest"].get("generated", {})),
            "removed": 0,
            "manifest": result["manifest"],
        }

    catalog = collect_catalog(source_specs())
    desired = set(catalog) - base.names
    existing: dict = manifest.get("generated", {})
    added_dirs: OrderedDict[str, tuple[str, int, int, int]] = OrderedDict(
        (key, tuple(value)) for key, value in manifest.get("added_dirs", {}).items()
    )
    to_generate: list[str] = []
    for name in sorted(desired):
        entry = existing.get(name)
        best = catalog[name][0]
        if entry is None:
            to_generate.append(name)
            continue
        out_path = theme_dir / entry.get("out", "")
        if not out_path.is_file() or not _candidate_still_valid(entry, best):
            to_generate.append(name)

    removed = [name for name in existing if name not in desired]
    if dry_run:
        log(
            f"would add/update {len(to_generate)} icons and remove {len(removed)}; "
            f"base names: {len(base.names)}, available fallback names: {len(catalog)}"
        )
        if to_generate:
            log("to add/update: " + ", ".join(to_generate[:20]))
        if removed:
            log("to remove: " + ", ".join(removed[:20]))
        return {"full_rebuild": False, "added": len(to_generate), "removed": len(removed)}

    if not to_generate and not removed:
        log("icon theme is already up to date")
        return {"full_rebuild": False, "added": 0, "removed": 0}

    unresolved: list[str] = []
    stats: Counter = Counter()
    mask_jobs: list[MaskJob] = []
    for name in to_generate:
        old_out = existing.get(name, {}).get("out")
        _generate_name(
            name,
            catalog[name],
            theme_dir,
            base.dirs,
            added_dirs,
            existing,
            mask_jobs,
            unresolved,
            stats,
            style,
        )
        new_out = existing.get(name, {}).get("out")
        if old_out and new_out and old_out != new_out:
            _remove_output(theme_dir, old_out)

    results = run_mask_jobs(mask_jobs, workers, style)
    apply_mask_results(
        mask_jobs,
        results,
        theme_dir,
        base.dirs,
        added_dirs,
        existing,
        unresolved,
        stats,
        style,
    )

    for name in removed:
        entry = existing.pop(name, None)
        if entry:
            _remove_output(theme_dir, entry.get("out", ""))

    # Rebuild the added-directory list from the icons that actually exist.
    added_dirs = OrderedDict()
    for entry in existing.values():
        context_output_dir(entry.get("context", "apps"), base.dirs, added_dirs)

    write_index(theme_dir, base.index, base.dirs, added_dirs)
    manifest["generated"] = existing
    manifest["unresolved"] = sorted(set(desired) - set(existing))
    manifest["base"] = base_name
    manifest["base_path"] = str(base.path)
    manifest["base_fingerprint"] = base.fingerprint
    manifest["base_icon_count"] = len(base.names)
    manifest["mask_version"] = MASK_VERSION
    manifest["color_mode"] = style.mode
    manifest["color_class"] = style.css_class
    manifest["built_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    manifest["added_dirs"] = {key: list(value) for key, value in added_dirs.items()}
    save_manifest(theme_dir, manifest)

    log(
        f"sync complete: {len(to_generate)} added/updated, {len(removed)} removed, "
        f"{len(manifest['unresolved'])} unresolved"
    )
    return {
        "full_rebuild": False,
        "added": len(to_generate),
        "removed": len(removed),
        "stats": stats,
        "manifest": manifest,
    }


def run_kbuildsycoca() -> None:
    executable = shutil.which("kbuildsycoca6") or shutil.which("kbuildsycoca5")
    if not executable:
        log("kbuildsycoca6 not found; skip menu cache refresh", "warning")
        return
    try:
        subprocess.run(
            [executable, "--noincremental"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
    except Exception as exc:
        log(f"kbuildsycoca failed: {exc}", "warning")


def clear_icon_cache() -> None:
    candidates = [
        XDG_CACHE_HOME / "icon-cache.kcache",
        XDG_CACHE_HOME / "kiconcache",
    ]
    for path in candidates:
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass


def activate_theme() -> None:
    theme_dir = DEFAULT_INSTALL_DIR
    if not (theme_dir / INDEX_FILE).is_file():
        die(f"{theme_dir} is not installed; run install first")
    kwrite = shutil.which("kwriteconfig6") or shutil.which("kwriteconfig5")
    if not kwrite:
        die("kwriteconfig6 not found")
    subprocess.run(
        [kwrite, "--file", "kdeglobals", "--group", "Icons", "--key", "Theme", THEME_DIR_NAME],
        check=True,
    )
    clear_icon_cache()
    run_kbuildsycoca()
    log(f"activated {THEME_DIR_NAME}")
    log("restart plasmashell or log out/in so already running applications reload their icons")


def install_theme(
    base_name: str,
    output: Path,
    workers: int = 0,
    refresh: bool = True,
    activate: bool = False,
    style: ColorStyle = DEFAULT_COLOR_STYLE,
) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_name(f".{output.name}.install-{int(time.time())}")
    if staging.exists():
        shutil.rmtree(staging)
    log(f"building {THEME_DIR_NAME} -> {staging}")
    result = build_theme(base_name, staging, workers, style)
    swap_theme(staging, output)
    if refresh:
        run_kbuildsycoca()
    if activate:
        activate_theme()
    stats = result["stats"]
    print(
        f"installed: {output}\n"
        f"  color mode: {style.mode} ({style.css_class})\n"
        f"  base icons: {result['manifest']['base_icon_count']}\n"
        f"  generated icons: {len(result['manifest']['generated'])}\n"
        f"  patched base: {stats.get('base_patched', 0)}, masked base: {stats.get('base_masked', 0)}\n"
        f"  unresolved: {len(result['manifest']['unresolved'])}\n"
        f"  size: {result['size'] / (1024 * 1024):.1f} MiB"
    )
    return result


def kreadconfig(group: str, key: str) -> str:
    executable = shutil.which("kreadconfig6") or shutil.which("kreadconfig5")
    if not executable:
        return ""
    try:
        result = subprocess.run(
            [executable, "--file", "kdeglobals", "--group", group, "--key", key],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def desktop_icons() -> dict[str, Path]:
    icons: dict[str, Path] = {}
    for root in DESKTOP_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*.desktop"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            icon = None
            no_display = False
            for raw in text.splitlines():
                line = raw.strip()
                if line.startswith("Icon=") and icon is None:
                    icon = line.split("=", 1)[1].strip()
                elif line == "NoDisplay=true":
                    no_display = True
            if icon and not no_display:
                icons.setdefault(icon, path)
    return icons


def status_theme(theme_dir: Path) -> None:
    if not (theme_dir / INDEX_FILE).is_file():
        print(f"theme: {theme_dir} (not installed)")
    else:
        index = parse_theme_index(theme_dir / INDEX_FILE)
        follows = index.get("Icon Theme", {}).get("FollowsColorScheme", "")
        names = scan_output_names(theme_dir)
        manifest = load_manifest(theme_dir)
        print(f"theme: {theme_dir}")
        print(f"  follows color scheme: {follows}")
        if manifest:
            print(
                "  color mode: "
                f"{manifest.get('color_mode', DEFAULT_COLOR_MODE)} "
                f"({manifest.get('color_class', '')})"
            )
        print(f"  icon names: {len(names)}")
        print(f"  size: {directory_size(theme_dir) / (1024 * 1024):.1f} MiB")
        if manifest:
            generated = manifest.get("generated", {})
            methods = Counter(entry.get("method", "?") for entry in generated.values())
            source_labels = []
            for entry in generated.values():
                parts = Path(entry.get("src", "")).parts
                source_labels.append(parts[-4] if len(parts) >= 4 else "?")
            themes = Counter(source_labels)
            print(f"  generated: {len(generated)} ({dict(methods)})")
            print(f"  sources: {dict(themes.most_common(6))}")
            print(f"  base fingerprint: {manifest.get('base_fingerprint', '')[:16]}")
            print(f"  built: {manifest.get('built_at', '')}")
            if manifest.get("unresolved"):
                print(f"  unresolved: {len(manifest['unresolved'])}")
                for name in manifest["unresolved"][:20]:
                    print(f"    - {name}")
        else:
            print("  manifest: missing (run install to create it)")

    active = kreadconfig("Icons", "Theme")
    accent = kreadconfig("General", "AccentColor")
    accent_from_wallpaper = kreadconfig("General", "accentColorFromWallpaper")
    print(f"active icon theme: {active or '(unknown)'}")
    print(f"accent color: {accent or '(unknown)'}" + (
        " (from wallpaper)" if accent_from_wallpaper == "true" else ""
    ))
    if active != THEME_DIR_NAME:
        print(f"note: {THEME_DIR_NAME} is not active; run activate when ready")

    icons = desktop_icons()
    missing = sorted(
        name for name in icons if not name.startswith("/") and name not in names
    )
    absolute = sorted(name for name in icons if name.startswith("/"))
    print(f"desktop icon names: {len(icons)}")
    if missing:
        print(f"desktop icons without a themed file: {len(missing)}")
        for name in missing[:30]:
            print(f"  - {name}  ({icons[name].name})")
    if absolute:
        print(f"absolute-path desktop icons (cannot be themed): {len(absolute)}")
        for name in absolute[:10]:
            print(f"  - {name}")


def install_units(enable: bool = False, disable: bool = False) -> None:
    LOCAL_BIN.parent.mkdir(parents=True, exist_ok=True)
    script_source = Path(__file__).resolve()
    if script_source != LOCAL_BIN.resolve():
        shutil.copy2(script_source, LOCAL_BIN)
    os.chmod(LOCAL_BIN, 0o755)

    # Keep the notices next to the installed helper so a full rebuild started
    # by the systemd timer still ships the license files.
    RESOURCE_DIR.mkdir(parents=True, exist_ok=True)
    source_dir = script_source.parent
    for filename in ("NOTICE.md", "README.md", "LICENSE"):
        source = source_dir / filename
        if source.is_file():
            try:
                shutil.copy2(source, RESOURCE_DIR / filename)
            except OSError as exc:
                log(f"cannot install {filename}: {exc}", "warning")
    licenses_dir = source_dir / "licenses"
    if licenses_dir.is_dir():
        try:
            shutil.copytree(licenses_dir, RESOURCE_DIR / "licenses", dirs_exist_ok=True)
        except OSError as exc:
            log(f"cannot install licenses/: {exc}", "warning")

    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)

    installed_manifest = load_manifest(DEFAULT_INSTALL_DIR) or {}
    installed_mode = installed_manifest.get("color_mode", DEFAULT_COLOR_MODE)
    service = f"""[Unit]
Description=Synchronize the {THEME_DISPLAY_NAME} icon theme
After=graphical-session.target
ConditionPathExists=%h/.local/share/icons/{THEME_DIR_NAME}/index.theme

[Service]
Type=oneshot
ExecStart=%h/.local/bin/{APP_NAME} sync --quiet --color {installed_mode}
Nice=10
IOSchedulingClass=idle
"""
    timer = """[Unit]
Description=Periodically synchronize the Plasma Accent icon theme

[Timer]
OnStartupSec=2min
OnUnitActiveSec=15min
RandomizedDelaySec=30s
AccuracySec=1min
Unit=plasma-accent-icons-sync.service

[Install]
WantedBy=timers.target
"""
    (SYSTEMD_USER_DIR / "plasma-accent-icons-sync.service").write_text(service, encoding="utf-8")
    (SYSTEMD_USER_DIR / "plasma-accent-icons-sync.timer").write_text(timer, encoding="utf-8")
    systemctl = shutil.which("systemctl")
    if systemctl:
        subprocess.run([systemctl, "--user", "daemon-reload"], check=False)
        if disable:
            subprocess.run(
                [systemctl, "--user", "disable", "--now", "plasma-accent-icons-sync.timer"],
                check=False,
            )
        if enable:
            subprocess.run(
                [systemctl, "--user", "enable", "--now", "plasma-accent-icons-sync.timer"],
                check=False,
            )
    print(f"installed: {LOCAL_BIN}")
    print(f"installed: {SYSTEMD_USER_DIR}/plasma-accent-icons-sync.{{service,timer}}")
    if enable:
        print("timer enabled: plasma-accent-icons-sync.timer")
    elif disable:
        print("timer disabled")
    else:
        print("timer not enabled; run install-units --enable to turn it on")


def uninstall_theme(theme_dir: Path, force: bool = False, remove_units: bool = False) -> None:
    active = kreadconfig("Icons", "Theme")
    if active == THEME_DIR_NAME and not force:
        die(
            f"{THEME_DIR_NAME} is the active icon theme. Switch to another theme first, "
            "or pass --force."
        )
    if theme_dir.exists():
        shutil.rmtree(theme_dir)
        print(f"removed: {theme_dir}")
    if remove_units:
        systemctl = shutil.which("systemctl")
        if systemctl:
            subprocess.run(
                [systemctl, "--user", "disable", "--now", "plasma-accent-icons-sync.timer"],
                check=False,
            )
        for filename in ("plasma-accent-icons-sync.service", "plasma-accent-icons-sync.timer"):
            path = SYSTEMD_USER_DIR / filename
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        if systemctl:
            subprocess.run([systemctl, "--user", "daemon-reload"], check=False)
        try:
            LOCAL_BIN.unlink()
        except FileNotFoundError:
            pass
        if RESOURCE_DIR.exists():
            shutil.rmtree(RESOURCE_DIR, ignore_errors=True)
    run_kbuildsycoca()


def clean_cache(all_files: bool = False) -> None:
    for path in [CACHE_DIR / "masks", CACHE_DIR / "build"]:
        if path.exists():
            shutil.rmtree(path)
            print(f"removed: {path}")
    if all_files and CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
        print(f"removed: {CACHE_DIR}")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def print_build_summary(output: Path, result: dict) -> None:
    stats = result["stats"]
    manifest = result["manifest"]
    print(
        f"built: {output}\n"
        f"  base icons: {manifest['base_icon_count']}\n"
        f"  generated icons: {len(manifest['generated'])}\n"
        f"  patched base: {stats.get('base_patched', 0)}, masked base: {stats.get('base_masked', 0)}\n"
        f"  unresolved: {len(manifest['unresolved'])}\n"
        f"  size: {result['size'] / (1024 * 1024):.1f} MiB"
    )
    if manifest["unresolved"]:
        print("  first unresolved: " + ", ".join(manifest["unresolved"][:10]))


def main(argv: Optional[list[str]] = None) -> int:
    global QUIET, VERBOSE

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
                        help="only print warnings and errors")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="print progress details")

    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Build and maintain a KDE icon theme that follows the system accent color.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    color_choices = sorted(COLOR_MODES)
    color_help = (
        "palette class used for the icons (default: text, the light window text "
        "color; accent uses the darker selection/accent color)"
    )

    p_build = sub.add_parser("build", parents=[common], help="build the theme in a directory")
    p_build.add_argument("--base", default=DEFAULT_BASE_THEME, help="base theme (default: YAMIS)")
    p_build.add_argument("--output", default=str(DEFAULT_BUILD_DIR), help="output directory")
    p_build.add_argument("--color", choices=color_choices, default=DEFAULT_COLOR_MODE, help=color_help)
    p_build.add_argument("--jobs", type=int, default=0, help="mask worker processes (0 = auto)")
    p_build.add_argument("--no-cache", action="store_true", help="discard the mask cache first")

    p_install = sub.add_parser("install", parents=[common], help="build and install the theme")
    p_install.add_argument("--base", default=DEFAULT_BASE_THEME, help="base theme (default: YAMIS)")
    p_install.add_argument("--output", default=str(DEFAULT_INSTALL_DIR), help="install directory")
    p_install.add_argument("--color", choices=color_choices, default=DEFAULT_COLOR_MODE, help=color_help)
    p_install.add_argument("--jobs", type=int, default=0, help="mask worker processes (0 = auto)")
    p_install.add_argument("--no-cache", action="store_true", help="discard the mask cache first")
    p_install.add_argument("--no-refresh", action="store_true", help="skip kbuildsycoca")
    p_install.add_argument("--activate", action="store_true", help="activate the theme after installing")

    p_sync = sub.add_parser("sync", parents=[common], help="add icons for newly installed applications")
    p_sync.add_argument("--base", default=DEFAULT_BASE_THEME, help="base theme (default: YAMIS)")
    p_sync.add_argument("--output", default=str(DEFAULT_INSTALL_DIR), help="installed theme directory")
    p_sync.add_argument("--color", choices=color_choices, default=None,
                        help="override the installed color mode (default: keep the current one)")
    p_sync.add_argument("--jobs", type=int, default=0, help="mask worker processes (0 = auto)")
    p_sync.add_argument("--force", action="store_true", help="rebuild the whole theme")
    p_sync.add_argument("--dry-run", action="store_true", help="show what would change")

    p_status = sub.add_parser("status", parents=[common], help="show theme and coverage status")
    p_status.add_argument("--output", default=str(DEFAULT_INSTALL_DIR), help="theme directory")

    sub.add_parser("activate", parents=[common], help="switch the KDE icon theme to Plasma-Accent")

    p_uninstall = sub.add_parser("uninstall", parents=[common], help="remove the theme")
    p_uninstall.add_argument("--output", default=str(DEFAULT_INSTALL_DIR), help="theme directory")
    p_uninstall.add_argument("--force", action="store_true", help="remove even if it is active")
    p_uninstall.add_argument("--units", action="store_true", help="also remove the systemd timer and helper")

    p_units = sub.add_parser("install-units", parents=[common],
                             help="install the helper and the systemd timer")
    unit_group = p_units.add_mutually_exclusive_group()
    unit_group.add_argument("--enable", action="store_true", help="enable and start the timer")
    unit_group.add_argument("--disable", action="store_true", help="disable and stop the timer")

    p_clean = sub.add_parser("clean", parents=[common], help="remove caches")
    p_clean.add_argument("--all", action="store_true", help="remove the whole cache directory")

    args = parser.parse_args(argv)
    QUIET = bool(getattr(args, "quiet", False))
    VERBOSE = bool(getattr(args, "verbose", False))

    if getattr(args, "no_cache", False):
        shutil.rmtree(CACHE_DIR / "masks", ignore_errors=True)

    if args.command == "build":
        output = Path(args.output).expanduser()
        result = build_theme(args.base, output, args.jobs, resolve_color_style(args.color))
        print_build_summary(output, result)
        return 0

    if args.command == "install":
        output = Path(args.output).expanduser()
        install_theme(
            args.base,
            output,
            args.jobs,
            refresh=not args.no_refresh,
            activate=args.activate,
            style=resolve_color_style(args.color),
        )
        return 0

    if args.command == "sync":
        output = Path(args.output).expanduser()
        installed_mode = (load_manifest(output) or {}).get("color_mode")
        style = resolve_color_style(args.color or installed_mode or DEFAULT_COLOR_MODE)
        sync_theme(
            output,
            args.base,
            args.jobs,
            force=args.force,
            dry_run=args.dry_run,
            style=style,
        )
        return 0

    if args.command == "status":
        status_theme(Path(args.output).expanduser())
        return 0

    if args.command == "activate":
        activate_theme()
        return 0

    if args.command == "uninstall":
        uninstall_theme(Path(args.output).expanduser(), force=args.force, remove_units=args.units)
        return 0

    if args.command == "install-units":
        install_units(enable=args.enable, disable=args.disable)
        return 0

    if args.command == "clean":
        clean_cache(all_files=args.all)
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
    except SystemExit:
        raise
    except Exception as exc:
        if VERBOSE:
            raise
        die(f"unexpected error: {exc}")

