"""Glue between Home Assistant, the trip tracker and the database."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_GPS_ACCURACY,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    EVENT_HOMEASSISTANT_STOP,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    ACTIVITY_BIKE,
    CONF_ELEVATION_SOURCE,
    CONF_ELEVATION_THRESHOLD,
    CONF_ELEVATION_URL,
    CONF_MAX_ACCURACY,
    CONF_MIN_DISTANCE,
    CONF_MIN_DURATION,
    CONF_REQUIRE_CONFIRMATION,
    CONF_RETENTION_DAYS,
    CONF_SOURCE_ENTITY,
    CONF_STALE_TIMEOUT,
    CONF_START_DURATION,
    CONF_START_SPEED,
    CONF_STOP_DURATION,
    CONF_STOP_SPEED,
    CONF_TRACK_ALL_ACTIVITIES,
    DB_FILENAME,
    DEFAULT_ELEVATION_THRESHOLD,
    DEFAULT_ELEVATION_URL,
    DEFAULT_MAX_ACCURACY,
    DEFAULT_MIN_DISTANCE,
    DEFAULT_MIN_DURATION,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_STALE_TIMEOUT,
    DEFAULT_START_DURATION,
    DEFAULT_START_SPEED,
    DEFAULT_STOP_DURATION,
    DEFAULT_STOP_SPEED,
    ELEVATION_SOURCE_DEM,
    ELEVATION_SOURCE_GPS,
    EVENT_SEGMENT_MATCHED,
    EVENT_SEGMENT_RECORD,
    EVENT_TRIP_DISCARDED,
    EVENT_TRIP_FINISHED,
    EVENT_TRIP_STARTED,
    PERIODS,
    SIGNAL_UPDATE,
)
from .elevation import ElevationCache, ElevationError, async_elevations_for_track
from .geo import elevation_stats
from .segments import Segment, match_segment
from .stats import period_bounds
from .storage import TripStore
from .tracker import GpsPoint, TrackerConfig, Trip, TripTracker

_LOGGER = logging.getLogger(__name__)

TICK_INTERVAL = timedelta(seconds=60)
# The sensors read a cached aggregate. Recompute it now and then so trips that
# arrived without the integration noticing - a GPX import, the demo seeder, a
# manual database edit - do not leave the sensors stuck on old numbers.
STATS_REFRESH_INTERVAL = timedelta(minutes=5)
STATS_ACTIVITY = ACTIVITY_BIKE


class BikeTrackerCoordinator:
    """Owns the tracker state machine, the store and the derived statistics."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.store = TripStore(hass.config.path(DB_FILENAME))
        self.tracker = TripTracker(
            config=self._tracker_config(),
            on_trip_finished=self._handle_trip_finished,
            on_trip_discarded=self._handle_trip_discarded,
            on_trip_started=self._handle_trip_started,
        )
        self.stats: dict[str, Any] = {}
        self.last_trip: dict[str, Any] | None = None
        self._unsubs: list[Any] = []
        self._lock = asyncio.Lock()
        # Saves still running; awaited before Home Assistant stops or the
        # database is closed, so no ride is written into a closed store.
        self._store_tasks: set[asyncio.Task[None]] = set()
        self._stopping = False
        # Riding the same roads over and over means most DEM lookups are
        # repeats - the cache keeps those off the network entirely.
        self._elevation_cache = ElevationCache()

    # -- configuration ---------------------------------------------------

    @property
    def options(self) -> dict[str, Any]:
        merged = dict(self.entry.data)
        merged.update(self.entry.options)
        return merged

    @property
    def source_entity(self) -> str:
        return str(self.options.get(CONF_SOURCE_ENTITY, ""))

    @property
    def track_all_activities(self) -> bool:
        return bool(self.options.get(CONF_TRACK_ALL_ACTIVITIES, True))

    @property
    def require_confirmation(self) -> bool:
        return bool(self.options.get(CONF_REQUIRE_CONFIRMATION, False))

    @property
    def elevation_source(self) -> str:
        return str(self.options.get(CONF_ELEVATION_SOURCE, ELEVATION_SOURCE_GPS))

    @property
    def elevation_url(self) -> str:
        return str(self.options.get(CONF_ELEVATION_URL, DEFAULT_ELEVATION_URL))

    @property
    def elevation_threshold_m(self) -> float:
        return float(
            self.options.get(CONF_ELEVATION_THRESHOLD, DEFAULT_ELEVATION_THRESHOLD)
        )

    def _tracker_config(self) -> TrackerConfig:
        opts = self.options
        return TrackerConfig(
            max_accuracy_m=float(opts.get(CONF_MAX_ACCURACY, DEFAULT_MAX_ACCURACY)),
            start_speed_kmh=float(opts.get(CONF_START_SPEED, DEFAULT_START_SPEED)),
            start_duration_s=float(
                opts.get(CONF_START_DURATION, DEFAULT_START_DURATION)
            ),
            stop_speed_kmh=float(opts.get(CONF_STOP_SPEED, DEFAULT_STOP_SPEED)),
            stop_duration_s=float(opts.get(CONF_STOP_DURATION, DEFAULT_STOP_DURATION)),
            stale_timeout_s=float(opts.get(CONF_STALE_TIMEOUT, DEFAULT_STALE_TIMEOUT)),
            min_distance_m=float(opts.get(CONF_MIN_DISTANCE, DEFAULT_MIN_DISTANCE)),
            min_duration_s=float(opts.get(CONF_MIN_DURATION, DEFAULT_MIN_DURATION)),
            elevation_threshold_m=float(
                opts.get(CONF_ELEVATION_THRESHOLD, DEFAULT_ELEVATION_THRESHOLD)
            ),
            use_gps_altitude=opts.get(CONF_ELEVATION_SOURCE, ELEVATION_SOURCE_GPS)
            == ELEVATION_SOURCE_GPS,
        )

    def apply_options(self) -> None:
        """Re-read options after the user changed them."""
        self.tracker.config = self._tracker_config()

    # -- elevation from a DEM -------------------------------------------

    async def _async_fetch_json(
        self, method: str, url: str, body: dict[str, Any] | None
    ) -> Any:
        """HTTP transport handed to the elevation module."""
        session = async_get_clientsession(self.hass)
        async with session.request(
            method, url, json=body, timeout=aiohttp.ClientTimeout(total=45)
        ) as response:
            if response.status != 200:
                raise ElevationError(
                    f"Elevation backend returned HTTP {response.status}"
                )
            # Some instances answer text/plain; do not let aiohttp object.
            return await response.json(content_type=None)

    async def async_dem_altitudes(
        self, track: list[tuple[float, float]]
    ) -> list[float | None] | None:
        """DEM elevation per track point, or None if the lookup failed."""
        try:
            return await async_elevations_for_track(
                self._async_fetch_json,
                self.elevation_url,
                track,
                self._elevation_cache,
            )
        except (TimeoutError, ElevationError, aiohttp.ClientError) as err:
            _LOGGER.warning(
                "DEM elevation lookup against %s failed (%s) - keeping GPS altitude",
                self.elevation_url,
                err,
            )
            return None

    async def _async_apply_dem(self, trip: Trip) -> None:
        """Replace the GPS altitude of a finished trip with DEM values."""
        if self.elevation_source != ELEVATION_SOURCE_DEM or not trip.points:
            return

        altitudes = await self.async_dem_altitudes(
            [(p.lat, p.lon) for p in trip.points]
        )
        if altitudes is None:
            # Fall back to whatever the GPS reported; the tracker skipped the
            # calculation because the source is not "gps", so redo it here.
            self._recompute_elevation(trip)
            return

        for point, altitude in zip(trip.points, altitudes, strict=True):
            if altitude is not None:
                point.alt = altitude
        self._recompute_elevation(trip)

    def _recompute_elevation(self, trip: Trip) -> None:
        gain, loss, low, high = elevation_stats(
            [p.alt for p in trip.points], self.elevation_threshold_m
        )
        trip.elevation_gain_m = round(gain, 1)
        trip.elevation_loss_m = round(loss, 1)
        trip.elevation_min_m = round(low, 1) if low is not None else None
        trip.elevation_max_m = round(high, 1) if high is not None else None

    async def async_refresh_trip_elevation(self, trip_id: int) -> dict[str, Any] | None:
        """Recompute one stored trip's elevation from the DEM.

        Returns the new figures, or None when the trip has no track or the
        backend could not be reached.
        """
        track = await self.hass.async_add_executor_job(self.store.get_track, trip_id)
        if not track:
            return None

        altitudes = await self.async_dem_altitudes(
            [(float(p["lat"]), float(p["lon"])) for p in track]
        )
        if altitudes is None:
            return None

        await self.hass.async_add_executor_job(
            self.store.update_point_altitudes, trip_id, altitudes
        )
        gain, loss, low, high = elevation_stats(altitudes, self.elevation_threshold_m)
        result = {
            "trip_id": trip_id,
            "elevation_gain_m": round(gain, 1),
            "elevation_loss_m": round(loss, 1),
            "elevation_min_m": round(low, 1) if low is not None else None,
            "elevation_max_m": round(high, 1) if high is not None else None,
        }
        await self.hass.async_add_executor_job(
            self.store.update_elevation,
            trip_id,
            result["elevation_gain_m"],
            result["elevation_loss_m"],
            result["elevation_min_m"],
            result["elevation_max_m"],
        )
        return result

    # -- lifecycle -------------------------------------------------------

    async def async_setup(self) -> None:
        await self.hass.async_add_executor_job(self.store.connect)
        await self.async_refresh_statistics()

        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [self.source_entity], self._handle_state_event
            )
        )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._handle_tick, TICK_INTERVAL)
        )
        # Home Assistant does not unload config entries when it stops, so
        # async_shutdown never runs on a restart. Save a running ride here.
        self._unsubs.append(
            self.hass.bus.async_listen(
                EVENT_HOMEASSISTANT_STOP, self._async_handle_ha_stop
            )
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._handle_stats_tick, STATS_REFRESH_INTERVAL
            )
        )

        # Seed the tracker with the current position so the very first ride is
        # not missed after a Home Assistant restart.
        state = self.hass.states.get(self.source_entity)
        if state is not None:
            point = self._point_from_state(state)
            if point is not None:
                self.tracker.add_point(point)

    async def _async_handle_ha_stop(self, _event: Event) -> None:
        self._stopping = True
        await self.async_save_running_trip()

    async def async_save_running_trip(self) -> None:
        """Close a running ride and wait until every pending save is done."""
        if self.tracker.is_recording:
            self.tracker.force_stop()
        if self._store_tasks:
            await asyncio.gather(*list(self._store_tasks), return_exceptions=True)

    async def async_shutdown(self) -> None:
        """Unload: stop listening, save a running ride, then close the store."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self.async_save_running_trip()
        await self.hass.async_add_executor_job(self.store.close)

    # -- event handling --------------------------------------------------

    @callback
    def _handle_state_event(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        point = self._point_from_state(new_state)
        if point is None:
            return
        self.tracker.add_point(point)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    @callback
    def _point_from_state(self, state: Any) -> GpsPoint | None:
        attrs = state.attributes
        lat = attrs.get(ATTR_LATITUDE)
        lon = attrs.get(ATTR_LONGITUDE)
        if lat is None or lon is None:
            return None

        # The companion app reports speed in m/s on iOS and Android;
        # -1 means "unknown".
        reported = attrs.get("speed")
        speed_kmh: float | None = None
        if isinstance(reported, (int, float)) and reported >= 0:
            speed_kmh = float(reported) * 3.6

        altitude = attrs.get("altitude")
        if isinstance(altitude, (int, float)) and altitude == 0:
            # Android reports a hard 0 when it has no barometric/GNSS altitude.
            altitude = None

        return GpsPoint(
            ts=state.last_updated.timestamp(),
            lat=float(lat),
            lon=float(lon),
            alt=float(altitude) if isinstance(altitude, (int, float)) else None,
            accuracy=(
                float(attrs[ATTR_GPS_ACCURACY])
                if isinstance(attrs.get(ATTR_GPS_ACCURACY), (int, float))
                else None
            ),
            reported_speed_kmh=speed_kmh,
        )

    async def _handle_tick(self, _now: Any) -> None:
        self.tracker.tick(dt_util.utcnow().timestamp())
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    async def _handle_stats_tick(self, _now: Any) -> None:
        if self.tracker.is_recording:
            # Mid-ride the numbers change anyway once the trip is saved.
            return
        await self.async_refresh_statistics()

    # -- tracker callbacks (sync, called from the event loop) ------------

    def _handle_trip_started(self, trip: Trip) -> None:
        _LOGGER.debug("Trip %s started", trip.uid)
        self.hass.bus.async_fire(
            EVENT_TRIP_STARTED, {"uid": trip.uid, "started_at": trip.started_at}
        )

    def _handle_trip_finished(self, trip: Trip) -> None:
        task = self.hass.async_create_task(self._async_store_trip(trip))
        self._store_tasks.add(task)
        task.add_done_callback(self._store_tasks.discard)

    def _handle_trip_discarded(self, trip: Trip, reason: str) -> None:
        _LOGGER.debug(
            "Trip %s discarded (%s): %.0f m in %.0f s",
            trip.uid,
            reason,
            trip.distance_m,
            trip.duration_s,
        )
        self.hass.bus.async_fire(
            EVENT_TRIP_DISCARDED,
            {"uid": trip.uid, "reason": reason, "distance_m": round(trip.distance_m)},
        )

    async def _async_store_trip(self, trip: Trip) -> None:
        if not self.track_all_activities and trip.activity != ACTIVITY_BIKE:
            self._handle_trip_discarded(trip, f"filtered:{trip.activity}")
            return

        if self._stopping:
            # No network lookups while Home Assistant shuts down; the DEM
            # elevation can be refreshed later for this trip.
            if self.elevation_source == ELEVATION_SOURCE_DEM:
                self._recompute_elevation(trip)
        else:
            await self._async_apply_dem(trip)

        async with self._lock:
            trip_id = await self.hass.async_add_executor_job(
                self.store.save_trip,
                trip,
                self.source_entity,
                not self.require_confirmation,
            )
            retention = int(
                self.options.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS)
            )
            if retention > 0:
                await self.hass.async_add_executor_job(
                    self.store.purge_older_than, retention
                )
            await self.async_refresh_statistics()

        _LOGGER.info(
            "Recorded %s trip #%s: %.2f km in %.0f min (%.1f km/h, %.0f m up)",
            trip.activity,
            trip_id,
            trip.distance_m / 1000.0,
            trip.duration_s / 60.0,
            trip.avg_moving_speed_kmh,
            trip.elevation_gain_m,
        )
        self.hass.bus.async_fire(
            EVENT_TRIP_FINISHED,
            {
                "trip_id": trip_id,
                "uid": trip.uid,
                "activity": trip.activity,
                "confidence": trip.activity_confidence,
                "distance_km": round(trip.distance_m / 1000.0, 2),
                "duration_min": round(trip.duration_s / 60.0, 1),
                "moving_time_min": round(trip.moving_time_s / 60.0, 1),
                "avg_speed_kmh": round(trip.avg_moving_speed_kmh, 1),
                "max_speed_kmh": round(trip.max_speed_kmh, 1),
                "elevation_gain_m": trip.elevation_gain_m,
            },
        )

        await self.async_match_segments(trip_id, trip.activity)

    # -- segments --------------------------------------------------------

    async def async_match_segments(self, trip_id: int, activity: str) -> list[str]:
        """Check one stored trip against every segment, record the efforts.

        Returns the names of the segments that matched.
        """
        segments = await self.hass.async_add_executor_job(self.store.list_segments)
        if not segments:
            return []

        track_rows = await self.hass.async_add_executor_job(
            self.store.get_track, trip_id
        )
        track = [
            (float(row["ts"]), float(row["lat"]), float(row["lon"]))
            for row in track_rows
        ]
        if len(track) < 2:
            return []

        matched: list[str] = []
        for row in segments:
            segment = Segment(
                id=int(row["id"]),
                name=str(row["name"]),
                start_lat=float(row["start_lat"]),
                start_lon=float(row["start_lon"]),
                end_lat=float(row["end_lat"]),
                end_lon=float(row["end_lon"]),
                length_m=float(row["length_m"]),
                radius_m=float(row["radius_m"]),
                activity=str(row["activity"]),
            )
            if segment.activity and segment.activity != activity:
                continue

            effort = match_segment(segment, track)
            if effort is None:
                continue

            previous_best = await self.hass.async_add_executor_job(
                self.store.best_effort, segment.id
            )
            await self.hass.async_add_executor_job(
                self.store.save_effort, segment.id, trip_id, effort
            )
            matched.append(segment.name)

            payload = {
                "segment_id": segment.id,
                "segment": segment.name,
                "trip_id": trip_id,
                "duration_s": effort.duration_s,
                "distance_m": effort.distance_m,
                "avg_speed_kmh": effort.avg_speed_kmh,
            }
            self.hass.bus.async_fire(EVENT_SEGMENT_MATCHED, payload)

            is_record = (
                previous_best is None
                or effort.duration_s < float(previous_best["duration_s"])
            )
            if is_record:
                _LOGGER.info(
                    "New personal best on segment %s: %.0f s",
                    segment.name,
                    effort.duration_s,
                )
                self.hass.bus.async_fire(
                    EVENT_SEGMENT_RECORD,
                    {
                        **payload,
                        "previous_best_s": (
                            float(previous_best["duration_s"])
                            if previous_best
                            else None
                        ),
                    },
                )

        if matched:
            async_dispatcher_send(self.hass, SIGNAL_UPDATE)
        return matched

    # -- statistics ------------------------------------------------------

    async def async_refresh_statistics(self) -> None:
        self.stats = await self.hass.async_add_executor_job(self._compute_statistics)
        self.last_trip = await self.hass.async_add_executor_job(
            self.store.get_last_trip, STATS_ACTIVITY
        )
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    def compute_period_stats(self, activity: str | None = STATS_ACTIVITY) -> dict[str, Any]:
        """Aggregate every period straight from the database.

        Deliberately not cached: trips can appear without the integration
        noticing - a GPX import from another process, the demo seeder, a
        manual edit of the database. Five indexed aggregates are cheap enough
        to just run them.
        """
        now = dt_util.now()
        return {
            period: self.store.aggregate(*period_bounds(period, now), activity)
            for period in PERIODS
        }

    def _compute_statistics(self) -> dict[str, Any]:
        result: dict[str, Any] = self.compute_period_stats()
        result["by_activity"] = self.store.counts_by_activity()
        return result

    def stat(self, period: str, key: str, default: float = 0.0) -> float:
        return float(self.stats.get(period, {}).get(key, default) or default)

    # -- live trip snapshot ---------------------------------------------

    @property
    def current_trip_distance_m(self) -> float:
        return self.tracker.trip.distance_m if self.tracker.trip else 0.0

    @property
    def current_trip_duration_s(self) -> float:
        return self.tracker.trip.duration_s if self.tracker.trip else 0.0

    def current_trip_dict(self) -> dict[str, Any] | None:
        trip = self.tracker.trip
        if trip is None:
            return None
        return {
            "uid": trip.uid,
            "started_at": trip.started_at,
            "distance_m": round(trip.distance_m, 1),
            "duration_s": round(trip.duration_s, 1),
            "moving_time_s": round(trip.moving_time_s, 1),
            "max_speed_kmh": round(trip.max_speed_kmh, 1),
            "point_count": len(trip.points),
            "track": [[round(p.lat, 6), round(p.lon, 6)] for p in trip.points],
        }

    def database_path(self) -> str:
        return os.fspath(self.store.path)
