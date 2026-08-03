"""
gui.py

UI layout and widget construction for the main application window.
Split out from main.py to keep window/thread wiring and recording
logic separate from layout code.

MainApp (in main.py) inherits from GuiMixin alongside QMainWindow, so
every `self.<widget>` created here becomes an attribute on the running
MainApp instance. This file only builds and arranges widgets and
defines how they look in each state — it does not own any
recording/session logic itself; the methods it calls out to
(start_recording, populate_ports, show_about, etc.) are implemented
in main.py.

Also home to LoadCalibrationDialog, the per-trial load-cell
calibration wizard shown at the start of every recording (see
main.py's start_recording()): an explicit zero step, followed by a
two-direction max-pull capture, or a skip-to-defaults path.
"""

from datetime import datetime

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLineEdit, QTextEdit, QLabel,
    QGroupBox, QComboBox, QAction, QDialog
)
import pyqtgraph as pg


# Stream keys shown in the status panel, in display order, paired with
# a human-readable label. Must match the keys used when registering
# streams with StorageThread in main.py.
STATUS_STREAMS = [
    ("data", "EMG (data.csv)"),
    ("raw_data", "Raw (raw_data.csv)"),
    ("events", "Events (events.csv)"),
    ("raw_events", "Raw events (raw_events.csv)"),
    ("mcu", "MCU (mcu.csv)"),
]


class GuiMixin:
    """
    Mixin providing all widget construction for MainApp. Expects to be
    combined with QMainWindow (for menuBar()/setCentralWidget()) and
    with a class exposing the callback methods referenced below
    (start_recording, stop_recording, open_data_folder, open_plotter,
    populate_ports, on_port_selected, show_about, open_log_file,
    open_settings_file).
    """

    # =====================================================
    # MENU
    # =====================================================

    def init_menu(self):
        menubar = self.menuBar()
        help_menu = menubar.addMenu("Справка")

        about_action = QAction("О программе", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

        open_log_action = QAction("Открыть лог-файл", self)
        open_log_action.triggered.connect(self.open_log_file)
        help_menu.addAction(open_log_action)

        open_settings_action = QAction("Открыть файл настроек", self)
        open_settings_action.triggered.connect(self.open_settings_file)
        help_menu.addAction(open_settings_action)

    # =====================================================
    # MAIN LAYOUT
    # =====================================================

    def init_ui(self):

        self.setWindowTitle("Acquisition System")

        # -------------------------------------------------
        # RECORDING BANNER (top of window, spans full width)
        # -------------------------------------------------
        self.status_banner = QLabel()
        self.status_banner.setAlignment(Qt.AlignCenter)
        self.status_banner.setFixedHeight(32)
        self._set_banner_idle()

        # -------------------------------------------------
        # PRIMARY CONTROLS — Start/Stop, visually distinct
        # from the utility buttons below since these are the
        # only two touched while a session is actually running
        # -------------------------------------------------
        self.start_btn = QPushButton("START")
        self.stop_btn = QPushButton("STOP")

        self.start_btn.setStyleSheet(
            "QPushButton { background-color: #2e7d32; color: white; "
            "font-weight: bold; padding: 10px; font-size: 14px; }"
        )
        self.stop_btn.setStyleSheet(
            "QPushButton { padding: 10px; font-size: 14px; }"
        )
        self.stop_btn.setEnabled(False)

        self.start_btn.clicked.connect(self.start_recording)
        self.stop_btn.clicked.connect(self.stop_recording)

        primary_btn_row = QHBoxLayout()
        primary_btn_row.addWidget(self.start_btn)
        primary_btn_row.addWidget(self.stop_btn)

        # -------------------------------------------------
        # UTILITY CONTROLS — pre/post-session only, disabled
        # while recording (set_recording_ui_state handles that)
        # -------------------------------------------------
        self.open_folder_btn = QPushButton("Открыть папку с данными")
        self.plot_session_btn = QPushButton("График сохранённого сеанса")

        self.open_folder_btn.clicked.connect(self.open_data_folder)
        self.plot_session_btn.clicked.connect(self.open_plotter)

        utility_btn_row = QHBoxLayout()
        utility_btn_row.addWidget(self.open_folder_btn)
        utility_btn_row.addWidget(self.plot_session_btn)

        # -------------------------------------------------
        # STREAM STATUS PANEL — row counts per CSV, updated by
        # MainApp.update_status_panel() on a timer. This is the
        # actual "is data being saved" indicator, independent of
        # whether the live plots happen to be moving.
        # -------------------------------------------------
        status_box = QGroupBox("Статус потоков")
        status_grid = QGridLayout()

        self.stream_status_labels = {}

        for row, (key, display_name) in enumerate(STATUS_STREAMS):
            name_label = QLabel(display_name)
            count_label = QLabel("—")
            count_label.setAlignment(Qt.AlignRight)
            status_grid.addWidget(name_label, row, 0)
            status_grid.addWidget(count_label, row, 1)
            self.stream_status_labels[key] = count_label

        status_box.setLayout(status_grid)

        # -------------------------------------------------
        # LEFT SIDE (banner + buttons + status + plots)
        # -------------------------------------------------
        left_layout = QVBoxLayout()
        left_layout.addWidget(self.status_banner)
        left_layout.addLayout(primary_btn_row)
        left_layout.addLayout(utility_btn_row)
        left_layout.addWidget(status_box)

        # plot widget gets inserted later in init_plot()
        self.left_layout = left_layout

        # -------------------------------------------------
        # SESSION INFO GROUP (subject metadata only)
        # -------------------------------------------------
        self.name_edit = QLineEdit()
        self.name_edit.setText("Пациент 1")

        self.session_edit = QLineEdit()
        self.session_edit.setText("Номер 0")

        self.date_label = QLabel()
        self.date_label.setText(datetime.now().strftime("%d/%m/%Y %H:%M:%S"))

        self.comment_edit = QTextEdit()
        self.comment_edit.setPlaceholderText(
            "Для комментариев\n\n\n"
            "Информация в полях участник, номер сеанса, время-дата и комментарии сохраняется "
            "ТОЛЬКО при начале сеанса записи (нажатии кнопки старт)"
        )

        session_box = QGroupBox("Информация о сеансе")
        session_layout = QVBoxLayout()
        session_layout.addWidget(QLabel("Имя"))
        session_layout.addWidget(self.name_edit)
        session_layout.addWidget(QLabel("Номер сеанса"))
        session_layout.addWidget(self.session_edit)
        session_layout.addWidget(QLabel("Время проведения сеанса"))
        session_layout.addWidget(self.date_label)
        session_layout.addWidget(QLabel("Примечания"))
        session_layout.addWidget(self.comment_edit, stretch=1)
        session_box.setLayout(session_layout)

        # -------------------------------------------------
        # DEVICE GROUP (COM port + connection status) — split
        # out from session info since it's hardware setup, not
        # session metadata
        # -------------------------------------------------
        self.port_combo = QComboBox()
        self.port_refresh_btn = QPushButton("Обновить")

        self.port_refresh_btn.clicked.connect(self.populate_ports)
        self.port_combo.activated[str].connect(self.on_port_selected)

        port_row = QHBoxLayout()
        port_row.addWidget(self.port_combo, stretch=1)
        port_row.addWidget(self.port_refresh_btn)

        self.mcu_status_label = QLabel("МК: —")
        self.lsl_status_label = QLabel("LSL: —")

        device_box = QGroupBox("Устройство")
        device_layout = QVBoxLayout()
        device_layout.addWidget(QLabel("COM порт"))
        device_layout.addLayout(port_row)
        device_layout.addWidget(self.mcu_status_label)
        device_layout.addWidget(self.lsl_status_label)
        device_box.setLayout(device_layout)

        self.populate_ports()

        # -------------------------------------------------
        # RIGHT SIDE
        # -------------------------------------------------
        right_layout = QVBoxLayout()
        right_layout.addWidget(session_box)
        right_layout.addWidget(device_box)
        right_layout.addStretch()

        right_widget = QWidget()
        right_widget.setLayout(right_layout)
        right_widget.setMaximumWidth(320)

        # -------------------------------------------------
        # FOOTER — always-visible proof of where data is landing,
        # not just a console print
        # -------------------------------------------------
        self.saved_to_label = QLabel("")
        self.saved_to_label.setStyleSheet("color: gray; font-size: 11px;")

        # -------------------------------------------------
        # MAIN LAYOUT
        # -------------------------------------------------
        left_widget = QWidget()
        left_widget.setLayout(left_layout)

        top_layout = QHBoxLayout()
        top_layout.addWidget(left_widget, stretch=4)
        top_layout.addWidget(right_widget, stretch=1)

        outer_layout = QVBoxLayout()
        outer_layout.addLayout(top_layout)
        outer_layout.addWidget(self.saved_to_label)

        container = QWidget()
        container.setLayout(outer_layout)

        self.setCentralWidget(container)

    # =====================================================
    # PLOT SETUP (unchanged from before — live plots stay)
    # =====================================================

    def init_plot(self):

        pg.setConfigOption("background", "w")
        pg.setConfigOption("foreground", "k")

        self.plot = pg.GraphicsLayoutWidget()
        self.left_layout.addWidget(self.plot)

        self.emg_plot = self.plot.addPlot(title="EMG")
        self.emg_curve = self.emg_plot.plot(pen="k")

        self.plot.nextRow()
        self.angle_plot = self.plot.addPlot(title="Angle")
        self.angle_curve = self.angle_plot.plot(pen="b")

        self.plot.nextRow()
        self.load_plot = self.plot.addPlot(title="Load")
        self.load_curve = self.load_plot.plot(pen="r")

        pg.setConfigOptions(antialias=False)

        for curve in [self.emg_curve, self.angle_curve, self.load_curve]:
            curve.setDownsampling(auto=True, method='peak')
            curve.setClipToView(True)

        for p in [self.emg_plot, self.angle_plot, self.load_plot]:
            p.setMouseEnabled(x=False, y=False)
            p.hideButtons()
            p.setMenuEnabled(False)

    # =====================================================
    # RECORDING-STATE VISUALS
    # =====================================================
    # Called from MainApp.start_recording()/stop_recording() so every
    # widget affected by recording state flips together in one place
    # and can never drift out of sync with each other.
    # =====================================================

    def _set_banner_idle(self):
        self.status_banner.setText("Не идёт запись")
        self.status_banner.setStyleSheet(
            "background-color: #444; color: white; font-weight: bold;"
        )

    def _set_banner_recording(self, subject_label):
        self.status_banner.setText(f"● ЗАПИСЬ — {subject_label}")
        self.status_banner.setStyleSheet(
            "background-color: #c62828; color: white; font-weight: bold;"
        )

    def set_recording_ui_state(self, recording: bool, subject_label: str = ""):
        if recording:
            self._set_banner_recording(subject_label)
        else:
            self._set_banner_idle()

        self.start_btn.setEnabled(not recording)
        self.stop_btn.setEnabled(recording)
        self.open_folder_btn.setEnabled(not recording)
        self.plot_session_btn.setEnabled(not recording)
        self.port_combo.setEnabled(not recording)
        self.port_refresh_btn.setEnabled(not recording)

    def update_stream_status(self, row_counts: dict):
        """row_counts: {stream_key: row_count}, from StorageThread.get_row_counts()."""
        for key, label in self.stream_status_labels.items():
            count = row_counts.get(key)
            label.setText(str(count) if count is not None else "—")

    def update_device_status(self, mcu_connected: bool, lsl_connected_count: int, lsl_total: int):
        self.mcu_status_label.setText(
            f"МК: {'подключен' if mcu_connected else 'нет соединения'}"
        )
        self.mcu_status_label.setStyleSheet(
            f"color: {'#2e7d32' if mcu_connected else '#c62828'};"
        )

        self.lsl_status_label.setText(
            f"LSL: найдено потоков {lsl_connected_count}/{lsl_total}"
        )
        self.lsl_status_label.setStyleSheet(
            f"color: {'#2e7d32' if lsl_connected_count == lsl_total else '#c62828'};"
        )

    def set_saved_to(self, folder):
        self.saved_to_label.setText(f"Сохранено в: {folder}" if folder else "")


class LoadCalibrationDialog(QDialog):
    """
    Per-trial load-cell calibration wizard, run at the start of every
    recording (the rig gets re-mounted per subject/arm, so this can't
    be a once-and-saved calibration).

    Flow:
      choice        -> Calibrate / Use default values / Cancel
      zero_wait      -> "hand off the sensor, press SPACE" (Calibrate only)
      zero_capturing -> 5s automatic average of raw values -> zero established
      zero_done      -> shows the zero value, "press SPACE to continue"
      wait1 / cap1   -> direction 1 max-pull capture (existing spacebar-gated flow)
      wait2 / cap2   -> direction 2 max-pull capture
      done           -> shows both peaks, "press SPACE to start recording"

    "Use default values" skips straight to accept() without touching
    load_zero at all — MCUThread's own silent auto-zero (unlocked on
    the way out) picks it up exactly as it did before calibration
    existed.

    While this dialog is open, mcu_thread.load_zero_locked is held
    True so MCUThread's background auto-zero can't race the explicit
    zero step above — see mcu_thread.py.
    """

    ZERO_SECONDS = 5.0
    CAPTURE_SECONDS = 2.5

    def __init__(self, mcu_thread, parent=None):
        super().__init__(parent)

        self.mcu_thread = mcu_thread

        self.stage = "choice"
        self.zero_value = None
        self._zero_samples = []
        self.peak_dir1 = None
        self.peak_dir2 = None
        self._current_peak = None
        self.calibration_skipped = False

        # Hold off MCUThread's own silent auto-zero for the whole
        # time this dialog is open — see class docstring.
        self.mcu_thread.load_zero_locked = True
        self.mcu_thread.load_zero = None
        self.mcu_thread.load_zero_samples = []

        self.setWindowTitle("Калибровка датчика нагрузки")
        self.setModal(True)
        self.setMinimumWidth(440)

        self.instruction_label = QLabel(
            "Выполнить калибровку датчика нагрузки перед этим сеансом?"
        )
        self.instruction_label.setWordWrap(True)
        self.instruction_label.setStyleSheet("font-size: 14px; font-weight: bold;")

        self.readout_label = QLabel("")
        self.readout_label.setStyleSheet("color: gray;")

        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)

        # -------------------------------------------------
        # Choice buttons — only visible on the initial screen.
        # NoFocus + no auto-default so a later Space press (used for
        # the actual capture flow) can never accidentally trigger one
        # of these via button-focus space-activation.
        # -------------------------------------------------
        self.calibrate_btn = QPushButton("Откалибровать")
        self.defaults_btn = QPushButton("Значения по умолчанию")
        self.cancel_btn = QPushButton("Отмена")

        for btn in (self.calibrate_btn, self.defaults_btn, self.cancel_btn):
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setAutoDefault(False)
            btn.setDefault(False)

        self.calibrate_btn.clicked.connect(self._start_calibration)
        self.defaults_btn.clicked.connect(self._use_defaults)
        self.cancel_btn.clicked.connect(self.reject)

        button_row = QHBoxLayout()
        button_row.addWidget(self.calibrate_btn)
        button_row.addWidget(self.defaults_btn)
        button_row.addWidget(self.cancel_btn)

        layout = QVBoxLayout()
        layout.addWidget(self.instruction_label)
        layout.addWidget(self.readout_label)
        layout.addWidget(self.result_label)
        layout.addLayout(button_row)
        self.setLayout(layout)

        self.capture_timer = QTimer(self)
        self.capture_timer.setSingleShot(True)
        self.capture_timer.timeout.connect(self._on_timer_done)

        self.mcu_thread.data.connect(self._on_data)

    # -------------------------------------------------
    # CHOICE SCREEN ACTIONS
    # -------------------------------------------------

    def _start_calibration(self):
        self.calibrate_btn.hide()
        self.defaults_btn.hide()
        # cancel_btn stays visible/available for the whole flow

        self.stage = "zero_wait"
        self.instruction_label.setText(
            "Уберите руку/груз с датчика.\n"
            "Нажмите ПРОБЕЛ, чтобы установить ноль (5 секунд)."
        )
        self.readout_label.setText("")

    def _use_defaults(self):
        self.calibration_skipped = True
        # Deliberately leave load_zero alone (still None) and unlock
        # -- MCUThread's own silent auto-zero takes over from here,
        # exactly as it did before per-trial calibration existed.
        self.mcu_thread.load_zero_locked = False
        self.accept()

    # -------------------------------------------------
    # ZERO STEP
    # -------------------------------------------------

    def _begin_zero_capture(self):
        self.stage = "zero_capturing"
        self._zero_samples = []
        self.instruction_label.setText("Установка нуля... не трогайте датчик.")
        self.capture_timer.start(int(self.ZERO_SECONDS * 1000))

    def _finish_zero_capture(self):

        if self._zero_samples:
            self.zero_value = sum(self._zero_samples) / len(self._zero_samples)
        else:
            # No samples arrived during the window (e.g. MCU not
            # connected) -- fall back to 0.0 rather than crashing;
            # the resulting calibration will be meaningless, but the
            # operator can see that from the live readout staying
            # blank and cancel/retry.
            self.zero_value = 0.0

        self.mcu_thread.set_load_zero(self.zero_value)

        self._append_result(f"Ноль установлен: {self.zero_value:.0f}")
        self.stage = "zero_done"
        self.instruction_label.setText(
            "Ноль установлен. Нажмите ПРОБЕЛ, чтобы продолжить калибровку усилия."
        )
        self.readout_label.setText("")

    # -------------------------------------------------
    # MAX-PULL CAPTURE (direction 1 / direction 2)
    # -------------------------------------------------

    def _show_waiting_prompt(self):
        direction_num = 1 if self.stage == "wait1" else 2
        self.instruction_label.setText(
            f"Направление {direction_num}: максимальное усилие.\n"
            f"Нажмите ПРОБЕЛ, когда участник готов тянуть."
        )
        self.readout_label.setText("Текущее значение: —")

    def _begin_capture(self):
        self.stage = "cap1" if self.stage == "wait1" else "cap2"
        self._current_peak = None

        direction_num = 1 if self.stage == "cap1" else 2
        self.instruction_label.setText(f"Направление {direction_num}: тяните сейчас!")

        self.capture_timer.start(int(self.CAPTURE_SECONDS * 1000))

    def _finish_capture(self):

        if self.stage == "cap1":
            self.peak_dir1 = self._current_peak
            self._append_result(f"Направление 1: пик = {self._fmt(self.peak_dir1)}")
            self.stage = "wait2"
            self._show_waiting_prompt()

        elif self.stage == "cap2":
            self.peak_dir2 = self._current_peak
            self._append_result(f"Направление 2: пик = {self._fmt(self.peak_dir2)}")
            self.stage = "done"
            # Calibration flow complete -- load_zero is already set,
            # so the lock no longer matters, but clear it for
            # cleanliness/consistency with the other exit paths.
            self.mcu_thread.load_zero_locked = False
            self.instruction_label.setText(
                "Калибровка завершена. Нажмите ПРОБЕЛ, чтобы начать запись."
            )
            self.readout_label.setText("")

    # -------------------------------------------------
    # SHARED TIMER DISPATCH
    # -------------------------------------------------

    def _on_timer_done(self):
        if self.stage == "zero_capturing":
            self._finish_zero_capture()
        elif self.stage in ("cap1", "cap2"):
            self._finish_capture()

    def _append_result(self, line):
        current = self.result_label.text()
        self.result_label.setText(f"{current}\n{line}" if current else line)

    @staticmethod
    def _fmt(value):
        return "—" if value is None else f"{value:.0f}"

    # -------------------------------------------------
    # DATA / INPUT
    # -------------------------------------------------

    def _on_data(self, pc_perf_ts, mcu_ts, angle_raw, angle, load_raw, load_norm):

        if self.stage == "zero_capturing":
            self._zero_samples.append(load_raw)
            self.readout_label.setText(f"Текущее сырое значение: {load_raw:.0f}")
            return

        if self.stage not in ("cap1", "cap2"):
            return

        zero = self.mcu_thread.load_zero

        if zero is None:
            # Shouldn't happen at this point (zero step already ran
            # before cap1/cap2 becomes reachable) -- guard anyway.
            return

        corrected = load_raw - zero
        self.readout_label.setText(f"Текущее значение: {corrected:.0f}")

        if self._current_peak is None or abs(corrected) > abs(self._current_peak):
            self._current_peak = corrected

    def keyPressEvent(self, event):

        if event.key() != Qt.Key_Space:
            super().keyPressEvent(event)
            return

        if self.stage == "zero_wait":
            self._begin_zero_capture()

        elif self.stage == "zero_done":
            self.stage = "wait1"
            self._show_waiting_prompt()

        elif self.stage in ("wait1", "wait2"):

            if self.mcu_thread.load_zero is None:
                self.instruction_label.setText(
                    self.instruction_label.text() +
                    "\n(Ноль ещё не установлен, подождите...)"
                )
                return

            self._begin_capture()

        elif self.stage == "done":
            self.accept()

        # Space does nothing on the "choice" screen — deliberate,
        # button-driven only, so a stray spacebar press can't
        # accidentally start or skip calibration before the operator
        # has read the prompt.

    def reject(self):
        # Operator cancelled (button or Esc) at any stage -- make
        # sure MCUThread isn't left permanently locked out of its own
        # auto-zero.
        self.mcu_thread.load_zero_locked = False
        super().reject()

    def closeEvent(self, event):
        self.mcu_thread.load_zero_locked = False
        try:
            self.mcu_thread.data.disconnect(self._on_data)
        except Exception:
            pass
        super().closeEvent(event)

    # -------------------------------------------------
    # RESULT
    # -------------------------------------------------

    def get_calibration(self):
        """
        Returns (pos_peak, neg_peak) — measured peak raw deviations
        from zero for whichever direction came out positive/negative
        (not raw capture order), or (None, None) if calibration was
        skipped. mcu_thread.set_load_calibration() applies the 2x
        headroom multiplier to turn these into actual spans.
        """
        if self.calibration_skipped:
            return None, None

        peaks = [p for p in (self.peak_dir1, self.peak_dir2) if p is not None]

        pos_candidates = [p for p in peaks if p >= 0]
        neg_candidates = [p for p in peaks if p < 0]

        pos_peak = max(pos_candidates) if pos_candidates else None
        neg_peak = abs(min(neg_candidates)) if neg_candidates else None

        return pos_peak, neg_peak