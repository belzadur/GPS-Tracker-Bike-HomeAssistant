"""Trip detection state machine.

Consumes a stream of GPS fixes and emits finished trips. Pure Python, no
Home Assistant imports, so the whole detection pipeline can be replayed
against a recorded track in the test suite.

State machine
-------------
    IDLE ---- speed >= start_speed --------> CANDIDATE
    CANDIDATE - held for start_duration ---> ACTIVE   (candidate buffer is
                                                       backfilled into the trip
                                                       so we do not lose the
                                                       first ~45 s)
    CANDIDATE - speed drops ---------------> IDLE
    ACTIVE ---- speed < stop_speed for stop_duration --> finish
    ACTIVE ---- no fix for stale_timeout ---------------> finish
"""

from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from .classify import classify
from .const import (
    ACTIVITY_UNKNOWN,
    DEFAULT_ELEVATION_THRESHOLD,
    DEFAULT_MAX_ACCURACY,
    DEFAULT_MIN_DISTANCE,
    DEFAULT_MIN_DURATION,
    DEFAULT_STALE_TIMEOUT,
    DEFAULT_START_DURATION,
    DEFAULT_START_SPEED,
    DEFAULT_STOP_DURATION,
    DEFAULT_STOP_SPEED,
    MAX_PLAUSIBLE_SPEED_KMH,
    MIN_SEGMENT_SECONDS,
    MOVING_SPEED_THRESHOLD_KMH,
    SPEED_SMOOTHING_WINDOW,
    STATE_ACTIVE,
    STATE_CANDIDATE,
    STATE_IDLE,
)
from .geo import elevation_stats, haversine, speed_kmh


@dataclass(slots=True)
class GpsPoint:
    """A single location fix."""

    ts: float
    lat: float
    lon: float
    alt: float | None = None
    accuracy: float | None = None
    reported_speed_kmh: float | None = None
    # Filled in by the tracker:
    speed_kmh: float = 0.0
    distance_m: float = 0.0


@dataclass(slots=True)
class TrackerConfig:
    """Tunable detection parameters."""

    max_accuracy_m: float = DEFAULT_MAX_ACCURACY
    start_speed_kmh: float = DEFAULT_START_SPEED
    start_duration_s: float = DEFAULT_START_DURATION
    stop_speed_kmh: float = DEFAULT_STOP_SPEED
    stop_duration_s: float = DEFAULT_STOP_DURATION
    stale_timeout_s: float = DEFAULT_STALE_TIMEOUT
    min_distance_m: float = DEFAULT_MIN_DISTANCE
    min_duration_s: float = DEFAULT_MIN_DURATION
    elevation_threshold_m: float = DEFAULT_ELEVATION_THRESHOLD
    use_gps_altitude: bool = True


@dataclass(slots=True)
class Trip:
    """A detected trip plus its computed statistics."""

    uid: str
    points: list[GpsPoint] = field(default_factory=list)
    distance_m: float = 0.0
    moving_time_s: float = 0.0
    max_speed_kmh: float = 0.0
    activity: str = ACTIVITY_UNKNOWN
    activity_confidence: float = 0.0
    elevation_gain_m: float = 0.0
    elevation_loss_m: float = 0.0
    elevation_min_m: float | None = None
    elevation_max_m: float | None = None
    manual: bool = False

    @property
    def started_at(self) -> float:
        return self.points[0].ts if self.points else 0.0

    @property
    def ended_at(self) -> float:
        return self.points[-1].ts if self.points else 0.0

    @property
    def duration_s(self) -> float:
        return max(0.0, self.ended_at - self.started_at)

    @property
    def avg_speed_kmh(self) -> float:
        return speed_kmh(self.distance_m, self.duration_s)

    @property
    def avg_moving_speed_kmh(self) -> float:
        return speed_kmh(self.distance_m, self.moving_time_s)

    def finalize(self, cfg: TrackerConfig) -> None:
        """Compute derived statistics once the trip has ended."""
        if cfg.use_gps_altitude:
            gain, loss, low, high = elevation_stats(
                [p.alt for p in self.points], cfg.elevation_threshold_m
            )
            self.elevation_gain_m = round(gain, 1)
            self.elevation_loss_m = round(loss, 1)
            self.elevation_min_m = round(low, 1) if low is not None else None
            self.elevation_max_m = round(high, 1) if high is not None else None

        if not self.manual or self.activity == ACTIVITY_UNKNOWN:
            result = classify(
                [p.speed_kmh for p in self.points],
                self.distance_m,
                self.duration_s,
            )
            self.activity = result.activity
            self.activity_confidence = result.confidence


def resolve_speed(
    reported: float | None, computed: float, trust_zero: bool = False
) -> float:
    """Decide which speed to trust for one fix.

    The device's own Doppler speed is far more accurate than differentiating
    positions - but only when the device actually has one. Android's companion
    app fills unknown fields with 0 instead of omitting them: a phone whose
    location provider reports no speed sends ``speed: 0`` while its position
    moves across town.

    Taking that at face value pins the tracker to a standstill and no ride is
    ever detected, however far or fast you go. So a reported zero counts as
    "no data" and the speed derived from the positions wins. That costs
    nothing when the rider really is standing still, because then the derived
    speed is near zero too.

    ``trust_zero`` is set once the device has reported a speed above 0. Such
    a device does have a speed fix, so its 0 is a real reading. This matters
    when the phone stops updating its position for a while, reports 0 and
    then catches up on the distance in one step: the speed derived from that
    single step is far too high (e.g. 105 m in 2.8 s = 133 km/h), the reported
    0 is right.
    """
    if (
        reported is not None
        and 0.0 <= reported <= MAX_PLAUSIBLE_SPEED_KMH
        and (reported > 0.0 or trust_zero)
    ):
        return reported
    return computed


def trip_from_points(
    points: list[GpsPoint],
    cfg: TrackerConfig,
    activity: str | None = None,
) -> Trip:
    """Build a finished trip out of an already-recorded track.

    The state machine is bypassed - the ride is over, there is nothing to
    detect - but the same distance, speed and elevation maths is applied, so
    an imported ride is directly comparable with a recorded one.

    Passing ``activity`` pins the classification instead of guessing it.
    """
    trip = Trip(uid=uuid.uuid4().hex, manual=activity is not None)
    if activity is not None:
        trip.activity = activity
    reports_speed = any((p.reported_speed_kmh or 0.0) > 0.0 for p in points)

    previous: GpsPoint | None = None
    for point in points:
        if previous is None:
            point.speed_kmh = point.reported_speed_kmh or 0.0
            trip.points.append(point)
            previous = point
            continue

        dt = point.ts - previous.ts
        if dt < MIN_SEGMENT_SECONDS:
            continue

        distance = haversine(previous.lat, previous.lon, point.lat, point.lon)
        if speed_kmh(distance, dt) > MAX_PLAUSIBLE_SPEED_KMH:
            continue

        point.distance_m = distance
        point.speed_kmh = resolve_speed(
            point.reported_speed_kmh, speed_kmh(distance, dt), reports_speed
        )

        trip.points.append(point)
        trip.distance_m += distance
        trip.max_speed_kmh = max(trip.max_speed_kmh, point.speed_kmh)
        if point.speed_kmh >= MOVING_SPEED_THRESHOLD_KMH:
            trip.moving_time_s += dt
        previous = point

    trip.finalize(cfg)
    return trip


class TripTracker:
    """Feed GPS points in, get finished trips out."""

    def __init__(
        self,
        config: TrackerConfig | None = None,
        on_trip_finished: Callable[[Trip], None] | None = None,
        on_trip_discarded: Callable[[Trip, str], None] | None = None,
        on_trip_started: Callable[[Trip], None] | None = None,
    ) -> None:
        self.config = config or TrackerConfig()
        self._on_finished = on_trip_finished
        self._on_discarded = on_trip_discarded
        self._on_started = on_trip_started

        self.state: str = STATE_IDLE
        self.trip: Trip | None = None
        self._last_point: GpsPoint | None = None
        self._speed_window: deque[float] = deque(maxlen=SPEED_SMOOTHING_WINDOW)
        self._candidate: list[GpsPoint] = []
        self._above_since: float | None = None
        self._below_since: float | None = None
        self.rejected_points: int = 0
        # True once the device has reported a speed above 0; from then on its
        # 0 counts as a real reading too (see resolve_speed).
        self._device_reports_speed: bool = False

    # -- public API ------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        return self.state == STATE_ACTIVE

    @property
    def current_speed_kmh(self) -> float:
        if not self._speed_window:
            return 0.0
        return round(sum(self._speed_window) / len(self._speed_window), 2)

    def add_point(self, point: GpsPoint) -> None:
        """Process one GPS fix."""
        if not self._accept(point):
            return
        if (point.reported_speed_kmh or 0.0) > 0.0:
            self._device_reports_speed = True

        previous = self._last_point
        if previous is None:
            point.speed_kmh = point.reported_speed_kmh or 0.0
            self._last_point = point
            self._speed_window.append(point.speed_kmh)
            self._candidate = [point]
            return

        dt = point.ts - previous.ts
        if dt < MIN_SEGMENT_SECONDS:
            # Duplicate or out-of-order fix - ignore.
            return

        dist = haversine(previous.lat, previous.lon, point.lat, point.lon)
        raw_speed = speed_kmh(dist, dt)

        if raw_speed > MAX_PLAUSIBLE_SPEED_KMH:
            # GPS teleport (tunnel exit, cell-tower fix). Drop it, but keep
            # the timeline moving so we do not get stuck.
            self.rejected_points += 1
            return

        point.distance_m = dist
        point.speed_kmh = resolve_speed(
            point.reported_speed_kmh, raw_speed, self._device_reports_speed
        )

        self._speed_window.append(point.speed_kmh)
        smoothed = self.current_speed_kmh
        self._last_point = point

        if self.state == STATE_ACTIVE:
            self._accumulate(point, dt)
            self._check_stop(point, smoothed)
        else:
            self._check_start(point, smoothed)

    def tick(self, now: float) -> None:
        """Call periodically. Closes trips when fixes stop arriving."""
        if self.state != STATE_ACTIVE or self._last_point is None:
            return
        if now - self._last_point.ts >= self.config.stale_timeout_s:
            self._finish("stale")

    def force_start(self, point: GpsPoint | None = None) -> None:
        """Manually open a trip (service call)."""
        if self.state == STATE_ACTIVE:
            return
        seed = point or self._last_point
        self.trip = Trip(uid=uuid.uuid4().hex, manual=True)
        if seed is not None:
            self.trip.points.append(seed)
        self.state = STATE_ACTIVE
        self._below_since = None
        self._candidate = []
        if self._on_started:
            self._on_started(self.trip)

    def force_stop(self) -> None:
        """Manually close the running trip (service call)."""
        if self.state == STATE_ACTIVE:
            self._finish("manual")

    def discard(self) -> None:
        """Throw away the running trip without saving it."""
        if self.trip is not None and self._on_discarded:
            self._on_discarded(self.trip, "discarded")
        self._reset()

    # -- internals -------------------------------------------------------

    def _accept(self, point: GpsPoint) -> bool:
        if point.lat is None or point.lon is None:
            return False
        if not (-90.0 <= point.lat <= 90.0 and -180.0 <= point.lon <= 180.0):
            return False
        if point.lat == 0.0 and point.lon == 0.0:
            return False
        if (
            point.accuracy is not None
            and self.config.max_accuracy_m > 0
            and point.accuracy > self.config.max_accuracy_m
        ):
            self.rejected_points += 1
            return False
        return True

    def _check_start(self, point: GpsPoint, smoothed: float) -> None:
        if smoothed >= self.config.start_speed_kmh:
            if self._above_since is None:
                self._above_since = point.ts
                self._candidate = [point]
                self.state = STATE_CANDIDATE
            else:
                self._candidate.append(point)
            if point.ts - self._above_since >= self.config.start_duration_s:
                self._open_trip()
        else:
            self._above_since = None
            self._candidate = [point]
            self.state = STATE_IDLE

    def _open_trip(self) -> None:
        self.trip = Trip(uid=uuid.uuid4().hex)
        self.state = STATE_ACTIVE
        self._below_since = None
        # Backfill the candidate window so the trip starts where we actually
        # started moving, not 45 s later.
        previous_ts: float | None = None
        for point in self._candidate:
            self.trip.points.append(point)
            self.trip.distance_m += point.distance_m
            self.trip.max_speed_kmh = max(self.trip.max_speed_kmh, point.speed_kmh)
            moving = point.speed_kmh >= MOVING_SPEED_THRESHOLD_KMH
            if previous_ts is not None and moving:
                self.trip.moving_time_s += point.ts - previous_ts
            previous_ts = point.ts
        self._candidate = []
        self._above_since = None
        if self._on_started:
            self._on_started(self.trip)

    def _accumulate(self, point: GpsPoint, dt: float) -> None:
        assert self.trip is not None
        self.trip.points.append(point)
        self.trip.distance_m += point.distance_m
        self.trip.max_speed_kmh = max(self.trip.max_speed_kmh, point.speed_kmh)
        if point.speed_kmh >= MOVING_SPEED_THRESHOLD_KMH:
            self.trip.moving_time_s += dt

    def _check_stop(self, point: GpsPoint, smoothed: float) -> None:
        if smoothed < self.config.stop_speed_kmh:
            if self._below_since is None:
                self._below_since = point.ts
            elif point.ts - self._below_since >= self.config.stop_duration_s:
                self._finish("stopped")
        else:
            self._below_since = None

    def _finish(self, reason: str) -> None:
        trip = self.trip
        self._reset()
        if trip is None:
            return

        # Trim the trailing standstill so parked time is not part of the trip.
        trip = _trim_tail(trip, self.config.stop_speed_kmh)
        trip.finalize(self.config)

        too_short = (
            trip.distance_m < self.config.min_distance_m
            or trip.duration_s < self.config.min_duration_s
        )
        if too_short and not trip.manual:
            if self._on_discarded:
                self._on_discarded(trip, f"too_short:{reason}")
            return

        if self._on_finished:
            self._on_finished(trip)

    def _reset(self) -> None:
        self.trip = None
        self.state = STATE_IDLE
        self._below_since = None
        self._above_since = None
        self._candidate = []


def _trim_tail(trip: Trip, stop_speed_kmh: float) -> Trip:
    """Drop trailing points that were recorded while standing still."""
    points = trip.points
    index = len(points) - 1
    while index > 1 and points[index].speed_kmh < stop_speed_kmh:
        index -= 1
    if index < len(points) - 1:
        removed = points[index + 1 :]
        trip.points = points[: index + 1]
        trip.distance_m -= sum(p.distance_m for p in removed)
        trip.distance_m = max(0.0, trip.distance_m)
    return trip
