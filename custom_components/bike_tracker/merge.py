"""Merge several stored trips into one.

Pure Python like ``storage.py`` and ``tracker.py``, so it can be tested
without Home Assistant.
"""

from __future__ import annotations

from typing import Any

from .storage import TripStore
from .tracker import GpsPoint, TrackerConfig, trip_from_segments


def merge_trips(
    store: TripStore,
    trip_ids: list[int],
    cfg: TrackerConfig,
    source_entity: str | None = None,
) -> dict[str, Any] | None:
    """Join the tracks of ``trip_ids`` into one new trip.

    Each trip's points are recalculated as one segment of the new trip with
    ``trip_from_segments`` (distance, duration, moving time, speeds,
    elevation, activity); the gap between the trips counts as neither
    distance nor moving time, and no point is dropped because of it. Notes
    are joined, the source trips are deleted. If all trips share one
    activity it is kept, otherwise the classifier decides again.

    Returns the new trip, or ``None`` if one of the ids does not exist.
    """
    ids = sorted(set(trip_ids))
    trips = [store.get_trip(trip_id) for trip_id in ids]
    if any(trip is None for trip in trips):
        return None
    ordered = sorted(trips, key=lambda trip: trip["started_at"])

    segments: list[list[GpsPoint]] = [
        [
            GpsPoint(
                ts=row["ts"],
                lat=row["lat"],
                lon=row["lon"],
                alt=row["alt"],
                accuracy=row["accuracy"],
                reported_speed_kmh=row["speed_kmh"],
            )
            for row in store.get_track(source["id"])
        ]
        for source in ordered
    ]

    activities = {trip["activity"] for trip in trips}
    activity = next(iter(activities)) if len(activities) == 1 else None
    merged = trip_from_segments(segments, cfg, activity)
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
