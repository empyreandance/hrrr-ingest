"""Sanity checks on the parameter catalog."""

from __future__ import annotations

from hrrr_ingest.catalog import CATALOG, native_parameters
from hrrr_ingest.compute.units import CONVERTERS


def test_parameter_ids_unique():
    params = native_parameters()
    ids = [p.parameter_id for p in params]
    assert len(ids) == len(set(ids))
    assert CATALOG.keys() == set(ids)


def test_every_filter_specifies_type_of_level():
    # dgz_thickness (layth) is the one exception: eccodes reports its level type
    # as "unknown", so it is selected by shortName alone (verified to extract).
    for p in native_parameters():
        assert "typeOfLevel" in p.filters or "shortName" in p.filters, p.parameter_id


def test_conversions_reference_known_converters():
    for p in native_parameters():
        if p.convert is not None:
            assert p.convert in CONVERTERS, f"{p.parameter_id}: {p.convert}"


def test_categorical_params_flagged():
    cats = [p for p in native_parameters() if p.param_class == "categorical"]
    assert {p.parameter_id for p in cats} == {"cat_snow", "cat_icep", "cat_frzr", "cat_rain"}


def test_3d_fields_marked():
    threed = {p.parameter_id for p in native_parameters() if p.is_3d}
    assert "t_pressure" in threed and "hgt_pressure" in threed
    # 3D fields select by shortName on the isobaric level type
    for pid in threed:
        assert CATALOG[pid].filters["typeOfLevel"] == "isobaricInhPa"
