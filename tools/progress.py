"""Measure xbox2xbox progress from the source tree and render the Pages site.

Three metrics, all counted from code, never from running games:

* HLE exports: every `hle_export!(Name)` registration in crates/x2x-hle/src/modules
  is declared; it is implemented when its `fn Name` body is not a stub (a body
  that only returns E_NOTIMPL, calls `missing`, or is `unimplemented!`/`todo!`).
* GFX10 opcodes: the decoder tables in crates/x2x-shader/src/gfx10/opcodes.rs
  against the AMD RDNA 1 + RDNA 2 instruction list (tools/rdna_isa.txt).
* D3D12.X methods: the vtable slots wired to real functions in crates/x2x-gpu/src
  against the slots those vtables expose (the `missing_table::<KIND, N>` capacity);
  a slot left unwired logs and fails, so it counts as not implemented.

The tree may be a skeleton with no crates yet: exports and D3D12.X then count 0/0
and every reference opcode is not decoded. Outputs progress.json, badge SVGs, a
treemap (progress.svg) and progress.html; --compare prints a markdown PR delta report.
"""

import argparse
import json
import os
import re
import sys
from fractions import Fraction
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HLE = ROOT / "crates" / "x2x-hle" / "src" / "modules"
OPCODES = ROOT / "crates" / "x2x-shader" / "src" / "gfx10" / "opcodes.rs"
GPU = ROOT / "crates" / "x2x-gpu" / "src"
ISA = Path(__file__).resolve().parent / "rdna_isa.txt"
SOURCE = f'https://github.com/{os.environ.get("GITHUB_REPOSITORY", "torvaldsdev/xbox2xbox")}/blob/main'
EXPORT = re.compile(r"\bhle_export!\s*\(\s*(\w+)\s*\)")
FN = re.compile(r"\bfn\s+(\w+)\s*(?:<[^{}]*>)?\s*\(")
TABLE = re.compile(r"const (\w+): &\[\(u16, &str\)\] = &\[(.*?)\];", re.S)
TABLE_ENTRY = re.compile(r'\(0x[0-9a-fA-F]+,\s*"([a-z0-9_]+)"\)')
FLAT_SEGMENTS = ("GLOBAL_", "SCRATCH_")
VTABLE = re.compile(r"missing_table::<\s*\{?\s*(?:object::)?([A-Za-z_]\w*)\s*\}?\s*,\s*(\d+)\s*>")
WIRE = re.compile(r"\bf\[(\d+)\]\s*=\s*([A-Za-z_][\w:]*)\s+as\s+\*const\s*\(\s*\)")
METHOD_ARRAY = re.compile(r"const (\w+): \[\(usize, \*const \(\)\); \d+\] = \[(.*?)\];", re.S)
ARRAY_WIRE = re.compile(r"\(\s*([^,()]+?)\s*,\s*([A-Za-z_][\w:]*)\s+as\s+\*const\s*\(\s*\)\s*\)")
BASE_SLOTS = {1: "add_ref", 2: "release"}
OBJECT_SLOTS = {3: "get_private_data", 4: "set_private_data", 6: "set_name"}
STUB_BODY = (
    r"",
    r"(?:return\s+)?(?:\w+::)*missing\s*(?:::<[^()]*>)?\s*\([^()]*\)\s*;?",
    r"(?:return\s+)?(?:\w+::)*E_NOTIMPL\s*;?",
    r"(?:return\s+)?fail\s*\([^()]*\bE_NOTIMPL\b[^()]*\)\s*;?",
    r"(?:unimplemented|todo|panic)\s*!\s*(?:\([^()]*\)|\[[^\[\]]*\])\s*;?",
)
REPORT_ROWS = 100
PANEL_WIDTH, GAP, MAP_HEIGHT, HEADER = 495, 10, 280, 30
MODULE_TIERS = ("Large modules", "Medium modules", "Small modules")
GRID_COLUMNS, GRID_ROW = 12, 28
DONE_COLOR, TODO_COLOR, BORDER, TEXT = "#2ea043", "#6e7681", "#0d1117", "#ffffff"


def unique_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def mask(src):
    """Blank out comments and string/char literals, keeping offsets and newlines."""
    out = list(src)
    i, n = 0, len(src)

    def blank(a, b):
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i + 2)
            blank(i, n if j < 0 else j)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if src.startswith("/*", j):
                    depth += 1
                    j += 2
                elif src.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            blank(i, j)
            i = j
        elif raw := re.match(r'(?:br|r)(#*)"', src[i:]):
            close = '"' + raw.group(1)
            j = src.find(close, i + raw.end())
            j = n if j < 0 else j + len(close)
            blank(i, j)
            i = j
        elif c == '"' or (c == "b" and src[i + 1:i + 2] == '"'):
            j = i + (2 if c == "b" else 1)
            while j < n:
                if src[j] == "\\":
                    j += 2
                elif src[j] == '"':
                    j += 1
                    break
                else:
                    j += 1
            blank(i, j)
            i = j
        elif c == "'":
            j = i + 1
            if j < n and src[j] == "\\":
                j += 1
                if src[j:j + 2] == "u{":
                    k = src.find("}", j)
                    j = n if k < 0 else k + 1
                elif src[j:j + 1] == "x":
                    j += 3
                else:
                    j += 1
                if src[j:j + 1] == "'":
                    j += 1
                blank(i, j)
                i = j
            elif src[j + 1:j + 2] == "'":
                blank(i, j + 2)
                i = j + 2
            else:
                i += 1
        else:
            i += 1
    return "".join(out)


def fn_bodies(masked):
    bodies = {}
    for match in FN.finditer(masked):
        depth, j = 1, match.end()
        while j < len(masked) and depth:
            if masked[j] == "(":
                depth += 1
            elif masked[j] == ")":
                depth -= 1
            j += 1
        k = masked.find("{", j)
        if k < 0 or ";" in masked[j:k]:
            continue
        depth, end = 1, k + 1
        while end < len(masked) and depth:
            if masked[end] == "{":
                depth += 1
            elif masked[end] == "}":
                depth -= 1
            end += 1
        bodies[match.group(1)] = masked[k + 1:end - 1]
    return bodies


def is_stub(body):
    text = " ".join(body.split())
    return any(re.fullmatch(pattern, text) for pattern in STUB_BODY)


def module_files(root):
    groups = {}
    for path in sorted(root.iterdir()):
        if path.is_file() and path.suffix == ".rs" and path.stem != "mod":
            groups.setdefault(path.stem, []).append(path)
        elif path.is_dir():
            files = sorted(path.rglob("*.rs"))
            if files:
                groups.setdefault(path.name, []).extend(files)
    return groups


def collect_exports():
    if not HLE.is_dir():
        return summarize([])
    crate = {}
    for path in sorted((HLE.parent).rglob("*.rs")):
        crate.update(fn_bodies(mask(path.read_text(encoding="utf-8", errors="ignore"))))
    groups = []
    for name, files in sorted(module_files(HLE).items()):
        local, declared = {}, []
        for path in files:
            masked = mask(path.read_text(encoding="utf-8", errors="ignore"))
            local.update(fn_bodies(masked))
            for export in EXPORT.finditer(masked):
                if export.group(1) not in declared:
                    declared.append(export.group(1))
        group = {"name": name, "label": name, "done": 0, "todo": 0, "done_names": [], "todo_names": []}
        for export in declared:
            body = local.get(export, crate.get(export))
            state = "done" if body is not None and not is_stub(body) else "todo"
            group[state] += 1
            group[f"{state}_names"].append(export)
        groups.append(group)
    return summarize(groups)


def select_exports(exports, modules):
    if not modules:
        return exports
    known = {group["name"] for group in exports["groups"]}
    missing = [name for name in modules if name not in known]
    if missing:
        label = "module" if len(missing) == 1 else "modules"
        raise ValueError(f"unknown {label}: {', '.join(missing)}")
    chosen = set(modules)
    return summarize([group for group in exports["groups"] if group["name"] in chosen])


def load_isa():
    isa = {}
    for line in ISA.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line and not line.startswith("#"):
            parts = line.split()
            if len(parts) != 2:
                raise ValueError(f"tools/rdna_isa.txt: expected 'name encoding' per line, got: {line}")
            isa[parts[0]] = parts[1]
    return isa


def collect_shaders():
    isa = load_isa()
    supported = set()
    if OPCODES.is_file():
        source = OPCODES.read_text(encoding="utf-8", errors="ignore")
        tables = TABLE.findall(source)
        if not tables:
            raise ValueError(f"{OPCODES} has no opcode tables")
        for _, body in tables:
            supported.update(name.upper() for name in TABLE_ENTRY.findall(body))
        flat = [name for name in supported if name.startswith("FLAT_")]
        supported.update(segment + name.removeprefix("FLAT_") for name in flat for segment in FLAT_SEGMENTS)
    groups = {}
    for name, encoding in isa.items():
        group = groups.setdefault(encoding, {"name": encoding, "label": encoding, "done": 0, "todo": 0,
                                             "done_names": [], "todo_names": []})
        state = "done" if name in supported else "todo"
        group[state] += 1
        group[f"{state}_names"].append(name)
    result = summarize(sorted(groups.values(), key=lambda g: g["name"]))
    result["extra"] = sorted(supported - isa.keys())
    return result


def summarize(groups):
    done = sum(g["done"] for g in groups)
    total = done + sum(g["todo"] for g in groups)
    return {"done": done, "total": total, "percent": round(100 * done / total, 2) if total else 0, "groups": groups}


def strip_tests(masked):
    """Blank out #[cfg(test)] modules so their vtables are not counted."""
    out = list(masked)
    for match in re.finditer(r"#\[cfg\(test\)\]", masked):
        head = re.match(r"\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+\w+\s*\{", masked[match.end():])
        if not head:
            continue
        depth, k = 1, match.end() + head.end()
        while k < len(masked) and depth:
            if masked[k] == "{":
                depth += 1
            elif masked[k] == "}":
                depth -= 1
            k += 1
        for t in range(match.start(), k):
            if out[t] != "\n":
                out[t] = " "
    return "".join(out)


def enclosing_block(masked, pos):
    """Body of the brace block that contains pos (a vtable builder's block)."""
    depth, start, i = 0, 0, pos
    while i > 0:
        i -= 1
        if masked[i] == "}":
            depth += 1
        elif masked[i] == "{":
            if depth == 0:
                start = i
                break
            depth -= 1
    depth, k = 0, start
    while k < len(masked):
        if masked[k] == "{":
            depth += 1
        elif masked[k] == "}":
            depth -= 1
            if depth == 0:
                return masked[start + 1:k]
        k += 1
    return masked[start + 1:]


def collect_gpu():
    if not GPU.is_dir():
        return summarize([])
    groups = module_files(GPU)
    index = {}
    for name, files in groups.items():
        local = {}
        for path in files:
            local.update(fn_bodies(strip_tests(mask(path.read_text(encoding="utf-8", errors="ignore")))))
        index[name] = local

    def implemented(target, current):
        parts = target.split("::")
        fn = parts[-1]
        module = current
        if len(parts) > 1:
            head = parts[0]
            if head in ("crate", "super") and len(parts) > 2:
                head = parts[1]
            if head in index:
                module = head
        body = index.get(module, {}).get(fn)
        if body is None and module != current:
            body = index.get(current, {}).get(fn)
        return body is None or not is_stub(body)

    out = []
    for name, files in sorted(groups.items()):
        done = total = 0
        done_names, todo_names = [], []
        for path in files:
            masked = strip_tests(mask(path.read_text(encoding="utf-8", errors="ignore")))
            arrays = {}
            for array in METHOD_ARRAY.finditer(masked):
                entries = []
                for entry in ARRAY_WIRE.finditer(array.group(2)):
                    try:
                        slot = int(entry.group(1).strip(), 0)
                    except ValueError:
                        continue
                    entries.append((slot, entry.group(2)))
                arrays[array.group(1)] = entries
            for builder in VTABLE.finditer(masked):
                kind, capacity = builder.group(1).lower(), int(builder.group(2))
                block = enclosing_block(masked, builder.start())
                base = dict(BASE_SLOTS)
                if "d3d12_object(" in block:
                    base.update(OBJECT_SLOTS)
                slots = {slot: (True, label) for slot, label in base.items() if slot < capacity}

                def wire(slot, target):
                    if slot < capacity:
                        slots[slot] = (implemented(target, name), target.split("::")[-1])

                for assignment in WIRE.finditer(block):
                    wire(int(assignment.group(1)), assignment.group(2))
                for array_name, entries in arrays.items():
                    if re.search(rf"\b{re.escape(array_name)}\b", block):
                        for slot, target in entries:
                            wire(slot, target)
                for slot in range(capacity):
                    state, label = slots.get(slot, (False, "slot"))
                    method = f"{kind}.{label}#{slot}"
                    total += 1
                    if state:
                        done += 1
                        done_names.append(method)
                    else:
                        todo_names.append(method)
        out.append({"name": name, "label": name, "done": done, "todo": total - done,
                    "done_names": done_names, "todo_names": todo_names})
    return summarize(out)


def worst_ratio(row, side):
    area = sum(row)
    return max(max(side * side * r / area ** 2, area ** 2 / (side * side * r)) for r in row)


def place(row, x, y, w, h, rects):
    thickness = sum(row) / min(w, h)
    offset = 0
    for area in row:
        length = area / thickness
        if w >= h:
            rects.append((x, y + offset, thickness, length))
        else:
            rects.append((x + offset, y, length, thickness))
        offset += length
    return (x + thickness, y, w - thickness, h) if w >= h else (x, y + thickness, w, h - thickness)


def squarify(values, x, y, w, h):
    total = sum(values)
    areas = [v * w * h / total for v in values]
    rects, row = [], []
    while areas:
        side = min(w, h)
        if not row or worst_ratio(row + [areas[0]], side) <= worst_ratio(row, side):
            row.append(areas.pop(0))
            continue
        x, y, w, h = place(row, x, y, w, h, rects)
        row = []
    if row:
        place(row, x, y, w, h, rects)
    return rects


def cells(count, x, y, w, h):
    if not count or w <= 0 or h <= 0:
        return []
    rows = max(1, min(count, round((count * h / w) ** 0.5)))
    result = []
    for r in range(rows):
        in_row = count // rows + (r < count % rows)
        cw = w / in_row
        result += [(x + c * cw, y + r * h / rows, cw, h / rows) for c in range(in_row)]
    return result


def rect(x, y, w, h, color, stroke=0.5):
    return (f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" '
            f'fill="{color}" stroke="{BORDER}" stroke-width="{stroke}"/>')


def text(x, y, value, size):
    return (f'<text x="{x:.2f}" y="{y:.2f}" font-family="sans-serif" font-size="{size}" fill="{TEXT}" '
            f'stroke="{BORDER}" stroke-width="3" paint-order="stroke">{escape(value)}</text>')


def plain_text(x, y, value, size=12, anchor="start"):
    return (f'<text x="{x:.2f}" y="{y:.2f}" font-family="sans-serif" font-size="{size}" fill="{TEXT}" '
            f'text-anchor="{anchor}">{escape(value)}</text>')


def tile_label(group, w, h):
    label_length = int((w - 10) / 7)
    label = group["label"][:label_length] if label_length > 0 else ""
    return label if (label == group["label"] or len(label) >= 3) and h > 22 else ""


def map_groups(data):
    return sorted((g for g in data["groups"] if g["done"] + g["todo"]), key=lambda g: -(g["done"] + g["todo"]))


def labelled_tier(groups):
    shown = list(groups)
    while len(shown) > 1:
        rects = squarify([g["done"] + g["todo"] for g in shown], 0, HEADER, PANEL_WIDTH, MAP_HEIGHT)
        if all(tile_label(g, w, h) for g, (x, y, w, h) in zip(shown, rects)):
            break
        shown.pop()
    return shown


def module_tiers(exports):
    rest, tiers = map_groups(exports), []
    empty = [g for g in exports["groups"] if not g["done"] + g["todo"]]
    while rest and len(tiers) < len(MODULE_TIERS) - 1:
        tier = labelled_tier(rest)
        tiers.append(summarize(tier + empty))
        rest = rest[len(tier):]
    if rest:
        tiers.append(summarize(rest + empty))
    return tiers


def heading(title, data, x, y):
    return text(x + 4, y + 21, f'{title}: {data["percent"]}% ({data["done"]}/{data["total"]})', 16)


def tile(group, x, y, w, h):
    total = group["done"] + group["todo"]
    parts = [f'<g><title>{escape(group["name"])}: {group["done"]}/{total} ({100 * group["done"] / total:.0f}%)</title>']
    for i, cell in enumerate(cells(total, x + 1, y + 1, w - 2, h - 2)):
        parts.append(rect(*cell, DONE_COLOR if i < group["done"] else TODO_COLOR))
    parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" fill="none" stroke="{BORDER}" stroke-width="2"/>')
    label = tile_label(group, w, h)
    if label:
        parts.append(text(x + 5, y + 16, label, 12))
    parts.append("</g>")
    return parts


def treemap(title, data, left, top=0):
    groups = map_groups(data)
    parts = [heading(title, data, left, top)]
    for group, (x, y, w, h) in zip(groups, squarify([g["done"] + g["todo"] for g in groups], left, top + HEADER, PANEL_WIDTH, MAP_HEIGHT)):
        parts += tile(group, x, y, w, h)
    return parts


def tile_grid(title, data, top):
    groups = sorted(map_groups(data), key=lambda g: (g["label"].lower(), g["name"]))
    width = (2 * PANEL_WIDTH + GAP) / GRID_COLUMNS
    parts = [heading(title, data, 0, top)]
    for index, group in enumerate(groups):
        parts += tile(group, index % GRID_COLUMNS * width, top + HEADER + index // GRID_COLUMNS * GRID_ROW, width, GRID_ROW)
    return parts, HEADER + -(-len(groups) // GRID_COLUMNS) * GRID_ROW


def render(exports, shaders, gpu):
    width, row = 2 * PANEL_WIDTH + GAP, HEADER + MAP_HEIGHT
    tiers = module_tiers(exports)
    body = [heading("HLE exports*", exports, 0, 0)]
    top = HEADER
    if not any(map_groups(exports)):
        body.append(plain_text(4, top + 24, "No HLE exports declared yet; the map appears when crates/x2x-hle lands.", 13))
        top += 44
    for index, (title, data) in enumerate(zip(MODULE_TIERS[:2], tiers[:2])):
        body += treemap(title, data, index * (PANEL_WIDTH + GAP), top)
    top += row if tiers[:2] else 0
    if len(tiers) > 2:
        grid, height = tile_grid(MODULE_TIERS[2], tiers[2], top + 10)
        body += grid
        top += height + 10
    body += treemap("GFX10 opcodes decoded", shaders, 0, top)
    top += row
    body += treemap("D3D12.X methods", gpu, 0, top)
    if not any(map_groups(gpu)):
        body.append(plain_text(4, top + HEADER + 24, "No D3D12.X vtables yet; the map appears when crates/x2x-gpu lands.", 13))
    height = top + row
    return "\n".join([f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">',
                      rect(0, 0, width, height, BORDER, 0), *body, "</svg>"])


def badge(label, data, value=None, color=None):
    percent = data["percent"]
    color = color or ("#4c1" if percent >= 90 else "#97ca00" if percent >= 60 else "#dfb317" if percent >= 30 else "#fe7d37")
    value = value or f"{percent}%"
    left, right = 10 + 7 * len(label), 10 + 7 * len(value)
    width = left + right
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="20" role="img" aria-label="{label}: {value}">'
            f'<rect width="{left}" height="20" fill="#555"/><rect x="{left}" width="{right}" height="20" fill="{color}"/>'
            f'<g fill="#fff" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11" text-anchor="middle">'
            f'<text x="{left / 2}" y="14">{label}</text><text x="{left + right / 2}" y="14">{value}</text></g></svg>')


def table(title, column, data):
    rows = [f"<h2>{title}</h2>", "<table>",
            f"<tr><th>{column}</th><th>Implemented</th><th>Total</th><th>%</th></tr>"]
    for group in sorted(data["groups"], key=lambda g: g["name"].lower()):
        total = group["done"] + group["todo"]
        if not total:
            rows.append(f'<tr><td>{escape(group["name"])}</td><td colspan="3">None</td></tr>')
            continue
        percent = f'{100 * group["done"] / total:.0f}%'
        rows.append(f'<tr><td>{escape(group["name"])}</td><td>{group["done"]}</td><td>{total}</td><td>{percent}</td></tr>')
    rows.append(f'<tr><th>Total</th><th>{data["done"]}</th><th>{data["total"]}</th><th>{data["percent"]}%</th></tr>')
    rows.append("</table>")
    return "\n".join(rows)


def names_list(title, names):
    if not names:
        return ""
    return (f"<details><summary>{escape(title)} ({len(names)})</summary>"
            f"<p><code>{escape(', '.join(names))}</code></p></details>")


def page(exports, shaders, gpu):
    missing = sorted(name for group in shaders["groups"] for name in group["todo_names"])
    return "\n".join([
        '<!DOCTYPE html>',
        '<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="color-scheme" content="light dark"><title>xbox2xbox progress</title>',
        '<style>:root{color-scheme:light dark}body{font-family:sans-serif;max-width:1000px;margin:auto;padding:16px}'
        'img{max-width:100%}table{border-collapse:collapse}th,td{border:1px solid #888;padding:2px 8px}'
        'td+td,th+th{text-align:right}code{word-break:break-word}</style>',
        '</head><body>',
        '<h1>xbox2xbox progress</h1>',
        '<img src="progress.svg" alt="progress map">',
        f'<p>Generated by <a href="{SOURCE}/tools/progress.py">tools/progress.py</a> after relevant pushes to <code>main</code>, '
        'daily, and when the Pages workflow is run manually. '
        'Raw numbers: <a href="progress.json">progress.json</a>. <a href="history.html">Progress history</a> (one point per day).</p>',
        table("HLE exports", "Module", exports),
        '<p>An export is declared when a module registers it with <code>hle_export!</code>; it is implemented when its body '
        'is not a stub. A stub only returns <code>E_NOTIMPL</code> (directly or through <code>fail</code>), calls '
        '<code>missing</code>, or is <code>unimplemented!</code>/<code>todo!</code>. '
        f'The total only includes exports already declared in <a href="{SOURCE}/crates/x2x-hle/src/modules">crates/x2x-hle/src/modules</a>, '
        'not every function the Xbox system DLLs export; it grows as more are declared. '
        'These numbers are counted from the code, not from playing games, so they say nothing about whether a game runs.</p>',
        table("GFX10 opcodes decoded", "Encoding", shaders),
        f'<p>The total is the AMD RDNA 1 + RDNA 2 instruction list (<a href="{SOURCE}/tools/rdna_isa.txt">tools/rdna_isa.txt</a>). '
        f'An opcode is decoded when the <a href="{SOURCE}/crates/x2x-shader/src/gfx10">decoder</a> tables recognize it; '
        'decoded does not mean the translation to DXIL is implemented or correct. '
        'Decoder names not present in AMD\'s public list are not counted.</p>',
        names_list("Reference opcodes not decoded yet", missing),
        names_list("Decoder names outside the reference list", shaders["extra"]),
        table("D3D12.X methods", "Module", gpu),
        f'<p>A D3D12.X method is implemented when its vtable slot in <a href="{SOURCE}/crates/x2x-gpu/src">crates/x2x-gpu/src</a> '
        'is wired to a real function (<code>f[slot] = ...</code>); the base object methods count too. '
        'The total is the number of slots the vtables expose (the <code>missing_table</code> capacity): a slot left '
        'unwired logs and fails, so it counts as not implemented. Wired does not mean the method is correct or complete.</p>',
        '</body></html>',
    ]) + "\n"


SITE_REPORTS = ("progress.json", "badge-exports.svg", "badge-shaders.svg", "badge-gpu.svg", "progress.svg", "progress.html", "index.html")


def require_distinct_reports(output):
    paths = [output / name for name in SITE_REPORTS]
    for index, path in enumerate(paths):
        if not path.exists():
            continue
        if not path.is_file():
            raise ValueError(f"{path}: report destination must be a regular file")
        for other in paths[index + 1:]:
            if other.exists() and path.samefile(other):
                raise ValueError(f"{path.name} and {other.name} in {output} are the same file")
    targets = [os.path.normcase(os.path.realpath(path)) for path in paths]
    for index, target in enumerate(targets):
        if target in targets[index + 1:]:
            raise ValueError(f"generated reports in {output} alias each other")


def site_data(exports, shaders, gpu):
    return {"exports": exports, "shaders": shaders, "gpu": gpu}


def write_site(output, exports, shaders, gpu):
    output.mkdir(parents=True, exist_ok=True)
    require_distinct_reports(output)
    (output / "progress.json").write_text(json.dumps(site_data(exports, shaders, gpu), indent=2), encoding="utf-8")
    (output / "badge-exports.svg").write_text(badge("HLE exports*", exports), encoding="utf-8")
    (output / "badge-shaders.svg").write_text(badge("GFX10 decoded", shaders), encoding="utf-8")
    (output / "badge-gpu.svg").write_text(badge("D3D12.X methods", gpu), encoding="utf-8")
    (output / "progress.svg").write_text(render(exports, shaders, gpu) + "\n", encoding="utf-8")
    (output / "progress.html").write_text(page(exports, shaders, gpu), encoding="utf-8")
    (output / "index.html").write_text(
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0; url=progress.html"><title>xbox2xbox</title></head>'
        '<body><p><a href="progress.html">xbox2xbox progress</a></p></body></html>\n', encoding="utf-8")


def names(data, state):
    return {(g["name"], n) for g in data["groups"] for n in g.get(f"{state}_names", [])}


def details(icon, title, column, items):
    if not items:
        return []
    rows = [f"<details>\n<summary>{icon} {len(items)} {title}</summary>\n", f"| {column} | Name |", "| - | - |"]
    rows += [f"| {group} | `{name}` |" for group, name in sorted(items)[:REPORT_ROWS]]
    if len(items) > REPORT_ROWS:
        rows.append(f"| ... | {len(items) - REPORT_ROWS} more |")
    return rows + ["\n</details>"]


def exact_delta(base, head):
    try:
        return Fraction(100 * head["done"], head["total"]) - Fraction(100 * base["done"], base["total"])
    except (KeyError, TypeError, ZeroDivisionError):
        return None


def small_delta(delta):
    text = f"{float(delta):+.4f}"
    if float(text) == 0:
        return f'{"+" if delta > 0 else "-"}<0.0001'
    return text.rstrip("0")


def compare(title, column, unit, base, head):
    base_done, head_done = names(base, "done"), names(head, "done")
    base_all, head_all = base_done | names(base, "todo"), head_done | names(head, "todo")
    implemented, declared = head_done - base_done, head_all - base_all - head_done
    regressed, removed = base_done & (head_all - head_done), base_all - head_all
    if not (implemented or declared or regressed or removed):
        return []
    delta = round(head["percent"] - base["percent"], 2)
    exact = exact_delta(base, head) if delta == 0 else None
    shown = small_delta(exact) if exact else f"{delta:+}"
    icon = "📈" if (exact or delta) > 0 else "📉" if (exact or delta) < 0 else "➖"
    counts = [f"{n:+} {label}" for n, label in ((len(implemented), "implemented"), (len(declared), "declared"),
                                                (-len(regressed), "reverted"), (-len(removed), "removed")) if n]
    unit = unit[:-1] if len(implemented) + len(declared) + len(regressed) + len(removed) == 1 else unit
    changes = f', {", ".join(counts)} {unit}' if counts else ""
    lines = [f'{icon} **{title}**: {head["percent"]}% ({shown}%{changes})', ""]
    lines += details("✅", "implemented", column, implemented)
    lines += details("🆕", "declared as stubs", column, declared)
    lines += details("⚠️", "went back to stubs", column, regressed)
    lines += details("🗑️", "removed", column, removed)
    return lines + [""]


def module_progress(group):
    if group is None:
        return "Not present", None
    total = group["done"] + group["todo"]
    if not total:
        return "No exports", None
    percent = 100 * group["done"] / total
    return f'{group["done"]}/{total} ({percent:.2f}%)', percent


def percent_change(old_percent, new_percent):
    if old_percent is None or new_percent is None:
        return "n/a"
    return f'{round(new_percent - old_percent, 2) or 0:+.2f}%'


def module_report(base, head):
    before, after = ({group["name"]: group for group in data["groups"]} for data in (base, head))
    fields = ("done", "todo", "done_names", "todo_names")
    rows = []
    for name in sorted(before.keys() | after.keys(), key=str.casefold):
        old, new = before.get(name), after.get(name)
        if old is not None and new is not None and all(old.get(field) == new.get(field) for field in fields):
            continue
        old_text, old_percent = module_progress(old)
        new_text, new_percent = module_progress(new)
        delta = percent_change(old_percent, new_percent)
        if delta == "+0.00%":
            exact = exact_delta({"done": old["done"], "total": old["done"] + old["todo"]},
                                {"done": new["done"], "total": new["done"] + new["todo"]})
            if exact:
                delta = f"{small_delta(exact)}%"
        rows.append(f"| {name} | {old_text} | {new_text} | {delta} |")
    if not rows:
        return []
    lines = ["#### Changed modules (implemented exports)", "",
             "| Module | Before | After | Change |", "| - | - | - | - |"] + rows[:REPORT_ROWS]
    if len(rows) > REPORT_ROWS:
        lines.append(f"| ... | {len(rows) - REPORT_ROWS} more modules | | |")
    return lines + [""]


def report(base, head):
    lines = compare("HLE exports", "Module", "exports", base["exports"], head["exports"])
    lines += module_report(base["exports"], head["exports"])
    lines += compare("GFX10 opcodes decoded", "Encoding", "opcodes", base["shaders"], head["shaders"])
    lines += compare("D3D12.X methods", "Module", "methods", base["gpu"], head["gpu"])
    return "\n".join(lines)


def validate_metric(data, label, with_extra=False):
    if not isinstance(data, dict):
        raise ValueError(f"{label} must be an object")
    required = {"done", "total", "percent", "groups"}
    allowed = required | {"extra"} if with_extra else required
    if not required <= data.keys() <= allowed:
        raise ValueError(f"{label} has unexpected fields")
    done, total, percent = data["done"], data["total"], data["percent"]
    if type(done) is not int or type(total) is not int or not 0 <= done <= total:
        raise ValueError(f"{label} has invalid counts")
    if type(percent) not in (int, float) or percent != (round(100 * done / total, 2) if total else 0):
        raise ValueError(f"{label} percent does not match its counts")
    if not isinstance(data["groups"], list):
        raise ValueError(f"{label} groups must be a list")
    seen, group_done, group_total = set(), 0, 0
    for group in data["groups"]:
        if not isinstance(group, dict) or group.keys() != {"name", "label", "done", "todo", "done_names", "todo_names"}:
            raise ValueError(f"{label} has a malformed group")
        name = group["name"]
        if not isinstance(name, str) or not name or name in seen:
            raise ValueError(f"{label} has a duplicate or empty group name")
        seen.add(name)
        if not isinstance(group["label"], str):
            raise ValueError(f"{label} group {name} has a non-string label")
        gdone, gtodo = group["done"], group["todo"]
        if type(gdone) is not int or type(gtodo) is not int or gdone < 0 or gtodo < 0:
            raise ValueError(f"{label} group {name} has invalid counts")
        for state, count in (("done", gdone), ("todo", gtodo)):
            value = group[f"{state}_names"]
            if not isinstance(value, list) or len(value) != count or any(not isinstance(n, str) for n in value):
                raise ValueError(f"{label} group {name} {state}_names does not match its count")
        group_done += gdone
        group_total += gdone + gtodo
    if (group_done, group_total) != (done, total):
        raise ValueError(f"{label} groups do not sum to its totals")
    if with_extra and (not isinstance(data["extra"], list) or any(not isinstance(n, str) for n in data["extra"])):
        raise ValueError(f"{label} extra must be a list of names")


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or snapshot.keys() != {"exports", "shaders", "gpu"}:
        raise ValueError("snapshot must hold exactly exports, shaders and gpu")
    validate_metric(snapshot["exports"], "exports")
    validate_metric(snapshot["shaders"], "shaders", with_extra=True)
    validate_metric(snapshot["gpu"], "gpu")


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    global HLE, OPCODES, GPU
    previous = HLE, OPCODES, GPU
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--root", type=Path, help="source tree to measure instead of the one containing this script")
    parser.add_argument("--compare", type=Path, nargs=2, metavar=("BASE", "HEAD"),
                        help="print a markdown report of the changes between two progress.json files")
    parser.add_argument("--module", action="append", metavar="NAME",
                        help="limit export counts to these x2x-hle modules; repeat to include several. Shader and D3D12.X counts stay complete")
    parser.add_argument("--json", action="store_true",
                        help="print the progress.json object to stdout instead of the summary lines")
    try:
        args = parser.parse_args(argv)
        if args.compare:
            if args.module or args.json:
                parser.error("--compare does not combine with --module or --json")
            snapshots = []
            for path in args.compare:
                try:
                    snapshots.append(json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_json_keys))
                except (OSError, ValueError, RecursionError) as error:
                    parser.error(f"{path}: {error}")
            for path, snapshot in zip(args.compare, snapshots):
                try:
                    validate_snapshot(snapshot)
                except ValueError as error:
                    parser.error(f"{path}: {error}")
            base, head = snapshots
            print(report(base, head), end="")
            return 0
        if not args.output and not (args.module or args.json):
            parser.error("the output directory is required")
        if args.root:
            HLE = args.root / "crates" / "x2x-hle" / "src" / "modules"
            OPCODES = args.root / "crates" / "x2x-shader" / "src" / "gfx10" / "opcodes.rs"
            GPU = args.root / "crates" / "x2x-gpu" / "src"
        try:
            exports, shaders, gpu = collect_exports(), collect_shaders(), collect_gpu()
        except OSError as error:
            parser.error(f"{args.root or ROOT} is not a source tree: {error.filename}: {error.strerror}")
        except ValueError as error:
            parser.error(f"{args.root or ROOT} is not a source tree: {error}")
        try:
            exports = select_exports(exports, args.module or [])
        except ValueError as error:
            parser.error(str(error))
        if args.output:
            try:
                write_site(args.output, exports, shaders, gpu)
            except ValueError as error:
                parser.error(str(error))
            except OSError as error:
                parser.error(f"{args.output}: {error.strerror or error}")
        if args.json:
            print(json.dumps(site_data(exports, shaders, gpu), indent=2))
        else:
            print(f'exports {exports["done"]}/{exports["total"]} ({exports["percent"]}%)')
            print(f'GFX10 opcodes decoded {shaders["done"]}/{shaders["total"]} ({shaders["percent"]}%)')
            print(f'D3D12.X methods {gpu["done"]}/{gpu["total"]} ({gpu["percent"]}%)')
        return 0
    finally:
        HLE, OPCODES, GPU = previous


if __name__ == "__main__":
    raise SystemExit(main())