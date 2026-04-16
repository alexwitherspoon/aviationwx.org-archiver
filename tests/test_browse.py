"""Unit tests for app.browse lazy listing helpers."""

import os

from app.browse import (
    build_preview_images,
    index_child_file_counts,
    index_list_all_filenames,
    paginate_list,
    parse_browse_path,
    safe_browse_segments,
    scandir_child_names,
    scandir_list_filenames,
)


def test_parse_browse_path_empty():
    assert parse_browse_path("") == ()
    assert parse_browse_path(None) == ()


def test_parse_browse_path_segments():
    assert parse_browse_path("KSEA") == ("KSEA",)
    assert parse_browse_path("KSEA/2024/06/15/cam") == (
        "KSEA",
        "2024",
        "06",
        "15",
        "cam",
    )


def test_parse_browse_path_rejects_traversal():
    try:
        parse_browse_path("a/../b")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_index_child_file_counts_airport_level():
    files = {
        "KSEA/2024/01/01/cam/a.jpg": {"size": 1},
        "KSEA/2024/01/01/cam/b.jpg": {"size": 1},
        "KPWT/2024/01/01/cam/a.jpg": {"size": 1},
    }
    c = index_child_file_counts(files, ())
    assert c["KSEA"] == 2
    assert c["KPWT"] == 1


def test_index_child_file_counts_year_level():
    files = {
        "KSEA/2024/01/01/cam/a.jpg": {"size": 1},
        "KSEA/2023/01/01/cam/a.jpg": {"size": 1},
    }
    c = index_child_file_counts(files, ("KSEA",))
    assert c["2024"] == 1
    assert c["2023"] == 1


def test_index_list_all_filenames():
    files = {
        "KSEA/2024/01/01/cam/z.jpg": {"size": 1},
        "KSEA/2024/01/01/cam/a.jpg": {"size": 1},
    }
    names = index_list_all_filenames(files, ("KSEA", "2024", "01", "01", "cam"))
    assert names == ["a.jpg", "z.jpg"]


def test_index_list_all_filenames_os_joined_keys():
    """Index keys from os.path.join match the same logical path as slash form."""
    key = os.path.join("KSEA", "2024", "01", "01", "cam", "x.jpg")
    files = {key: {"size": 1}}
    names = index_list_all_filenames(files, ("KSEA", "2024", "01", "01", "cam"))
    assert names == ["x.jpg"]


def test_paginate_list():
    items = ["a", "b", "c", "d"]
    assert paginate_list(items, 0, 2) == (4, ["a", "b"])
    assert paginate_list(items, 2, 2) == (4, ["c", "d"])
    assert paginate_list(items, 10, 2) == (4, [])


def test_build_preview_images_truncation():
    files = [f"{i}.jpg" for i in range(10)]
    prev, trunc = build_preview_images(
        files, ("KSEA", "2024", "01", "01", "cam"), preview_limit=3
    )
    assert trunc is True
    assert len(prev) == 3
    assert prev[0]["filename"] == "0.jpg"
    assert prev[2]["index"] == 2


def test_scandir_roundtrip_tmpdir(tmp_path):
    cam = tmp_path / "KSEA" / "2024" / "01" / "01" / "north"
    cam.mkdir(parents=True)
    (cam / "x.jpg").write_bytes(b"x")
    (cam / "notes.txt").write_text("hi")
    root = str(tmp_path)
    assert scandir_child_names(root, ()) == ["KSEA"]
    assert scandir_child_names(root, ("KSEA",)) == ["2024"]
    assert scandir_list_filenames(root, ("KSEA", "2024", "01", "01", "north")) == [
        "notes.txt",
        "x.jpg",
    ]


def test_safe_browse_segments():
    assert safe_browse_segments(("KSEA", "2024")) is True
    assert safe_browse_segments(("bad..name",)) is False
