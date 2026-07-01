# -*- coding: utf-8 -*-
"""
Generic sensor command class for BLE datalogger devices.
Based on the Bleak GATT client for cross-platform BLE communication.
"""
import logging
import asyncio
import os
from bleak import BleakClient, BleakScanner
from functools import reduce
import struct
from typing import Optional, Callable, Dict, Any
from enum import Enum

# Standard UUIDs for the datalogger devices (GSP protocol)
GSP_SERVICE_UUID = "34802252-7185-4d5d-b431-630e7050e8f0"
WRITE_CHARACTERISTIC_UUID = "34800001-7185-4d5d-b431-630e7050e8f0"
NOTIFY_CHARACTERISTIC_UUID = "34800002-7185-4d5d-b431-630e7050e8f0"

DL_STATES = {
    1: "Unknown",
    2: "Ready",
    3: "Logging",
}
# GSP Command Codes
GSP_CMD_HELLO = 0
GSP_CMD_SUBSCRIBE = 1
GSP_CMD_UNSUBSCRIBE = 2
GSP_CMD_FETCH_LOG = 3
GSP_CMD_GET = 4
GSP_CMD_CLEAR_LOGBOOK = 5
GSP_CMD_PUT_DATALOGGER_CONFIG = 6
GSP_CMD_PUT_SYSTEMMODE = 7
GSP_CMD_PUT_UTCTIME = 8
GSP_CMD_PUT_DATALOGGER_STATE = 9

# GSP Response Codes
GSP_RESP_COMMAND_RESPONSE = 1
GSP_RESP_DATA = 2
GSP_RESP_DATA_PART2 = 3


class CommandType(Enum):
    HELLO = GSP_CMD_HELLO
    SUBSCRIBE = GSP_CMD_SUBSCRIBE
    UNSUBSCRIBE = GSP_CMD_UNSUBSCRIBE
    FETCH_LOG = GSP_CMD_FETCH_LOG
    GET = GSP_CMD_GET
    CLEAR_LOGBOOK = GSP_CMD_CLEAR_LOGBOOK
    PUT_DATALOGGER_CONFIG = GSP_CMD_PUT_DATALOGGER_CONFIG
    PUT_SYSTEMMODE = GSP_CMD_PUT_SYSTEMMODE
    PUT_UTCTIME = GSP_CMD_PUT_UTCTIME
    PUT_DATALOGGER_STATE = GSP_CMD_PUT_DATALOGGER_STATE


class DataView:
    """Helper class for parsing binary data from BLE responses."""
    
    def __init__(self, array, bytes_per_element=1):
        self.array = array
        self.bytes_per_element = bytes_per_element

    def __get_binary(self, start_index, byte_count, signed=False):
        integers = [self.array[start_index + x] for x in range(byte_count)]
        bytes = [integer.to_bytes(
            self.bytes_per_element, byteorder='little', signed=signed) for integer in integers]
        return reduce(lambda a, b: a + b, bytes)

    def get_uint_8(self, start_index):
        bytes_to_read = 1
        return int.from_bytes(self.__get_binary(start_index, bytes_to_read), byteorder='little')

    def get_uint_16(self, start_index):
        bytes_to_read = 2
        return int.from_bytes(self.__get_binary(start_index, bytes_to_read), byteorder='little')

    def get_uint_32(self, start_index):
        bytes_to_read = 4
        binary = self.__get_binary(start_index, bytes_to_read)
        return struct.unpack('<I', binary)[0]

    def get_float_32(self, start_index):
        bytes_to_read = 4
        binary = self.__get_binary(start_index, bytes_to_read)
        return struct.unpack('<f', binary)[0]


class SensorCommand:
    """Generic sensor command class for BLE datalogger operations with context manager support."""
    
    def __init__(self, end_of_serial: str, set_time: bool = True, logger: Optional[logging.Logger] = None):
        self.end_of_serial = end_of_serial
        self.set_time = set_time
        self.logger = logger or logging.getLogger(__name__)
        self.logger.setLevel(logging.getLogger().level)
        self.client: Optional[BleakClient] = None
        self.device_address: Optional[str] = None
        self.device_name: Optional[str] = None
        self.is_connected = False
        self.response_queue = asyncio.Queue()
        self.disconnected_event = asyncio.Event()
        self.response_handlers: Dict[int, Callable] = {}
        
    async def __aenter__(self):
        """Async context manager entry - discover and connect to device."""
        #self.logger.info(f"Entering context for device with serial ending: {self.end_of_serial}", stack_info=True)
        await self._discover_and_connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - ensure proper disconnection."""
        #self.logger.info(f"Exiting context for device {self.device_name}", stack_info=True)
        await self.disconnect()
        return False  # Don't suppress exceptions
    
    async def _discover_and_connect(self) -> bool:
        """Combined discover and connect operation."""

        logging.getLogger("bleak.backends.winrt.scanner").setLevel(logging.WARNING)
        logging.getLogger("bleak.backends.winrt.client").setLevel(logging.WARNING)

        # Discover device with early detection callback
        device_found = asyncio.Event()
        target_device = None
        
        def detection_callback(device, advertisement_data):
            nonlocal target_device

            # Ignore unnaamed devices
            if not device.name:
                return

            # Ignore everything except Movesense devices
            if not device.name.startswith("Movesense"):
                return

            self.logger.debug(f"Found device: {device}")
            if device.name and device.name.endswith(self.end_of_serial):
                self.logger.info(f"Target device found: {device.name}")
                target_device = device
                device_found.set()
        
        scanner = BleakScanner(detection_callback=detection_callback)
        await scanner.start()
        
        try:
            # Wait for device to be found or timeout after 10 seconds
            await asyncio.wait_for(device_found.wait(), timeout=10)
            self.device_address = target_device.address
            self.device_name = target_device.name
        except asyncio.TimeoutError:
            raise RuntimeError(f"Device ending with '{self.end_of_serial}' not found")
        finally:
            await scanner.stop()
        
        # Connect to device
        try:
            self.client = BleakClient(
                self.device_address, 
                disconnected_callback=self._disconnect_callback
            )
            # Set sensor reference for testing (if mock client supports it)
            if hasattr(self.client, 'set_sensor'):
                self.client.set_sensor(self)

            await self.client.connect()
            self.is_connected = True
            self.logger = logging.getLogger(f"SensorCommand-{self.device_name}")
            self.logger.setLevel(logging.getLogger().level)
            self.logger.info(f"Connected to {self.device_name}")

            for service in self.client.services:
                self.logger.info(f"Service: {service.uuid}")
                for char in service.characteristics:
                    self.logger.info(f"  Characteristic: {char.uuid}, properties: {char.properties}")

            # Start notifications
            await self.client.start_notify(
                NOTIFY_CHARACTERISTIC_UUID, 
                self._notification_handler
            )

            if self.set_time:
                import time
                utc_time = int(time.time()*1000*1000)  # microseconds
                await self.set_utc_time(utc_time)
                self.logger.info(f"UTC time set to {utc_time}")

            self.logger.info(f"Notifications enabled: {self.device_name}")
            return True
            
        # except Exception as e:
        #     self.logger.error(f"Connection failed: {e}\nPlease try again.")
        #     raise RuntimeError(f"Failed to connect to {self.device_name}: {e}")

        except Exception as e:
            # Debug: log what services were found before the failure (if any)
            if self.client and hasattr(self.client, 'services') and self.client.services:
                for service in self.client.services:
                    self.logger.warning(f"Service found before failure: {service.uuid}")
                    for char in service.characteristics:
                        self.logger.warning(f"  Characteristic: {char.uuid}, properties: {char.properties}")
            else:
                self.logger.info("No services/characteristics discovered before failure")
            self.logger.error(f"Connection failed: {e}\nPlease try again.")
            raise RuntimeError(f"Failed to connect to {self.device_name}: {e}")
        
        
    async def discover_device(self, end_of_serial: str) -> bool:
        """Discover device by serial number suffix. (Deprecated - use context manager instead)"""
        self.logger.warning("discover_device is deprecated. Use 'async with SensorCommand(serial)' instead.")
        devices = await BleakScanner.discover()
        
        for device in devices:
            self.logger.debug(f"Found device: {device}")
            if device.name and device.name.endswith(end_of_serial):
                self.logger.info(f"Target device found: {device.name}")
                self.device_address = device.address
                self.device_name = device.name
                return True
                
        self.logger.error(f"Device ending with '{end_of_serial}' not found")
        return False
    
    async def connect(self) -> bool:
        """Establish BLE connection to the discovered device. (Deprecated - use context manager instead)"""
        self.logger.warning("connect is deprecated. Use 'async with SensorCommand(serial)' instead.")
        if not self.device_address:
            self.logger.error("No device address available. Run discover_device first.")
            return False
            
        try:
            self.client = BleakClient(
                self.device_address, 
                disconnected_callback=self._disconnect_callback
            )
            await self.client.connect()
            self.is_connected = True

            for service in self.client.services:
                self.logger.info(f"Service: {service.uuid}")
                for char in service.characteristics:
                    self.logger.info(f"  Characteristic: {char.uuid}, properties: {char.properties}")
            
            # Start notifications
            await self.client.start_notify(
                NOTIFY_CHARACTERISTIC_UUID, 
                self._notification_handler
            )
            
            self.logger.info(f"Connected to {self.device_name}")
            return True
            
        # except Exception as e:
        #     self.logger.error(f"Connection failed: {e}\nPlease try again.")
        #     return False
        except Exception as e:
            # Debug: log what services were found before the failure (if any)
            if self.client and hasattr(self.client, 'services') and self.client.services:
                for service in self.client.services:
                    self.logger.info(f"Service found before failure: {service.uuid}")
                    for char in service.characteristics:
                        self.logger.info(f"  Characteristic: {char.uuid}, properties: {char.properties}")
            else:
                self.logger.info("No services/characteristics discovered before failure")
            self.logger.error(f"Connection failed: {e}\nPlease try again.")
            raise RuntimeError(f"Failed to connect to {self.device_name}: {e}")
    
    async def disconnect(self):
        """Disconnect from the BLE device."""
        self.logger.info(f"Disconnecting from device {self.device_name}...")
        if self.client and self.is_connected:
            try:
                await self.client.stop_notify(NOTIFY_CHARACTERISTIC_UUID)
                await self.client.disconnect()
                self.logger.info(f"Disconnected from device {self.device_name}")
                self.is_connected = False
            except Exception as e:
                self.logger.error(f"Disconnect error: {e}")
    
    def _disconnect_callback(self, client):
        """Handle unexpected disconnection."""
        self.logger.info("Device disconnect detected")
        self.is_connected = False
        self.disconnected_event.set()
    
    async def _notification_handler(self, sender, data):
        """Handle incoming notifications from the device."""
        try:
            dv = DataView(data)
            response_code = dv.get_uint_8(0)
            reference = dv.get_uint_8(1)
            
            response_data = {
                'response_code': response_code,
                'reference': reference,
                'raw_data': data,
                'parsed': dv
            }
            #self.logger.debug(f"Notification received: {response_data}")

            # Parse response based on GSP protocol
            if response_code == GSP_RESP_COMMAND_RESPONSE:
                # Command response: response_code(1), reference(1), status_code(2), data(N)
                if len(data) >= 4:
                    if hasattr(self, "hello_ref") and reference == self.hello_ref:
                        data_idx = 2
                        status_code = 200  # HELLO command does not have status code
                    else:
                        status_code = dv.get_uint_16(2)
                        data_idx = 4

                    self.logger.debug(f"Command status: {status_code}")
                    response_data['status_code'] = status_code
                    response_data['command_data'] = data[data_idx:] if len(data) > data_idx else b''
                else:
                    response_data['status_code'] = None
                    response_data['command_data'] = b''
                    
            elif response_code in [GSP_RESP_DATA, GSP_RESP_DATA_PART2]:
                # Data response: response_code(1), reference(1), data(N)
                response_data['data_payload'] = data[2:] if len(data) > 2 else b''
            
            # Queue the response for processing
            #self.logger.debug(f"Queuing response: {response_data}")
            await self.response_queue.put(response_data)
            
        except Exception as e:

            self.logger.error(f"Error handling notification:", exc_info=e)
    
    async def send_command(self, command_bytes: bytearray, timeout: float = 10.0) -> Dict[str, Any]:
        """Send a command and wait for response."""
        if not self.is_connected:
            raise RuntimeError("Not connected to device")
        
        try:
            # Clear any pending responses
            while not self.response_queue.empty():
                await self.response_queue.get()
            
            # Send the command
            await self.client.write_gatt_char(
                WRITE_CHARACTERISTIC_UUID, 
                command_bytes, 
                response=True
            )
            
            # Wait for response with timeout
            try:
                response = await asyncio.wait_for(
                    self.response_queue.get(), 
                    timeout=timeout
                )
                return response
            except asyncio.TimeoutError:
                raise TimeoutError(f"Command timeout after {timeout}s")
                
        except Exception as e:
            self.logger.error(f"Command failed: {e}")
            raise
    
    async def get_battery_level(self) -> Dict[str, Any]:
        response = await self.get_resource("/System/Energy/Level")
        logging.info(f"Battery level result: {response}")
        
        retval = {}
        if response.get('success'):
            status_code = response.get('status_code', 0)
            if status_code == 200:  # HTTP OK
                # Parse HELLO response data
                command_data = response.get('data', b'')
                
                if len(command_data) >= 1:  # At least protocol version + null terminators
                    dv = DataView(command_data)
                    batt_level = dv.get_uint_8(0)
                    
                    retval.update({
                        'battery_level': batt_level})
        return retval

    async def get_status(self) -> Dict[str, Any]:
        """Get device status using HELLO command."""
        hello_command = bytearray([GSP_CMD_HELLO, 100])  # HELLO command with reference 100
        self.hello_ref = 100
        response = await self.send_command(hello_command)

        retval = {}
        if response.get('response_code') == GSP_RESP_COMMAND_RESPONSE:
            status_code = response.get('status_code', 0)
            if status_code == 200:  # HTTP OK
                # Parse HELLO response data
                command_data = response.get('command_data', b'')
                
                if len(command_data) >= 5:  # At least protocol version + null terminators
                    dv = DataView(command_data)
                    protocol_version = dv.get_uint_8(0)
                    
                    # Parse null-terminated strings
                    strings = command_data[1:].decode('utf-8', errors='ignore').split('\x00')
                    serial_number = strings[0] if len(strings) > 0 else ""
                    product_name = strings[1] if len(strings) > 1 else ""
                    dfu_mac = strings[2] if len(strings) > 2 else ""
                    app_name = strings[3] if len(strings) > 3 else ""
                    app_version = strings[4] if len(strings) > 4 else ""
                    retval.update({
                        'success': True,
                        'protocol_version': protocol_version,
                        'serial_number': serial_number,
                        'product_name': product_name,
                        'dfu_mac': dfu_mac,
                        'app_name': app_name,
                        'app_version': app_version
                    })
        dlstate_command = bytearray([GSP_CMD_GET, 101] + list("/Mem/DataLogger/State".encode()))  # GET command with reference 101
        dlstate_response = await self.send_command(dlstate_command)
    
        if dlstate_response.get('response_code') == GSP_RESP_COMMAND_RESPONSE:
            status_code = dlstate_response.get('status_code', 0)
            if status_code == 200:  # HTTP OK
                # Parse GET /Mem/DataLogger/State response data
                command_data = dlstate_response.get('command_data', b'')
                if len(command_data) >= 1:  # single byte
                    dv = DataView(command_data)
                    dlstate = dv.get_uint_8(0)

                    retval.update({
                        'dlstate': dlstate
                    })
            else:
                retval.update({
                    'success': False,
                    'error': f'GET DLSTATE command failed with status {status_code}',
                    'status_code': status_code
                })
        else:
            retval.update({
                'success': False,
                'error': f'Unexpected response code: {response.get("response_code")}'
            })
        return retval
    
    async def configure_device(self, config_data: bytearray) -> Dict[str, Any]:
        """Configure device using PUT_DATALOGGER_CONFIG command."""
        # config_data should be null-terminated UTF-8 strings of resource paths
        command = bytearray([GSP_CMD_PUT_DATALOGGER_CONFIG, 102]) + config_data
        self.logger.info(f"Configuring device with data: {config_data}")

        response = await self.send_command(command)
        
        if response.get('response_code') == GSP_RESP_COMMAND_RESPONSE:
            status_code = response.get('status_code', 0)
            return {
                'success': status_code == 200,
                'status_code': status_code,
                'raw_data': response.get('raw_data')
            }
        else:
            return {
                'success': False,
                'error': f'Unexpected response code: {response.get("response_code")}'
            }
    
    async def start_logging(self, log_id: int = 1) -> Dict[str, Any]:
        """Start data logging using PUT_DATALOGGER_STATE command."""
        # State 3 = start logging
        command = bytearray([GSP_CMD_PUT_DATALOGGER_STATE, 103, 3])
        response = await self.send_command(command)
        
        if response.get('response_code') == GSP_RESP_COMMAND_RESPONSE:
            status_code = response.get('status_code', 0)
            return {
                'success': status_code == 200,
                'status_code': status_code,
                'log_id': log_id,
                'raw_data': response.get('raw_data')
            }
        else:
            return {
                'success': False,
                'error': f'Unexpected response code: {response.get("response_code")}'
            }
    
    async def stop_logging(self) -> Dict[str, Any]:
        """Stop data logging using PUT_DATALOGGER_STATE command."""
        # State 2 = stop logging / Ready
        command = bytearray([GSP_CMD_PUT_DATALOGGER_STATE, 104, 2])
        response = await self.send_command(command)
        
        if response.get('response_code') == GSP_RESP_COMMAND_RESPONSE:
            status_code = response.get('status_code', 0)
            return {
                'success': status_code == 200,
                'status_code': status_code,
                'raw_data': response.get('raw_data')
            }
        else:
            return {
                'success': False,
                'error': f'Unexpected response code: {response.get("response_code")}'
            }
        
    def parse_logbook_entries(self, raw_data: bytes) -> list[dict]:
        """Parse logbook entries from raw data bytes."""
        entries = []
        entry_size = 16  
        header_size = 5
        
        if not raw_data:
            logging.debug("No raw data to parse")
            return entries
        
        logging.debug(f"Raw data length: {len(raw_data)} bytes")
        logging.debug(f"Raw data (hex): {raw_data.hex()}")

        if len(raw_data) < header_size:
            logging.warning(f"Raw data too short (less than {header_size} bytes)")
            return entries
        
        # Skip header and get actual entry data
        entry_data = raw_data[header_size:]
        logging.debug(f"Entry data length after header: {len(entry_data)} bytes")
        logging.debug(f"Entry data (hex): {entry_data.hex()}")
        
        if len(entry_data) % entry_size != 0:
            logging.warning(f"Entry data length ({len(entry_data)}) is not a multiple of {entry_size}")
            logging.warning(f"Remaining bytes: {len(entry_data) % entry_size}")

        offset = 0
        entry_index = 0
        while offset + entry_size <= len(entry_data):
            entry_bytes = entry_data[offset:offset + entry_size]
            
            # Parse fields
            entry_id = int.from_bytes(entry_bytes[0:4], byteorder='little', signed=False)
            last_modified = int.from_bytes(entry_bytes[4:8], byteorder='little', signed=False)
            size = int.from_bytes(entry_bytes[8:16], byteorder='little', signed=False)

            entry = {
                'id': entry_id,
                'last_modified': last_modified,
                'size': size
            }
            
            logging.debug(f"Entry {entry_index}: ID={entry_id}, Last Modified={last_modified}, Size={size}")
            logging.debug(f"  Raw bytes: {entry_bytes.hex()}")

            entries.append(entry)
            offset += entry_size
            entry_index += 1

            # Check if there are leftover bytes
        if offset < len(entry_data):
            leftover = entry_data[offset:]
            logging.warning(f"Leftover bytes at end: {leftover.hex()} ({len(leftover)} bytes)")
        
        logging.debug(f"Total entries parsed: {len(entries)}")
        return entries

    async def get_log_list(self) -> dict:
        """Get logbook entries from sensor."""
        command = bytearray([GSP_CMD_GET, 109]) + b'/Mem/Logbook/entries\x00'
        response = await self.send_command(command)
        
        if response.get('response_code') == GSP_RESP_COMMAND_RESPONSE:
            status_code = response.get('status_code', 0)
            raw_data = response.get('raw_data')
            
            if status_code == 200:
                try:
                    # Parse the logbook entries
                    entries = self.parse_logbook_entries(raw_data)
                    logging.debug(f"Parsed {len(entries)} logbook entries")
                    
                    return {
                        'success': True,
                        'status_code': status_code,
                        'entries': entries,
                        'raw_data': raw_data
                    }
                except Exception as e:
                    logging.error(f"Failed to parse logbook entries: {e}")
                    return {
                        'success': False,
                        'status_code': status_code,
                        'error': f'Parsing failed: {str(e)}',
                        'raw_data': raw_data
                    }
            else:
                return {
                    'success': False,
                    'status_code': status_code,
                    'raw_data': raw_data
                }
        else:
            return {
                'success': False,
                'error': f'Unexpected response code: {response.get("response_code")}'
            }
        
    async def fetch_data(self, log_id: int = 1, progress_callback=None, output_file: Optional[str] = None) -> Dict[str, Any]:
        """Fetch logged data using FETCH_LOG command."""
        if output_file:
            
            filename = f'{output_file}'
            if not filename.endswith('.sbem'):
                filename += '.sbem'
        else:
            filename = f'log_{log_id}_{self.device_name}.sbem'
        
        # Send FETCH_LOG command with log ID
        log_id_bytes = log_id.to_bytes(4, byteorder='little')
        command = bytearray([GSP_CMD_FETCH_LOG, 101]) + log_id_bytes
        
        try:
            # Send the fetch command
            init_response = await self.send_command(command)

            if init_response.get('response_code') != GSP_RESP_COMMAND_RESPONSE:
                return {
                    'success': False,
                    'error': f'Unexpected response to FETCH_LOG command: {init_response.get("response_code")}'
                }
            
            status_code = init_response.get('status_code', 0)
            if status_code != 200:
                return {
                    'success': False,
                    'status_code': status_code,
                    'error': f'FETCH_LOG command failed with status {status_code}'
                }
            
            # Now receive data packets
            dirname = os.path.dirname(filename)
            if dirname:
                os.makedirs(dirname, exist_ok=True)

            last_offset = 0
            missing_packet_detected = False
            with open(filename, 'wb') as f_log:
                while True:
                    try:
                        # Wait for data packets with timeout
                        data_response = await asyncio.wait_for(
                            self.response_queue.get(), 
                            timeout=30.0
                        )
                        
                        if data_response.get('response_code') in [GSP_RESP_DATA, GSP_RESP_DATA_PART2]:
                            data_payload = data_response.get('data_payload', b'')
                            
                            if len(data_payload) >= 4:
                                dv = DataView(data_payload)
                                offset = dv.get_uint_32(0)
                                bytes_to_write = data_payload[4:]
                                packet_size = len(bytes_to_write)

                                expected_offset = last_offset

                                if offset != expected_offset:
                                    missing_packet_detected = True
                                    self.logger.error( f"Packet loss detected Expected offset {expected_offset}, "
                                                        f"but received {offset}. Missing {offset - expected_offset} bytes."
                                                    )
                                last_offset = offset + packet_size

                                if progress_callback:
                                    progress_callback(offset + len(bytes_to_write))

                                if len(bytes_to_write) > 0:
                                    f_log.seek(offset)
                                    f_log.write(bytes_to_write)
                                else:
                                    # Empty data means end of log
                                    self.logger.info(f"End of log data received for log: {log_id}")
                                    # Wait a bit to ensure all data is processed
                                    await asyncio.sleep(1)
                                    break
                            else:
                                self.logger.warning("Received data packet too short")
                                break
                        
                    except asyncio.TimeoutError:
                        self.logger.warning("Data fetch timeout")
                        break

            if missing_packet_detected: 
                return {
                    'success': False,
                    'filename': filename,
                    'size': last_offset,
                    'log_id': log_id,
                    'error': 'Incomplete log data: one or more packets were missing during transfer'
                }
            
            return {
                'success': True,
                'filename': filename,
                'size': last_offset,
                'log_id': log_id
            }
            
        except Exception as e:
            self.logger.error(f"Data fetch failed:", exc_info=e)
            return {
                'success': False,
                'error': str(e)
            }
    
    async def subscribe_to_resource(self, resource_path: str, reference: int = 1) -> Dict[str, Any]:
        """Subscribe to a resource data stream using SUBSCRIBE command."""
        path_bytes = resource_path.encode('utf-8') + b'\x00'  # Null-terminated
        command = bytearray([GSP_CMD_SUBSCRIBE, reference]) + path_bytes
        response = await self.send_command(command)
        
        if response.get('response_code') == GSP_RESP_COMMAND_RESPONSE:
            status_code = response.get('status_code', 0)
            return {
                'success': status_code == 200,
                'status_code': status_code,
                'reference': reference,
                'resource_path': resource_path,
                'raw_data': response.get('raw_data')
            }
        else:
            return {
                'success': False,
                'error': f'Unexpected response code: {response.get("response_code")}'
            }
    
    async def unsubscribe_from_resource(self, reference: int) -> Dict[str, Any]:
        """Unsubscribe from a resource data stream using UNSUBSCRIBE command."""
        command = bytearray([GSP_CMD_UNSUBSCRIBE, reference])
        response = await self.send_command(command)
        
        if response.get('response_code') == GSP_RESP_COMMAND_RESPONSE:
            status_code = response.get('status_code', 0)
            return {
                'success': status_code == 200,
                'status_code': status_code,
                'reference': reference,
                'raw_data': response.get('raw_data')
            }
        else:
            return {
                'success': False,
                'error': f'Unexpected response code: {response.get("response_code")}'
            }
    
    async def get_resource(self, resource_path: str) -> Dict[str, Any]:
        """Get resource data using GET command."""
        path_bytes = resource_path.encode('utf-8') + b'\x00'  # Null-terminated
        command = bytearray([GSP_CMD_GET, 105]) + path_bytes
        response = await self.send_command(command)
        
        if response.get('response_code') == GSP_RESP_COMMAND_RESPONSE:
            status_code = response.get('status_code', 0)
            return {
                'success': status_code == 200,
                'status_code': status_code,
                'resource_path': resource_path,
                'data': response.get('command_data', b''),
                'raw_data': response.get('raw_data')
            }
        else:
            return {
                'success': False,
                'error': f'Unexpected response code: {response.get("response_code")}'
            }
    
    async def set_system_mode(self, system_mode: int) -> Dict[str, Any]:
        """Set system mode using PUT_SYSTEMMODE command."""
        command = bytearray([GSP_CMD_PUT_SYSTEMMODE, 107, system_mode])
        response = await self.send_command(command)
        
        if response.get('response_code') == GSP_RESP_COMMAND_RESPONSE:
            status_code = response.get('status_code', 0)
            return {
                'success': status_code == 202,
                'status_code': status_code,
                'system_mode': system_mode,
                'raw_data': response.get('raw_data')
            }
        else:
            return {
                'success': False,
                'error': f'Unexpected response code: {response.get("response_code")}'
            }
    
    async def set_utc_time(self, utc_time_us: int) -> Dict[str, Any]:
        """Set UTC time using PUT_UTCTIME command."""
        time_bytes = utc_time_us.to_bytes(8, byteorder='little')
        command = bytearray([GSP_CMD_PUT_UTCTIME, 108]) + time_bytes
        response = await self.send_command(command)
        
        if response.get('response_code') == GSP_RESP_COMMAND_RESPONSE:
            status_code = response.get('status_code', 0)
            return {
                'success': status_code == 200,
                'status_code': status_code,
                'utc_time': utc_time_us,
                'raw_data': response.get('raw_data')
            }
        else:
            return {
                'success': False,
                'error': f'Unexpected response code: {response.get("response_code")}'
            }
    
    async def erase_memory(self) -> Dict[str, Any]:
        """Erase device memory using CLEAR_LOGBOOK command."""
        command = bytearray([GSP_CMD_CLEAR_LOGBOOK, 106])  # CLEAR_LOGBOOK command
        response = await self.send_command(command)
        
        if response.get('response_code') == GSP_RESP_COMMAND_RESPONSE:
            status_code = response.get('status_code', 0)
            return {
                'success': status_code == 200,
                'status_code': status_code,
                'raw_data': response.get('raw_data')
            }
        else:
            return {
                'success': False,
                'error': f'Unexpected response code: {response.get("response_code")}'
            }
    
    async def execute_command(self, command_type: CommandType, **kwargs) -> Dict[str, Any]:
        """Execute a command by type with error handling."""
        try:
            if command_type == CommandType.HELLO:
                return await self.get_status()
            elif command_type == CommandType.PUT_DATALOGGER_CONFIG:
                config_data = kwargs.get('config_data', bytearray())
                return await self.configure_device(config_data)
            elif command_type == CommandType.PUT_DATALOGGER_STATE:
                # Check if we want to start or stop based on state parameter
                state = kwargs.get('state', 0)
                if state == 1:
                    return await self.start_logging(kwargs.get('log_id', 1))
                else:
                    return await self.stop_logging()
            elif command_type == CommandType.FETCH_LOG:
                log_id = kwargs.get('log_id', 1)
                output_file = kwargs.get('output_file')
                return await self.fetch_data(log_id, output_file)
            elif command_type == CommandType.CLEAR_LOGBOOK:
                return await self.erase_memory()
            elif command_type == CommandType.SUBSCRIBE:
                resource_path = kwargs.get('resource_path', '')
                return await self.subscribe_to_resource(resource_path)
            elif command_type == CommandType.UNSUBSCRIBE:
                reference = kwargs.get('reference', 0)
                return await self.unsubscribe_from_resource(reference)
            elif command_type == CommandType.GET:
                resource_path = kwargs.get('resource_path', '')
                return await self.get_resource(resource_path)
            elif command_type == CommandType.PUT_SYSTEMMODE:
                system_mode = kwargs.get('system_mode', 0)
                return await self.set_system_mode(system_mode)
            elif command_type == CommandType.PUT_UTCTIME:
                utc_time = kwargs.get('utc_time', 0)
                return await self.set_utc_time(utc_time)
            else:
                raise ValueError(f"Unknown command type: {command_type}")
                
        except Exception as e:
            self.logger.error(f"Command execution failed: {e}")
            return {
                'success': False,
                'error': str(e)
            }


async def run_sensor_command(end_of_serial: str, command_type: CommandType, **kwargs) -> Dict[str, Any]:
    """Convenience function to run a single command on a sensor using context manager."""
    try:
        async with SensorCommand(end_of_serial) as sensor:
            result = await sensor.execute_command(command_type, **kwargs)
            return result
        
    except Exception as e:
        return {'success': False, 'error': str(e)}


if __name__ == "__main__":
    print("This module is intended to be imported and used within other scripts.")
    exit(1)