from PyQt5.QtCore import QThread, pyqtSignal
from pylsl import resolve_streams, StreamInlet
import traceback
import time


class LSLThread(QThread):
    """
    LSL acquisition thread.

    Important architecture:
    - Full-rate EMG samples go directly to emg_queue for CSV saving.
    - Qt signal is used only for live plot and is throttled.
    - First valid LSL timestamp is used as EMG zero.
    """

    # Plot-only signal: relative_time, emg_value
    data = pyqtSignal(float, float)

    def __init__(
        self,
        start_event,
        emg_queue,
        session_start_getter=None,
        plot_hz=50.0,
    ):
        super().__init__()

        self.start_event = start_event
        self.emg_queue = emg_queue
        self.session_start_getter = session_start_getter

        self.plot_hz = float(plot_hz)
        self.plot_interval = 1.0 / self.plot_hz

        self.running = True
        self.inlet = None

        self.was_recording = False
        self.last_plot_emit_time = 0.0

        self.saved_count = 0
        self.plot_emit_count = 0

        # First valid LSL timestamp during current recording.
        # All EMG timestamps are saved/plotted relative to this.
        self.first_lsl_ts = None

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
                self.first_lsl_ts = None
                self.msleep(50)
                continue

            try:

                # -------------------------------------------------
                # Reconnect if needed
                # -------------------------------------------------
                if self.inlet is None:
                    self.connect()
                    self.msleep(200)
                    continue

                # -------------------------------------------------
                # First loop after START:
                # remove old samples already sitting in LSL buffer
                # -------------------------------------------------
                if not self.was_recording:

                    try:
                        flushed = self.inlet.flush()
                        print(f"[LSL] flushed {flushed} old samples")
                    except Exception as e:
                        print(f"[LSL] flush failed: {e}")

                    self.was_recording = True
                    self.first_lsl_ts = None
                    self.last_plot_emit_time = 0.0
                    self.saved_count = 0
                    self.plot_emit_count = 0

                # -------------------------------------------------
                # Pull chunk instead of one sample in a tight loop
                # -------------------------------------------------
                samples, timestamps = self.inlet.pull_chunk(
                    timeout=0.02,
                    max_samples=128
                )

                if not timestamps:
                    self.msleep(1)
                    continue


                latest_t_rel = None
                latest_value = None

                # -------------------------------------------------
                # Save ALL samples to CSV queue
                # -------------------------------------------------
                for sample, lsl_ts in zip(samples, timestamps):

                    try:
                        value = float(sample[0])
                    except Exception:
                        continue

                    # Keep original LSL sample timestamp
                    sample_ts = float(lsl_ts)

                    # -------------------------------------------------
                    # IMPORTANT:
                    # First valid LSL timestamp becomes zero.
                    # This prevents huge values like 1780776694.8098414
                    # in live plot and CSV.
                    # -------------------------------------------------
                    if self.first_lsl_ts is None:
                        self.first_lsl_ts = sample_ts
                        print(f"[LSL] first_lsl_ts = {self.first_lsl_ts:.6f}")

                    t_rel = sample_ts - self.first_lsl_ts

                    if t_rel < 0:
                        continue

                    # PC arrival/save timestamp
                    pc_ts = time.perf_counter()

                    self.emg_queue.put(
                        (
                            sample_ts,
                            pc_ts,
                            t_rel,
                            value
                        )
                    )

                    self.saved_count += 1
                    latest_t_rel = t_rel
                    latest_value = value

                # -------------------------------------------------
                # Emit to GUI only at plot_hz
                # -------------------------------------------------
                now = time.perf_counter()

                if (
                    latest_t_rel is not None
                    and latest_value is not None
                    and now - self.last_plot_emit_time >= self.plot_interval
                ):
                    self.data.emit(latest_t_rel, latest_value)
                    self.last_plot_emit_time = now
                    self.plot_emit_count += 1

            except Exception:

                print("[LSL ERROR]")
                traceback.print_exc()

                self.inlet = None
                self.was_recording = False
                self.first_lsl_ts = None
                self.msleep(500)

    # =====================================================
    # CONNECTION
    # =====================================================

    def connect(self):

        try:
            streams = resolve_streams()

            if streams:

                for stream in streams:

                    if stream.type() == "Data":

                        self.inlet = StreamInlet(
                            stream,
                            max_buflen=1,
                            recover=True
                        )

                        print("[LSL] Connected to stream type Data")

                        return

            print("[LSL] resolve_streams found no Data stream")

        except Exception:

            print("[LSL CONNECT ERROR]")
            traceback.print_exc()
            self.inlet = None

    # =====================================================
    # SYNC RESET
    # =====================================================

    def reset_sync(self):

        self.was_recording = False
        self.first_lsl_ts = None
        self.last_plot_emit_time = 0.0
        self.saved_count = 0
        self.plot_emit_count = 0

        print("[LSL] reset")

    # =====================================================
    # STOP
    # =====================================================

    def stop(self):

        self.running = False

        self.quit()
        self.wait(1000)