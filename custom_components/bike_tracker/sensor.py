"""Statistics sensors for Bike Tracker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfLength, UnitOfSpeed, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    PERIOD_MONTH,
    PERIOD_TODAY,
    PERIOD_TOTAL,
    PERIOD_WEEK,
    PERIOD_YEAR,
)
from .coordinator import BikeTrackerCoordinator
from .entity import BikeTrackerEntity


@dataclass(frozen=True, kw_only=True)
class BikeSensorDescription(SensorEntityDescription):
    """Sensor description with a value getter."""

    value_fn: Callable[[BikeTrackerCoordinator], Any]
    attrs_fn: Callable[[BikeTrackerCoordinator], dict[str, Any]] | None = None


def _distance(period: str) -> Callable[[BikeTrackerCoordinator], float]:
    return lambda c: round(c.stat(period, "distance_m") / 1000.0, 2)


def _duration(period: str) -> Callable[[BikeTrackerCoordinator], float]:
    return lambda c: round(c.stat(period, "moving_time_s") / 60.0, 1)


def _trips(period: str) -> Callable[[BikeTrackerCoordinator], int]:
    return lambda c: int(c.stat(period, "trips"))


def _elevation(period: str) -> Callable[[BikeTrackerCoordinator], float]:
    return lambda c: round(c.stat(period, "elevation_gain_m"), 0)


def _last(key: str, default: Any = None) -> Callable[[BikeTrackerCoordinator], Any]:
    def getter(coordinator: BikeTrackerCoordinator) -> Any:
        trip = coordinator.last_trip
        return trip.get(key, default) if trip else default

    return getter


DISTANCE_KM = {
    "device_class": SensorDeviceClass.DISTANCE,
    "native_unit_of_measurement": UnitOfLength.KILOMETERS,
    "suggested_display_precision": 2,
}
MINUTES = {
    "device_class": SensorDeviceClass.DURATION,
    "native_unit_of_measurement": UnitOfTime.MINUTES,
    "suggested_display_precision": 0,
}
SPEED = {
    "device_class": SensorDeviceClass.SPEED,
    "native_unit_of_measurement": UnitOfSpeed.KILOMETERS_PER_HOUR,
    "suggested_display_precision": 1,
}
METRES = {
    "device_class": SensorDeviceClass.DISTANCE,
    "native_unit_of_measurement": UnitOfLength.METERS,
    "suggested_display_precision": 0,
}

SENSORS: tuple[BikeSensorDescription, ...] = (
    # --- Live -----------------------------------------------------------
    BikeSensorDescription(
        key="current_speed",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:speedometer",
        value_fn=lambda c: c.tracker.current_speed_kmh,
        **SPEED,
    ),
    BikeSensorDescription(
        key="current_trip_distance",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-marker-distance",
        value_fn=lambda c: round(c.current_trip_distance_m / 1000.0, 2),
        attrs_fn=lambda c: {"trip": c.current_trip_dict()},
        **DISTANCE_KM,
    ),
    BikeSensorDescription(
        key="current_trip_duration",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-outline",
        value_fn=lambda c: round(c.current_trip_duration_s / 60.0, 1),
        **MINUTES,
    ),
    # --- Last trip ------------------------------------------------------
    BikeSensorDescription(
        key="last_trip_distance",
        icon="mdi:bike",
        value_fn=lambda c: round((_last("distance_m", 0.0)(c) or 0.0) / 1000.0, 2),
        attrs_fn=lambda c: {
            "trip_id": _last("id")(c),
            "activity": _last("activity")(c),
            "confidence": _last("activity_confidence")(c),
            "started_at": _iso(_last("started_at")(c)),
            "ended_at": _iso(_last("ended_at")(c)),
            "max_speed_kmh": _last("max_speed_kmh")(c),
            "elevation_gain_m": _last("elevation_gain_m")(c),
            "elevation_loss_m": _last("elevation_loss_m")(c),
        },
        **DISTANCE_KM,
    ),
    BikeSensorDescription(
        key="last_trip_duration",
        icon="mdi:timer",
        value_fn=lambda c: round((_last("moving_time_s", 0.0)(c) or 0.0) / 60.0, 1),
        **MINUTES,
    ),
    BikeSensorDescription(
        key="last_trip_avg_speed",
        icon="mdi:speedometer-medium",
        value_fn=lambda c: _last("avg_moving_kmh", 0.0)(c),
        **SPEED,
    ),
    BikeSensorDescription(
        key="last_trip_elevation_gain",
        icon="mdi:elevation-rise",
        value_fn=lambda c: _last("elevation_gain_m", 0.0)(c),
        **METRES,
    ),
)


def _period_sensors() -> tuple[BikeSensorDescription, ...]:
    """Build distance/duration/trip-count/elevation sensors per period."""
    result: list[BikeSensorDescription] = []
    for period in (PERIOD_TODAY, PERIOD_WEEK, PERIOD_MONTH, PERIOD_YEAR, PERIOD_TOTAL):
        # Calendar buckets reset, so TOTAL_INCREASING would be wrong for them.
        # The all-time totals are TOTAL, not TOTAL_INCREASING: they go down
        # when a trip is deleted, merged or reclassified, and HA would read
        # that drop as a meter reset and add the whole total to the sum again.
        state_class = (
            SensorStateClass.TOTAL
            if period == PERIOD_TOTAL
            else SensorStateClass.MEASUREMENT
        )
        result.extend(
            [
                BikeSensorDescription(
                    key=f"distance_{period}",
                    state_class=state_class,
                    icon="mdi:bike",
                    value_fn=_distance(period),
                    attrs_fn=lambda c, p=period: {
                        "trips": int(c.stat(p, "trips")),
                        "longest_trip_km": round(c.stat(p, "longest_trip_m") / 1000, 2),
                        "max_speed_kmh": round(c.stat(p, "max_speed_kmh"), 1),
                        "avg_speed_kmh": round(c.stat(p, "avg_speed_kmh"), 1),
                    },
                    **DISTANCE_KM,
                ),
                BikeSensorDescription(
                    key=f"duration_{period}",
                    state_class=state_class,
                    icon="mdi:timer-sand",
                    value_fn=_duration(period),
                    **MINUTES,
                ),
                BikeSensorDescription(
                    key=f"trips_{period}",
                    state_class=state_class,
                    icon="mdi:counter",
                    native_unit_of_measurement="trips",
                    value_fn=_trips(period),
                ),
                BikeSensorDescription(
                    key=f"elevation_{period}",
                    state_class=state_class,
                    icon="mdi:elevation-rise",
                    value_fn=_elevation(period),
                    **METRES,
                ),
            ]
        )
    return tuple(result)


def _iso(timestamp: float | None) -> str | None:
    if not timestamp:
        return None
    return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinator: BikeTrackerCoordinator = hass.data[DOMAIN][entry.entry_id]
    descriptions = SENSORS + _period_sensors()
    async_add_entities(BikeTrackerSensor(coordinator, desc) for desc in descriptions)


class BikeTrackerSensor(BikeTrackerEntity, SensorEntity):
    """A single statistics sensor."""

    entity_description: BikeSensorDescription

    def __init__(
        self, coordinator: BikeTrackerCoordinator, description: BikeSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        try:
            return self.entity_description.value_fn(self.coordinator)
        except (TypeError, ValueError, KeyError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        try:
            return self.entity_description.attrs_fn(self.coordinator)
        except (TypeError, ValueError, KeyError):
            return None
