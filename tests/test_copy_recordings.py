"""scripts/copy_recordings.py: imaging and videos to the server, renamed after the session."""

import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "copy_recordings", Path(__file__).parents[1] / "scripts" / "copy_recordings.py")
cr = importlib.util.module_from_spec(_spec)
sys.modules["copy_recordings"] = cr  # dataclasses look the module up while it loads
_spec.loader.exec_module(cr)

DAY = "20261121"
NAME = "RD138_20261121_100820"
LATER = datetime(2026, 11, 22, 9, 0).timestamp()


def put(path, saved, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (saved.timestamp(), saved.timestamp()))
    return path


@pytest.fixture
def rig(tmp_path):
    server = tmp_path / "server"
    (server / "RD138" / "Training" / NAME).mkdir(parents=True)  # copied from the behaviour PC
    src = {k: tmp_path / k for k in ("imaging", "top", "side")}
    for p in src.values():
        p.mkdir()
    return server, src


def run(server, src, answers, tmp_path, extra=(), now=LATER):
    said, asked = [], list(answers)
    argv = ["--date", DAY, "--server", str(server), "--imaging", str(src["imaging"]),
            "--topview", str(src["top"]), "--sideview", str(src["side"]), "--log", str(tmp_path / "log.csv"), *extra]
    code = cr.main(argv, ask=lambda _: asked.pop(0), say=said.append, now=now)
    return code, "\n".join(said)


def test_copies_imaging_and_videos_renamed(rig, tmp_path):
    server, src = rig
    tif = put(src["imaging"] / "RD138" / DAY / "file_00001.tif", datetime(2026, 11, 21, 10, 52), b"tif" * 50)
    put(src["top"] / "RD138" / DAY / "cam0.avi", datetime(2026, 11, 21, 10, 50), b"top")
    put(src["side"] / "RD138" / DAY[2:] / "cam1.AVI", datetime(2026, 11, 21, 10, 51), b"side")  # YYMMDD works too

    code, out = run(server, src, ["", "y"], tmp_path)
    assert code == 0, out
    imaging = server / "RD138" / "Recording" / "Imaging" / NAME / f"{NAME}.tiff"
    video = server / "RD138" / "Recording" / "Video" / NAME
    assert imaging.read_bytes() == b"tif" * 50
    assert (video / f"{NAME}_topview.avi").read_bytes() == b"top"
    assert (video / f"{NAME}_sideview.avi").read_bytes() == b"side"
    assert tif.is_file()  # originals untouched
    assert str(tif) in out and f"goes to {imaging}" in out  # full paths shown
    assert (tmp_path / "log.csv").read_text(encoding="utf-8").count("copied") == 3

    code, out = run(server, src, ["", "y"], tmp_path)  # again: everything already there
    assert code == 0 and out.count(": skipped") == 3


def test_several_tifs_are_listed_unticked_and_numbered_when_ticked(rig, tmp_path):
    server, src = rig
    put(src["imaging"] / "RD138" / DAY / "fov.tif", datetime(2026, 11, 21, 9, 0))  # before the session
    put(src["imaging"] / "RD138" / DAY / "b.tif", datetime(2026, 11, 21, 11, 30), b"b")
    put(src["imaging"] / "RD138" / DAY / "a.tif", datetime(2026, 11, 21, 11, 0), b"a")
    # items: 1 fov (not matched), 2 a, 3 b
    code, out = run(server, src, ["1", "2 3", "", "y"], tmp_path)
    assert code == 0, out
    assert "1 cannot be ticked (no session)" in out
    folder = server / "RD138" / "Recording" / "Imaging" / NAME
    assert sorted(p.name for p in folder.iterdir()) == [f"{NAME}_1.tiff", f"{NAME}_2.tiff"]
    assert (folder / f"{NAME}_2.tiff").read_bytes() == b"b"


def test_two_side_videos_must_be_narrowed_to_one(rig, tmp_path):
    server, src = rig
    put(src["side"] / "RD138" / DAY / "s1.avi", datetime(2026, 11, 21, 10, 30), b"1")
    put(src["side"] / "RD138" / DAY / "s2.avi", datetime(2026, 11, 21, 10, 40), b"2")
    code, out = run(server, src, ["1 2", "", "1", "", "y"], tmp_path)
    assert code == 0, out
    assert "2 sideview files ticked, tick only one" in out and "Fix the problems above first." in out
    assert (server / "RD138" / "Recording" / "Video" / NAME / f"{NAME}_sideview.avi").read_bytes() == b"2"


def test_file_still_recording_is_not_copied(rig, tmp_path):
    server, src = rig
    put(src["top"] / "RD138" / DAY / "cam0.avi", datetime(2026, 11, 21, 11, 0))
    code, out = run(server, src, ["1", ""], tmp_path, now=datetime(2026, 11, 21, 11, 0, 20).timestamp())
    assert "still recording?" in out and "Nothing ticked." in out
    assert not (server / "RD138" / "Recording").exists()


def test_needs_the_behaviour_session_on_the_server(rig, tmp_path):
    server, src = rig
    put(src["imaging"] / "RD140" / DAY / "a.tif", datetime(2026, 11, 21, 11, 0))
    code, out = run(server, src, [], tmp_path)
    assert "RD140: recordings found but no behaviour session" in out and "Nothing to copy." in out


def test_never_replaces_a_file_on_the_server(rig, tmp_path):
    server, src = rig
    put(src["top"] / "RD138" / DAY / "cam0.avi", datetime(2026, 11, 21, 10, 50), b"top video")
    dst = server / "RD138" / "Recording" / "Video" / NAME / f"{NAME}_topview.avi"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"partial")
    code, out = run(server, src, ["", "y"], tmp_path)
    assert code == 1 and "another size" in out
    assert dst.read_bytes() == b"partial"


def test_unreachable_folder_asks_and_no_copy_without_yes(rig, tmp_path):
    server, src = rig
    put(src["imaging"] / "RD138" / DAY / "a.tif", datetime(2026, 11, 21, 11, 0))
    src["top"] = tmp_path / "pc_off"
    code, out = run(server, src, ["n"], tmp_path)
    assert code == 1 and "NOT REACHABLE" in out
    code, out = run(server, src, ["y", "", "n"], tmp_path)  # go on, then answer no to the copy
    assert code == 0 and not (server / "RD138" / "Recording").exists()


def test_server_not_reachable(rig, tmp_path):
    _, src = rig
    code, out = run(tmp_path / "offline", src, [], tmp_path)
    assert code == 1 and "Cannot reach the server" in out
