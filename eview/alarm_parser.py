"""
Alarm Code Parser for EV04/Eview Devices

Centralized, reusable module for decoding alarm code bitmasks from MQTT trackerAlarm events.
The alarm code is a 32-bit integer where each bit represents a specific alarm type.
"""

from typing import Dict, List, Optional, Tuple


# Alarm type definitions mapped by bit position in the 32-bit alarm code
ALARM_TYPES: Dict[int, Dict[str, str]] = {
    0:  {"type": "battery_low", "priority": "high", "label": "Battery Low"},
    1:  {"type": "over_speed", "priority": "medium", "label": "Over Speed"},
    2:  {"type": "fall_detection", "priority": "critical", "label": "Fall Detection"},
    3:  {"type": "tilt", "priority": "medium", "label": "Tilt Alert"},
    4:  {"type": "geofence_1", "priority": "high", "label": "Geofence Zone 1"},
    5:  {"type": "geofence_2", "priority": "high", "label": "Geofence Zone 2"},
    6:  {"type": "geofence_3", "priority": "high", "label": "Geofence Zone 3"},
    7:  {"type": "geofence_4", "priority": "high", "label": "Geofence Zone 4"},
    8:  {"type": "power_off", "priority": "medium", "label": "Power Off"},
    9:  {"type": "power_on", "priority": "low", "label": "Power On"},
    10: {"type": "motion", "priority": "low", "label": "Motion Alert"},
    11: {"type": "no_motion", "priority": "medium", "label": "No Motion"},
    12: {"type": "sos", "priority": "critical", "label": "SOS"},
    13: {"type": "side_button_1", "priority": "critical", "label": "Side Button 1"},
    14: {"type": "side_button_2", "priority": "high", "label": "Side Button 2"},
    15: {"type": "charging_start", "priority": "low", "label": "Charging Started"},
    16: {"type": "charging_stop", "priority": "low", "label": "Charging Stopped"},
    17: {"type": "sos_ending", "priority": "medium", "label": "SOS Ending"},
    19: {"type": "welfare_check", "priority": "medium", "label": "Welfare Check"},
    21: {"type": "fall_ending", "priority": "low", "label": "Fall Alert Ending"},
    24: {"type": "leave_home", "priority": "high", "label": "Left Home"},
    25: {"type": "at_home", "priority": "low", "label": "At Home"},
}

# Bits 26-29 indicate IN/OUT direction for geofences 1-4 respectively
# 0 = exited the zone, 1 = entered the zone
GEO_DIRECTION_BITS: Dict[int, int] = {
    4: 26,  # Geofence 1 direction at bit 26
    5: 27,  # Geofence 2 direction at bit 27
    6: 28,  # Geofence 3 direction at bit 28
    7: 29,  # Geofence 4 direction at bit 29
}


def parse_alarm_code(alarm_code: int, alarm_code_extend: int = 0) -> List[Dict]:
    """
    Parse a 32-bit alarm code into a list of active alarm events.

    Args:
        alarm_code: 32-bit integer alarm code from MQTT trackerAlarm
        alarm_code_extend: Extended alarm code (reserved for future use)

    Returns:
        List of alarm dicts with keys: type, priority, label, bit, direction (for geofences)
    """
    if alarm_code is None or alarm_code == 0:
        return []

    active_alarms = []

    for bit, alarm_info in ALARM_TYPES.items():
        if alarm_code & (1 << bit):
            alarm = {
                "type": alarm_info["type"],
                "priority": alarm_info["priority"],
                "label": alarm_info["label"],
                "bit": bit,
            }

            # For geofence alarms, determine direction (in/out)
            if bit in GEO_DIRECTION_BITS:
                direction_bit = GEO_DIRECTION_BITS[bit]
                is_entering = bool(alarm_code & (1 << direction_bit))
                alarm["direction"] = "enter" if is_entering else "exit"
                alarm["zone_number"] = bit - 3  # bits 4-7 map to zones 1-4

            active_alarms.append(alarm)

    return active_alarms


def is_fall_detection(alarm_code: int) -> bool:
    """Check if alarm code contains a fall detection event (bit 2)."""
    if alarm_code is None:
        return False
    return bool(alarm_code & (1 << 2))


def is_battery_low(alarm_code: int) -> bool:
    """Check if alarm code contains a battery low event (bit 0)."""
    if alarm_code is None:
        return False
    return bool(alarm_code & (1 << 0))


def is_geofence_alert(alarm_code: int) -> Tuple[bool, Optional[int], Optional[str]]:
    """
    Check if alarm code contains a geofence event (bits 4-7).

    Returns:
        Tuple of (is_active, zone_number, direction)
        - is_active: True if any geofence alarm is set
        - zone_number: 1-4 indicating which zone triggered (first found)
        - direction: 'enter' or 'exit'
    """
    if alarm_code is None:
        return (False, None, None)

    for bit in range(4, 8):
        if alarm_code & (1 << bit):
            direction_bit = GEO_DIRECTION_BITS[bit]
            is_entering = bool(alarm_code & (1 << direction_bit))
            direction = "enter" if is_entering else "exit"
            zone_number = bit - 3
            return (True, zone_number, direction)

    return (False, None, None)


def is_sos(alarm_code: int) -> bool:
    """Check if alarm code contains an SOS event (bit 12)."""
    if alarm_code is None:
        return False
    return bool(alarm_code & (1 << 12))


def is_button_press(alarm_code: int) -> Tuple[bool, Optional[str]]:
    """
    Check if alarm code contains a button press event (bits 12-14).

    Returns:
        Tuple of (is_pressed, button_type)
    """
    if alarm_code is None:
        return (False, None)

    button_bits = {
        12: "SOS Button",
        13: "Side Call Button 1",
        14: "Side Call Button 2",
    }

    for bit, name in button_bits.items():
        if alarm_code & (1 << bit):
            return (True, name)

    return (False, None)


def get_alarm_priority(alarm_code: int) -> str:
    """
    Get the highest priority level from all active alarms.

    Returns: 'critical', 'high', 'medium', or 'low'
    """
    priority_order = ["critical", "high", "medium", "low"]
    alarms = parse_alarm_code(alarm_code)

    if not alarms:
        return "low"

    for priority in priority_order:
        if any(a["priority"] == priority for a in alarms):
            return priority

    return "low"


def alarm_code_to_event_type(alarm_code: int) -> str:
    """
    Convert alarm code to a primary event type string for storage.
    Returns the highest-priority alarm type as the event_type.
    """
    if alarm_code is None or alarm_code == 0:
        return "unknown"

    # Check in priority order
    if is_fall_detection(alarm_code):
        return "fall_detection"
    if is_sos(alarm_code):
        return "sos"

    is_geo, _zone, direction = is_geofence_alert(alarm_code)
    if is_geo:
        return f"geofence_{direction}"

    if is_battery_low(alarm_code):
        return "battery_low"

    # Fallback: return the first active alarm type
    alarms = parse_alarm_code(alarm_code)
    if alarms:
        return alarms[0]["type"]

    return "unknown"
