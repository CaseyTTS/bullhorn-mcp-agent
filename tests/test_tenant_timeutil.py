"""AC-21: UTC coercion primitive and timezone validation (D-4A-11)."""

import math
import zoneinfo

import pytest

from bullhorn_mcp.tenant.timeutil import (
    MAX_EPOCH_MILLIS,
    MIN_EPOCH_MILLIS,
    coerce_epoch_millis_to_utc_iso,
    format_utc,
    normalize_timestamp,
    parse_utc,
    validate_timezone,
)


def _has_tz_database() -> bool:
    try:
        zoneinfo.ZoneInfo("America/New_York")
    except Exception:
        return False
    return True


class TestCoerceEpochMillis:
    def test_epoch_zero(self):
        assert coerce_epoch_millis_to_utc_iso(0) == "1970-01-01T00:00:00Z"

    def test_millis_kept(self):
        assert coerce_epoch_millis_to_utc_iso(1530815115887) == "2018-07-05T18:25:15.887Z"

    def test_negative(self):
        assert coerce_epoch_millis_to_utc_iso(-1) == "1969-12-31T23:59:59.999Z"

    def test_range_edges(self):
        assert coerce_epoch_millis_to_utc_iso(MIN_EPOCH_MILLIS) == "0001-01-01T00:00:00Z"
        assert coerce_epoch_millis_to_utc_iso(MAX_EPOCH_MILLIS) == "9999-12-31T23:59:59.999Z"

    @pytest.mark.parametrize(
        "value",
        [True, False, 1.0, math.nan, math.inf, -math.inf, "0", None, [], {}, b"0", MAX_EPOCH_MILLIS + 1, MIN_EPOCH_MILLIS - 1, 10**400],
        ids=repr,
    )
    def test_rejects_only_with_value_error(self, value):
        with pytest.raises(ValueError) as info:
            coerce_epoch_millis_to_utc_iso(value)
        assert type(info.value) is ValueError
        assert len(str(info.value)) < 200

    def test_int_subclass_rejected(self):
        class Sneaky(int):
            pass

        with pytest.raises(ValueError):
            coerce_epoch_millis_to_utc_iso(Sneaky(5))


class TestValidateTimezone:
    def test_utc_passes(self):
        assert validate_timezone("UTC") == "UTC"

    @pytest.mark.parametrize(
        "name",
        ["Mars/Olympus_Mons", "", "../../etc/passwd", "/etc/localtime", "UTC\n", "a" * 10_000, "Europe/../UTC", "\x00", "é/ü"],
        ids=lambda v: repr(v)[:30],
    )
    def test_unknown_fails_bounded(self, name):
        with pytest.raises(ValueError) as info:
            validate_timezone(name)
        assert len(str(info.value)) < 200

    @pytest.mark.parametrize("value", [None, 1, ["UTC"], {"UTC": 1}], ids=repr)
    def test_non_string(self, value):
        with pytest.raises(ValueError):
            validate_timezone(value)

    @pytest.mark.skipif(not _has_tz_database(), reason="no IANA tz database on this host (tzdata is Phase 5)")
    def test_known_zone_passes(self):
        assert validate_timezone("America/New_York") == "America/New_York"


class TestTimestamps:
    def test_round_trip(self):
        assert format_utc(parse_utc("2026-10-06T12:00:00Z")) == "2026-10-06T12:00:00Z"
        assert format_utc(parse_utc("2026-10-06T12:00:00.5Z")) == "2026-10-06T12:00:00.500000Z"

    @pytest.mark.parametrize("value", ["2026-13-01T00:00:00Z", "2026-10-06 12:00:00", "x", 5, None, "2026-10-06T12:00:00+00:00"])
    def test_invalid(self, value):
        assert parse_utc(value) is None
        assert normalize_timestamp(value) is None
