"""
main.py

Application entry point and main GUI window (PyQt5). Wires together
all the acquisition threads (LSL streams, MCU serial, UDP forwarding,
CSV storage), owns the live plot, and handles session start/stop and
recording-session bookkeeping (session folder, session_info.txt).

GUI layout/widget construction lives in gui.py (GuiMixin) — this file
owns thread wiring, recording control, and data handling only.
"""

import sys
import threading
import os
import subprocess
from queue import Queue
from datetime import datetime
from utils import data_dir
import time

from logging_setup import init_logging
init_logging()

from PyQt5.QtWidgets import QApplication, QMainWindow, QMessageBox, QFileDialog
from PyQt5.QtCore import QTimer

from gui import GuiMixin

from lsl_thread import LSLStreamWorker
from mcu_thread import MCUThread
from storage_thread import StorageThread
from udp_sender_thread import UDPSenderThread
from plotter import plot_session_folder

import serial.tools.list_ports
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtCore import QUrl
from config import load_config, save_config, settings_path
from utils import logs_dir
from version import APP_NAME, APP_VERSION, get_build_date


class MainApp(QMainWindow, GuiMixin):

    def __init__(self):
        super().__init__()

        # =====================================================
        # STATE
        # =====================================================
        self.recording = False
        self.start_event = threading.Event()
        self.folder = None

        self.window_size = 10

        # -------------------------------------------------
        # EMG LIVE PLOT DISPLAY SETTINGS
        # -------------------------------------------------
        # This affects ONLY the live EMG plot X axis.
        # CSV still receives real synchronized timestamps.
        # -------------------------------------------------
        self.emg_plot_fs = 1000.0
        self.emg_plot_sample_index = 0

        # =====================================================
        # TIME BASE
        # =====================================================
        self.session_start = None

        # Kept for compatibility
        self.emg_start_time = None

        # =====================================================
        # QUEUES
        # =====================================================
        self.data_queue = Queue()
        self.raw_data_queue = Queue()
        self.events_queue = Queue()
        self.raw_events_queue = Queue()
        self.mcu_queue = Queue()

        # =====================================================
        # BUFFERS
        # =====================================================
        # EMG plot buffers:
        #   self.t_emg = display-only sample-index time
        #   self.v_emg = EMG values
        #
        # MCU plot buffers:
        #   real relative timestamps
        # =====================================================
        self.t_emg, self.v_emg = [], []
        self.t_mcu, self.angle, self.load = [], [], []

        # =====================================================
        # THREADS
        # =====================================================
        self.lsl_data = LSLStreamWorker(
            label="data", stream_type="Data",
            start_event=self.start_event, out_queue=self.data_queue,
            plot_hz=50.0,   # this one drives the live EMG plot, as before
        )
        self.lsl_raw_data = LSLStreamWorker(
            label="raw_data", stream_type="Raw_Data",
            start_event=self.start_event, out_queue=self.raw_data_queue,
            plot_hz=0.0,    # logged only, not plotted
        )
        self.lsl_events = LSLStreamWorker(
            label="events", stream_type="Events",
            start_event=self.start_event, out_queue=self.events_queue,
            name_must_not_contain="raw", plot_hz=0.0,
            pull_timeout=0.5,   # events are sparse, no need to poll at 20ms
        )
        self.lsl_raw_events = LSLStreamWorker(
            label="raw_events", stream_type="Events",
            start_event=self.start_event, out_queue=self.raw_events_queue,
            name_must_contain="raw", plot_hz=0.0,
            pull_timeout=0.5,
        )

        self.lsl_workers = (self.lsl_data, self.lsl_raw_data, self.lsl_events, self.lsl_raw_events)

        self.cfg = load_config()

        mcu_port = self.cfg.get("mcu", "port", fallback="COM6")
        mcu_baud = self.cfg.getint("mcu", "baud", fallback=115200)

        self.mcu_thread = MCUThread(mcu_port, mcu_baud, self.start_event)
        self.udp_thread = UDPSenderThread(self.start_event)

        self.mcu_thread.raw.connect(self.udp_thread.on_data)

        self.storage_thread = StorageThread(
            folder_getter=self.get_folder,
            recording_flag=self.is_recording,
        )

        self.storage_thread.register_stream("data",       self.data_queue,       "data.csv",       header=self.lsl_data.header)
        self.storage_thread.register_stream("raw_data",   self.raw_data_queue,   "raw_data.csv",   header=self.lsl_raw_data.header)
        self.storage_thread.register_stream("events",     self.events_queue,     "events.csv",     header=self.lsl_events.header, max_rows_per_cycle=50)
        self.storage_thread.register_stream("raw_events", self.raw_events_queue, "raw_events.csv", header=self.lsl_raw_events.header, max_rows_per_cycle=50)
        self.storage_thread.register_stream("mcu",        self.mcu_queue,        "mcu.csv",        header=["pc_perf_counter_s", "mcu_timestamp_us", "angle_raw", "angle_deg", "load_raw", "load_norm"])

        self.lsl_data.data.connect(self.on_emg)
        self.mcu_thread.data.connect(self.on_mcu)

        # UI (built by GuiMixin, defined in gui.py)
        self.init_ui()
        self.init_menu()
        self.init_plot()

        # start threads
        self.lsl_data.start()
        self.lsl_raw_data.start()
        self.lsl_events.start()
        self.lsl_raw_events.start()
        self.mcu_thread.start()
        self.storage_thread.start()
        self.udp_thread.start()

        # plot timer
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_plot)

        # status panel timer — row counts + device connection state.
        # Runs independently of the plot timer so status stays visible
        # (e.g. "MCU not connected") even when not recording.
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self.update_status_panel)
        self.status_timer.start(1000)

    # =====================================================
    # MENU CALLBACKS
    # =====================================================

    def show_about(self):
        QMessageBox.information(
            self,
            "О программе",
            f"{APP_NAME}\nВерсия: {APP_VERSION}\nСборка от: {get_build_date()}\n\n"
            f"Данные:\n{data_dir()}\n"
            f"Логи:\n{logs_dir()}\n"
            f"Настройки:\n{settings_path()}"
        )

    def open_log_file(self):
        path = logs_dir() / "app.log"
        if not path.exists():
            QMessageBox.warning(self, "Файл не найден", f"Лог-файл не найден:\n{path}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def open_settings_file(self):
        path = settings_path()
        if not path.exists():
            QMessageBox.warning(self, "Файл не найден", f"Файл настроек не найден:\n{path}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # =====================================================
    # SESSION
    # =====================================================

    def create_session(self):

        # Use a writable folder for CSVs
        folder_base = data_dir()

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]

        self.folder = folder_base / ts
        self.folder.mkdir(parents=True, exist_ok=True)

        # -------------------------
        # SAVE SESSION INFO
        # -------------------------
        info_file = self.folder / "session_info.txt"

        with open(info_file, "w", encoding="utf-8") as f:

            f.write(f"Время и дата начала сеанса: {ts}\n")
            f.write(f"Участник: {self.name_edit.text()}\n")
            f.write(f"Номер сеанса: {self.session_edit.text()}\n")
            f.write("\n")

            f.write("Комментарии:\n")
            f.write(self.comment_edit.toPlainText())
            f.write("\n\n")
            f.write(
                f"Session start perf_counter: "
                f"{self.session_start:.9f}\n"
            )

        print("\n[SESSION START]")
        print(self.folder)

        self.set_saved_to(self.folder)

    def get_folder(self):
        return self.folder

    def get_session_start(self):
        return self.session_start

    # =====================================================
    # PORT HANDLING
    # =====================================================

    def populate_ports(self):

        current = self.port_combo.currentText() if self.port_combo.count() else None

        self.port_combo.blockSignals(True)
        self.port_combo.clear()

        ports = [p.device for p in serial.tools.list_ports.comports()]

        configured_port = self.cfg.get("mcu", "port", fallback="")
        if configured_port and configured_port not in ports:
            ports.append(configured_port)  # show it even if not currently plugged in

        self.port_combo.addItems(ports)

        to_select = current or configured_port
        if to_select in ports:
            self.port_combo.setCurrentText(to_select)

        self.port_combo.blockSignals(False)

    def on_port_selected(self, port_name):

        if not port_name:
            return

        self.cfg.set("mcu", "port", port_name)
        save_config(self.cfg)

        if hasattr(self.mcu_thread, "request_port_change"):
            self.mcu_thread.request_port_change(port_name)
        else:
            QMessageBox.warning(
                self,
                "Требуется перезапуск",
                f"COM порт сохранён: {port_name}\n"
                f"Живое переподключение недоступно в этой версии — "
                f"перезапустите приложение."
            )

    def open_data_folder(self):

        # Always resolve base data directory via existing helper
        folder_base = data_dir()
        path = str(folder_base)

        # Windows only
        if os.name == "nt":
            subprocess.Popen(["explorer", path])

    # =====================================================
    # CONTROL
    # =====================================================

    def start_recording(self):

        self.start_event.clear()
        self.reset_buffers()

        self.recording = True

        # IMPORTANT:
        # real samples define session zero
        self.session_start = time.perf_counter()
        self.emg_start_time = None

        # Update visible date at recording start
        self.date_label.setText(datetime.now().strftime("%d/%m/%Y %H:%M:%S"))

        self.create_session()

        # reset synchronization / baselines
        if hasattr(self.mcu_thread, "reset_sync"):
            self.mcu_thread.reset_sync()

        for worker in self.lsl_workers:
            worker.reset_sync()

        # flush stale serial data
        if self.mcu_thread.ser is not None:

            try:
                self.mcu_thread.ser.reset_input_buffer()
                self.mcu_thread.ser.reset_output_buffer()

            except Exception as e:
                print(f"[MCU FLUSH ERROR] {e}")

        self.start_event.set()

        subject_label = f"{self.name_edit.text()} / {self.session_edit.text()}"
        self.set_recording_ui_state(True, subject_label)

        self.timer.start(50)

        print("[START]")

    def stop_recording(self):

        self.recording = False

        self.start_event.clear()

        # give LSL thread time to flush / stop pulling after recording
        QTimer.singleShot(300, self._finish_stop)

    def is_recording(self):
        return self.recording

    # =====================================================
    # RESET
    # =====================================================

    def reset_buffers(self):
        self.t_emg, self.v_emg = [], []
        self.t_mcu, self.angle, self.load = [], [], []

        for q in (self.data_queue, self.raw_data_queue,
                  self.events_queue, self.raw_events_queue,
                  self.mcu_queue):
            while not q.empty():
                try:
                    q.get_nowait()
                except Exception:
                    break

        while not self.mcu_queue.empty():
            try:
                self.mcu_queue.get_nowait()
            except Exception:
                break

    def _finish_stop(self):
        self.timer.stop()
        self.set_recording_ui_state(False)
        print("[STOP]")
        print("Saved in:", self.folder)

    # =====================================================
    # DATA HANDLERS
    # =====================================================

    def on_emg(self, t_rel, v):

        # -------------------------------------------------
        # IMPORTANT:
        # This function is now PLOT ONLY.
        #
        # Full-rate EMG saving is done directly inside
        # LSLThread -> self.emg_queue.
        # -------------------------------------------------

        self.t_emg.append(t_rel)
        self.v_emg.append(v)

        self.trim_many(self.t_emg, self.v_emg, max_len=5000)

    def on_mcu(self, pc_time, mcu_time_us, angle_raw, a, load_raw, load_norm):

        if self.session_start is None:
            return

        t_rel = pc_time - self.session_start

        self.mcu_queue.put(
            (
                pc_time,
                mcu_time_us,
                angle_raw,
                a,
                load_raw,
                load_norm
            )
        )

        self.t_mcu.append(t_rel)
        self.angle.append(a)
        self.load.append(load_norm)

        self.trim_many(self.t_mcu, self.angle, self.load, max_len=5000)

    def trim_many(self, *lists, max_len=5000):

        if not lists:
            return

        # Use the longest list length, because one list may already be longer
        # from previous bad trimming.
        current_len = max(len(x) for x in lists)

        if current_len <= max_len:
            return

        for x in lists:
            if len(x) > max_len:
                del x[:-max_len]

    def open_plotter(self):

        folder_base = data_dir()

        if not folder_base.exists():
            QMessageBox.warning(
                self,
                "Папка не найдена",
                f"Папка с данными не найдена:\n{folder_base}"
            )
            return

        selected_folder = QFileDialog.getExistingDirectory(
            self, "Выберите папку сеанса",
            str(folder_base),
            QFileDialog.ShowDirsOnly
        )

        if not selected_folder:
            return

        try:
            plot_session_folder(selected_folder)

        except Exception as e:
            QMessageBox.critical(
                self,
                "Ошибка построения графика",
                str(e)
            )

    # =====================================================
    # PLOT UPDATE
    # =====================================================

    def update_plot(self):

        if not self.recording:
            return

        # ---------------- EMG ----------------
        n_emg = min(len(self.t_emg), len(self.v_emg))

        if n_emg > 1:

            t_emg = self.t_emg[-n_emg:]
            v_emg = self.v_emg[-n_emg:]

            try:
                self.emg_curve.setData(t_emg, v_emg)

                t_max = t_emg[-1]
                t_min = t_emg[0]
                left = max(t_min, t_max - self.window_size)

                if t_max > left:
                    self.emg_plot.setXRange(left, t_max, padding=0)

            except Exception as e:
                print(f"[PLOT EMG ERROR] {e}")

        # ---------------- MCU ----------------
        n_mcu = min(len(self.t_mcu), len(self.angle), len(self.load))

        if n_mcu > 1:

            t_mcu = self.t_mcu[-n_mcu:]
            angle = self.angle[-n_mcu:]
            load = self.load[-n_mcu:]

            try:
                self.angle_curve.setData(t_mcu, angle)
                self.load_curve.setData(t_mcu, load)

                t_max = t_mcu[-1]
                t_min = t_mcu[0]
                left = max(t_min, t_max - self.window_size)

                if t_max > left:
                    self.angle_plot.setXRange(left, t_max, padding=0)
                    self.load_plot.setXRange(left, t_max, padding=0)

            except Exception as e:
                print(f"[PLOT MCU ERROR] {e}")

    # =====================================================
    # STATUS PANEL UPDATE (row counts + connection status)
    # =====================================================

    def update_status_panel(self):
        row_counts = self.storage_thread.get_row_counts()
        self.update_stream_status(row_counts)

        mcu_connected = self.mcu_thread.is_connected()

        lsl_connected_count = sum(1 for w in self.lsl_workers if w.is_connected())
        lsl_total = len(self.lsl_workers)

        self.update_device_status(mcu_connected, lsl_connected_count, lsl_total)

    # =====================================================
    # CLEAN EXIT
    # =====================================================

    def closeEvent(self, event):

        self.recording = False
        self.start_event.clear()

        try:
            self.timer.stop()
            self.status_timer.stop()
        except Exception:
            pass

        for worker in self.lsl_workers:
            worker.stop()

        self.mcu_thread.stop()
        self.storage_thread.stop()
        self.udp_thread.stop()

        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainApp()
    window.show()
    sys.exit(app.exec_())