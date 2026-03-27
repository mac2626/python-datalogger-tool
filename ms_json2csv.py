import asyncio
import sys
import json
import os
import io
import csv
import logging
from datetime import datetime

import aiofiles

log = logging.getLogger(__name__)

ECG_LSB_TO_MV = 0.000381469726563

async def convert_json_to_csv(input_file, output_file, progress_callback = None):
    """
    Convert a JSON file containing sensor data to CSV format.
    Works for ECG, ACC, GYRO, TEMP, MeasIMU6, MeasIMU9, and similar Meas* streams.
    """
    if progress_callback:
        progress_callback(1, 5, "Parsing JSON")  # Initial progress callback for parsing step
    async with aiofiles.open(input_file, 'r') as f:
        #print("Parsing JSON...")
        log.debug("Parsing JSON...")
        strcontent = await f.read()
        content = json.loads(strcontent)

    if progress_callback:
        progress_callback(2, 5, "Parsing JSON")  # Initial progress callback for parsing step

    samples = content.get("Samples", [])
    #print(f"Total sample entries: {len(samples)}")
    log.debug(f"Total sample entries: {len(samples)}")

    # Group samples by stream name
    sample_streams = {}
    for sample in samples:
        sample_type = list(sample.keys())[0]
        if sample_type == "TimeDetailed":
            continue
        sample_streams.setdefault(sample_type, []).append(sample[sample_type])

    #print(f"Streams found: {list(sample_streams.keys())}")
    log.debug(f"Streams found: {list(sample_streams.keys())}")

    # Extract time reference (if available)
    time_detailed = next((s["TimeDetailed"] for s in samples if "TimeDetailed" in s), {})
    relative_time = time_detailed.get("relativeTime", 0)
    utc_time = time_detailed.get("utcTime", 0)
    try:
        relative_time = int(relative_time) / 1000
    except Exception:
        relative_time = 0
    try:
        utc_time_str = datetime.utcfromtimestamp(int(utc_time) / 1_000_000).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except Exception:
        utc_time_str = "N/A"

    # Process each stream
    for stream_name, entries in sample_streams.items():
        #print(f"\n{'='*60}\nProcessing stream: {stream_name}")
        log.debug(f"Processing stream: {stream_name}")
        
        # For IMU6/IMU9, we need to split into separate CSV files per sensor type
        if stream_name in ["MeasIMU6", "MeasIMU9"]:
            await process_imu_stream(stream_name, entries, output_file, relative_time, utc_time_str)
        elif stream_name == "MeasHR":
            await process_hr_stream(stream_name, entries, output_file, relative_time, utc_time_str)
        else:
            await process_regular_stream(stream_name, entries, output_file, relative_time, utc_time_str, progress_callback)

def get_missing_value(stream_name):
    """Return appropriate missing value based on stream type."""
    if "ECG" in stream_name.upper():
        return -1.5  # -1.5mV for ECG
    else:
        return 0.0  # Default

def detect_missing_chunks(entries, stream_name, tolerance=1.5):
    """
    Detect missing chunks by analyzing timestamp gaps between consecutive chunks.
    Uses the first two chunks to establish expected chunk interval.
    
    Returns:
        tuple: (missing_chunks list, expected_chunk_dt)
    """
    if len(entries) < 2:
        return [], 0

    timestamps = []
    for entry in entries[:10]:
        ts = entry.get("Timestamp") or entry.get("timestamp")
        if ts is not None:
            timestamps.append(ts)
        if len(timestamps) >= 2:
            break

    if len(timestamps) < 2:
        #print(f"  Warning: Cannot determine chunk interval for {stream_name}")
        log.warning(f"Cannot determine chunk interval for {stream_name}")
        return [], 0
    
    expected_chunk_dt = timestamps[1] - timestamps[0]
    #print(f"  Expected chunk interval for {stream_name}: {expected_chunk_dt} ms")
    log.debug(f"Expected chunk interval for {stream_name}: {expected_chunk_dt} ms")

    missing_chunks = []
    
    # Scan through all chunks to find gaps 
    for i in range(len(entries) - 1):
        current_ts = entries[i].get("Timestamp") or entries[i].get("timestamp")
        next_ts = entries[i + 1].get("Timestamp") or entries[i + 1].get("timestamp")

        if current_ts is None or next_ts is None:
            continue

        gap = next_ts - current_ts

        # If gap is significantly larger than expected, we have missing chunks
        if gap > expected_chunk_dt * tolerance:
            num_missing = int(round((gap - expected_chunk_dt) / expected_chunk_dt))
            #print(f"  Missing chunk(s) detected between index {i} and {i+1}")
            #print(f"    Current ts: {current_ts}, Next ts: {next_ts}, Gap: {gap:.1f}ms")
            #print(f"    Expected interval: {expected_chunk_dt:.1f}ms, Missing chunks: {num_missing}")
            log.debug(f"Missing chunk(s) detected between index {i} and {i+1}")
            log.debug(f"  Current ts: {current_ts}, Next ts: {next_ts}, Gap: {gap:.1f}ms")
            log.debug(f"  Expected interval: {expected_chunk_dt:.1f}ms, Missing chunks: {num_missing}")

            # Store info about where to insert and how many
            for j in range(1, num_missing + 1):
                expected_ts = int(current_ts + j * expected_chunk_dt)
                missing_chunks.append((i, expected_ts))
    
    return missing_chunks


async def process_hr_stream(stream_name, entries, output_file, relative_time, utc_time_str):
    """Process heart rate (MeasHR) streams with RR intervals."""
    
    all_data = []
    cumulative_time = 0  # Track cumulative time based on RR intervals
    
    for idx, entry in enumerate(entries):
        average = entry.get("average", 0)
        rr_data = entry.get("rrData", [])
        
        # Each RR interval is in milliseconds
        for rr_ms in rr_data:
            all_data.append({
                "timestamp_ms": cumulative_time,
                "average_hr": average,
                "rr_interval_ms": rr_ms
            })
            cumulative_time += rr_ms
    
    if not all_data:
        #print(f"No valid samples found for {stream_name}, skipping.")
        log.warning(f"No valid samples found for {stream_name}, skipping.")
        return
    
    base, _ = os.path.splitext(output_file)
    stream_output = f"{base}_{stream_name}.csv"
    
    #print(f"Writing {len(all_data)} samples to {stream_output}")
    log.debug(f"Writing {len(all_data)} samples to {stream_output}")
    with open(stream_output, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        
        header = ["Timestamp_ms", "Average_HR", "RR_Interval_ms", "RelativeTime", relative_time, "UTC", utc_time_str]
        writer.writerow(header)
        
        for sample in all_data:
            writer.writerow([
                sample["timestamp_ms"],
                f"{sample['average_hr']:.2f}",
                sample["rr_interval_ms"]
            ])
    
    #print(f"Saved {len(all_data)} samples to {stream_output}")
    log.debug(f"Saved {len(all_data)} samples to {stream_output}")

async def process_imu_stream(stream_name, entries, output_file, relative_time, utc_time_str):
    """Process IMU6/IMU9 streams which contain multiple sensor arrays."""

    if len(entries) < 2:
        #print(f"Warning: Not enough entries in {stream_name} to estimate sampling interval.")
        log.warning(f"Not enough entries in {stream_name} to estimate sampling interval.")
        return

    # --- Determine dt from the first two timestamps ---
    first_ts = entries[0].get("Timestamp") or entries[0].get("timestamp")
    second_ts = None
    for entry in entries[1:]:
        ts = entry.get("Timestamp") or entry.get("timestamp")
        if ts is not None:
            second_ts = ts
            break

    if first_ts is None or second_ts is None or second_ts <= first_ts:
        #print(f"Warning: Cannot determine sample interval for {stream_name}")
        log.warning(f"Cannot determine sample interval for {stream_name}")
        return

    expected_chunk_dt = second_ts - first_ts
    #print(f"  Estimated chunk-to-chunk interval for {stream_name}: {expected_chunk_dt:.3f} ms")
    log.debug(f"Estimated chunk-to-chunk interval for {stream_name}: {expected_chunk_dt:.3f} ms")

    # Try to estimate per-sample dt assuming similar sample counts in chunks
    first_chunk = entries[0]
    sample_counts = []
    for v in first_chunk.values():
        if isinstance(v, list):
            sample_counts.append(len(v))
    samples_per_chunk = max(sample_counts) if sample_counts else 1

    dt = expected_chunk_dt / samples_per_chunk
    freq_hz = 1000.0 / dt
    #print(f"  Estimated sample interval: {dt:.4f} ms  ->  {freq_hz:.2f} Hz")
    log.debug(f"Estimated sample interval: {dt:.4f} ms  ->  {freq_hz:.2f} Hz, row 210")
    
    # Collect data by sensor type (Acc, Gyro, Magn)
    sensor_data = {}
    
    for chunk_idx, entry in enumerate(entries):
        timestamp = entry.get("Timestamp") or entry.get("timestamp")
        
        if timestamp is None:
            #print(f"Warning: chunk {chunk_idx} missing Timestamp, skipping")
            log.warning(f"Warning: chunk {chunk_idx} missing Timestamp, skipping")
            continue
        
        # Find all array fields (ArrayAcc, ArrayGyro, ArrayMagn)
        for key, value in entry.items():
            if key.lower() == "timestamp":
                continue
            
            # Extract sensor type from key (e.g., ArrayAcc -> Acc)
            if key.startswith("Array"):
                sensor_type = key[5:]  # Remove "Array" prefix
            else:
                sensor_type = key
            
            if not isinstance(value, list):
                continue
            
            if sensor_type not in sensor_data:
                sensor_data[sensor_type] = []
            
            # Add timestamped samples
            for i, sample in enumerate(value):
                ts = int(timestamp + i * dt)
                if isinstance(sample, dict) and all(k in sample for k in ("x", "y", "z")):
                    sensor_data[sensor_type].append((ts, [sample["x"], sample["y"], sample["z"]]))
                else:
                    sensor_data[sensor_type].append((ts, sample))
    
    # Write separate CSV for each sensor type
    base, _ = os.path.splitext(output_file)
    for sensor_type, data in sensor_data.items():
        if not data:
            continue
        
        data.sort(key=lambda x: x[0])
        stream_output = f"{base}_{stream_name}_{sensor_type}.csv"
        
        #print(f"Writing {len(data)} samples to {stream_output}")
        log.debug(f"Writing {len(data)} samples to {stream_output}")
        async with aiofiles.open(stream_output, "w", newline="") as csvfile:
            first_val = data[0][1]
            if isinstance(first_val, list) and len(first_val) == 3:
                header = ["Timestamp_ms", "X", "Y", "Z", "RelativeTime", relative_time, "UTC", utc_time_str]
            else:
                header = ["Timestamp_ms", "Value", "RelativeTime", relative_time, "UTC", utc_time_str]
            # Write header via buffer 
            buf = io.StringIO()
            csv.writer(buf).writerow(header)
            await csvfile.write(buf.getvalue())
            # Write data rows in buffered chunks
            chunk_size = 1000
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            for i, (ts, val) in enumerate(data):
                if isinstance(val, list):
                    writer.writerow([ts] + [f"{v:.6f}" for v in val])
                else:
                    writer.writerow([ts, f"{val:.6f}"])
                if (i + 1) % chunk_size == 0:
                    await csvfile.write(buffer.getvalue())
                    buffer = io.StringIO()
                    writer = csv.writer(buffer)
            remaining = buffer.getvalue()
            if remaining:
                await csvfile.write(remaining)
        
        #print(f"Saved {len(data)} samples to {stream_output}")
        log.debug(f"Saved {len(data)} samples to {stream_output}")

async def process_regular_stream(stream_name, entries, output_file, relative_time, utc_time_str, progress_callback=None):
    """Process regular (non-IMU) data streams, detect sample-level gaps, and fill missing areas with -1.5mV."""

    if not entries:
        #print(f"No entries for {stream_name}, skipping.")
        log.warning(f"No entries for {stream_name}, skipping.")
        return

    all_data = []
    prev_dt = None  # Sample interval (ms)
    # log.debug(f"Processing stream: {stream_name}")
    # Flatten all chunks into (timestamp, value) pairs ---
    for chunk_idx, entry in enumerate(entries):
        # Extract timestamp
        timestamp = entry.get("Timestamp") or entry.get("timestamp")
        if timestamp is None:
            for v in entry.values():
                if isinstance(v, dict) and "Timestamp" in v:
                    timestamp = v["Timestamp"]
                    entry = v
                    break

        if timestamp is None:
            #print(f"Chunk {chunk_idx} missing timestamp, skipping.")
            log.warning(f"Chunk {chunk_idx} missing timestamp, skipping.")
            continue

        # --- Identify data field ---
        data_keys = [k for k in entry.keys() if k.lower() != "timestamp"]
        if not data_keys:
            #print(f"No data field in chunk {chunk_idx}.")
            log.warning(f"No data field in chunk {chunk_idx}.")
            continue

        preferred_fields = ["Samples", "Measurement", "ArrayAcc", "ArrayGyro", "ArrayMag"]
        data_key = next((k for k in preferred_fields if k in entry), data_keys[0])
        data_array = entry[data_key]

        # --- Normalize into list of numeric or [x,y,z] samples ---
        if isinstance(data_array, (int, float)):
            values = [data_array]
        elif isinstance(data_array, dict):
            if all(isinstance(v, (int, float)) for v in data_array.values()):
                values = [[data_array.get("x", 0), data_array.get("y", 0), data_array.get("z", 0)]]
            else:
                values = []
                for v in data_array.values():
                    if isinstance(v, list):
                        values.extend(v)
        elif isinstance(data_array, list):
            if len(data_array) > 0 and isinstance(data_array[0], dict) and all(k in data_array[0] for k in ("x", "y", "z")):
                values = [[i["x"], i["y"], i["z"]] for i in data_array]
            else:
                values = data_array
        else:
            #print(f" Unsupported data format in chunk {chunk_idx}, skipping.")
            log.warning(f" Unsupported data format in chunk {chunk_idx}, skipping.")
            continue

        n = len(values)
        if n == 0:
            continue

        # --- Estimate dt (per-sample interval) if not yet known ---
        # log.debug(f"[chunk {chunk_idx}] prev_dt={prev_dt}, checking if dt estimation needed...")
        if prev_dt is None and chunk_idx + 1 < len(entries):
            next_ts = entries[chunk_idx + 1].get("Timestamp") or entries[chunk_idx + 1].get("timestamp")
            log.debug(f"[chunk {chunk_idx}] next_ts={next_ts} (from chunk {chunk_idx + 1})")
            if next_ts and next_ts > timestamp:
                prev_dt = (next_ts - timestamp) / n
                #print(f" Estimated sample interval: {prev_dt:.3f} ms ({1000/prev_dt:.2f} Hz)")
                #log.debug(f" Estimated sample interval: {prev_dt:.3f} ms ({1000/prev_dt:.2f} Hz, row 349)")
                log.debug(f"[chunk {chunk_idx}] dt estimated: ({next_ts} - {timestamp}) / {n} = {prev_dt:.3f} ms ({1000/prev_dt:.2f} Hz)")
            else:
                prev_dt = 5.0  # fallback
                log.debug(f"[chunk {chunk_idx}] next_ts invalid or not > timestamp -> fallback prev_dt=5.0 ms")
        elif prev_dt is None:
            prev_dt = 5.0
            log.debug(f"[chunk {chunk_idx}] no next chunk available -> fallback prev_dt=5.0 ms")
        else:
            # log.debug(f"[chunk {chunk_idx}] prev_dt already set to {prev_dt:.3f} ms, skipping estimation")
            pass
            
        # --- Append all samples with estimated timestamps ---
        # log.debug(f"[chunk {chunk_idx}] appending {n} samples starting at ts={timestamp}, prev_dt={prev_dt:.3f}")
        # for i, val in enumerate(values):
        #     ts = int(timestamp + i * prev_dt)
        #     all_data.append((ts, val))
        # log.debug(f"[chunk {chunk_idx}] all_data size now={len(all_data)}")
        # PRE-LOOP: log everything we are about to iterate over
        # log.debug(f"[chunk {chunk_idx}][PRE-LOOP] n={n}, timestamp={timestamp!r} (type={type(timestamp).__name__}), prev_dt={prev_dt!r} (type={type(prev_dt).__name__})")
        # log.debug(f"[chunk {chunk_idx}][PRE-LOOP] values type={type(values).__name__}, len={len(values)}, first_val={values[0]!r} (type={type(values[0]).__name__}), last_val={values[-1]!r} (type={type(values[-1]).__name__})")
        try:
            for i, val in enumerate(values):
                # INSIDE-LOOP: log every iteration
                # log.debug(f"[chunk {chunk_idx}][LOOP i={i}] val={val!r} (type={type(val).__name__}), ts_raw={timestamp + i * prev_dt!r}, ts_int={int(timestamp + i * prev_dt)}")
                ts = int(timestamp + i * prev_dt)
                all_data.append((ts, val))
        except Exception as e:
            # EXCEPTION: log exactly where and what crashed
            log.error(f"[chunk {chunk_idx}][LOOP-CRASH] crashed at i={i}, val={val!r} (type={type(val).__name__}), timestamp={timestamp!r}, prev_dt={prev_dt!r}, error={type(e).__name__}: {e}")
            raise
        # log.debug(f"[chunk {chunk_idx}][POST-LOOP] all_data size now={len(all_data)}")

    log.debug(f"[process_regular_stream] chunk loop done, all_data size={len(all_data)}")

    if not all_data:
        #print(f"No valid samples found for {stream_name}, skipping.")
        log.warning(f"No valid samples found for {stream_name}, skipping.")
        return

    # --- Sort samples by timestamp ---
    log.debug(f"[process_regular_stream] sorting {len(all_data)} samples by timestamp...")
    all_data.sort(key=lambda x: x[0])
    log.debug(f"[process_regular_stream] sort done. ts range: {all_data[0][0]} -> {all_data[-1][0]}")

    # --- Fill missing gaps ---
    filled_data = []
    is_ecg = "ECG" in stream_name.upper()
    log.debug(f"[process_regular_stream] is_ecg={is_ecg}, prev_dt={prev_dt:.3f} ms")

    filled_data.append(all_data[0])
    prev_ts = all_data[0][0]
    # log.debug(f"[process_regular_stream] gap-fill start: first ts={prev_ts}, samples to process={len(all_data)-1}")

    if is_ecg:
        missing_value = get_missing_value(stream_name)
        log.debug(f"[process_regular_stream] ECG missing_value={missing_value}")
        for ts, val in all_data[1:]:
            gap = ts - prev_ts
            # log.debug(f"[gap-fill] ts={ts}, prev_ts={prev_ts}, gap={gap:.1f} ms, threshold={prev_dt * 1.5:.1f} ms")
            if gap > prev_dt * 1.5:
                num_missing = int((gap // prev_dt) - 1)
                log.debug(f"[gap-fill] gap exceeds threshold -> num_missing={num_missing}")
                if num_missing > 0:
                    log.warning(f"[gap-fill] ECG gap: {gap:.1f} ms -> inserting {num_missing} fill samples (from {prev_ts} to {ts})")
                    #print(f" ECG gap detected: {gap:.1f}ms -> inserting {num_missing} missing samples (from {prev_ts} to {ts})")
                    log.debug(f"[gap-fill] ECG gap detected: {gap:.1f}ms -> inserting {num_missing} missing samples (from {prev_ts} to {ts})")
                    for i in range(num_missing):
                        prev_ts += prev_dt
                        filled_data.append((int(prev_ts), missing_value))
                        log.debug(f"[gap-fill]   inserted fill sample at ts={int(prev_ts)}, val={missing_value}")
            filled_data.append((ts, val))
            prev_ts = ts
    else:
        # For non-ECG, just keep samples as-is (no gap filling)
        filled_data.extend(all_data[1:])
        #print(" Non-ECG stream: skipping gap filling.")
        log.debug("[process_regular_stream] Non-ECG stream: skipping gap filling.")
        log.debug(" Non-ECG stream: skipping gap filling.") 
    #print(f"  Total samples (after filling): {len(filled_data)}")
    log.debug(f"  Total samples (after filling): {len(filled_data)}")
    log.debug(f"[process_regular_stream] Total samples after filling: {len(filled_data)}")

    # --- Write output CSV ---
    base, _ = os.path.splitext(output_file)
    stream_output = f"{base}_{stream_name}.csv"
    log.debug(f"[process_regular_stream] writing CSV to {stream_output!r}")
    if progress_callback:
        progress_callback(0, len(filled_data), stream_name)

    async with aiofiles.open(stream_output, "w", newline="") as csvfile:
        # Determine header
        first_val = filled_data[0][1]
        log.debug(f"[process_regular_stream] first_val={first_val!r}, type={type(first_val).__name__}")
        if isinstance(first_val, list) and len(first_val) == 3:
            header = ["Timestamp_ms", "X", "Y", "Z", "RelativeTime", relative_time, "UTC", utc_time_str]
            log.debug("[process_regular_stream] using X/Y/Z header")
        else:
            header = ["Timestamp_ms", "Value", "RelativeTime", relative_time, "UTC", utc_time_str]
            log.debug("[process_regular_stream] using scalar Value header")

        # Write header first
        buf = io.StringIO()
        csv.writer(buf).writerow(header)
        await csvfile.write(buf.getvalue())

        # Write data in chunks of 1000 rows
        chunk_size = 1000
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        for i, (ts, val) in enumerate(filled_data):
            if isinstance(val, list):
                writer.writerow([ts] + [f"{v:.6f}" for v in val])
            else:
                writer.writerow([ts, f"{val:.6f}"])

            if (i + 1) % chunk_size == 0:
                if progress_callback:
                    progress_callback(i + 1, len(filled_data), stream_name)
                await csvfile.write(buffer.getvalue())
                buffer = io.StringIO()
                writer = csv.writer(buffer)

        # Remaining rows
        remaining = buffer.getvalue()
        if remaining:
            await csvfile.write(remaining)

    #print(f"Saved {len(filled_data)} samples to {stream_output}\n")
    log.debug(f"Saved {len(filled_data)} samples to {stream_output}\n")
    log.info(f"[process_regular_stream] Saved {len(filled_data)} samples to {stream_output}")

async def main():
    if len(sys.argv) < 3:
        #print(f"Usage: python {sys.argv[0]} <input_json_file> <output_csv_file>")
        log.error(f"Usage: python {sys.argv[0]} <input_json_file> <output_csv_file>")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2]
    await convert_json_to_csv(input_file, output_file)

if __name__ == "__main__":
    asyncio.run(main())