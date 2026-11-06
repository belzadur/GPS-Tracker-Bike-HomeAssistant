"""The Bike Tracker integration.

Turns the raw location stream of the Home Assistant companion app into
recorded trips, statistics and a map - entirely on the Home Assistant
instance. The phone only reports its position; no processing happens there.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import (
    ACTIVITIES,
    DEFAULT_SEGMENT_RADIUS_M,
    DOMAIN,
    PLATFORMS,
    SERVICE_CREATE_SEGMENT,
    SERVICE_DELETE_SEGMENT,
    SERVICE_DELETE_TRIP,
    SERVICE_DISCARD_TRIP,
    SERVICE_EXPORT_GPX,
    SERVICE_IMPORT_GPX,
    SERVICE_PLAN_ROUTE,
    SERVICE_PURGE,
    SERVICE_REFRESH_ELEVATION,
    SERVICE_RESCAN_SEGMENTS,
    SERVICE_SET_ACTIVITY,
    SERVICE_START_TRIP,
    SERVICE_STOP_TRIP,
)
from .coordinator import BikeTrackerCoordinator
from .gpx import GpxError, build_gpx
from .http_api import async_register_views
from .importer import gpx_files_in, is_duplicate, read_gpx_file, trip_from_gpx_track
from .routing import async_plan_route
from .segments import segment_from_track

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

TRIP_ID_SCHEMA = vol.Schema({vol.Required("trip_id"): cv.positive_int})
SET_ACTIVITY_SCHEMA = vol.Schema(
    {
        vol.Required("trip_id"): cv.positive_int,
        vol.Required("activity"): vol.In(ACTIVITIES),
    }
)
EXPORT_GPX_SCHEMA = vol.Schema(
    {
        vol.Required("trip_id"): cv.positive_int,
        vol.Optional("path"): cv.string,
    }
)
PLAN_ROUTE_SCHEMA = vol.Schema(
    {
        vol.Required("start"): cv.string,
        vol.Required("destination"): cv.string,
        vol.Optional("profile", default="bike"): cv.string,
        vol.Optional("notify_device"): cv.string,
    }
)
PURGE_SCHEMA = vol.Schema({vol.Optional("days", default=0): cv.positive_int})
IMPORT_GPX_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Optional("activity"): vol.In(ACTIVITIES),
    }
)
REFRESH_ELEVATION_SCHEMA = vol.Schema(
    {
        vol.Optional("trip_id"): cv.positive_int,
        vol.Optional("days", default=0): cv.positive_int,
    }
)
CREATE_SEGMENT_SCHEMA = vol.Schema(
    {
        vol.Required("name"): cv.string,
        vol.Required("trip_id"): cv.positive_int,
        vol.Optional("start_index", default=0): cv.positive_int,
        vol.Optional("end_index"): cv.positive_int,
        vol.Optional("radius_m", default=DEFAULT_SEGMENT_RADIUS_M): vol.Coerce(float),
    }
)
SEGMENT_ID_SCHEMA = vol.Schema({vol.Required("segment_id"): cv.positive_int})
RESCAN_SEGMENTS_SCHEMA = vol.Schema(
    {vol.Optional("days", default=0): cv.positive_int}
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Bike Tracker from a config entry."""
    coordinator = BikeTrackerCoordinator(hass, entry)
    await coordinator.async_setup()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    async_register_views(hass)
    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: BikeTrackerCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
        if not hass.data[DOMAIN]:
            for service in (
                SERVICE_START_TRIP,
                SERVICE_STOP_TRIP,
                SERVICE_DISCARD_TRIP,
                SERVICE_SET_ACTIVITY,
                SERVICE_DELETE_TRIP,
                SERVICE_EXPORT_GPX,
                SERVICE_IMPORT_GPX,
                SERVICE_PLAN_ROUTE,
                SERVICE_PURGE,
                SERVICE_REFRESH_ELEVATION,
                SERVICE_CREATE_SEGMENT,
                SERVICE_DELETE_SEGMENT,
                SERVICE_RESCAN_SEGMENTS,
            ):
                hass.services.async_remove(DOMAIN, service)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _first_coordinator(hass: HomeAssistant) -> BikeTrackerCoordinator:
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise HomeAssistantError("Bike Tracker is not set up")
    return next(iter(entries.values()))


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the integration's services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_START_TRIP):
        return

    async def handle_start(call: ServiceCall) -> None:
        _first_coordinator(hass).tracker.force_start(
            now=dt_util.utcnow().timestamp()
        )

    async def handle_stop(call: ServiceCall) -> None:
        _first_coordinator(hass).tracker.force_stop()

    async def handle_discard(call: ServiceCall) -> None:
        _first_coordinator(hass).tracker.discard()

    async def handle_set_activity(call: ServiceCall) -> None:
        coordinator = _first_coordinator(hass)
        ok = await hass.async_add_executor_job(
            coordinator.store.set_activity,
            call.data["trip_id"],
            call.data["activity"],
        )
        if not ok:
            raise HomeAssistantError(f"No trip with id {call.data['trip_id']}")
        await coordinator.async_refresh_statistics()

    async def handle_delete(call: ServiceCall) -> None:
        coordinator = _first_coordinator(hass)
        ok = await hass.async_add_executor_job(
            coordinator.store.delete_trip, call.data["trip_id"]
        )
        if not ok:
            raise HomeAssistantError(f"No trip with id {call.data['trip_id']}")
        await coordinator.async_refresh_statistics()

    async def handle_export_gpx(call: ServiceCall) -> ServiceResponse:
        coordinator = _first_coordinator(hass)
        trip_id = call.data["trip_id"]
        trip = await hass.async_add_executor_job(coordinator.store.get_trip, trip_id)
        if trip is None:
            raise HomeAssistantError(f"No trip with id {trip_id}")
        track = await hass.async_add_executor_job(coordinator.store.get_track, trip_id)
        xml = build_gpx(trip, track)

        path = call.data.get("path") or hass.config.path(
            f"www/bike_tracker/trip_{trip_id}.gpx"
        )
        if not hass.config.is_allowed_path(path):
            raise HomeAssistantError(
                f"Path {path} is not allowed. "
                "Add its directory to allowlist_external_dirs."
            )

        def _write() -> None:
            import os

            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(xml)

        await hass.async_add_executor_job(_write)
        return {"path": path, "bytes": len(xml.encode("utf-8"))}

    async def handle_plan_route(call: ServiceCall) -> ServiceResponse:
        coordinator = _first_coordinator(hass)
        return await async_plan_route(
            hass,
            coordinator,
            call.data["start"],
            call.data["destination"],
            call.data.get("profile", "bike"),
            call.data.get("notify_device"),
        )

    async def handle_import_gpx(call: ServiceCall) -> ServiceResponse:
        coordinator = _first_coordinator(hass)
        path = call.data["path"]
        if not hass.config.is_allowed_path(path):
            raise HomeAssistantError(
                f"Path {path} is not allowed. "
                "Add its directory to allowlist_external_dirs."
            )

        files = await hass.async_add_executor_job(gpx_files_in, path)
        if not files:
            raise HomeAssistantError(f"No .gpx files found at {path}")

        # A GPX file carries real barometric or DEM elevation - always use it,
        # regardless of what the live tracker is configured to do.
        cfg = replace(coordinator.tracker.config, use_gps_altitude=True)
        imported: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for file_path in files:
            try:
                tracks = await hass.async_add_executor_job(read_gpx_file, file_path)
            except (GpxError, OSError) as err:
                skipped.append({"file": file_path, "reason": str(err)})
                continue

            for track in tracks:
                try:
                    trip = trip_from_gpx_track(
                        track, cfg, call.data.get("activity")
                    )
                except GpxError as err:
                    skipped.append({"file": file_path, "reason": str(err)})
                    continue

                existing = await hass.async_add_executor_job(
                    coordinator.store.find_overlapping, trip.started_at, trip.ended_at
                )
                if is_duplicate(trip, existing):
                    skipped.append(
                        {"file": file_path, "reason": "already imported"}
                    )
                    continue

                trip_id = await hass.async_add_executor_job(
                    coordinator.store.save_trip, trip, f"gpx:{file_path}", True
                )
                await coordinator.async_match_segments(trip_id, trip.activity)
                imported.append(
                    {
                        "file": file_path,
                        "trip_id": trip_id,
                        "name": track.name,
                        "activity": trip.activity,
                        "distance_km": round(trip.distance_m / 1000.0, 2),
                        "duration_min": round(trip.duration_s / 60.0, 1),
                        "elevation_gain_m": trip.elevation_gain_m,
                    }
                )

        await coordinator.async_refresh_statistics()
        _LOGGER.info(
            "GPX import: %s trips imported, %s skipped", len(imported), len(skipped)
        )
        return {"imported": imported, "skipped": skipped}

    async def handle_refresh_elevation(call: ServiceCall) -> ServiceResponse:
        coordinator = _first_coordinator(hass)
        trip_id = call.data.get("trip_id")
        if trip_id is not None:
            trip_ids = [int(trip_id)]
        else:
            days = int(call.data.get("days", 0))
            start = None
            if days > 0:
                start = (dt_util.now() - timedelta(days=days)).timestamp()
            trips = await hass.async_add_executor_job(
                coordinator.store.list_trips, 10000, 0, None, start, None
            )
            trip_ids = [int(row["id"]) for row in trips]

        updated: list[dict[str, Any]] = []
        for current_id in trip_ids:
            result = await coordinator.async_refresh_trip_elevation(current_id)
            if result is not None:
                updated.append(result)

        await coordinator.async_refresh_statistics()
        return {"updated": updated, "count": len(updated)}

    async def handle_create_segment(call: ServiceCall) -> ServiceResponse:
        coordinator = _first_coordinator(hass)
        trip_id = int(call.data["trip_id"])
        rows = await hass.async_add_executor_job(coordinator.store.get_track, trip_id)
        if len(rows) < 2:
            raise HomeAssistantError(f"Trip {trip_id} has no usable track")

        track = [
            (float(row["ts"]), float(row["lat"]), float(row["lon"])) for row in rows
        ]
        trip = await hass.async_add_executor_job(coordinator.store.get_trip, trip_id)
        segment = segment_from_track(
            name=call.data["name"],
            track=track,
            start_index=int(call.data.get("start_index", 0)),
            end_index=(
                int(call.data["end_index"]) if "end_index" in call.data else None
            ),
            radius_m=float(call.data.get("radius_m", DEFAULT_SEGMENT_RADIUS_M)),
            activity=str((trip or {}).get("activity") or "bike"),
        )

        try:
            segment_id = await hass.async_add_executor_job(
                coordinator.store.create_segment, segment
            )
        except sqlite3.IntegrityError as err:
            raise HomeAssistantError(
                f"A segment named '{segment.name}' already exists"
            ) from err

        matched = await _rescan(coordinator, 0)
        return {
            "segment_id": segment_id,
            "name": segment.name,
            "length_m": segment.length_m,
            "efforts_found": matched,
        }

    async def handle_delete_segment(call: ServiceCall) -> None:
        coordinator = _first_coordinator(hass)
        ok = await hass.async_add_executor_job(
            coordinator.store.delete_segment, call.data["segment_id"]
        )
        if not ok:
            raise HomeAssistantError(f"No segment with id {call.data['segment_id']}")

    async def handle_rescan_segments(call: ServiceCall) -> ServiceResponse:
        coordinator = _first_coordinator(hass)
        matched = await _rescan(coordinator, int(call.data.get("days", 0)))
        return {"efforts_found": matched}

    async def _rescan(coordinator: BikeTrackerCoordinator, days: int) -> int:
        """Re-run segment matching over stored trips. Returns effort count."""
        start = None
        if days > 0:
            start = (dt_util.now() - timedelta(days=days)).timestamp()
        trips = await hass.async_add_executor_job(
            coordinator.store.list_trips, 10000, 0, None, start, None
        )
        found = 0
        for row in trips:
            matches = await coordinator.async_match_segments(
                int(row["id"]), str(row["activity"])
            )
            found += len(matches)
        return found

    async def handle_purge(call: ServiceCall) -> ServiceResponse:
        coordinator = _first_coordinator(hass)
        removed = await hass.async_add_executor_job(
            coordinator.store.purge_older_than, call.data.get("days", 0)
        )
        await coordinator.async_refresh_statistics()
        return {"deleted": removed}

    hass.services.async_register(DOMAIN, SERVICE_START_TRIP, handle_start)
    hass.services.async_register(DOMAIN, SERVICE_STOP_TRIP, handle_stop)
    hass.services.async_register(DOMAIN, SERVICE_DISCARD_TRIP, handle_discard)
    hass.services.async_register(
        DOMAIN, SERVICE_SET_ACTIVITY, handle_set_activity, schema=SET_ACTIVITY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_TRIP, handle_delete, schema=TRIP_ID_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_GPX,
        handle_export_gpx,
        schema=EXPORT_GPX_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PLAN_ROUTE,
        handle_plan_route,
        schema=PLAN_ROUTE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PURGE,
        handle_purge,
        schema=PURGE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_GPX,
        handle_import_gpx,
        schema=IMPORT_GPX_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_ELEVATION,
        handle_refresh_elevation,
        schema=REFRESH_ELEVATION_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CREATE_SEGMENT,
        handle_create_segment,
        schema=CREATE_SEGMENT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_SEGMENT,
        handle_delete_segment,
        schema=SEGMENT_ID_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESCAN_SEGMENTS,
        handle_rescan_segments,
        schema=RESCAN_SEGMENTS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
