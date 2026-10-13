from pathlib import Path

import pytest

from solarlife import profiles
from solarlife.errors import InvalidWriteError, ProfileError
from solarlife.profiles import Profile

CURRENT = [
    {"name": "battery_type", "value": "Lithium", "unit": ""},
    {"name": "float_voltage", "value": 13.6, "unit": "V"},
    {"name": "boost_voltage", "value": 14.4, "unit": "V"},
]


def test_parameter_names_are_the_twelve_polled_parameters():
    assert len(profiles.PARAMETER_NAMES) == 12
    assert profiles.PARAMETER_NAMES[0] == "battery_type" and profiles.PARAMETER_NAMES[-1] == "mt_series_manual_control_default"
    assert set(profiles.DESCRIPTIONS) == set(profiles.PARAMETER_NAMES)


def test_parse_normalises_values():
    p = profiles.parse_profile("values:\n  battery_type: agm\n  float_voltage: '13.6'\n  mt_series_manual_control_default: Off\n", default_name="x")
    assert p.name == "x"
    assert p.values == {"battery_type": "AGM", "float_voltage": 13.6, "mt_series_manual_control_default": "Off"}  # Off: YAML boolean mapped back


@pytest.mark.parametrize(
    "text, message",
    [
        ("values:\n  slave_id: 2\n", "not a profile parameter"),
        ("values:\n  clear_all: Clear\n", "not a profile parameter"),
        ("values:\n  battery_voltage_level: 12\n", "not a profile parameter"),  # read-only
        ("values:\n  nonsense: 1\n", "not a profile parameter"),
        ("values: {}\n", "non-empty"),
        ("name: x\n", "non-empty"),
        ("- a\n- b\n", "expected a mapping"),
        ("values: [", "invalid YAML"),
        ("values:\n  battery_type: AGM\nvalues2: 1\n", "unknown key"),
    ],
)
def test_parse_rejects(text, message):
    with pytest.raises(ProfileError, match=message):
        profiles.parse_profile(text)


def test_parse_reports_bad_values_with_the_source():
    with pytest.raises(InvalidWriteError, match=r"f\.yml: battery_type: 'plutonium' is not one of"):
        profiles.parse_profile("values:\n  battery_type: plutonium\n", source="f.yml")
    with pytest.raises(InvalidWriteError, match="float_voltage: 'high' is not a number"):
        profiles.parse_profile("values:\n  float_voltage: high\n")


def test_save_and_load_round_trip(tmp_path):
    p = profiles.profile_from_values(CURRENT, name="mine", description="test")
    text = profiles.dump_profile(p, device="AA:BB")
    assert text.startswith("# solarlife profile, saved ") and "# from device AA:BB" in text
    path = tmp_path / "mine.yml"
    path.write_text(text, encoding="utf-8")
    loaded = profiles.load_profile(str(path))
    assert (loaded.name, loaded.description, loaded.values) == ("mine", "test", p.values)
    assert profiles.profile_from_values([{"name": "float_voltage", "value": 13.6000001}], name="x").values == {"float_voltage": 13.6}
    # On/Off survive the YAML round trip
    on = profiles.profile_from_values([{"name": "mt_series_manual_control_default", "value": "On"}], name="x")
    assert profiles.parse_profile(profiles.dump_profile(on)).values == on.values


def test_profile_from_values_needs_something():
    with pytest.raises(ProfileError):
        profiles.profile_from_values([{"name": "battery_voltage_level", "value": 12.0}], name="x")


def test_load_by_name_and_path(tmp_path):
    assert profiles.load_profile("agm-12v").values["battery_type"] == "AGM"
    with pytest.raises(ProfileError, match="available: agm-12v"):
        profiles.load_profile("nope")
    with pytest.raises(ProfileError, match="cannot read profile"):
        profiles.load_profile(str(tmp_path / "missing.yml"))
    (tmp_path / "agm-12v").write_text("values:\n  battery_type: GEL\n")  # a plain name is never a file
    assert profiles.load_profile("agm-12v").values["battery_type"] == "AGM"


def test_premade_profiles_are_valid():
    premade = profiles.list_premade()
    assert {p.name for p in premade} == {"lead-acid-flooded-12v", "agm-12v", "gel-12v", "lifepo4-4s-12v"}
    for p in premade:
        assert p.description and p.values["system_rated_voltage_level"] == "12V"
        assert not set(p.values) - set(profiles.PARAMETER_NAMES)


def test_diff_only_lists_differences_in_register_order():
    p = Profile("p", {"float_voltage": 13.6, "boost_voltage": 14.0, "battery_type": "lithium"})
    changes = profiles.diff(p, CURRENT)
    assert [(c.name, c.old, c.new) for c in changes] == [("boost_voltage", 14.4, 14.0)]


def test_diff_tolerates_float_noise_and_flags_missing_values():
    p = Profile("p", {"float_voltage": 13.601, "equalizing_voltage": 14.6})
    changes = profiles.diff(p, CURRENT)
    assert [(c.name, c.old, c.new, c.unit) for c in changes] == [("equalizing_voltage", None, 14.6, "V")]
    assert profiles.same(13.6, 13.601) and not profiles.same(13.6, 13.62) and profiles.same("AGM", "agm")


def test_the_committed_doc_matches_the_code():
    doc = Path(__file__).parents[1] / "docs" / "parameters.md"
    text = doc.read_text(encoding="utf-8")
    assert profiles.render_doc(text) == text, "run: python -m solarlife.profiles --write docs/parameters.md"
    assert all(f"`{name}`" in text for name in profiles.PARAMETER_NAMES)


def test_render_doc_needs_markers():
    with pytest.raises(ProfileError, match="markers"):
        profiles.render_doc("no markers here")
