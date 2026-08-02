"""
lsl_thread.py

Acquisition worker for a single LSL (Lab Streaming Layer) stream. One
instance of this class is created per logical stream (EMG data, raw
data, events, raw events) — see main.py for how they're wired up.
Handles connecting to the matching LSL stream, pulling samples, and
routing them to CSV storage and (optionally) the live plot.
"""

from PyQt5.QtCore import QThread, pyqtSignal
from pylsl import resolve_streams, StreamInlet
import traceback
import time


class LSLStreamWorker(QThread):
    """
    Generic LSL acquisition worker for ONE logical stream.

    Matches a stream by LSL 'type' (stable across devices), optionally
    narrowed by a substring filter/exclude on the stream 'name' — this
    is needed when two streams share the same type (e.g. two "Events"
    streams) and can only be told apart by name. Since names vary by
    device/vendor, the filter is a heuristic, not a guarantee: if it
    can't cleanly disambiguate, a warning is logged rather than
    silently guessing wrong.

    Full-rate samples (ALL channels) go directly to `out_queue` for
    CSV saving, as a tuple:
    (lsl_timestamp_s, pc_perf_counter_s, relative_time_s, ch0, ch1, ...)

    A throttled Qt signal (`data`) is emitted for live plotting of the
    first channel only, if plot_hz > 0. Set plot_hz=0 for streams you
    don't want to plot (e.g. raw/events) to avoid unnecessary GUI traffic.
    """

    # relative_time, first_channel_value
    data = pyqtSignal(float, float)

    def __init__(
        self,
        label,
        stream_type,
        start_event,
        out_queue,
        name_must_contain=None,       # e.g. "raw" -> only match names containing this
        name_must_not_contain=None,   # e.g. "raw" -> exclude names containing this
        plot_hz=0.0,                  # 0 disables the plot signal entirely
        pull_timeout=0.02,
        max_samples=128,
    ):
        super().__init__()

        self.label = label
        self.stream_type = stream_type
        self.start_event = start_event
        self.out_queue = out_queue

        self.name_must_contain = (
            name_must_contain.lower() if name_must_contain else None
        )
        self.name_must_not_contain = (
            name_must_not_contain.lower() if name_must_not_contain else None
        )

        self.plot_hz = float(plot_hz)
        self.plot_interval = (1.0 / self.plot_hz) if self.plot_hz > 0 else None

        self.pull_timeout = pull_timeout
        self.max_samples = max_samples

        self.running = True
        self.inlet = None
        self.channel_count = None

        # Tracks whether we were mid-recording on the previous loop
        # iteration, so the transition into a new recording (buffer
        # flush, timestamp reset) only fires once, right at the start.
        self.was_recording = False
        self.last_plot_emit_time = 0.0
        self.first_lsl_ts = None

        self.saved_count = 0

    # =====================================================
    # MAIN LOOP
    # =====================================================

    def run(self):

        self.connect()

        while self.running:

            recording = self.start_event.is_set()

            if not recording:
                self.was_recording = False
                self.first_lsl_ts = None
                self.msleep(50)
                continue

            try:

                if self.inlet is None:
                    self.connect()
                    self.msleep(200)
                    continue

                if not self.was_recording:

                    # Drop any samples LSL buffered before recording
                    # actually started, so t=0 lines up with the real
                    # start of the session rather than whenever the
                    # stream first connected.
                    try:
                        flushed = self.inlet.flush()
                        print(f"[LSL:{self.label}] flushed {flushed} old samples")
                    except Exception as e:
                        print(f"[LSL:{self.label}] flush failed: {e}")

                    self.was_recording = True
                    self.first_lsl_ts = None
                    self.last_plot_emit_time = 0.0
                    self.saved_count = 0

                samples, timestamps = self.inlet.pull_chunk(
                    timeout=self.pull_timeout,
                    max_samples=self.max_samples,
                )

                if not timestamps:
                    self.msleep(1)
                    continue

                latest_t_rel = None
                latest_first_ch = None

                for sample, lsl_ts in zip(samples, timestamps):

                    sample_ts = float(lsl_ts)

                    # First valid LSL timestamp becomes zero, per-stream.
                    if self.first_lsl_ts is None:
                        self.first_lsl_ts = sample_ts
                        print(f"[LSL:{self.label}] first_lsl_ts = {self.first_lsl_ts:.6f}")

                    t_rel = sample_ts - self.first_lsl_ts

                    if t_rel < 0:
                        continue

                    pc_ts = time.perf_counter()

                    try:
                        channels = tuple(float(v) for v in sample)
                    except Exception:
                        continue

                    row = (sample_ts, pc_ts, t_rel) + channels
                    self.out_queue.put(row)

                    self.saved_count += 1
                    latest_t_rel = t_rel
                    latest_first_ch = channels[0] if channels else None

                # Only emit for the plot at most once per plot_interval,
                # using the last sample in the chunk — no need to emit
                # per-sample when the GUI can't render that fast anyway.
                if self.plot_interval is not None and latest_t_rel is not None:
                    now = time.perf_counter()
                    if now - self.last_plot_emit_time >= self.plot_interval:
                        self.data.emit(latest_t_rel, latest_first_ch)
                        self.last_plot_emit_time = now

            except Exception:

                print(f"[LSL:{self.label} ERROR]")
                traceback.print_exc()

                self.inlet = None
                self.was_recording = False
                self.first_lsl_ts = None
                self.msleep(500)

    # =====================================================
    # CONNECTION / STREAM MATCHING
    # =====================================================

    def _matches(self, stream_info):

        if stream_info.type() != self.stream_type:
            return False

        name = stream_info.name().lower()

        if self.name_must_contain and self.name_must_contain not in name:
            return False

        if self.name_must_not_contain and self.name_must_not_contain in name:
            return False

        return True

    def connect(self):

        try:
            streams = resolve_streams()

            candidates = [s for s in streams if self._matches(s)]

            if not candidates:
                print(f"[LSL:{self.label}] no matching stream found "
                      f"(type={self.stream_type}, "
                      f"must_contain={self.name_must_contain}, "
                      f"must_not_contain={self.name_must_not_contain})")
                self.inlet = None
                return

            if len(candidates) > 1:
                names = [s.name() for s in candidates]
                print(f"[LSL:{self.label}] WARNING: {len(candidates)} streams matched "
                      f"type={self.stream_type} with the same name filter — "
                      f"disambiguation heuristic failed. Candidates: {names}. "
                      f"Using the first one: {names[0]}")

            stream = candidates[0]

            self.inlet = StreamInlet(stream, max_buflen=1, recover=True)
            self.channel_count = stream.channel_count()

            print(f"[LSL:{self.label}] connected to '{stream.name()}' "
                  f"type={stream.type()} channels={self.channel_count} "
                  f"srate={stream.nominal_srate()}")

        except Exception:

            print(f"[LSL:{self.label} CONNECT ERROR]")
            traceback.print_exc()
            self.inlet = None

    def header(self):
        """
        Column header for this stream's CSV, sized to the actual
        channel count discovered at connect time. Falls back to a
        single 'ch0' column if not connected yet (should not normally
        happen, since workers connect immediately on thread start,
        before recording begins).
        """
        n = self.channel_count or 1
        return ["lsl_timestamp_s", "pc_perf_counter_s", "relative_time_s"] + \
               [f"ch{i}" for i in range(n)]

    # =====================================================
    # SYNC RESET / STOP
    # =====================================================

    def reset_sync(self):
        self.was_recording = False
        self.first_lsl_ts = None
        self.last_plot_emit_time = 0.0
        self.saved_count = 0
        print(f"[LSL:{self.label}] reset")

    def stop(self):
        self.running = False
        self.quit()
        self.wait(1000)