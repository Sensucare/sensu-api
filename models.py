import datetime
from typing import Dict, Optional, Any, List
from pydantic import BaseModel, Field


class WatchAssociation(BaseModel):
    imei: str
    label: Optional[str] = None
    linked_at: str
    updated_at: str


class LinkWatchRequest(BaseModel):
    imei: str = Field(..., min_length=10, max_length=20, pattern=r'^\d+$')
    label: Optional[str] = Field(None, max_length=100)


class UnlinkWatchResponse(BaseModel):
    detail: str


class SendCommandRequest(BaseModel):
    command: str
    params: Optional[str] = ""


class RawCommandRequest(BaseModel):
    payload: str


class SchedulerConfigRequest(BaseModel):
    test_interval_seconds: Optional[int] = None
    auto_test_interval_minutes: Optional[int] = None
    enabled_tests: Optional[List[str]] = None
    auto_configure_on_login: Optional[bool] = None


class FallDetectionConfig(BaseModel):
    enabled: bool
    sensitivity: int  # 1-3 (1=low, 2=medium, 3=high)


class WorkingModeRequest(BaseModel):
    mode: int = Field(..., ge=1, le=3, description="1=normal (15min), 2=power-saving (60min), 3=emergency (1min)")


class CustomModeRequest(BaseModel):
    interval_seconds: int = Field(..., ge=30, description="Reporting interval in seconds (minimum 30)")
    gps_enabled: bool = Field(True, description="Enable GPS tracking")


class ReminderItem(BaseModel):
    time: str = Field(..., pattern="^[0-2][0-9]:[0-5][0-9]$", description="Time in HH:MM format")
    days: str = Field("1234567", description="Days of week (1=Mon, 7=Sun)")
    enabled: bool = Field(True)
    type: int = Field(1, ge=1, le=3, description="1=medicine, 2=water, 3=sedentary")


class RemindersRequest(BaseModel):
    reminders: List[ReminderItem] = Field(..., min_items=1, max_items=10)


class BPCalibrationRequest(BaseModel):
    systolic: int = Field(..., ge=60, le=250, description="Systolic blood pressure (mmHg)")
    diastolic: int = Field(..., ge=40, le=150, description="Diastolic blood pressure (mmHg)")
    age: int = Field(..., ge=1, le=120, description="User age")
    is_male: bool = Field(..., description="True for male, False for female")


class FallEvent(BaseModel):
    id: int
    imei: str
    timestamp: str
    alarm_type: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    location_raw: Optional[str] = None
    device_status: Optional[str] = None
    processed_at: str
    created_at: str


class FallEventStats(BaseModel):
    total_events: int
    events_by_device: List[Dict[str, Any]]
    events_by_type: List[Dict[str, Any]]
    events_by_day: List[Dict[str, Any]]


class AlarmEvent(BaseModel):
    id: int
    imei: str
    timestamp: str
    alarm_type: str
    alarm_description: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    speed_kmh: Optional[float] = None
    direction_deg: Optional[float] = None
    gsm_signal: Optional[int] = None
    satellites: Optional[int] = None
    battery_level: Optional[int] = None
    remaining_space: Optional[int] = None
    fortification_state: Optional[int] = None
    working_mode: Optional[int] = None
    mcc: Optional[int] = None
    mnc: Optional[int] = None
    lac: Optional[int] = None
    cid: Optional[int] = None
    language: Optional[str] = None
    reply_flags: Optional[str] = None
    wifi_data: Optional[List[Dict[str, Any]]] = None
    location_raw: Optional[str] = None
    device_status: Optional[Dict[str, Any]] = None
    processed_at: str
    created_at: str


class AlarmEventStats(BaseModel):
    total_events: int
    events_by_device: List[Dict[str, Any]]
    events_by_type: List[Dict[str, Any]]
    events_by_day: List[Dict[str, Any]]


def _device_to_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    if not d:
        return {}
    # Convert datetimes to ISO strings
    def _iso(dt):
        return dt.isoformat() if isinstance(dt, datetime.datetime) else dt
    out = {
        "status": d.get("status"),
        "last_seen": _iso(d.get("last_seen")),
        "last_location": d.get("last_location"),
        "last_alarm": d.get("last_alarm"),
        "metrics": d.get("metrics", {}),
        "fall_detection_config": d.get("fall_detection_config"),
    }
    if out.get("last_location") and isinstance(out["last_location"], dict):
        out["last_location"] = {
            **out["last_location"],
            "received_at": _iso(out["last_location"].get("received_at")),
        }
    if out.get("last_alarm") and isinstance(out["last_alarm"], dict):
        out["last_alarm"] = {
            **out["last_alarm"],
            "received_at": _iso(out["last_alarm"].get("received_at")),
        }
    if out.get("metrics") and isinstance(out["metrics"], dict):
        temp = out["metrics"].get("temperature")
        if isinstance(temp, dict):
            temp["received_at"] = _iso(temp.get("received_at"))
        hr = out["metrics"].get("heart_rate")
        if isinstance(hr, dict):
            hr["received_at"] = _iso(hr.get("received_at"))
        bp = out["metrics"].get("blood_pressure")
        if isinstance(bp, dict):
            bp["received_at"] = _iso(bp.get("received_at"))
        spo2 = out["metrics"].get("blood_oxygen")
        if isinstance(spo2, dict):
            spo2["received_at"] = _iso(spo2.get("received_at"))
        bs = out["metrics"].get("blood_sugar")
        if isinstance(bs, dict):
            bs["received_at"] = _iso(bs.get("received_at"))
        health = out["metrics"].get("health")
        if isinstance(health, dict):
            health["received_at"] = _iso(health.get("received_at"))
        # Handle fall events metrics
        fall_events = out["metrics"].get("fall_events")
        if isinstance(fall_events, dict) and "last_event" in fall_events:
            fall_events["last_event"] = _iso(fall_events["last_event"])
    return out


def _format_watch_association(record: Dict[str, Any]) -> Dict[str, Any]:
    """Shape database watch association payload for API responses."""
    return {
        "imei": record.get("imei"),
        "label": record.get("label"),
        "linked_at": record.get("linked_at"),
        "updated_at": record.get("updated_at"),
    }
