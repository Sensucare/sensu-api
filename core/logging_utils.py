import threading
import datetime
import logging
import os
import json
from typing import Optional, Tuple


logger = logging.getLogger(__name__)


class DataLoggerHandler(logging.Handler):
    """Custom logging handler to capture all terminal logs"""

    def __init__(self, data_logger):
        super().__init__()
        self.data_logger = data_logger
        self._in_emit = False  # Prevent recursion

    def emit(self, record):
        """Emit a log record to the data logger"""
        # Prevent infinite recursion
        if self._in_emit:
            return

        try:
            self._in_emit = True
            # Skip logs from this handler itself to avoid recursion
            if record.name == __name__ and "Failed to write" in record.getMessage():
                return
            # Save to data logger
            self.data_logger._write_terminal_log(record.levelname, record.getMessage())
        except Exception:
            # Avoid infinite recursion if logging fails
            pass
        finally:
            self._in_emit = False


class DataLogger:
    """Logger for all incoming and outgoing data"""

    def __init__(self, log_file: str = "gps_watch_data.log"):
        self.log_file = log_file
        self._lock = threading.RLock()
        # Ensure log directory exists
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)

        # Set up custom handler to capture all terminal logs
        self._setup_terminal_logging()

    def log_incoming(self, address: Tuple[str, int], imei: Optional[str], data: str):
        """Log incoming data from device"""
        self._write_log("INCOMING", address, imei, data)

    def log_outgoing(self, address: Tuple[str, int], imei: Optional[str], data: str):
        """Log outgoing data to device"""
        self._write_log("OUTGOING", address, imei, data)

    def _write_log(self, direction: str, address: Tuple[str, int], imei: Optional[str], data: str):
        """Write log entry to file"""
        with self._lock:
            timestamp = datetime.datetime.now().isoformat()
            log_entry = {
                "timestamp": timestamp,
                "direction": direction,
                "address": f"{address[0]}:{address[1]}",
                "imei": imei or "unknown",
                "data": data.strip()
            }

            try:
                with open(self.log_file, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(log_entry) + '\n')
            except Exception as e:
                logger.error(f"Failed to write to log file: {e}")

    def _write_system_log(self, level: str, message: str):
        """Write system log entry to file"""
        with self._lock:
            timestamp = datetime.datetime.now().isoformat()
            log_entry = {
                "timestamp": timestamp,
                "direction": "SYSTEM",
                "level": level,
                "message": message
            }

            try:
                with open(self.log_file, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(log_entry) + '\n')
            except Exception as e:
                # Use print to avoid infinite recursion
                print(f"Failed to write to log file: {e}")

    def _write_terminal_log(self, level: str, message: str):
        """Write terminal log entry to file"""
        with self._lock:
            timestamp = datetime.datetime.now().isoformat()
            log_entry = {
                "timestamp": timestamp,
                "direction": "TERMINAL",
                "level": level,
                "message": message
            }

            try:
                with open(self.log_file, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(log_entry) + '\n')
            except Exception as e:
                # Use print to avoid infinite recursion
                print(f"Failed to write terminal log to file: {e}")

    def _setup_terminal_logging(self):
        """Set up custom handler to capture all terminal logs"""
        try:
            # Create custom handler
            handler = DataLoggerHandler(self)
            handler.setLevel(logging.INFO)  # Capture INFO and above

            # Add handler to the main logger to capture application logs
            app_logger = logging.getLogger("watch_app")
            app_logger.addHandler(handler)

            # Also add to uvicorn logger to capture server logs
            uvicorn_logger = logging.getLogger("uvicorn")
            uvicorn_logger.addHandler(handler)

        except Exception as e:
            print(f"Failed to setup terminal logging: {e}")

    def get_logs(self, limit: Optional[int] = None) -> str:
        """Get all logs as plain text"""
        try:
            if not os.path.exists(self.log_file):
                return "No logs available yet.\n"

            with open(self.log_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            if limit:
                lines = lines[-limit:]

            formatted_logs = []
            for line in lines:
                try:
                    entry = json.loads(line.strip())
                    direction = entry.get('direction', '')

                    if direction in ['SYSTEM', 'TERMINAL']:
                        # Format system/terminal logs
                        timestamp = entry['timestamp'].replace('T', ' ')
                        if '.' in timestamp:
                            # Convert microseconds to milliseconds
                            parts = timestamp.split('.')
                            timestamp = parts[0] + ',' + parts[1][:3]
                        else:
                            timestamp += ',000'
                        formatted_line = f"{timestamp} - {entry.get('level', 'INFO')} - {entry['message']}"
                    else:
                        # Format communication logs
                        formatted_line = f"[{entry['timestamp']}] {entry['direction']} - {entry['address']} (IMEI: {entry['imei']}) - {entry['data']}"
                    formatted_logs.append(formatted_line)
                except json.JSONDecodeError:
                    # Handle any malformed lines
                    formatted_logs.append(f"[MALFORMED] {line.strip()}")

            return '\n'.join(formatted_logs) + '\n'

        except Exception as e:
            # Use print to avoid infinite recursion
            print(f"Failed to read log file: {e}")
            return f"Error reading logs: {e}\n"
