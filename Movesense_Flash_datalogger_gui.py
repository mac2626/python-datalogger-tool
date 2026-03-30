import asyncio
import logging
from logging import FileHandler
import multiprocessing
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
import aiofiles
from async_tkinter_loop import async_handler, async_mainloop
from csv2edf import csv_to_edf_plus
from ms_json2csv import convert_json_to_csv
from PIL import Image, ImageTk
from datetime import datetime, UTC, timezone
from packaging import version
import subprocess
import os
import sys
import io
import re
import json
import traceback
import webbrowser
from contextlib import redirect_stdout
import datalogger_tool as tool  
import queue
import threading
import time

import tkinter as tk
from tkinter import ttk, scrolledtext

# Debug file logging setup
_start_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

# When frozen by PyInstaller, write log next to the .exe; otherwise next to the script
if getattr(sys, 'frozen', False):
    _base_dir = os.path.dirname(sys.executable)
else:
    _base_dir = os.path.dirname(os.path.abspath(__file__))

_log_path = os.path.join(_base_dir, f"debug_{_start_ts}.log")

logging.root.setLevel(logging.DEBUG)
#get_local_bt_info()

SBEM2JSON_EXECUTABLE = "sbem2json.exe" if os.name == "nt" else "sbem2json"  # Ensure this is in your PATH or have in current directory

class AdvancedConfigDialog:
    """Dialog for advanced configuration options"""
    MAX_SELECTIONS = 3  

    def __init__(self, parent, current_config):
        self.result = None
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Advanced Configuration")
        self.dialog.geometry("600x550")
        self.dialog.transient(parent)
        self.dialog.grab_set()

        # Main frame
        main_frame = ttk.Frame(self.dialog, padding="20")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self.dialog.columnconfigure(0, weight=1)
        self.dialog.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(1, weight=1)
        
        # Title
        ttk.Label(main_frame, text="Advanced Logging Configuration", 
                  font=("", 12, "bold")).grid(row=0, column=0, sticky=tk.W, pady=(0, 10))
        
        # Measurement options
        options_frame = ttk.LabelFrame(main_frame, text="Available Measurements", padding="10")
        options_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=(0, 10))
        options_frame.columnconfigure(0, weight=1)

        # Define available measurements
        IMU_rates = [13, 26, 52, 104, 208, 416, 833]
        self.measurements = {
            "/Meas/Acc": IMU_rates,
            "/Algo/ECGRR": [],
            "/Meas/Gyro": IMU_rates,
            "/Meas/Ecg": [125, 128, 200, 250, 256, 500, 512],
            "/Meas/HR": [],
            "/Meas/IMU6" : IMU_rates,
            "/Meas/IMU6m" : IMU_rates,
            "/Meas/IMU9" : IMU_rates,
            "/Meas/Magn": IMU_rates,
            "/Meas/Temp": [],
            
        }

        self.vars = {}       # checkbutton variables
        self.rate_vars = {}  # combobox variables

        for i, (meas, rates) in enumerate(self.measurements.items()):
            row_frame = ttk.Frame(options_frame)
            row_frame.grid(row=i, column=0, sticky=(tk.W, tk.E), pady=2)
            row_frame.columnconfigure(0, minsize=120)  
            row_frame.columnconfigure(1, weight=0) 
            
            var = tk.BooleanVar(value=False)
            self.vars[meas] = var
            chk = ttk.Checkbutton(row_frame, text=meas, variable=var,
                                  command=lambda m=meas: self.on_checkbox_toggle(m))  
            chk.grid(row=0, column=0, sticky=tk.W)

            if rates:
                rate_var = tk.StringVar(value=str(rates[0]))
                self.rate_vars[meas] = rate_var
                cb = ttk.Combobox(row_frame, textvariable=rate_var,
                                  values=[str(r) for r in rates],
                                  width=6, state="readonly")
                cb.grid(row=0, column=1, sticky=tk.W)  
                cb.bind("<<ComboboxSelected>>", lambda e, m=meas: self.on_rate_change(m))

        # Counter & feedback
        counter_frame = ttk.Frame(main_frame)
        counter_frame.grid(row=2, column=0, sticky=tk.W, pady=(0, 10))
        self.counter_label = ttk.Label(counter_frame, text="")
        self.counter_label.grid(row=0, column=0, sticky=tk.W)
        self.warning_label = ttk.Label(counter_frame, text="", foreground="red")
        self.warning_label.grid(row=1, column=0, sticky=tk.W)
        self.update_counter_label() 

        # Configuration output 
        config_frame = ttk.LabelFrame(main_frame, text="Configuration Paths", padding="10")
        config_frame.grid(row=3, column=0, sticky=(tk.W, tk.E), pady=(0, 10))
        config_frame.columnconfigure(0, weight=1)
        
        self.config_text = scrolledtext.ScrolledText(config_frame, height=5, width=60, wrap=tk.WORD)
        self.config_text.grid(row=0, column=0, sticky=(tk.W, tk.E))
        self.config_text.insert("1.0", current_config)
        self.config_text.configure(state='disabled')
        
        # Buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=4, column=0, sticky=(tk.E), pady=(10, 0))
        
        ttk.Button(button_frame, text="Reset to Default", command=self.reset_default).grid(row=0, column=0, padx=5)
        ttk.Button(button_frame, text="Cancel", command=self.cancel).grid(row=0, column=1, padx=5)
        ttk.Button(button_frame, text="Apply", command=self.apply).grid(row=0, column=2, padx=5)
        
        # Center dialog on parent
        self.dialog.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.dialog.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.dialog.winfo_height()) // 2
        self.dialog.geometry(f"+{x}+{y}")

    # When checkbox is toggled
    def on_checkbox_toggle(self, meas):
        selected_count = sum(var.get() for var in self.vars.values())
        if selected_count > self.MAX_SELECTIONS:
            # Too many -> uncheck and warn
            self.vars[meas].set(False)
            self.warning_label.configure(text=f"⚠ You can select at most {self.MAX_SELECTIONS} paths.")
        else:
            self.warning_label.configure(text="")
        self.update_config_text()
        self.update_counter_label()

    # Updates counter label
    def update_counter_label(self):
        selected_count = sum(var.get() for var in self.vars.values())
        remaining = self.MAX_SELECTIONS - selected_count

        if remaining == 0:
            msg = f"Selected {selected_count}/{self.MAX_SELECTIONS}. You have reached the maximum number of selections."
            color = "black"
        else:
            msg = f"Selected {selected_count}/{self.MAX_SELECTIONS}. You can still choose {remaining} more."
            color = "black"

        self.counter_label.configure(text=msg, foreground=color)

    # Live update handler
    def update_config_text(self):
        """Refresh config text area based on current selections"""
        selected = []
        for meas, var in self.vars.items():
            if var.get():
                rate = self.rate_vars.get(meas)
                if rate:
                    # Add /mV for ECG paths
                    if "ECG" in meas.upper():
                        selected.append(f"{meas}/{rate.get()}/mV")
                    else:
                        selected.append(f"{meas}/{rate.get()}")
                else:
                    selected.append(meas)
        config_text = ", ".join(selected)
        self.config_text.configure(state='normal')
        self.config_text.delete("1.0", tk.END)
        self.config_text.insert("1.0", config_text)
        self.config_text.configure(state='disabled')

    def on_rate_change(self, meas):
        """Handle rate changes and show warning if rates are too high"""
        self.update_config_text() 

        rate_var = self.rate_vars.get(meas)
        if not rate_var:
            return

        try:
            rate = int(rate_var.get())
        except ValueError:
            rate = 0

        warning_msg = ""

        # ECG high rate warning
        if "ECG" in meas.upper() and rate > 200:
            warning_msg = (
                f"Warning: High ECG sampling rate ({rate} Hz) may cause recording issues, "
                f"especially when measuring multiple parameters simultaneously."
            )

        # IMU high rate warning (for Acc, Gyro, IMU6, IMU9, etc.)
        elif any(x in meas.upper() for x in ["IMU", "ACC", "GYRO"]) and rate > 104:
            warning_msg = (
                f"Warning: High IMU sampling rate ({rate} Hz) may cause recording issues, "
                f"especially when measuring multiple parameters simultaneously."
            )

        # Update label (black text, as you requested)
        self.warning_label.configure(text=warning_msg, foreground="black")

        # Refresh counter as well
        self.update_counter_label()

    def reset_default(self):
        for meas in self.vars:
            self.vars[meas].set(False)
        self.vars["/Meas/Ecg"].set(True)
        self.rate_vars["/Meas/Ecg"].set("200")
        self.warning_label.configure(text="")
        self.update_config_text()
        self.update_counter_label()

    def cancel(self):
        self.dialog.destroy()

    def apply(self):
        """Collect selected options and sample rates"""
        self.result = self.config_text.get("1.0", tk.END).strip()
        self.dialog.destroy()

class AboutDialog:
    """Dialog for About information and licenses"""
    def __init__(self, parent):
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("About Movesense Flash Datalogger Tool")
        self.dialog.geometry("600x500")
        self.dialog.transient(parent)
        self.dialog.grab_set()
        
        # Main frame
        main_frame = ttk.Frame(self.dialog, padding="20")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self.dialog.columnconfigure(0, weight=1)
        self.dialog.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(1, weight=1)
        
        # Title
        ttk.Label(main_frame, text="Movesense Flash Datalogger Tool", 
                  font=("", 14, "bold")).grid(row=0, column=0, pady=(0, 10))
        
        # Contact info
        contact_frame = ttk.LabelFrame(main_frame, text="Information", padding="10")
        contact_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=(0, 10))
        
        # Email
        ttk.Label(contact_frame, text="Email: ").grid(row=0, column=0, sticky=tk.W)
        email_label = ttk.Label(contact_frame, text="info@movesense.com", 
                               foreground="blue", cursor="hand2")
        email_label.grid(row=0, column=1, sticky=tk.W)
        email_label.bind("<Button-1>", lambda e: webbrowser.open("mailto:info@movesense.com"))

        # Website
        ttk.Label(contact_frame, text="Website: ").grid(row=1, column=0, sticky=tk.W)
        website_label = ttk.Label(contact_frame, text="www.movesense.com", 
                                  foreground="blue", cursor="hand2")
        website_label.grid(row=1, column=1, sticky=tk.W)
        website_label.bind("<Button-1>", lambda e: webbrowser.open("https://www.movesense.com"))

        # GitHub Repository
        ttk.Label(contact_frame, text="Git Repository: ").grid(row=2, column=0, sticky=tk.W)
        github_label = ttk.Label(contact_frame, text="github.com/movesense/flash-datalogger", 
                                 foreground="blue", cursor="hand2")
        github_label.grid(row=2, column=1, sticky=tk.W)
        github_label.bind("<Button-1>", lambda e: webbrowser.open("https://bitbucket.org/movesense/python-datalogger-tool/src/master/"))

        # License information
        license_frame = ttk.LabelFrame(main_frame, text="License Information", padding="10")
        license_frame.grid(row=2, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 10))
        license_frame.columnconfigure(0, weight=1)
        license_frame.rowconfigure(0, weight=1)
        
        license_text = scrolledtext.ScrolledText(license_frame, height=15, width=70, wrap=tk.WORD)
        license_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # Add license text
        licenses = """Movesense Flash Datalogger Tool\n\n
This tool is provided under the MIT License.
Copyright (c) 2025 Movesense.
"""
        
        license_text.insert("1.0", licenses)
        license_text.configure(state='disabled')
        
        # Close button
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=3, column=0, sticky=tk.E, pady=(10, 0))
        ttk.Button(button_frame, text="Close", command=self.dialog.destroy).grid(row=0, column=0)
        
        # Center dialog on parent
        self.dialog.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.dialog.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.dialog.winfo_height()) // 2
        self.dialog.geometry(f"+{x}+{y}")

class DataloggerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Movesense Flash Datalogger Tool")
        self.root.geometry("800x800")

        self.logging_configured = False
        self.logging_active = False
        self.device_connected = False
        
        # Load and display the logo
        try:
            logo_path = os.path.join(os.path.dirname(__file__), "Movesense logomark white.png")
            logo_image = Image.open(logo_path)
            # Resize the image
            logo_image = logo_image.resize((55, 35), Image.Resampling.LANCZOS)
            self.logo_photo = ImageTk.PhotoImage(logo_image)
            
            # Create and configure a label for the logo
            self.logo_label = ttk.Label(self.root, image=self.logo_photo)
            self.logo_label.grid(row=0, column=1, padx=(0, 30), pady=(10, 0), sticky=tk.NE) 
        except Exception as e:
            print(f"Could not load logo: {e}")
        
        # Configure grid weights
        self.root.columnconfigure(0, weight=1)
        self.root.columnconfigure(1, weight=0)  
        self.root.rowconfigure(1, weight=1)
        
        # Create main container
        main_frame = ttk.Frame(root, padding="10")
        main_frame.grid(row=1, column=0, columnspan=2, sticky=(tk.W, tk.E, tk.N, tk.S))  
        main_frame.columnconfigure(0, weight=1)
        
        # Serial Numbers Section
        serial_frame = ttk.LabelFrame(main_frame, text="Device Serial Number", padding="5")
        serial_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=(0, 5))
        serial_frame.columnconfigure(1, weight=1)
        
        ttk.Label(serial_frame, text="1.    Serial Number:").grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self.serial_entry = ttk.Entry(serial_frame, width=50)
        self.serial_entry.grid(row=0, column=1, sticky=(tk.W, tk.E))
        ttk.Label(serial_frame, text="(e.g., 254230002030)", 
                 font=("", 8), foreground="gray").grid(row=1, column=1, sticky=tk.W)
        
        # Verbose checkbox
        self.verbose_var = tk.BooleanVar()
        self.verbose_check = ttk.Checkbutton(
            serial_frame,
            text="Verbose logging",
            variable=self.verbose_var
        )
        self.verbose_check.grid(row=0, column=2, padx=(10, 0))
        self.verbose_check.grid_remove()

        # Advanced UI checkbox 
        self.advanced_ui_var = tk.BooleanVar()
        self.advanced_ui_check = ttk.Checkbutton(
            main_frame,
            text="Advanced UI",
            variable=self.advanced_ui_var,
            command=self.toggle_advanced_ui
        )
        self.advanced_ui_check.grid(row=0, column=0, sticky=tk.NE, padx=(0, 10), pady=(0, 10))

        # Commands Section
        cmd_frame = ttk.LabelFrame(main_frame, text="Commands", padding="5")
        cmd_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=(0, 5))

        # Configure columns
        cmd_frame.columnconfigure(0, minsize=25)   # narrow column for numbers
        cmd_frame.columnconfigure(1, weight=0)     # buttons
        cmd_frame.columnconfigure(2, weight=1)     # text fields, labels, frames stretch
        
        # Row 0: Connect / Status
        ttk.Label(cmd_frame, text="2.").grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self.connect_button = ttk.Button(cmd_frame, text="Connect", command=self.check_status, width=20)
        self.connect_button.grid(row=0, column=1, padx=5, pady=5)
        self.connection_status_label = ttk.Label(cmd_frame, text="Check device connection and info")
        self.connection_status_label.grid(row=0, column=2, sticky=tk.W, padx=5)

        # Row 1: Config with Advanced button
        self.config_label = ttk.Label(cmd_frame, text="3.")
        self.config_label.grid(row=1, column=0, sticky=tk.W, padx=(0, 5))
        self.config_label.grid_remove()  

        self.config_button = ttk.Button(cmd_frame, text="Configure Logging", command=self.configure_logging, width=20)
        self.config_button.grid(row=1, column=1, padx=5, pady=5)
        self.config_button.grid_remove()  

        # Config entry frame with advanced button
        self.config_entry_frame = ttk.Frame(cmd_frame)
        self.config_entry_frame.grid(row=1, column=2, columnspan=2, sticky=(tk.W, tk.E), padx=5)
        self.config_entry_frame.grid_remove()  
        self.config_entry_frame.columnconfigure(0, weight=1)

        self.config_entry_var = tk.StringVar(value="/Meas/ECG/200/mV")
        self.config_entry = ttk.Entry(self.config_entry_frame, textvariable=self.config_entry_var, width=30)
        self.config_entry.grid(row=0, column=0, sticky=(tk.W, tk.E))

        self.configure_button = ttk.Button(self.config_entry_frame, text="Configure...", 
                command=self.show_advanced_config, width=12)
        self.configure_button.grid(row=0, column=1, padx=(5, 0))
        
        # Row 3: Start Logging
        self.start_label = ttk.Label(cmd_frame, text="3.")
        self.start_label.grid(row=3, column=0, sticky=tk.W, padx=(0, 5))
        self.start_button = ttk.Button(cmd_frame, text="Start Logging", command=self.start_logging, width=20, state='disabled')
        self.start_button.grid(row=3, column=1, padx=5, pady=5)

        self.logging_status_label = ttk.Label(cmd_frame, text="Begin data logging")
        self.logging_status_label.grid(row=3, column=2, sticky=tk.W, padx=5)

        self.logging_status_label.config(text=f"Begin data logging with path {self.config_entry_var.get()}")
        self.config_entry_var.trace_add("write", lambda *args: self.logging_status_label.config(
            text=f"Begin data logging with path {self.config_entry_var.get()}"))

        # Compact separator between Start and Stop
        ttk.Separator(cmd_frame, orient='horizontal').grid(row=4, column=0, columnspan=3, sticky="ew", pady=10)

        # Row 3: Stop Logging
        self.stop_label = ttk.Label(cmd_frame, text="4.")
        self.stop_label.grid(row=5, column=0, sticky=tk.W, padx=(0, 5))
        self.stop_button = ttk.Button(cmd_frame, text="Stop Logging", command=self.stop_logging, width=20, state='disabled')
        self.stop_button.grid(row=5, column=1, padx=5, pady=5)
        ttk.Label(cmd_frame, text="Stop the logging process").grid(row=5, column=2, sticky=tk.W, padx=5)
        
        # Row 4: Fetch
        self.fetch_label = ttk.Label(cmd_frame, text="5.")
        self.fetch_label.grid(row=6, column=0, sticky=tk.W, padx=(0, 5))
        self.fetch_button = ttk.Button(cmd_frame, text="Load Data", command=self.fetch_data, width=20, state='disabled')
        self.fetch_button.grid(row=6, column=1, padx=5, pady=5)

        # Progress section
        self.progress_frame = ttk.LabelFrame(cmd_frame, text="Download Progress", padding="5")
        self.progress_frame.grid(row=7, column=0, columnspan=3, sticky=(tk.W, tk.E), padx=5, pady=(5,0))
        self.progress_frame.grid_remove()

        # Overall progress
        ttk.Label(self.progress_frame, text="Overall:").grid(row=0, column=0, sticky=tk.W)
        self.overall_progress = ttk.Progressbar(self.progress_frame, length=400, mode='determinate')
        self.overall_progress.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=5)
        self.overall_label = ttk.Label(self.progress_frame, text="")
        self.overall_label.grid(row=0, column=2, sticky=tk.W)

        # Current file progress
        ttk.Label(self.progress_frame, text="Current File:").grid(row=1, column=0, sticky=tk.W)
        self.file_progress = ttk.Progressbar(self.progress_frame, length=400, mode='determinate')
        self.file_progress.grid(row=1, column=1, sticky=(tk.W, tk.E), padx=5)
        self.file_label = ttk.Label(self.progress_frame, text="")
        self.file_label.grid(row=1, column=2, sticky=tk.W)

        self.progress_frame.columnconfigure(1, weight=1)

        self.logging_serial = None
        self.serial_entry.bind('<KeyRelease>', self.on_serial_change)

        fetch_frame = ttk.Frame(cmd_frame)
        fetch_frame.grid(row=6, column=2, columnspan=3, sticky=(tk.W, tk.E), padx=5)
        cmd_frame.columnconfigure(2, weight=1)  
        fetch_frame.columnconfigure(1, weight=1) 

        ttk.Label(fetch_frame, text="Path:").grid(row=0, column=0, padx=(0, 5))
        self.output_entry = ttk.Entry(fetch_frame, width=150) 
        self.output_entry.grid(row=0, column=1, sticky="ew")  
        #ttk.Button(fetch_frame, text="Browse...", command=self.browse_output).grid(row=0, column=2, padx=(5, 0))
        self.browse_button = ttk.Button(fetch_frame, text="Browse...", command=self.browse_output)
        self.browse_button.grid(row=0, column=2, padx=(5, 0))
        
        # Output Section
        output_frame = ttk.LabelFrame(main_frame, text="Output", padding="5")
        output_frame.grid(row=3, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 5))
        output_frame.columnconfigure(0, weight=1)
        output_frame.rowconfigure(0, weight=1)
        self.output_text = scrolledtext.ScrolledText(output_frame, height=5, wrap=tk.WORD, state='disabled')
        self.output_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # Clear output button
        #ttk.Button(output_frame, text="Clear Output", command=self.clear_output).grid(row=1, column=0, pady=(5, 0))
        self.clear_button = ttk.Button(output_frame, text="Clear Output", command=self.clear_output)
        self.clear_button.grid(row=1, column=0, pady=(5, 0), sticky=tk.W)

        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        status_bar = ttk.Label(main_frame, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status_bar.grid(row=4, column=0, sticky=(tk.W, tk.E))

        # Erase Memory and About button frame
        bottom_frame = ttk.Frame(main_frame, padding="5")
        bottom_frame.grid(row=5, column=0, sticky=(tk.W, tk.E))  
        bottom_frame.columnconfigure(0, weight=1)
        
        # About button on the left
        #ttk.Button(bottom_frame, text="About", command=self.show_about, width=10).grid(row=0, column=0, sticky=tk.W)
        self.about_button = ttk.Button(bottom_frame, text="About", command=self.show_about, width=10)
        self.about_button.grid(row=0, column=0, sticky=tk.W)


        # Battery status label
        self.battery_label = ttk.Label(bottom_frame, text="Battery: --", foreground="gray")
        self.battery_label.grid(row=0, column=1, padx=(10, 5), sticky=tk.W)

        # Erase button on the right
        self.erase_button = ttk.Button(bottom_frame, text="Erase Memory", command=self.erase_memory, width=20)
        self.erase_button.grid(row=0, column=3, padx=5, sticky=tk.E)

        # Force checkbox 
        self.force_var = tk.BooleanVar()
        self.force_check = ttk.Checkbutton(bottom_frame, text="Force (skip confirmation)", variable=self.force_var)
        self.force_check.grid(row=0, column=2, padx=5, sticky=tk.E)
        self.force_check.grid_remove()
        
        # Configure row weights for resizing
        main_frame.rowconfigure(3, weight=1)

        # Set up queue for logging from background threads
        self._log_queue = queue.Queue()
        _fh = FileHandler(_log_path, mode='w') # Open in write mode to start fresh each time
        _fh.setLevel(logging.DEBUG)
        _fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        logging.getLogger().addHandler(_fh)

        # Start log processing
        self._process_log_queue()

    def _process_log_queue(self):
        """Drain the log queue on the main thread - called repeatedly via after()"""
        try:
            maxloop=1000
            while self._log_queue.qsize() > 0 and maxloop > 0:
                maxloop -= 1
                msg = self._log_queue.get_nowait()
                if isinstance(msg, logging.LogRecord):
                    self._log_file_handler.emit(msg)
                else:
                    print(f"Processing log message: {msg}\n")  # Debug print
                    msg_type, args = msg[0], msg[1]
                    if msg_type == 'log':
                        self.log_output(args)
                    elif msg_type == 'status':
                        self.status_var.set(args)
                #print(f"queue size after processing: {self._log_queue.qsize()}, maxloop: {maxloop}\n")  # Debug print
        except queue.Empty:
            pass
        except Exception as e:
            print(f"Error processing log queue: {e}")
            print(traceback.format_exc())
            print()
        finally:
            self.root.after(50, self._process_log_queue)

    def update_button_states(self):
        """Update button states based on connection and logging status"""
        if not self.device_connected:
            # Not connected - disable everything except Connect
            self.start_button.config(state='disabled')
            self.stop_button.config(state='disabled')
            self.fetch_button.config(state='disabled')
            if hasattr(self, 'config_button'):
                self.config_button.config(state='disabled')
            if hasattr(self, 'erase_button'):
                self.erase_button.config(state='disabled')
            if hasattr(self, 'configure_button'):
                self.configure_button.config(state='disabled')
            #self.connection_status_label.config(text="Not connected - click Connect first", foreground="red")
        elif self.logging_active:
            # Connected and logging - only Stop should be enabled
            self.connect_button.config(state='normal')
            self.start_button.config(state='disabled')
            self.stop_button.config(state='normal')
            self.fetch_button.config(state='disabled')
            self.about_button.config(state='normal')
            self.browse_button.config(state='normal')
            if hasattr(self, 'config_button'):
                self.config_button.config(state='disabled')
            if hasattr(self, 'erase_button'):
                self.erase_button.config(state='disabled')
            if hasattr(self, 'configure_button'):
                self.configure_button.config(state='disabled')
            #self.connection_status_label.config(text="Device connected - Logging active", foreground="green")
        else:
            # Connected but not logging - Start and Load Data enabled
            self.connect_button.config(state='normal')
            self.start_button.config(state='normal')
            self.stop_button.config(state='disabled')
            self.fetch_button.config(state='normal')
            self.clear_button.config(state='normal')
            self.about_button.config(state='normal')
            self.browse_button.config(state='normal')
            if hasattr(self, 'config_button'):
                self.config_button.config(state='normal')
            if hasattr(self, 'erase_button'):
                self.erase_button.config(state='normal')
            if hasattr(self, 'configure_button'):
                self.configure_button.config(state='normal')
            #self.connection_status_label.config(text="Device connected - Ready", foreground="green")

    def disable_all_buttons(self):
        """Disable all action buttons immediately"""
        def _disable():
            for btn_name in [
                'connect_button',
                'start_button',
                'stop_button',
                'fetch_button',
                'config_button',
                'erase_button',
                'configure_button',
                'browse_button',
                'about_button',
                'clear_button'
            ]:
                btn = getattr(self, btn_name, None)
                if btn:
                    btn.config(state='disabled')
        self.root.after(0, _disable)
    
    def on_serial_change(self, event=None):
        """Called when serial number entry is modified"""
        current_serial = self.serial_entry.get().strip()
        # If serial number changed, reset connection status
        if hasattr(self, 'last_connected_serial'):
            if current_serial != self.last_connected_serial:
                self.device_connected = False
                self.logging_active = False
                self.logging_serial = None
                self.update_button_states()
                self.log_output(f"Serial number changed. Please click Connect again.\n")
        if hasattr(self, 'battery_label'):
            self.battery_label.config(text="Battery: --", foreground="gray")


    def show_advanced_config(self):
        """Show the advanced configuration dialog"""
        current_config = self.config_entry.get().strip()
        dialog = AdvancedConfigDialog(self.root, current_config)
        self.root.wait_window(dialog.dialog)
        
        if dialog.result is not None:
            self.config_entry.delete(0, tk.END)
            # Convert multiline to single line with spaces
            config_text = ' '.join(dialog.result.split())
            self.config_entry.insert(0, config_text)
            self.log_output(f"Configuration updated: {config_text}\n")

    def toggle_advanced_ui(self):
        """Toggle visibility of advanced UI elements"""
        if self.advanced_ui_var.get():
            self.config_label.grid()
            self.config_button.grid()
            self.config_entry_frame.grid() 
            self.verbose_check.grid()
            self.force_check.grid()
            self.start_label.config(text="4.")
            self.stop_label.config(text="5.")
            self.fetch_label.config(text="6.")
            self.update_button_states()
        else:
            self.config_label.grid_remove()
            self.config_button.grid_remove()
            self.config_entry_frame.grid_remove()
            self.verbose_check.grid_remove()
            self.force_check.grid_remove()
            self.start_label.config(text="3.")
            self.stop_label.config(text="4.")
            self.fetch_label.config(text="5.")
    
    def log_output(self, message, newline=True):
        """Add message to output text widget"""
        # Filter out specific error messages
        lines_to_filter = [
            "TimelineJsonFormatter::embedAttributes: invalid map<K, T> key"
        ]
        
        # Check if message should be filtered
        for filter_text in lines_to_filter:
            if filter_text in message:
                return 
            
        self.output_text.configure(state='normal')
        if newline:
            self.output_text.insert(tk.END, message + "\n")
        else:
            self.output_text.insert(tk.END, message)
        self.output_text.see(tk.END)
        self.output_text.configure(state='disabled')
    
    def clear_output(self):
        """Clear the output text widget"""
        self.output_text.configure(state='normal')
        self.output_text.delete(1.0, tk.END)
        self.output_text.configure(state='disabled')
    
    def get_serial_number(self):
        """Get serial number from entry field"""
        serial = self.serial_entry.get().strip()
        if not serial:
            messagebox.showwarning("Warning", "Please enter a serial number")
            return None
        return serial
    
    def logging_data(self):
        """Print dots to show logging is active"""
        if hasattr(self, 'logging_active') and self.logging_active:
            # Alternate between two different marks
            if not hasattr(self, 'logging_counter'):
                self.logging_counter = 0
            
            mark = "*" if self.logging_counter % 2 == 0 else "."
            self.log_output(mark, newline=False)
            self.logging_counter += 1
            
            self.root.after(2000, self.logging_data)

        if hasattr(self, 'fetching_active') and self.fetching_active:
            # Alternate between two different marks
            if not hasattr(self, 'logging_counter'):
                self.logging_counter = 0
            
            mark = "*" if self.logging_counter % 2 == 0 else "."
            self.log_output(mark, newline=False)
            self.logging_counter += 1
            
            self.root.after(2000, self.logging_data)

    def extract_utc_time_from_json(self, json_file):
        """Extract UTC time from JSON file"""
        try:
            with open(json_file, 'r') as f:
                content = json.load(f)
                time_detailed = {}
                samples = content.get("Samples", [])
                for sample in samples:
                    if "TimeDetailed" in sample:
                        time_detailed = sample["TimeDetailed"]
                        break
                utc_time = time_detailed.get("utcTime", "")
                if utc_time:
                    utc_time = int(utc_time)
                    utc_time_str = datetime.fromtimestamp(utc_time / 1_000_000,tz=UTC).strftime('%Y-%m-%d_%H%M%S')
                    return utc_time_str
        except Exception as e:
            self._log_queue.put(('log', f"Error extracting UTC time from JSON: {str(e)}\n"))
        return None

    def rename_files_with_utc(self, base_file, utc_time):
        """Rename related files (JSON, CSV, EDF) to include UTC time"""
        try:
            dir_path = os.path.dirname(base_file)
            base_name = os.path.splitext(os.path.basename(base_file))[0]
            
            parts = base_name.split('_')
            if len(parts) >= 4:
                new_base = '_'.join(parts[:4])
                new_name = f"{new_base}_{utc_time}"
                
                # Check if already correctly named
                if base_name == new_name:
                    self._log_queue.put(('log', f"File already has correct UTC timestamp: {base_name}\n"))
                    return base_file
                
                # Only handle JSON, CSV, and EDF (not SBEM)
                extensions = ['.json', '.csv', '.edf']
                
                for ext in extensions:
                    old_file = os.path.join(dir_path, f"{base_name}{ext}")
                    new_file = os.path.join(dir_path, f"{new_name}{ext}")
                    
                    if os.path.exists(old_file):
                        if old_file == new_file:
                            # Already correctly named
                            continue
                        
                        if os.path.exists(new_file):
                            # Target already exists - don't rename, just delete the duplicate
                            self._log_queue.put(('log', f"Target exists, removing duplicate: {os.path.basename(old_file)}\n"))
                            os.remove(old_file)
                        else:
                            # Rename to correct name
                            os.rename(old_file, new_file)
                            self._log_queue.put(('log', f"Renamed: {os.path.basename(old_file)} -> {os.path.basename(new_file)}\n"))
                
                return new_file
                    
        except Exception as e:
            self._log_queue.put(('log', f"Error renaming files: {str(e)}\n"))
            return None
    
    @async_handler
    async def check_status(self):
        # Disable immediately to prevent double clicks
        self.disable_all_buttons()
        if self.verbose_var.get():
            logging.getLogger().setLevel(logging.DEBUG)
        """Check device status"""
        try:
            serial = self.serial_entry.get().strip()
            logging.getLogger(__name__).info(f"check_status called, serial='{serial}")

            if not serial:
                messagebox.showwarning("Warning", "Please enter a valid serial number.")
                self.root.after(0, self.status_var.set, "Missing serial number.")
                self.device_connected = False
                self.update_button_states()
                return
            
            self.root.after(0, self.log_output, "Connecting sensor and loading status. Please note that battery state may take a moment to load.")
            self.root.after(0, self.status_var.set, "Connecting sensor and loading status.")
                
            # Capture stdout to show in GUI
            output = io.StringIO()
            with redirect_stdout(output):
                status = await tool.fetch_status_and_battery(serial=serial, args=None)

            # Handle tool errors
            if isinstance(status, dict) and not status.get("success", True):
                error_msg = status.get("error", "Status fetch failed (unknown error).")

                # Check if it's a characteristic UUID error
                if "characteristic" in error_msg.lower() or "uuid" in error_msg.lower():
                    messagebox.showerror(
                        "Characteristic UUID Error",
                        f"The firmware on the sensor does not have GSP protocol.\n"
                        f"Please update the sensor to latest default firmware.\n"
                    )
                else:
                    messagebox.showerror(
                        "Connection Error",
                        f"Failed to connect to device.\n\n{error_msg}"
                    )

                self.root.after(0, self.log_output, f"Error during status fetch: {error_msg}\n")
                self.root.after(0, self.status_var.set, "Status fetch failed.")
                self.device_connected = False
                self.update_button_states()
                return
            
            # Check if product name is Movesense 
            product_name = status.get('product_name', '')
            if product_name != 'Movesense':
                messagebox.showerror(
                    "Incompatible Device", 
                    f"Please use a Movesense Flash sensor."
                )
                self.root.after(0, self.log_output, 
                    f"Error: Incompatible device detected.\n")
                self.root.after(0, self.status_var.set, "Incompatible device.")
                self.device_connected = False
                self.update_button_states()
                return
            
            # Check if app verison supports datalogging
            app_version = status.get('app_version', '')

            # Handle non-standard responses
            if not app_version or app_version.lower() == 'hello':
                messagebox.showerror(
                    "Unsupported App Version", 
                    f"Movesense Flash Datalogger Tool requires app version 1.0.1 or higher (release 2.3.1).\n"
                    f"Please update the device firmware."
                )
                self.root.after(0, self.log_output,
                    f"Error: Unsupported or unknown app version detected.\n")
                self.root.after(0, self.status_var.set, "Unsupported app version.")
                self.device_connected = False
                self.update_button_states()
                return
            # Parse version and compare
            try:
                if version.parse(app_version) < version.parse('1.0.1'):
                    messagebox.showerror(
                        "Unsupported App Version",
                        f"Movesense Flash Datalogger Tool requires app version 1.0.1 or higher (release 2.3.1).\n"
                        f"Please update the device firmware."
                    )
                    self.root.after(0, self.log_output,
                        f"Error: Unsupported app version detected.\n")
                    self.root.after(0, self.status_var.set, "Unsupported app version.")
                    self.device_connected = False
                    self.update_button_states()
                    return
            except Exception as e:
                messagebox.showerror(
                    "Invalid App Version",
                    f"Please ensure device firmware is up to date. Detected app version: {app_version}"
                )
                self.root.after(0, self.log_output,
                    f"Error: Invalid app version format detected: {app_version}\n")
                self.root.after(0, self.status_var.set, "Invalid app version.")
                self.device_connected = False
                self.update_button_states()
                return
            
            # Connection successful!
            self.device_connected = True
            self.last_connected_serial = serial
            logging.getLogger(__name__).info(f"Connected successfully to {serial}, dlstate={status.get('dlstate')}, app_version={status.get('app_version')}")

            # Sync logging_active with device state
            dlstate = status.get('dlstate', 1)

            if dlstate == 3:
                self.logging_active = True
                self.logging_serial = serial
                #self.fetch_button.config(state='disabled')
                self.root.after(0, self.log_output, 
                    f"Device is currently logging. Use 'Stop Logging' to stop.\n")
                self.root.after(2000, self.logging_data)
            else:
                self.logging_active = False
                self.logging_serial = None
                #self.fetch_button.config(state='normal')

            with redirect_stdout(output):
                print(f"Device {serial} status:")
                print(f"  Protocol version: {status.get('protocol_version', 'Unknown')}")
                print(f"  Serial number: {status.get('serial_number', 'Unknown')}")
                print(f"  Product name: {status.get('product_name', 'Unknown')}")
                print(f"  App name: {status.get('app_name', 'Unknown')}")
                print(f"  App version: {status.get('app_version', 'Unknown')}")
                print(f"  DataLogger state: {tool.DL_STATES[status.get('dlstate', 1)]}")
                print(f"  Battery level: {status.get('battery_level', 'Unknown')}%")

            # Update GUI with captured output
            self.root.after(0, self.log_output, output.getvalue())
            #self.root.after(0, self.status_var.set, "Status check completed.")

            if status.get('battery_level') is not None:
                level = status.get('battery_level', '?')
                color = 'green' if isinstance(level, int) and level > 15 else 'red'
                self.root.after(0, lambda l=level, c=color: self.battery_label.config(text=f"Battery: {l}%", foreground=c))
            else:
                self.root.after(0, lambda: self.battery_label.config(text="Battery: N/A", foreground="gray"))

            self.root.after(0, self.status_var.set, "Status check completed.")

            # Update button states based on connection and logging status
            self.update_button_states()
                
        except Exception as e:
            logging.getLogger(__name__).error(f"check_status exception: {str(e)}\n{traceback.format_exc()}")
            self.root.after(0, self.log_output, f"\nError: {str(e)}\n")
            self.root.after(0, self.status_var.set, "Error occurred")
            self.device_connected = False
            self.update_button_states()

        finally:
            self.root.after(0, lambda: self.connect_button.config(state='normal'))

    @async_handler
    async  def configure_logging(self):
        """Configure logging paths"""
        if self.verbose_var.get():
            logging.getLogger().setLevel(logging.DEBUG)
        self.disable_all_buttons()  
        try:
            raw_input = self.config_entry.get().strip()
            paths = [p.strip() for p in re.split(r'[,\s]+', raw_input) if p.strip()]

            self.root.after(0, self.log_output, "Configure logging started...")
            self.root.after(0, self.status_var.set, "Configure logging started.")

            if not paths:
                messagebox.showwarning("Warning", "Please enter at least one resource path")
                return
            serial = self.serial_entry.get().strip()
            if not serial:
                messagebox.showwarning("Warning", "Please enter a valid serial number.")
                self.root.after(0, self.status_var.set, "Missing serial number.")
                return
            
            # Capture stdout to show in GUI
            output = io.StringIO()
            with redirect_stdout(output):
                result = await tool.configure_device(serial, paths=paths)
            
            # Update GUI with captured output
            self.root.after(0, self.log_output, output.getvalue())

            if isinstance(result, dict) and not result.get("success", True):
                error_msg = result.get("error", "Configuration failed (unknown error).")
                self.root.after(0, self.log_output, f"Error during configuration: {error_msg}\n")
                self.root.after(0, self.status_var.set, "Configuration failed.")
                return
        
            self.root.after(0, self.log_output, f"Logging configured for device {serial} with paths: {paths}")
            self.root.after(0, self.status_var.set, "Logging configured successfully.")
            self.logging_configured = True
               
        except Exception as e:
            error_text = f"\nError: {str(e)}\n\n{traceback.format_exc()}"
            self.root.after(0, self.log_output, error_text)
            self.root.after(0, self.status_var.set, "Error occurred.")

        finally:
            self.root.after(0, self.update_button_states)
        
    @async_handler
    async def start_logging(self):
        """Start logging"""
        logging.getLogger(__name__).info("=== start_logging called ===")
        if self.verbose_var.get():
            logging.getLogger().setLevel(logging.DEBUG)
            logging.getLogger(__name__).info("Verbose mode enabled, log level set to DEBUG")

        # Check if device is connected
        if not self.device_connected:
            logging.getLogger(__name__).info("Device not connected, aborting start_logging")
            messagebox.showwarning("Warning", "Please connect to the device first.")
            self.root.after(0, self.status_var.set, "Not connected.")
            return
        
        self.disable_all_buttons()

        try:
            serial = self.serial_entry.get().strip()
            logging.getLogger(__name__).info(f"Serial number entered: '{serial}'")
            if not serial:
                logging.getLogger(__name__).info("Serial number is empty, aborting")
                messagebox.showwarning("Warning", "Please enter a valid serial number.")
                self.root.after(0, self.status_var.set, "Missing serial number.")
                return

            if not self.logging_configured:
                logging.getLogger(__name__).info(f"Serial bytes: {serial.encode()}")
                logging.getLogger(__name__).info(f"device_connected={self.device_connected}, logging_configured={self.logging_configured}")
                logging.getLogger(__name__).info("Logging not yet configured — running configure + start flow")
                self.root.after(0, self.log_output, "Configuring and starting logging...")
                self.root.after(0, self.status_var.set, "Configuring and starting logging.")

                # Parse paths safely (split by spaces, commas, etc.)
                raw_input = self.config_entry.get().strip()
                paths = [p.strip() for p in re.split(r'[,\s]+', raw_input) if p.strip()]
                logging.getLogger(__name__).info(f"Parsed resource paths: {paths}")
                if not paths:
                    logging.getLogger(__name__).info("No paths provided, aborting")
                    messagebox.showwarning("Warning", "Please enter at least one resource path.")
                    self.root.after(0, self.status_var.set, "Missing configuration paths.")
                    return

                # Capture stdout
                output = io.StringIO()
                with redirect_stdout(output):
                    logging.getLogger(__name__).info(f"Calling tool.configure_device with serial={serial}, paths={paths}")
                    config_result = await tool.configure_device(serial, paths=paths)
                    logging.getLogger(__name__).info(f"configure_device result: {config_result}")
                    start_result = None
                    if not (isinstance(config_result, dict) and not config_result.get("success", True)):
                        logging.getLogger(__name__).info("Configuration succeeded, calling tool.start_logging")
                        # start_result = await tool.start_logging(serial, args=None)
                        logging.getLogger(__name__).info(f"About to call tool.start_logging, serial type={type(serial)}, value='{serial}'")
                        try:
                            start_result = await tool.start_logging(serial, args=None)
                        except Exception as inner_e:
                            logging.getLogger(__name__).info(f"tool.start_logging raised an exception: {type(inner_e).__name__}: {inner_e}")
                            raise
                        logging.getLogger(__name__).info(f"start_logging raw result: {start_result}, type={type(start_result)}")

                # Update GUI with captured stdout first
                self.root.after(0, self.log_output, output.getvalue())

                # Handle configuration errors
                if isinstance(config_result, dict) and not config_result.get("success", True):
                    error_msg = config_result.get("error", "Configuration failed (unknown error).")
                    logging.getLogger(__name__).info(f"Configuration error: {error_msg}")
                    self.root.after(0, self.log_output, f"Error during configuration: {error_msg}\n")
                    self.root.after(0, self.status_var.set, "Configuration failed.")
                    return

                self.logging_configured = True
                logging.getLogger(__name__).info("logging_configured set to True")

                # Handle start errors
                if isinstance(start_result, dict) and not start_result.get("success", True):
                    error_msg = start_result.get("error", "Start failed (unknown error).")
                    logging.getLogger(__name__).info(f"Start error: {error_msg}")
                    self.root.after(0, self.log_output, f"Error during start: {error_msg}\n")
                    self.root.after(0, self.status_var.set, "Error occurred while starting.")
                    return

                # Success path
                logging.getLogger(__name__).info(f"Logging successfully started on device {serial}")
                self.root.after(0, self.log_output, f"Logging started on device {serial}. Recording data...")
                self.root.after(0, self.status_var.set, "Logging started.")
                self.logging_active = True
                self.logging_serial = serial
                self.update_button_states()
                self.root.after(2000, self.logging_data)
                logging.getLogger(__name__).info("Button states updated, logging_data scheduled in 2000ms")

            else:
                # Already configured — just start logging
                logging.getLogger(__name__).info("Logging already configured — skipping configure, calling start directly")
                self.root.after(0, self.log_output, f"Starting logging on device {serial}...")
                self.root.after(0, self.status_var.set, "Starting logging...")

                output = io.StringIO()
                with redirect_stdout(output):
                    logging.getLogger(__name__).info(f"Calling tool.start_logging with serial={serial}")
                    start_result = await tool.start_logging(serial, args=None)
                    logging.getLogger(__name__).info(f"start_logging result: {start_result}")

                self.root.after(0, self.log_output, output.getvalue())

                if isinstance(start_result, dict) and not start_result.get("success", True):
                    error_msg = start_result.get("error", "Start failed (unknown error).")
                    logging.getLogger(__name__).info(f"Start error: {error_msg}")
                    self.root.after(0, self.log_output, f"Error during start: {error_msg}\n")
                    self.root.after(0, self.status_var.set, "Error occurred while starting.")
                    return

                logging.getLogger(__name__).info(f"Logging successfully started on device {serial} (already configured path)")
                self.root.after(0, self.log_output, f"Logging started on device {serial}. Recording data...")
                self.root.after(0, self.status_var.set, "Logging started.")
                self.logging_active = True
                self.update_button_states()
                self.root.after(2000, self.logging_data)
                logging.getLogger(__name__).info("Button states updated, logging_data scheduled in 2000ms")

        except Exception as e:
            error_text = f"\nError: {str(e)}\n\n{traceback.format_exc()}"
            logging.getLogger(__name__).info(f"Exception caught in start_logging: {str(e)}")
            self.root.after(0, self.log_output, error_text)
            self.root.after(0, self.status_var.set, "Error occurred.")
        
        finally:
            # Restore correct button states
            self.root.after(0, self.update_button_states)

    @async_handler
    async def stop_logging(self):
        """Stop logging"""
        if self.verbose_var.get():
            logging.getLogger().setLevel(logging.DEBUG)

        # Check if device is connected
        if not self.device_connected:
            messagebox.showwarning("Warning", "Please connect to the device first.")
            self.root.after(0, self.status_var.set, "Not connected.")
            return
        
        self.disable_all_buttons()

        try:

            serial = self.serial_entry.get().strip()
            if not serial:
                messagebox.showwarning("Warning", "Please enter a valid serial number")
                return
            
            output = io.StringIO()
            self.root.after(0, self.log_output, f"\nStopping logging on device {serial}...")
            self.root.after(0, self.status_var.set, "Stopping logging.")

            with redirect_stdout(output):
                result = await tool.stop_logging(serial=serial, args=None)

            self.root.after(0, self.log_output, output.getvalue())
            
            # Check result structure
            if isinstance(result, dict) and not result.get("success", True):
                error_msg = result.get("error", "Unknown error")
                self.root.after(0, self.log_output, f"Error: {error_msg}\n")
                self.root.after(0, self.status_var.set, "Error occurred")
            else:
                self.root.after(0, self.log_output, f"Logging stopped on device {serial}")
                self.root.after(0, self.status_var.set, "Logging stopped")
                self.logging_active = False
                self.logging_serial = None
                self.logging_configured = False
                self.update_button_states()

        except Exception as e:
            import traceback
            error_text = f"Error: {str(e)}\n\n{traceback.format_exc()}"
            self.root.after(0, self.log_output, error_text)
            self.root.after(0, self.status_var.set, "Error occurred")

        finally:
            # Restore correct button states
            self.root.after(0, self.update_button_states)
        
    @async_handler
    async def fetch_data(self):
        """Fetch data from devices"""
        self.disable_all_buttons()
        if self.verbose_var.get():
            logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger(__name__).info(f"fetch_data called")

        # Check if device is connected
        if not self.device_connected:
            messagebox.showwarning("Warning", "Please connect to the device first.")
            self.root.after(0, self.status_var.set, "Not connected.")
            return
        
        serial = self.serial_entry.get().strip()
        if not serial:
            logging.getLogger(__name__).warning("Fetch aborted: serial missing")
            messagebox.showwarning("Warning", "Please enter a valid serial number")
            return
        
        # Add this check at the beginning
        if self.logging_active:
            logging.getLogger(__name__).warning("Fetch aborted: logging is active")
            messagebox.showwarning(
                "Logging Active", 
                "Cannot load data while logging is active. Please stop logging first."
            )
            self.root.after(0, self.status_var.set, "Cannot load data - logging is active")
            return

        # Get output directory
        raw_path = self.output_entry.get().strip()
        logging.getLogger(__name__).info(f"Raw output path: '{raw_path}'")
        if not raw_path:
            # Default to the directory where the app/exe resides
            if getattr(sys, 'frozen', False):
                # Running as a PyInstaller .exe
                output_dir = os.path.dirname(sys.executable)
            else:
                # Running as a .py script
                output_dir = os.path.dirname(os.path.abspath(__file__))
        else:
            output_dir = os.path.abspath(raw_path)

        logging.getLogger(__name__).info(f"Resolved output_dir: '{output_dir}'")

        # Create directory if needed
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as e:
            logging.getLogger(__name__).error(f"Failed to create output directory: {e}")
            messagebox.showerror("Invalid Path", f"Could not create output directory:\n{e}")
            self.root.after(0, self.update_button_states)
            return

        self.root.after(0, self.log_output, f"Using directory: {output_dir}\n")

        try:
            logging.getLogger(__name__).info("Starting fetch process")
            self.root.after(0, self.log_output, f"\nLoading data from device {serial}.")
            self.root.after(0, self.status_var.set, "Loading data from device")

            # Reset progress bars
            self.root.after(0, self.reset_progress_bars)
            self.root.after(0, self.progress_frame.grid)

            # Set logging active flag 
            self.fetching_active = True
            logging.getLogger(__name__).info("Scheduling logging_data watchdog in 2000 ms")
            self.root.after(2000, self.logging_data)

            # Capture stdout to show in GUI
            output = io.StringIO()

            # Progress tracking variables
            self.total_logs = 0
            self.total_bytes = 0

            def progress_callback(bytes_downloaded, log_id, total_size, current_log_num=1, total_log_count=1, file_sizes=None, bytes_downloaded_so_far=0, total_bytes=0, has_known_size=True):   
                # Set total logs if not set (first callback)
                if self.total_logs == 0 and total_log_count > 0:
                    self.total_logs = total_log_count
                    self.total_bytes = total_bytes
                    #logging.getLogger(__name__).debug(f"progress_callback init: total_logs={total_log_count}, total_bytes={total_bytes}")

                # Update current file progress
                if total_size > 0:
                    file_percent = min(100, (bytes_downloaded / total_size) * 100)
                    #logging.getLogger(__name__).debug(f"progress_callback: log_id={log_id}, {bytes_downloaded}/{total_size} bytes ({file_percent:.1f}%)")
                    self.root.after(0, self.update_file_progress, 
                        file_percent, bytes_downloaded, total_size, log_id)
                
                # Update overall progress
                if self.total_logs > 0 and self.total_bytes > 0:
                    # Byte-based progress for known files
                    total_bytes_downloaded = bytes_downloaded_so_far + bytes_downloaded
                    overall_percent = min(100, (total_bytes_downloaded / self.total_bytes) * 100)
                    self.root.after(0, self.update_overall_progress, 
                        overall_percent, current_log_num, self.total_logs)
                elif self.total_logs > 0:
                    # Fallback to file-count-based progress if total bytes is 0 or unknown
                    overall_percent = min(100, ((current_log_num - 1 + (bytes_downloaded / max(total_size, 1))) / self.total_logs) * 100)
                    self.root.after(0, self.update_overall_progress, 
                        overall_percent, current_log_num, self.total_logs)

                if has_known_size:
                    self.root.after(0, self.status_var.set, 
                        f"Fetching log {log_id}: {bytes_downloaded:,} / {total_size:,} bytes")  
                else:
                    # For unknown size logs (discovered by probing)
                    self.root.after(0, self.status_var.set, 
                        f"Fetching log {log_id} (discovered): {bytes_downloaded:,} bytes")  

            with redirect_stdout(output):
                # Step 1: Fetch data
                logging.getLogger(__name__).info(f"Step 1: Starting BLE fetch for serial={serial}, output_dir={output_dir}")
                result = await tool.fetch_data(serial=serial, args=None, output_dir=output_dir, progress_callback=progress_callback)
                logging.getLogger(__name__).info(f"Step 1: fetch_data result={result}")
                self.logging_configured = False
               
            # Update GUI with fetch output
            self.root.after(0, self.hide_progress_area) 
            logging.getLogger(__name__).info(f"Step 1: BLE fetch complete, success={result.get('success')}, files={result.get('files_fetched')}")
            self.root.after(0, self.log_output, output.getvalue())

            if not result.get("success", False):
                self.root.after(0, self.log_output, f"\nError fetching data: {result.get('error', 'Unknown error')}\n")
                self.root.after(0, self.status_var.set, "Error occurred during data fetch")
                self.fetching_active = False
                return
            
            #self.root.after(0, self.log_output, "\nLogging completed successfully.")

            # Steps 2-4: Run all file conversions
            async def file_format_conversion():
                try:
                    # Step 2: Convert SBEM to JSON
                    self._log_queue.put(('log', "\n--- Converting SBEM to JSON ---"))
                    self._log_queue.put(('status', "Converting SBEM to JSON..."))

                    sbem_folder = os.path.join(output_dir, "sbem-files")
                    if not os.path.exists(sbem_folder):
                        os.makedirs(sbem_folder)
                        self._log_queue.put(('log', f"Created folder: {sbem_folder}"))
                    
                    sbem_files = []
                    for root_dir, dirs, files in os.walk(output_dir):
                        if 'sbem-files' in root_dir:
                            continue
                        for file in files:
                            if file.endswith('.sbem'):
                                sbem_files.append(os.path.join(root_dir, file))

                    logging.getLogger(__name__).info(f"Step 2: Found {len(sbem_files)} SBEM files to convert")
                    if not sbem_files:
                        self._log_queue.put(('log', "No SBEM files found to convert."))
                    else:
                        for sbem_file in sbem_files:
                            #logging.getLogger(__name__).info(f"Step 2: Converting {sbem_file}")
                            logging.getLogger(__name__).info(f"Step 2: Converting {sbem_file} (size={os.path.getsize(sbem_file)} bytes)")
                            self._log_queue.put(('log', f"Converting: {sbem_file}"))

                            original_dir = os.path.dirname(sbem_file)
                            sbem_filename = os.path.basename(sbem_file)
                            json_filename = os.path.splitext(sbem_filename)[0] + '.json'
                            json_file = os.path.join(original_dir, json_filename)

                            if getattr(sys, 'frozen', False):
                                application_path = sys._MEIPASS
                            else:
                                application_path = os.path.dirname(os.path.abspath(__file__))

                            sbem2json_exe = os.path.join(application_path, SBEM2JSON_EXECUTABLE)
                            converter_cmd = f'"{sbem2json_exe}" --sbem2json "{sbem_file}" --output "{json_file}"'
                            
                            conv_process = await asyncio.create_subprocess_shell(
                                converter_cmd,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT
                            )
                            conv_output, _ = await conv_process.communicate()
                            if conv_output:
                                conv_output = conv_output.decode()
                                stdout_lines = conv_output.strip().splitlines()
                                for line in stdout_lines:
                                    if "invalid map<K, T>" not in line and "SmlDescriptor::parseModifier:" not in line:
                                        self._log_queue.put(('log', line))
                            
                            #await asyncio.sleep(0.3)
                            if conv_process.returncode != 0:
                                logging.getLogger(__name__).warning(f"Step 2: SBEM conversion failed for {sbem_filename}, returncode={conv_process.returncode}")
                                self._log_queue.put(('log', f"Warning: Conversion failed for {sbem_filename}\n"))
                            else:
                                logging.getLogger(__name__).info(f"Step 2: Created {json_file}")
                                self._log_queue.put(('log', f"\nCreated: {json_file}\n"))
                                
                                utc_time = self.extract_utc_time_from_json(json_file)
                                if utc_time:
                                    self._log_queue.put(('log', f"UTC time from file: {utc_time}"))
                                    self.rename_files_with_utc(json_file, utc_time)
                                    # Reconstruct renamed JSON path directly
                                    dir_path = os.path.dirname(json_file)
                                    base_name = os.path.splitext(os.path.basename(json_file))[0]
                                    parts = base_name.split('_')
                                    if len(parts) >= 4:
                                        new_base = '_'.join(parts[:4])
                                        json_file = os.path.join(dir_path, f"{new_base}_{utc_time}.json")
                                    logging.getLogger(__name__).info(f"Step 2: Renamed files based on UTC time for {json_file}")

                                
                                new_sbem_path = os.path.join(sbem_folder, sbem_filename)
                                
                                try:
                                    if os.path.exists(new_sbem_path):
                                        os.remove(sbem_file)
                                        self._log_queue.put(('log', f"Removed SBEM (already archived): {sbem_filename}"))
                                    else:
                                        os.rename(sbem_file, new_sbem_path)
                                        self._log_queue.put(('log', f"Moved SBEM to: {new_sbem_path}"))
                                except Exception as e:
                                    self._log_queue.put(('log', f"Warning: Could not move SBEM file: {str(e)}"))

                                # Move JSON to json-files folder only in Advanced UI mode
                                if self.advanced_ui_var.get():
                                    json_folder = os.path.join(output_dir, "json-files")
                                    os.makedirs(json_folder, exist_ok=True)
                                    new_json_path = os.path.join(json_folder, os.path.basename(json_file))
                                    try:
                                        if os.path.exists(new_json_path):
                                            self._log_queue.put(('log', f"JSON already archived: {os.path.basename(json_file)}"))
                                        else:
                                            os.rename(json_file, new_json_path)
                                            json_file = new_json_path  # update reference for any further use
                                            self._log_queue.put(('log', f"Moved JSON to: {new_json_path}"))
                                    except Exception as e:
                                        self._log_queue.put(('log', f"Warning: Could not move JSON file: {str(e)}"))

                    # Step 3: Convert JSON to CSV
                    self._log_queue.put(('log', "\n--- Converting JSON to CSV ---"))
                    self._log_queue.put(('status', "Converting JSON to CSV..."))
                    self.progress_frame.grid()  # Show progress bars for conversion steps
                    advanced_ui = self.advanced_ui_var.get()
                    json_folder = os.path.join(output_dir, "json-files")
                    csv_folder = os.path.join(output_dir, "csv-files") if advanced_ui else output_dir
                    if advanced_ui:
                        os.makedirs(csv_folder, exist_ok=True)

                    json_files = []
                    scan_dirs = [json_folder] if os.path.exists(json_folder) else [output_dir]
                    for scan_dir in scan_dirs:
                        for root_dir, dirs, files in os.walk(scan_dir):
                            if any(x in root_dir for x in ['venv', '.venv', 'site-packages', 'sbem-files']):
                                continue
                            for file in files:
                                if file.endswith('.json'):
                                    json_path = os.path.join(root_dir, file)
                                    csv_path = os.path.join(csv_folder, os.path.splitext(file)[0] + '.csv')
                                    if os.path.exists(csv_path):
                                        self._log_queue.put(('log', f"Skipping {file} - CSV already exists"))
                                    else:
                                        json_files.append(json_path)

                    logging.getLogger(__name__).info(f"Step 3: Found {len(json_files)} JSON files to convert to CSV")
                    if not json_files:
                        self._log_queue.put(('log', "No JSON files found to convert"))
                    else:
                        counter = 0
                        
                        self.reset_progress_bars()
                        for json_file in json_files:
                            self.root.after(0, self.update_overall_progress, counter*100/len(json_files), counter, len(json_files))
                            #logging.getLogger(__name__).info(f"Step 3: Converting {json_file}")
                            logging.getLogger(__name__).info(f"Step 3: Converting {json_file} (size={os.path.getsize(json_file)} bytes)")
                            self._log_queue.put(('log', f"Converting: {json_file}"))

                            csv_file = os.path.join(csv_folder, os.path.splitext(os.path.basename(json_file))[0] + '.csv')
                            try:
                                await convert_json_to_csv(input_file=json_file, output_file=csv_file, progress_callback=self.update_csv_progress)
                                logging.getLogger(__name__).info(f"Step 3: Created {csv_file}")
                                self._log_queue.put(('log', f"\nCreated: {csv_file}"))
                                if not advanced_ui:
                                    try:
                                        os.remove(json_file)
                                        logging.getLogger(__name__).info(f"Step 3: Removed intermediate JSON: {json_file}")
                                    except Exception as e:
                                        logging.getLogger(__name__).warning(f"Step 3: Could not remove intermediate JSON {json_file}: {str(e)}")
                            except Exception as e:
                                logging.getLogger(__name__).error(f"Step 3: CSV conversion failed for {json_file}: {str(e)}")
                                self._log_queue.put(('log', f"Warning: CSV conversion failed for {json_file}: {str(e)}"))
                            counter += 1
                            self.root.after(0, self.update_overall_progress, counter*100/len(json_files), counter, len(json_files))
                    
                    # Step 4: Convert CSV to EDF
                    self.hide_progress_area()  # Hide progress bars- edf is fast enough that we don't need them for this step
                    self._log_queue.put(('log', "\n--- Converting CSV to EDF ---"))
                    self._log_queue.put(('status', "Converting CSV to EDF..."))

                    csv_files = []
                    csv_files_total = 0
                    csv_folder = os.path.join(output_dir, "csv-files") if advanced_ui else output_dir
                    scan_root = csv_folder if os.path.exists(csv_folder) else output_dir
                    for root_dir, dirs, files in os.walk(scan_root):
                        if any(x in root_dir for x in ['venv', 'site-packages']):
                            continue
                        for file in files:
                            file_lower = file.lower()
                            is_ecg = (file_lower.endswith('.csv') and 
                                    ('measecg' in file_lower or 'measecgmv' in file_lower or 
                                    (file_lower.startswith('log_') and 'ecg' in file_lower)))
                            if is_ecg:
                                csv_files_total += 1
                                csv_path = os.path.join(root_dir, file)
                                edf_path = os.path.splitext(csv_path)[0] + '.edf'
                                if os.path.exists(edf_path):
                                    self._log_queue.put(('log', f"Skipping {file} - EDF already exists"))
                                else:
                                    csv_files.append(csv_path)

                    logging.getLogger(__name__).info(f"Step 4: Found {len(csv_files)} ECG CSV files to convert to EDF (total ECG CSVs: {csv_files_total})")
                    if not csv_files:
                        if csv_files_total == 0:
                            self._log_queue.put(('log', "No CSV files found in directory"))
                        else:
                            self._log_queue.put(('log', "All CSV files already converted. No new EDF files to create."))
                    else:
                        for csv_file in csv_files:
                            #logging.getLogger(__name__).info(f"Step 4: Converting {csv_file}")
                            logging.getLogger(__name__).info(f"Step 4: Converting {csv_file} (size={os.path.getsize(csv_file)} bytes)")
                            self._log_queue.put(('log', f"Converting: {csv_file}"))
                            try:
                                first_ts_ms = None
                                last_ts_ms = None
                                with open(csv_file, 'rb') as f:
                                    header = f.readline().decode('utf-8', errors='replace').strip()
                                    second_line = f.readline().decode('utf-8', errors='replace').strip()
                                    try:
                                        first_ts_ms = float(second_line.split(',')[0])
                                    except (ValueError, IndexError):
                                        pass
                                    # Seek to last ~256 bytes to find the last non-empty line
                                    f.seek(0, 2)  # end of file
                                    file_size = f.tell()
                                    seek_back = min(256, file_size)
                                    f.seek(-seek_back, 2)
                                    tail = f.read().decode('utf-8', errors='replace')
                                last_line = next(
                                    (l for l in reversed(tail.splitlines()) if l.strip()),
                                    None
                                    )
                                if last_line:
                                    try:
                                        last_ts_ms = float(last_line.split(',')[0])
                                    except (ValueError, IndexError):
                                        pass
                                parts = header.split(',')
                                utc_str = parts[5]
                                logging.getLogger(__name__).debug(f"Step 4: Parsed UTC string='{utc_str}' from header of {csv_file}")
                                utc_time_dt = datetime.strptime(utc_str[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                                logging.getLogger(__name__).debug(f"Step 4: Parsed utc_time_dt={utc_time_dt}")

                                duration_suffix = ""
                                if first_ts_ms is not None and last_ts_ms is not None:
                                    total_s = int(round((last_ts_ms - first_ts_ms) / 1000))
                                    h, rem = divmod(total_s, 3600)
                                    m, s = divmod(rem, 60)
                                    if h > 0:
                                        duration_suffix = f"_{h}h{m}min{s}s"
                                    elif m > 0:
                                        duration_suffix = f"_{m}min{s}s"
                                    else:
                                        duration_suffix = f"_{s}s"
                                    logging.getLogger(__name__).debug(f"Step 4: Recording duration={duration_suffix}")
 
                                base_name = os.path.splitext(os.path.basename(csv_file))[0] + duration_suffix
                                edf_file = os.path.join(output_dir, base_name + '.edf')

                                await csv_to_edf_plus(csv_filename=csv_file,
                                            edf_filename=edf_file,
                                            sampling_freq=None,
                                            unit='mV',
                                            scale_factor=1,
                                            recording_start=utc_time_dt)
                                logging.getLogger(__name__).info(f"Step 4: Created {edf_file}")
                                self._log_queue.put(('log', f"\nCreated: {edf_file}"))
                                # In simple mode, delete the intermediate CSV now that EDF is created
                                if not advanced_ui:
                                    try:
                                        os.remove(csv_file)
                                        logging.getLogger(__name__).info(f"Step 4: Removed intermediate CSV: {csv_file}")
                                    except Exception as e:
                                        logging.getLogger(__name__).warning(f"Step 4: Could not remove intermediate CSV {csv_file}: {str(e)}")

                            except Exception as e:
                                logging.getLogger(__name__).error(f"Step 4: EDF conversion failed for {csv_file}: {str(e)}")
                                self._log_queue.put(('log', f"Warning: EDF conversion failed for {csv_file}: {str(e)}"))

                    self.fetching_active = False
                    logging.getLogger(__name__).info("conversion_worker completed successfully")
                    self._log_queue.put(('status', "All conversions completed."))
                    self._log_queue.put(('log', "All conversions completed."))
                    self._log_queue.put(('log', "\nDone!\n"))

                except Exception as e:
                    self.fetching_active = False
                    logging.getLogger(__name__).error(f"conversion_worker exception: {str(e)}\n{traceback.format_exc()}")
                    self._log_queue.put(('log', f"\nError: {str(e)}"))
                    self._log_queue.put(('status', "Error occurred"))

            # threading.Thread(target=conversion_worker, daemon=True).start()
            # logging.getLogger(__name__).info("conversion_worker thread launched")
            await file_format_conversion()
        
        except Exception as e:
            self.fetching_active = False
            logging.getLogger(__name__).error(f"fetch_data outer exception: {str(e)}\n{traceback.format_exc()}")
            self.root.after(0, self.log_output, f"\nError: {str(e)}")
            self.root.after(0, self.status_var.set, "Error occurred")

        finally:
            self.root.after(0, self.update_button_states)
    
    @async_handler
    async def erase_memory(self):
        self.disable_all_buttons()
        """Erase device memory"""
        if self.verbose_var.get():
            logging.getLogger().setLevel(logging.DEBUG)
        if not self.force_var.get():
            result = messagebox.askyesno(
                "Confirm Erase",
                "Are you sure you want to erase all logged data?\nThis action cannot be undone!"
            )
            if not result:
                self.log_output("\nMemory erase cancelled by user\n")
                self.root.after(0, self.update_button_states)
                return
        try:
            # Get serial number first
            serial = self.serial_entry.get().strip()
            if not serial:
                self.root.after(0, lambda: messagebox.showerror("Error", "Please enter a serial number"))
                return
            
            # Update status
            self.root.after(0, self.status_var.set, "Connecting to device...")
            self.root.after(0, self.log_output, f"Attempting to connect to device {serial}...")
            self.root.after(0, self.log_output, f"Erasing memory on device {serial}...")
            self.root.after(0, self.status_var.set, "Erasing memory on device.")
            
            # Always use force=True as required by the device protocol
            output = io.StringIO()
            with redirect_stdout(output):
                await tool.erase_memory(serial=serial)
            
            # Update GUI with captured output
            self.root.after(0, self.log_output, output.getvalue())
            self.root.after(0, self.log_output, "Memory erased successfully")
            self.root.after(0, self.status_var.set, "Memory erased")
                
        except Exception as e:
            error_msg = str(e)
            self.root.after(0, self.log_output, f"\nError: {error_msg}")
            self.root.after(0, self.status_var.set, "Error occurred")
            self.root.after(0, lambda: messagebox.showerror("Error", f"Failed to erase memory: {error_msg}"))

        finally:
            self.root.after(0, self.update_button_states)
   
    def browse_output(self):
        """Browse for output directory"""
        directory = filedialog.askdirectory(title="Select Output Directory")
        if directory:
            self.output_entry.delete(0, tk.END)
            self.output_entry.insert(0, directory)
    
    def show_about(self):
        """Show the About dialog"""
        AboutDialog(self.root)

    def reset_progress_bars(self):
        """Reset progress bars"""
        self.overall_progress['value'] = 0
        self.file_progress['value'] = 0
        self.overall_label.config(text="")
        self.file_label.config(text="")

    def hide_progress_area(self):
        """Hide the download progress section after completion"""
        self.progress_frame.grid_remove()

    def update_file_progress(self, percent, downloaded, total, log_id):
        """Update file progress bar"""
        self.file_progress['value'] = percent
        self.file_label.config(
        text=f"Log {log_id}: {downloaded:,} / {total:,} bytes ({percent:.1f}%)"
        )

    def update_csv_progress(self, processed, total, stream):
        """Update CSV progress bar"""
        percent = (processed / total) * 100 if total > 0 else 0
        #logging.getLogger().debug(f"update_csv_progress: {processed}/{total} ({percent:.1f}%)")
        self.file_progress['value'] = percent
        self.file_label.config(
        text=f"CSV {stream}: {processed:,} / {total:,} rows ({percent:.1f}%)"
        )

    def update_overall_progress(self, percent, current, total):
        """Update overall progress bar"""
        #logging.getLogger().debug(f"update_overall_progress: {current}/{total} ({percent:.1f}%)")
        self.overall_progress['value'] = percent
        self.overall_label.config(
            text=f"Files: {current} / {total} ({percent:.1f}%)"
        )

root = tk.Tk()
app = DataloggerGUI(root)
async_mainloop(root)

