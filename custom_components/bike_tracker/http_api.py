"""Authenticated REST endpoints consumed by the Lovelace card.

All views require a valid Home Assistant token (the frontend supplies it
automatically via ``hass.callApi``), so no data leaves the instance
unauthenticated.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import ACTIVITIES, DOMAIN
from .geo import bounding_box, simplify
from .gpx import build_gpx
from .merge import merge_trips
from .routing import async_plan_route
from .stats import rolling_window

_LOGGER = logging.getLogger(__name__)
_VIEWS_REGISTERED = f"{DOMAIN}_views_registered"


def async_register_views(hass: HomeAssistant) -> None:
    """Register the HTTP views once."""
    if hass.data.get(_VIEWS_REGISTERED):
        return
    hass.http.register_view(TripsView)
    hass.http.register_view(TripMergeView)
    hass.http.register_view(TripDetailView)
    hass.http.register_view(TripTrackView)
    hass.http.register_view(TripGpxView)
    hass.http.register_view(StatsView)
    hass.http.register_view(CurrentView)
    hass.http.register_view(SegmentsView)
    hass.http.register_view(SegmentDetailView)
    hass.http.register_view(RouteView)
    hass.data[_VIEWS_REGISTERED] = True


def _coordinator(request: web.Request) -> Any:
    hass: HomeAssistant = request.app["hass"]
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise web.HTTPServiceUnavailable(reason="Bike Tracker not configured")
    return next(iter(entries.values()))


def _int_param(request: web.Request, name: str, default: int) -> int:
    try:
        return int(request.query.get(name, default))
    except (TypeError, ValueError):
        return default


class TripsView(HomeAssistantView):
    """GET /api/bike_tracker/trips"""

    url = "/api/bike_tracker/trips"
    name = "api:bike_tracker:trips"

    async def get(self, request: web.Request) -> web.Response:
        coordinator = _coordinator(request)
        hass: HomeAssistant = request.app["hass"]

        activity = request.query.get("activity")
        if activity and activity not in ACTIVITIES:
            return self.json_message("Unknown activity", 400)

        days = _int_param(request, "days", 0)
        start = end = None
        if days > 0:
            start, end = rolling_window(dt_util.now(), days)

        trips = await hass.async_add_executor_job(
            coordinator.store.list_trips,
            _int_param(request, "limit", 50),
            _int_param(request, "offset", 0),
            activity,
            start,
            end,
        )
        return self.json({"trips": trips, "count": len(trips)})


class TripMergeView(HomeAssistantView):
    """POST /api/bike_tracker/merge  Body: {"trip_ids": [4, 5]}"""

    url = "/api/bike_tracker/merge"
    name = "api:bike_tracker:merge"

    async def post(self, request: web.Request) -> web.Response:
        coordinator = _coordinator(request)
        hass: HomeAssistant = request.app["hass"]
        try:
            body = await request.json()
            ids = sorted({int(i) for i in body["trip_ids"]})
        except (ValueError, KeyError, TypeError):
            return self.json_message("Body needs trip_ids", 400)
        if len(ids) < 2:
            return self.json_message("Need at least two trips", 400)

        async with coordinator._lock:
            trip = await hass.async_add_executor_job(
                merge_trips,
                coordinator.store,
                ids,
                coordinator._tracker_config(),
                coordinator.source_entity,
            )
        if trip is None:
            return self.json_message("Trip not found", 404)
        await coordinator.async_refresh_statistics()
        await coordinator.async_match_segments(trip["id"], trip["activity"])
        return self.json(trip)


class TripDetailView(HomeAssistantView):
    """GET / PATCH / DELETE /api/bike_tracker/trips/{trip_id}"""

    url = "/api/bike_tracker/trips/{trip_id}"
    name = "api:bike_tracker:trip"

    async def get(self, request: web.Request, trip_id: str) -> web.Response:
        coordinator = _coordinator(request)
        hass: HomeAssistant = request.app["hass"]
        trip = await hass.async_add_executor_job(
            coordinator.store.get_trip, int(trip_id)
        )
        if trip is None:
            return self.json_message("Trip not found", 404)
        track = await hass.async_add_executor_job(
            coordinator.store.get_track, int(trip_id)
        )
        coords = [(p["lat"], p["lon"]) for p in track]
        trip["track"] = [[round(lat, 6), round(lon, 6)] for lat, lon in coords]
        trip["elevation_profile"] = _elevation_profile(track)
        trip["speed_profile"] = [
            [round(p["ts"] - track[0]["ts"], 1), p["speed_kmh"]] for p in track
        ]
        trip["bounds"] = bounding_box(coords)
        return self.json(trip)

    async def post(self, request: web.Request, trip_id: str) -> web.Response:
        """Update a trip. Body: {"activity": "...", "note": "..."}"""
        coordinator = _coordinator(request)
        hass: HomeAssistant = request.app["hass"]
        try:
            body = await request.json()
        except ValueError:
            return self.json_message("Invalid JSON", 400)

        if "activity" in body:
            if body["activity"] not in ACTIVITIES:
                return self.json_message("Unknown activity", 400)
            await hass.async_add_executor_job(
                coordinator.store.set_activity, int(trip_id), body["activity"]
            )
        if "note" in body:
            await hass.async_add_executor_job(
                coordinator.store.set_note, int(trip_id), body["note"]
            )
        if body.get("confirmed"):
            await hass.async_add_executor_job(coordinator.store.confirm, int(trip_id))

        await coordinator.async_refresh_statistics()
        trip = await hass.async_add_executor_job(
            coordinator.store.get_trip, int(trip_id)
        )
        return self.json(trip or {})

    async def delete(self, request: web.Request, trip_id: str) -> web.Response:
        coordinator = _coordinator(request)
        hass: HomeAssistant = request.app["hass"]
        ok = await hass.async_add_executor_job(
            coordinator.store.delete_trip, int(trip_id)
        )
        if not ok:
            return self.json_message("Trip not found", 404)
        await coordinator.async_refresh_statistics()
        return self.json({"deleted": int(trip_id)})


class TripTrackView(HomeAssistantView):
    """GET /api/bike_tracker/trips/{trip_id}/track?tolerance=5"""

    url = "/api/bike_tracker/trips/{trip_id}/track"
    name = "api:bike_tracker:track"

    async def get(self, request: web.Request, trip_id: str) -> web.Response:
        coordinator = _coordinator(request)
        hass: HomeAssistant = request.app["hass"]
        track = await hass.async_add_executor_job(
            coordinator.store.get_track, int(trip_id)
        )
        coords = [(p["lat"], p["lon"]) for p in track]
        tolerance = float(request.query.get("tolerance", 5.0))
        if tolerance > 0:
            coords = simplify(coords, tolerance)
        return self.json(
            {
                "points": [[round(lat, 6), round(lon, 6)] for lat, lon in coords],
                "bounds": bounding_box(coords),
            }
        )


class TripGpxView(HomeAssistantView):
    """GET /api/bike_tracker/trips/{trip_id}/gpx"""

    url = "/api/bike_tracker/trips/{trip_id}/gpx"
    name = "api:bike_tracker:gpx"

    async def get(self, request: web.Request, trip_id: str) -> web.Response:
        coordinator = _coordinator(request)
        hass: HomeAssistant = request.app["hass"]
        trip = await hass.async_add_executor_job(
            coordinator.store.get_trip, int(trip_id)
        )
        if trip is None:
            return self.json_message("Trip not found", 404)
        track = await hass.async_add_executor_job(
            coordinator.store.get_track, int(trip_id)
        )
        return web.Response(
            body=build_gpx(trip, track).encode("utf-8"),
            content_type="application/gpx+xml",
            headers={
                "Content-Disposition": f'attachment; filename="trip_{trip_id}.gpx"'
            },
        )


class StatsView(HomeAssistantView):
    """GET /api/bike_tracker/stats?days=30"""

    url = "/api/bike_tracker/stats"
    name = "api:bike_tracker:stats"

    async def get(self, request: web.Request) -> web.Response:
        coordinator = _coordinator(request)
        hass: HomeAssistant = request.app["hass"]
        days = _int_param(request, "days", 30)
        start, end = rolling_window(dt_util.now(), days)
        activity = request.query.get("activity", "bike")

        daily = await hass.async_add_executor_job(
            coordinator.store.daily_totals, start, end, activity
        )
        # Read the periods from the database rather than the coordinator's
        # cache: trips can be written by something other than the integration
        # (GPX import, the demo seeder), and a stale cache next to a live bar
        # chart is worse than one extra query.
        periods = await hass.async_add_executor_job(
            coordinator.compute_period_stats, activity
        )
        return self.json(
            {
                "periods": periods,
                "by_activity": coordinator.stats.get("by_activity", {}),
                "daily": daily,
                "window_days": days,
            }
        )


class CurrentView(HomeAssistantView):
    """GET /api/bike_tracker/current - live trip, for the map overlay."""

    url = "/api/bike_tracker/current"
    name = "api:bike_tracker:current"

    async def get(self, request: web.Request) -> web.Response:
        coordinator = _coordinator(request)
        return self.json(
            {
                "recording": coordinator.tracker.is_recording,
                "state": coordinator.tracker.state,
                "speed_kmh": coordinator.tracker.current_speed_kmh,
                "trip": coordinator.current_trip_dict(),
            }
        )


class SegmentsView(HomeAssistantView):
    """GET /api/bike_tracker/segments - definitions plus personal bests."""

    url = "/api/bike_tracker/segments"
    name = "api:bike_tracker:segments"

    async def get(self, request: web.Request) -> web.Response:
        coordinator = _coordinator(request)
        hass: HomeAssistant = request.app["hass"]
        segments = await hass.async_add_executor_job(
            coordinator.store.segments_with_stats
        )
        return self.json({"segments": segments, "count": len(segments)})


class SegmentDetailView(HomeAssistantView):
    """GET / DELETE /api/bike_tracker/segments/{segment_id}"""

    url = "/api/bike_tracker/segments/{segment_id}"
    name = "api:bike_tracker:segment"

    async def get(self, request: web.Request, segment_id: str) -> web.Response:
        coordinator = _coordinator(request)
        hass: HomeAssistant = request.app["hass"]
        segment = await hass.async_add_executor_job(
            coordinator.store.get_segment, int(segment_id)
        )
        if segment is None:
            return self.json_message("Segment not found", 404)
        segment["efforts"] = await hass.async_add_executor_job(
            coordinator.store.list_efforts, int(segment_id), 100
        )
        segment["best"] = await hass.async_add_executor_job(
            coordinator.store.best_effort, int(segment_id)
        )
        return self.json(segment)

    async def delete(self, request: web.Request, segment_id: str) -> web.Response:
        coordinator = _coordinator(request)
        hass: HomeAssistant = request.app["hass"]
        ok = await hass.async_add_executor_job(
            coordinator.store.delete_segment, int(segment_id)
        )
        if not ok:
            return self.json_message("Segment not found", 404)
        return self.json({"deleted": int(segment_id)})


class RouteView(HomeAssistantView):
    """POST /api/bike_tracker/route - what the card's route panel calls.

    Body: {"start": "...", "destination": "...", "profile": "bike",
           "notify_device": "notify.mobile_app_..."}
    """

    url = "/api/bike_tracker/route"
    name = "api:bike_tracker:route"

    async def post(self, request: web.Request) -> web.Response:
        coordinator = _coordinator(request)
        hass: HomeAssistant = request.app["hass"]
        try:
            body = await request.json()
        except ValueError:
            return self.json_message("Invalid JSON", 400)

        start = str(body.get("start", "")).strip()
        destination = str(body.get("destination", "")).strip()
        if not start or not destination:
            return self.json_message("start and destination are required", 400)

        try:
            route = await async_plan_route(
                hass,
                coordinator,
                start,
                destination,
                str(body.get("profile", "bike")),
                body.get("notify_device") or None,
            )
        except HomeAssistantError as err:
            return self.json_message(str(err), 400)
        return self.json(route)


def _elevation_profile(track: list[dict[str, Any]]) -> list[list[float]]:
    """[[cumulative_distance_km, altitude_m], ...] for the chart."""
    from .geo import haversine, median_filter

    points = [p for p in track if p.get("alt") is not None]
    if len(points) < 2:
        return []
    altitudes = median_filter([float(p["alt"]) for p in points], 5)
    profile: list[list[float]] = []
    total = 0.0
    for index, point in enumerate(points):
        if index:
            previous = points[index - 1]
            total += haversine(
                previous["lat"], previous["lon"], point["lat"], point["lon"]
            )
        profile.append([round(total / 1000.0, 3), round(altitudes[index], 1)])
    return profile
