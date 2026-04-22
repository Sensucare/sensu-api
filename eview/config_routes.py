import datetime
import json
import logging
import os
from typing import Dict, Optional

import boto3
from fastapi import APIRouter, HTTPException, Query, Depends

from auth.core import get_current_user
from eview.models import (
    FallDetectionConfigRequest, FallDetectionConfigResponse,
    GeofenceRequest, GeofenceResponse,
    BatteryConfigRequest, BatteryConfigResponse,
    DeviceAlertResponse,
    ALERT_EVENT_TYPES, ALERT_PRIORITY_MAP, ALERT_MESSAGE_MAP,
    ContactNumberResponse, ContactNumbersResponse,
    SetContactNumberRequest,
)

logger = logging.getLogger(__name__)

# ─── SQS Device Command Queue ───────────────────────────────────────────────

DEVICE_COMMAND_QUEUE_URL = os.getenv("DEVICE_COMMAND_QUEUE_URL")

_sqs_client = None


def _get_sqs_client():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client(
            "sqs",
            region_name=os.getenv("AWS_DEFAULT_REGION", "us-west-1"),
        )
    return _sqs_client


def enqueue_device_command(command: str, device_id: str, payload: dict):
    """Publish a device command to the SQS queue for async processing."""
    queue_url = DEVICE_COMMAND_QUEUE_URL
    if not queue_url:
        logger.warning(
            f"DEVICE_COMMAND_QUEUE_URL not set — falling back to sync for {command}"
        )
        return False

    message = {
        "command": command,
        "device_id": device_id,
        "payload": payload,
        "enqueued_at": datetime.datetime.utcnow().isoformat(),
    }

    try:
        _get_sqs_client().send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(message),
        )
        logger.info(f"Enqueued {command} for device {device_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to enqueue {command} for device {device_id}: {e}")
        return False

router = APIRouter()


def get_user_manager():
    from watch_app import user_manager
    return user_manager


def get_eview_event_manager():
    from watch_app import eview_event_manager
    return eview_event_manager


def get_device_settings_manager():
    from watch_app import device_settings_manager
    return device_settings_manager


def get_geofence_manager():
    from watch_app import geofence_manager
    return geofence_manager


def get_evmars_client_instance():
    from watch_app import evmars_client
    return evmars_client


async def _verify_device_access(device_id: str, user_id: str) -> None:
    """Raise 403 if user doesn't own this device."""
    owners = await get_user_manager().get_device_owners(device_id)
    if user_id not in owners:
        raise HTTPException(status_code=403, detail="Access denied to this device")


# --- Fall Detection Config ---

@router.get("/api/device/{device_id}/fall-detection/config", tags=["device-config"],
         summary="Get fall detection config")
async def get_fall_detection_config(device_id: str, current_user: Dict = Depends(get_current_user)):
    """Get the current fall detection configuration for a device."""
    await _verify_device_access(device_id, current_user["user_id"])

    settings = await get_device_settings_manager().get_settings(device_id)
    return FallDetectionConfigResponse(
        device_id=device_id,
        enabled=bool(settings.get("fall_detection_enabled", False)),
        sensitivity=settings.get("fall_sensitivity", 3),
        dial=bool(settings.get("fall_dial_enabled", True)),
    )


@router.put("/api/device/{device_id}/fall-detection/config", tags=["device-config"],
         summary="Update fall detection config")
async def update_fall_detection_config(
    device_id: str,
    config: FallDetectionConfigRequest,
    current_user: Dict = Depends(get_current_user)
):
    """Update fall detection settings and push to device via EVMars API."""
    await _verify_device_access(device_id, current_user["user_id"])

    # Save locally
    await get_device_settings_manager().upsert_settings(
        device_id,
        fall_detection_enabled=config.enabled,
        fall_sensitivity=config.sensitivity,
        fall_dial_enabled=config.dial,
    )

    # Push to device via EVMars API
    result = get_evmars_client_instance().configure_fall_detection(
        device_id=device_id,
        enabled=config.enabled,
        sensitivity=config.sensitivity,
        dial=config.dial,
    )

    if result is None:
        logger.warning(f"Failed to push fall detection config to device {device_id}")

    return FallDetectionConfigResponse(
        device_id=device_id,
        enabled=config.enabled,
        sensitivity=config.sensitivity,
        dial=config.dial,
    )


# --- Geofence CRUD ---

@router.get("/api/device/{device_id}/geofences", tags=["device-config"],
         summary="List geofences for device")
async def list_geofences(device_id: str, current_user: Dict = Depends(get_current_user)):
    """Get all configured geofences for a device."""
    await _verify_device_access(device_id, current_user["user_id"])

    geofences = await get_geofence_manager().get_geofences(device_id)
    return [GeofenceResponse(**g) for g in geofences]


@router.post("/api/device/{device_id}/geofences", tags=["device-config"],
          summary="Create geofence", status_code=201)
async def create_geofence(
    device_id: str,
    geofence: GeofenceRequest,
    current_user: Dict = Depends(get_current_user)
):
    """Create a new geofence zone and enqueue device sync."""
    await _verify_device_access(device_id, current_user["user_id"])

    # Find next available zone number (1-4)
    zone_number = await get_geofence_manager().get_next_available_zone(device_id)
    if zone_number is None:
        raise HTTPException(status_code=409, detail="Maximum 4 geofences per device reached")

    # Save to database (synced_to_device defaults to false)
    await get_geofence_manager().create_geofence(
        user_id=current_user["user_id"],
        device_id=device_id,
        zone_number=zone_number,
        name=geofence.name,
        center_lat=geofence.center_lat,
        center_lng=geofence.center_lng,
        radius_meters=geofence.radius_meters,
        direction=geofence.direction,
        detect_interval_seconds=geofence.detect_interval_seconds,
        enabled=geofence.enabled,
    )

    # Enqueue device sync via SQS (async — Lambda worker handles retries)
    evmars_direction = {"ENTER": "in", "LEAVE": "out", "BOTH": "both"}.get(
        geofence.direction.upper(), "out"
    )
    enqueue_device_command("configure_geofence", device_id, {
        "zone_number": zone_number,
        "center_lat": geofence.center_lat,
        "center_lng": geofence.center_lng,
        "radius_meters": geofence.radius_meters,
        "direction": evmars_direction,
        "enabled": geofence.enabled,
    })

    geo_record = await get_geofence_manager().get_geofence(device_id, zone_number)
    return GeofenceResponse(**geo_record)


@router.put("/api/device/{device_id}/geofences/{zone_number}", tags=["device-config"],
         summary="Update geofence")
async def update_geofence(
    device_id: str,
    zone_number: int,
    geofence: GeofenceRequest,
    current_user: Dict = Depends(get_current_user)
):
    """Update an existing geofence zone and enqueue device sync."""
    await _verify_device_access(device_id, current_user["user_id"])

    if zone_number < 1 or zone_number > 4:
        raise HTTPException(status_code=400, detail="Zone number must be 1-4")

    existing = await get_geofence_manager().get_geofence(device_id, zone_number)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Geofence zone {zone_number} not found")

    # Update in database (marks syncedToDevice=false automatically)
    await get_geofence_manager().update_geofence(device_id, zone_number,
        name=geofence.name,
        center_lat=geofence.center_lat,
        center_lng=geofence.center_lng,
        radius_meters=geofence.radius_meters,
        direction=geofence.direction,
        detect_interval_seconds=geofence.detect_interval_seconds,
        enabled=geofence.enabled,
    )

    # Enqueue device sync via SQS
    evmars_direction = {"ENTER": "in", "LEAVE": "out", "BOTH": "both"}.get(
        geofence.direction.upper(), "out"
    )
    enqueue_device_command("configure_geofence", device_id, {
        "zone_number": zone_number,
        "center_lat": geofence.center_lat,
        "center_lng": geofence.center_lng,
        "radius_meters": geofence.radius_meters,
        "direction": evmars_direction,
        "enabled": geofence.enabled,
    })

    geo_record = await get_geofence_manager().get_geofence(device_id, zone_number)
    return GeofenceResponse(**geo_record)


@router.delete("/api/device/{device_id}/geofences/{zone_number}", tags=["device-config"],
            summary="Delete geofence")
async def delete_geofence(
    device_id: str,
    zone_number: int,
    current_user: Dict = Depends(get_current_user)
):
    """Delete a geofence zone and enqueue device disable."""
    await _verify_device_access(device_id, current_user["user_id"])

    if zone_number < 1 or zone_number > 4:
        raise HTTPException(status_code=400, detail="Zone number must be 1-4")

    existing = await get_geofence_manager().get_geofence(device_id, zone_number)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Geofence zone {zone_number} not found")

    # Enqueue disable on device via SQS
    enqueue_device_command("disable_geofence", device_id, {
        "zone_number": zone_number,
    })

    # Remove from database
    await get_geofence_manager().delete_geofence(device_id, zone_number)

    return {"detail": f"Geofence zone {zone_number} deleted"}


@router.post("/api/device/{device_id}/geofences/sync", tags=["device-config"],
          summary="Sync all geofences to device")
async def sync_geofences(device_id: str, current_user: Dict = Depends(get_current_user)):
    """Enqueue re-sync of all geofences to the device via SQS."""
    await _verify_device_access(device_id, current_user["user_id"])

    geofences = await get_geofence_manager().get_geofences(device_id)
    if not geofences:
        return {"detail": "No geofences to sync", "enqueued": 0}

    enqueued = 0
    for geo in geofences:
        if not geo.get("enabled"):
            enqueue_device_command("disable_geofence", device_id, {
                "zone_number": geo["zone_number"],
            })
        else:
            db_direction = geo.get("direction", "LEAVE")
            evmars_direction = {"ENTER": "in", "LEAVE": "out", "BOTH": "both"}.get(
                db_direction.upper() if db_direction else "LEAVE", "out"
            )
            enqueue_device_command("configure_geofence", device_id, {
                "zone_number": geo["zone_number"],
                "center_lat": geo["center_lat"],
                "center_lng": geo["center_lng"],
                "radius_meters": geo["radius_meters"],
                "direction": evmars_direction,
                "enabled": True,
            })
        enqueued += 1

    return {"detail": f"Sync enqueued for {enqueued} geofence(s)", "enqueued": enqueued}


# --- Battery Config ---

@router.get("/api/device/{device_id}/battery/config", tags=["device-config"],
         summary="Get battery alert config")
async def get_battery_config(device_id: str, current_user: Dict = Depends(get_current_user)):
    """Get the battery low alert threshold configuration."""
    await _verify_device_access(device_id, current_user["user_id"])

    settings = await get_device_settings_manager().get_settings(device_id)
    return BatteryConfigResponse(
        device_id=device_id,
        threshold=settings.get("battery_threshold", 20),
    )


@router.put("/api/device/{device_id}/battery/config", tags=["device-config"],
         summary="Update battery alert config")
async def update_battery_config(
    device_id: str,
    config: BatteryConfigRequest,
    current_user: Dict = Depends(get_current_user)
):
    """Update the battery low alert threshold (stored locally, checked on MQTT events)."""
    await _verify_device_access(device_id, current_user["user_id"])

    await get_device_settings_manager().upsert_settings(
        device_id,
        battery_threshold=config.threshold,
    )

    # Push new threshold to the MQTT service's in-memory cache so it
    # takes effect immediately for incoming events.
    try:
        from eview.mqtt_service import get_mqtt_service
        mqtt_svc = get_mqtt_service()
        if mqtt_svc is not None:
            mqtt_svc.set_battery_threshold(device_id, config.threshold)
    except Exception as e:
        logger.warning(f"Could not update MQTT battery threshold cache: {e}")

    # Read back from DB to confirm the write persisted
    settings = await get_device_settings_manager().get_settings(device_id)

    return BatteryConfigResponse(
        device_id=device_id,
        threshold=settings.get("battery_threshold", 20),
    )


# --- Unified Alerts ---

@router.get("/api/device/{device_id}/alerts", tags=["device-alerts"],
         summary="Get device alerts")
async def get_device_alerts(
    device_id: str,
    event_type: Optional[str] = Query(None, description="Filter: fall_detection, sos, geofence_exit, geofence_enter, battery_low, button_press"),
    start_date: Optional[str] = Query(None, description="Start date (ISO format)"),
    end_date: Optional[str] = Query(None, description="End date (ISO format)"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: Dict = Depends(get_current_user)
):
    """
    Get unified alerts for a device with optional type filtering.
    Returns fall detection, geofence, battery low, SOS, and button press events.
    """
    await _verify_device_access(device_id, current_user["user_id"])

    try:
        start_dt = datetime.datetime.fromisoformat(start_date) if start_date else None
        end_dt = datetime.datetime.fromisoformat(end_date) if end_date else None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")

    # Default to only user-facing alert types; override with explicit filter
    if event_type:
        alert_types = [event_type]
    else:
        alert_types = ALERT_EVENT_TYPES

    events = await get_eview_event_manager().get_events_by_device(
        device_id=device_id,
        event_types=alert_types,
        start_date=start_dt,
        end_date=end_dt,
        limit=limit,
        offset=offset,
    )

    # Transform to alert responses
    alerts = []
    for ev in events:
        ev_type = ev.get("eventType", "unknown")
        raw_payload = ev.get("rawPayload")
        metadata = None
        if isinstance(raw_payload, str):
            try:
                metadata = json.loads(raw_payload)
            except (json.JSONDecodeError, TypeError):
                metadata = None
        elif isinstance(raw_payload, dict):
            metadata = raw_payload

        # Convert datetime to ISO string (append Z to indicate UTC)
        ts = ev.get("timestamp") or ev.get("processedAt")
        timestamp_str = f"{ts.isoformat()}Z" if ts else ""

        alerts.append(DeviceAlertResponse(
            id=ev["id"],
            device_id=device_id,
            event_type=ev_type,
            priority=ALERT_PRIORITY_MAP.get(ev_type, "medium"),
            timestamp=timestamp_str,
            message=ALERT_MESSAGE_MAP.get(ev_type, f"Alert: {ev_type}"),
            latitude=ev.get("lat"),
            longitude=ev.get("lng"),
            battery=ev.get("batteryLevel"),
            metadata=metadata,
        ))

    return alerts


# --- Contact Numbers ---

@router.get("/api/device/{device_id}/contacts", tags=["device-config"],
         summary="Get authorized contact numbers",
         response_model=ContactNumbersResponse)
async def get_contact_numbers(device_id: str, current_user: Dict = Depends(get_current_user)):
    """Get all authorized contact numbers configured on the device."""
    await _verify_device_access(device_id, current_user["user_id"])

    numbers = get_evmars_client_instance().get_contact_numbers(device_id)
    if numbers is None:
        raise HTTPException(status_code=502, detail="Failed to read contact numbers from device")

    contacts = [
        ContactNumberResponse(
            index=entry.get("index", i),
            number=entry.get("number", ""),
            enabled=bool(entry.get("enable")),
            call=bool(entry.get("call")),
            sms=bool(entry.get("sms")),
            flag=entry.get("flag", 0),
        )
        for i, entry in enumerate(numbers)
    ]

    return ContactNumbersResponse(device_id=device_id, contacts=contacts)


@router.put("/api/device/{device_id}/contacts/{index}", tags=["device-config"],
         summary="Set a contact number")
async def set_contact_number(
    device_id: str,
    index: int,
    body: SetContactNumberRequest,
    current_user: Dict = Depends(get_current_user),
):
    """Set an authorized contact number on a specific slot (0-9)."""
    await _verify_device_access(device_id, current_user["user_id"])

    if index < 0 or index > 9:
        raise HTTPException(status_code=400, detail="Index must be 0-9")

    if index != body.index:
        raise HTTPException(status_code=400, detail="URL index and body index must match")

    result = get_evmars_client_instance().set_contact_number(
        device_id=device_id,
        index=body.index,
        number=body.number,
        enabled=body.enabled,
        call=body.call,
        sms=body.sms,
    )

    if result is None:
        raise HTTPException(status_code=502, detail="Failed to send contact number to device")

    return {
        "detail": f"Contact number set on slot {body.index}",
        "index": body.index,
        "number": body.number,
        "enabled": body.enabled,
    }


@router.delete("/api/device/{device_id}/contacts/{index}", tags=["device-config"],
            summary="Delete a contact number")
async def delete_contact_number(
    device_id: str,
    index: int,
    current_user: Dict = Depends(get_current_user),
):
    """Clear a contact number slot on the device."""
    await _verify_device_access(device_id, current_user["user_id"])

    if index < 0 or index > 9:
        raise HTTPException(status_code=400, detail="Index must be 0-9")

    result = get_evmars_client_instance().delete_contact_number(device_id, index)
    if result is None:
        raise HTTPException(status_code=502, detail="Failed to clear contact number on device")

    return {"detail": f"Contact number slot {index} cleared"}


# --- Device Actions ---

@router.post("/api/device/{device_id}/find", tags=["device-config"],
          summary="Make device beep/vibrate")
async def find_device(device_id: str, current_user: Dict = Depends(get_current_user)):
    """Send a find command to make the device beep/vibrate."""
    await _verify_device_access(device_id, current_user["user_id"])

    result = get_evmars_client_instance().find_device(device_id)
    if result is None:
        raise HTTPException(status_code=502, detail="Failed to send find command to device")

    return {"detail": "Find command sent to device"}


@router.post("/api/device/{device_id}/locate", tags=["device-config"],
          summary="Request immediate location update")
async def locate_device(device_id: str, current_user: Dict = Depends(get_current_user)):
    """Request an immediate location update from the device."""
    await _verify_device_access(device_id, current_user["user_id"])

    result = get_evmars_client_instance().request_location(device_id)
    if result is None:
        raise HTTPException(status_code=502, detail="Failed to send locate command to device")

    return {"detail": "Location request sent to device"}
