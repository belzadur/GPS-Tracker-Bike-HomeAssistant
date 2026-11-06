"""End-to-end tests of the trip detection state machine."""

from bt.const import ACTIVITY_BIKE, ACTIVITY_WALK
from bt.tracker import GpsPoint, TrackerConfig, TripTracker, trip_from_points
from simulator import standstill, straight_track

T0 = 1_700_000_000.0
LAT, LON = 52.5, 13.4


def run(points, config=None):
    finished, discarded = [], []
    tracker = TripTracker(
        config=config or TrackerConfig(),
        on_trip_finished=finished.append,
        on_trip_discarded=lambda trip, reason: discarded.append((trip, reason)),
    )
    for point in points:
        tracker.add_point(point)
    tracker.tick(points[-1].ts + 10_000)
    return tracker, finished, discarded


def test_detects_a_bike_ride():
    points = (
        standstill(T0, LAT, LON, 300)
        + straight_track(T0 + 310, LAT, LON, 20.0, 1200)
        + standstill(T0 + 1520, LAT, LON + 0.1, 400)
    )
    _, finished, _ = run(points)

    assert len(finished) == 1
    trip = finished[0]
    # 20 km/h for 20 minutes is ~6.7 km.
    assert 6000 < trip.distance_m < 7200
    assert trip.activity == ACTIVITY_BIKE
    assert trip.duration_s > 1000


def test_walking_is_not_a_bike_ride():
    points = straight_track(T0, LAT, LON, 5.0, 2400)
    _, finished, _ = run(points)
    # A walk is slower than the start threshold, so no trip opens at all.
    assert finished == []


def test_brisk_walk_is_classified_as_walk():
    config = TrackerConfig(start_speed_kmh=4.0, start_duration_s=30)
    points = straight_track(T0, LAT, LON, 6.0, 2400) + standstill(
        T0 + 2410, LAT, LON, 300
    )
    _, finished, _ = run(points, config)
    assert len(finished) == 1
    assert finished[0].activity == ACTIVITY_WALK


def test_short_hop_is_discarded():
    points = (
        straight_track(T0, LAT, LON, 18.0, 60)
        + standstill(T0 + 70, LAT, LON + 0.004, 300)
    )
    _, finished, discarded = run(points)
    assert finished == []
    assert discarded and discarded[0][1].startswith("too_short")


def test_stop_ends_the_trip_and_a_second_ride_is_separate():
    points = (
        straight_track(T0, LAT, LON, 22.0, 900)
        + standstill(T0 + 910, LAT, LON + 0.08, 900)
        + straight_track(T0 + 1820, LAT, LON + 0.08, 22.0, 900)
        + standstill(T0 + 2730, LAT, LON + 0.16, 600)
    )
    _, finished, _ = run(points)
    assert len(finished) == 2


def test_gps_teleport_is_rejected():
    points = straight_track(T0, LAT, LON, 20.0, 1200)
    points.insert(60, GpsPoint(ts=points[60].ts - 1, lat=48.1, lon=11.5, accuracy=5.0))
    tracker, finished, _ = run(points + standstill(T0 + 1210, LAT, LON + 0.1, 400))
    assert tracker.rejected_points >= 1
    assert len(finished) == 1
    assert finished[0].distance_m < 20_000


def test_inaccurate_fixes_are_dropped():
    points = straight_track(T0, LAT, LON, 20.0, 600, accuracy=250.0)
    tracker, finished, _ = run(points)
    assert finished == []
    assert tracker.rejected_points == len(points)


def test_elevation_gain_is_measured():
    points = straight_track(
        T0,
        LAT,
        LON,
        20.0,
        1800,
        altitude_start=100.0,
        altitude_rate_m_per_km=10.0,
    ) + standstill(T0 + 1810, LAT, LON + 0.15, 400)
    _, finished, _ = run(points)
    assert len(finished) == 1
    # 10 km at +10 m/km = ~100 m of climbing.
    assert 80 < finished[0].elevation_gain_m < 120
    assert finished[0].elevation_loss_m == 0.0


def test_trailing_standstill_is_trimmed():
    points = straight_track(T0, LAT, LON, 20.0, 1200) + standstill(
        T0 + 1210, LAT, LON + 0.1, 600
    )
    _, finished, _ = run(points)
    trip = finished[0]
    # The parked minutes at the end must not inflate the duration.
    assert trip.duration_s < 1400


def test_manual_start_and_stop():
    tracker = TripTracker()
    points = straight_track(T0, LAT, LON, 12.0, 300)
    tracker.add_point(points[0])
    tracker.force_start(points[0])
    assert tracker.is_recording
    for point in points[1:]:
        tracker.add_point(point)
    finished = []
    tracker._on_finished = finished.append
    tracker.force_stop()
    assert len(finished) == 1
    assert finished[0].manual is True


def test_discard_drops_the_trip():
    tracker, _, _ = run(straight_track(T0, LAT, LON, 20.0, 600))
    tracker.discard()
    assert tracker.trip is None
    assert not tracker.is_recording


# --- a phone that reports no speed --------------------------------------


def zero_speed(points):
    """Android's companion app fills unknown fields with 0 instead of
    omitting them, so a phone without a speed fix sends ``speed: 0`` while
    its position moves across town."""
    for point in points:
        point.reported_speed_kmh = 0.0
    return points


def test_ride_is_detected_although_the_phone_reports_zero_speed():
    points = (
        standstill(T0, LAT, LON, 300)
        + zero_speed(straight_track(T0 + 310, LAT, LON, 22.0, 1200))
        + standstill(T0 + 1520, LAT, LON + 0.1, 400)
    )

    _, finished, _ = run(points)

    assert len(finished) == 1, "a reported speed of 0 must not mask real movement"
    assert finished[0].activity == ACTIVITY_BIKE
    # 22 km/h for 20 minutes is ~7.3 km, plus the buffered candidate points.
    assert 6800 < finished[0].distance_m < 8200


def test_a_real_standstill_stays_a_standstill_without_reported_speed():
    # The counterpart: falling back to the derived speed must not invent
    # movement when the rider is genuinely parked.
    points = zero_speed(standstill(T0, LAT, LON, 1800))

    tracker, finished, _ = run(points)

    assert finished == []
    assert tracker.current_speed_kmh < 1.0


def test_reported_speed_still_wins_when_the_device_has_one():
    points = straight_track(T0, LAT, LON, 20.0, 600)
    for point in points:
        point.reported_speed_kmh = 25.0

    run(points)

    # 25 reported beats the ~20 derived from the positions.
    assert all(p.speed_kmh == 25.0 for p in points[1:])


# --- a phone that has a speed fix but stalls its position ----------------


def stalled_then_catching_up(points, first, count, catch_up_s=4.0):
    """The phone stops updating its position for ``count`` fixes, reports 0
    meanwhile and then catches up on the whole distance in one step that
    arrives ``catch_up_s`` seconds after the last stalled fix."""
    frozen = points[first - 1]
    for point in points[first : first + count]:
        point.lat, point.lon = frozen.lat, frozen.lon
        point.reported_speed_kmh = 0.0
    catch_up = points[first + count]
    catch_up.ts = points[first + count - 1].ts + catch_up_s
    catch_up.reported_speed_kmh = 0.0
    return points


def reporting(points, speed_kmh):
    for point in points:
        point.reported_speed_kmh = speed_kmh
    return points


def test_zero_from_a_device_with_speed_fix_is_trusted():
    ride = stalled_then_catching_up(
        reporting(straight_track(T0 + 310, LAT, LON, 20.0, 1200), 20.0), 60, 2
    )
    points = (
        reporting(standstill(T0, LAT, LON, 300), 0.0)
        + ride
        + reporting(standstill(T0 + 1520, LAT, LON + 0.1, 400), 0.0)
    )

    _, finished, _ = run(points)

    assert len(finished) == 1
    # Derived from the catch-up step this would be ~150 km/h.
    assert finished[0].max_speed_kmh <= 20.0


def test_trip_from_points_trusts_zero_from_a_device_with_speed_fix():
    points = stalled_then_catching_up(
        reporting(straight_track(T0, LAT, LON, 20.0, 600), 20.0), 30, 2
    )

    trip = trip_from_points(points, TrackerConfig(), ACTIVITY_BIKE)

    assert trip.max_speed_kmh <= 20.0


def test_manual_start_ignores_an_old_fix():
    # The phone sat still for five hours, so the last fix is old. A manual
    # start must not stretch the trip back to it.
    tracker = TripTracker()
    old = straight_track(T0, LAT, LON, 0.0, 0)[0]
    tracker.add_point(old)
    later = T0 + 5 * 3600
    tracker.force_start(now=later)
    ride = straight_track(later + 5, LAT, LON + 0.001, 16.0, 600)
    for point in ride:
        tracker.add_point(point)
    finished = []
    tracker._on_finished = finished.append
    tracker.force_stop()
    trip = finished[0]
    assert trip.duration_s < 700
    assert trip.moving_time_s < 700


def test_manual_start_keeps_a_fresh_fix():
    tracker = TripTracker()
    first = straight_track(T0, LAT, LON, 12.0, 0)[0]
    tracker.add_point(first)
    tracker.force_start(now=T0 + 20)
    assert tracker.trip.points == [first]
