import threading
import time
import datetime
import logging
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from watch.server import GPSWatchTCPServer

logger = logging.getLogger(__name__)


class HealthTestScheduler:
    """Scheduler for sending periodic health test commands to active GPS watches"""
    
    def __init__(self, server: 'GPSWatchTCPServer'):
        self.server = server
        self.running = False
        self._lock = threading.RLock()
        self._scheduler_thread: Optional[threading.Thread] = None
        
        # Configuration
        self.test_interval_seconds = 60  # How often to send test commands
        self.auto_test_interval_minutes = 1  # Auto-test interval for devices (in minutes)
        self.enabled_tests = ['heart_rate', 'blood_pressure', 'temperature', 'blood_oxygen']
        self.auto_configure_on_login = True  # Whether to set auto-test intervals on device login
        
        # Internal state
        self._last_test_time = {}  # Track last test time per device
        
    def get_config(self) -> Dict:
        """Get current scheduler configuration"""
        with self._lock:
            return {
                'running': self.running,
                'test_interval_seconds': self.test_interval_seconds,
                'auto_test_interval_minutes': self.auto_test_interval_minutes,
                'enabled_tests': self.enabled_tests.copy(),
                'auto_configure_on_login': self.auto_configure_on_login,
            }
    
    def update_config(self, config: Dict) -> None:
        """Update scheduler configuration"""
        with self._lock:
            if 'test_interval_seconds' in config:
                self.test_interval_seconds = max(10, int(config['test_interval_seconds']))
            if 'auto_test_interval_minutes' in config:
                self.auto_test_interval_minutes = max(1, int(config['auto_test_interval_minutes']))
            if 'enabled_tests' in config:
                valid_tests = ['heart_rate', 'blood_pressure', 'temperature', 'blood_oxygen']
                self.enabled_tests = [t for t in config['enabled_tests'] if t in valid_tests]
            if 'auto_configure_on_login' in config:
                self.auto_configure_on_login = bool(config['auto_configure_on_login'])
        
        logger.info(f"Scheduler configuration updated: {self.get_config()}")
    
    def start(self) -> bool:
        """Start the health test scheduler"""
        with self._lock:
            if self.running:
                logger.warning("Scheduler is already running")
                return False
            
            self.running = True
            self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
            self._scheduler_thread.start()
            
            logger.info(f"Health test scheduler started - interval: {self.test_interval_seconds}s, tests: {self.enabled_tests}")
            return True
    
    def stop(self) -> bool:
        """Stop the health test scheduler"""
        with self._lock:
            if not self.running:
                return False
            
            self.running = False
            
            # Wait for scheduler thread to finish
            if self._scheduler_thread and self._scheduler_thread.is_alive():
                self._scheduler_thread.join(timeout=5.0)
            
            logger.info("Health test scheduler stopped")
            return True
    
    def configure_device_auto_tests(self, imei: str) -> bool:
        """Configure auto-test intervals for a device when it logs in"""
        if not self.auto_configure_on_login:
            return False
        
        success = True
        interval_minutes = self.auto_test_interval_minutes
        
        try:
            # Set heart rate auto-test interval (BP86)
            if 'heart_rate' in self.enabled_tests:
                cmd = self.server.handler.create_auto_test_heart_rate_command(imei, True, interval_minutes)
                if not self.server.send_to_device(imei, cmd):
                    logger.warning(f"Failed to set heart rate auto-test for {imei}")
                    success = False
                else:
                    logger.info(f"Set heart rate auto-test interval to {interval_minutes} min for {imei}")
            
            # Set temperature auto-test interval (BP87)
            if 'temperature' in self.enabled_tests:
                cmd = self.server.handler.create_auto_test_temperature_command(imei, True, interval_minutes)
                if not self.server.send_to_device(imei, cmd):
                    logger.warning(f"Failed to set temperature auto-test for {imei}")
                    success = False
                else:
                    logger.info(f"Set temperature auto-test interval to {interval_minutes} min for {imei}")
                    
        except Exception as e:
            logger.error(f"Error configuring auto-tests for {imei}: {e}")
            success = False
        
        return success
    
    def send_test_command(self, imei: str, test_type: str) -> bool:
        """Send a specific test command to a device"""
        try:
            cmd = None
            
            if test_type == 'heart_rate':
                cmd = self.server.handler.create_test_heart_rate_command(imei)
            elif test_type == 'blood_pressure':
                cmd = self.server.handler.create_test_blood_pressure_command(imei)
            elif test_type == 'temperature':
                cmd = self.server.handler.create_test_temperature_command(imei)
            elif test_type == 'blood_oxygen':
                cmd = self.server.handler.create_test_blood_oxygen_command(imei)
            else:
                logger.warning(f"Unknown test type: {test_type}")
                return False
            
            if cmd and self.server.send_to_device(imei, cmd):
                logger.debug(f"Sent {test_type} test command to {imei}")
                return True
            else:
                logger.warning(f"Failed to send {test_type} test command to {imei}")
                return False
                
        except Exception as e:
            logger.error(f"Error sending {test_type} test to {imei}: {e}")
            return False
    
    def send_all_test_commands(self, imei: str) -> int:
        """Send all enabled test commands to a device"""
        success_count = 0
        
        for test_type in self.enabled_tests:
            if self.send_test_command(imei, test_type):
                success_count += 1
        
        return success_count
    
    def _scheduler_loop(self) -> None:
        """Main scheduler loop that runs in background thread"""
        logger.info("Health test scheduler loop started")
        
        while self.running:
            try:
                # Get list of active/connected devices
                sessions = self.server.manager.list_sessions()
                active_devices = [imei for imei, session in sessions.items() if session.get('connected')]
                
                if not active_devices:
                    logger.debug("No active devices to test")
                else:
                    logger.debug(f"Sending test commands to {len(active_devices)} active devices: {active_devices}")
                    
                    # Send test commands to each active device
                    total_commands = 0
                    for imei in active_devices:
                        commands_sent = self.send_all_test_commands(imei)
                        total_commands += commands_sent
                        
                        # Update last test time
                        self._last_test_time[imei] = datetime.datetime.now()
                    
                    if total_commands > 0:
                        logger.info(f"Sent {total_commands} test commands to {len(active_devices)} devices")
                
                # Clean up last test times for disconnected devices
                connected_imeis = set(active_devices)
                with self._lock:
                    old_imeis = set(self._last_test_time.keys()) - connected_imeis
                    for imei in old_imeis:
                        del self._last_test_time[imei]
                
                # Sleep until next test cycle
                for _ in range(self.test_interval_seconds):
                    if not self.running:
                        break
                    time.sleep(1)
                    
            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}")
                # Sleep a bit before retrying
                time.sleep(5)
        
        logger.info("Health test scheduler loop ended")
    
    def get_status(self) -> Dict:
        """Get scheduler status and statistics"""
        with self._lock:
            sessions = self.server.manager.list_sessions()
            active_devices = [imei for imei, session in sessions.items() if session.get('connected')]
            
            status = {
                'running': self.running,
                'active_devices_count': len(active_devices),
                'active_devices': active_devices,
                'config': self.get_config(),
                'last_test_times': {
                    imei: dt.isoformat() if isinstance(dt, datetime.datetime) else dt
                    for imei, dt in self._last_test_time.items()
                }
            }
            
            return status