from PyQt5.QtCore import QThread, pyqtSignal
from pylsl import local_clock

import serial
import serial.tools.list_ports
import time
import json
import traceback
import logging
import sys
from pathlib import Path


# =====================================================
# DEBUG LOGGING SETUP
# =====================================================

def _app_base_dir() -> Path:
    """
    Returns app folder both in normal Python and Nuitka standalone exe.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    return Path(__file__).resolve().parent


LOG_FILE = _app_base_dir() / "mcu_debug.log"

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
)


def log_print(message: str):
    """
    Print to console and also write to debug log file.
    """
    print(message, flush=True)
    logging.info(message)


def log_exception(message: str):
    """
    Print traceback and write it to log file.
    """
    print(message, flush=True)
    traceback.print_exc()
    logging.exception(message)


class MCUThread(QThread):

    # pc_local_clock_timestamp, angle, normalized_load
    data = pyqtSignal(float, float, float)

    # raw JSON line for UDP forwarding
    raw = pyqtSignal(str)

    def __init__(self, port, baud, start_event):

        super().__init__()

        self.port = port
        self.baud = baud
        self.start_event = start_event

        self.running = True
        self.ser = None

        self.was_recording = False

        # ----------------------------------------
        # HX711 relative normalization settings
        # ----------------------------------------

        self.load_zero = None
        self.load_zero_samples = []

        # At 10 Hz:
        # 5 samples = about 0.5 seconds.
        # During these first samples, load output is 0.0.
        self.load_zero_sample_count = 5

        # Raw load difference that maps to +1 or -1.
        self.LOAD_SPAN = 500000.0

        # ----------------------------------------
        # Debug counters
        # ----------------------------------------

        self.raw_count = 0
        self.valid_count = 0
        self.parse_reject_count = 0
        self.empty_line_count = 0
        self.error_count = 0
        self.reconnect_count = 0

        self.last_line_time = time.monotonic()
        self.last_debug_report_time = time.monotonic()
        self.last_empty_warning_time = time.monotonic()

        log_print("[MCU DEBUG] MCUThread created")
        log_print(f"[MCU DEBUG] Log file: {LOG_FILE}")
        log_print(f"[MCU DEBUG] Initial port={self.port}, baud={self.baud}")

    # =====================================================
    # MAIN LOOP
    # =====================================================

    def run(self):

        log_print("[MCU DEBUG] Thread started")
        self.connect()

        while self.running:

            recording = self.start_event.is_set()

            # -------------------------------------------------
            # Recording inactive
            # -------------------------------------------------
            if not recording:
                if self.was_recording:
                    log_print("[MCU DEBUG] Recording became inactive")

                self.was_recording = False
                self.msleep(50)
                continue

            try:

                # -------------------------------------------------
                # Reconnect if needed
                # -------------------------------------------------
                if self.ser is None:
                    log_print("[MCU DEBUG] Serial object is None. Trying reconnect...")
                    self.connect()
                    self.msleep(500)
                    continue

                # -------------------------------------------------
                # First loop after START:
                # flush old serial samples already waiting in buffer
                # -------------------------------------------------
                if not self.was_recording:

                    log_print("[MCU DEBUG] Recording started")

                    try:
                        self.ser.reset_input_buffer()
                        self.ser.reset_output_buffer()
                        log_print("[MCU DEBUG] Serial buffers flushed at recording start")
                    except Exception:
                        log_exception("[MCU ERROR] Serial buffer flush failed")

                    self.was_recording = True

                    # Reset debug counters for this recording
                    self.raw_count = 0
                    self.valid_count = 0
                    self.parse_reject_count = 0
                    self.empty_line_count = 0
                    self.error_count = 0

                    self.last_line_time = time.monotonic()
                    self.last_debug_report_time = time.monotonic()
                    self.last_empty_warning_time = time.monotonic()

                # -------------------------------------------------
                # Read one serial line
                # -------------------------------------------------
                raw_bytes = self.ser.readline()

                if not raw_bytes:
                    self.empty_line_count += 1

                    now = time.monotonic()
                    if now - self.last_empty_warning_time >= 2.0:
                        log_print(
                            "[MCU WARNING] No serial line received for >2 seconds. "
                            f"empty_line_count={self.empty_line_count}, "
                            f"port={self.port}, "
                            f"ser_is_open={self.ser.is_open if self.ser else None}"
                        )
                        self.last_empty_warning_time = now

                    continue

                self.last_line_time = time.monotonic()

                try:
                    line = raw_bytes.decode("utf-8", errors="replace").strip()
                except Exception:
                    log_exception("[MCU ERROR] Failed to decode serial bytes")
                    continue

                if not line:
                    self.empty_line_count += 1
                    continue

                self.raw_count += 1

                # Forward raw Arduino JSON to UDP sender
                if line.startswith("{"):
                    self.raw.emit(line)
                else:
                    self.parse_reject_count += 1
                    self._periodic_debug_report(
                        extra=f"Non-JSON line rejected: {line[:250]}"
                    )
                    continue

                parsed = self.parse(line)

                if parsed is None:
                    self.parse_reject_count += 1
                    self._periodic_debug_report(
                        extra=f"JSON parse rejected: {line[:250]}"
                    )
                    continue

                # Arduino timestamp is parsed for validation/debug,
                # but currently not used for saved timestamps.
                mcu_ts, angle, load = parsed

                # -------------------------------------------------
                # Timestamp source
                # -------------------------------------------------
                aligned_ts = local_clock()

                # -------------------------------------------------
                # Relative HX711 normalization: -1 to +1
                # -------------------------------------------------

                if self.load_zero is None:

                    self.load_zero_samples.append(load)

                    if len(self.load_zero_samples) >= self.load_zero_sample_count:
                        self.load_zero = (
                            sum(self.load_zero_samples)
                            / len(self.load_zero_samples)
                        )

                        log_print(f"[LOAD ZERO] {self.load_zero:.3f}")

                    load_norm = 0.0

                else:

                    load_corrected = load - self.load_zero
                    load_norm = load_corrected / self.LOAD_SPAN

                    if load_norm < -1.0:
                        load_norm = -1.0
                    elif load_norm > 1.0:
                        load_norm = 1.0

                self.valid_count += 1

                self.data.emit(aligned_ts, angle, load_norm)

                self._periodic_debug_report()

            except serial.SerialException:
                self.error_count += 1
                log_exception("[MCU ERROR] SerialException in MCU thread")
                self._close_serial_after_error()
                self.msleep(1000)

            except OSError:
                self.error_count += 1
                log_exception("[MCU ERROR] OSError in MCU thread")
                self._close_serial_after_error()
                self.msleep(1000)

            except Exception:
                self.error_count += 1
                log_exception("[MCU ERROR] Unexpected exception in MCU thread")
                self._close_serial_after_error()
                self.msleep(1000)

        log_print("[MCU DEBUG] Thread loop ended")

    # =====================================================
    # PERIODIC DEBUG REPORT
    # =====================================================

    def _periodic_debug_report(self, extra=None):

        now = time.monotonic()

        if now - self.last_debug_report_time < 1.0:
            return

        status = (
            "[MCU DEBUG] "
            f"raw_count={self.raw_count}, "
            f"valid_count={self.valid_count}, "
            f"parse_reject_count={self.parse_reject_count}, "
            f"empty_line_count={self.empty_line_count}, "
            f"error_count={self.error_count}, "
            f"reconnect_count={self.reconnect_count}, "
            f"port={self.port}, "
            f"ser_is_open={self.ser.is_open if self.ser else None}"
        )

        log_print(status)

        if extra:
            log_print(f"[MCU DEBUG] {extra}")

        self.last_debug_report_time = now

    # =====================================================
    # SERIAL CONNECTION
    # =====================================================

    def connect(self):

        self.reconnect_count += 1

        self.ser = None

        log_print("[MCU DEBUG] connect() called")
        log_print(f"[MCU DEBUG] Requested/configured port: {self.port}")

        ports = list(serial.tools.list_ports.comports())

        if not ports:
            log_print("[MCU WARNING] No COM ports found by list_ports()")

        else:
            log_print("[MCU DEBUG] Available COM ports:")

            for port_info in ports:
                log_print(
                    "    "
                    f"device={port_info.device}, "
                    f"description={port_info.description}, "
                    f"hwid={port_info.hwid}, "
                    f"manufacturer={port_info.manufacturer}, "
                    f"product={port_info.product}"
                )

        # -------------------------------------------------
        # Prefer manually configured port first
        # -------------------------------------------------
        if self.port:

            try:

                log_print(f"[MCU DEBUG] Trying configured port: {self.port}")

                self.ser = serial.Serial(
                    self.port,
                    self.baud,
                    timeout=0.1,
                    write_timeout=0.1
                )

                self.ser.reset_output_buffer()
                self.ser.reset_input_buffer()

                time.sleep(0.3)

                log_print(f"[MCU] Connected to configured port {self.port}")

                return

            except Exception:
                log_exception(f"[MCU ERROR] Configured port failed: {self.port}")
                self.ser = None

        # -------------------------------------------------
        # Fallback: try all available ports
        # -------------------------------------------------
        for port_info in ports:

            try:

                port_name = port_info.device

                log_print(
                    f"[MCU DEBUG] Trying auto port: {port_name}, "
                    f"description={port_info.description}, "
                    f"hwid={port_info.hwid}"
                )

                self.ser = serial.Serial(
                    port_name,
                    self.baud,
                    timeout=0.1,
                    write_timeout=0.1
                )

                self.ser.reset_output_buffer()
                self.ser.reset_input_buffer()

                time.sleep(0.3)

                self.port = port_name

                log_print(f"[MCU] Connected to auto port {port_name}")

                return

            except Exception:
                log_exception(f"[MCU ERROR] Failed auto port {port_info.device}")
                self.ser = None

        log_print("[MCU ERROR] No usable serial port found")

    # =====================================================
    # PARSER
    # =====================================================

    def parse(self, line):

        if not line.startswith("{"):
            return None

        try:
            data = json.loads(line)
        except Exception:
            return None

        # Validate message type
        if data.get("type") != "input":
            return None

        # Preferred current firmware timestamp
        t_us = data.get("t_us")

        # Fallback for older firmware, useful for debugging
        t_ms = data.get("t_ms")

        angle = data.get("angle_deg")
        load = data.get("force_g")

        if angle is None or load is None:
            return None

        try:

            if t_us is not None:
                mcu_ts = float(t_us) * 1e-6
            elif t_ms is not None:
                mcu_ts = float(t_ms) * 1e-3
                log_print("[MCU WARNING] Firmware sent t_ms instead of t_us")
            else:
                return None

            angle = float(angle)
            load = float(load)

        except Exception:
            return None

        return mcu_ts, angle, load

    # =====================================================
    # ERROR CLEANUP
    # =====================================================

    def _close_serial_after_error(self):

        try:
            if self.ser:
                log_print(f"[MCU DEBUG] Closing serial port after error: {self.port}")
                self.ser.close()
        except Exception:
            log_exception("[MCU ERROR] Failed to close serial port after error")

        self.ser = None
        self.was_recording = False

    # =====================================================
    # SYNC / BASELINE RESET
    # =====================================================

    def reset_sync(self):

        self.was_recording = False

        self.load_zero = None
        self.load_zero_samples = []
        
        self.raw_count = 0
        self.valid_count = 0
        self.parse_reject_count = 0
        self.empty_line_count = 0
        self.error_count = 0

        self.last_line_time = time.monotonic()
        self.last_debug_report_time = time.monotonic()
        self.last_empty_warning_time = time.monotonic()

        log_print("[MCU DEBUG] reset_sync() called")

    # =====================================================
    # STOP
    # =====================================================

    def stop(self):

        log_print("[MCU DEBUG] stop() called")

        self.running = False

        try:
            if self.ser:
                log_print(f"[MCU DEBUG] Closing serial port on stop: {self.port}")
                self.ser.close()
        except Exception:
            log_exception("[MCU ERROR] Failed to close serial port on stop")

        self.quit()
        self.wait(1000)

        log_print("[MCU DEBUG] stop() finished")