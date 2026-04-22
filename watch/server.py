import socket
import threading
import logging
from typing import Optional, Tuple
from contextlib import suppress

from watch.protocol import GPSWatchProtocolHandler, ConnectionManager, StaleSessionReaper

logger = logging.getLogger(__name__)


class GPSWatchTCPServer:
    """TCP Server for GPS Watch Protocol"""

    def __init__(self, host: str = '0.0.0.0', port: int = 5088,
                 alarm_event_manager=None, fall_event_manager=None, data_logger=None):
        self.host = host
        self.port = port
        self.server_socket = None
        self.running = False
        self.data_logger = data_logger
        self.handler = GPSWatchProtocolHandler(
            alarm_event_manager=alarm_event_manager,
            fall_event_manager=fall_event_manager,
            data_logger=data_logger,
        )
        self.manager = ConnectionManager(self.handler)
        self._reaper = StaleSessionReaper(self.manager, self.handler)
        self.scheduler = None  # Will be initialized later to avoid circular import
    
    def handle_client(self, client_socket: socket.socket, address: Tuple[str, int]):
        """Handle individual client connection"""
        logger.info(f"New connection from {address}")
        current_imei: Optional[str] = None
        # Enable TCP keepalive
        self.manager._set_tcp_keepalive(client_socket)
        try:
            buffer = ""
            while self.running:
                # Receive data with timeout
                client_socket.settimeout(60.0)  # 60 second timeout
                try:
                    data = client_socket.recv(1024)
                    if not data:
                        logger.info(f"Client {address} disconnected")
                        break
                except socket.timeout:
                    # No data received within timeout - connection might still be alive
                    continue
                
                # Decode and add to buffer
                try:
                    decoded_data = data.decode('utf-8', errors='ignore')
                    buffer += decoded_data
                    logger.debug(f"Raw data received: {decoded_data}")
                except Exception as e:
                    logger.error(f"Decode error: {e}")
                    continue
                
                # Process complete messages (ending with #)
                while '#' in buffer:
                    end_idx = buffer.index('#') + 1
                    message = buffer[:end_idx].strip()  # Strip any whitespace
                    buffer = buffer[end_idx:]
                    
                    if message:  # Only process non-empty messages
                        logger.info(f"Received from {address}: {message}")
                        
                        # Log incoming data
                        self.data_logger.log_incoming(address, current_imei, message)
                        
                        # Process message and get response
                        response = self.handler.process_message(message)
                        
                        # On login (AP00), map IMEI -> socket so future sends use IMEI
                        try:
                            parsed = self.handler.parse_message(message)
                            if parsed and parsed.get("command", "").upper() == "AP00":
                                imei = parsed.get("params", "").strip()
                                if imei:
                                    self.manager.register(imei, client_socket, address)
                                    current_imei = imei
                                    logger.info(f"Registered IMEI {imei} to {address}")
                                    
                                    # Configure auto-test intervals for the device
                                    if self.scheduler:
                                        try:
                                            self.scheduler.configure_device_auto_tests(imei)
                                        except Exception as e:
                                            logger.error(f"Error configuring auto-tests for {imei}: {e}")
                                else:
                                    logger.warning("AP00 received without IMEI")
                            else:
                                # For messages after login, update device state with IMEI context
                                if current_imei:
                                    cmd = parsed.get("command", "").upper()
                                    params = parsed.get("params", "")
                                    if cmd == "AP01":
                                        response = self.handler.handle_ap01_location_for_imei(current_imei, params)
                                    elif cmd == "AP03":
                                        response = self.handler.handle_ap03_heartbeat_for_imei(current_imei, params)
                                    elif cmd == "AP10":
                                        response = self.handler.handle_ap10_alarm_for_imei(current_imei, params)
                                    elif cmd == "AP49":
                                        response = self.handler.handle_ap49_heart_rate_for_imei(current_imei, params)
                                    elif cmd == "AP50":
                                        response = self.handler.handle_ap50_temperature_for_imei(current_imei, params)
                                    elif cmd == "APHT":
                                        response = self.handler.handle_apht_health_for_imei(current_imei, params)
                                    elif cmd == "APHP":
                                        response = self.handler.handle_aphp_health_params_for_imei(current_imei, params)
                                    elif cmd in ("APXL", "APXY", "APXT", "APXZ"):
                                        # Acknowledge device responses to BPXL/BPXY/BPXT/BPXZ by echoing APxx with params
                                        response = f"IWAP{cmd[2:]},{params}#"
                                    # Touch last_seen for any post-login message
                                    self.manager.touch(current_imei)
                        except Exception as e:
                            logger.error(f"Error mapping IMEI to socket: {e}")
                        
                        if response:
                            logger.info(f"Sending to {address}: {response}")
                            
                            # Log outgoing data
                            self.data_logger.log_outgoing(address, current_imei, response)
                            
                            try:
                                client_socket.send(response.encode('utf-8'))
                            except Exception as e:
                                logger.error(f"Error sending response: {e}")
                                break
                        
        except Exception as e:
            logger.error(f"Error handling client {address}: {e}")
        finally:
            logger.info(f"Connection closed from {address}")
            client_socket.close()
            # Cleanup IMEI mapping for this socket if present
            self.manager.unregister_socket(client_socket)
            with self.handler._lock:
                if current_imei and current_imei in self.handler.devices:
                    dev = self.handler.devices[current_imei]
                    dev["status"] = "offline"
                    self.handler.devices[current_imei] = dev
    
    def send_to_device(self, imei: str, message: str):
        """Send message to specific device by IMEI"""
        ok = self.manager.send(imei, message)
        if ok:
            logger.info(f"Sent to IMEI {imei}: {message}")
            # Log outgoing command (get address from manager if available)
            sessions = self.manager.list_sessions()
            session = sessions.get(imei)
            address = session.get("address") if session else ("unknown", 0)
            if address and isinstance(address, tuple):
                self.data_logger.log_outgoing(address, imei, message)
            return True
        logger.warning(f"No active connection for IMEI {imei}")
        return False
    
    def send_heartbeat_monitor(self, imei: str, seconds: int = 720) -> str:
        """
        Send heartbeat monitor command to device
        
        Args:
            imei (str): Device IMEI
            seconds (int): Heartbeat interval in seconds (default: 720 = 12 minutes)
        
        Returns:
            str: The generated command (for logging/debugging)
        """
        command = self.handler.create_heartbeat_monitor_command(imei, seconds)
        
        # Send directly to mapped IMEI
        try:
            if self.send_to_device(imei, command):
                logger.info(f"Heartbeat monitor command sent to device {imei}")
        except Exception as e:
            logger.error(f"Failed to send heartbeat monitor to IMEI {imei}: {e}")
        
        return command
    
    def start(self):
        """Start the TCP server"""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        try:
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(5)
            self.running = True
            
            # Initialize and start scheduler
            if not self.scheduler:
                from watch.scheduler import HealthTestScheduler
                self.scheduler = HealthTestScheduler(self)
                self.scheduler.start()
                logger.info("Health test scheduler initialized and started")
            
            # Start session reaper
            self._reaper.start()
            
            logger.info(f"GPS Watch TCP Server started on {self.host}:{self.port}")
            logger.info("Waiting for connections...")
            
            while self.running:
                try:
                    client_socket, address = self.server_socket.accept()
                    
                    # Handle each client in a separate thread
                    client_thread = threading.Thread(
                        target=self.handle_client,
                        args=(client_socket, address)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                    
                except socket.error as e:
                    if self.running:
                        logger.error(f"Socket error: {e}")
                        
        except Exception as e:
            logger.error(f"Server error: {e}")
        finally:
            self.stop()
    
    def stop(self):
        """Stop the TCP server"""
        self.running = False
        
        # Stop scheduler
        if self.scheduler:
            self.scheduler.stop()
            logger.info("Health test scheduler stopped")
        
        if self.server_socket:
            self.server_socket.close()
        
        # Close all client connections
        with self.manager._lock:
            for client_socket in list(self.manager._sock_to_imei.keys()):
                with suppress(Exception):
                    client_socket.close()
                self.manager.unregister_socket(client_socket)
        with suppress(Exception):
            self._reaper.stop()
        
        logger.info("Server stopped")

