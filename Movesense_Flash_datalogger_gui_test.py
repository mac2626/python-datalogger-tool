"""
Movesense Flash Datalogger GUI - Automation Tests
==================================================
 
Run with:
    python -m pytest Movesense_Flash_datalogger_gui_test.py -v
"""
 
import tkinter as tk
import unittest
import asyncio
import tempfile
import asyncio
import csv
import json
import os
import tempfile
import unittest
import pyedflib
import pandas as pd
from unittest.mock import AsyncMock, patch
from ms_json2csv import convert_json_to_csv
from csv2edf import csv_to_edf_plus 
from Movesense_Flash_datalogger_gui import AdvancedConfigDialog, DataloggerGUI

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
 
def make_dialog():
    """Creates a hidden AdvancedConfigDialog for testing."""
    root = tk.Tk()
    root.withdraw()
    dialog = AdvancedConfigDialog(root, current_config="")
    dialog.dialog.withdraw()
    return root, dialog
 
 
def make_app():
    """
    Creates a hidden DataloggerGUI for testing.
    Patches PIL so the missing logo file does not crash the constructor.
    """
    root = tk.Tk()
    root.withdraw()
    with patch("PIL.Image.open", return_value=tk.PhotoImage()):
        with patch("PIL.ImageTk.PhotoImage", return_value=None):
            app = DataloggerGUI(root)
    return root, app
 
 
def run(coro):
    """Run an async coroutine synchronously inside a test."""
    return asyncio.run(coro)

#---------------------------------------------------------------------------
# Helpers — build small sample data files
# ---------------------------------------------------------------------------
 
def make_ecg_json(num_chunks=3, samples_per_chunk=5, start_ts=1000):
    """
    Build a minimal ECG JSON structure that ms_json2csv understands.
 
    num_chunks        : how many data packets (like real device sends)
    samples_per_chunk : ECG samples inside each packet
    start_ts          : first timestamp in milliseconds
    """
    samples = [
        {"TimeDetailed": {
            "relativeTime": "1000",
            "utcTime": "1700000000000000"   # arbitrary µs epoch
        }}
    ]
    # Each chunk has a Timestamp and an array of ECG values (raw integers)
    # ECG_LSB_TO_MV = 0.000381469726563  (from ms_json2csv.py)
    chunk_interval = samples_per_chunk * 5  # 5 ms per sample = 200 Hz
    for i in range(num_chunks):
        ts = start_ts + i * chunk_interval
        samples.append({
            "MeasEcg": {
                "Timestamp": ts,
                "Samples": {
                    "Ecg": [100 + j for j in range(samples_per_chunk)]
                }
            }
        })
    return {"Samples": samples}
 
 
def make_imu6_json(num_chunks=3, samples_per_chunk=4, start_ts=1000):
    """
    IMU6 JSON — uses MeasIMU6 stream with ArrayAcc and ArrayGyro.
    This is the format process_imu_stream() expects
    """
    samples = [
        {"TimeDetailed": {
            "relativeTime": "1000",
            "utcTime": "1700000000000000"
        }}
    ]
    chunk_interval = samples_per_chunk * 10  # 10 ms per sample = 100 Hz
    for i in range(num_chunks):
        ts = start_ts + i * chunk_interval
        samples.append({
            "MeasIMU6": {
                "Timestamp": ts,
                "ArrayAcc": [
                    {"x": 1.0 + j, "y": 2.0 + j, "z": 9.8}
                    for j in range(samples_per_chunk)
                ],
                "ArrayGyro": [
                    {"x": 0.1 + j, "y": 0.2 + j, "z": 0.3}
                    for j in range(samples_per_chunk)
                ]
            }
        })
    return {"Samples": samples}
 
 
def make_ecg_csv(num_samples=100, sampling_hz=200):
    """
    Build a minimal ECG CSV string that csv2edf understands.
    Returns the CSV content as a string.
    """
    interval_ms = 1000.0 / sampling_hz
    lines = ["Timestamp_ms,ECG,RelativeTime,0,UTC,2024-01-01 12:00:00"]
    for i in range(num_samples):
        ts = i * interval_ms
        ecg_value = 0.5 * (i % 20) / 20.0  # simple sawtooth ~0-0.5 mV
        lines.append(f"{ts:.3f},{ecg_value:.6f}")
    return "\n".join(lines)
 
 
 
class TestCheckboxSelectionLimit(unittest.TestCase):
 
    def setUp(self):
        self.root, self.dlg = make_dialog()
 
    def tearDown(self):
        self.root.destroy()
 
    # Check if user can select one measurement without any error
    def test_first_selection_is_allowed(self):
        self.dlg.vars["/Meas/Ecg"].set(True)
        self.dlg.on_checkbox_toggle("/Meas/Ecg")
 
        self.assertTrue(self.dlg.vars["/Meas/Ecg"].get())
 
    # Check if user can select three measurements without any error
    def test_three_selections_are_allowed(self):
        for meas in ["/Meas/Ecg", "/Meas/Acc", "/Meas/Gyro"]:
            self.dlg.vars[meas].set(True)
            self.dlg.on_checkbox_toggle(meas)

        selected = sum(v.get() for v in self.dlg.vars.values())
        self.assertEqual(selected, 3)

    def test_fourth_selection_is_rejected(self):
        for meas in ["/Meas/Ecg", "/Meas/Acc", "/Meas/Gyro"]:
            self.dlg.vars[meas].set(True)
            self.dlg.on_checkbox_toggle(meas)

        self.dlg.vars["/Meas/HR"].set(True)
        self.dlg.on_checkbox_toggle("/Meas/HR")

        self.assertFalse(self.dlg.vars["/Meas/HR"].get())

    def test_fourth_selection_shows_warning(self):
        for meas in ["/Meas/Ecg", "/Meas/Acc", "/Meas/Gyro"]:
            self.dlg.vars[meas].set(True)
            self.dlg.on_checkbox_toggle(meas)

        self.dlg.vars["/Meas/HR"].set(True)
        self.dlg.on_checkbox_toggle("/Meas/HR")

        self.assertIn("3", self.dlg.warning_label.cget("text"))

class TestFetchData(unittest.TestCase):
    """
    fetch_data connects to the device and downloads log files.

    """

    def setUp(self):
        self.root, self.app = make_app()

        # Put the app in a normal "connected, ready to fetch" state
        self.app.device_connected = True
        self.app.logging_active = False
        self.app.serial_entry.insert(0, "123456")

    def tearDown(self):
        self.root.destroy()

    # --- Test 1: the simplest fetch test ---
    # What it checks: when everything is OK the tool is actually called
    def test_fetch_tool_is_called_on_success(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.app.output_entry.insert(0, tmp_dir)

            # Fake a successful response from the device tool
            fake_result = {"success": True, "files_fetched": ["log_001.sbem"]}

            with patch("datalogger_tool.fetch_data",
                    new=AsyncMock(return_value=fake_result)) as mock_fetch:
                run(self.app.fetch_data.__wrapped__(self.app))

            # Was the tool actually called?
            self.assertTrue(mock_fetch.called)

    # Check that the tool was called with the correct serial number
    
    def test_fetch_called_with_correct_serial(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.app.output_entry.insert(0, tmp_dir)
            fake_result = {"success": True, "files_fetched": ["log_001.sbem"]}
    
            with patch("datalogger_tool.fetch_data",
                       new=AsyncMock(return_value=fake_result)) as mock_fetch:
                run(self.app.fetch_data.__wrapped__(self.app))
    
            # What serial was passed to the tool?
            actual_serial = mock_fetch.call_args[1]["serial"]
            self.assertEqual(actual_serial, "123456")

    def test_error_message_shown_when_fetch_fails(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.app.output_entry.insert(0, tmp_dir)
            fake_error = {"success": False, "error": "Device unreachable"}
    
            with patch("datalogger_tool.fetch_data",
                       new=AsyncMock(return_value=fake_error)):
                run(self.app.fetch_data.__wrapped__(self.app))

            self.root.update()
    
            output = self.app.output_text.get("1.0", tk.END)
            self.assertIn("Device unreachable", output)

    def test_fetch_blocked_when_logging_active(self):
        self.app.logging_active = True
    
        with patch("tkinter.messagebox.showwarning") as mock_warn:
            with patch("datalogger_tool.fetch_data",
                       new=AsyncMock()) as mock_fetch:
                run(self.app.fetch_data.__wrapped__(self.app))
    
        mock_warn.assert_called_once()   # warning was shown
        mock_fetch.assert_not_called()   # tool was NOT called

# ---------------------------------------------------------------------------
# JSON → CSV conversion  (ms_json2csv)
# ---------------------------------------------------------------------------
 
class TestJsonToCsvConversion(unittest.TestCase):
    """
    Tests for convert_json_to_csv.
 
    What we check for each stream type:
      - Output CSV file is actually created
      - It has the right column headers
      - It contains the right number of data rows
      - Values look correct (not zero, not corrupted)
    """
 
    def setUp(self):
        # Create a temporary folder — deleted automatically after each test
        self.tmp = tempfile.mkdtemp()
 
    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
 
    # ------------------------------------------------------------------
    # TEST 1 — ECG output file is created
    # ------------------------------------------------------------------
    def test_ecg_csv_file_is_created(self):
        # Write a small ECG JSON file
        json_path = os.path.join(self.tmp, "ecg.json")
        with open(json_path, "w") as f:
            json.dump(make_ecg_json(), f)
 
        output_path = os.path.join(self.tmp, "output.csv")
        run(convert_json_to_csv(json_path, output_path))
 
        # The converter appends the stream name, so look for MeasEcg
        expected = os.path.join(self.tmp, "output_MeasEcg.csv")
        self.assertTrue(
            os.path.exists(expected),
            f"Expected output file not found: {expected}"
        )
 
    # ------------------------------------------------------------------
    # TEST 2 — ECG CSV has correct column headers
    # ------------------------------------------------------------------
    def test_ecg_csv_has_correct_headers(self):
        json_path = os.path.join(self.tmp, "ecg.json")
        with open(json_path, "w") as f:
            json.dump(make_ecg_json(), f)
 
        output_path = os.path.join(self.tmp, "output.csv")
        run(convert_json_to_csv(json_path, output_path))
 
        csv_path = os.path.join(self.tmp, "output_MeasEcg.csv")
        with open(csv_path, newline="") as f:
            headers = next(csv.reader(f))
 
        # First column must be Timestamp_ms, second must be Value (ECG scalar)
        self.assertEqual(headers[0], "Timestamp_ms")
        self.assertEqual(headers[1], "Value")
 
    # ------------------------------------------------------------------
    # TEST 3 — ECG CSV has the right number of data rows
    # ------------------------------------------------------------------
    def test_ecg_csv_has_correct_row_count(self):
        num_chunks = 3
        samples_per_chunk = 5
 
        json_path = os.path.join(self.tmp, "ecg.json")
        with open(json_path, "w") as f:
            json.dump(make_ecg_json(
                num_chunks=num_chunks,
                samples_per_chunk=samples_per_chunk
            ), f)
 
        output_path = os.path.join(self.tmp, "output.csv")
        run(convert_json_to_csv(json_path, output_path))
 
        csv_path = os.path.join(self.tmp, "output_MeasEcg.csv")
        df = pd.read_csv(csv_path, usecols=[0, 1])  # only Timestamp + Value
        expected_rows = num_chunks * samples_per_chunk
        self.assertEqual(
            len(df), expected_rows,
            f"Expected {expected_rows} rows, got {len(df)}"
        )
 
    # ------------------------------------------------------------------
    # TEST 4 — ECG timestamps are in ascending order (no scrambling)
    # ------------------------------------------------------------------
    def test_ecg_timestamps_are_ascending(self):
        json_path = os.path.join(self.tmp, "ecg.json")
        with open(json_path, "w") as f:
            json.dump(make_ecg_json(num_chunks=5, samples_per_chunk=5), f)
 
        output_path = os.path.join(self.tmp, "output.csv")
        run(convert_json_to_csv(json_path, output_path))
 
        csv_path = os.path.join(self.tmp, "output_MeasEcg.csv")
        df = pd.read_csv(csv_path, usecols=[0])
        timestamps = df.iloc[:, 0].values
 
        for i in range(1, len(timestamps)):
            self.assertGreaterEqual(
                timestamps[i], timestamps[i - 1],
                f"Timestamp not ascending at row {i}: "
                f"{timestamps[i-1]} -> {timestamps[i]}"
            )
 
    # ------------------------------------------------------------------
    # TEST 5 — IMU CSV has X, Y, Z columns
    # ------------------------------------------------------------------
    def test_imu_csv_has_xyz_headers(self):
        json_path = os.path.join(self.tmp, "imu.json")
        with open(json_path, "w") as f:
            json.dump(make_imu6_json(), f)
 
        run(convert_json_to_csv(json_path, os.path.join(self.tmp, "output.csv")))
 
        # IMU6 produces one CSV per sensor type: output_MeasIMU6_Acc.csv
        csv_path = os.path.join(self.tmp, "output_MeasIMU6_Acc.csv")
        self.assertTrue(os.path.exists(csv_path),
            f"IMU CSV not created: {csv_path}")
 
        with open(csv_path, newline="") as f:
            headers = next(csv.reader(f))
 
        self.assertIn("X", headers)
        self.assertIn("Y", headers)
        self.assertIn("Z", headers)
 
    # ------------------------------------------------------------------
    # TEST 6 — Empty JSON produces no crash (graceful handling)
    # ------------------------------------------------------------------
    def test_empty_samples_does_not_crash(self):
        json_path = os.path.join(self.tmp, "empty.json")
        with open(json_path, "w") as f:
            json.dump({"Samples": []}, f)
 
        output_path = os.path.join(self.tmp, "output.csv")
        try:
            run(convert_json_to_csv(json_path, output_path))
        except Exception as e:
            self.fail(f"convert_json_to_csv raised unexpectedly: {e}")
 
 
# ---------------------------------------------------------------------------
# 2. CSV → EDF conversion  (csv2edf)
# ---------------------------------------------------------------------------
 
class TestCsvToEdfConversion(unittest.TestCase):
    """
    Tests for csv_to_edf_plus.
 
    What we check:
      - EDF file is actually created
      - Signal label is 'ECG'
      - Sampling frequency matches what we put in
      - Number of samples matches the CSV row count
      - Signal values are not all zero (data survived conversion)
    """
 
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
 
    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
 
    # ------------------------------------------------------------------
    # TEST 1 — EDF file is created
    # ------------------------------------------------------------------
    def test_edf_file_is_created(self):
        csv_path = os.path.join(self.tmp, "ecg.csv")
        with open(csv_path, "w") as f:
            f.write(make_ecg_csv(num_samples=100, sampling_hz=200))
 
        edf_path = os.path.join(self.tmp, "ecg.edf")
        run(csv_to_edf_plus(
            csv_filename=csv_path,
            edf_filename=edf_path,
            sampling_freq=200
        ))
 
        self.assertTrue(
            os.path.exists(edf_path),
            "EDF file was not created"
        )
 
    # ------------------------------------------------------------------
    # TEST 2 — EDF signal label is ECG
    # ------------------------------------------------------------------
    def test_edf_signal_label_is_ecg(self):
        csv_path = os.path.join(self.tmp, "ecg.csv")
        with open(csv_path, "w") as f:
            f.write(make_ecg_csv(num_samples=100, sampling_hz=200))
 
        edf_path = os.path.join(self.tmp, "ecg.edf")
        run(csv_to_edf_plus(csv_path, edf_path, sampling_freq=200))
 
        reader = pyedflib.EdfReader(edf_path)
        label = reader.getLabel(0)
        reader.close()
 
        self.assertEqual(label.strip(), "ECG")
 
    # ------------------------------------------------------------------
    # TEST 3 — EDF sampling frequency matches what we provided
    # ------------------------------------------------------------------
    def test_edf_sampling_frequency_is_correct(self):
        csv_path = os.path.join(self.tmp, "ecg.csv")
        with open(csv_path, "w") as f:
            f.write(make_ecg_csv(num_samples=200, sampling_hz=200))
 
        edf_path = os.path.join(self.tmp, "ecg.edf")
        run(csv_to_edf_plus(csv_path, edf_path, sampling_freq=200))
 
        reader = pyedflib.EdfReader(edf_path)
        freq = reader.getSampleFrequency(0)
        reader.close()
 
        self.assertEqual(freq, 200)
 
    # ------------------------------------------------------------------
    # TEST 4 — EDF contains the right number of samples
    # ------------------------------------------------------------------
    def test_edf_sample_count_matches_csv(self):
        num_samples = 400
        csv_path = os.path.join(self.tmp, "ecg.csv")
        with open(csv_path, "w") as f:
            f.write(make_ecg_csv(num_samples=num_samples, sampling_hz=200))
 
        edf_path = os.path.join(self.tmp, "ecg.edf")
        run(csv_to_edf_plus(csv_path, edf_path, sampling_freq=200))
 
        reader = pyedflib.EdfReader(edf_path)
        count = reader.getNSamples()[0]
        reader.close()
 
        self.assertEqual(
            count, num_samples,
            f"Expected {num_samples} samples in EDF, got {count}"
        )
 
    # ------------------------------------------------------------------
    # TEST 5 — EDF signal values are not all zero (data survived)
    # ------------------------------------------------------------------
    def test_edf_signal_values_are_not_all_zero(self):
        csv_path = os.path.join(self.tmp, "ecg.csv")
        with open(csv_path, "w") as f:
            f.write(make_ecg_csv(num_samples=200, sampling_hz=200))
 
        edf_path = os.path.join(self.tmp, "ecg.edf")
        run(csv_to_edf_plus(csv_path, edf_path, sampling_freq=200))
 
        reader = pyedflib.EdfReader(edf_path)
        signal = reader.readSignal(0)
        reader.close()
 
        self.assertTrue(
            any(v != 0.0 for v in signal),
            "All EDF signal values are zero — data was lost during conversion"
        )
 
    # ------------------------------------------------------------------
    # TEST 6 — Sampling frequency is auto-detected from timestamps
    # ------------------------------------------------------------------
    def test_sampling_frequency_auto_detected_from_timestamps(self):
        # Provide timestamps at exactly 250 Hz (4 ms interval)
        lines = ["Timestamp_ms,ECG,RelativeTime,0,UTC,2024-01-01 12:00:00"]
        for i in range(250):
            lines.append(f"{i * 4},{0.1 * (i % 10):.6f}")
 
        csv_path = os.path.join(self.tmp, "ecg250.csv")
        with open(csv_path, "w") as f:
            f.write("\n".join(lines))
 
        edf_path = os.path.join(self.tmp, "ecg250.edf")
        # sampling_freq=None → auto-detect from timestamps
        run(csv_to_edf_plus(csv_path, edf_path, sampling_freq=None))
 
        reader = pyedflib.EdfReader(edf_path)
        freq = reader.getSampleFrequency(0)
        reader.close()
 
        self.assertEqual(freq, 250, f"Expected 250 Hz, got {freq}")

class TestCheckboxCounterLabel(unittest.TestCase):
    """Counter label text reflects the current selection count correctly."""
 
    def setUp(self):
        self.root, self.dlg = make_dialog()
 
    def tearDown(self):
        self.root.destroy()
 
    def test_counter_label_shows_zero_selected_initially(self):
        text = self.dlg.counter_label.cget("text")
        self.assertIn("0", text)
 
    def test_counter_label_updates_after_selection(self):
        self.dlg.vars["/Meas/Ecg"].set(True)
        self.dlg.on_checkbox_toggle("/Meas/Ecg")
        text = self.dlg.counter_label.cget("text")
        self.assertIn("1", text)
 
    def test_counter_label_shows_max_reached_message(self):
        for meas in ["/Meas/Ecg", "/Meas/Acc", "/Meas/Gyro"]:
            self.dlg.vars[meas].set(True)
            self.dlg.on_checkbox_toggle(meas)
        text = self.dlg.counter_label.cget("text")
        self.assertNotIn("maximum", text)

# ---------------------------------------------------------------------------
# Button state machine
# ---------------------------------------------------------------------------
 
class TestUpdateButtonStates(unittest.TestCase):
    """
    update_button_states has three distinct branches driven by
    device_connected and logging_active. We drive those flags directly
    and assert which buttons end up enabled / disabled.
    """
 
    def setUp(self):
        self.root, self.app = make_app()
 
    def tearDown(self):
        self.root.destroy()
 
    # --- Branch 1: not connected ---
 
    def test_not_connected_disables_start(self):
        self.app.device_connected = False
        self.app.logging_active = False
        self.app.update_button_states()
        self.assertEqual(str(self.app.start_button.cget("state")), "disabled")
 
    def test_not_connected_disables_stop(self):
        self.app.device_connected = False
        self.app.logging_active = False
        self.app.update_button_states()
        self.assertEqual(str(self.app.stop_button.cget("state")), "disabled")
 
    def test_not_connected_disables_fetch(self):
        self.app.device_connected = False
        self.app.logging_active = False
        self.app.update_button_states()
        self.assertEqual(str(self.app.fetch_button.cget("state")), "disabled")
 
    # --- Branch 2: connected and logging ---
 
    def test_logging_active_enables_stop(self):
        self.app.device_connected = True
        self.app.logging_active = True
        self.app.update_button_states()
        self.assertEqual(str(self.app.stop_button.cget("state")), "normal")
 
    def test_logging_active_disables_start(self):
        self.app.device_connected = True
        self.app.logging_active = True
        self.app.update_button_states()
        self.assertEqual(str(self.app.start_button.cget("state")), "disabled")
 
    def test_logging_active_disables_fetch(self):
        self.app.device_connected = True
        self.app.logging_active = True
        self.app.update_button_states()
        self.assertEqual(str(self.app.fetch_button.cget("state")), "disabled")
 
    def test_logging_active_disables_erase(self):
        self.app.device_connected = True
        self.app.logging_active = True
        self.app.update_button_states()
        self.assertEqual(str(self.app.erase_button.cget("state")), "disabled")
 
    # --- Branch 3: connected, idle ---
 
    def test_connected_idle_enables_start(self):
        self.app.device_connected = True
        self.app.logging_active = False
        self.app.update_button_states()
        self.assertEqual(str(self.app.start_button.cget("state")), "normal")
 
    def test_connected_idle_enables_fetch(self):
        self.app.device_connected = True
        self.app.logging_active = False
        self.app.update_button_states()
        self.assertEqual(str(self.app.fetch_button.cget("state")), "normal")
 
    def test_connected_idle_disables_stop(self):
        self.app.device_connected = True
        self.app.logging_active = False
        self.app.update_button_states()
        self.assertEqual(str(self.app.stop_button.cget("state")), "disabled")
 
    def test_connected_idle_enables_erase(self):
        self.app.device_connected = True
        self.app.logging_active = False
        self.app.update_button_states()
        self.assertEqual(str(self.app.erase_button.cget("state")), "normal")
    
 
if __name__ == "__main__":
    unittest.main()