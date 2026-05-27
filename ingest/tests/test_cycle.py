"""Tests for cycle math (the one piece of real orchestration logic so far)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hrrr_ingest.cycle import (
    EXTENDED_MAX_FH,
    STANDARD_MAX_FH,
    Cycle,
    current_cycle,
    parse_cycle_id,
)


def _utc(y, m, d, h):
    return datetime(y, m, d, h, tzinfo=UTC)


class TestForecastHourRanges:
    @pytest.mark.parametrize("hour", [0, 6, 12, 18])
    def test_extended_cycles_go_to_f48(self, hour):
        cycle = Cycle(init=_utc(2026, 5, 24, hour))
        assert cycle.is_extended
        assert cycle.max_forecast_hour == EXTENDED_MAX_FH
        assert cycle.forecast_hours == list(range(0, 49))

    @pytest.mark.parametrize("hour", [1, 5, 11, 17, 23])
    def test_standard_cycles_stop_at_f18(self, hour):
        cycle = Cycle(init=_utc(2026, 5, 24, hour))
        assert not cycle.is_extended
        assert cycle.max_forecast_hour == STANDARD_MAX_FH
        assert cycle.forecast_hours == list(range(0, 19))


class TestGribKey:
    def test_key_format(self):
        cycle = Cycle(init=_utc(2026, 5, 24, 0))
        assert cycle.grib_key(6) == "hrrr.20260524/conus/hrrr.t00z.wrfprsf06.grib2"

    def test_key_zero_pads_hour_and_fh(self):
        cycle = Cycle(init=_utc(2026, 5, 24, 3))
        assert cycle.grib_key(0) == "hrrr.20260524/conus/hrrr.t03z.wrfprsf00.grib2"

    def test_out_of_range_forecast_hour_rejected(self):
        standard = Cycle(init=_utc(2026, 5, 24, 1))  # f00-f18 only
        with pytest.raises(ValueError):
            standard.grib_key(24)


class TestCurrentCycle:
    def test_default_lag_picks_previous_hour(self):
        # Cron fires at init+65min: at 01:05Z the available cycle is 00Z.
        cycle = current_cycle(now=_utc(2026, 5, 24, 1).replace(minute=5))
        assert cycle.cycle_id == "2026052400"

    def test_truncates_to_top_of_hour(self):
        cycle = current_cycle(now=_utc(2026, 5, 24, 14).replace(minute=37))
        # 14:37 - 65min = 13:32 -> truncated to 13Z
        assert cycle.cycle_id == "2026052413"

    def test_crosses_midnight(self):
        cycle = current_cycle(now=_utc(2026, 5, 24, 0).replace(minute=30))
        # 00:30 - 65min = previous day 23:25 -> 23Z previous day
        assert cycle.cycle_id == "2026052323"

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError):
            current_cycle(now=datetime(2026, 5, 24, 12))


class TestParseCycleId:
    def test_roundtrip(self):
        assert parse_cycle_id("2026052400").cycle_id == "2026052400"

    @pytest.mark.parametrize("bad", ["2026", "20260524", "not-a-cycle", "202605240"])
    def test_rejects_malformed(self, bad):
        with pytest.raises(ValueError):
            parse_cycle_id(bad)


class TestCycleValidation:
    def test_rejects_naive_init(self):
        with pytest.raises(ValueError):
            Cycle(init=datetime(2026, 5, 24, 12))

    def test_rejects_sub_hour_precision(self):
        with pytest.raises(ValueError):
            Cycle(init=datetime(2026, 5, 24, 12, 30, tzinfo=UTC))
