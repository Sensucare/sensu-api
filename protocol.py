import socket
import threading
import datetime
import logging
import re
import json
from typing import Dict, Optional, Tuple, Any, List
from contextlib import suppress

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Thread-safe manager to track IMEI -> socket/session and socket -> IMEI."""
    def __init__(self, handler: 'GPSWatchProtocolHandler'):
        self._lock = threading.RLock()
        self._imei_to_sock: Dict[str, socket.socket] = {}
        self._sock_to_imei: Dict[socket.socket, str] = {}
        self._addr_by_imei: Dict[str, Tuple[str, int]] = {}
        self._last_seen_by_imei: Dict[str, datetime.datetime] = {}
        self._handler = handler

    def _set_tcp_keepalive(self, sock: socket.socket) -> None:
        with suppress(Exception):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        # Platform-specific tuning (best-effort)
        with suppress(Exception):
            # macOS uses TCP_KEEPALIVE (seconds) for idle time
            if hasattr(socket, 'TCP_KEEPALIVE'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 60)
        with suppress(Exception):
            # Linux
            if hasattr(socket, 'TCP_KEEPIDLE'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            if hasattr(socket, 'TCP_KEEPINTVL'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            if hasattr(socket, 'TCP_KEEPCNT'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)

    def register(self, imei: str, sock: socket.socket, address: Tuple[str, int]) -> None:
        with self._lock:
            old_sock = self._imei_to_sock.get(imei)
            if old_sock and old_sock is not sock:
                with suppress(Exception):
                    old_sock.close()
                self._sock_to_imei.pop(old_sock, None)
            self._imei_to_sock[imei] = sock
            self._sock_to_imei[sock] = imei
            self._addr_by_imei[imei] = address
            self._last_seen_by_imei[imei] = datetime.datetime.now()
            self._set_tcp_keepalive(sock)

    def touch(self, imei: str) -> None:
        with self._lock:
            self._last_seen_by_imei[imei] = datetime.datetime.now()

    def unregister_socket(self, sock: socket.socket) -> None:
        with self._lock:
            imei = self._sock_to_imei.pop(sock, None)
            if imei and self._imei_to_sock.get(imei) is sock:
                self._imei_to_sock.pop(imei, None)
                self._addr_by_imei.pop(imei, None)
                # Keep handler device data; only connection mapping removed

    def get_socket(self, imei: str) -> Optional[socket.socket]:
        with self._lock:
            return self._imei_to_sock.get(imei)

    def list_sessions(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            out: Dict[str, Dict[str, Any]] = {}
            for imei, sock in self._imei_to_sock.items():
                out[imei] = {
                    "imei": imei,
                    "address": self._addr_by_imei.get(imei),
                    "last_seen": self._last_seen_by_imei.get(imei),
                    "connected": True,
                }
            # Include devices known to handler that may be offline
            for imei in self._handler.devices.keys():
                if imei not in out:
                    out[imei] = {
                        "imei": imei,
                        "address": None,
                        "last_seen": self._handler.devices.get(imei, {}).get("last_seen"),
                        "connected": False,
                    }
            return out

    def send(self, imei: str, message: str) -> bool:
        sock = self.get_socket(imei)
        if not sock:
            return False
        try:
            sock.send(message.encode('utf-8'))
            return True
        except Exception as e:
            logger.error(f"Error sending to IMEI {imei}: {e}")
            with suppress(Exception):
                sock.close()
            self.unregister_socket(sock)
            return False


class StaleSessionReaper(threading.Thread):
    """Background thread that closes sockets that have not been seen recently."""
    def __init__(self, manager: ConnectionManager, handler: 'GPSWatchProtocolHandler', stale_seconds: int = 600, interval_seconds: int = 60):
        super().__init__(daemon=True)
        self.manager = manager
        self.handler = handler
        self.stale_seconds = stale_seconds
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                now = datetime.datetime.now()
                sessions = self.manager.list_sessions()
                for imei, s in sessions.items():
                    last_seen = s.get("last_seen")
                    if s.get("connected") and isinstance(last_seen, datetime.datetime):
                        delta = (now - last_seen).total_seconds()
                        if delta > self.stale_seconds:
                            # Close and unregister stale connection
                            sock = self.manager.get_socket(imei)
                            if sock:
                                logger.warning(f"Closing stale connection for IMEI {imei} (idle {int(delta)}s)")
                                with suppress(Exception):
                                    sock.close()
                                self.manager.unregister_socket(sock)
                            # Update device status
                            with self.handler._lock:
                                d = self.handler.devices.get(imei)
                                if d:
                                    d["status"] = "offline"
                                    self.handler.devices[imei] = d
            except Exception as e:
                logger.error(f"StaleSessionReaper error: {e}")
            self._stop.wait(self.interval_seconds)

    def stop(self):
        self._stop.set()


class GPSWatchProtocolHandler:
    """Handler for GPS watch protocol messages"""

    def __init__(self, alarm_event_manager=None, fall_event_manager=None, data_logger=None):
        self.devices: Dict[str, Dict[str, Any]] = {}  # Store device info by IMEI
        self._lock = threading.RLock()
        self.alarm_event_manager = alarm_event_manager
        self.fall_event_manager = fall_event_manager
        self.data_logger = data_logger
    
    def parse_message(self, data: str) -> Dict:
        """Parse incoming message from GPS watch"""
        if not data.startswith("IW") or not data.endswith("#"):
            return {"error": "Invalid message format"}
        
        # Remove IW prefix and # suffix
        content = data[2:-1]
        
        # Normalize leading asterisk if present: IW*AP00*... -> AP00*...
        if content.startswith("*"):
            content = content[1:]
        
        # Handle both formats: with asterisks (IW*AP00*...) and without (IWAP00...)
        if '*' in content:
            # Format with asterisks
            parts = content.split("*")
            if len(parts) < 2:
                return {"error": "Invalid message structure"}
            command = parts[0]
            params = "*".join(parts[1:]) if len(parts) > 1 else ""
        else:
            # Format without asterisks - extract command from beginning
            # Commands are typically 4 characters (AP00, AP01, etc.)
            if len(content) < 4:
                return {"error": "Message too short"}
            
            command = content[:4]
            params = content[4:] if len(content) > 4 else ""
        
        return {
            "command": command,
            "params": params,
            "raw": data
        }
    
    def _format_ap_ack(self, ap_cmd: str, params: str) -> str:
        """Format acknowledgment for APxx responses.
        Ensures we reply with IWAPxx and avoid duplicate commas when params are empty.
        """
        cleaned = (params or "").strip()
        cleaned = cleaned.strip(",")
        if cleaned:
            return f"IW{ap_cmd},{cleaned}#"
        # Keep a single comma before # to mirror device format like 'IWAPXT,#'
        return f"IW{ap_cmd},#"
    
    def handle_ap00_login(self, params: str) -> str:
        """Handle AP00 login package"""
        # Extract IMEI (should be 15 digits)
        imei = params.strip()
        
        # Validate IMEI length
        if not imei or not imei.isdigit():
            logger.warning(f"Invalid IMEI received: {imei}")
        
        with self._lock:
            device = self.devices.get(imei, {})
            device.update({
                "last_seen": datetime.datetime.now(),
                "status": "online",
            })
            # initialize metrics containers if not present
            device.setdefault("metrics", {})
            device.setdefault("last_location", None)
            device.setdefault("last_alarm", None)
            self.devices[imei] = device
        
        # Generate response: BP00 with server time and timezone (asterisk format)
        current_time = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
        timezone = "-6"
        response = f"IWBP00,{current_time},{timezone}#"
        
        logger.info(f"Device login - IMEI: {imei}")
        return response
    
    def handle_ap01_location_for_imei(self, imei: str, params: str) -> str:
        """Handle AP01 GPS/LBS/Status location package"""
        # Store raw location payload and parsed fields
        parsed = None
        with suppress(Exception):
            parsed = self.parse_ap01_location(params)
        with self._lock:
            device = self.devices.setdefault(imei, {"metrics": {}})
            device["last_location"] = {
                "raw": params,
                "received_at": datetime.datetime.now(),
                "parsed": parsed,
            }
            device["last_seen"] = datetime.datetime.now()
            device["status"] = "online"
            self.devices[imei] = device
        logger.info(f"Location data received for {imei}: {params[:80]}...")
        return "IWBP01#"
    
    def handle_ap03_heartbeat_for_imei(self, imei: str, params: str) -> str:
        """Handle AP03 heartbeat package"""
        with self._lock:
            device = self.devices.setdefault(imei, {"metrics": {}})
            device["last_seen"] = datetime.datetime.now()
            device["status"] = "online"
            self.devices[imei] = device
        logger.info(f"Heartbeat received from {imei}: {params}")
        return "IWBP03#"
    
    def handle_ap10_alarm_for_imei(self, imei: str, params: str) -> str:
        """Handle AP10 alarm package including fall detection"""
        parsed_alarm = None
        is_fall_detection = False
        
        try:
            # Parse the AP10 alarm data with enhanced parser
            parsed_alarm = self.parse_ap10_alarm(params)
            alarm_type = parsed_alarm.get("alarm_type", "00")
            is_fall_detection = parsed_alarm.get("is_fall_detection", False)
            
            # Save comprehensive alarm event to database
            try:
                # Save the alarm event with all parsed data
                event_id = self.alarm_event_manager.save_alarm_event(
                    imei=imei,
                    timestamp=datetime.datetime.now(),
                    alarm_data=parsed_alarm
                )
                
                logger.info(f"Saved alarm event {event_id} for IMEI {imei}: type={alarm_type}")
                
                # If it's a fall detection alarm, also save to fall events table
                if is_fall_detection:
                    self.fall_event_manager.save_fall_event(
                        imei=imei,
                        timestamp=datetime.datetime.now(),
                        alarm_type=alarm_type,
                        latitude=parsed_alarm.get("latitude"),
                        longitude=parsed_alarm.get("longitude"),
                        location_raw=params,
                        device_status=json.dumps(parsed_alarm.get("device_status", {}))
                    )
                    
                    # Log fall detection with special severity
                    logger.critical(f"FALL DETECTION ALARM from {imei}: type={alarm_type}, location=({parsed_alarm.get('latitude')}, {parsed_alarm.get('longitude')})")
                    
                    # Log to data logger with special direction
                    self.data_logger._write_system_log("CRITICAL", f"FALL_DETECTION - IMEI: {imei}, Type: {alarm_type}, Location: ({parsed_alarm.get('latitude')}, {parsed_alarm.get('longitude')})")
                else:
                    # Log regular alarm
                    logger.warning(f"ALARM from {imei}: type={alarm_type}, description={parsed_alarm.get('alarm_description', 'Unknown')}")
                    
                    # Log to data logger
                    self.data_logger._write_system_log("WARNING", f"ALARM - IMEI: {imei}, Type: {alarm_type}, Description: {parsed_alarm.get('alarm_description', 'Unknown')}")
                
            except Exception as e:
                logger.error(f"Failed to save alarm event for {imei}: {e}")
            
        except Exception as e:
            logger.error(f"Error parsing AP10 alarm from {imei}: {e}")
        
        with self._lock:
            device = self.devices.setdefault(imei, {"metrics": {}})
            device["last_alarm"] = {
                "raw": params,
                "received_at": datetime.datetime.now(),
                "parsed": parsed_alarm,
                "is_fall_detection": is_fall_detection,
            }
            
            # Update alarm counts in metrics
            metrics = device.setdefault("metrics", {})
            metrics["alarm_events"] = metrics.get("alarm_events", {})
            metrics["alarm_events"]["total_count"] = metrics["alarm_events"].get("total_count", 0) + 1
            metrics["alarm_events"]["last_event"] = datetime.datetime.now()
            metrics["alarm_events"]["last_type"] = parsed_alarm.get("alarm_type") if parsed_alarm else "unknown"
            
            # Update fall detection counts if applicable
            if is_fall_detection:
                metrics["fall_events"] = metrics.get("fall_events", {})
                metrics["fall_events"]["total_count"] = metrics["fall_events"].get("total_count", 0) + 1
                metrics["fall_events"]["last_event"] = datetime.datetime.now()
                metrics["fall_events"]["last_type"] = parsed_alarm.get("alarm_type") if parsed_alarm else "unknown"
            
            device["last_seen"] = datetime.datetime.now()
            device["status"] = "online"
            self.devices[imei] = device
        
        return "IWBP10#"
    
    def handle_apht_health(self, params: str) -> str:
        """Handle APHT health data (heart rate, blood pressure)"""
        parts = params.split(",")
        if len(parts) >= 3:
            heart_rate = parts[0]
            systolic = parts[1]
            diastolic = parts[2]
            logger.info(f"Health data - HR: {heart_rate}, BP: {systolic}/{diastolic}")
        return "IWBPHT#"

    def handle_apht_health_for_imei(self, imei: str, params: str) -> str:
        parts = params.split(",")
        with self._lock:
            device = self.devices.setdefault(imei, {"metrics": {}})
            metrics = device.setdefault("metrics", {})
            metrics["health"] = {
                "raw": params,
                "received_at": datetime.datetime.now(),
            }
            if len(parts) >= 3:
                metrics["health"].update({
                    "heart_rate": parts[0],
                    "systolic": parts[1],
                    "diastolic": parts[2],
                })
                # Update specific buckets for HR and BP for easier consumption
                metrics["heart_rate"] = {
                    "value": parts[0],
                    "received_at": datetime.datetime.now(),
                }
                metrics["blood_pressure"] = {
                    "systolic": parts[1],
                    "diastolic": parts[2],
                    "received_at": datetime.datetime.now(),
                }
            device["last_seen"] = datetime.datetime.now()
            device["status"] = "online"
            self.devices[imei] = device
        logger.info(f"Health data for {imei}: {params}")
        return "IWBPHT#"
    
    def handle_ap49_heart_rate_for_imei(self, imei: str, params: str) -> str:
        """Handle AP49 heart rate data"""
        with self._lock:
            device = self.devices.setdefault(imei, {"metrics": {}})
            metrics = device.setdefault("metrics", {})
            metrics["heart_rate"] = {
                "value": params,
                "received_at": datetime.datetime.now(),
            }
            device["last_seen"] = datetime.datetime.now()
            device["status"] = "online"
            self.devices[imei] = device
        logger.info(f"Heart rate for {imei}: {params}")
        return "IWBP49#"
    
    def handle_ap50_temperature_for_imei(self, imei: str, params: str) -> str:
        """Handle AP50 temperature data"""
        parts = params.split(",")
        if len(parts) >= 2:
            temperature = parts[0]
            battery = parts[1]
            with self._lock:
                device = self.devices.setdefault(imei, {"metrics": {}})
                metrics = device.setdefault("metrics", {})
                metrics["temperature"] = {
                    "celsius": temperature,
                    "battery": battery,
                    "received_at": datetime.datetime.now(),
                }
                device["last_seen"] = datetime.datetime.now()
                device["status"] = "online"
                self.devices[imei] = device
            logger.info(f"Temperature for {imei}: {temperature}°C, Battery: {battery}%")
        else:
            logger.warning(f"Invalid AP50 temperature data format for {imei}: {params}")
        return "IWBP50#"
    
    def handle_aphp_health_params_for_imei(self, imei: str, params: str) -> str:
        """Handle APHP composite health data: HR, BP, SPO2, blood sugar, temperature"""
        parts = [p.strip() for p in params.split(",")]
        heart_rate = parts[0] if len(parts) > 0 and parts[0] != "" else None
        systolic = parts[1] if len(parts) > 1 and parts[1] != "" else None
        diastolic = parts[2] if len(parts) > 2 and parts[2] != "" else None
        spo2 = parts[3] if len(parts) > 3 and parts[3] != "" else None
        blood_sugar = parts[4] if len(parts) > 4 and parts[4] != "" else None
        temperature = parts[5] if len(parts) > 5 and parts[5] != "" else None

        now = datetime.datetime.now()
        with self._lock:
            device = self.devices.setdefault(imei, {"metrics": {}})
            metrics = device.setdefault("metrics", {})

            health_record = {
                "raw": params,
                "received_at": now,
            }
            if heart_rate is not None:
                health_record["heart_rate"] = heart_rate
            if systolic is not None and diastolic is not None:
                health_record["systolic"] = systolic
                health_record["diastolic"] = diastolic
            if spo2 is not None:
                health_record["spo2"] = spo2
            if blood_sugar is not None:
                health_record["blood_sugar"] = blood_sugar
            if temperature is not None:
                health_record["temperature"] = temperature
            metrics["health"] = health_record

            if heart_rate is not None:
                metrics["heart_rate"] = {"value": heart_rate, "received_at": now}
            if systolic is not None and diastolic is not None:
                metrics["blood_pressure"] = {
                    "systolic": systolic,
                    "diastolic": diastolic,
                    "received_at": now,
                }
            if spo2 is not None:
                metrics["blood_oxygen"] = {"spo2": spo2, "received_at": now}
            if blood_sugar is not None:
                metrics["blood_sugar"] = {"mg_dL": blood_sugar, "received_at": now}
            if temperature is not None:
                metrics["temperature"] = {
                    "celsius": temperature,
                    "battery": metrics.get("temperature", {}).get("battery"),
                    "received_at": now,
                }

            device["last_seen"] = now
            device["status"] = "online"
            self.devices[imei] = device

        logger.info(f"Composite health (APHP) for {imei}: {params}")
        return "IWBPHP#"
    
    def process_message(self, data: str) -> Optional[str]:
        """Process incoming message and return response"""
        parsed = self.parse_message(data)
        
        if "error" in parsed:
            logger.error(f"Parse error: {parsed['error']} - Message: {data}")
            return None
        
        command = parsed["command"].upper()  # Ensure uppercase
        params = parsed["params"]
        
        logger.debug(f"Parsed - Command: {command}, Params: {params}")
        
        # Route to appropriate handler based on command
        handlers = {
            "AP00": self.handle_ap00_login,
            # The following handlers without IMEI context are kept for backward compatibility,
            # but server should call the *_for_imei variants to update device state.
            "AP01": lambda p: "IWBP01#",
            "AP02": lambda p: "IWBP02#",  # Multiple bases location
            "AP03": lambda p: "IWBP03#",
            "AP07": lambda p: f"IWBP07{p}#",  # Audio message
            "AP10": lambda p: "IWBP10#",
            "AP49": lambda p: "IWBP49#",
            "APHT": self.handle_apht_health,
            "APHP": lambda p: "IWBPHP#",  # Multiple health params
            "AP50": lambda p: "IWBP50#",
            "AP97": lambda p: "IWBP97#",  # Sleep data
            "APWT": lambda p: "IWBPWT#",  # Weather sync
            "APHD": lambda p: "IWBPHD#",  # ECG data
            # Acknowledge device responses to test commands (no-op commands)
            "APXL": lambda p: self._format_ap_ack("APXL", p),
            "APXY": lambda p: self._format_ap_ack("APXY", p),
            "APXT": lambda p: self._format_ap_ack("APXT", p),
            "APXZ": lambda p: self._format_ap_ack("APXZ", p),
            # New command response handlers
            # "AP33": lambda p: f"IWAP33,{p}#",  # Working mode response
            # "AP34": lambda p: f"IWAP34,{p}#",  # Custom mode response
            # "AP16": lambda p: f"IWAP16,{p}#",  # Real-time location response
            # "AP85": lambda p: f"IWAP85,{p}#",  # Reminder response
            # "APJZ": lambda p: f"IWAPJZ,{p}#",  # BP calibration response
        }
        
        handler = handlers.get(command)
        if handler:
            return handler(params)
        else:
            logger.warning(f"Unknown command: {command}")
            # Some devices might expect a response even for unknown commands
            # You can return a generic response or None
            return None

    def get_device_snapshot(self, imei: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            device = self.devices.get(imei)
            if not device:
                return None
            
            # Get fall detection configuration from database
            fall_config = None
            try:
                settings = self.fall_event_manager.get_device_settings(imei)
                if settings:
                    fall_config = {
                        "enabled": bool(settings["fall_detection_enabled"]),
                        "sensitivity": settings["fall_sensitivity"],
                        "updated_at": settings["updated_at"]
                    }
            except Exception as e:
                logger.error(f"Error getting fall detection config for {imei}: {e}")
            
            # Return a shallow copy to avoid concurrent mutation issues
            snapshot = {
                "imei": imei,
                "status": device.get("status"),
                "last_seen": device.get("last_seen"),
                "last_location": device.get("last_location"),
                "last_alarm": device.get("last_alarm"),
                "metrics": device.get("metrics", {}),
                "fall_detection_config": fall_config
            }
            
            return snapshot

    # =====================
    # Parsing helpers
    # =====================
    def _nmea_to_decimal(self, dm: str, hemisphere: str, is_latitude: bool) -> Optional[float]:
        try:
            if is_latitude:
                degrees = int(dm[:2])
                minutes = float(dm[2:])
            else:
                degrees = int(dm[:3])
                minutes = float(dm[3:])
            value = degrees + minutes / 60.0
            if hemisphere in ("S", "W"):
                value = -value
            return round(value, 6)
        except Exception:
            return None

    def parse_ap01_location(self, params: str) -> Dict[str, Any]:
        """Parse AP01 params to structured JSON-friendly dict.

        Expected format (concatenated fields):
        DDYYMM [A|V] lat(NMEA) [N|S] lon(NMEA) [E|W] speed(km/h) HHMMSS direction status14 , MCC , MNC , LAC , CID , WIFI_LIST
        WIFI_LIST: SSID|MAC|RSSI [& SSID|MAC|RSSI]*
        """
        # Remove trailing whitespace
        s = params.strip()
        # Regex to capture the fixed header and optional LBS/WIFI
        # Date format is DDYYMM (day, year, month)
        pattern = re.compile(
            r"^(?P<date>\d{6})(?P<valid>[AV])"
            r"(?P<lat_dm>\d{2}\d{2}\.\d+)(?P<lat_hemi>[NS])"
            r"(?P<lon_dm>\d{3}\d{2}\.\d+)(?P<lon_hemi>[EW])"
            r"(?P<speed>\d+\.\d{1,3})"
            r"(?P<time>\d{6})"
            r"(?P<direction>\d+(?:\.\d+)?)"
            r"(?P<status>\d{14})"
            r"(?:,(?P<mcc>\d+),(?P<mnc>\d+),(?P<lac>\d+),(?P<cid>\d+))?"
            r"(?:,(?P<wifi>.*))?$"
        )
        m = pattern.match(s)
        if not m:
            return {"raw": s, "parse_error": True}

        groups = m.groupdict()
        date_str = groups["date"]  # DDYYMM format
        time_str = groups["time"]  # HHMMSS (UTC)
        # Parse DDYYMM format
        day = int(date_str[:2])
        yy = int(date_str[2:4])
        month = int(date_str[4:6])
        # Compose timestamp (assume 20YY for YY<70, else 19YY)
        year = 2000 + yy if yy < 70 else 1900 + yy
        hour = int(time_str[:2])
        minute = int(time_str[2:4])
        second = int(time_str[4:6])
        timestamp_iso: Optional[str] = None
        try:
            timestamp_iso = datetime.datetime(year, month, day, hour, minute, second).isoformat()
        except Exception:
            # Device sometimes sends out-of-range time (e.g., seconds=80). Keep components but omit ISO timestamp.
            timestamp_iso = None

        lat_dm = groups["lat_dm"]
        lon_dm = groups["lon_dm"]
        lat = self._nmea_to_decimal(lat_dm, groups["lat_hemi"], True)
        lon = self._nmea_to_decimal(lon_dm, groups["lon_hemi"], False)

        # Status 14-digit breakdown: 3+3+3+1+2+2
        status = groups["status"]
        try:
            gsm_signal = int(status[0:3])
            satellites = int(status[3:6])
            battery = int(status[6:9])
            remaining_space = int(status[9:10])
            fortification_state = int(status[10:12])
            working_mode = int(status[12:14])
        except Exception:
            gsm_signal = satellites = battery = remaining_space = None
            fortification_state = working_mode = None

        lbs = None
        if groups.get("mcc") and groups.get("mnc") and groups.get("lac") and groups.get("cid"):
            with suppress(Exception):
                lbs = {
                    "mcc": int(groups["mcc"]),
                    "mnc": int(groups["mnc"]),
                    "lac": int(groups["lac"]),
                    "cid": int(groups["cid"]),
                }

        wifi_list: Optional[str] = groups.get("wifi")
        wifi = []
        if wifi_list:
            for item in wifi_list.split("&"):
                item = item.strip()
                if not item:
                    continue
                parts = [p.strip() for p in item.split("|")]
                if len(parts) >= 3:
                    ssid, mac, rssi = parts[0], parts[1], parts[2]
                    # Normalize placeholder SSIDs like 'a' used by some firmwares
                    if ssid.lower() in {"a", "na", "null", "unknown"}:
                        ssid = None
                    with suppress(Exception):
                        rssi = int(rssi)
                    wifi.append({"ssid": ssid, "mac": mac, "rssi": rssi})

        return {
            "raw": s,
            "valid": groups["valid"] == "A",
            "date": f"{year:04d}-{month:02d}-{day:02d}",
            "time_utc": f"{hour:02d}:{minute:02d}:{second:02d}",
            "timestamp_utc": timestamp_iso,
            "latitude": lat,
            "longitude": lon,
            "lat_ddmm": f"{lat_dm}{groups['lat_hemi']}",
            "lon_ddmm": f"{lon_dm}{groups['lon_hemi']}",
            "speed_kmh": float(groups["speed"]) if groups.get("speed") else None,
            "direction_deg": float(groups["direction"]) if groups.get("direction") else None,
            "status": {
                "gsm_signal": gsm_signal,
                "satellites": satellites,
                "battery": battery,
                "remaining_space": remaining_space,
                "fortification_state": fortification_state,
                "working_mode": working_mode,
            },
            "lbs": lbs,
            "wifi": wifi,
        }
    
    def parse_ap10_alarm(self, params: str) -> Dict[str, Any]:
        """Parse AP10 alarm parameters to extract comprehensive alarm data
        
        Expected format:
        DDYYMM [A|V] lat(NMEA) [N|S] lon(NMEA) [E|W] speed(km/h) HHMMSS direction status14 , MCC , MNC , LAC , CID , alarm_state, language, flags, WIFI_LIST
        """
        try:
            s = params.strip()
            
            # Split by comma to get all parts
            parts = [p.strip() for p in s.split(',')]
            
            # Initialize result with defaults
            result = {
                "raw": s,
                "alarm_type": "00",
                "alarm_description": "No alarm",
                "latitude": None,
                "longitude": None,
                "speed_kmh": None,
                "direction_deg": None,
                "gsm_signal": None,
                "satellites": None,
                "battery_level": None,
                "remaining_space": None,
                "fortification_state": None,
                "working_mode": None,
                "mcc": None,
                "mnc": None,
                "lac": None,
                "cid": None,
                "language": None,
                "reply_flags": None,
                "wifi_data": [],
                "location_raw": s,
                "device_status": {},
                "is_fall_detection": False,
                "parse_error": False
            }
            
            if len(parts) < 6:
                result["parse_error"] = True
                result["error"] = "Insufficient parameters in alarm data"
                return result
            
            # Parse location portion (first part before LBS data)
            location_portion = parts[0]
            location_data = self.parse_ap01_location(location_portion + ",460,0,9520,3671")
            
            if location_data and not location_data.get("parse_error"):
                result.update({
                    "latitude": location_data.get("latitude"),
                    "longitude": location_data.get("longitude"),
                    "speed_kmh": location_data.get("speed_kmh"),
                    "direction_deg": location_data.get("direction_deg"),
                    "device_status": location_data.get("status", {}),
                    "wifi_data": location_data.get("wifi", [])
                })
                
                # Extract status fields
                status = location_data.get("status", {})
                if status:
                    result.update({
                        "gsm_signal": status.get("gsm_signal"),
                        "satellites": status.get("satellites"),
                        "battery_level": status.get("battery"),
                        "remaining_space": status.get("remaining_space"),
                        "fortification_state": status.get("fortification_state"),
                        "working_mode": status.get("working_mode")
                    })
            
            # Parse LBS data (MCC, MNC, LAC, CID)
            if len(parts) >= 5:
                try:
                    result["mcc"] = int(parts[1]) if parts[1] else None
                    result["mnc"] = int(parts[2]) if parts[2] else None
                    result["lac"] = int(parts[3]) if parts[3] else None
                    result["cid"] = int(parts[4]) if parts[4] else None
                except (ValueError, IndexError):
                    pass
            
            # Parse alarm state
            if len(parts) >= 6:
                result["alarm_type"] = parts[5]
                result["alarm_description"] = self._get_alarm_description(parts[5])
                result["is_fall_detection"] = parts[5] in ["05", "06"]
            
            # Parse language
            if len(parts) >= 7:
                result["language"] = parts[6]
            
            # Parse reply flags
            if len(parts) >= 8:
                result["reply_flags"] = parts[7]
            
            # Parse WiFi data (remaining parts)
            if len(parts) > 8:
                wifi_parts = parts[8:]
                wifi_data = []
                for wifi_part in wifi_parts:
                    if '|' in wifi_part:
                        wifi_items = wifi_part.split('|')
                        if len(wifi_items) >= 3:
                            wifi_data.append({
                                "ssid": wifi_items[0] if wifi_items[0] else None,
                                "mac": wifi_items[1] if wifi_items[1] else None,
                                "rssi": int(wifi_items[2]) if wifi_items[2].isdigit() else None
                            })
                result["wifi_data"] = wifi_data
            
            return result
            
        except Exception as e:
            logger.error(f"Error parsing AP10 alarm: {e}")
            return {
                "raw": params,
                "parse_error": True,
                "error": str(e),
                "alarm_type": "00",
                "alarm_description": "Parse error"
            }
    
    def _get_alarm_description(self, alarm_state: str) -> str:
        """Get human-readable description for alarm state"""
        alarm_descriptions = {
            "00": "No alarm",
            "01": "SOS alarm", 
            "03": "Not wearing alarm",
            "05": "Fall down alarm",
            "06": "Fall down alarm (variant)"
        }
        return alarm_descriptions.get(alarm_state, f"Unknown alarm ({alarm_state})")
    
    def send_command(self, imei: str, command: str, params: str) -> str:
        """Generate command to send to device"""
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        return f"IW{command},{imei},{timestamp},{params}#"
    
    def create_heartbeat_monitor_command(self, imei: str, seconds: int = 720) -> str:
        """
        Create a simple heartbeat monitor data command
        
        Args:
            imei (str): Device IMEI (15 digits)
            seconds (int): Heartbeat interval in seconds (default: 720 = 12 minutes)
        
        Returns:
            str: Formatted heartbeat monitor command
            Format: IWBP86,{imei},{timestamp},{status},{seconds}#
        """
        # Get current timestamp in HHMMSS format
        current_time = datetime.datetime.now().strftime("%H%M%S")
        
        # Status: 1 = enable heartbeat monitoring
        status = 1
        
        # Format: IWBP86,{imei},{timestamp},{status},{seconds}#
        command = f"IWBP86,{imei},{current_time},{status},{seconds}#"
        
        logger.info(f"Generated heartbeat monitor command: {command}")
        return command
    
    def create_test_heart_rate_command(self, imei: str) -> str:
        """Create BPXL Test heart rate command"""
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        command = f"IWBPXL,{imei},{timestamp}#"
        return command
    
    def create_test_blood_pressure_command(self, imei: str) -> str:
        """Create BPXY Test blood pressure command"""
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        command = f"IWBPXY,{imei},{timestamp}#"
        return command
    
    def create_test_temperature_command(self, imei: str) -> str:
        """Create BPXT Test temperature command"""
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        command = f"IWBPXT,{imei},{timestamp}#"
        return command
    
    def create_test_blood_oxygen_command(self, imei: str) -> str:
        """Create BPXZ Test blood oxygen command"""
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        command = f"IWBPXZ,{imei},{timestamp}#"
        return command
    
    def create_auto_test_heart_rate_command(self, imei: str, enabled: bool, minutes: int) -> str:
        """Create BP86 Set the interval of heart rate auto testing command"""
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        status = 1 if enabled else 0
        # Convert minutes to minutes (protocol expects minutes, not seconds)
        command = f"IWBP86,{imei},{timestamp},{status},{minutes}#"
        return command
    
    def create_auto_test_temperature_command(self, imei: str, enabled: bool, minutes: int) -> str:
        """Create BP87 set the interval of auto Test temperature command"""
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        status = 1 if enabled else 0
        # Convert minutes to minutes (protocol expects minutes)
        command = f"IWBP87,{imei},{timestamp},{status},{minutes}#"
        return command
    
    def create_fall_detection_switch_command(self, imei: str, enabled: bool) -> str:
        """Create BP76 fall down switch command"""
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        status = 1 if enabled else 0
        command = f"IWBP76,{imei},{timestamp},{status}#"
        logger.info(f"Generated fall detection switch command: {command}")
        return command
    
    def create_fall_sensitivity_command(self, imei: str, sensitivity_level: int) -> str:
        """Create BP77 fall down sensitivity command
        
        Args:
            imei: Device IMEI
            sensitivity_level: 1-3 (1=low, 2=medium, 3=high/most sensitive)
        """
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        # Validate sensitivity level
        if sensitivity_level not in [1, 2, 3]:
            raise ValueError("Sensitivity level must be 1, 2, or 3")
        command = f"IWBP77,{imei},{timestamp},{sensitivity_level}#"
        logger.info(f"Generated fall sensitivity command: {command}")
        return command
    
    def create_working_mode_command(self, imei: str, mode: int) -> str:
        """Create BP33 Working Mode command
        
        Args:
            imei: Device IMEI
            mode: Working mode (1=normal 15min, 2=power-saving 60min, 3=emergency 1min)
        
        Returns:
            str: Formatted BP33 command
        """
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        # Validate mode
        if mode not in [1, 2, 3]:
            raise ValueError("Mode must be 1 (normal), 2 (power-saving), or 3 (emergency)")
        command = f"IWBP33,{imei},{timestamp},{mode}#"
        logger.info(f"Generated working mode command: {command}")
        return command
    
    def create_custom_working_mode_command(self, imei: str, interval_seconds: int, gps_enabled: bool) -> str:
        """Create BP34 Custom Working Mode command
        
        Args:
            imei: Device IMEI
            interval_seconds: Custom reporting interval in seconds (minimum 30)
            gps_enabled: Whether to enable GPS tracking
        
        Returns:
            str: Formatted BP34 command
        """
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        mode = 8  # Custom mode identifier per protocol
        gps_status = 1 if gps_enabled else 0
        
        # Validate interval
        if interval_seconds < 30:
            raise ValueError("Interval must be at least 30 seconds")
        
        command = f"IWBP34,{imei},{timestamp},{mode},{interval_seconds},{gps_status}#"
        logger.info(f"Generated custom working mode command: {command}")
        return command
    
    def create_realtime_location_command(self, imei: str) -> str:
        """Create BP16 Real-time locating command
        
        Forces immediate location report - device responds with AP16 first,
        then asynchronously sends AP01 location data.
        
        Args:
            imei: Device IMEI
        
        Returns:
            str: Formatted BP16 command
        """
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        command = f"IWBP16,{imei},{timestamp}#"
        logger.info(f"Generated real-time location command: {command}")
        return command
    
    def create_reminder_command(self, imei: str, reminders: List[Dict[str, Any]]) -> str:
        """Create BP85 Set alarm/reminder command
        
        Args:
            imei: Device IMEI
            reminders: List of reminder dicts with keys:
                - time: str (HH:MM format)
                - days: str (1234567 for Mon-Sun, empty for specific days)
                - enabled: bool
                - type: int (1=medicine, 2=water, 3=sedentary)
        
        Returns:
            str: Formatted BP85 command
        """
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        master_switch = 1  # Enable all reminders
        count = len(reminders)
        
        if count == 0:
            raise ValueError("At least one reminder must be provided")
        if count > 10:
            raise ValueError("Maximum 10 reminders allowed")
        
        reminder_strings = []
        for r in reminders:
            # Validate reminder data
            if 'time' not in r:
                raise ValueError("Reminder must include 'time' field")
            
            time_str = r['time'].replace(':', '')  # Convert HH:MM to HHMM
            days = r.get('days', '1234567')  # Default all days
            enabled = 1 if r.get('enabled', True) else 0
            reminder_type = r.get('type', 1)  # Default to medicine reminder
            
            # Validate reminder type
            if reminder_type not in [1, 2, 3]:
                raise ValueError("Reminder type must be 1 (medicine), 2 (water), or 3 (sedentary)")
            
            reminder_strings.append(f"{time_str},{days},{enabled},{reminder_type}")
        
        reminders_formatted = '@'.join(reminder_strings)
        command = f"IWBP85,{imei},{timestamp},{master_switch},{count},{reminders_formatted}#"
        logger.info(f"Generated reminder command: {command}")
        return command
    
    def create_bp_calibration_command(self, imei: str, systolic: int, diastolic: int, age: int, is_male: bool) -> str:
        """Create BPJZ blood pressure calibration command
        
        Calibrates blood pressure readings - may trigger automatic BP monitoring after calibration.
        
        Args:
            imei: Device IMEI
            systolic: Systolic blood pressure (60-250 mmHg)
            diastolic: Diastolic blood pressure (40-150 mmHg)
            age: User age (1-120 years)
            is_male: True for male, False for female
        
        Returns:
            str: Formatted BPJZ command
        """
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        gender = 1 if is_male else 0
        
        # Validate blood pressure values
        if not (60 <= systolic <= 250):
            raise ValueError("Systolic pressure must be between 60 and 250 mmHg")
        if not (40 <= diastolic <= 150):
            raise ValueError("Diastolic pressure must be between 40 and 150 mmHg")
        if systolic <= diastolic:
            raise ValueError("Systolic pressure must be higher than diastolic pressure")
        if not (1 <= age <= 120):
            raise ValueError("Age must be between 1 and 120 years")
        
        command = f"IWBPJZ,{imei},{timestamp},{systolic},{diastolic},{age},{gender}#"
        logger.info(f"Generated BP calibration command: {command}")
        return command

