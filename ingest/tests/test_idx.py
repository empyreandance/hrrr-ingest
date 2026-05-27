"""Tests for the wgrib2 .idx inventory parser."""

from __future__ import annotations

from hrrr_ingest.idx import parse_idx, select

SAMPLE = (
    "1:0:d=2024010100:HGT:50 mb:anl:\n"
    "2:731414:d=2024010100:TMP:50 mb:anl:\n"
    "3:1285782:d=2024010100:CLMR:50 mb:anl:\n"
    "4:1436730:d=2024010100:UGRD:10 m above ground:anl:\n"
)


def test_parses_all_rows():
    entries = parse_idx(SAMPLE)
    assert len(entries) == 4
    assert entries[0].var == "HGT"
    assert entries[3].var == "UGRD"
    assert entries[3].level == "10 m above ground"


def test_byte_ranges_chain_to_next_offset():
    entries = parse_idx(SAMPLE)
    assert entries[0].start == 0
    assert entries[0].end == 731414
    assert entries[0].http_range == "bytes=0-731413"


def test_last_entry_has_open_ended_range():
    entries = parse_idx(SAMPLE)
    assert entries[-1].end is None
    assert entries[-1].http_range == "bytes=1436730-"


def test_select_predicate():
    entries = parse_idx(SAMPLE)
    hydro = select(entries, lambda e: e.var == "CLMR")
    assert len(hydro) == 1 and hydro[0].msg == 3


def test_blank_lines_ignored():
    assert parse_idx("\n\n" + SAMPLE + "\n") == parse_idx(SAMPLE)
