from dataclasses import dataclass

from scripts.const import (
    FLAT_USAGE_SENSOR_NAME,
    MONTH_FLAT_USAGE_SENSOR_NAME,
    MONTH_PEAK_USAGE_SENSOR_NAME,
    MONTH_TIP_USAGE_SENSOR_NAME,
    MONTH_VALLEY_USAGE_SENSOR_NAME,
    PEAK_USAGE_SENSOR_NAME,
    TIP_USAGE_SENSOR_NAME,
    VALLEY_USAGE_SENSOR_NAME,
    YEARLY_FLAT_USAGE_SENSOR_NAME,
    YEARLY_PEAK_USAGE_SENSOR_NAME,
    YEARLY_TIP_USAGE_SENSOR_NAME,
    YEARLY_VALLEY_USAGE_SENSOR_NAME,
)


@dataclass(frozen=True)
class SensorSpec:
    sensor_name: str
    icon: str


TOU_DAILY_SENSORS = {
    "valley_usage": SensorSpec(VALLEY_USAGE_SENSOR_NAME, "mdi:weather-night"),
    "flat_usage": SensorSpec(FLAT_USAGE_SENSOR_NAME, "mdi:weather-sunset-up"),
    "peak_usage": SensorSpec(PEAK_USAGE_SENSOR_NAME, "mdi:chart-bell-curve"),
    "tip_usage": SensorSpec(TIP_USAGE_SENSOR_NAME, "mdi:chart-bell-curve-cumulative"),
}

TOU_PERIOD_SENSORS = {
    "month": {
        "valley_usage": SensorSpec(MONTH_VALLEY_USAGE_SENSOR_NAME, "mdi:weather-night"),
        "flat_usage": SensorSpec(MONTH_FLAT_USAGE_SENSOR_NAME, "mdi:weather-sunset-up"),
        "peak_usage": SensorSpec(MONTH_PEAK_USAGE_SENSOR_NAME, "mdi:chart-bell-curve"),
        "tip_usage": SensorSpec(MONTH_TIP_USAGE_SENSOR_NAME, "mdi:chart-bell-curve-cumulative"),
    },
    "year": {
        "valley_usage": SensorSpec(YEARLY_VALLEY_USAGE_SENSOR_NAME, "mdi:weather-night"),
        "flat_usage": SensorSpec(YEARLY_FLAT_USAGE_SENSOR_NAME, "mdi:weather-sunset-up"),
        "peak_usage": SensorSpec(YEARLY_PEAK_USAGE_SENSOR_NAME, "mdi:chart-bell-curve"),
        "tip_usage": SensorSpec(YEARLY_TIP_USAGE_SENSOR_NAME, "mdi:chart-bell-curve-cumulative"),
    },
}


def tou_detail_enabled() -> bool:
    import os

    return os.getenv("PUBLISH_TOU_DETAIL_SENSORS", "false").lower() == "true"
