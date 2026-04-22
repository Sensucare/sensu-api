from typing import Dict, Optional, Any, List
from pydantic import BaseModel, Field, field_validator

# ==================== EVIEW DEVICE MODELS ====================

# Map legacy device type names to normalized values
DEVICE_TYPE_ALIASES = {
    'eview_button': 'PENDANT',
    'pendant': 'PENDANT',
    'PENDANT': 'PENDANT',
    'hub': 'HUB',
    'HUB': 'HUB',
}


class DeviceAssociation(BaseModel):
    """Device association (unified for watches and Eview buttons)"""
    id: str
    user_id: str
    device_id: str
    device_type: str  # 'PENDANT' or 'HUB'
    device_name: Optional[str] = None
    label: Optional[str] = None
    is_primary: bool = False
    linked_at: Optional[str] = None


class LinkDeviceRequest(BaseModel):
    """Request to link a device to user"""
    device_id: str = Field(..., min_length=10, max_length=20, pattern=r'^\d+$')
    device_type: str = Field('PENDANT')
    label: Optional[str] = Field(None, max_length=100)
    product_id: Optional[str] = Field(None, max_length=50)

    @field_validator('device_type')
    @classmethod
    def normalize_device_type(cls, v: str) -> str:
        """Normalize device_type to PENDANT or HUB, accepting aliases like 'eview_button'."""
        normalized = DEVICE_TYPE_ALIASES.get(v)
        if normalized is None:
            raise ValueError(f"Invalid device_type: {v}. Must be one of: {list(DEVICE_TYPE_ALIASES.keys())}")
        return normalized


class EviewStatus(BaseModel):
    """Eview device status"""
    device_id: str
    device_name: Optional[str] = None
    online: bool = False
    last_seen: Optional[str] = None
    battery: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy_meters: Optional[float] = None
    is_gps: Optional[bool] = None
    is_wifi: Optional[bool] = None
    is_gsm: Optional[bool] = None
    is_motion: Optional[bool] = None
    is_charging: Optional[bool] = None
    work_mode: Optional[int] = None
    signal_strength: Optional[int] = None


class EviewEvent(BaseModel):
    """Eview device event"""
    id: str
    device_id: str
    event_type: str
    timestamp: str
    device_name: Optional[str] = None
    battery: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy_meters: Optional[float] = None
    is_gps: Optional[bool] = None
    is_wifi: Optional[bool] = None
    is_gsm: Optional[bool] = None
    is_motion: Optional[bool] = None
    button_type: Optional[str] = None
    processed_at: Optional[str] = None
    created_at: Optional[str] = None


class EviewMQTTStatus(BaseModel):
    """MQTT service status"""
    connected: bool
    running: bool
    broker: str
    client_id: str
    product_id: str
    monitored_devices: List[str]
    monitored_device_count: int


# ==================== DEVICE CONFIGURATION ENDPOINTS ====================

# --- Pydantic Models for Device Config ---

class FallDetectionConfigRequest(BaseModel):
    """Request body for configuring fall detection."""
    enabled: bool
    sensitivity: int = Field(1, ge=1, le=9, description="Sensitivity 1-9 (1=least, 9=most)")
    dial: bool = Field(True, description="Call SOS numbers on fall detection")


class FallDetectionConfigResponse(BaseModel):
    """Fall detection configuration response."""
    enabled: bool
    sensitivity: int
    dial: bool
    device_id: str


class GeofenceRequest(BaseModel):
    """Request body for creating/updating a geofence."""
    name: str = Field(..., min_length=1, max_length=100)
    center_lat: float = Field(..., ge=-90, le=90)
    center_lng: float = Field(..., ge=-180, le=180)
    radius_meters: float = Field(..., ge=50, le=65535)
    direction: str = Field("LEAVE")
    enabled: bool = Field(True)
    detect_interval_seconds: int = Field(180, ge=60, le=86400)

    @field_validator('direction')
    @classmethod
    def normalize_direction(cls, v: str) -> str:
        """Accept both app format (in/out/both) and backend format (ENTER/LEAVE/BOTH)."""
        mapping = {
            'in': 'ENTER', 'out': 'LEAVE', 'both': 'BOTH',
            'ENTER': 'ENTER', 'LEAVE': 'LEAVE', 'BOTH': 'BOTH',
        }
        normalized = mapping.get(v)
        if normalized is None:
            raise ValueError(f"Invalid direction: {v}. Must be one of: in, out, both, ENTER, LEAVE, BOTH")
        return normalized


class GeofenceResponse(BaseModel):
    """Geofence configuration response."""
    id: Optional[str] = None
    user_id: Optional[str] = None
    device_id: str
    zone_number: int
    name: str
    enabled: bool = True
    shape: str = "circle"
    center_lat: float
    center_lng: float
    radius_meters: float
    direction: str
    detect_interval_seconds: int = 300
    synced_to_device: bool = False
    last_synced_at: Optional[str] = None
    created_at: Optional[str] = None


class BatteryConfigRequest(BaseModel):
    """Request body for battery alert configuration."""
    threshold: int = Field(20, ge=5, le=50, description="Battery low threshold percentage")


class BatteryConfigResponse(BaseModel):
    """Battery configuration response."""
    threshold: int
    device_id: str


class DeviceAlertResponse(BaseModel):
    """Unified alert response."""
    id: str
    device_id: str
    event_type: str
    priority: str
    timestamp: str
    message: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    battery: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None




# ==================== CONTACT NUMBER MANAGEMENT ====================

class ContactNumberEntry(BaseModel):
    """A single authorized contact number on the device."""
    index: int = Field(..., ge=0, le=9, description="Contact slot 0-9")
    number: str = Field(..., max_length=20, description="Phone number")
    enabled: bool = Field(True)
    call: bool = Field(True, description="Device can dial this number")
    sms: bool = Field(True, description="Device accepts SMS from this number")


class ContactNumberResponse(BaseModel):
    """A single contact number as returned from the device."""
    index: int
    number: str
    enabled: bool
    call: bool
    sms: bool
    flag: int


class ContactNumbersResponse(BaseModel):
    """All authorized contact numbers for a device."""
    device_id: str
    contacts: List[ContactNumberResponse]


class SetContactNumberRequest(BaseModel):
    """Request to set a single authorized contact number."""
    index: int = Field(..., ge=0, le=9, description="Contact slot 0-9")
    number: str = Field(..., min_length=1, max_length=20, pattern=r'^\+?\d+$', description="Phone number")
    enabled: bool = Field(True)
    call: bool = Field(True, description="Allow device to dial this number")
    sms: bool = Field(True, description="Accept SMS from this number")


class DeleteContactNumberRequest(BaseModel):
    """Request to clear a contact number slot."""
    index: int = Field(..., ge=0, le=9, description="Contact slot 0-9 to clear")


# Only these event types are user-facing alerts (excludes trackerRealTime, trackerAlarm, etc.)
ALERT_EVENT_TYPES = [
    "sos", "fall_detection", "battery_low",
    "geofence_enter", "geofence_exit", "button_press",
]

# Alert priority and message maps
ALERT_PRIORITY_MAP = {
    "fall_detection": "critical",
    "sos": "critical",
    "geofence_exit": "high",
    "geofence_enter": "high",
    "battery_low": "high",
    "button_press": "high",
}

ALERT_MESSAGE_MAP = {
    "fall_detection": "Caída detectada",
    "sos": "Se presionó el botón SOS",
    "geofence_exit": "El dispositivo salió de la zona segura",
    "geofence_enter": "El dispositivo entró a la zona segura",
    "battery_low": "Nivel de batería bajo",
    "button_press": "Botón presionado",
}
