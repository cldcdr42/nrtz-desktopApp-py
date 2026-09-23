"""
movement_model.py

Real-time movement-parameter estimator. Runs as its own QThread,
consuming full-rate EMG samples (from the "Data" LSL stream, which may
carry more than one EMG channel) and the live MCU angle in parallel,
and produces one scalar "movement parameter" PER EMG channel -- meant
to eventually drive a torque command back to the MCU once outgoing-
command support lands in mcu_thread.py.

Current model (deliberately simple, a placeholder meant to be tuned
or replaced once there's real subject data to look at):
    - EMG activity = RMS of the EMG signal over a short rolling time
      window (time-based, not a fixed sample count, so it stays
      correct even if the LSL stream's actual rate drifts a bit from
      its nominal rate) -- tracked SEPARATELY for each EMG channel.
    - The angle acts as a single, global on/off gate shared by every
      channel (there's only one MCU angle, not one per EMG channel):
      the model only produces nonzero output once the forearm angle
      has passed a configured threshold, in a configured direction
      (e.g. "only assist once the arm is above 30 degrees").
    - Final output per channel = ReLU(EMG_activity - threshold) while
      the gate is open, else 0.0. Channels are NOT combined -- each
      gets its own independent movement parameter.

Feeding this thread (see main.py for the actual wiring):
    - EMG samples arrive via a plain Queue (`emg_queue`), as
      (t_rel, values) where `values` is a tuple/list with one entry
      per EMG channel. This must be a DEDICATED queue fed by
      LSLStreamWorker's optional `model_queue` param (lsl_thread.py),
      NOT the throttled `data` Qt signal used for the live plot --
      that signal is decimated to plot_hz, single-channel only, and
      would give an inaccurate RMS.
    - The MCU angle arrives via set_latest_angle(), called directly
      from the GUI thread's on_mcu() handler. This is a plain
      lock-protected value, not a Qt signal -- like every other worker
      thread in this project, ModelThread runs its own while loop
      rather than a Qt event loop, so a cross-thread queued signal
      into it would never actually get delivered.
"""

import time
import threading
from collections import deque
from queue import Empty

from PyQt5.QtCore import QThread, pyqtSignal

from logging_setup import log_print, log_exception


class MovementModel:
    """
    Pure computation: one rolling-window EMG RMS per channel, combined
    with a single shared angle gate, into one ReLU'd output per channel.

    Deliberately isolated from all threading concerns so it can be
    tested or swapped out on its own without touching ModelThread.
    """

    # ---------------------------------------------------------------
    # Defaults -- placeholder values that need real tuning once there's
    # actual hardware/subject data to look at. Kept as class attributes
    # (not buried in method bodies) so they're easy to find and adjust
    # without hunting through the compute logic.
    # ---------------------------------------------------------------
    DEFAULT_WINDOW_MS = 100.0          # RMS window length
    DEFAULT_EMG_THRESHOLD = 50.0       # raw EMG units -- amplifier/gain dependent
    DEFAULT_ANGLE_GATE_DEG = 30.0      # gate opens once angle passes this
    DEFAULT_GATE_DIRECTION = "above"   # "above" or "below"

    def __init__(
        self,
        window_ms=None,
        emg_threshold=None,
        angle_gate_deg=None,
        gate_direction=None,
    ):
        self.window_s = (window_ms if window_ms is not None else self.DEFAULT_WINDOW_MS) / 1000.0
        self.emg_threshold = emg_threshold if emg_threshold is not None else self.DEFAULT_EMG_THRESHOLD
        self.angle_gate_deg = angle_gate_deg if angle_gate_deg is not None else self.DEFAULT_ANGLE_GATE_DEG

        self.gate_direction = (gate_direction or self.DEFAULT_GATE_DIRECTION).lower()
        if self.gate_direction not in ("above", "below"):
            log_print(f"[MODEL] Unknown gate_direction '{self.gate_direction}', defaulting to 'above'")
            self.gate_direction = "above"

        # One rolling window (deque of (t_rel, value)) and one running
        # sum-of-squares per EMG channel, so RMS is O(1) per incoming
        # sample instead of re-summing the whole window every time --
        # this runs once per channel per EMG sample, at full LSL rate,
        # so it's worth avoiding the O(window) cost per call.
        #
        # Channel count isn't known until the first sample arrives
        # (same reasoning as LSLStreamWorker only knowing its own
        # channel_count after connect()), so these start empty and are
        # lazily sized on the first push_emg_sample() call.
        self._buffers = None   # list[deque] once sized
        self._sumsq = None     # list[float] once sized

    # =====================================================
    # EMG WINDOW
    # =====================================================

    def push_emg_sample(self, t_rel, values):
        """
        Add one multi-channel EMG sample and evict, per channel,
        whatever's fallen out of that channel's window.

        `values` is a sequence with one entry per EMG channel. The
        channel count is fixed on the first call; if a later call
        somehow carries more channels than that (shouldn't normally
        happen -- the LSL stream's channel count doesn't change mid
        session), the extra ones are grown into rather than dropped,
        so a channel-count surprise is visible in behavior rather than
        silently losing data.
        """

        if self._buffers is None:
            self._buffers = [deque() for _ in values]
            self._sumsq = [0.0 for _ in values]

        while len(self._buffers) < len(values):
            self._buffers.append(deque())
            self._sumsq.append(0.0)

        cutoff = t_rel - self.window_s

        for idx, v in enumerate(values):

            buf = self._buffers[idx]
            buf.append((t_rel, v))
            self._sumsq[idx] += v * v

            while buf and buf[0][0] < cutoff:
                _old_t, old_v = buf.popleft()
                self._sumsq[idx] -= old_v * old_v

            # Guard against float drift ever pushing this slightly negative.
            if self._sumsq[idx] < 0.0:
                self._sumsq[idx] = 0.0

    def emg_rms(self):
        """Current RMS per channel, as a list. Empty list if no data seen yet."""

        if not self._buffers:
            return []

        rms_list = []

        for buf, sumsq in zip(self._buffers, self._sumsq):
            n = len(buf)
            rms_list.append((sumsq / n) ** 0.5 if n else 0.0)

        return rms_list

    def channel_count(self):
        """Number of EMG channels seen so far this session, 0 if none yet."""
        return len(self._buffers) if self._buffers else 0

    def reset(self):
        """Clears all rolling windows -- called between recordings so a
        stale window from the previous session can't bleed into the next.
        Channel count is rediscovered from scratch on the next sample."""
        self._buffers = None
        self._sumsq = None

    # =====================================================
    # GATE + OUTPUT
    # =====================================================

    def gate_open(self, angle_deg):
        if self.gate_direction == "above":
            return angle_deg >= self.angle_gate_deg
        return angle_deg <= self.angle_gate_deg

    def compute(self, angle_deg):
        """
        Combines each channel's current EMG RMS (over its rolling
        window, already fed via push_emg_sample) with the single
        shared angle gate into one movement parameter PER channel.

        Returns (rms_list, movement_param_list), same length, in
        channel order, so the caller can log/plot raw activity
        alongside the gated output for every channel.
        """
        rms_list = self.emg_rms()

        if not self.gate_open(angle_deg):
            return rms_list, [0.0 for _ in rms_list]

        movement_params = [max(0.0, rms - self.emg_threshold) for rms in rms_list]
        return rms_list, movement_params


class ModelThread(QThread):
    """
    Real-time worker: drains multi-channel EMG samples from
    `emg_queue`, tracks the latest MCU angle (set from the GUI thread
    via set_latest_angle()), and periodically emits one movement
    parameter per EMG channel.

    Output is throttled to `emit_hz` (default 50 Hz, matching the live
    EMG plot rate) rather than emitted on every single incoming EMG
    sample -- there's no reason to push a Qt signal across threads at
    1000 Hz when nothing downstream (GUI, eventually MCU commands)
    needs to react that fast.
    """

    # t_rel, emg_rms_list (object: list[float]), angle_deg, movement_param_list (object: list[float])
    #
    # Fixed-arity float args won't work here since the channel count
    # isn't known until runtime -- 'object' lets the signal carry a
    # plain Python list of whatever length the stream turns out to
    # have, same idea PyQt uses for any variable-shaped payload.
    output = pyqtSignal(float, object, float, object)

    def __init__(
        self,
        start_event,
        emg_queue,
        out_queue=None,
        model=None,
        lsl_worker=None,
        emit_hz=50.0,
        pull_timeout=0.02,
    ):
        super().__init__()

        self.start_event = start_event
        self.emg_queue = emg_queue
        self.out_queue = out_queue  # optional: for CSV logging via StorageThread

        self.model = model or MovementModel()

        # Reference to the LSLStreamWorker feeding emg_queue, used ONLY
        # so header() can name columns correctly (emg_rms_ch0, ch1, ...)
        # at the moment StorageThread opens files -- which can happen
        # before this thread's own model has processed a single real
        # sample yet, so self.model.channel_count() can't be trusted
        # for that. The LSL worker already knows its channel_count
        # reliably by then (set at LSL connect time, well before
        # recording ever starts) -- same single-source-of-truth
        # reasoning LSLStreamWorker.header() itself relies on.
        self.lsl_worker = lsl_worker

        self.emit_hz = float(emit_hz)
        self.emit_interval = (1.0 / self.emit_hz) if self.emit_hz > 0 else None
        self.pull_timeout = pull_timeout

        self.running = True
        self.was_recording = False
        self.last_emit_time = 0.0

        # Latest angle, set from the GUI thread's on_mcu() handler.
        # Plain lock rather than a Qt signal -- see module docstring.
        self._angle_lock = threading.Lock()
        self._latest_angle = 0.0

        log_print("[MODEL] ModelThread created")

    # =====================================================
    # EXTERNAL INPUT (called from other threads)
    # =====================================================

    def set_latest_angle(self, angle_deg):
        """Thread-safe setter, called from the GUI thread's on_mcu()."""
        with self._angle_lock:
            self._latest_angle = angle_deg

    def _get_latest_angle(self):
        with self._angle_lock:
            return self._latest_angle

    # =====================================================
    # MAIN LOOP
    # =====================================================

    def run(self):

        log_print("[MODEL] Thread started")

        while self.running:

            recording = self.start_event.is_set()

            # -------------------------------------------------
            # Recording inactive
            # -------------------------------------------------
            if not recording:
                if self.was_recording:
                    log_print("[MODEL] Recording became inactive")
                self.was_recording = False
                self._drain_queue()
                self.msleep(50)
                continue

            try:

                # -------------------------------------------------
                # First loop after START: drop anything that piled up
                # before recording actually began, and reset the
                # rolling windows so t=0 starts clean, same idea as
                # LSLStreamWorker/MCUThread's own was_recording reset.
                # -------------------------------------------------
                if not self.was_recording:
                    self._drain_queue()
                    self.model.reset()
                    self.last_emit_time = 0.0
                    self.was_recording = True
                    log_print("[MODEL] Recording started -- windows reset")

                t_rel, values = self.emg_queue.get(timeout=self.pull_timeout)

            except Empty:
                continue
            except Exception:
                log_exception("[MODEL ERROR] Failed reading emg_queue")
                continue

            try:
                self.model.push_emg_sample(t_rel, values)

                if self.emit_interval is not None:
                    now = time.perf_counter()
                    if now - self.last_emit_time < self.emit_interval:
                        continue
                    self.last_emit_time = now

                angle = self._get_latest_angle()
                rms_list, movement_params = self.model.compute(angle)

                self.output.emit(t_rel, rms_list, angle, movement_params)

                if self.out_queue is not None:
                    self.out_queue.put((t_rel, *rms_list, angle, *movement_params))

            except Exception:
                log_exception("[MODEL ERROR] Unexpected exception in ModelThread")

        log_print("[MODEL] Thread loop ended")

    # =====================================================
    # HELPERS
    # =====================================================

    def _drain_queue(self):
        while True:
            try:
                self.emg_queue.get_nowait()
            except Empty:
                break

    def header(self):
        """
        Column header for this stream's CSV, sized to the actual EMG
        channel count -- read from the LSL worker (reliably known by
        recording start) rather than from self.model, which may not
        have processed a real sample yet at the moment this is called.
        Falls back to 1 channel if that's ever unavailable.
        """
        n = 1
        if self.lsl_worker is not None and getattr(self.lsl_worker, "channel_count", None):
            n = self.lsl_worker.channel_count

        cols = ["relative_time_s"]
        cols += [f"emg_rms_ch{i}" for i in range(n)]
        cols += ["angle_deg"]
        cols += [f"movement_param_ch{i}" for i in range(n)]
        return cols

    # =====================================================
    # SYNC RESET (called explicitly from main.py at recording start,
    # same convention as LSLStreamWorker.reset_sync() / MCUThread.reset_sync())
    # =====================================================

    def reset_sync(self):
        self.model.reset()
        self.last_emit_time = 0.0
        self.was_recording = False
        log_print("[MODEL] reset_sync() called")

    # =====================================================
    # STOP
    # =====================================================

    def stop(self):
        self.running = False
        self.quit()
        self.wait(1000)
