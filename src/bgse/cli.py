"""Command line interface - useful for scripting and for headless checks."""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

from . import locate
from .formats import lsf, lsmf
from .formats.verify import check_codecs, roundtrip
from .model import SaveModel
from .save import Savegame


def _open(path: str) -> SaveModel:
    return SaveModel(Savegame.open(path))


def cmd_gui(args) -> int:
    from .app import main as app_main
    return app_main([])


def cmd_web(args) -> int:
    from .webapp import main as web_main
    return web_main(args.host, args.port, not args.no_browser)


def cmd_where(args) -> int:
    print(json.dumps(locate.describe_environment(), indent=2))
    return 0


def cmd_list(args) -> int:
    saves = locate.saves_in(args.dir) if args.dir else locate.find_saves()
    if not saves:
        print("No savegames found.  Try `bgse where` to see the paths searched.")
        return 1
    for s in saves:
        print(f"{s.modified:%Y-%m-%d %H:%M}  {s.size/1e6:7.1f} MB  {s.name}")
        print(f"    {s.path}")
    return 0


def cmd_info(args) -> int:
    model = _open(args.save)
    ov = model.save
    print(f"{ov.path}")
    print(f"  files: {', '.join(ov.package.names())}")
    info = ov.save_info
    for key in ("Save Name", "Difficulty", "Game Version", "Current Level"):
        if info.get(key):
            print(f"  {key}: {info[key]}")
    print("\n  party:")
    for m in model.party():
        slot = "-" if m.xp_slot is None else str(m.xp_slot)
        print(f"    [{m.index}] {m.name:<14} {m.class_label:<28} "
              f"lvl {m.level:<3} xp {m.xp_total:<8} (ecs slot {slot})")
    return 0


def cmd_classes(args) -> int:
    from . import gamedata
    data = gamedata.shared()
    if data.empty:
        print("No game install found, so class GUIDs cannot be named.")
        print("Use `bgse where` to check the detected install path.")
    else:
        s = data.summary()
        print(f"game data: {s['total']} definitions {s['by_category']}")
    model = _open(args.save)
    rows = [r for r in model.class_rows() if r["classes"]]
    print(f"\n{len(rows)} entities with class levels:")
    for r in rows:
        print(f"  [{r['index']:>4}] total {r['total_level']:>2}  {r['label']}")
    return 0


def cmd_set_class_level(args) -> int:
    model = _open(args.save)
    model.set_class_level(args.row, args.entry, args.level)
    result = model.write(args.output, backup=not args.no_backup)
    row = next(r for r in model.class_rows() if r["index"] == args.row)
    print(f"row {args.row} is now: {row['label']}")
    print(f"wrote {result['bytes']:,} bytes to {result['path']}")
    if result.get("backup"):
        print(f"backup: {result['backup']}")
    return 0


def cmd_build(args) -> int:
    """Everything decoded about each party member's build."""
    model = _open(args.save)
    for m in model.party():
        print(f"\n[{m.index}] {m.name} - {m.class_label}, level {m.level}"
              + (f", {m.race_name}" if m.race_name else ""))
        if m.abilities:
            print("     " + "  ".join(f"{a['short']} {a['value']:>2}"
                                      for a in m.abilities))
        for c in m.class_levels:
            print(f"     class  {c['label']} level {c['level']}")
        for f in m.feats:
            print(f"     feat   level {f['level']}: {f['feat']}")
        print(f"     rows: xp={m.xp_slot} class/stats={m.class_row} "
              f"progression={m.progression_row}")
    return 0


def cmd_set_ability(args) -> int:
    model = _open(args.save)
    member = next((m for m in model.party() if m.index == args.character), None)
    if member is None or member.class_row is None:
        print(f"character {args.character} has no matched stats row", file=sys.stderr)
        return 1
    names = [a["short"] for a in member.abilities]
    if args.ability.upper() not in names:
        print(f"ability must be one of {names}", file=sys.stderr)
        return 1
    index = names.index(args.ability.upper())
    model.set_ability(member.class_row, index, args.value)
    result = model.write(args.output, backup=not args.no_backup)
    print(f"{member.name}: {args.ability.upper()} -> {args.value}")
    print(f"wrote {result['bytes']:,} bytes to {result['path']}")
    if result.get("backup"):
        print(f"backup: {result['backup']}")
    return 0


def cmd_items(args) -> int:
    model = _open(args.save)
    data = model.items()
    print(f"{data['resolved']}/{data['total']} item entities resolved against "
          f"{data['templates_known']:,} known root templates")
    rows = data["items"]
    if args.query:
        low = args.query.lower()
        rows = [r for r in rows if low in r["name"].lower()
                or low in r["stats"].lower()]
    print(f"\n{len(rows)} distinct templates:")
    for r in rows[:args.limit]:
        print(f"  {r['count']:>5} x {r['name']:<46} {r['type']:<10} {r['stats']}")
    if len(rows) > args.limit:
        print(f"  ... {len(rows)-args.limit} more (raise --limit)")
    return 0


def cmd_components(args) -> int:
    model = _open(args.save)
    types = model.component_types(args.file, args.query, not args.all)
    print(f"{len(types)} component types in {args.file}")
    for t in types:
        print(f"  {t['count']:>7} x {t['element_size']:<5} {t['name']}")
    return 0


def cmd_dump(args) -> int:
    model = _open(args.save)
    res = model.component_rows(args.file, args.component, args.start, args.limit)
    t = res["type"]
    print(f"{t['name']}: {t['count']} x {t['element_size']} bytes @ {t['data_offset']}")
    for row in res["rows"]:
        print(f"  [{row['index']:>5}] {row['hex']}")
    return 0


def cmd_set_xp(args) -> int:
    model = _open(args.save)
    members = model.party()
    target = next((m for m in members if m.index == args.character), None)
    if target is None:
        print(f"no character with index {args.character}", file=sys.stderr)
        return 1
    if target.xp_slot is None:
        print(f"{target.name} has no matching ECS experience row", file=sys.stderr)
        return 1
    model.set_experience(target.xp_slot, args.xp)
    result = model.write(args.output, backup=not args.no_backup)
    print(f"{target.name}: xp -> {args.xp}")
    print(f"wrote {result['bytes']:,} bytes to {result['path']}")
    if result.get("backup"):
        print(f"backup: {result['backup']}")
    return 0


def cmd_verify(args) -> int:
    save = Savegame.open(args.save)
    failures = 0
    for entry in save.package.files:
        if not entry.name.endswith(".lsf"):
            continue
        blob = entry.data
        problems = check_codecs(blob) + roundtrip(blob)
        doc = lsf.LSFDocument.from_bytes(blob)
        note = ""
        region = doc.resource.regions.get("NewAge")
        if region is not None:
            ecs_blob = region.get("NewAge")
            m = lsmf.LSMFDocument.from_bytes(ecs_blob)
            same = m.to_bytes() == ecs_blob
            note = f"  ecs: {len(m.types)} types, byte-identical={same}"
            if not same:
                problems.append("ECS blob did not re-serialise identically")
        status = "OK  " if not problems else "FAIL"
        if problems:
            failures += 1
        print(f"[{status}] {entry.name}{note}")
        for p in problems[:5]:
            print(f"        {p}")
    print("RESULT:", "all good" if not failures else f"{failures} file(s) failed")
    return 0 if not failures else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bgse", description="Baldur's Gate 3 save editor")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("gui", help="launch the desktop app").set_defaults(fn=cmd_gui)
    sub.add_parser("where", help="show the paths searched").set_defaults(fn=cmd_where)

    s = sub.add_parser("web", help="serve the same UI in your browser")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=0, help="0 picks a free port")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(fn=cmd_web)

    s = sub.add_parser("list", help="list savegames")
    s.add_argument("--dir", help="look in this directory instead")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("info", help="summarise a savegame")
    s.add_argument("save")
    s.set_defaults(fn=cmd_info)

    s = sub.add_parser("build", help="show each party member's decoded build")
    s.add_argument("save")
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("set-ability", help="set an ability score")
    s.add_argument("save")
    s.add_argument("character", type=int, help="party index from `bgse info`")
    s.add_argument("ability", help="STR DEX CON INT WIS CHA")
    s.add_argument("value", type=int)
    s.add_argument("-o", "--output")
    s.add_argument("--no-backup", action="store_true")
    s.set_defaults(fn=cmd_set_ability)

    s = sub.add_parser("classes", help="show decoded class levels")
    s.add_argument("save")
    s.set_defaults(fn=cmd_classes)

    s = sub.add_parser("set-class-level", help="set one class level")
    s.add_argument("save")
    s.add_argument("row", type=int, help="row index from `bgse classes`")
    s.add_argument("entry", type=int, help="which class in that row (0-based)")
    s.add_argument("level", type=int)
    s.add_argument("-o", "--output")
    s.add_argument("--no-backup", action="store_true")
    s.set_defaults(fn=cmd_set_class_level)

    s = sub.add_parser("items", help="list the items in a save, by name")
    s.add_argument("save")
    s.add_argument("--query", default="", help="filter by name or stats")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_items)

    s = sub.add_parser("components", help="list ECS component types")
    s.add_argument("save")
    s.add_argument("--file", default="Globals.lsf")
    s.add_argument("--query", default="")
    s.add_argument("--all", action="store_true", help="include empty components")
    s.set_defaults(fn=cmd_components)

    s = sub.add_parser("dump", help="dump raw elements of one component")
    s.add_argument("save")
    s.add_argument("component")
    s.add_argument("--file", default="Globals.lsf")
    s.add_argument("--start", type=int, default=0)
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_dump)

    s = sub.add_parser("set-xp", help="set a character's experience")
    s.add_argument("save")
    s.add_argument("character", type=int, help="party index from `bgse info`")
    s.add_argument("xp", type=int)
    s.add_argument("-o", "--output", help="write elsewhere instead of in place")
    s.add_argument("--no-backup", action="store_true")
    s.set_defaults(fn=cmd_set_xp)

    s = sub.add_parser("verify", help="check every container in a save round-trips")
    s.add_argument("save")
    s.set_defaults(fn=cmd_verify)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return 1
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
