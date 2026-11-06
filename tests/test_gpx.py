"""GPX export/import round trip and the importer's guard rails."""

from __future__ import annotations

import pytest
from bt.gpx import GpxError, build_gpx, parse_gpx
from bt.importer import is_duplicate, trip_from_gpx_track
from bt.tracker import TrackerConfig
from simulator import straight_track

START_TS = 1_720_000_000.0

GPX_MINIMAL = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>Feierabendrunde</name>
    <type>cycling</type>
    <trkseg>
      <trkpt lat="49.0000" lon="12.0000">
        <ele>340.0</ele><time>2024-07-03T16:00:00Z</time>
      </trkpt>
      <trkpt lat="49.0000" lon="12.0020">
        <ele>352.0</ele><time>2024-07-03T16:00:30Z</time>
      </trkpt>
      <trkpt lat="49.0000" lon="12.0040">
        <ele>361.0</ele><time>2024-07-03T16:01:00Z</time>
      </trkpt>
    </trkseg>
  </trk>
</gpx>
"""

GPX_NO_TIMES = """<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Geplante Route</name><trkseg>
    <trkpt lat="49.0" lon="12.0"><ele>340</ele></trkpt>
    <trkpt lat="49.0" lon="12.01"><ele>360</ele></trkpt>
  </trkseg></trk>
</gpx>
"""


def config() -> TrackerConfig:
    return TrackerConfig(min_distance_m=0, min_duration_s=0)


# --- parsing ------------------------------------------------------------


def test_parse_reads_points_name_and_activity():
    tracks = parse_gpx(GPX_MINIMAL)

    assert len(tracks) == 1
    assert tracks[0].name == "Feierabendrunde"
    assert tracks[0].activity == "bike"
    assert len(tracks[0].points) == 3
    assert tracks[0].points[0].alt == 340.0
    assert tracks[0].has_times


def test_parse_handles_gpx_1_0_namespace():
    xml = GPX_MINIMAL.replace(
        "http://www.topografix.com/GPX/1/1", "http://www.topografix.com/GPX/1/0"
    )

    assert len(parse_gpx(xml)[0].points) == 3


def test_parse_merges_multiple_segments():
    xml = GPX_MINIMAL.replace(
        "</trkseg>",
        '</trkseg><trkseg><trkpt lat="49.0" lon="12.006">'
        "<time>2024-07-03T16:01:30Z</time></trkpt></trkseg>",
        1,
    )

    assert len(parse_gpx(xml)[0].points) == 4


def test_parse_rejects_non_gpx():
    with pytest.raises(GpxError):
        parse_gpx("<html><body>nope</body></html>")


def test_parse_rejects_broken_xml():
    with pytest.raises(GpxError):
        parse_gpx("<gpx><trk>")


def test_parse_rejects_a_file_without_track_points():
    with pytest.raises(GpxError):
        parse_gpx('<gpx version="1.1"><metadata/></gpx>')


# --- import -------------------------------------------------------------


def test_import_builds_a_trip_with_distance_and_duration():
    track = parse_gpx(GPX_MINIMAL)[0]

    trip = trip_from_gpx_track(track, config())

    assert trip.activity == "bike"
    assert trip.duration_s == pytest.approx(60.0)
    assert trip.distance_m > 250  # ~292 m at 49 deg N
    assert trip.elevation_gain_m >= 0


def test_import_rejects_a_planned_route_without_timestamps():
    track = parse_gpx(GPX_NO_TIMES)[0]

    with pytest.raises(GpxError, match="no timestamps"):
        trip_from_gpx_track(track, config())


def test_import_can_pin_the_activity():
    track = parse_gpx(GPX_MINIMAL)[0]

    trip = trip_from_gpx_track(track, config(), activity="walk")

    assert trip.activity == "walk"


def test_import_sorts_points_that_arrive_out_of_order():
    xml = GPX_MINIMAL.replace("16:00:00Z", "16:02:00Z")
    track = parse_gpx(xml)[0]

    trip = trip_from_gpx_track(track, config())

    timestamps = [p.ts for p in trip.points]
    assert timestamps == sorted(timestamps)


# --- round trip ---------------------------------------------------------


def test_export_then_import_preserves_the_ride():
    points = straight_track(
        START_TS, 49.0, 12.0, speed_kmh=22.0, duration_s=900, altitude_start=340.0
    )
    stored_trip = {"started_at": START_TS, "activity": "bike"}
    stored_track = [
        {"ts": p.ts, "lat": p.lat, "lon": p.lon, "alt": p.alt, "speed_kmh": 0.0}
        for p in points
    ]

    xml = build_gpx(stored_trip, stored_track)
    reimported = trip_from_gpx_track(parse_gpx(xml)[0], config())

    assert len(reimported.points) == len(points)
    assert reimported.duration_s == pytest.approx(900.0)
    # 22 km/h for 15 minutes is 5.5 km.
    assert reimported.distance_m == pytest.approx(5500.0, rel=0.02)


# --- duplicate detection ------------------------------------------------


def test_duplicate_detection_matches_a_ride_that_starts_at_the_same_time():
    trip = trip_from_gpx_track(parse_gpx(GPX_MINIMAL)[0], config())
    existing = [{"started_at": trip.started_at + 30.0}]

    assert is_duplicate(trip, existing) is True


def test_duplicate_detection_lets_a_different_ride_through():
    trip = trip_from_gpx_track(parse_gpx(GPX_MINIMAL)[0], config())
    existing = [{"started_at": trip.started_at + 3600.0}]

    assert is_duplicate(trip, existing) is False


def test_duplicate_detection_with_nothing_stored_yet():
    trip = trip_from_gpx_track(parse_gpx(GPX_MINIMAL)[0], config())

    assert is_duplicate(trip, []) is False
