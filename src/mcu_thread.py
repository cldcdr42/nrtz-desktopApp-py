"""
mcu_thread.py

Serial acquisition worker for the MCU (Arduino) sending angle/load
sensor data over a COM port as JSON lines. Handles connecting/
reconnecting to the serial port, parsing each line, normalizing the
HX711 load cell reading against a per-trial calibrated span (see
set_load_calibration()/set_load_zero(), driven by the calibration
dialog in gui.py before each recording), and forwarding both parsed
data (for plotting/CSV) and raw JSON (for UDP forwarding) via signals.
"""

from PyQt5.QtCore import QThread, pyqtSignal

import serial
import serial.tools.list_ports
import time
import json
import traceback
import logging
import sys
from pathlib import Path

from logging_setup import log_print, log_exception


class MCUThread(QThread):

    # pc_perf_timestamp, mcu_time, angle_raw, angle, load_raw, load_norm
    data = pyqtSignal(float, float, float, float, float, float)

    # raw JSON line for UDP forwarding
    raw = pyqtSignal(str)

    def __init__(self, port, baud, start_event, default_span=500000.0):

        super().__init__()

        self.port = port
        self.baud = baud
        self.start_event = start_event

        self.running = True
        self.ser = None

        self.was_recording = False

        # Set from another thread (GUI) via request_port_change(); the
        # run() loop below applies it on its own thread, never touched
        # directly from outside.
        self._pending_port = None

        # ----------------------------------------
        # HX711 relative normalization settings
        # ----------------------------------------

        self.load_zero = None
        self.load_zero_samples = []

        # At 10 Hz:
        # 5 samples = about 0.5 seconds.
        # During these first samples, load output is 0.0.
        # This silent auto-zero only actually runs when load_zero is
        # None AND load_zero_locked is False -- see run() below. It's
        # the "use default values" fallback path (no explicit
        # calibration dialog zero step), kept as the original
        # behavior for that case.
        self.load_zero_sample_count = 5

        # While True, the auto-zero block in run() does nothing, even
        # if load_zero is still None. Set by the calibration dialog
        # for the entire time it's open, so its own explicit 5-second
        # zero step (or its decision to fall back to auto-zero) can't
        # be raced by this thread quietly zeroing itself in the
        # background first.
        self.load_zero_locked = False

        # Fallback span (raw units mapping to +-1.0), sourced from
        # settings.ini via main.py, used only when a per-trial
        # calibration hasn't been set via set_load_calibration() --
        # e.g. the operator chose "use default values".
        self.LOAD_SPAN = float(default_span)

        # Per-direction spans, set from the max-pull calibration
        # dialog before each trial (see gui.py LoadCalibrationDialog
        # and set_load_calibration() below). Both are positive
        # magnitudes, already include the 2x calibration-to-clamp
        # headroom (see set_load_calibration). Not reset by
        # reset_sync() — calibration is a property of "this trial's
        # setup", separate from the zero baseline.
        self.load_span_pos = self.LOAD_SPAN
        self.load_span_neg = self.LOAD_SPAN

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
        log_print(f"[MCU DEBUG] Initial port={self.port}, baud={self.baud}")

    # =====================================================
    # MAIN LOOP
    # =====================================================

    def run(self):

        log_print("[MCU DEBUG] Thread started")
        self.connect()

        while self.running:

            self._apply_pending_port_change()

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

                # Timestamp when sample arrived at PC
                pc_perf_ts = time.perf_counter()

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
                mcu_ts, angle_raw, angle, load = parsed

                # -------------------------------------------------
                # Relative HX711 normalization: -1 to +1
                # -------------------------------------------------

                if self.load_zero is None:

                    # Silent auto-zero fallback — only actually runs
                    # when not locked. The calibration dialog locks
                    # this for its entire lifetime and either sets
                    # load_zero explicitly (Calibrate) or unlocks
                    # this on its way out without setting it (Use
                    # defaults), letting this run exactly as it did
                    # before per-trial calibration existed.
                    if not self.load_zero_locked:

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

                    # Asymmetric per-direction span: the rig measures
                    # forearm pull strength, which differs between
                    # directions (different arm, different subject) —
                    # a single symmetric span would misrepresent one
                    # side. Spans come from the per-trial calibration
                    # dialog via set_load_calibration(); until that's
                    # been run, both default to LOAD_SPAN.
                    span = self.load_span_pos if load_corrected >= 0 else self.load_span_neg

                    load_norm = (load_corrected / span) if span else 0.0

                    if load_norm < -1.0:
                        load_norm = -1.0
                    elif load_norm > 1.0:
                        load_norm = 1.0

                self.valid_count += 1

                self.data.emit(
                    pc_perf_ts,
                    mcu_ts,
                    angle_raw,
                    angle,
                    load,
                    load_norm
                )

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
    # LOAD CALIBRATION (driven externally, from gui.py's
    # LoadCalibrationDialog, before each trial)
    # =====================================================

    def set_load_zero(self, zero_value):
        """
        Explicitly sets the zero baseline, bypassing the silent
        auto-zero. Called by the calibration dialog after its
        dedicated 5-second "hand off the sensor" step, using an
        average of raw samples collected over that window (more
        stable than the old quick 5-sample average).
        """
        self.load_zero = zero_value
        self.load_zero_samples = []
        log_print(f"[MCU] Load zero set explicitly: {zero_value:.3f}")

    def set_load_calibration(self, pos_peak, neg_peak):
        """
        Set per-direction max-pull spans from a calibration dialog.
        pos_peak / neg_peak are the measured peak raw deviations from
        zero during each direction's capture window (not spans
        themselves) — the actual span used for normalization is
        2x that peak, so a real effort matching the exact calibrated
        max lands at load_norm=0.5, not 1.0, leaving headroom for a
        harder pull later in the trial without clamping/losing data.
        Genuinely exceeding 2x the calibrated peak still clamps to
        +-1.0 — this just moves the ceiling up, not removes it.

        Either argument may be None (that direction's calibration
        capture never registered a reading) — falls back to
        LOAD_SPAN for that direction rather than leaving it unset.
        """

        CALIBRATION_HEADROOM = 2.0

        if pos_peak is not None and pos_peak > 0:
            self.load_span_pos = CALIBRATION_HEADROOM * pos_peak
        else:
            self.load_span_pos = self.LOAD_SPAN
            log_print("[MCU] No positive-direction calibration captured, using default span")

        if neg_peak is not None and neg_peak > 0:
            self.load_span_neg = CALIBRATION_HEADROOM * neg_peak
        else:
            self.load_span_neg = self.LOAD_SPAN
            log_print("[MCU] No negative-direction calibration captured, using default span")

        log_print(
            f"[MCU] Load calibration set: "
            f"pos_span={self.load_span_pos:.0f} (peak={pos_peak}), "
            f"neg_span={self.load_span_neg:.0f} (peak={neg_peak})"
        )

    # =====================================================
    # LIVE PORT CHANGE (requested from GUI thread)
    # =====================================================

    def request_port_change(self, new_port):
        """
        Thread-safe entry point for the GUI to ask for a different COM
        port. Only sets a flag — the actual reconnect happens inside
        run(), on this thread, never here.
        """
        self._pending_port = new_port
        log_print(f"[MCU] Port change requested -> {new_port}")

    def _apply_pending_port_change(self):

        if self._pending_port is None:
            return

        new_port = self._pending_port
        self._pending_port = None

        log_print(f"[MCU DEBUG] Applying pending port change -> {new_port}")

        self.port = new_port

        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                log_exception("[MCU ERROR] Failed to close serial port during port change")
            self.ser = None

        # Reconnect immediately rather than waiting for the next
        # recording start, so the user gets feedback right away if
        # the new port doesn't work.
        self.connect()

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

        angle_raw = data.get("angle_raw")
        angle = data.get("angle_deg")
        load = data.get("force_g")

        if angle_raw is None or angle is None or load is None:
            return None

        try:

            if t_us is not None:
                mcu_ts = int(t_us)
            elif t_ms is not None:
                mcu_ts = float(t_ms) * 1e-3
                log_print("[MCU WARNING] Firmware sent t_ms instead of t_us")
            else:
                return None

            angle_raw = float(angle_raw)
            angle = float(angle)
            load = float(load)

        except Exception:
            return None

        return mcu_ts, angle_raw, angle, load

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

    def reset_sync(self, reset_zero=True):
        """
        Resets sync/debug state for a fresh trial.

        reset_zero=True (default) restores the original behavior:
        clears load_zero so the next samples re-establish it. Pass
        reset_zero=False after a calibration dialog run — whether it
        set an explicit zero (Calibrate) or deliberately left it to
        be auto-set on the next samples (Use defaults) — so this
        call doesn't undo that decision.

        Never touches load_span_pos/load_span_neg — those come from
        set_load_calibration() and represent a separate, already
        completed step for this trial.
        """

        self.was_recording = False

        if reset_zero:
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

        log_print(f"[MCU DEBUG] reset_sync() called (reset_zero={reset_zero})")

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

    # =====================================================
    # STATUS (for GUI status panel)
    # =====================================================

    def is_connected(self):
        """True if the serial port is currently open. Used by the GUI status panel."""
        return self.ser is not None and self.ser.is_open