import asyncio
import pandas as pd
import numpy as np
import pyedflib
from pyedflib import FILETYPE_EDFPLUS
from datetime import datetime
import sys
import os

async def csv_to_edf_plus(csv_filename, edf_filename=None, sampling_freq=None, unit='mV', scale_factor=1, recording_start=None):
    """
    Convert a CSV file with single ECG channel data to EDF+ format.
    
    Parameters:
    -----------
    csv_filename : str
        Path to the input CSV file
    edf_filename : str, optional
        Path to the output EDF+ file. If None, will use the same name as the CSV file but with .edf extension.
    sampling_freq : float, optional
        Sampling frequency in Hz. If None, will be estimated from timestamps.
    unit : str, optional
        Unit of measurement ('mV', 'uV', or 'V'). Default is 'mV'.
    scale_factor : float, optional
        Additional scaling factor to apply to the data. Default is 1.0.
    """
    if edf_filename is None:
        edf_filename = os.path.splitext(csv_filename)[0] + '.edf'

    if recording_start is None:
        recording_start = datetime.now()

    # Read the CSV file - only first two columns
    print(f"Reading CSV file: {csv_filename}")
    try:
        # First attempt to read with headers
        df = pd.read_csv(csv_filename)
        if len(df.columns) >= 2:
            # Check if there are metadata rows (like "Relative Time:" and "UTC Time:")
            if df.iloc[0].astype(str).str.contains('Time:').any():
                # Skip the first row if it contains metadata
                df = pd.read_csv(csv_filename, skiprows=1, names=['Timestamp_ms', 'ECG'])
            else:
                # Use only first two columns and rename them
                df = df.iloc[:, 0:2]
                df.columns = ['Timestamp_ms', 'ECG']
    except Exception as e:
        print(f"Error reading CSV: {str(e)}")
        raise
    
    # Remove any rows with NaN values
    df = df.dropna()
    
    print(f"Loaded {len(df)} samples")
    
    # Calculate sampling frequency based on timestamps if not provided
    if sampling_freq is None:
        try:
            # Handle both string and numeric timestamp formats
            if df['Timestamp_ms'].dtype == object:  # If timestamps are strings
                df['Timestamp_ms'] = pd.to_numeric(df['Timestamp_ms'].str.replace('"', '').str.strip(), errors='coerce')
            else:  # If timestamps are already numeric
                df['Timestamp_ms'] = pd.to_numeric(df['Timestamp_ms'], errors='coerce')
            
            timestamps = df['Timestamp_ms'].values
            time_diffs = np.diff(timestamps)
            mean_interval = np.mean(time_diffs)
            sampling_freq_raw = 1000.0 / mean_interval  # Timestamps in ms
            sampling_freq = round(sampling_freq_raw)
            print(f"Estimated sampling frequency: {sampling_freq_raw:.2f} Hz (mean interval: {mean_interval:.2f} ms)")
            print(f"Rounded sampling frequency: {sampling_freq} Hz")
        except Exception as e:
            print(f"Warning: Could not calculate sampling frequency from timestamps: {str(e)}")
            print("Using default sampling frequency of 200 Hz")
            sampling_freq = 200  # Default to 200 Hz if we can't calculate it
    else:
        print(f"Using provided sampling frequency: {sampling_freq:.2f} Hz")

    # Convert ECG values to numeric and apply scaling factor
    try:
        # Handle both string and numeric input formats
        if df['ECG'].dtype == object:  # If the data is string type
            df['ECG'] = pd.to_numeric(df['ECG'].str.replace('"', '').str.strip(), errors='coerce')
        else:  # If the data is already numeric
            df['ECG'] = pd.to_numeric(df['ECG'], errors='coerce')
            
        ecg_data = df['ECG'].values.astype(np.float64) * scale_factor
    except Exception as e:
        raise ValueError(f"Error converting ECG values to numeric format: {str(e)}")
    
    # Show statistics
    data_min = np.min(ecg_data)
    data_max = np.max(ecg_data)
    data_mean = np.mean(ecg_data)
    data_std = np.std(ecg_data)
    
    print(f"\nData statistics:")
    print(f"  Min: {data_min:.3f} {unit}")
    print(f"  Max: {data_max:.3f} {unit}")
    print(f"  Mean: {data_mean:.3f} {unit}")
    print(f"  Std Dev: {data_std:.3f} {unit}")
    print(f"  Peak-to-peak: {data_max - data_min:.3f} {unit}")
    
    # Check if data looks reasonable for ECG
    peak_to_peak = data_max - data_min
    if unit == 'mV' and peak_to_peak < 0.1:
        print(f"\n WARNING: Peak-to-peak amplitude ({peak_to_peak:.3f} mV) seems very small for ECG!")
        print(f"  Typical ECG in mV: 0.5-3.0 mV peak-to-peak")
        print(f"  Your data might be in microvolts (µV) or need scaling.")
    
    # Use data range with margin, but ensure reasonable minimum range
    range_margin = max(abs(data_max), abs(data_min)) * 0.2
    physical_max = data_max + range_margin
    physical_min = data_min - range_margin
    
    # Ensure minimum range to avoid flat-looking signals
    if physical_max - physical_min < 1.0:
        center = (physical_max + physical_min) / 2
        physical_max = center + 0.5
        physical_min = center - 0.5

    # Round to avoid EDF+ precision warnings
    physical_max = round(physical_max, 2)
    physical_min = round(physical_min, 2)
    
    signal_header = {
        'label': 'ECG',
        'dimension': unit,
        'sample_frequency': sampling_freq,
        'physical_max': physical_max,
        'physical_min': physical_min,
        'digital_max': 32767,
        'digital_min': -32768,
        'transducer': 'AgCl electrodes',
        'prefilter': 'HP:0.05Hz LP:150Hz'
    }
    
    print(f"\nEDF signal parameters:")
    print(f"  Physical range: [{physical_min:.3f}, {physical_max:.3f}] {unit}")
    print(f"  Digital range: [-32768, 32767]")

    # Prepare EDF+ header
    header = {
        'technician': '',
        'recording_additional': 'Converted from CSV',
        'patientname': 'Unknown',
        'patient_additional': '',
        'patientcode': '',
        'equipment': 'CSV Converter',
        'admincode': '',
        'sex': '',
        'startdate': recording_start,
        'birthdate': ''
    }

    # Create EDF+ file
    print(f"\nCreating EDF+ file: {edf_filename}")
    if recording_start:
        print(f"Recording start time: {recording_start.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        print(f"Recording start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (current time, none provided)")
    print(f"Writing {len(ecg_data)} samples...")
        
    try:
        f = pyedflib.EdfWriter(edf_filename, n_channels=1, file_type=FILETYPE_EDFPLUS)
        f.setSignalHeaders([signal_header])
        f.setHeader(header)
        
        # Write all samples at once
        f.writeSamples([ecg_data])
        
        f.close()

        # Verify file size
        file_size = os.path.getsize(edf_filename)
        print(f"\n Successfully converted {csv_filename} to {edf_filename}")
        print(f"  - Channels: 1 (ECG)")
        print(f"  - Samples: {len(df)}")
        print(f"  - Sampling frequency: {sampling_freq:.2f} Hz")
        print(f"  - Duration: {len(df)/sampling_freq:.2f} seconds")
        print(f"  - EDF file size: {file_size / 1024:.1f} KB")
        
        return edf_filename
        
    except Exception as e:
        print(f"\n Error creating EDF+ file: {e}")
        import traceback
        traceback.print_exc()
        raise

def main():
    if len(sys.argv) < 2:
        print("Usage: python csv2edf.py <input_csv_file> [output_edf_file] [sampling_freq] [unit] [scale_factor]")
        print("\nArguments:")
        print("  input_csv_file  : CSV file with timestamp and ECG data")
        print("  output_edf_file : Output EDF file (optional, default: same name as CSV)")
        print("  sampling_freq   : Sampling frequency in Hz (optional, auto-detected)")
        print("  unit            : Unit of measurement: 'mV', 'uV', or 'V' (optional, default: 'mV')")
        print("  scale_factor    : Scaling multiplier (optional, default: 1.0)")
        print("\nExamples:")
        print("  python csv2edf.py ecg_data.csv")
        print("  python csv2edf.py ecg_data.csv output.edf")
        print("  python csv2edf.py ecg_data.csv output.edf 250")
        print("  python csv2edf.py ecg_data.csv output.edf 250 uV")
        print("  python csv2edf.py ecg_data.csv output.edf 250 mV 1000")
        return

    csv_filename = sys.argv[1]
    edf_filename = sys.argv[2] if len(sys.argv) > 2 else None
    sampling_freq = float(sys.argv[3]) if len(sys.argv) > 3 else None
    unit = sys.argv[4] if len(sys.argv) > 4 else 'mV'
    scale_factor = float(sys.argv[5]) if len(sys.argv) > 5 else 1000.0

    if not os.path.exists(csv_filename):
        print(f"Error: File '{csv_filename}' not found!")
        return

    asyncio.run(csv_to_edf_plus(csv_filename, edf_filename, sampling_freq, unit, scale_factor))

if __name__ == "__main__":
    main()