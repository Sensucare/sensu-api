import datetime
import logging
from typing import Dict, Optional, Any, List

from fastapi import APIRouter, HTTPException, Query, Depends
from fastapi.responses import PlainTextResponse

from auth.core import get_current_user
from watch.models import (
    WatchAssociation, LinkWatchRequest, UnlinkWatchResponse,
    SendCommandRequest, RawCommandRequest, SchedulerConfigRequest,
    FallDetectionConfig, WorkingModeRequest, CustomModeRequest,
    ReminderItem, RemindersRequest, BPCalibrationRequest,
    FallEvent, FallEventStats, AlarmEvent, AlarmEventStats,
    _device_to_dict, _format_watch_association,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def get_server_instance():
    """Lazy import to avoid circular dependencies."""
    from watch_app import server_instance
    return server_instance


def get_fall_event_manager():
    from watch_app import fall_event_manager
    return fall_event_manager


def get_alarm_event_manager():
    from watch_app import alarm_event_manager
    return alarm_event_manager


def get_user_manager():
    from watch_app import user_manager
    return user_manager


def get_data_logger():
    from watch_app import data_logger
    return data_logger

@router.get(
    "/api/user/watches",
    tags=["watches"],
    summary="List watches linked to the authenticated user",
    response_model=List[WatchAssociation],
)
def list_user_watches(current_user: Dict = Depends(get_current_user)):
    records = get_user_manager().list_user_watches(current_user["user_id"])
    return [_format_watch_association(record) for record in records]


@router.post(
    "/api/user/watches",
    tags=["watches"],
    summary="Link a watch IMEI to the authenticated user",
    response_model=WatchAssociation,
)
def link_user_watch(body: LinkWatchRequest, current_user: Dict = Depends(get_current_user)):
    try:
        record = get_user_manager().link_watch_to_user(
            user_id=current_user["user_id"],
            imei=body.imei,
            label=body.label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        logger.error("Failed to persist watch link for user %s: %s", current_user["user_id"], exc)
        raise HTTPException(status_code=500, detail="Failed to link watch")

    return _format_watch_association(record)


@router.delete(
    "/api/user/watches/{imei}",
    tags=["watches"],
    summary="Remove a watch association from the authenticated user",
    response_model=UnlinkWatchResponse,
)
def unlink_user_watch(imei: str, current_user: Dict = Depends(get_current_user)):
    try:
        removed = get_user_manager().unlink_watch_from_user(
            user_id=current_user["user_id"],
            imei=imei,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not removed:
        raise HTTPException(status_code=404, detail="Watch not linked to this user")

    return UnlinkWatchResponse(detail="Watch unlinked successfully")


@router.get("/api/watches", tags=["watches"], summary="List watch sessions and known devices")
def api_list_watches(current_user: Dict = Depends(get_current_user)):
    sessions = get_server_instance().manager.list_sessions()
    # Normalize datetimes
    for s in sessions.values():
        if isinstance(s.get("last_seen"), datetime.datetime):
            s["last_seen"] = s["last_seen"].isoformat()
    return sessions


@router.get("/api/watches/{imei}", tags=["watches"], summary="Get watch status and last-known data")
def api_get_watch(imei: str, current_user: Dict = Depends(get_current_user)):
    sessions = get_server_instance().manager.list_sessions()
    session = sessions.get(imei)
    device = get_server_instance().handler.get_device_snapshot(imei)
    if not (session or device):
        raise HTTPException(status_code=404, detail="Watch not found")
    if session and isinstance(session.get("last_seen"), datetime.datetime):
        session["last_seen"] = session["last_seen"].isoformat()
    return {
        "session": session or {"imei": imei, "connected": False},
        "device": _device_to_dict(device or {}),
    }


@router.get("/api/watches/{imei}/metrics", tags=["watches"], summary="Get comprehensive health and sensor metrics for a watch")
def api_get_metrics(imei: str, current_user: Dict = Depends(get_current_user)):
    """
    Retrieve the latest health and sensor metrics for a specific GPS watch device.
    
    This endpoint returns all available metrics data including:
    - Heart rate measurements (from AP49 messages)
    - Temperature readings with battery level (from AP50 messages) 
    - Blood pressure data - systolic/diastolic (from APHT messages)
    - Health composite data with timestamps
    
    Each metric includes the raw data received from the device, parsed values where applicable,
    and the timestamp when the data was received by the server.
    
    Args:
        imei: The 15-digit IMEI identifier of the GPS watch device
        
    Returns:
        dict: Dictionary containing all available metrics for the device, with each metric
              including 'value', 'received_at' timestamp, and any parsed components.
              Returns empty dict {} if no metrics are available.
              
    Raises:
        HTTPException 404: If the watch device is not found in the system
    """
    device = get_server_instance().handler.get_device_snapshot(imei)
    if not device:
        raise HTTPException(status_code=404, detail="Watch not found")
    return _device_to_dict(device).get("metrics", {})


@router.get("/api/watches/{imei}/location", tags=["watches"], summary="Get last-known location (parsed)")
def api_get_location(imei: str, current_user: Dict = Depends(get_current_user)):
    device = get_server_instance().handler.get_device_snapshot(imei)
    if not device or not device.get("last_location"):
        raise HTTPException(status_code=404, detail="Location not available")
    loc = _device_to_dict(device).get("last_location")
    parsed = loc.get("parsed") if isinstance(loc, dict) else None
    if parsed and isinstance(parsed, dict):
        # ensure any internal timestamps are ISO
        if parsed.get("timestamp_utc") and isinstance(parsed["timestamp_utc"], datetime.datetime):
            parsed["timestamp_utc"] = parsed["timestamp_utc"].isoformat()
    return loc


@router.post("/api/watches/{imei}/command", tags=["commands"], summary="Send structured command to a watch")
def api_send_command(imei: str, body: SendCommandRequest, current_user: Dict = Depends(get_current_user)):
    message = get_server_instance().handler.send_command(imei, body.command.upper(), body.params or "")
    ok = get_server_instance().send_to_device(imei, message)
    if not ok:
        raise HTTPException(status_code=503, detail="Watch not connected")
    return {"status": "sent", "payload": message}


@router.post("/api/watches/{imei}/raw", tags=["commands"], summary="Send raw payload to a watch")
def api_send_raw(imei: str, body: RawCommandRequest, current_user: Dict = Depends(get_current_user)):
    payload = body.payload
    ok = get_server_instance().send_to_device(imei, payload)
    if not ok:
        raise HTTPException(status_code=503, detail="Watch not connected")
    return {"status": "sent", "payload": payload}


@router.get("/api/logs", tags=["system"], summary="Get all communication logs in plain text")
def api_get_logs(limit: Optional[int] = None, current_user: Dict = Depends(get_current_user)):
    """
    Retrieve all incoming and outgoing communication logs in plain text format.
    
    This endpoint returns a chronological log of all data exchanged between the server
    and GPS watch devices, including:
    - Incoming messages from devices (login, location, heartbeat, alarms, etc.)
    - Outgoing responses and commands sent to devices
    - Timestamps, device addresses, and IMEI identifiers
    - Raw message payloads
    
    Args:
        limit: Optional parameter to limit the number of recent log entries returned.
               If not specified, returns all available logs.
    
    Returns:
        Plain text response containing formatted log entries, one per line.
        Each line includes timestamp, direction (INCOMING/OUTGOING), device info, and message data.
    """
    logs_content = get_data_logger().get_logs(limit)
    return PlainTextResponse(logs_content, media_type="text/plain")


@router.get("/api/scheduler/status", tags=["commands"], summary="Get health test scheduler status and configuration")
def api_get_scheduler_status(current_user: Dict = Depends(get_current_user)):
    """
    Get the current status and configuration of the health test scheduler.
    
    Returns information about:
    - Whether the scheduler is running
    - Current configuration (test intervals, enabled tests, etc.)
    - Number of active devices being tested
    - Last test times for each device
    
    Returns:
        dict: Scheduler status including running state, configuration, and statistics
    """
    if not get_server_instance().scheduler:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    return get_server_instance().scheduler.get_status()


@router.post("/api/scheduler/config", tags=["commands"], summary="Update health test scheduler configuration")
def api_update_scheduler_config(body: SchedulerConfigRequest, current_user: Dict = Depends(get_current_user)):
    """
    Update the health test scheduler configuration.
    
    This endpoint allows you to modify:
    - test_interval_seconds: How often to send test commands (minimum 10 seconds)
    - auto_test_interval_minutes: Device auto-test interval (minimum 1 minute)
    - enabled_tests: List of test types to perform (heart_rate, blood_pressure, temperature, blood_oxygen)
    - auto_configure_on_login: Whether to configure devices automatically on login
    
    Args:
        body: Configuration parameters to update
        
    Returns:
        dict: Updated scheduler configuration
    """
    if not get_server_instance().scheduler:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    
    config = body.dict(exclude_none=True)
    get_server_instance().scheduler.update_config(config)
    return get_server_instance().scheduler.get_config()


@router.post("/api/scheduler/start", tags=["commands"], summary="Start the health test scheduler")
def api_start_scheduler(current_user: Dict = Depends(get_current_user)):
    """
    Start the health test scheduler if it's not already running.
    
    Returns:
        dict: Operation result with success status
    """
    if not get_server_instance().scheduler:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    
    success = get_server_instance().scheduler.start()
    if success:
        return {"status": "started", "message": "Health test scheduler started successfully"}
    else:
        return {"status": "already_running", "message": "Scheduler was already running"}


@router.post("/api/scheduler/stop", tags=["commands"], summary="Stop the health test scheduler")
def api_stop_scheduler(current_user: Dict = Depends(get_current_user)):
    """
    Stop the health test scheduler.
    
    Returns:
        dict: Operation result with success status
    """
    if not get_server_instance().scheduler:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    
    success = get_server_instance().scheduler.stop()
    if success:
        return {"status": "stopped", "message": "Health test scheduler stopped successfully"}
    else:
        return {"status": "already_stopped", "message": "Scheduler was not running"}


@router.post("/api/watches/{imei}/test/{test_type}", tags=["commands"], summary="Trigger specific health test for a watch")
def api_trigger_test(imei: str, test_type: str, current_user: Dict = Depends(get_current_user)):
    """
    Trigger a specific health test for a watch device immediately.
    
    Available test types:
    - heart_rate: Trigger heart rate measurement (BPXL command)
    - blood_pressure: Trigger blood pressure measurement (BPXY command)  
    - temperature: Trigger temperature measurement (BPXT command)
    - blood_oxygen: Trigger blood oxygen measurement (BPXZ command)
    
    Args:
        imei: The IMEI of the target watch device
        test_type: Type of test to trigger
        
    Returns:
        dict: Operation result with success status
    """
    if not get_server_instance().scheduler:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    
    valid_tests = ['heart_rate', 'blood_pressure', 'temperature', 'blood_oxygen']
    if test_type not in valid_tests:
        raise HTTPException(status_code=400, detail=f"Invalid test type. Valid types: {valid_tests}")
    
    # Check if device is connected
    sessions = get_server_instance().manager.list_sessions()
    session = sessions.get(imei)
    if not session or not session.get('connected'):
        raise HTTPException(status_code=404, detail="Watch not connected")
    
    success = get_server_instance().scheduler.send_test_command(imei, test_type)
    if success:
        return {"status": "sent", "message": f"{test_type} test command sent to {imei}"}
    else:
        raise HTTPException(status_code=503, detail=f"Failed to send {test_type} test command")


@router.get("/api/watches/{imei}/fall-events", tags=["watches"], summary="Get fall detection events for a specific watch")
def api_get_fall_events_by_imei(
    imei: str,
    start_date: Optional[str] = Query(None, description="Start date (ISO format: YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (ISO format: YYYY-MM-DD)"),
    limit: int = Query(100, description="Maximum number of events to return"),
    current_user: Dict = Depends(get_current_user)
):
    """
    Retrieve fall detection events for a specific GPS watch device.
    
    This endpoint returns fall detection events (alarm types 05 and 06) with location data,
    timestamps, and device status information.
    
    Args:
        imei: The 15-digit IMEI identifier of the GPS watch device
        start_date: Optional start date filter (YYYY-MM-DD format)
        end_date: Optional end date filter (YYYY-MM-DD format)
        limit: Maximum number of events to return (default: 100)
        
    Returns:
        List[FallEvent]: List of fall detection events
    """
    try:
        start_dt = datetime.datetime.fromisoformat(start_date) if start_date else None
        end_dt = datetime.datetime.fromisoformat(end_date) if end_date else None
        
        events = get_fall_event_manager().get_fall_events_by_imei(
            imei=imei,
            start_date=start_dt,
            end_date=end_dt,
            limit=limit
        )
        
        return events
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")
    except Exception as e:
        logger.error(f"Error retrieving fall events for {imei}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/fall-events", tags=["watches"], summary="Get all fall detection events")
def api_get_all_fall_events(
    start_date: Optional[str] = Query(None, description="Start date (ISO format: YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (ISO format: YYYY-MM-DD)"),
    imei: Optional[str] = Query(None, description="Filter by specific IMEI"),
    alarm_type: Optional[str] = Query(None, description="Filter by alarm type (05 or 06)"),
    limit: int = Query(1000, description="Maximum number of events to return"),
    current_user: Dict = Depends(get_current_user)
):
    """
    Retrieve all fall detection events across all devices with optional filtering.
    
    This endpoint returns fall detection events from all GPS watch devices in the system,
    with support for filtering by date range, device IMEI, and alarm type.
    
    Args:
        start_date: Optional start date filter (YYYY-MM-DD format)
        end_date: Optional end date filter (YYYY-MM-DD format)
        imei: Optional IMEI filter to show events from specific device
        alarm_type: Optional alarm type filter (05 or 06)
        limit: Maximum number of events to return (default: 1000)
        
    Returns:
        List[FallEvent]: List of fall detection events
    """
    try:
        start_dt = datetime.datetime.fromisoformat(start_date) if start_date else None
        end_dt = datetime.datetime.fromisoformat(end_date) if end_date else None
        
        events = get_fall_event_manager().get_all_fall_events(
            start_date=start_dt,
            end_date=end_dt,
            imei=imei,
            alarm_type=alarm_type,
            limit=limit
        )
        
        return events
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")
    except Exception as e:
        logger.error(f"Error retrieving fall events: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/fall-events/stats", tags=["watches"], summary="Get fall detection statistics")
def api_get_fall_event_stats(
    start_date: Optional[str] = Query(None, description="Start date (ISO format: YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (ISO format: YYYY-MM-DD)"),
    current_user: Dict = Depends(get_current_user)
):
    """
    Get statistical data about fall detection events.
    
    This endpoint provides aggregate statistics including:
    - Total number of fall events
    - Events grouped by device (IMEI)
    - Events grouped by alarm type (05 vs 06)
    - Events grouped by day (last 30 days)
    
    Args:
        start_date: Optional start date filter (YYYY-MM-DD format)
        end_date: Optional end date filter (YYYY-MM-DD format)
        
    Returns:
        FallEventStats: Statistical data about fall detection events
    """
    try:
        start_dt = datetime.datetime.fromisoformat(start_date) if start_date else None
        end_dt = datetime.datetime.fromisoformat(end_date) if end_date else None
        
        stats = get_fall_event_manager().get_fall_event_statistics(
            start_date=start_dt,
            end_date=end_dt
        )
        
        return stats
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")
    except Exception as e:
        logger.error(f"Error retrieving fall event statistics: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/watches/{imei}/fall-detection/config", tags=["commands"], summary="Get fall detection configuration for a watch")
def api_get_fall_detection_config(imei: str, current_user: Dict = Depends(get_current_user)):
    """
    Get the current fall detection configuration for a specific GPS watch device.
    
    Returns the enabled status, sensitivity level, and last update timestamp
    for fall detection settings.
    
    Args:
        imei: The 15-digit IMEI identifier of the GPS watch device
        
    Returns:
        dict: Fall detection configuration including enabled status and sensitivity
    """
    try:
        settings = get_fall_event_manager().get_device_settings(imei)
        
        if not settings:
            # Return default settings if none found
            return {
                "imei": imei,
                "enabled": False,
                "sensitivity": 2,
                "updated_at": None,
                "configured": False
            }
        
        return {
            "imei": imei,
            "enabled": bool(settings["fall_detection_enabled"]),
            "sensitivity": settings["fall_sensitivity"],
            "updated_at": settings["updated_at"],
            "configured": True
        }
        
    except Exception as e:
        logger.error(f"Error retrieving fall detection config for {imei}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/watches/{imei}/fall-detection/config", tags=["commands"], summary="Update fall detection configuration for a watch")
def api_update_fall_detection_config(imei: str, config: FallDetectionConfig, current_user: Dict = Depends(get_current_user)):
    """
    Update fall detection configuration for a specific GPS watch device.
    
    This endpoint sends BP76 (enable/disable) and BP77 (sensitivity) commands
    to the device and stores the configuration in the database.
    
    Args:
        imei: The 15-digit IMEI identifier of the GPS watch device
        config: Fall detection configuration (enabled and sensitivity)
        
    Returns:
        dict: Updated configuration and command status
    """
    try:
        # Validate sensitivity level
        if config.sensitivity not in [1, 2, 3]:
            raise HTTPException(status_code=400, detail="Sensitivity must be 1 (low), 2 (medium), or 3 (high)")
        
        # Check if device is connected
        sessions = get_server_instance().manager.list_sessions()
        session = sessions.get(imei)
        if not session or not session.get('connected'):
            raise HTTPException(status_code=404, detail="Watch not connected")
        
        success_count = 0
        commands_sent = []
        
        # Send BP76 command (enable/disable fall detection)
        try:
            enable_cmd = get_server_instance().handler.create_fall_detection_switch_command(imei, config.enabled)
            if get_server_instance().send_to_device(imei, enable_cmd):
                success_count += 1
                commands_sent.append(f"BP76 (enabled={config.enabled})")
                logger.info(f"Sent fall detection enable command to {imei}: {config.enabled}")
            else:
                logger.warning(f"Failed to send fall detection enable command to {imei}")
        except Exception as e:
            logger.error(f"Error sending BP76 command to {imei}: {e}")
        
        # Send BP77 command (sensitivity level)
        try:
            sensitivity_cmd = get_server_instance().handler.create_fall_sensitivity_command(imei, config.sensitivity)
            if get_server_instance().send_to_device(imei, sensitivity_cmd):
                success_count += 1
                commands_sent.append(f"BP77 (sensitivity={config.sensitivity})")
                logger.info(f"Sent fall sensitivity command to {imei}: {config.sensitivity}")
            else:
                logger.warning(f"Failed to send fall sensitivity command to {imei}")
        except Exception as e:
            logger.error(f"Error sending BP77 command to {imei}: {e}")
        
        # Save configuration to database
        try:
            get_fall_event_manager().save_device_settings(
                imei=imei,
                fall_detection_enabled=config.enabled,
                fall_sensitivity=config.sensitivity
            )
            logger.info(f"Saved fall detection configuration for {imei}")
        except Exception as e:
            logger.error(f"Error saving fall detection config for {imei}: {e}")
            raise HTTPException(status_code=500, detail="Failed to save configuration")
        
        if success_count == 0:
            raise HTTPException(status_code=503, detail="Failed to send configuration commands to device")
        
        return {
            "status": "updated",
            "imei": imei,
            "enabled": config.enabled,
            "sensitivity": config.sensitivity,
            "commands_sent": commands_sent,
            "success_count": success_count,
            "total_commands": 2,
            "updated_at": datetime.datetime.now().isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating fall detection config for {imei}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/watches/{imei}/fall-detection/test", tags=["commands"], summary="Trigger a test fall detection alarm")
def api_test_fall_detection(imei: str, current_user: Dict = Depends(get_current_user)):
    """
    Trigger a test fall detection alarm for a specific GPS watch device.
    
    This is useful for testing the fall detection system and verifying that
    events are properly logged and processed by the server.
    
    Note: This creates a simulated fall event in the database for testing purposes.
    
    Args:
        imei: The 15-digit IMEI identifier of the GPS watch device
        
    Returns:
        dict: Test result with created event ID
    """
    try:
        # Check if device exists in sessions
        sessions = get_server_instance().manager.list_sessions()
        if imei not in sessions:
            raise HTTPException(status_code=404, detail="Device not found")
        
        # Create a test fall event
        event_id = get_fall_event_manager().save_fall_event(
            imei=imei,
            timestamp=datetime.datetime.now(),
            alarm_type="05",  # Fall detection test
            latitude=None,  # No location for test
            longitude=None,
            location_raw="TEST_FALL_EVENT",
            device_status="test"
        )
        
        # Log the test event
        logger.info(f"Created test fall detection event {event_id} for {imei}")
        get_data_logger()._write_system_log("INFO", f"TEST_FALL_DETECTION - IMEI: {imei}, Event ID: {event_id}")
        
        return {
            "status": "test_created",
            "imei": imei,
            "event_id": event_id,
            "alarm_type": "05",
            "timestamp": datetime.datetime.now().isoformat(),
            "message": "Test fall detection event created successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating test fall event for {imei}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/watches/{imei}/alarm/latest", tags=["watches"], summary="Get latest alarm status for a specific watch")
def api_get_latest_alarm(imei: str, current_user: Dict = Depends(get_current_user)):
    """
    Get the latest alarm event for a specific GPS watch device.
    
    This endpoint returns the most recent alarm data including:
    - Alarm type and description
    - Location data (latitude, longitude, speed, direction)
    - Device status (battery, signal, etc.)
    - LBS data (MCC, MNC, LAC, CID)
    - WiFi information
    - Timestamps and processing information
    
    Args:
        imei: The 15-digit IMEI identifier of the GPS watch device
        
    Returns:
        AlarmEvent: Latest alarm event data or 404 if no alarms found
    """
    try:
        alarm_event = get_alarm_event_manager().get_latest_alarm_by_imei(imei)
        
        if not alarm_event:
            raise HTTPException(status_code=404, detail="No alarm events found for this device")
        
        return alarm_event
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving latest alarm for {imei}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/watches/{imei}/alarm/events", tags=["watches"], summary="Get alarm event history for a specific watch")
def api_get_alarm_events_by_imei(
    imei: str,
    start_date: Optional[str] = Query(None, description="Start date (ISO format: YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (ISO format: YYYY-MM-DD)"),
    limit: int = Query(100, description="Maximum number of events to return"),
    current_user: Dict = Depends(get_current_user)
):
    """
    Retrieve alarm event history for a specific GPS watch device.
    
    This endpoint returns a list of alarm events with comprehensive data including
    location, device status, and alarm details.
    
    Args:
        imei: The 15-digit IMEI identifier of the GPS watch device
        start_date: Optional start date filter (YYYY-MM-DD format)
        end_date: Optional end date filter (YYYY-MM-DD format)
        limit: Maximum number of events to return (default: 100)
        
    Returns:
        List[AlarmEvent]: List of alarm events
    """
    try:
        start_dt = datetime.datetime.fromisoformat(start_date) if start_date else None
        end_dt = None
        if end_date:
            # Convert end_date to end of day (23:59:59.999999) to include the entire day
            end_dt = datetime.datetime.fromisoformat(end_date).replace(
                hour=23, minute=59, second=59, microsecond=999999
            )

        events = get_alarm_event_manager().get_alarm_events_by_imei(
            imei=imei,
            start_date=start_dt,
            end_date=end_dt,
            limit=limit
        )
        
        return events
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")
    except Exception as e:
        logger.error(f"Error retrieving alarm events for {imei}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/alarm/events", tags=["watches"], summary="Get all alarm events across all devices")
def api_get_all_alarm_events(
    start_date: Optional[str] = Query(None, description="Start date (ISO format: YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (ISO format: YYYY-MM-DD)"),
    imei: Optional[str] = Query(None, description="Filter by specific IMEI"),
    alarm_type: Optional[str] = Query(None, description="Filter by alarm type (00, 01, 03, 05, 06)"),
    limit: int = Query(1000, description="Maximum number of events to return"),
    current_user: Dict = Depends(get_current_user)
):
    """
    Retrieve all alarm events across all devices with optional filtering.
    
    This endpoint returns alarm events from all GPS watch devices in the system,
    with support for filtering by date range, device IMEI, and alarm type.
    
    Args:
        start_date: Optional start date filter (YYYY-MM-DD format)
        end_date: Optional end date filter (YYYY-MM-DD format)
        imei: Optional IMEI filter to show events from specific device
        alarm_type: Optional alarm type filter (00=no alarm, 01=SOS, 03=not wearing, 05/06=fall)
        limit: Maximum number of events to return (default: 1000)
        
    Returns:
        List[AlarmEvent]: List of alarm events
    """
    try:
        start_dt = datetime.datetime.fromisoformat(start_date) if start_date else None
        end_dt = None
        if end_date:
            # Convert end_date to end of day (23:59:59.999999) to include the entire day
            end_dt = datetime.datetime.fromisoformat(end_date).replace(
                hour=23, minute=59, second=59, microsecond=999999
            )

        events = get_alarm_event_manager().get_all_alarm_events(
            start_date=start_dt,
            end_date=end_dt,
            imei=imei,
            alarm_type=alarm_type,
            limit=limit
        )
        
        return events
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")
    except Exception as e:
        logger.error(f"Error retrieving alarm events: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/alarm/events/stats", tags=["watches"], summary="Get alarm event statistics")
def api_get_alarm_event_stats(
    start_date: Optional[str] = Query(None, description="Start date (ISO format: YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (ISO format: YYYY-MM-DD)"),
    current_user: Dict = Depends(get_current_user)
):
    """
    Get statistical data about alarm events.
    
    This endpoint provides aggregate statistics including:
    - Total number of alarm events
    - Events grouped by device (IMEI)
    - Events grouped by alarm type and description
    - Events grouped by day (last 30 days)
    
    Args:
        start_date: Optional start date filter (YYYY-MM-DD format)
        end_date: Optional end date filter (YYYY-MM-DD format)
        
    Returns:
        AlarmEventStats: Statistical data about alarm events
    """
    try:
        start_dt = datetime.datetime.fromisoformat(start_date) if start_date else None
        end_dt = datetime.datetime.fromisoformat(end_date) if end_date else None
        
        stats = get_alarm_event_manager().get_alarm_statistics(
            start_date=start_dt,
            end_date=end_dt
        )
        
        return stats
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")
    except Exception as e:
        logger.error(f"Error retrieving alarm event statistics: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/watches/{imei}/working-mode", tags=["commands"], summary="Set device working mode")
def api_set_working_mode(imei: str, body: WorkingModeRequest, current_user: Dict = Depends(get_current_user)):
    """
    Set the working mode for a GPS watch device.
    
    This endpoint sends a BP33 command to configure the device's reporting frequency:
    - Mode 1: Normal mode (location every 15 minutes with WiFi and LBS)
    - Mode 2: Power-saving mode (location every 60 minutes with WiFi and LBS)  
    - Mode 3: Emergency mode (location every 1 minute with GPS, WiFi and LBS)
    
    Args:
        imei: The 15-digit IMEI identifier of the GPS watch device
        body: Working mode configuration
        
    Returns:
        dict: Command status and generated command payload
    """
    try:
        # Check if device is connected
        sessions = get_server_instance().manager.list_sessions()
        session = sessions.get(imei)
        if not session or not session.get('connected'):
            raise HTTPException(status_code=404, detail="Watch not connected")
        
        # Generate and send command
        command = get_server_instance().handler.create_working_mode_command(imei, body.mode)
        ok = get_server_instance().send_to_device(imei, command)
        
        if not ok:
            raise HTTPException(status_code=503, detail="Failed to send command to device")
        
        mode_descriptions = {
            1: "Normal mode (15min intervals)",
            2: "Power-saving mode (60min intervals)", 
            3: "Emergency mode (1min intervals)"
        }
        
        return {
            "status": "sent",
            "imei": imei,
            "mode": body.mode,
            "description": mode_descriptions[body.mode],
            "command": command
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting working mode for {imei}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/watches/{imei}/custom-mode", tags=["commands"], summary="Set custom working mode")
def api_set_custom_mode(imei: str, body: CustomModeRequest, current_user: Dict = Depends(get_current_user)):
    """
    Set a custom working mode for a GPS watch device.
    
    This endpoint sends a BP34 command to configure custom reporting intervals
    and GPS settings. Provides more flexibility than preset working modes.
    
    Args:
        imei: The 15-digit IMEI identifier of the GPS watch device
        body: Custom mode configuration with interval and GPS settings
        
    Returns:
        dict: Command status and configuration details
    """
    try:
        # Check if device is connected
        sessions = get_server_instance().manager.list_sessions()
        session = sessions.get(imei)
        if not session or not session.get('connected'):
            raise HTTPException(status_code=404, detail="Watch not connected")
        
        # Generate and send command
        command = get_server_instance().handler.create_custom_working_mode_command(
            imei, body.interval_seconds, body.gps_enabled
        )
        ok = get_server_instance().send_to_device(imei, command)
        
        if not ok:
            raise HTTPException(status_code=503, detail="Failed to send command to device")
        
        return {
            "status": "sent",
            "imei": imei,
            "interval_seconds": body.interval_seconds,
            "gps_enabled": body.gps_enabled,
            "description": f"Custom mode: {body.interval_seconds}s intervals, GPS {'enabled' if body.gps_enabled else 'disabled'}",
            "command": command
        }
        
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error setting custom mode for {imei}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/watches/{imei}/locate-now", tags=["commands"], summary="Trigger immediate location report")
def api_locate_now(imei: str, current_user: Dict = Depends(get_current_user)):
    """
    Trigger an immediate location report from a GPS watch device.
    
    This endpoint sends a BP16 command to force immediate location reporting.
    The device responds with AP16 acknowledgment first, then asynchronously
    sends AP01 location data with current position and status.
    
    Args:
        imei: The 15-digit IMEI identifier of the GPS watch device
        
    Returns:
        dict: Command status and information
    """
    try:
        # Check if device is connected
        sessions = get_server_instance().manager.list_sessions()
        session = sessions.get(imei)
        if not session or not session.get('connected'):
            raise HTTPException(status_code=404, detail="Watch not connected")
        
        # Generate and send command  
        command = get_server_instance().handler.create_realtime_location_command(imei)
        ok = get_server_instance().send_to_device(imei, command)
        
        if not ok:
            raise HTTPException(status_code=503, detail="Failed to send command to device")
        
        return {
            "status": "sent",
            "imei": imei,
            "message": "Real-time location request sent. Device will respond with current location data.",
            "command": command
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error triggering location for {imei}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/watches/{imei}/reminders", tags=["commands"], summary="Configure device reminders")
def api_set_reminders(imei: str, body: RemindersRequest, current_user: Dict = Depends(get_current_user)):
    """
    Configure scheduled reminders for a GPS watch device.
    
    This endpoint sends a BP85 command to set up medication, water, or
    sedentary reminders. Can configure multiple reminders with different
    schedules and types.
    
    Args:
        imei: The 15-digit IMEI identifier of the GPS watch device
        body: Reminder configuration with list of reminders
        
    Returns:
        dict: Command status and configured reminders
    """
    try:
        # Check if device is connected
        sessions = get_server_instance().manager.list_sessions()
        session = sessions.get(imei)
        if not session or not session.get('connected'):
            raise HTTPException(status_code=404, detail="Watch not connected")
        
        # Convert Pydantic models to dicts for the command method
        reminders_list = []
        for reminder in body.reminders:
            reminders_list.append({
                "time": reminder.time,
                "days": reminder.days,
                "enabled": reminder.enabled,
                "type": reminder.type
            })
        
        # Generate and send command
        command = get_server_instance().handler.create_reminder_command(imei, reminders_list)
        ok = get_server_instance().send_to_device(imei, command)
        
        if not ok:
            raise HTTPException(status_code=503, detail="Failed to send command to device")
        
        reminder_types = {1: "medicine", 2: "water", 3: "sedentary"}
        
        return {
            "status": "sent",
            "imei": imei,
            "reminders_count": len(body.reminders),
            "reminders": [
                {
                    "time": r.time,
                    "days": r.days,
                    "enabled": r.enabled,
                    "type_name": reminder_types[r.type]
                }
                for r in body.reminders
            ],
            "command": command
        }
        
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error setting reminders for {imei}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/watches/{imei}/calibrate-bp", tags=["commands"], summary="Calibrate blood pressure readings")
def api_calibrate_bp(imei: str, body: BPCalibrationRequest, current_user: Dict = Depends(get_current_user)):
    """
    Calibrate blood pressure readings for a GPS watch device.
    
    This endpoint sends a BPJZ command to calibrate the device's blood pressure
    sensor based on user profile (age, gender) and known BP readings. May trigger
    automatic blood pressure monitoring after calibration.
    
    Args:
        imei: The 15-digit IMEI identifier of the GPS watch device
        body: Calibration data including BP values, age, and gender
        
    Returns:
        dict: Command status and calibration details
    """
    try:
        # Check if device is connected
        sessions = get_server_instance().manager.list_sessions()
        session = sessions.get(imei)
        if not session or not session.get('connected'):
            raise HTTPException(status_code=404, detail="Watch not connected")
        
        # Generate and send command
        command = get_server_instance().handler.create_bp_calibration_command(
            imei, body.systolic, body.diastolic, body.age, body.is_male
        )
        ok = get_server_instance().send_to_device(imei, command)
        
        if not ok:
            raise HTTPException(status_code=503, detail="Failed to send command to device")
        
        return {
            "status": "sent",
            "imei": imei,
            "calibration": {
                "systolic": body.systolic,
                "diastolic": body.diastolic,
                "age": body.age,
                "gender": "male" if body.is_male else "female"
            },
            "message": "Blood pressure calibration sent. This may trigger automatic BP monitoring.",
            "command": command
        }
        
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error calibrating BP for {imei}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
