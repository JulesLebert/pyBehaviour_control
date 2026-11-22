#!/usr/bin/env python3
"""Copy the day's imaging and videos to the server, renamed after the behaviour session.

Run it on the imaging PC (fiber link to the server), after the behaviour PC has
copied the behaviour session ("Copy today to server"):

    python copy_recordings.py                    # today, every mouse found
    python copy_recordings.py --date 20261121 --mouse RD138

Only needs Python 3.8 or newer, no packages: copy this one file to the PC.
Edit SERVER and SOURCES below once, or give --server / --imaging / --topview / --sideview.

What it does:
1. Checks it can reach the server and each source folder.
2. Finds the mice that have a day folder (<mouse>/YYYYMMDD or <mouse>/YYMMDD) in a source.
3. Reads their behaviour sessions of that day on the server (<server>/<mouse>/Training/).
4. A file goes with the last session that started before the file was last saved.
   One file for a session is ticked; several are listed unticked for you to tick.
   A file saved in the last minute is never copied (still recording?).
5. Shows every file in full and where it goes, then asks before copying:
     imaging  -> <server>/<mouse>/Recording/Imaging/<session>/<session>.tiff
                 (several ticked: <session>_1.tiff, _2... in the order saved)
     topview  -> <server>/<mouse>/Recording/Video/<session>/<session>_topview.avi
     sideview -> <server>/<mouse>/Recording/Video/<session>/<session>_sideview.avi
6. Copies each file straight under its final name. The server is write-only, so
   nothing there is ever replaced: same size already there = skipped, other size
   = reported and left alone. The originals are not changed or deleted.
   The PC is kept awake while copying. Ctrl+C stops after the current file.
   Each file is written to copy_recordings_log.csv next to this script.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

# --- edit once per PC --------------------------------------------------------------
SERVER = "//sv-nas1.rcp.epfl.ch/Petersen-Lab/data"
SOURCES = {
    "imaging": "D:/Robin",
    "topview": "//sv-07-094/bhv_cam_top",
    "sideview": "//sv-07-094/bhv_cam_side",
}
# ------------------------------------------------------------------------------------

EXTENSIONS = {"imaging": (".tif", ".tiff"), "topview": (".avi",), "sideview": (".avi",)}
SETTLE_S = 60  # a file changed more recently than this may still be recording
CHUNK = 16 * 1024 * 1024
LOG = Path(__file__).with_name("copy_recordings_log.csv")


@dataclass
class Session:
    mouse: str
    name: str  # <mouse>_<YYYYMMDD>_<HHMMSS>
    start: datetime


@dataclass
class Item:
    kind: str  # imaging | topview | sideview
    source: Path
    size: int
    saved: float  # st_mtime
    session: Session | None  # None: saved before the first session of the day
    selected: bool = False
    ready: bool = True  # False: changed less than SETTLE_S ago


# --- finding ------------------------------------------------------------------------

def size_text(n: float) -> str:
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "kB") else f"{n:.1f} {unit}"
        n /= 1000
    return ""


def can_read(folder: str | Path) -> bool:
    try:
        with os.scandir(folder) as it:
            next(it, None)
        return True
    except OSError:
        return False


def day_folders(root: Path, mouse: str, day: str) -> list[Path]:
    """<root>/<mouse>/YYYYMMDD and <root>/<mouse>/YYMMDD, those that exist."""
    return [f for f in (root / mouse / day, root / mouse / day[2:]) if f.is_dir()]


def mice_with_data(sources: dict[str, str], day: str) -> list[str]:
    mice = set()
    for root in sources.values():
        try:
            for p in Path(root).iterdir():
                if p.is_dir() and day_folders(Path(root), p.name, day):
                    mice.add(p.name)
        except OSError:
            pass
    return sorted(mice)


def sessions_on_server(server: Path, mouse: str, day: str) -> list[Session]:
    pattern = re.compile(rf"^{re.escape(mouse)}_{day}_(\d{{6}})$")
    try:
        names = sorted(p.name for p in (server / mouse / "Training").iterdir() if p.is_dir())
    except OSError:
        return []
    out = []
    for name in names:
        m = pattern.match(name)
        if m:
            out.append(Session(mouse, name, datetime.strptime(day + m.group(1), "%Y%m%d%H%M%S")))
    return out


def find_items(kind: str, root: Path, mouse: str, day: str, sessions: list[Session], now: float) -> list[Item]:
    files = []
    for folder in day_folders(root, mouse, day):
        for p in folder.iterdir():
            if p.is_file() and p.suffix.lower() in EXTENSIONS[kind]:
                st = p.stat()
                files.append((p, st.st_size, st.st_mtime))
    files.sort(key=lambda f: (f[2], f[0].name))
    items = []
    for p, size, saved in files:
        started = [s for s in sessions if s.start <= datetime.fromtimestamp(saved)]
        items.append(Item(kind, p, size, saved, started[-1] if started else None,
                          ready=now - saved >= SETTLE_S))
    for s in sessions:  # tick the file when it is the only one of its kind for the session
        mine = [i for i in items if i.session is s]
        if len(mine) == 1 and mine[0].ready:
            mine[0].selected = True
    return items


# --- names --------------------------------------------------------------------------

def plan(items: list[Item], server: Path) -> tuple[list[tuple[Item, Path]], list[str]]:
    """(ticked items with their full server path, errors that block the copy)."""
    out, errors = [], []
    ticked = [i for i in items if i.selected and i.ready and i.session]
    sessions = []
    for i in ticked:
        if i.session not in sessions:
            sessions.append(i.session)
    for s in sessions:
        tifs = sorted((i for i in ticked if i.session is s and i.kind == "imaging"), key=lambda i: (i.saved, i.source.name))
        for k, i in enumerate(tifs, 1):
            name = f"{s.name}.tiff" if len(tifs) == 1 else f"{s.name}_{k}.tiff"
            out.append((i, server / s.mouse / "Recording" / "Imaging" / s.name / name))
        for kind in ("topview", "sideview"):
            vids = [i for i in ticked if i.session is s and i.kind == kind]
            if len(vids) > 1:
                errors.append(f"{s.name}: {len(vids)} {kind} files ticked, tick only one")
                continue
            for i in vids:
                name = f"{s.name}_{kind}{i.source.suffix.lower()}"
                out.append((i, server / s.mouse / "Recording" / "Video" / s.name / name))
    return out, errors


def show(items: list[Item], server: Path, say=print) -> None:
    targets = {id(i): t for i, t in plan(items, server)[0]}
    current = "-"
    for n, i in enumerate(items, 1):
        name = i.session.name if i.session else None
        if name != current:
            current = name
            say("")
            say(f"Session {name}" if name else "Not matched (saved before the first session of the day):")
        when = datetime.fromtimestamp(i.saved).strftime("%H:%M")
        box = "[x]" if id(i) in targets else "[ ]"
        say(f"  {box} {n:>2}  {i.kind:<8} {i.source}  ({size_text(i.size)}, saved {when})")
        if not i.ready:
            say("           changed less than a minute ago (still recording?): not copied")
        elif id(i) in targets:
            say(f"           goes to {targets[id(i)]}")


# --- copying ------------------------------------------------------------------------

def keep_awake(on: bool) -> None:
    """Ask Windows not to sleep while copying (no admin rights needed)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        es_continuous, es_system_required = 0x80000000, 0x00000001
        flags = es_continuous | es_system_required if on else es_continuous
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


class Progress:
    def __init__(self, total: int, say=print):
        self.total, self.done, self.say = total, 0, say
        self.t0 = self.last = time.monotonic()

    def add(self, n: int, label: str, force: bool = False) -> None:
        self.done += n
        now = time.monotonic()
        if not force and now - self.last < 1:
            return
        self.last = now
        text = f"\r  {label}: {size_text(self.done)} of {size_text(self.total)}"
        elapsed = now - self.t0
        if elapsed > 3 and self.done:
            rate = self.done / elapsed
            left = max(self.total - self.done, 0) / rate
            h, m = divmod(round(left / 60), 60)
            text += f", {size_text(rate)}/s, about {f'{h} h {m:02d} min' if h else f'{max(m, 1)} min'} left"
        sys.stdout.write(text.ljust(110))
        sys.stdout.flush()


def copy_one(src: Path, dst: Path, size: int, progress: Progress, stop: dict) -> str:
    """'copied', 'skipped' (same size already there) or a problem. Never overwrites."""
    try:
        if dst.exists():
            have = dst.stat().st_size
            progress.add(size, dst.name)
            if have == size:
                return "skipped"
            return f"already on the server with another size ({have} vs {size} bytes); left as it is"
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        progress.add(size, dst.name)
        return f"error: {exc}"
    written = 0
    created = False
    try:
        with src.open("rb") as fin, dst.open("xb") as fout:  # "x": never overwrite
            created = True
            while True:
                if stop.get("now"):
                    raise KeyboardInterrupt
                chunk = fin.read(CHUNK)
                if not chunk:
                    break
                fout.write(chunk)
                written += len(chunk)
                progress.add(len(chunk), dst.name)
        if written != size or dst.stat().st_size != size:
            raise OSError(f"size on the server does not match ({written} of {size} bytes)")
    except KeyboardInterrupt:
        return "stopped in the middle: a partial file is on the server and cannot be removed from here"
    except OSError as exc:
        progress.add(size - written, dst.name)
        partial = " (a partial file may be on the server; it cannot be removed from here)" if created else ""
        return f"error: {exc}{partial}"
    return "copied"


def write_log(log: Path, rows: list[tuple]) -> None:
    new = not log.exists()
    with log.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["when", "source", "destination", "bytes", "result"])
        w.writerows(rows)


def copy_all(todo: list[tuple[Item, Path]], log: Path, say=print) -> list[str]:
    stop: dict = {}

    def on_ctrl_c(*_):
        if stop.get("after"):
            stop["now"] = True
        else:
            stop["after"] = True
            say("\n  Ctrl+C: stopping after this file (press again to stop now and leave a partial file).")

    old = signal.signal(signal.SIGINT, on_ctrl_c)
    progress = Progress(sum(i.size for i, _ in todo), say)
    problems, rows = [], []
    keep_awake(True)
    try:
        for n, (item, dst) in enumerate(todo, 1):
            if stop.get("after"):
                problems.append("stopped by Ctrl+C before the remaining files")
                break
            result = copy_one(item.source, dst, item.size, progress, stop)
            progress.add(0, dst.name, force=True)
            say(f"\n  {n}/{len(todo)} {dst.name}: {result}")
            rows.append((datetime.now().isoformat(timespec="seconds"), str(item.source), str(dst), item.size, result))
            if result not in ("copied", "skipped"):
                problems.append(f"{dst}: {result}")
    finally:
        keep_awake(False)
        signal.signal(signal.SIGINT, old)
        try:
            write_log(log, rows)
        except OSError as exc:
            say(f"Could not write the log {log}: {exc}")
    return problems


# --- main ---------------------------------------------------------------------------

def main(argv: list[str] | None = None, ask=input, say=print, now: float | None = None) -> int:
    ap = argparse.ArgumentParser(description="Copy imaging and videos to the server, renamed after the session.")
    ap.add_argument("--date", default=date.today().strftime("%Y%m%d"), help="YYYYMMDD (default: today)")
    ap.add_argument("--mouse", action="append", help="only this mouse (can be given several times)")
    ap.add_argument("--server", default=SERVER)
    for kind in SOURCES:
        ap.add_argument(f"--{kind}", default=SOURCES[kind], help=f"folder holding <mouse>/<date>/ ('' to skip)")
    ap.add_argument("--log", default=str(LOG))
    args = ap.parse_args(argv)
    day = args.date
    if not re.fullmatch(r"\d{8}", day):
        say(f"--date must be YYYYMMDD, not {day!r}")
        return 2
    now = time.time() if now is None else now
    server = Path(args.server)
    sources = {k: getattr(args, k) for k in SOURCES if getattr(args, k)}

    # 1. access
    say("Checking folders:")
    ok_server = server.is_dir()
    say(f"  server    {'OK ' if ok_server else 'NOT REACHABLE'}  {server}")
    reachable = {}
    for kind, root in sources.items():
        ok = can_read(root)
        say(f"  {kind:<9} {'OK ' if ok else 'NOT REACHABLE'}  {root}")
        if ok:
            reachable[kind] = root
    if not ok_server:
        say("Cannot reach the server. Is the network drive connected?")
        return 1
    if len(reachable) < len(sources):
        if ask("Some folders cannot be read. Go on without them? [y/N] ").strip().lower() != "y":
            return 1

    # 2-4. sessions and files
    mice = args.mouse or mice_with_data(reachable, day)
    if not mice:
        say(f"No <mouse>/{day} folder in the sources: nothing to copy.")
        return 0
    items: list[Item] = []
    for mouse in mice:
        sessions = sessions_on_server(server, mouse, day)
        found = []
        for kind, root in reachable.items():
            try:
                found += find_items(kind, Path(root), mouse, day, sessions, now)
            except OSError as exc:
                say(f"\n{mouse}: cannot read the {kind} folder ({exc}); left out.")
        if not found:
            continue
        if not sessions:
            say(f"\n{mouse}: recordings found but no behaviour session of {day} in {server / mouse / 'Training'}."
                " Copy the behaviour session first.")
            continue
        items += sorted(found, key=lambda i: (i.session.start if i.session else datetime.min, i.kind != "imaging",
                                              i.kind, i.saved))
    if not items:
        say("Nothing to copy.")
        return 0

    # 5. choose
    while True:
        show(items, server, say)
        todo, errors = plan(items, server)
        say("")
        for e in errors:
            say(f"  ! {e}")
        say(f"  {len(todo)} file(s) ticked, {size_text(sum(i.size for i, _ in todo))}.")
        answer = ask("Numbers to tick/untick (e.g. '2 3'), Enter to go on, q to quit: ").strip().lower()
        if answer == "q":
            return 0
        if not answer:
            if errors:
                say("Fix the problems above first.")
                continue
            break
        for word in answer.replace(",", " ").split():
            if word.isdigit() and 1 <= int(word) <= len(items):
                i = items[int(word) - 1]
                if i.ready and i.session:
                    i.selected = not i.selected
                else:
                    say(f"  {word} cannot be ticked ({'still recording?' if not i.ready else 'no session'})")
            else:
                say(f"  '{word}' is not a file number")
    if not todo:
        say("Nothing ticked.")
        return 0
    total = sum(i.size for i, _ in todo)
    try:
        free = shutil.disk_usage(server).free
    except OSError:
        free = None
    if free is not None and free < total:
        say(f"Not enough space on the server: {size_text(total)} to copy, {size_text(free)} free.")
        return 1
    if ask(f"Copy {len(todo)} file(s), {size_text(total)}, to the server? Names cannot be changed there. [y/N] "
           ).strip().lower() != "y":
        return 0

    # 6. copy
    problems = copy_all(todo, Path(args.log), say)
    say("")
    if problems:
        say("Problems:")
        for p in problems:
            say(f"  {p}")
        return 1
    say(f"Done. Log: {args.log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
