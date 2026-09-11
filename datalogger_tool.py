import argparse
from datetime import datetime
import sys
import asyncio
import logging
import time
from sensor_command import DL_STATES, SensorCommand, CommandType, GSP_CMD_HELLO, GSP_CMD_PUT_DATALOGGER_CONFIG, GSP_CMD_PUT_DATALOGGER_STATE, GSP_CMD_FETCH_LOG, GSP_CMD_CLEAR_LOGBOOK

async def fetch_status(serial, args):
    """Fetch status from a specific device."""
    try:
        async with SensorCommand(serial) as sensor:
            result = await sensor.get_status()
            return result
    except Exception as e:
        return {'success':False, 'error':str(e)}
    
async def fetch_status_and_battery(serial, args):
    """Fetch status from a specific device."""
    try:
        async with SensorCommand(serial) as sensor:
            result = await sensor.get_status()

            result.update(await sensor.get_battery_state())
            result.update(await sensor.get_battery_level())

            logs = await sensor.get_log_list()
            
            if logs.get('success'):
                entries = logs.get('entries', [])

                result['log_count'] = len(entries)
                result['has_logs'] = len(entries) > 0

                logging.info(f"Found {len(entries)} logs in logbook for device {serial}")

            else:
                result['log_count'] = 0
                result['has_logs'] = False

            return result
        
    except Exception as e:
        logging.exception('Failed to fetch status')

        logging.info(f"Exception type: {type(e)}")
        logging.info(f"Exception name: {type(e).__name__}")
        logging.info(f"Exception text: {e}")

        return {'success':False, 'error':str(e), 'exception_type':type(e).__name__}

async def configure_device(serial, args = None, paths = None):
    """Configure a specific device."""
    try:
        async with SensorCommand(serial) as sensor:
            # Use config file if provided
            config_data = bytearray()  # Default empty config

            if not paths and hasattr(args, 'path') and args.path:
                paths = args.path
        
            paths.append("/Time/Detailed")
            for path in paths:
                logging.info(f"- Adding path {path} to DataLogger configuration")
                config_data.extend(path.encode('utf-8') + b'\0')

            result = await sensor.configure_device(config_data)
            return result
    except Exception as e:
        return {'success':False, 'error':str(e)}

async def start_logging(serial, args):
    """Start logging on a specific device."""
    try:
        async with SensorCommand(serial) as sensor:
            result = await sensor.start_logging()
            status = await sensor.get_status()
            logging.info(f"Sensor state after start: {status}")
            return result
    except Exception as e:
        return {'success':False, 'error':str(e)}

async def stop_logging(serial, args):
    """Stop logging and boot sensor to start a new log."""
    try:
        async with SensorCommand(serial) as sensor:
            # First, stop the current logging session to flush data to file
            logging.info(f"Stopping logging for device {serial}...")
            stop_result = await sensor.stop_logging()
            logging.info(f"Stop result: success={stop_result.get('success')}, status_code={stop_result.get('status_code')}")
            if not stop_result.get('success'):
                return stop_result
            
            # Then boot the sensor (system_mode=5) to start a new log
            logging.info(f"Booting sensor {serial} with system_mode=5...")
            boot_result = await sensor.set_system_mode(5)
            logging.info(f"Boot result: success={boot_result.get('success')}, status_code={boot_result.get('status_code')}")

            # Accept status 202 (Accepted) as success for boot command
            boot_status = boot_result.get('status_code', 0)
            if boot_status in [200, 202]:
                boot_result['success'] = True
                logging.info(f"Boot command accepted with status {boot_status}")

            await sensor.disconnect()
            logging.info(f"Waiting 4 seconds for sensor {serial} to complete boot...")
            await asyncio.sleep(4)
            logging.info(f"Boot complete for device {serial}")

            return boot_result
            
    except Exception as e:
        return {'success':False, 'error':str(e)}

async def fetch_data(serial, args, output_dir=None, progress_callback=None):
    """Fetch data from a specific device."""
    fetched_files = []
    try:
        async with SensorCommand(serial, set_time=False) as sensor:
            # Dictionary to store all discovered logs
            discovered_logs = {}
            
            # Step 1: Get first batch of logs (max 4) from logbook
            logs = await sensor.get_log_list()
            
            if logs.get('success'):
                entries = logs.get('entries', [])
                logging.info(f"Found {len(entries)} logs in logbook for device {serial}")
                
                # Store logs from logbook with known sizes
                for entry in entries:
                    discovered_logs[entry['id']] = {
                        'size': entry.get('size', 0),
                        'last_modified': entry.get('last_modified', 0),
                        'has_known_size': True
                    }
            else:
                logging.warning(f"Failed to get logbook entries: {logs.get('error')}")
            
            # Step 2: Try to discover additional logs by attempting to fetch them
            # Start from the highest known log ID + 1, or from 1 if no logs found
            if discovered_logs:
                next_log_id = max(discovered_logs.keys()) + 1
            else:
                next_log_id = 1
            
            # Try to find more logs 
            while True:
                logging.debug(f"Probing for log ID {next_log_id}")
                
                # Try to fetch this log
                probe_result = await sensor.fetch_data(log_id=next_log_id, output_file=None, progress_callback=None)
                
                if probe_result.get('success'):
                    # Log exists! Add to discovered logs
                    discovered_logs[next_log_id] = {
                        'size': probe_result.get('size', 0),
                        'last_modified': 0,  # Unknown
                        'has_known_size': False  # Discovered by probing
                    }
                    logging.info(f"Discovered additional log ID {next_log_id} (size: {probe_result.get('size', 0)} bytes)")
                    next_log_id += 1
                elif probe_result.get('status_code') == 404:
                    # Log not found - no more logs exist
                    logging.info(f"No log found at ID {next_log_id} (404), stopping probe")
                    break
                else:
                    # No more logs found
                    logging.debug(f"No log found at ID {next_log_id}, stopping probe")
                    break
            
            # Now fetch all discovered logs
            total_logs = len(discovered_logs)
            logging.info(f"Total logs to fetch: {total_logs}")
            
            # Calculate total bytes across all files
            file_sizes = [log_info['size'] for log_info in discovered_logs.values()]
            total_bytes = sum(file_sizes)
            bytes_downloaded_so_far = 0
            
            # Fetch each log
            for idx, (log_id, log_info) in enumerate(sorted(discovered_logs.items()), 1):
                start_time = datetime.now()
                logging.info(f"Fetching log {log_id} from device {serial} ({idx}/{total_logs})")
                logging.info(f"=== FETCHING log_id={log_id}, size={log_info['size']} ===")
                
                output_file = None
                full_serial = sensor.device_name
                if output_dir:
                    output_file = f"{output_dir}/Movesense_log_{log_id}_{full_serial}.sbem"
                elif hasattr(args, 'output') and args.output:
                    output_file = f"{args.output}/Movesense_log_{log_id}_{full_serial}.sbem"
                
                total_size = log_info['size']
                has_known_size = log_info['has_known_size']
                
                def progress_callback_inner(count):
                    if progress_callback:
                        # Only update progress bar if we know the size beforehand
                        #logging.debug(f"Progress callback: bytes={count}, log_id={log_id}, total_size={total_size}, idx={idx}, total_logs={total_logs}")
                        progress_callback(count, log_id, total_size, idx, total_logs, 
                                        file_sizes=file_sizes, 
                                        bytes_downloaded_so_far=bytes_downloaded_so_far, 
                                        total_bytes=total_bytes,
                                        has_known_size=has_known_size)
                
                result = await sensor.fetch_data(
                    log_id=log_id, 
                    output_file=output_file, 
                    progress_callback=progress_callback_inner
                )

                logging.info(f"=== RESULT for log_id={log_id}: success={result.get('success')} ===")

                if not result.get('success', False):
                    logging.warning(f"Failed to fetch log {log_id}")
                    logging.error(f"FAILED: {result}")
                    if 'status_code' in result and result['status_code'] != 404:
                        logging.error(f"Status {result['status_code']}: {result.get('error', 'Unknown error')}")
                else:
                    end_time = datetime.now()
                    duration = (end_time - start_time).total_seconds()
                    size_kb = result.get('size', 0) / 1024
                    speed = size_kb / duration if duration > 0 else 0
                    
                    if has_known_size:
                        logging.info(f"Fetched log {log_id}, size {size_kb:.2f} kB in {duration:.2f} seconds. speed: {speed:.2f} kB/s")
                    else:
                        logging.info(f"Fetched log {log_id} (discovered), size {size_kb:.2f} kB in {duration:.2f} seconds. speed: {speed:.2f} kB/s")

                    filename = result.get('filename')
                    if filename:
                        fetched_files.append(filename)

                    bytes_downloaded_so_far += total_size

            # Reset sensor to avoid the 409 error on Sensor firmware <= 2.3.1
            logging.info(f"Resetting device {serial} to system mode <5>")
            await sensor.set_system_mode(5)

            await sensor.disconnect()

            return {'success': True, 'files_fetched': fetched_files, 'total_logs': total_logs}

    except Exception as e:
        logging.error(f"Error fetching data: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}

async def erase_memory(serial):
    """Erase memory on a specific device."""
    try:
        async with SensorCommand(serial, set_time=False) as sensor:
            result = await sensor.erase_memory()
            return result
    except Exception as e:
        return {'success': False, 'error': str(e)}
    
async def get_battery_level(serial):
    """Get battery level from a specific device."""
    try:
        async with SensorCommand(serial, set_time=False) as sensor:
            result = await sensor.get_resource("/System/Energy/Level")
            logging.info(f"Battery level result: {result}")
            if result.get('success'):
                data = result.get('data', b'')
                if len(data) >= 1:
                    from sensor_command import DataView
                    level = DataView(data).get_uint_8(0)
                    result['battery_level'] = level
            return result
    except Exception as e:
        return {'success': False, 'error': str(e)}

def run_async_command(coro):
    """Run an async command, handling both sync and async contexts."""
    try:
        # Check if we're already in an event loop
        loop = asyncio.get_running_loop()
        # If we are, create a task and run it
        task = asyncio.create_task(coro)
        return loop.run_until_complete(task)
    except RuntimeError:
        # No event loop running, safe to use asyncio.run
        return asyncio.run(coro)

#!/usr/bin/env python3


def status_command(serial, args):
    """Check device status synchronously using HELLO command."""
    print(f"Checking status for device: {serial}")
    print(f"Device {serial}: Connecting...")
    try:
        status = run_async_command(fetch_status(serial, args))
        if status.get('success', False):
            print(f"Device {serial} status: OK")
            print(f"  Protocol version: {status.get('protocol_version', 'Unknown')}")
            print(f"  Serial number: {status.get('serial_number', 'Unknown')}")
            print(f"  Product name: {status.get('product_name', 'Unknown')}")
            print(f"  App name: {status.get('app_name', 'Unknown')}")
            print(f"  App version: {status.get('app_version', 'Unknown')}")
            print(f"  DataLogger state: {DL_STATES[status.get('dlstate', 1)]}")
        else:
            print(f"Device {serial} error: {status.get('error', 'Unknown error')}")
    except Exception as e:
        print(f"Device {serial} failed: {e}")

def config_command(serial, args) -> bool:
    """Configure devices synchronously using PUT_DATALOGGER_CONFIG command."""
    print(f"Configuring device {serial}...")
    try:
        config_result = run_async_command(configure_device(serial, args))
        result = config_result.get('success', False)
        if result:
            print(f"Device {serial} configured successfully")
        else:
            print(f"Device {serial} configuration failed: {config_result.get('error', 'Unknown error')}")
        return result
    except Exception as e:
        print(f"Device {serial} configuration error: {e}")
        return False

def start_command(serial, args) -> bool:
    """Start logging on devices synchronously using PUT_DATALOGGER_STATE command."""
    print(f"Starting logging for device {serial}...")
    try:
        start_result = run_async_command(start_logging(serial, args))
        result = start_result.get('success', False)
        if result:
            print(f"Device {serial} logging started successfully")
        else:
            print(f"Device {serial} start failed: {start_result.get('error', 'Unknown error')}")
        return result
    except Exception as e:
        print(f"Device {serial} start error: {e}")
        return False

def stop_command(serial, args) -> bool:
    """Stop logging on devices synchronously using PUT_DATALOGGER_STATE command."""
    print(f"Stopping logging for device {serial}...")
    try:
        stop_result = run_async_command(stop_logging(serial, args))
        result = stop_result.get('success', False)
        if result:
            print(f"Device {serial} logging stopped successfully")
        else:
            print(f"Device {serial} stop failed: {stop_result.get('error', 'Unknown error')}")
        return result
    except Exception as e:
        print(f"Device {serial} stop error: {e}")
        return False

def fetch_command(serial, args) -> bool:
    """Fetch data from devices synchronously using FETCH_LOG command."""
    print(f"Fetching data from device {serial}...")
    try:
        fetch_result = run_async_command(fetch_data(serial, args))
        result = fetch_result.get('success', False)
        if result:
            filename = fetch_result.get('filename', 'unknown')
            print(f"Device {serial} data saved to: {filename}")
        else:
            print(f"Device {serial} fetch failed: {fetch_result.get('error', 'Unknown error')}")
        return result
    except Exception as e:
        print(f"Device {serial} fetch error: {e}")
        return False

def erasemem_command(serial, args) -> bool:
    """Erase memory on devices synchronously using CLEAR_LOGBOOK command."""
    print(f"Erasing memory for device {serial}...")

    # Check if force flag is provided
    force = getattr(args, 'force', False)

    if not force:
        # Interactive confirmation
        print(f"\n⚠️  WARNING: This will erase ALL logged data from device {serial}!")
        print("This action cannot be undone.")
        response = input(f"Are you sure you want to erase memory on device {serial}? Type 'yes' or 'y' to confirm: ").strip().lower()
        if response.lower() not in ['yes', 'y']:
            print(f"Memory erase cancelled for device {serial}")
            return True # consider cancelled as success => no retry needed
        
    print(f"Erasing memory for device {serial}...")
    try:
        erase_result = run_async_command(erase_memory(serial))
        result = erase_result.get('success', False)
        if result:
            print(f"Device {serial} memory erased successfully")
        else:
            print(f"Device {serial} erase failed: {erase_result.get('error', 'Unknown error')}")
        return result
    except Exception as e:
        print(f"Device {serial} erase error: {e}")
        return False

def all_sensors_command(command_func, args, retrys=10):
    retry_count = 0
    serials = list(args.serial_numbers)
    while retry_count <= retrys:
        next_serials = []
        for serial in serials:
            print(f"Executing command for device {serial}...")
            try:
                result = command_func(serial, args)
                if not result:
                    next_serials.append(serial)
            except Exception as e:
                logging.warning(f"Error executing command for device {serial}: {e}")
                next_serials.append(serial)

        retry_count += 1
        serials = next_serials
        if not serials or len(serials) == 0:
            break
        print(f"Retrying for sensors {','.join(serials)} in 5 seconds. attempt {retry_count}/{retrys}")
        time.sleep(5)  # wait a bit before retrying    

def main():
    # Setup logging
    logging.basicConfig(
        level=logging.WARNING, 
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    parser = argparse.ArgumentParser(description='Datalogger Tool Command Line Interface')
    parser.add_argument('-V', '--verbose', action='store_true', help='Enable verbose logging')
    
    subparsers = parser.add_subparsers(dest='command', help='Commands')

    # Status command
    status_parser = subparsers.add_parser('status', help='Check device status')
    status_parser.add_argument('-s', '--serial_numbers', nargs='+', help='List of serial numbers. separate multiple serial numbers with space.')

    # Config command
    config_parser = subparsers.add_parser('config', help='Configure datalogger', description='Configure datalogger settings')
    config_parser.add_argument('-p', '--path', nargs='+', help='Resource paths to add to configuration. separate multiple paths with space.')
    config_parser.add_argument('-s', '--serial_numbers', nargs='+', help='List of serial numbers. separate multiple serial numbers with space.')

    # Start command
    start_parser = subparsers.add_parser('start', help='Start logging')
    start_parser.add_argument('-s', '--serial_numbers', nargs='+', help='List of serial numbers. separate multiple serial numbers with space.')

    # Stop command
    stop_parser = subparsers.add_parser('stop', help='Stop logging')
    stop_parser.add_argument('-s', '--serial_numbers', nargs='+', help='List of serial numbers. separate multiple serial numbers with space.')

    # Fetch command
    fetch_parser = subparsers.add_parser('fetch', help='Fetch data')
    fetch_parser.add_argument('-o', '--output', help='Output directory')
    fetch_parser.add_argument('-s', '--serial_numbers', nargs='+', help='List of serial numbers. separate multiple serial numbers with space.')

    # Erasemem command
    erasemem_parser = subparsers.add_parser('erasemem', help='Erase device memory')
    erasemem_parser.add_argument('--force', action='store_true', help='Force erase without confirmation')
    erasemem_parser.add_argument('-s', '--serial_numbers', nargs='+', help='List of serial numbers. separate multiple serial numbers with space.')

    args = parser.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)
        logging.debug("args: " + str(args))

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    # Command dispatch
    commands = {
        'status': status_command,
        'config': config_command,
        'start': start_command,
        'stop': stop_command,
        'fetch': fetch_command,
        'erasemem': erasemem_command
    }

    if args.command == 'status':
        retrys = 0
    else:
        retrys = 10

    all_sensors_command(commands[args.command], args, retrys)

if __name__ == '__main__':
    main()