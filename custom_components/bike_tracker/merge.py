"""Merge several stored trips into one.

Pure Python like ``storage.py`` and ``tracker.py``, so it can be tested
without Home Assistant.
"""

from __future__ import annotations

from typing import Any

from .storage import TripStore
from .tracker import GpsPoint, TrackerConfig, trip_from_points


def merge_trips(
    store: TripStore,
    trip_ids: list[int],
    cfg: TrackerConfig,
    source_entity: str | None = None,
) -> dict[str, Any] | None:
    """Join the tracks of ``trip_ids`` into one new trip.

    The points of all trips are taken in time order and recalculated with
    ``trip_from_points`` (distance, duration, moving time, speeds, elevation,
    activity). Notes are joined, the source trips are deleted. If all trips
    share one activity it is kept, otherwise the classifier decides again.

    Returns the new trip, or ``None`` if one of the ids does not exist.
    """
    ids = sorted(set(trip_ids))
    trips = [store.get_trip(trip_id) for trip_id in ids]
    if any(trip is None for trip in trips):
        return None
    ordered = sorted(trips, key=lambda trip: trip["started_at"])

    points: list[GpsPoint] = []
    for index, source in enumerate(ordered):
        for number, row in enumerate(store.get_track(source["id"])):
            # First fix after the pause between two trips: derive its speed
            # from the gap, otherwise the pause counts as moving time.
            gap_start = index > 0 and number == 0
            points.append(
                GpsPoint(
                    ts=row["ts"],
                    lat=row["lat"],
                    lon=row["lon"],
                    alt=row["alt"],
                    accuracy=row["accuracy"],
                    reported_speed_kmh=None if gap_start else row["speed_kmh"],
                )
            )
    points.sort(key=lambda point: point.ts)

    activities = {trip["activity"] for trip in trips}
    activity = next(iter(activities)) if len(activities) == 1 else None
    merged = trip_from_points(points, cfg, activity)
    if activity is not None:
        # A pinned activity would otherwise end up with confidence 0.
        merged.activity_confidence = max(
            trip["activity_confidence"] or 0.0 for trip in trips
        )

    new_id = store.save_trip(merged, source_entity, True)
    notes = [trip["note"] for trip in ordered if trip["note"]]
    if notes:
        store.set_note(new_id, " / ".join(notes))
    for trip_id in ids:
        store.delete_trip(trip_id)
    return store.get_trip(new_id)
