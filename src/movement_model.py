"""
movement_model.py

Real-time single-DOF motor-assist controller. Runs as its own QThread,
consuming full-rate EMG samples (from the "Data" LSL stream) and the
live MCU angle/load in parallel, and produces ONE motor assist command
(there is exactly one motor, driving one horizontal-plane forearm DOF).

Design (velocity-following / admittance-style assistance -- see the
proof-of-concept discussion this was built from):
    - Angle is the DOMINANT signal. Angular velocity (derived here
      from consecutive set_latest_angle() calls, smoothed) drives both
      the direction and most of the magnitude of the assist command,
      so the motor follows the direction the forearm is already
      moving in.
    - EMG contributes only a SMALL term (K_EMG, ~1-2%). It's tracked
      per-channel via the existing rolling-RMS infrastructure below,
      but only the first channel (this rig's one real biceps channel)
      drives the command. EMG is deliberately kept small both because
      it's noisy in this single-channel setup and because its main
      value is speed, not magnitude: EMG onset precedes real movement
      by roughly 50-150ms (electromechanical delay), so it mostly
      helps the movement-onset GATE open slightly early.
    - Load/force is wired through (set_latest_load()) but its gain
      (K_FORCE) defaults to 0.0 -- flip it once the load calibration
      is trusted.
    - A movement-onset gate (velocity OR EMG crossing a threshold,
      with hysteresis so it doesn't chatter) decides whether the
      motor should be doing anything at all; outside the gate the
      command is forced to exactly 0.0.
    - Output is rate-limited (MAX_COMMAND_SLEW) so motor commands
      never jump discontinuously between updates.

This is intentionally simple: linear gains, exponential smoothing, a
hysteresis gate. No ML, no dynamics model. TUNE THE CONSTANTS BELOW
against real bench data -- with the motor mechanically disconnected --
before trusting this near a person.

Feeding this thread (see main.py for the actual wiring):
    - EMG samples arrive via a plain Queue (`emg_queue`), as
      (t_rel, values) where `values` is a tuple/list with one entry
      per EMG channel. This must be a DEDICATED queue fed by
      LSLStreamWorker's optional `model_queue` param (lsl_thread.py),
      NOT the throttled `data` Qt signal used for the live plot --
      that signal is decimated to plot_hz, single-channel only, and
      would give an inaccurate RMS.
    - The MCU angle arrives via set_latest_angle(), and load via
      set_latest_load(), both called directly from the GUI thread's
      on_mcu() handler. These are plain lock-protected values, not Qt
      signals -- like every other worker thread in this project,
      ModelThread runs its own while loop rather than a Qt event
      loop, so a cross-thread queued signal into it would never
      actually get delivered.
    - The resulting assist command reaches the MCU via
      MainApp.on_model_output() -> mcu_thread.send_command(), gated
      by mcu_thread's own motor_enabled safety flag (see gui.py's
      "Разрешить управление мотором" checkbox) -- that flag, not
      anything in this file, is the final word on whether the motor
      can physically move.
"""

import time
import threading
from collections import deque
from queue import Empty

from PyQt5.QtCore import QThread, pyqtSignal

from logging_setup import log_print, log_exception


class MovementModel:
    """
    Pure computation: one rolling-window EMG RMS per channel (kept for
    diagnostics/logging on every channel), combined with angle/velocity
    (dominant) and load (disabled by default) into ONE motor assist
    command, via a hysteresis movement-onset gate.

    Deliberately isolated from all threading concerns so it can be
    tested or swapped out on its own without touching ModelThread.
    """

    # ---------------------------------------------------------------
    # Tunable gains/thresholds -- PLACEHOLDER values. Tune these on the
    # bench, against real recordings, with the motor mechanically
    # disconnected, before trusting this near a person. Kept as class
    # attributes so they're easy to find without hunting through the
    # compute logic.
    # ---------------------------------------------------------------
    DEFAULT_WINDOW_MS = 100.0            # RMS window length

    K_VELOCITY = 1.0                     # dominant term, applied to normalized velocity
    K_EMG = 0.02                         # ~1-2% contribution, deliberately small
    K_FORCE = 0.0                        # disabled until load calibration is trusted

    MAX_EXPECTED_VELOCITY_DEG_S = 150.0  # normalizes velocity to roughly [-1, 1]
                                          # before gains apply -- set this from the
                                          # peak angular_velocity in your own recordings

    EMG_REFERENCE_DEFAULT = 50.0         # smoothed |EMG| RMS at a moderate deliberate
                                          # contraction -- raw amplifier units, MUST be
                                          # recalibrated per subject/gain (see channel_count()
                                          # note below); this placeholder normalizes nothing
                                          # meaningful on its own

    VELOCITY_GATE_DEG_S = 3.0            # angular velocity considered "moving"
    EMG_GATE = 0.15                      # normalized EMG envelope considered "active"
    GATE_RELEASE_VELOCITY_DEG_S = 1.0    # hysteresis: lower thresholds release the gate
    GATE_RELEASE_EMG = 0.08

    MAX_COMMAND = 1.0
    MAX_COMMAND_SLEW = 0.15              # max change in command per compute() call

    # Optional hard safety clamp -- if set, command is forced to 0.0
    # outside this angle range regardless of everything else. None
    # (the default) disables the clamp entirely.
    ANGLE_SAFETY_MIN_DEG = None
    ANGLE_SAFETY_MAX_DEG = None

    def __init__(
        self,
        window_ms=None,
        emg_reference=None,
    ):
        self.window_s = (window_ms if window_ms is not None else self.DEFAULT_WINDOW_MS) / 1000.0
        self.emg_reference = max(
            emg_reference if emg_reference is not None else self.EMG_REFERENCE_DEFAULT,
            1e-6,
        )

        # One rolling window (deque of (t_rel, value)) and one running
        # sum-of-squares per EMG channel, so RMS is O(1) per incoming
        # sample instead of re-summing the whole window every time --
        # this runs once per channel per EMG sample, at full LSL rate,
        # so it's worth avoiding the O(window) cost per call.
        #
        # Channel count isn't known until the first sample arrives
        # (same reasoning as LSLStreamWorker only knowing its own
        # channel_count after connect()), so these start empty and are
        # lazily sized on the first push_emg_sample() call. Only
        # channel 0 (this rig's one real biceps channel) drives the
        # assist command below -- any others are tracked for logging
        # only.
        self._buffers = None   # list[deque] once sized
        self._sumsq = None     # list[float] once sized

        self._gated_on = False
        self._prev_command = 0.0

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
        """Clears all rolling windows plus gate/command state -- called
        between recordings so nothing from the previous session bleeds
        into the next. Channel count is rediscovered from scratch on
        the next sample."""
        self._buffers = None
        self._sumsq = None
        self._gated_on = False
        self._prev_command = 0.0

    # =====================================================
    # GATE + OUTPUT
    # =====================================================

    def _gate_open(self, velocity_deg_s, emg_norm):
        """
        Movement-onset gate with hysteresis: opens on EITHER a real
        angular velocity OR an EMG spike (whichever fires first --
        EMG typically leads by ~50-150ms), and only closes once BOTH
        drop below their (lower) release thresholds, so it doesn't
        chatter on/off right at the boundary.
        """
        moving = (
            abs(velocity_deg_s) > self.VELOCITY_GATE_DEG_S
            or emg_norm > self.EMG_GATE
        )
        releasing = (
            abs(velocity_deg_s) < self.GATE_RELEASE_VELOCITY_DEG_S
            and emg_norm < self.GATE_RELEASE_EMG
        )

        if not self._gated_on and moving:
            self._gated_on = True
        elif self._gated_on and releasing:
            self._gated_on = False

        return self._gated_on

    def compute(self, angle_deg, velocity_deg_s, load_norm=None):
        """
        Combines the primary (channel 0) EMG RMS with angle/velocity
        (dominant) and load (disabled by default, via K_FORCE) into
        ONE motor assist command -- there is exactly one motor.

        Returns (rms_list, movement_params) for compatibility with
        ModelThread/StorageThread: rms_list still has one entry per
        EMG channel (diagnostics), but movement_params is now a
        SINGLE-element list [assist_command], not one per channel.
        """
        rms_list = self.emg_rms()
        emg_rms_primary = rms_list[0] if rms_list else 0.0
        emg_norm = min(1.0, emg_rms_primary / self.emg_reference)

        gate_open = self._gate_open(velocity_deg_s, emg_norm)

        safety_blocked = (
            (self.ANGLE_SAFETY_MIN_DEG is not None and angle_deg < self.ANGLE_SAFETY_MIN_DEG)
            or (self.ANGLE_SAFETY_MAX_DEG is not None and angle_deg > self.ANGLE_SAFETY_MAX_DEG)
        )

        if not gate_open or safety_blocked:
            command = 0.0
        else:
            velocity_norm = velocity_deg_s / self.MAX_EXPECTED_VELOCITY_DEG_S
            direction = 1.0 if velocity_deg_s >= 0 else -1.0

            force_term = 0.0
            if load_norm is not None and self.K_FORCE != 0.0:
                force_term = self.K_FORCE * load_norm

            command = (
                self.K_VELOCITY * velocity_norm
                + self.K_EMG * emg_norm * direction
                + force_term
            )
            command = max(-self.MAX_COMMAND, min(self.MAX_COMMAND, command))

        # Rate-limit so the command sent to the motor never jumps
        # discontinuously between successive compute() calls.
        delta = command - self._prev_command
        delta = max(-self.MAX_COMMAND_SLEW, min(self.MAX_COMMAND_SLEW, delta))
        command = self._prev_command + delta
        self._prev_command = command

        return rms_list, [command]


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

    # t_rel, emg_rms_list (object: list[float]), angle_deg,
    # angular_velocity_deg_s, movement_param_list (object: [assist_command])
    #
    # Fixed-arity float args won't work for the EMG list since the
    # channel count isn't known until runtime -- 'object' lets the
    # signal carry a plain Python list of whatever length the stream
    # turns out to have, same idea PyQt uses for any variable-shaped
    # payload. movement_param_list is always length 1 now (one motor),
    # kept as a list for wire-format compatibility with header()/CSV.
    output = pyqtSignal(float, object, float, float, object)

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

        # Latest angle/velocity/load, set from the GUI thread's
        # on_mcu() handler. Plain lock rather than a Qt signal -- see
        # module docstring. Velocity is derived HERE, from consecutive
        # set_latest_angle() calls, since angle is the only signal in
        # this thread with a reliable per-sample timestamp available
        # at the point it arrives (EMG samples don't map 1:1 to angle
        # samples in real time -- different rates, different queues).
        self._state_lock = threading.Lock()
        self._latest_angle = 0.0
        self._latest_load = None
        self._velocity_smooth = 0.0
        self._prev_angle_for_velocity = None
        self._prev_angle_time = None

        log_print("[MODEL] ModelThread created")

    VELOCITY_SMOOTHING_ALPHA = 0.35  # EMA smoothing for the derived angular velocity

    # =====================================================
    # EXTERNAL INPUT (called from other threads)
    # =====================================================

    def set_latest_angle(self, angle_deg):
        """
        Thread-safe setter, called from the GUI thread's on_mcu()
        handler once per MCU sample (~10 Hz). Also derives a smoothed
        angular velocity (deg/s) from consecutive calls -- see the
        note in __init__ on why velocity is computed here rather than
        passed in.
        """
        now = time.perf_counter()

        with self._state_lock:

            if self._prev_angle_time is not None:
                dt = now - self._prev_angle_time
                if dt > 0:
                    raw_velocity = (angle_deg - self._prev_angle_for_velocity) / dt
                    a = self.VELOCITY_SMOOTHING_ALPHA
                    self._velocity_smooth = a * raw_velocity + (1 - a) * self._velocity_smooth

            self._prev_angle_for_velocity = angle_deg
            self._prev_angle_time = now
            self._latest_angle = angle_deg

    def set_latest_load(self, load_norm):
        """Thread-safe setter, called from the GUI thread's on_mcu() handler."""
        with self._state_lock:
            self._latest_load = load_norm

    def _get_latest_state(self):
        with self._state_lock:
            return self._latest_angle, self._velocity_smooth, self._latest_load

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
                    self.reset_sync()
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

                angle, velocity, load = self._get_latest_state()
                rms_list, movement_params = self.model.compute(angle, velocity, load)

                self.output.emit(t_rel, rms_list, angle, velocity, movement_params)

                if self.out_queue is not None:
                    self.out_queue.put((t_rel, *rms_list, angle, velocity, *movement_params))

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
        cols += ["angle_deg", "angular_velocity_deg_s", "assist_command"]
        return cols

    # =====================================================
    # SYNC RESET (called explicitly from main.py at recording start,
    # same convention as LSLStreamWorker.reset_sync() / MCUThread.reset_sync())
    # =====================================================

    def reset_sync(self):
        self.model.reset()
        self.last_emit_time = 0.0
        self.was_recording = False
        with self._state_lock:
            self._velocity_smooth = 0.0
            self._prev_angle_for_velocity = None
            self._prev_angle_time = None
        log_print("[MODEL] reset_sync() called")

    # =====================================================
    # STOP
    # =====================================================

    def stop(self):
        self.running = False
        self.quit()
        self.wait(1000)