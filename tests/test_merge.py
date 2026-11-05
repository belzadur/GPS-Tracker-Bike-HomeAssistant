"""Merging stored trips into one."""

import pathlib

from bt.const import ACTIVITY_BIKE
from bt.merge import merge_trips
from bt.storage import TripStore
from bt.tracker import TrackerConfig, TripTracker
from simulator import standstill, straight_track

T0 = 1_700_000_000.0
LAT, LON = 52.5, 13.4
PAUSE_S = 1800.0


def _ride(start_ts, lon):
    finished = []
    tracker = TripTracker(TrackerConfig(), on_trip_finished=finished.append)
    for point in straight_track(start_ts, LAT, lon, 20.0, 900) + standstill(
        start_ts + 910, LAT, lon + 0.08, 400
    ):
        tracker.add_point(point)
    tracker.tick(start_ts + 99_999)
    assert len(finished) == 1
    return finished[0]


def _store_with_two_rides(tmp_path: pathlib.Path):
    store = TripStore(str(tmp_path / "test.db"))
    store.connect()
    first = _ride(T0, LON)
    # The second ride starts where the first one ended, after a pause.
    second_start = first.points[-1].ts + PAUSE_S
    second = _ride(second_start, first.points[-1].lon)
    ids = [
        store.save_trip(first, "device_tracker.phone"),
        store.save_trip(second, "device_tracker.phone"),
    ]
    return store, ids, first, second


def test_two_trips_become_one(tmp_path):
    store, ids, first, second = _store_with_two_rides(tmp_path)
    store.set_note(ids[0], "hin")
    store.set_note(ids[1], "zurück")

    merged = merge_trips(store, ids, TrackerConfig(), "device_tracker.phone")

    assert merged is not None
    assert all(store.get_trip(trip_id) is None for trip_id in ids)
    assert [trip["id"] for trip in store.list_trips()] == [merged["id"]]
    assert merged["activity"] == ACTIVITY_BIKE
    assert merged["activity_confidence"] > 0
    assert merged["note"] == "hin / zurück"
    assert len(store.get_track(merged["id"])) == len(first.points) + len(second.points)
    assert abs(merged["distance_m"] - (first.distance_m + second.distance_m)) < 0.02 * (
        first.distance_m + second.distance_m
    )


def test_the_pause_is_not_moving_time(tmp_path):
    store, ids, first, second = _store_with_two_rides(tmp_path)

    merged = merge_trips(store, ids, TrackerConfig())

    assert merged["duration_s"] >= first.duration_s + second.duration_s + PAUSE_S
    assert merged["moving_time_s"] < first.moving_time_s + second.moving_time_s + 60


def test_unknown_trip_changes_nothing(tmp_path):
    store, ids, _, _ = _store_with_two_rides(tmp_path)

    assert merge_trips(store, [ids[0], 9999], TrackerConfig()) is None
    assert sorted(trip["id"] for trip in store.list_trips()) == ids
