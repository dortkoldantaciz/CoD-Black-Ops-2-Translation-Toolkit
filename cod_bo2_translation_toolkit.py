#!/usr/bin/env python
"""
    CoD BO2 Translation Toolkit.

Extracts Call of Duty: Black Ops II text/font assets from the game files or a
prepared dump folder and packs translated assets into text.bin, font.bin, or
all.bin for the runtime loader.

The script is intentionally self-contained. It can be used as a GUI app or from
the command line. If OpenAssetTools Unlinker.exe is available, the toolkit can dump
the English fastfiles into the selected output folder and then export the text
and font assets from that dump.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Iterable


SHORT_APP_NAME = "CoD BO2 Translation Toolkit"
APP_NAME = "Call of Duty Black Ops 2 Translation Toolkit"
APP_VERSION = "v1.1"
FORMAT_VERSION = 1
BIN_MAGIC = b"BO2TRBIN"
BIN_VERSION = 1
RUNTIME_CONFIG_MAGIC = b"T6TRCFG1"
RUNTIME_CONFIG_LOG_ENABLED = 1
WORK_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
DEFAULT_OUTPUT_ROOT = WORK_DIR / "output"
LIB_DIR = WORK_DIR / "lib"
RESOURCE_DIR = WORK_DIR / "resources"
DLL_LIB_DIR = LIB_DIR / "dll"
OAT_LIB_DIR = LIB_DIR / "OpenAssetTools"
DEFAULT_EXPORT_NAME = "bo2_english.txt"
DEFAULT_MANIFEST_NAME = "bo2_english.manifest.json"
LEGACY_PACKAGE_NAME = "dll.bin"
TEXT_PACKAGE_NAME = "text.bin"
FONT_PACKAGE_NAME = "font.bin"
ALL_PACKAGE_NAME = "all.bin"
PROXY_DLL_NAME = "xinput1_3.dll"
TEXT_ROOT_NAME = "texts"
FONT_ROOT_NAME = "fonts"
FONT_ATLAS_ROOT_NAME = "atlases"
FONT_METRICS_ROOT_NAME = "metrics"
FONT_GLYPHS_ROOT_NAME = "glyphs"
FONT_INTERNAL_ROOT_NAME = "_tool_data_do_not_edit"
FONT_ISOLATED_PREVIEW_ROOT_NAME = "DONT_CHANGE_ITS_JUST_ISOLATED_IMAGES"
FONT_METRIC_RESOURCE_DIR = RESOURCE_DIR / "font_metrics"
MAIN_FONT_ATLAS_STEMS = {"gamefonts_pc_720", "devfonts", "distfont"}
EXPORT_FONT_ATLAS_STEMS = {"gamefonts_pc_720"}
OAT_DUMP_ROOT_NAME = "DO_NOT_CHANGE_OR_DELETE_THIS_FILE"
MAIN_FONT_PREFIX = "720_"
FONT_ATLAS_WIDTH = 512
FONT_ATLAS_HEIGHT = 1024
FONT_PACK_PADDING = 1
FONT_GLYPH_CANVAS_WIDTH = 128
FONT_GLYPH_CANVAS_HEIGHT = 128
FONT_GLYPH_ORIGIN_X = 32
FONT_GLYPH_BASELINE_Y = 96
LOCALIZED_ROOT_NAME = "localizedstrings_by_zone"
SUBTITLES_ROOT_NAME = "subtitles"
FONT_FILE_NAMES = {
    "gamefonts_pc_720.dds",
    "gamefonts_pc_720.png",
    "distfont.dds",
    "distfont.png",
    "devfonts.dds",
    "devfonts.png",
    "gamefonts_pc.json",
    "gamefonts_pc_glow.json",
}


REFERENCE_RE = re.compile(r"^\s*REFERENCE\s+(.+?)\s*$")
LANG_RE = re.compile(r"^\s*LANG_ENGLISH\s+(.+?)\s*$")


@dataclass
class TextRecord:
    record_type: str
    source_rel_path: str
    original: str
    reference: str = ""
    line_number: int = 0
    ordinal: int = 0
    row: int = 0
    column: int = 0
    text_file: str = ""
    line: int = 0


def read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def hide_windows_path(path: Path) -> None:
    if os.name != "nt" or not path.exists():
        return
    try:
        import ctypes

        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs != -1:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs | 0x2)
    except Exception:
        pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_name(text: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_")
    return value or "file"


def escape_line(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")


def unescape_line(text: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char != "\\" or index + 1 >= len(text):
            out.append(char)
            index += 1
            continue
        marker = text[index + 1]
        if marker == "n":
            out.append("\n")
        elif marker == "r":
            out.append("\r")
        elif marker == "t":
            out.append("\t")
        elif marker == "\\":
            out.append("\\")
        else:
            out.append("\\")
            out.append(marker)
        index += 2
    return "".join(out)


def parse_quoted_value(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        try:
            return ast.literal_eval(raw)
        except Exception:
            return raw[1:-1]
    return raw


def quote_str_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def parse_reference(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        return parse_quoted_value(raw)
    return raw.split()[0] if raw else ""


def find_game_zone_dir(game_dir: Path) -> Path:
    candidates = [
        game_dir / "zone" / "english",
        game_dir / "zone" / "all",
        game_dir / "zone",
    ]
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.glob("*.ff")):
            return candidate
    raise FileNotFoundError("Could not find BO2 fastfiles under the selected game folder.")


def normalize_source_root(path: Path) -> Path:
    path = path.resolve()
    if (path / TEXT_ROOT_NAME).is_dir() or (path / FONT_ROOT_NAME).is_dir():
        return path
    if (path / "clean_extract").is_dir():
        return path / "clean_extract"
    return path


def discover_default_source() -> Path | None:
    candidates = [
        WORK_DIR / "source",
        WORK_DIR / "dump",
        WORK_DIR / "clean_extract",
    ]
    for candidate in candidates:
        if (candidate / TEXT_ROOT_NAME).is_dir() or (candidate / FONT_ROOT_NAME).is_dir():
            return candidate
    return None


def discover_default_oat_unlinker() -> Path | None:
    candidates = [
        OAT_LIB_DIR / "Unlinker.exe",
        LIB_DIR / "Unlinker.exe",
        WORK_DIR / "Unlinker.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def text_root_for(source_root: Path) -> Path:
    return source_root / TEXT_ROOT_NAME if (source_root / TEXT_ROOT_NAME).is_dir() else source_root


def is_subtitle_csv(path: Path) -> bool:
    lower_parts = {part.lower() for part in path.parts}
    return path.suffix.lower() == ".csv" and (
        path.name.lower() == "subtitles.csv" or "subtitles" in lower_parts or "video" in lower_parts
    )


def text_asset_files(text_root: Path) -> list[Path]:
    files = [path for path in text_root.rglob("*.str") if path.is_file()]
    files.extend(path for path in text_root.rglob("*.csv") if path.is_file() and is_subtitle_csv(path))
    return sorted(set(files))


def source_rel_for_text_file(text_root: Path, path: Path, grouped_root_name: str | None = None) -> str:
    if grouped_root_name:
        grouped_root = text_root / grouped_root_name
        if grouped_root in path.parents or path == grouped_root:
            return f"{grouped_root_name}/{path.relative_to(grouped_root).as_posix()}"
    return path.relative_to(text_root).as_posix()


def collect_str_records(text_root: Path) -> list[TextRecord]:
    root = text_root / LOCALIZED_ROOT_NAME if (text_root / LOCALIZED_ROOT_NAME).is_dir() else text_root
    records: list[TextRecord] = []
    if not root.is_dir():
        return records

    for path in sorted(root.rglob("*.str")):
        rel_path = source_rel_for_text_file(text_root, path, LOCALIZED_ROOT_NAME)
        current_reference = ""
        ordinal = 0
        for line_number, line in enumerate(read_text(path).splitlines(), start=1):
            ref_match = REFERENCE_RE.match(line)
            if ref_match:
                current_reference = parse_reference(ref_match.group(1))
                continue
            lang_match = LANG_RE.match(line)
            if not lang_match:
                continue
            records.append(
                TextRecord(
                    record_type="str",
                    source_rel_path=rel_path,
                    reference=current_reference or f"line_{line_number}",
                    line_number=line_number,
                    ordinal=ordinal,
                    original=parse_quoted_value(lang_match.group(1)),
                )
            )
            ordinal += 1
    return records


def collect_csv_records(text_root: Path) -> list[TextRecord]:
    root = text_root / SUBTITLES_ROOT_NAME if (text_root / SUBTITLES_ROOT_NAME).is_dir() else text_root
    records: list[TextRecord] = []
    if not root.is_dir():
        return records

    for path in sorted(root.rglob("*.csv")):
        if not is_subtitle_csv(path):
            continue
        rel_path = source_rel_for_text_file(text_root, path, SUBTITLES_ROOT_NAME)
        rows = list(csv.reader(read_text(path).splitlines()))
        for row_index, row in enumerate(rows):
            if len(row) < 4:
                continue
            records.append(
                TextRecord(
                    record_type="csv",
                    source_rel_path=rel_path,
                    row=row_index,
                    column=3,
                    original=row[3],
                )
            )
    return records


def collect_text_records(source_root: Path) -> list[TextRecord]:
    text_root = text_root_for(source_root)
    records = collect_str_records(text_root)
    records.extend(collect_csv_records(text_root))
    return records


def collect_font_files(source_root: Path) -> list[Path]:
    font_root = source_root / FONT_ROOT_NAME
    if not font_root.is_dir():
        return collect_raw_dump_font_files(source_root)
    return [path for path in sorted(font_root.rglob("*")) if path.is_file()]


def collect_raw_dump_font_files(source_root: Path) -> list[Path]:
    font_files: list[Path] = []
    for path in source_root.rglob("*"):
        if not path.is_file():
            continue
        parts = {part.lower() for part in path.relative_to(source_root).parts}
        name = path.name.lower()
        if "fonts" in parts or "font" in parts or name in FONT_FILE_NAMES:
            if path.suffix.lower() in {".csv", ".json", ".dds", ".png"}:
                font_files.append(path)
    return sorted(set(font_files))


def copy_tree_contents(src: Path, dst: Path) -> int:
    count = 0
    if not src.is_dir():
        return count
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, out)
        count += 1
    return count


def export_original_text_tree(source_root: Path, output_root: Path) -> int:
    text_root = text_root_for(source_root)
    count = 0
    for path in text_asset_files(text_root):
        rel = path.relative_to(text_root)
        out = output_root / TEXT_ROOT_NAME / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, out)
        count += 1
    return count


def write_single_txt(records: list[TextRecord], output_root: Path, text_name: str) -> dict:
    text_rel = text_name
    lines: list[str] = []
    manifest_records: list[dict] = []
    for index, record in enumerate(records):
        record.text_file = text_rel
        record.line = index
        lines.append(escape_line(record.original))
        manifest_records.append(record_to_manifest(record))
    write_text(output_root / text_rel, "\n".join(lines) + ("\n" if lines else ""))
    return {"text_files": [text_rel], "records": manifest_records}


def write_directory_txt(records: list[TextRecord], output_root: Path) -> dict:
    grouped: dict[str, list[TextRecord]] = {}
    for record in records:
        grouped.setdefault(record.source_rel_path, []).append(record)

    text_files: list[str] = []
    manifest_records: list[dict] = []
    for source_rel, group in sorted(grouped.items()):
        text_rel = f"{Path(source_rel).with_suffix('').as_posix()}.txt"
        text_rel = f"{TEXT_ROOT_NAME}/{text_rel}"
        text_files.append(text_rel)
        lines = []
        for index, record in enumerate(group):
            record.text_file = text_rel
            record.line = index
            lines.append(escape_line(record.original))
            manifest_records.append(record_to_manifest(record))
        write_text(output_root / text_rel, "\n".join(lines) + ("\n" if lines else ""))
    return {"text_files": text_files, "records": manifest_records}


def record_to_manifest(record: TextRecord) -> dict:
    item = {
        "line": record.line,
        "text_file": record.text_file,
        "type": record.record_type,
        "source_rel_path": record.source_rel_path,
        "original": record.original,
    }
    if record.record_type == "str":
        item.update(
            {
                "reference": record.reference,
                "line_number": record.line_number,
                "ordinal": record.ordinal,
            }
        )
    else:
        item.update({"row": record.row, "column": record.column})
    return item


def write_manifest(path: Path, data: dict) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def set_windows_hidden(path: Path) -> None:
    if os.name != "nt" or not path.exists():
        return
    try:
        subprocess.run(["attrib", "+h", str(path)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def export_texts(
    source_root: Path,
    output_root: Path,
    layout: str,
    directory_format: str,
    text_name: str = DEFAULT_EXPORT_NAME,
) -> dict:
    records = collect_text_records(source_root)
    output_root.mkdir(parents=True, exist_ok=True)

    if layout == "single":
        export_data = write_single_txt(records, output_root, text_name)
        text_format = "txt"
    elif directory_format == "txt":
        export_data = write_directory_txt(records, output_root)
        text_format = "txt"
    else:
        copied = export_original_text_tree(source_root, output_root)
        export_data = {"text_files": [], "records": [record_to_manifest(record) for record in records]}
        text_format = "original"
        export_data["copied_files"] = copied

    manifest = {
        "tool": APP_NAME,
        "format_version": FORMAT_VERSION,
        "asset": "text",
        "layout": layout,
        "text_format": text_format,
        "total_records": len(records),
        "text_files": export_data["text_files"],
        "records": export_data["records"],
    }
    if "copied_files" in export_data:
        manifest["copied_files"] = export_data["copied_files"]
    write_manifest(output_root / manifest_name_for_text(text_name), manifest)
    return manifest


def manifest_name_for_text(text_name: str) -> str:
    stem = Path(text_name).stem or "bo2_text"
    return f"{stem}.manifest.json"


def locate_prepared_font_root(source_root: Path) -> Path | None:
    candidates = [
        source_root / FONT_ROOT_NAME,
        source_root,
        WORK_DIR / "clean_extract" / FONT_ROOT_NAME,
    ]
    for candidate in candidates:
        if (
            (candidate / FONT_ATLAS_ROOT_NAME).is_dir()
            or (candidate / FONT_METRICS_ROOT_NAME).is_dir()
            or (candidate / FONT_INTERNAL_ROOT_NAME / FONT_METRICS_ROOT_NAME).is_dir()
        ):
            return candidate
    return None


def save_editable_font_png(source_path: Path, png_path: Path) -> None:
    try:
        from PIL import Image
    except Exception as exc:
        raise RuntimeError("Pillow is required to export editable PNG font atlases.") from exc

    image = Image.open(source_path).convert("RGBA")
    editable = bo2_font_atlas_to_editable_rgba(image)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    editable.save(png_path)


def bo2_font_atlas_to_editable_rgba(image):
    """Convert BO2 font textures to transparent white glyph PNGs for editing."""
    try:
        from PIL import Image, ImageChops
    except Exception as exc:
        raise RuntimeError("Pillow is required to export editable PNG font atlases.") from exc

    image = image.convert("RGBA")
    alpha = image.getchannel("A")
    if alpha.getextrema() == (255, 255):
        r, g, b, _a = image.split()
        mask = ImageChops.lighter(ImageChops.lighter(r, g), b)
    else:
        mask = alpha
    editable = Image.new("RGBA", image.size, (255, 255, 255, 0))
    editable.putalpha(mask)
    return editable


def load_font_atlas_image(font_source_root: Path, atlas_stem: str = "gamefonts_pc_720"):
    try:
        from PIL import Image
    except Exception as exc:
        raise RuntimeError("Pillow is required to export editable font glyphs.") from exc

    atlas_root = font_source_root / FONT_ATLAS_ROOT_NAME
    dds_path = atlas_root / f"{atlas_stem}.dds"
    png_path = atlas_root / f"{atlas_stem}.png"
    source = dds_path if dds_path.is_file() else png_path
    if not source.is_file():
        raise FileNotFoundError(f"Could not find {atlas_stem}.dds or {atlas_stem}.png.")

    image = Image.open(source).convert("RGBA")
    return bo2_font_atlas_to_editable_rgba(image)


def font_metric_metadata(meta_lines: list[str]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in meta_lines:
        parts = next(csv.reader([line]))
        if len(parts) >= 3 and parts[0] == "#meta":
            metadata[parts[1]] = parts[2]
    return metadata


def font_atlas_stem(meta_lines: list[str]) -> str:
    material = font_metric_metadata(meta_lines).get("material", "").lower()
    if "devfonts" in material:
        return "devfonts"
    if "distfont" in material:
        return "distfont"
    return "gamefonts_pc_720"


def parse_font_metric_csv(path: Path) -> tuple[list[str], str, list[dict[str, str]]]:
    meta_lines: list[str] = []
    header = ""
    rows: list[dict[str, str]] = []
    for line in read_text(path).splitlines():
        if line.startswith("#meta"):
            meta_lines.append(line)
            continue
        if line.startswith("#glyphIndex"):
            header = line
            continue
        if not line.strip() or line.startswith(",,,"):
            continue
        if not header:
            continue
        columns = [part[1:] if part.startswith("#") else part for part in header.split(",")]
        values = next(csv.reader([line]))
        if len(values) < len(columns):
            values.extend([""] * (len(columns) - len(values)))
        rows.append(dict(zip(columns, values)))
    if not header:
        raise ValueError(f"Missing glyph header in {path}.")
    return meta_lines, header, rows


def write_font_metric_csv(path: Path, meta_lines: list[str], header: str, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [part[1:] if part.startswith("#") else part for part in header.split(",")]
    lines = list(meta_lines)
    lines.append(",,,")
    lines.append(header)
    for row in rows:
        buffer = []
        for column in columns:
            buffer.append(str(row.get(column, "")))
        # Metric rows are simple enough that csv.writer's quoting is sufficient.
        import io

        stream = io.StringIO()
        writer = csv.writer(stream, lineterminator="")
        writer.writerow(buffer)
        lines.append(stream.getvalue())
    write_text(path, "\n".join(lines) + "\n")


def glyph_file_name(row: dict[str, str]) -> str:
    index = int(row.get("glyphIndex") or 0)
    code_hex = (row.get("hex") or "0000").upper().zfill(4)
    return f"{index:03d}_{code_hex}.png"


def paste_glyph_on_edit_canvas(glyph, x0: int, y0: int):
    from PIL import Image

    canvas = Image.new("RGBA", (FONT_GLYPH_CANVAS_WIDTH, FONT_GLYPH_CANVAS_HEIGHT), (255, 255, 255, 0))
    x = FONT_GLYPH_ORIGIN_X + x0
    y = FONT_GLYPH_BASELINE_Y + y0
    if x < 0 or y < 0 or x + glyph.size[0] > FONT_GLYPH_CANVAS_WIDTH or y + glyph.size[1] > FONT_GLYPH_CANVAS_HEIGHT:
        raise ValueError(f"Glyph does not fit the {FONT_GLYPH_CANVAS_WIDTH}x{FONT_GLYPH_CANVAS_HEIGHT} edit canvas at x0={x0}, y0={y0}.")
    canvas.alpha_composite(glyph, (x, y))
    return canvas


def export_glyph_font_files(font_source_root: Path, out_root: Path) -> int:
    try:
        from PIL import Image
    except Exception as exc:
        raise RuntimeError("Pillow is required to export editable font glyphs.") from exc

    metrics_source = font_source_root / FONT_METRICS_ROOT_NAME / "fonts"
    if not metrics_source.is_dir():
        raise FileNotFoundError("Could not find font metric CSV files.")

    atlases = {stem: load_font_atlas_image(font_source_root, stem) for stem in MAIN_FONT_ATLAS_STEMS}

    copied = 0
    manifest: dict[str, object] = {
        "tool": APP_NAME,
        "format_version": FORMAT_VERSION,
        "atlases": {stem: {"width": image.width, "height": image.height} for stem, image in atlases.items()},
        "padding": FONT_PACK_PADDING,
        "edit_canvas_width": FONT_GLYPH_CANVAS_WIDTH,
        "edit_canvas_height": FONT_GLYPH_CANVAS_HEIGHT,
        "edit_canvas_layout": "origin_baseline_v1",
        "edit_canvas_origin_x": FONT_GLYPH_ORIGIN_X,
        "edit_canvas_baseline_y": FONT_GLYPH_BASELINE_Y,
        "fonts": [],
    }
    manifest_fonts: list[dict[str, object]] = []

    for metric_path in sorted(metrics_source.rglob("*.csv")):
        font_name = metric_path.stem
        meta_lines, header, rows = parse_font_metric_csv(metric_path)
        atlas_stem = font_atlas_stem(meta_lines)
        atlas = atlases[atlas_stem]
        atlas_width, atlas_height = atlas.size
        metric_rel = metric_path.relative_to(font_source_root / FONT_METRICS_ROOT_NAME)
        metric_out = out_root / FONT_INTERNAL_ROOT_NAME / FONT_METRICS_ROOT_NAME / metric_rel
        metric_out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(metric_path, metric_out)
        copied += 1

        glyph_label = f"720_{font_name}" if atlas_stem == "gamefonts_pc_720" else font_name
        glyph_dir = out_root / FONT_GLYPHS_ROOT_NAME / glyph_label
        glyph_dir.mkdir(parents=True, exist_ok=True)
        glyph_entries: list[dict[str, object]] = []
        for row in rows:
            file_name = glyph_file_name(row)
            glyph_path = glyph_dir / file_name
            width = int(float(row.get("pixelWidth") or 0))
            height = int(float(row.get("pixelHeight") or 0))
            x0 = int(float(row.get("x0") or 0))
            y0 = int(float(row.get("y0") or 0))
            original_canvas_bbox = None
            if width <= 0 or height <= 0:
                Image.new("RGBA", (FONT_GLYPH_CANVAS_WIDTH, FONT_GLYPH_CANVAS_HEIGHT), (255, 255, 255, 0)).save(glyph_path)
            else:
                s0 = float(row.get("s0") or 0)
                t0 = float(row.get("t0") or 0)
                s1 = float(row.get("s1") or 0)
                t1 = float(row.get("t1") or 0)
                left = max(min(round(s0 * atlas_width), atlas_width), 0)
                top = max(min(round(t0 * atlas_height), atlas_height), 0)
                right = max(min(round(s1 * atlas_width), atlas_width), left + 1)
                bottom = max(min(round(t1 * atlas_height), atlas_height), top + 1)
                glyph = atlas.crop((left, top, right, bottom))
                edit_canvas = paste_glyph_on_edit_canvas(glyph, x0, y0)
                edit_canvas.save(glyph_path)
                bbox = edit_canvas.getchannel("A").getbbox()
                if bbox:
                    original_canvas_bbox = list(bbox)
            copied += 1
            glyph_entries.append(
                {
                    "file": glyph_path.relative_to(out_root).as_posix(),
                    "glyphIndex": row.get("glyphIndex", ""),
                    "hex": row.get("hex", ""),
                    "letter": row.get("letter", ""),
                    "original_pixel_width": width,
                    "original_pixel_height": height,
                    "original_canvas_bbox": original_canvas_bbox,
                    "original_sha256": sha256_file(glyph_path),
                }
            )

        manifest_fonts.append(
            {
                "font": font_name,
                "atlas": atlas_stem,
                "metrics": metric_out.relative_to(out_root).as_posix(),
                "glyph_dir": glyph_dir.relative_to(out_root).as_posix(),
                "glyphs": glyph_entries,
            }
        )

    manifest["fonts"] = manifest_fonts
    internal_root = out_root / FONT_INTERNAL_ROOT_NAME
    write_manifest(internal_root / "glyph_manifest.json", manifest)
    set_windows_hidden(internal_root)
    return copied + 1


def trim_glyph_image(image):
    from PIL import Image

    alpha = image.getchannel("A")
    bbox = alpha.getbbox()
    if not bbox:
        return Image.new("RGBA", (1, 1), (255, 255, 255, 0)), True
    cropped = image.crop(bbox)
    visible = Image.new("RGBA", cropped.size, (255, 255, 255, 0))
    visible.putalpha(cropped.getchannel("A"))
    return visible, False


def valid_bbox(value) -> list[int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            left, top, right, bottom = [int(part) for part in value]
        except Exception:
            return None
        if right > left and bottom > top:
            return [left, top, right, bottom]
    return None


def fallback_original_glyph_bbox(canvas_width: int, canvas_height: int, original_width: int, original_height: int) -> list[int]:
    left = max((canvas_width - original_width) // 2, 0)
    top = max((canvas_height - original_height) // 2, 0)
    return [left, top, left + max(original_width, 1), top + max(original_height, 1)]


def load_full_metric_codepoints(fonts_root: Path) -> set[int]:
    path = fonts_root / FONT_INTERNAL_ROOT_NAME / "full_metric_codepoints.txt"
    if not path.is_file():
        return set()
    values: set[int] = set()
    for line in read_text(path).splitlines():
        value = line.split("#", 1)[0].strip()
        if not value:
            continue
        try:
            values.add(int(value, 16))
        except ValueError:
            values.add(int(value))
    return values


def load_full_metric_base_codepoints(fonts_root: Path) -> dict[int, int]:
    path = fonts_root / FONT_INTERNAL_ROOT_NAME / "full_metric_base_codepoints.txt"
    if not path.is_file():
        return {}
    values: dict[int, int] = {}
    for line in read_text(path).splitlines():
        value = line.split("#", 1)[0].strip()
        if not value or "=" not in value:
            continue
        target, source = [part.strip() for part in value.split("=", 1)]
        try:
            target_codepoint = int(target, 16)
        except ValueError:
            target_codepoint = int(target)
        try:
            source_codepoint = int(source, 16)
        except ValueError:
            source_codepoint = int(source)
        values[target_codepoint] = source_codepoint
    return values


def load_full_metric_y_offsets(fonts_root: Path) -> dict[int, int]:
    path = fonts_root / FONT_INTERNAL_ROOT_NAME / "full_metric_y_offsets.txt"
    if not path.is_file():
        return {}
    values: dict[int, int] = {}
    for line in read_text(path).splitlines():
        value = line.split("#", 1)[0].strip()
        if not value or "=" not in value:
            continue
        target, offset = [part.strip() for part in value.split("=", 1)]
        try:
            target_codepoint = int(target, 16)
        except ValueError:
            target_codepoint = int(target)
        values[target_codepoint] = int(offset)
    return values


def load_full_metric_dx_offsets(fonts_root: Path) -> dict[int, int]:
    path = fonts_root / FONT_INTERNAL_ROOT_NAME / "full_metric_dx_offsets.txt"
    if not path.is_file():
        return {}
    values: dict[int, int] = {}
    for line in read_text(path).splitlines():
        value = line.split("#", 1)[0].strip()
        if not value or "=" not in value:
            continue
        target, offset = [part.strip() for part in value.split("=", 1)]
        try:
            target_codepoint = int(target, 16)
        except ValueError:
            target_codepoint = int(target)
        values[target_codepoint] = int(offset)
    return values


def load_full_metric_y_alignments(fonts_root: Path) -> dict[int, str]:
    path = fonts_root / FONT_INTERNAL_ROOT_NAME / "full_metric_y_alignments.txt"
    if not path.is_file():
        return {}
    values: dict[int, str] = {}
    for line in read_text(path).splitlines():
        value = line.split("#", 1)[0].strip()
        if not value or "=" not in value:
            continue
        target, alignment = [part.strip() for part in value.split("=", 1)]
        try:
            target_codepoint = int(target, 16)
        except ValueError:
            target_codepoint = int(target)
        values[target_codepoint] = alignment.lower()
    return values


def int_metric(row: dict[str, str], key: str, default: int = 0) -> int:
    try:
        return int(float(row.get(key) or default))
    except Exception:
        return default


def infer_canvas_metrics(rows: list[dict[str, str]], glyph_dir: Path, fallback_origin_x: int, fallback_baseline_y: int) -> tuple[int, int, int]:
    samples_x: list[int] = []
    samples_y: list[int] = []
    samples_right: list[int] = []
    for row in rows:
        letter = int_metric(row, "letter", -1)
        if not (33 <= letter <= 126):
            continue
        glyph_path = glyph_dir / glyph_file_name(row)
        if not glyph_path.is_file():
            continue
        try:
            from PIL import Image

            bbox = Image.open(glyph_path).convert("RGBA").getchannel("A").getbbox()
        except Exception:
            bbox = None
        if not bbox:
            continue
        x0 = int_metric(row, "x0")
        y0 = int_metric(row, "y0")
        width = int_metric(row, "pixelWidth", bbox[2] - bbox[0])
        dx = int_metric(row, "dx", x0 + width + 1)
        samples_x.append(int(bbox[0]) - x0)
        samples_y.append(int(bbox[1]) - y0)
        samples_right.append(dx - x0 - width)

    if not samples_x or not samples_y:
        return fallback_origin_x, fallback_baseline_y, 1

    import statistics

    origin_x = int(round(statistics.median(samples_x)))
    baseline_y = int(round(statistics.median(samples_y)))
    right_bearing = int(round(statistics.median(samples_right))) if samples_right else 1
    return origin_x, baseline_y, max(0, min(16, right_bearing))


def pack_glyph_images(glyph_items: list[dict[str, object]], atlas_width: int, atlas_height: int) -> tuple[dict[int, tuple[int, int, int, int]], object]:
    try:
        from PIL import Image
    except Exception as exc:
        raise RuntimeError("Pillow is required to pack edited font glyphs.") from exc

    total_area = sum(int(item["width"]) * int(item["height"]) for item in glyph_items)
    capacity = atlas_width * atlas_height
    if total_area > capacity:
        over = total_area - capacity
        raise ValueError(
            f"Total font atlas pixel budget was exceeded by {over} pixels. Delete unused glyph PNG files from any font folder and try again. Deleted glyphs will be packed as empty 1x1 characters."
        )

    atlas = Image.new("RGBA", (atlas_width, atlas_height), (255, 255, 255, 0))
    placements: dict[int, tuple[int, int, int, int]] = {}
    x = 0
    y = 0
    row_height = 0
    for item in sorted(glyph_items, key=lambda entry: (int(entry["height"]), int(entry["width"])), reverse=True):
        width = int(item["width"])
        height = int(item["height"])
        if x + width > atlas_width:
            x = 0
            y += row_height + FONT_PACK_PADDING
            row_height = 0
        if y + height > atlas_height:
            used = sum((right - left) * (bottom - top) for left, top, right, bottom in placements.values())
            remaining = sum(
                int(other["width"]) * int(other["height"])
                for other in glyph_items
                if int(other["id"]) not in placements
            )
            over = max((used + remaining) - capacity, 1)
            raise ValueError(
                f"Total font atlas pixel budget was exceeded by {over} pixels. Delete unused glyph PNG files from any font folder and try again. Deleted glyphs will be packed as empty 1x1 characters."
            )
        atlas.alpha_composite(item["image"], (x, y))
        placements[int(item["id"])] = (x, y, x + width, y + height)
        x += width + FONT_PACK_PADDING
        row_height = max(row_height, height)
    return placements, atlas


def rebuild_fonts_from_glyphs(input_root: Path, build_root: Path) -> list[dict]:
    try:
        from PIL import Image
    except Exception as exc:
        raise RuntimeError("Pillow is required to pack edited font glyphs.") from exc

    fonts_root = input_root / FONT_ROOT_NAME
    glyph_root = fonts_root / FONT_GLYPHS_ROOT_NAME
    manifest_path = fonts_root / FONT_INTERNAL_ROOT_NAME / "glyph_manifest.json"
    if not manifest_path.is_file():
        manifest_path = glyph_root / "glyph_manifest.json"
    if not manifest_path.is_file():
        return []

    manifest = json.loads(read_text(manifest_path))
    canvas_width = int(manifest.get("edit_canvas_width") or FONT_GLYPH_CANVAS_WIDTH)
    canvas_height = int(manifest.get("edit_canvas_height") or FONT_GLYPH_CANVAS_HEIGHT)
    canvas_layout = str(manifest.get("edit_canvas_layout") or "legacy_centered")
    canvas_origin_x = int(manifest.get("edit_canvas_origin_x") or FONT_GLYPH_ORIGIN_X)
    canvas_baseline_y = int(manifest.get("edit_canvas_baseline_y") or FONT_GLYPH_BASELINE_Y)
    atlas_specs = manifest.get("atlases") or {
        str(manifest.get("atlas") or "gamefonts_pc_720"): {
            "width": int(manifest.get("atlas_width") or FONT_ATLAS_WIDTH),
            "height": int(manifest.get("atlas_height") or FONT_ATLAS_HEIGHT),
        }
    }
    manifest_mtime = manifest_path.stat().st_mtime
    full_metric_codepoints = load_full_metric_codepoints(fonts_root)
    full_metric_base_codepoints = load_full_metric_base_codepoints(fonts_root)
    full_metric_y_offsets = load_full_metric_y_offsets(fonts_root)
    full_metric_dx_offsets = load_full_metric_dx_offsets(fonts_root)
    full_metric_y_alignments = load_full_metric_y_alignments(fonts_root)
    glyph_entries_by_font = {
        str(font_entry.get("font", "")): {
            int(glyph.get("glyphIndex") or index): glyph
            for index, glyph in enumerate(font_entry.get("glyphs", []))
        }
        for font_entry in manifest.get("fonts", [])
    }
    atlas_images: dict[str, object] = {}
    packed_files: list[dict] = []

    def atlas_size(stem: str) -> tuple[int, int]:
        spec = atlas_specs.get(stem, {})
        return int(spec.get("width") or FONT_ATLAS_WIDTH), int(spec.get("height") or FONT_ATLAS_HEIGHT)

    occupied_slots_by_atlas: dict[str, list[tuple[int, int, int, int]]] = {}
    for font_entry_for_slots in manifest.get("fonts", []):
        slot_metric_rel = font_entry_for_slots["metrics"]
        slot_metric_path = fonts_root / slot_metric_rel
        slot_metric_rel_posix = Path(slot_metric_rel).as_posix()
        if not slot_metric_path.is_file() and slot_metric_rel_posix.startswith(f"{FONT_METRICS_ROOT_NAME}/"):
            slot_metric_path = fonts_root / FONT_INTERNAL_ROOT_NAME / slot_metric_rel
        slot_meta_lines, _slot_header, slot_rows = parse_font_metric_csv(slot_metric_path)
        slot_atlas_stem = str(font_entry_for_slots.get("atlas") or font_atlas_stem(slot_meta_lines))
        slot_atlas_width, slot_atlas_height = atlas_size(slot_atlas_stem)
        occupied = occupied_slots_by_atlas.setdefault(slot_atlas_stem, [])
        for slot_row in slot_rows:
            slot_width = int_metric(slot_row, "pixelWidth")
            slot_height = int_metric(slot_row, "pixelHeight")
            if slot_width <= 0 or slot_height <= 0:
                continue
            slot_left = max(min(round(float(slot_row.get("s0") or 0) * slot_atlas_width), slot_atlas_width), 0)
            slot_top = max(min(round(float(slot_row.get("t0") or 0) * slot_atlas_height), slot_atlas_height), 0)
            occupied.append((slot_left - 1, slot_top - 1, slot_left + slot_width + 1, slot_top + slot_height + 1))

    def allocate_generated_slot(atlas_stem: str, width: int, height: int) -> tuple[int, int]:
        atlas_width, atlas_height = atlas_size(atlas_stem)
        occupied = occupied_slots_by_atlas.setdefault(atlas_stem, [])
        padded_width = max(width, 1) + 2
        padded_height = max(height, 1) + 2
        for y in range(1, max(atlas_height - padded_height, 1)):
            for x in range(1, max(atlas_width - padded_width, 1)):
                candidate = (x - 1, y - 1, x + padded_width, y + padded_height)
                if all(
                    candidate[2] <= rect[0]
                    or candidate[0] >= rect[2]
                    or candidate[3] <= rect[1]
                    or candidate[1] >= rect[3]
                    for rect in occupied
                ):
                    occupied.append(candidate)
                    return x, y
        raise RuntimeError(f"Could not find free atlas space for generated glyph in {atlas_stem}.")

    for font_entry in manifest.get("fonts", []):
        metric_rel = font_entry["metrics"]
        metric_path = fonts_root / metric_rel
        metric_rel_posix = Path(metric_rel).as_posix()
        if not metric_path.is_file() and metric_rel_posix.startswith(f"{FONT_METRICS_ROOT_NAME}/"):
            metric_path = fonts_root / FONT_INTERNAL_ROOT_NAME / metric_rel
        meta_lines, header, rows = parse_font_metric_csv(metric_path)
        atlas_stem = str(font_entry.get("atlas") or font_atlas_stem(meta_lines))
        atlas_width, atlas_height = atlas_size(atlas_stem)
        atlas = atlas_images.setdefault(atlas_stem, Image.new("RGBA", (atlas_width, atlas_height), (255, 255, 255, 0)))
        glyph_dir_rel = str(font_entry.get("glyph_dir") or f"{FONT_GLYPHS_ROOT_NAME}/720_{font_entry['font']}")
        glyph_dir_path = fonts_root / glyph_dir_rel
        inferred_origin_x, inferred_baseline_y, inferred_right_bearing = infer_canvas_metrics(
            rows,
            glyph_dir_path,
            canvas_origin_x,
            canvas_baseline_y,
        )
        rows_by_letter = {int_metric(metric_row, "letter", -1): metric_row for metric_row in rows}
        if full_metric_base_codepoints:
            next_glyph_index = max((int_metric(metric_row, "glyphIndex", -1) for metric_row in rows), default=-1) + 1
            for target_codepoint, source_codepoint in sorted(full_metric_base_codepoints.items()):
                if target_codepoint in rows_by_letter or source_codepoint not in rows_by_letter:
                    continue
                base_row = dict(rows_by_letter[source_codepoint])
                base_row["glyphIndex"] = str(next_glyph_index)
                base_row["letter"] = str(target_codepoint)
                base_row["hex"] = f"{target_codepoint:04X}"
                base_row["_generated_from_letter"] = str(source_codepoint)
                rows.append(base_row)
                rows_by_letter[target_codepoint] = base_row
                next_glyph_index += 1

        for row_index, row in enumerate(rows):
            glyph_path = glyph_dir_path / glyph_file_name(row)
            generated_source_row = rows_by_letter.get(int_metric(row, "_generated_from_letter", -1))
            image_glyph_path = glyph_path
            if not image_glyph_path.is_file() and generated_source_row:
                image_glyph_path = glyph_dir_path / glyph_file_name(generated_source_row)
            glyph_index = int(row.get("glyphIndex") or row_index)
            glyph_meta = glyph_entries_by_font.get(str(font_entry.get("font", "")), {}).get(glyph_index, {})
            old_x0 = int_metric(row, "x0")
            old_y0 = int_metric(row, "y0")
            old_dx = int_metric(row, "dx")
            if not image_glyph_path.is_file():
                canvas_image = None
                visible_bbox = None
                deleted = True
            else:
                canvas_image = Image.open(image_glyph_path).convert("RGBA")
                visible_bbox = canvas_image.getchannel("A").getbbox()
                deleted = visible_bbox is None

            original_width = int(glyph_meta.get("original_pixel_width") or row.get("pixelWidth") or 0)
            original_height = int(glyph_meta.get("original_pixel_height") or row.get("pixelHeight") or 0)
            original_bbox = valid_bbox(glyph_meta.get("original_canvas_bbox"))
            if original_bbox is None:
                original_bbox = fallback_original_glyph_bbox(canvas_width, canvas_height, original_width, original_height)
            original_sha256 = str(glyph_meta.get("original_sha256") or "")
            if not glyph_path.is_file() and not generated_source_row:
                edited = False
            elif full_metric_codepoints:
                edited = int_metric(row, "letter", -1) in full_metric_codepoints
            elif original_sha256:
                edited = sha256_file(glyph_path) != original_sha256
            else:
                # Legacy exports have no hashes. Their edited files are newer than
                # the hidden manifest; untouched glyph metrics must remain exact.
                edited = glyph_path.stat().st_mtime > manifest_mtime + 1.0

            original_left = max(min(round(float(row.get("s0") or 0) * atlas_width), atlas_width), 0)
            original_top = max(min(round(float(row.get("t0") or 0) * atlas_height), atlas_height), 0)

            if deleted:
                image = Image.new("RGBA", (1, 1), (255, 255, 255, 0))
                if edited:
                    row["pixelWidth"] = "1"
                    row["pixelHeight"] = "1"
                    row["x0"] = "0"
                    row["y0"] = "0"
                    row["dx"] = "1"
                    row["s0"] = f"{original_left / atlas_width:.6f}"
                    row["t0"] = f"{original_top / atlas_height:.6f}"
                    row["s1"] = f"{min(original_left + 1, atlas_width) / atlas_width:.6f}"
                    row["t1"] = f"{min(original_top + 1, atlas_height) / atlas_height:.6f}"
            elif edited:
                image, _deleted = trim_glyph_image(canvas_image)
                metric_width = max(image.size[0], 1)
                metric_height = max(image.size[1], 1)
                base_row = rows_by_letter.get(full_metric_base_codepoints.get(int_metric(row, "letter", -1), -1))
                if base_row:
                    base_x0 = int_metric(base_row, "x0")
                    base_y0 = int_metric(base_row, "y0")
                    base_dx = int_metric(base_row, "dx")
                    base_bbox = None
                    base_glyph_path = glyph_dir_path / glyph_file_name(base_row)
                    if base_glyph_path.is_file():
                        try:
                            base_bbox = Image.open(base_glyph_path).convert("RGBA").getchannel("A").getbbox()
                        except Exception:
                            base_bbox = None
                    new_x0 = base_x0
                    if full_metric_y_alignments.get(int_metric(row, "letter", -1)) == "bottom":
                        new_y0 = base_y0 + int_metric(base_row, "pixelHeight", metric_height) - metric_height
                    elif base_bbox:
                        new_y0 = base_y0 + (int(visible_bbox[1]) - int(base_bbox[1]))
                    else:
                        new_y0 = int(visible_bbox[1]) - inferred_baseline_y
                    new_dx = base_dx
                else:
                    new_x0 = int(visible_bbox[0]) - inferred_origin_x
                    new_y0 = int(visible_bbox[1]) - inferred_baseline_y
                    new_dx = new_x0 + metric_width + inferred_right_bearing
                if generated_source_row:
                    original_left, original_top = allocate_generated_slot(atlas_stem, metric_width, metric_height)
                new_dx += full_metric_dx_offsets.get(int_metric(row, "letter", -1), 0)
                new_dx = max(1, min(127, new_dx))
                row["pixelWidth"] = str(max(1, min(127, metric_width)))
                row["pixelHeight"] = str(max(1, min(127, metric_height)))
                row["x0"] = str(max(-128, min(127, new_x0)))
                new_y0 += full_metric_y_offsets.get(int_metric(row, "letter", -1), 0)
                row["y0"] = str(max(-128, min(127, new_y0)))
                row["dx"] = str(new_dx)
                row["s0"] = f"{original_left / atlas_width:.6f}"
                row["t0"] = f"{original_top / atlas_height:.6f}"
                row["s1"] = f"{min(original_left + metric_width, atlas_width) / atlas_width:.6f}"
                row["t1"] = f"{min(original_top + metric_height, atlas_height) / atlas_height:.6f}"
            else:
                # Preserve the original atlas slot and glyph dimensions. Only the
                # explicitly listed full-metric glyphs are allowed to change
                # metrics; untouched glyph metrics remain byte-for-byte stable.
                if canvas_layout == "origin_baseline_v1":
                    source_x = inferred_origin_x + old_x0
                    source_y = inferred_baseline_y + old_y0
                    crop_box = (source_x, source_y, source_x + original_width, source_y + original_height)
                else:
                    crop_box = tuple(original_bbox)
                image = canvas_image.crop(crop_box)

            if not deleted:
                paste_width = min(image.size[0], max(atlas_width - original_left, 0))
                paste_height = min(image.size[1], max(atlas_height - original_top, 0))
                if paste_width > 0 and paste_height > 0:
                    atlas.alpha_composite(image.crop((0, 0, paste_width, paste_height)), (original_left, original_top))

        metric_out = build_root / FONT_ROOT_NAME / metric_rel
        metric_rel_posix = Path(metric_rel).as_posix()
        if metric_rel_posix.startswith(f"{FONT_INTERNAL_ROOT_NAME}/"):
            metric_out = build_root / FONT_ROOT_NAME / Path(metric_rel_posix[len(FONT_INTERNAL_ROOT_NAME) + 1 :])
        write_font_metric_csv(metric_out, meta_lines, header, rows)
        packed_files.append({"path": metric_out.relative_to(build_root).as_posix(), "source": metric_out, "size": metric_out.stat().st_size, "sha256": sha256_file(metric_out)})

    for atlas_stem, atlas in atlas_images.items():
        atlas_path = build_root / FONT_ROOT_NAME / FONT_ATLAS_ROOT_NAME / f"{atlas_stem}.png"
        atlas_path.parent.mkdir(parents=True, exist_ok=True)
        atlas.save(atlas_path)
        packed_files.append({"path": atlas_path.relative_to(build_root).as_posix(), "source": atlas_path, "size": atlas_path.stat().st_size, "sha256": sha256_file(atlas_path)})

    return packed_files


def copy_prepared_fonts(font_source_root: Path, out_root: Path) -> int:
    copied = 0
    atlas_source = font_source_root / FONT_ATLAS_ROOT_NAME
    if atlas_source.is_dir():
        for path in sorted(atlas_source.rglob("*")):
            if not path.is_file() or path.suffix.lower() != ".png":
                continue
            if path.stem not in EXPORT_FONT_ATLAS_STEMS:
                continue
            rel = path.relative_to(atlas_source)
            out = out_root / FONT_ATLAS_ROOT_NAME / rel
            dds_source = path.with_suffix(".dds")
            if dds_source.is_file():
                save_editable_font_png(dds_source, out)
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, out)
            copied += 1

    for metrics_source in (
        font_source_root / FONT_METRICS_ROOT_NAME,
        font_source_root / FONT_INTERNAL_ROOT_NAME / FONT_METRICS_ROOT_NAME,
    ):
        copied += copy_720_font_metrics(metrics_source, out_root)

    return copied


def copy_720_font_metrics(metrics_source: Path, out_root: Path) -> int:
    copied = 0
    if metrics_source.is_dir():
        for path in sorted(metrics_source.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(metrics_source)
            if not rel.as_posix().startswith("fonts/"):
                continue
            try:
                meta_lines, _header, _rows = parse_font_metric_csv(path)
            except Exception:
                continue
            if font_atlas_stem(meta_lines) not in EXPORT_FONT_ATLAS_STEMS:
                continue
            out = out_root / FONT_METRICS_ROOT_NAME / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, out)
            copied += 1

    return copied


def bundled_font_metric_roots() -> list[Path]:
    roots = [FONT_METRIC_RESOURCE_DIR]
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        roots.append(Path(sys._MEIPASS) / "resources" / "font_metrics")
    seen: set[Path] = set()
    available: list[Path] = []
    for root in roots:
        try:
            resolved = root.resolve()
        except Exception:
            resolved = root
        if resolved in seen or not root.is_dir():
            continue
        seen.add(resolved)
        available.append(root)
    return available


def copy_bundled_font_metrics(out_root: Path) -> int:
    copied = 0
    for metrics_source in bundled_font_metric_roots():
        copied += copy_720_font_metrics(metrics_source, out_root)
    return copied


def convert_raw_font_atlases(source_root: Path, out_root: Path) -> tuple[int, list[dict]]:
    generated: list[dict] = []
    copied = 0
    for dds_path in collect_raw_dump_font_files(source_root):
        if dds_path.suffix.lower() != ".dds":
            continue
        if dds_path.stem not in EXPORT_FONT_ATLAS_STEMS:
            continue
        png_path = out_root / FONT_ATLAS_ROOT_NAME / f"{dds_path.stem}.png"
        try:
            save_editable_font_png(dds_path, png_path)
            copied += 1
            generated.append(
                {
                    "source": dds_path.relative_to(source_root).as_posix(),
                    "path": png_path.relative_to(out_root).as_posix(),
                    "kind": "transparent_white_glyph_png",
                }
            )
        except Exception as exc:
            generated.append(
                {
                    "source": dds_path.relative_to(source_root).as_posix(),
                    "warning": f"PNG font atlas export failed: {exc}",
                }
            )
    return copied, generated


def export_fonts(source_root: Path, output_root: Path) -> dict:
    out_root = output_root / FONT_ROOT_NAME
    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    prepared_font_root = locate_prepared_font_root(source_root)
    generated_png: list[dict] = []
    if prepared_font_root:
        copied = copy_prepared_fonts(prepared_font_root, out_root)
        if not (out_root / FONT_ATLAS_ROOT_NAME / "gamefonts_pc_720.png").is_file():
            raw_copied, generated_png = convert_raw_font_atlases(source_root, out_root)
            copied += raw_copied
    else:
        copied, generated_png = convert_raw_font_atlases(source_root, out_root)
    if not (out_root / FONT_ATLAS_ROOT_NAME / "gamefonts_pc_720.png").is_file():
        raise FileNotFoundError("Could not find gamefonts_pc_720 in the selected game source or prepared dump.")
    if not any((out_root / FONT_METRICS_ROOT_NAME).rglob("*.csv")):
        metrics_copied = copy_bundled_font_metrics(out_root)
        copied += metrics_copied
        if metrics_copied:
            generated_png.append(
                {
                    "source": "resources/font_metrics",
                    "path": f"{FONT_METRICS_ROOT_NAME}/fonts/720",
                    "kind": "font_metric_fallback",
                }
            )
    if not any((out_root / FONT_METRICS_ROOT_NAME).rglob("*.csv")):
        raise FileNotFoundError("Could not find BO2 720 font metric CSV files in the selected source or toolkit resources.")

    files = []
    for path in sorted(out_root.rglob("*")):
        if not path.is_file():
            continue
        files.append(
            {
                "path": path.relative_to(output_root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    manifest = {
        "tool": APP_NAME,
        "format_version": FORMAT_VERSION,
        "asset": "font",
        "font_format": "atlas_png_metrics",
        "exported_atlases": sorted(EXPORT_FONT_ATLAS_STEMS),
        "copied_files": copied,
        "generated_png": generated_png,
        "files": files,
    }
    write_manifest(output_root / "bo2_fonts.manifest.json", manifest)
    return manifest


def find_manifest(input_root: Path, explicit: Path | None = None) -> Path | None:
    if explicit:
        return explicit
    for manifest in sorted(input_root.glob("*.manifest.json")):
        try:
            data = json.loads(read_text(manifest))
        except Exception:
            continue
        if data.get("asset") == "text":
            return manifest
    return None


def read_txt_lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8-sig")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def load_translated_records_from_manifest(input_root: Path, manifest_path: Path) -> list[dict]:
    manifest = json.loads(read_text(manifest_path))
    if manifest.get("text_format") != "txt":
        raise ValueError("Packing from manifest currently requires a TXT export manifest.")

    line_cache: dict[str, list[str]] = {}
    packed: list[dict] = []
    for record in manifest.get("records", []):
        text_file = record["text_file"]
        if text_file not in line_cache:
            line_cache[text_file] = read_txt_lines(input_root / text_file)
        lines = line_cache[text_file]
        line_index = int(record["line"])
        if line_index >= len(lines):
            raise ValueError(f"Missing line {line_index + 1} in {text_file}.")
        translated = unescape_line(lines[line_index])
        item = dict(record)
        item["translated"] = translated
        packed.append(item)
    return packed


def parse_translated_original_tree(input_root: Path) -> list[dict]:
    text_root = input_root / TEXT_ROOT_NAME if (input_root / TEXT_ROOT_NAME).is_dir() else input_root
    records = collect_text_records(text_root)
    return [record_to_manifest(record) | {"translated": record.original} for record in records]


def should_parse_original_text_tree(input_root: Path) -> bool:
    if (input_root / TEXT_ROOT_NAME).is_dir():
        return True
    if (input_root / LOCALIZED_ROOT_NAME).is_dir() or (input_root / SUBTITLES_ROOT_NAME).is_dir():
        return True
    if any(input_root.glob("*.str")):
        return True
    return any(path.is_file() and is_subtitle_csv(path) for path in input_root.glob("*.csv"))


def collect_pack_fonts(input_root: Path) -> list[dict]:
    font_root = input_root / FONT_ROOT_NAME
    if not font_root.is_dir():
        return []
    files = []
    for path in sorted(font_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(input_root).as_posix()
        rel_parts = Path(rel).parts
        if FONT_ISOLATED_PREVIEW_ROOT_NAME in rel_parts or FONT_GLYPHS_ROOT_NAME in rel_parts or FONT_INTERNAL_ROOT_NAME in rel_parts:
            continue
        files.append({"path": rel, "source": path, "size": path.stat().st_size, "sha256": sha256_file(path)})
    return files


def collect_pack_fonts_for_package(input_root: Path, build_root: Path) -> list[dict]:
    if (input_root / FONT_ROOT_NAME / FONT_INTERNAL_ROOT_NAME / "glyph_manifest.json").is_file() or (input_root / FONT_ROOT_NAME / FONT_GLYPHS_ROOT_NAME / "glyph_manifest.json").is_file():
        return rebuild_fonts_from_glyphs(input_root, build_root)
    return collect_pack_fonts(input_root)


def write_dll_bin(records: list[dict], font_files: list[dict], output_path: Path) -> dict:
    package_manifest = {
        "tool": APP_NAME,
        "format_version": FORMAT_VERSION,
        "package_version": BIN_VERSION,
        "text_records": [
            {
                key: value
                for key, value in record.items()
                if key
                in {
                    "type",
                    "source_rel_path",
                    "reference",
                    "row",
                    "column",
                    "original",
                    "translated",
                }
            }
            for record in records
        ],
        "font_files": [
            {key: value for key, value in font.items() if key in {"path", "size", "sha256"}}
            for font in font_files
        ],
    }
    manifest_bytes = json.dumps(package_manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as handle:
        handle.write(BIN_MAGIC)
        handle.write(struct.pack("<IQ", BIN_VERSION, len(manifest_bytes)))
        handle.write(manifest_bytes)
        handle.write(struct.pack("<I", len(font_files)))
        for font in font_files:
            rel_bytes = font["path"].encode("utf-8")
            data = Path(font["source"]).read_bytes()
            handle.write(struct.pack("<HQ", len(rel_bytes), len(data)))
            handle.write(rel_bytes)
            handle.write(data)
    return package_manifest


def build_font_payload(font_files: list[dict]) -> bytes:
    manifest = {
        "tool": APP_NAME,
        "format_version": FORMAT_VERSION,
        "package_version": BIN_VERSION,
        "recalculated_glyph_metrics": ["x0", "y0", "dx", "pixelWidth", "pixelHeight", "s0", "t0", "s1", "t1"],
        "font_files": [
            {key: value for key, value in font.items() if key in {"path", "size", "sha256"}}
            for font in font_files
        ],
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    blob = bytearray()
    blob.extend(b"T6TRFNT1")
    blob.extend(struct.pack("<IQ", BIN_VERSION, len(manifest_bytes)))
    blob.extend(manifest_bytes)
    runtime_files: list[tuple[str, bytes]] = []
    for font in font_files:
        rel_path = str(font["path"]).replace("\\", "/")
        source = Path(font["source"])
        atlas_stem = source.stem.lower()
        if rel_path.lower().endswith(".png") and "/atlases/" in rel_path.lower() and atlas_stem in MAIN_FONT_ATLAS_STEMS:
            try:
                from PIL import Image
            except Exception as exc:
                raise RuntimeError("Pillow is required to package the font atlas.") from exc
            image = Image.open(source).convert("RGBA")
            data = bytearray(b"T6RGBA2\0")
            _pack_payload_bytes(data, atlas_stem.encode("utf-8"), "H")
            data.extend(struct.pack("<II", image.width, image.height))
            data.extend(image_to_bo2_font_texture_rgba(image))
            runtime_files.append((rel_path[:-4] + ".rgba", data))
            continue
        if rel_path.lower().endswith(".csv") and "/metrics/fonts/" in rel_path.lower():
            meta_lines, _header, rows = parse_font_metric_csv(source)
            metadata = font_metric_metadata(meta_lines)
            name = metadata.get("name") or f"fonts/{source.stem}"
            atlas_stem = font_atlas_stem(meta_lines)
            data = bytearray(b"T6FMETR2")
            _pack_payload_bytes(data, name.encode("utf-8"), "H")
            _pack_payload_bytes(data, atlas_stem.encode("utf-8"), "H")
            data.extend(struct.pack("<iiI", int(metadata.get("pixelHeight", 0)), int(metadata.get("isScalingAllowed", 0)), len(rows)))
            for row in rows:
                data.extend(
                    struct.pack(
                        "<Hbbbbbffff",
                        int(row.get("letter") or 0),
                        int(float(row.get("x0") or 0)),
                        int(float(row.get("y0") or 0)),
                        int(float(row.get("dx") or 0)),
                        int(float(row.get("pixelWidth") or 0)),
                        int(float(row.get("pixelHeight") or 0)),
                        float(row.get("s0") or 0),
                        float(row.get("t0") or 0),
                        float(row.get("s1") or 0),
                        float(row.get("t1") or 0),
                    )
                )
            runtime_files.append((rel_path[:-4] + ".t6font", bytes(data)))
            continue
        runtime_files.append((rel_path, source.read_bytes()))

    blob.extend(struct.pack("<I", len(runtime_files)))
    for rel_path, data in runtime_files:
        rel_bytes = rel_path.encode("utf-8")
        blob.extend(struct.pack("<HQ", len(rel_bytes), len(data)))
        blob.extend(rel_bytes)
        blob.extend(data)
    return bytes(blob)


def image_to_bo2_font_texture_rgba(image):
    """Convert editable font PNGs to the alpha-mask layout used by the runtime hook."""
    try:
        from PIL import Image, ImageChops
    except Exception as exc:
        raise RuntimeError("Pillow is required to package the font atlas.") from exc

    image = image.convert("RGBA")
    alpha = image.getchannel("A")
    if alpha.getextrema() == (255, 255):
        r, g, b, _a = image.split()
        mask = ImageChops.lighter(ImageChops.lighter(r, g), b)
    else:
        mask = alpha
    runtime = Image.new("RGBA", image.size, (255, 255, 255, 0))
    runtime.putalpha(mask)
    return runtime.tobytes()


def build_runtime_config(write_runtime_log: bool) -> bytes:
    flags = RUNTIME_CONFIG_LOG_ENABLED if write_runtime_log else 0
    return RUNTIME_CONFIG_MAGIC + struct.pack("<I", flags)


def write_font_payload(font_files: list[dict], output_path: Path, write_runtime_log: bool = True) -> dict:
    blob = build_font_payload(font_files) + build_runtime_config(write_runtime_log)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(blob)
    return {"path": output_path.name, "font_files": len(font_files), "bytes": len(blob)}


def _payload_bytes(value: object) -> bytes:
    return str(value if value is not None else "").encode("utf-8")


def _pack_payload_bytes(blob: bytearray, data: bytes, size_format: str) -> None:
    blob.extend(struct.pack("<" + size_format, len(data)))
    blob.extend(data)


def _legacy_stringtable_name(source_rel_path: str) -> str:
    value = str(source_rel_path or "").replace("\\", "/")
    if value.startswith(f"{SUBTITLES_ROOT_NAME}/"):
        value = value[len(SUBTITLES_ROOT_NAME) + 1 :]
    parts = value.split("/")
    if parts and parts[0].startswith("en_"):
        value = "/".join(parts[1:])
    if not value.startswith("video/"):
        value = f"video/{value}"
    return value


def write_legacy_text_payload(
    records: list[dict],
    output_path: Path,
    write_runtime_log: bool = True,
    append_runtime_config: bool = True,
) -> dict:
    localize_records = []
    stringtable_records = []
    for record in records:
        record_type = record.get("type")
        original = record.get("original", "")
        translated = record.get("translated", original)
        if record_type == "str":
            reference = record.get("reference")
            if reference:
                localize_records.append((reference, original, translated))
        elif record_type == "csv":
            table_name = _legacy_stringtable_name(record.get("source_rel_path", ""))
            stringtable_records.append(
                (
                    table_name,
                    int(record.get("row", 0)),
                    int(record.get("column", 3)),
                    original,
                    translated,
                )
            )

    blob = bytearray()
    blob.extend(b"T6TRTXT1")
    blob.extend(struct.pack("<II", len(localize_records), len(stringtable_records)))
    for reference, original, translated in localize_records:
        _pack_payload_bytes(blob, _payload_bytes(reference), "H")
        _pack_payload_bytes(blob, _payload_bytes(original), "I")
        _pack_payload_bytes(blob, _payload_bytes(translated), "I")
    for table_name, row, column, original, translated in stringtable_records:
        _pack_payload_bytes(blob, _payload_bytes(table_name), "H")
        blob.extend(struct.pack("<II", row, column))
        _pack_payload_bytes(blob, _payload_bytes(original), "I")
        _pack_payload_bytes(blob, _payload_bytes(translated), "I")

    if append_runtime_config:
        blob.extend(build_runtime_config(write_runtime_log))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(blob)
    return {
        "path": output_path.name,
        "localize_records": len(localize_records),
        "stringtable_records": len(stringtable_records),
        "bytes": len(blob),
    }


def find_proxy_dll() -> Path | None:
    for candidate in (DLL_LIB_DIR / PROXY_DLL_NAME, LIB_DIR / PROXY_DLL_NAME):
        if candidate.is_file():
            return candidate
    return None


def clean_package_output(output_root: Path, has_text: bool, has_font: bool) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    stale_names = [
        "BO2TranslationHook.dll",
        "t6tr_runtime_override.dll",
        "cod_bo2_translation_hook.dll",
        "t6tr_text_payload.bin",
        "dll.manifest.json",
        LEGACY_PACKAGE_NAME,
        PROXY_DLL_NAME,
    ]
    if has_text and has_font:
        stale_names.extend([TEXT_PACKAGE_NAME, FONT_PACKAGE_NAME, ALL_PACKAGE_NAME])
    elif has_text:
        stale_names.extend([TEXT_PACKAGE_NAME, ALL_PACKAGE_NAME])
    elif has_font:
        stale_names.extend([FONT_PACKAGE_NAME, ALL_PACKAGE_NAME])
    else:
        stale_names.extend([TEXT_PACKAGE_NAME, FONT_PACKAGE_NAME, ALL_PACKAGE_NAME])

    for name in stale_names:
        path = output_root / name
        if path.is_file():
            path.unlink()


def package_project(
    input_root: Path,
    output_root: Path,
    manifest_path: Path | None,
    runtime_dll: Path | None,
    write_runtime_log: bool = True,
) -> dict:
    manifest = find_manifest(input_root, manifest_path)
    if manifest and json.loads(read_text(manifest)).get("text_format") == "txt":
        text_records = load_translated_records_from_manifest(input_root, manifest)
    elif should_parse_original_text_tree(input_root):
        text_records = parse_translated_original_tree(input_root)
    else:
        text_records = []

    with tempfile.TemporaryDirectory(prefix="bo2_font_build_") as tmp:
        font_files = collect_pack_fonts_for_package(input_root, Path(tmp))
        has_text = bool(text_records)
        has_font = bool(font_files)
        if not has_text and not has_font:
            raise ValueError("The selected input folder does not contain text or font files to package.")

        clean_package_output(output_root, has_text, has_font)

        if has_text and has_font:
            output_path = output_root / ALL_PACKAGE_NAME
            payload = write_legacy_text_payload(
                text_records,
                output_path,
                write_runtime_log=write_runtime_log,
                append_runtime_config=False,
            )
            font_blob = build_font_payload(font_files)
            with output_path.open("ab") as handle:
                handle.write(font_blob)
                handle.write(build_runtime_config(write_runtime_log))
            payload["font_payload_bytes"] = len(font_blob)
        elif has_text:
            output_path = output_root / TEXT_PACKAGE_NAME
            payload = write_legacy_text_payload(text_records, output_path, write_runtime_log=write_runtime_log)
        else:
            output_path = output_root / FONT_PACKAGE_NAME
            payload = write_font_payload(font_files, output_path, write_runtime_log=write_runtime_log)

    package_manifest = {
        "tool": APP_NAME,
        "format_version": FORMAT_VERSION,
        "package_version": BIN_VERSION,
        "text_records": text_records,
        "font_files": [
            {key: value for key, value in font.items() if key in {"path", "size", "sha256"}}
            for font in font_files
        ],
        "package_bin": payload,
        "runtime_log_enabled": write_runtime_log,
        "recalculated_glyph_metrics": ["x0", "y0", "dx", "pixelWidth", "pixelHeight", "s0", "t0", "s1", "t1"] if has_font else [],
        "output_files": [output_path.name],
    }

    proxy_path = find_proxy_dll()
    if proxy_path:
        shutil.copy2(proxy_path, output_root / PROXY_DLL_NAME)
        package_manifest["copied_proxy_dll"] = PROXY_DLL_NAME
        package_manifest["output_files"].append(PROXY_DLL_NAME)
    else:
        package_manifest["proxy_dll_warning"] = (
            f"{PROXY_DLL_NAME} was not found in lib. The game will not auto-load the patch without it."
        )
    return package_manifest


def run_oat_unlinker(game_dir: Path, oat_unlinker: Path, output_root: Path) -> Path:
    zone_dir = find_game_zone_dir(game_dir)
    dump_root = output_root / OAT_DUMP_ROOT_NAME
    dump_root.mkdir(parents=True, exist_ok=True)
    hide_windows_path(dump_root)
    fastfiles = sorted(zone_dir.glob("en*.ff"))
    if not fastfiles:
        raise FileNotFoundError("No English fastfiles were found.")
    command = [
        str(oat_unlinker),
        "--output-folder",
        str(dump_root),
        "--search-path",
        str(game_dir),
        "--image-format",
        "DDS",
    ]
    command.extend(str(path) for path in fastfiles)
    subprocess.run(command, check=True)
    hide_windows_path(dump_root)
    return dump_root


def extract_assets(
    game_dir: Path,
    output_root: Path,
    source_root: Path | None,
    oat_unlinker: Path | None,
    mode: str,
    layout: str,
    directory_format: str,
) -> dict:
    if not game_dir.is_dir():
        raise FileNotFoundError("The selected game folder does not exist.")
    find_game_zone_dir(game_dir)

    resolved_oat = oat_unlinker if oat_unlinker else discover_default_oat_unlinker()
    resolved_source = normalize_source_root(source_root) if source_root else None
    if not resolved_source and resolved_oat:
        resolved_source = run_oat_unlinker(game_dir, resolved_oat, output_root)
    if not resolved_source:
        resolved_source = discover_default_source()
    if not resolved_source:
        raise FileNotFoundError(
            "OpenAssetTools was not found. Put Unlinker.exe and its required files in lib/OpenAssetTools next to this toolkit, or use --source with a prepared dump folder."
        )

    result: dict[str, object] = {
        "tool": APP_NAME,
        "source_root": str(resolved_source),
        "output_root": str(output_root),
        "mode": mode,
    }
    if mode in {"text", "both"}:
        result["text"] = export_texts(resolved_source, output_root, layout, directory_format)
    if mode in {"font", "both"}:
        result["font"] = export_fonts(resolved_source, output_root)
    warnings = []
    if (output_root / OAT_DUMP_ROOT_NAME).exists():
        hide_windows_path(output_root / OAT_DUMP_ROOT_NAME)
        warnings.append(f"Do not delete or edit the {OAT_DUMP_ROOT_NAME} folder. It contains the dump used by this export.")
    if any(output_root.glob("*.manifest.json")):
        warnings.append("Do not delete or edit .manifest.json files. They store the mapping data used by the packer.")
    if (output_root / FONT_ROOT_NAME / FONT_INTERNAL_ROOT_NAME).exists():
        warnings.append(f"Do not delete or edit fonts/{FONT_INTERNAL_ROOT_NAME}. It stores the original font metrics for automatic font repacking.")
    if warnings:
        result["warnings"] = warnings
    return result


class ToolkitApp(tk.Tk):
    WINDOW_WIDTH = 760
    WINDOW_HEIGHT = 770

    ASSET_LABELS = {
        "Text and Fonts": "both",
        "Text Only": "text",
        "Fonts Only": "font",
    }
    TEXT_EXPORT_LABELS = {
        "One TXT file (recommended)": ("single", "txt"),
        "Folder of TXT files": ("directory", "txt"),
        "Original STR/CSV files": ("directory", "original"),
    }
    TEXT_EXPORT_HINTS = {
        "One TXT file (recommended)": "Exports every translatable line into one TXT file and writes a manifest next to it.",
        "Folder of TXT files": "Exports one TXT file for each original source file and writes one manifest at the output root.",
        "Original STR/CSV files": "Exports BO2-style STR and subtitle CSV files for direct manual editing.",
    }

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry(f"{self.WINDOW_WIDTH}x{self.WINDOW_HEIGHT}")
        self.minsize(self.WINDOW_WIDTH, self.WINDOW_HEIGHT)
        self.configure(bg="#eef3f8")

        self.extract_game_folder = tk.StringVar()
        self.extract_output_folder = tk.StringVar(value=str(DEFAULT_OUTPUT_ROOT / "extracted"))
        self.extract_assets_choice = tk.StringVar(value="Text and Fonts")
        self.text_export_choice = tk.StringVar(value="One TXT file (recommended)")
        self.extract_status = tk.StringVar(value="Ready.")
        self.text_export_hint = tk.StringVar(value=self.TEXT_EXPORT_HINTS[self.text_export_choice.get()])

        self.pack_input_folder = tk.StringVar()
        self.pack_output_folder = tk.StringVar(value=str(DEFAULT_OUTPUT_ROOT / "package"))
        self.pack_runtime_log = tk.BooleanVar(value=True)
        self.pack_status = tk.StringVar(value="Ready.")

        self.build_style()
        self.build_ui()
        self.center_window()

    def center_window(self) -> None:
        self.update_idletasks()
        width = self.WINDOW_WIDTH
        height = self.WINDOW_HEIGHT
        x = max((self.winfo_screenwidth() - width) // 2, 0)
        y = max((self.winfo_screenheight() - height) // 2, 0)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def build_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("App.TFrame", background="#eef3f8")
        style.configure("Panel.TFrame", background="#ffffff", relief="flat")
        style.configure("Footer.TFrame", background="#f6f8fb", relief="flat")
        style.configure("TLabel", background="#eef3f8", foreground="#172033", font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background="#ffffff", foreground="#172033", font=("Segoe UI", 10))
        style.configure("Hint.TLabel", background="#ffffff", foreground="#667085", font=("Segoe UI", 9))
        style.configure("Title.TLabel", background="#eef3f8", foreground="#0f172a", font=("Segoe UI Semibold", 15))
        style.configure("Credit.TLabel", background="#eef3f8", foreground="#667085", font=("Segoe UI", 9))
        style.configure("Status.TLabel", background="#f6f8fb", foreground="#334155", font=("Segoe UI Semibold", 9))
        style.configure("DisabledHint.TLabel", background="#ffffff", foreground="#98a2b3", font=("Segoe UI", 9))
        style.configure("Panel.TCheckbutton", background="#ffffff", foreground="#172033", font=("Segoe UI", 10))
        style.map("Panel.TCheckbutton", background=[("active", "#ffffff")])
        style.configure("TEntry", padding=(10, 8), fieldbackground="#ffffff", bordercolor="#d3dce8", lightcolor="#d3dce8", darkcolor="#d3dce8")
        style.configure("TCombobox", padding=(10, 8), fieldbackground="#ffffff", bordercolor="#d3dce8", lightcolor="#d3dce8", darkcolor="#d3dce8")
        style.map("TCombobox", fieldbackground=[("disabled", "#d7dde6")], foreground=[("disabled", "#7c8798")])
        style.configure("TButton", font=("Segoe UI Semibold", 10), padding=(12, 8), borderwidth=0, focusthickness=0, focuscolor="")
        style.configure("Browse.TButton", background="#e8eef5", foreground="#172033", relief="flat")
        style.configure("Accent.TButton", background="#2454d6", foreground="#ffffff", relief="flat")
        button_layout = [
            (
                "Button.border",
                {
                    "sticky": "nswe",
                    "children": [
                        (
                            "Button.padding",
                            {
                                "sticky": "nswe",
                                "children": [("Button.label", {"sticky": "nswe"})],
                            },
                        )
                    ],
                },
            )
        ]
        style.layout("Browse.TButton", button_layout)
        style.layout("Accent.TButton", button_layout)
        style.map("Browse.TButton", background=[("active", "#dfe7f0"), ("pressed", "#d2deea")], relief=[("pressed", "flat")])
        style.map("Accent.TButton", background=[("active", "#1d4ed8"), ("pressed", "#1e40af")], relief=[("pressed", "flat")])
        style.configure("TNotebook", background="#eef3f8", borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", background="#dfe8f2", foreground="#334155", font=("Segoe UI Semibold", 10), padding=(16, 10), borderwidth=0, focusthickness=0, focuscolor="")
        style.map("TNotebook.Tab", background=[("selected", "#ffffff"), ("active", "#edf3f8")], foreground=[("selected", "#0f172a"), ("active", "#172033")])

    def build_ui(self) -> None:
        root = ttk.Frame(self, style="App.TFrame", padding=(22, 20, 22, 22))
        root.pack(fill="both", expand=True)
        header = ttk.Frame(root, style="App.TFrame")
        header.pack(fill="x")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text=f"Made by dörtkoldantaciz - {APP_VERSION}", style="Credit.TLabel").grid(row=0, column=1, sticky="ne", pady=(5, 0))

        tabs = ttk.Notebook(root, takefocus=False)
        tabs.pack(fill="both", expand=True, pady=(18, 0))
        extract_tab = ttk.Frame(tabs, style="Panel.TFrame", padding=22)
        pack_tab = ttk.Frame(tabs, style="Panel.TFrame", padding=22)
        tabs.add(extract_tab, text="Extract")
        tabs.add(pack_tab, text="Pack")
        self.build_extract_tab(extract_tab)
        self.build_pack_tab(pack_tab)

    def build_extract_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        self.field(parent, 0, "Game Folder", self.extract_game_folder, lambda: self.choose_folder(self.extract_game_folder))
        self.field(parent, 1, "Output Folder", self.extract_output_folder, lambda: self.choose_folder(self.extract_output_folder))
        self.combo_field(parent, 2, "Extract", self.extract_assets_choice, tuple(self.ASSET_LABELS), self.update_asset_mode)
        self.text_export_field, self.text_export_combo = self.combo_field(
            parent, 3, "Text Export Format", self.text_export_choice, tuple(self.TEXT_EXPORT_LABELS), self.update_text_export_hint
        )
        self.text_export_hint_label = ttk.Label(parent, textvariable=self.text_export_hint, style="Hint.TLabel", wraplength=660, justify="left")
        self.text_export_hint_label.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        footer = ttk.Frame(parent, style="Footer.TFrame", padding=(14, 12))
        footer.grid(row=5, column=0, sticky="ew", pady=(24, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.extract_status, style="Status.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 12))
        self.button(footer, text="Extract", style="Accent.TButton", command=self.extract).grid(row=1, column=0, sticky="ew")

    def build_pack_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        self.field(parent, 0, "Translated Input Folder", self.pack_input_folder, lambda: self.choose_folder(self.pack_input_folder))
        self.field(parent, 1, "Package Output Folder", self.pack_output_folder, lambda: self.choose_folder(self.pack_output_folder))
        ttk.Checkbutton(
            parent,
            text="Write t6_translation.log next to the DLL",
            variable=self.pack_runtime_log,
            style="Panel.TCheckbutton",
            takefocus=False,
        ).grid(row=2, column=0, sticky="w", pady=(0, 4))
        footer = ttk.Frame(parent, style="Footer.TFrame", padding=(14, 12))
        footer.grid(row=3, column=0, sticky="ew", pady=(24, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.pack_status, style="Status.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 12))
        self.button(footer, text="Build Package", style="Accent.TButton", command=self.pack_project).grid(row=1, column=0, sticky="ew")

    def button(self, parent: ttk.Frame, text: str, style: str, command) -> ttk.Button:
        def wrapped_command() -> None:
            command()
            self.after_idle(self.focus_set)
        return ttk.Button(parent, text=text, style=style, command=wrapped_command, takefocus=False)

    def field(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, command, button_text: str = "Browse") -> None:
        field = ttk.Frame(parent, style="Panel.TFrame")
        field.grid(row=row, column=0, sticky="ew", pady=(0, 18))
        field.columnconfigure(0, weight=1)
        ttk.Label(field, text=label, style="Panel.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 7))
        ttk.Entry(field, textvariable=variable, takefocus=False).grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.button(field, text=button_text, style="Browse.TButton", command=command).grid(row=1, column=1, sticky="e")

    def combo_field(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, values: tuple[str, ...], callback=None):
        field = ttk.Frame(parent, style="Panel.TFrame")
        field.grid(row=row, column=0, sticky="ew", pady=(0, 18))
        field.columnconfigure(0, weight=1)
        ttk.Label(field, text=label, style="Panel.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 7))
        combo = ttk.Combobox(field, textvariable=variable, values=values, state="readonly", takefocus=False)
        combo.grid(row=1, column=0, sticky="ew")

        def clear_selection() -> None:
            try:
                combo.selection_clear()
                combo.icursor("end")
            except tk.TclError:
                pass

        combo.bind("<FocusIn>", lambda _event: self.after_idle(clear_selection), add="+")
        combo.bind("<ButtonRelease-1>", lambda _event: self.after_idle(clear_selection), add="+")
        if callback:
            def selected(_event) -> None:
                callback()
                self.after_idle(clear_selection)
            combo.bind("<<ComboboxSelected>>", selected)
        else:
            combo.bind("<<ComboboxSelected>>", lambda _event: self.after_idle(clear_selection))
        return field, combo

    def choose_folder(self, variable: tk.StringVar) -> None:
        path = filedialog.askdirectory(title="Select folder")
        if path:
            variable.set(path)

    def update_text_export_hint(self) -> None:
        self.text_export_hint.set(self.TEXT_EXPORT_HINTS.get(self.text_export_choice.get(), ""))

    def update_asset_mode(self) -> None:
        asset_mode = self.ASSET_LABELS.get(self.extract_assets_choice.get(), "both")
        if asset_mode == "font":
            self.text_export_combo.configure(state="disabled")
            self.text_export_hint.set("Disabled because font-only extraction does not export text.")
            self.text_export_hint_label.configure(style="DisabledHint.TLabel")
        else:
            self.text_export_combo.configure(state="readonly")
            self.text_export_hint_label.configure(style="Hint.TLabel")
            self.update_text_export_hint()

    def extract(self) -> None:
        game_path = Path(self.extract_game_folder.get())
        output_root = Path(self.extract_output_folder.get())
        asset_mode = self.ASSET_LABELS[self.extract_assets_choice.get()]
        layout, directory_format = self.TEXT_EXPORT_LABELS[self.text_export_choice.get()]
        if not game_path.exists():
            self.task_failed(self.extract_status, FileNotFoundError("Select a valid Call of Duty: Black Ops II folder."))
            return
        def task() -> dict[str, object]:
            return extract_assets(game_path, output_root, None, None, asset_mode, layout, directory_format)
        def success(result: dict[str, object]) -> None:
            summary = summarize_result(result)
            text_count = summary.get("text", {}).get("total_records", 0) if isinstance(summary.get("text"), dict) else 0
            font_count = summary.get("font", {}).get("copied_files", 0) if isinstance(summary.get("font"), dict) else 0
            self.extract_status.set(f"Done. Text records: {text_count}, font files: {font_count}.")
            warnings = result.get("warnings", [])
            warning_text = "\n\nImportant:\n" + "\n".join(f"- {warning}" for warning in warnings) if warnings else ""
            messagebox.showinfo(APP_NAME, f"Extraction complete.\n\nOutput folder:\n{output_root}{warning_text}")
        self.run_task(self.extract_status, "Extracting...", task, success)

    def pack_project(self) -> None:
        input_root = Path(self.pack_input_folder.get())
        output_root = Path(self.pack_output_folder.get())
        if not input_root.is_dir():
            self.task_failed(self.pack_status, FileNotFoundError("Select a valid translated input folder."))
            return
        def task() -> dict[str, object]:
            return package_project(input_root, output_root, None, None, self.pack_runtime_log.get())
        def success(result: dict[str, object]) -> None:
            summary = summarize_result(result).get("package", {})
            text_count = summary.get("text_records", 0) if isinstance(summary, dict) else 0
            font_count = summary.get("font_files", 0) if isinstance(summary, dict) else 0
            self.pack_status.set(f"Done. Text records: {text_count}, font files: {font_count}.")
            output_files = ", ".join(summary.get("output_files", [])) if isinstance(summary, dict) else "the generated BIN file"
            messagebox.showinfo(APP_NAME, f"Package complete.\n\nCopy xinput1_3.dll and {output_files} next to t6sp.exe:\n{output_root}")
        self.run_task(self.pack_status, "Packing...", task, success)

    def run_task(self, status_var: tk.StringVar, busy_text: str, task, on_success) -> None:
        status_var.set(busy_text)
        def worker() -> None:
            try:
                result = task()
            except Exception as exc:
                self.after(0, self.task_failed, status_var, exc)
                return
            self.after(0, lambda: on_success(result))
        threading.Thread(target=worker, daemon=True).start()

    def task_failed(self, status_var: tk.StringVar, exc: Exception) -> None:
        status_var.set("Failed")
        messagebox.showerror(APP_NAME, str(exc))

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=APP_NAME)
    sub = parser.add_subparsers(dest="command")

    extract = sub.add_parser("extract", help="Extract text and/or fonts.")
    extract.add_argument("--game", required=True, help="BO2 game folder.")
    extract.add_argument("--out", required=True, help="Output folder.")
    extract.add_argument("--source", help="Prepared clean_extract or dump folder.")
    extract.add_argument("--oat-unlinker", help="OpenAssetTools Unlinker.exe path.")
    extract.add_argument("--mode", choices=("text", "font", "both"), default="both")
    extract.add_argument("--layout", choices=("single", "directory"), default="single")
    extract.add_argument("--directory-format", choices=("txt", "original"), default="txt")

    pack = sub.add_parser("pack", help="Build text.bin, font.bin, or all.bin from translated output.")
    pack.add_argument("--input", required=True, help="Translated input folder.")
    pack.add_argument("--out", required=True, help="Package output folder.")
    pack.add_argument("--manifest", help="TXT manifest path.")
    pack.add_argument("--no-runtime-log", action="store_true", help="Do not write t6_translation.log next to the DLL.")
    return parser


def summarize_result(result: dict) -> dict:
    summary: dict[str, object] = {
        "tool": result.get("tool", APP_NAME),
    }
    if "output_root" in result:
        summary["output_root"] = result["output_root"]
    if "source_root" in result:
        summary["source_root"] = result["source_root"]
    if "warnings" in result:
        summary["warnings"] = result["warnings"]
    if "text" in result:
        text = result["text"]
        summary["text"] = {
            "total_records": text.get("total_records"),
            "text_files": len(text.get("text_files", [])),
            "text_format": text.get("text_format"),
            "layout": text.get("layout"),
        }
    if "font" in result:
        font = result["font"]
        summary["font"] = {
            "copied_files": font.get("copied_files"),
        }
    if "package_version" in result:
        summary["package"] = {
            "text_records": len(result.get("text_records", [])),
            "font_files": len(result.get("font_files", [])),
            "copied_proxy_dll": result.get("copied_proxy_dll"),
            "package_bin": result.get("package_bin", {}).get("path") if isinstance(result.get("package_bin"), dict) else None,
            "output_files": result.get("output_files", []),
            "proxy_dll_warning": result.get("proxy_dll_warning"),
        }
    return summary


def emit_cli_summary(result: dict) -> None:
    stream = getattr(sys, "stdout", None)
    if stream is None:
        return
    stream.write(json.dumps(summarize_result(result), ensure_ascii=False, indent=2))
    stream.write("\n")
    stream.flush()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        ToolkitApp().mainloop()
        return 0

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "extract":
        result = extract_assets(
            Path(args.game),
            Path(args.out),
            Path(args.source) if args.source else None,
            Path(args.oat_unlinker) if args.oat_unlinker else None,
            args.mode,
            args.layout,
            args.directory_format,
        )
        emit_cli_summary(result)
        return 0
    if args.command == "pack":
        result = package_project(
            Path(args.input),
            Path(args.out),
            Path(args.manifest) if args.manifest else None,
            None,
            not args.no_runtime_log,
        )
        emit_cli_summary(result)
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())


