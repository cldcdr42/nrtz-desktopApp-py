from PyQt5.QtCore import QThread, pyqtSignal
from pylsl import local_clock

import serial
import serial.tools.list_ports
import time
import json


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

        # Baseline/tare value collected at the start of each recording
        self.load_zero = None
        self.load_zero_samples = []

        # At 10 Hz:
        # 5 samples = about 0.5 seconds
        # During these first samples, load output is 0.0
        self.load_zero_sample_count = 5

        # Tune this value.
        # Smaller = more sensitive.
        # Larger = less sensitive.
        #
        # This is the raw load difference that maps to +1 or -1.
        ## IMPORTANT: DEFAULT self.LOAD_SPAN = 800.0

        self.LOAD_SPAN = 1e9

    # =====================================================
    # MAIN LOOP
    # =====================================================

    def run(self):

        self.connect()

        while self.running:

            recording = self.start_event.is_set()

            # -------------------------------------------------
            # Recording inactive
            # -------------------------------------------------
            if not recording:
                self.was_recording = False
                self.msleep(50)
                continue

            try:

                # -------------------------------------------------
                # Reconnect if needed
                # -------------------------------------------------
                if self.ser is None:
                    self.connect()
                    self.msleep(200)
                    continue

                # -------------------------------------------------
                # First loop after START:
                # flush old serial samples already waiting in buffer
                # -------------------------------------------------
                if not self.was_recording:

                    try:
                        self.ser.reset_input_buffer()
                        self.ser.reset_output_buffer()
                        print("[MCU] serial buffers flushed")
                    except Exception as e:
                        print(f"[MCU] serial flush failed: {e}")

                    self.was_recording = True

                # -------------------------------------------------
                # Read one serial line
                # -------------------------------------------------
                line = self.ser.readline().decode(errors="ignore").strip()

                if not line:
                    continue

                # Forward raw Arduino JSON to UDP sender
                if line.startswith("{"):
                    self.raw.emit(line)

                parsed = self.parse(line)

                if parsed is None:
                    continue

                # Arduino timestamp is parsed for validation/debug,
                # but it is not used for saved timestamps.
                mcu_ts, angle, load = parsed

                # -------------------------------------------------
                # IMPORTANT:
                # Use this PC's pylsl.local_clock() as the master
                # timestamp source.
                #
                # This avoids drift between Arduino micros(),
                # proprietary LSL timestamps, and Python.
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

                        print(f"[LOAD ZERO] {self.load_zero:.3f}")

                    # During tare/baseline collection, output neutral value.
                    load_out = 0.0

                else:

                    # Baseline-corrected load.
                    # Same units as Arduino force_g.
                    load_out = load - self.load_zero

                self.data.emit(aligned_ts, angle, load_out)

            except Exception as e:

                print(f"[MCU ERROR] {e}")

                try:
                    if self.ser:
                        self.ser.close()
                except:
                    pass

                self.ser = None
                self.was_recording = False

    # =====================================================
    # SERIAL CONNECTION
    # =====================================================

    def connect(self):

        self.ser = None

        ports = list(serial.tools.list_ports.comports())

        # -------------------------------------------------
        # Try all available ports first
        # -------------------------------------------------
        if ports:

            for port_info in ports:

                try:

                    port_name = port_info.device

                    self.ser = serial.Serial(
                        port_name,
                        self.baud,
                        timeout=0.1
                    )

                    self.ser.reset_output_buffer()
                    self.ser.reset_input_buffer()

                    time.sleep(0.3)

                    self.port = port_name

                    print(f"[MCU] Connected to {port_name}")

                    return

                except Exception as e:

                    print(f"[MCU] Failed {port_info.device}: {e}")

        # -------------------------------------------------
        # Fallback to manually provided port
        # -------------------------------------------------
        if self.port:

            try:

                print(f"[MCU] Fallback port: {self.port}")

                self.ser = serial.Serial(
                    self.port,
                    self.baud,
                    timeout=0.1
                )

                self.ser.reset_output_buffer()
                self.ser.reset_input_buffer()

                time.sleep(0.3)

                print(f"[MCU] Connected to fallback port {self.port}")

            except Exception as e:

                print(f"[MCU] Fallback failed: {e}")

                self.ser = None

        else:

            print("[MCU] No ports available")

    # =====================================================
    # PARSER
    # =====================================================

    def parse(self, line):

        if not line.startswith("{"):
            return None

        try:
            data = json.loads(line)
        except:
            return None

        # Validate message type
        if data.get("type") != "input":
            return None

        t_us = data.get("t_us")
        angle = data.get("angle_deg")
        load = data.get("force_g")

        if t_us is None:
            return None

        if angle is None or load is None:
            return None

        try:

            # Arduino microseconds -> seconds.
            # Kept for validation/debug only.
            # Not used as final saved timestamp.
            mcu_ts = float(t_us) * 1e-6

            angle = float(angle)
            load = float(load)

        except:
            return None

        return mcu_ts, angle, load

    # =====================================================
    # SYNC / BASELINE RESET
    # =====================================================

    def reset_sync(self):

        self.was_recording = False

        # Reset load baseline every recording
        self.load_zero = None
        self.load_zero_samples = []

        print("[MCU] reset")

    # =====================================================
    # STOP
    # =====================================================

    def stop(self):

        self.running = False

        try:
            if self.ser:
                self.ser.close()
        except:
            pass

        self.quit()
        self.wait(1000)