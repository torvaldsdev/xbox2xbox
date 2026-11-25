"""Measure xbox2xbox progress from the source tree.

Two metrics, both counted from code, never from running games:

* HLE exports: every `hle_export!(Name)` registration in crates/x2x-hle/src/modules
  is declared; it is implemented when its `fn Name` body is not a stub (a body
  that only returns E_NOTIMPL, calls `missing`, or is `unimplemented!`/`todo!`).
* GFX10 opcodes: the decoder tables in crates/x2x-shader/src/gfx10/opcodes.rs
  against the AMD RDNA 1 + RDNA 2 instruction list (tools/rdna_isa.txt).

The tree may be a skeleton with no crates yet: exports then count 0/0 and every
reference opcode is not decoded. Prints summary lines, or a JSON snapshot with
--json; --compare prints a markdown PR delta report between two snapshots.
"""

import argparse
import json
import re
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HLE = ROOT / "crates" / "x2x-hle" / "src" / "modules"
OPCODES = ROOT / "crates" / "x2x-shader" / "src" / "gfx10" / "opcodes.rs"
ISA = Path(__file__).resolve().parent / "rdna_isa.txt"
EXPORT = re.compile(r"\bhle_export!\s*\(\s*(\w+)\s*\)")
FN = re.compile(r"\bfn\s+(\w+)\s*(?:<[^{}]*>)?\s*\(")
TABLE = re.compile(r"const (\w+): &\[\(u16, &str\)\] = &\[(.*?)\];", re.S)
TABLE_ENTRY = re.compile(r'\(0x[0-9a-fA-F]+,\s*"([a-z0-9_]+)"\)')
FLAT_SEGMENTS = ("GLOBAL_", "SCRATCH_")
STUB_BODY = (
    r"",
    r"(?:return\s+)?(?:\w+::)*missing\s*(?:::<[^()]*>)?\s*\([^()]*\)\s*;?",
    r"(?:return\s+)?(?:\w+::)*E_NOTIMPL\s*;?",
    r"(?:return\s+)?fail\s*\([^()]*\bE_NOTIMPL\b[^()]*\)\s*;?",
    r"(?:unimplemented|todo|panic)\s*!\s*(?:\([^()]*\)|\[[^\[\]]*\])\s*;?",
)
REPORT_ROWS = 100


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


def module_files():
    groups = {}
    for path in sorted(HLE.iterdir()):
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
    for name, files in sorted(module_files().items()):
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


def snapshot(exports, shaders):
    return {"exports": exports, "shaders": shaders}


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


def validate_snapshot(snap):
    if not isinstance(snap, dict) or snap.keys() != {"exports", "shaders"}:
        raise ValueError("snapshot must hold exactly exports and shaders")
    validate_metric(snap["exports"], "exports")
    validate_metric(snap["shaders"], "shaders", with_extra=True)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    global HLE, OPCODES
    previous = HLE, OPCODES
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, help="source tree to measure instead of the one containing this script")
    parser.add_argument("--compare", type=Path, nargs=2, metavar=("BASE", "HEAD"),
                        help="print a markdown report of the changes between two progress snapshots")
    parser.add_argument("--module", action="append", metavar="NAME",
                        help="limit export counts to these x2x-hle modules; repeat to include several. Shader counts stay complete")
    parser.add_argument("--json", action="store_true",
                        help="print the progress snapshot as JSON to stdout instead of the summary lines")
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
            for path, snap in zip(args.compare, snapshots):
                try:
                    validate_snapshot(snap)
                except ValueError as error:
                    parser.error(f"{path}: {error}")
            base, head = snapshots
            print(report(base, head), end="")
            return 0
        if args.root:
            HLE = args.root / "crates" / "x2x-hle" / "src" / "modules"
            OPCODES = args.root / "crates" / "x2x-shader" / "src" / "gfx10" / "opcodes.rs"
        try:
            exports, shaders = collect_exports(), collect_shaders()
        except OSError as error:
            parser.error(f"{args.root or ROOT} is not a source tree: {error.filename}: {error.strerror}")
        except ValueError as error:
            parser.error(f"{args.root or ROOT} is not a source tree: {error}")
        try:
            exports = select_exports(exports, args.module or [])
        except ValueError as error:
            parser.error(str(error))
        if args.json:
            print(json.dumps(snapshot(exports, shaders), indent=2))
        else:
            print(f'exports {exports["done"]}/{exports["total"]} ({exports["percent"]}%)')
            print(f'GFX10 opcodes decoded {shaders["done"]}/{shaders["total"]} ({shaders["percent"]}%)')
        return 0
    finally:
        HLE, OPCODES = previous


if __name__ == "__main__":
    raise SystemExit(main())