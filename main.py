import sys
import threading
import os
import subprocess
from queue import Queue
from datetime import datetime
from utils import resource_path
import time

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QPushButton, QLineEdit,
    QTextEdit, QLabel, QMessageBox,
    QFileDialog, QHBoxLayout
)
from PyQt5.QtCore import QTimer
import pyqtgraph as pg

from pylsl import local_clock

from lsl_thread import LSLThread
from mcu_thread_debug import MCUThread
from storage_thread import StorageThread
from udp_sender_thread import UDPSenderThread
from plotter import plot_session_folder


class MainApp(QMainWindow):

    def __init__(self):
        super().__init__()

        # -----------------------
        # STATE
        # -----------------------
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

        # For widget with info on the right
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

        # -----------------------
        # TIME BASE
        # -----------------------
        self.session_start = None

        # Kept for compatibility
        self.emg_start_time = None

        # -----------------------
        # QUEUES
        # -----------------------
        self.emg_queue = Queue()
        self.mcu_queue = Queue()

        # -----------------------
        # BUFFERS
        # -----------------------
        # EMG plot buffers:
        #   self.t_emg = display-only sample-index time
        #   self.v_emg = EMG values
        #
        # MCU plot buffers:
        #   real relative timestamps
        # -----------------------
        self.t_emg, self.v_emg = [], []
        self.t_mcu, self.angle, self.load = [], [], []

        # -----------------------
        # THREADS
        # -----------------------
        self.lsl_thread = LSLThread(
            self.start_event,
            self.emg_queue,
            self.get_session_start,
            plot_hz=50.0,
            )
        self.mcu_thread = MCUThread("COM6", 115200, self.start_event)
        self.udp_thread = UDPSenderThread(self.start_event)

        self.mcu_thread.raw.connect(self.udp_thread.on_data)

        self.storage_thread = StorageThread(
            self.emg_queue,
            self.mcu_queue,
            self.get_folder,
            self.is_recording,
            self.start_event
        )

        self.lsl_thread.data.connect(self.on_emg)
        self.mcu_thread.data.connect(self.on_mcu)

        # UI
        self.init_ui()
        self.init_plot()

        # start threads
        self.lsl_thread.start()
        self.mcu_thread.start()
        self.storage_thread.start()
        self.udp_thread.start()

        # plot timer
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_plot)

    # =====================================================
    # SESSION
    # =====================================================

    def create_session(self):

        # Use a writable folder for CSVs
        data_dir = resource_path("data", writable=True)

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]

        self.folder = data_dir / ts
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

    def get_folder(self):
        return self.folder
    
    def get_session_start(self):
        return self.session_start

    # =====================================================
    # UI
    # =====================================================

    def init_ui(self):

        self.setWindowTitle("Acquisition System")

        # =====================================================
        # BUTTONS
        # =====================================================

        start_btn = QPushButton("START")
        stop_btn = QPushButton("STOP")
        open_folder_btn = QPushButton("Открыть расположение сохраняемых файлов")
        plot_session_btn = QPushButton("Построить график сохранённого сеанса")

        start_btn.clicked.connect(self.start_recording)
        stop_btn.clicked.connect(self.stop_recording)
        open_folder_btn.clicked.connect(self.open_data_folder)
        plot_session_btn.clicked.connect(self.open_plotter)

        # =====================================================
        # LEFT SIDE (plots + buttons)
        # =====================================================

        left_layout = QVBoxLayout()

        left_layout.addWidget(start_btn)
        left_layout.addWidget(stop_btn)
        left_layout.addWidget(open_folder_btn)
        left_layout.addWidget(plot_session_btn)

        # plot widget gets inserted later in init_plot()
        self.left_layout = left_layout

        # =====================================================
        # RIGHT SIDE (session info)
        # =====================================================

        right_layout = QVBoxLayout()

        right_layout.addWidget(QLabel("Имя"))
        right_layout.addWidget(self.name_edit)

        right_layout.addWidget(QLabel("Номер сеанса"))
        right_layout.addWidget(self.session_edit)

        right_layout.addWidget(QLabel("Время проведения сеанса"))
        right_layout.addWidget(self.date_label)

        right_layout.addWidget(QLabel("Примечания"))
        right_layout.addWidget(self.comment_edit, stretch=1)

        right_layout.addStretch()

        right_widget = QWidget()
        right_widget.setLayout(right_layout)
        right_widget.setMaximumWidth(300)

        # =====================================================
        # MAIN LAYOUT
        # =====================================================

        # left side = plots/buttons
        left_widget = QWidget()
        left_widget.setLayout(left_layout)

        main_layout = QHBoxLayout()
        main_layout.addWidget(left_widget, stretch=4)
        main_layout.addWidget(right_widget, stretch=1)

        container = QWidget()
        container.setLayout(main_layout)

        self.setCentralWidget(container)

    def open_data_folder(self):

        # Always resolve base data directory via existing helper
        data_dir = resource_path("data", writable=True)

        path = str(data_dir)

        # Windows only
        if os.name == "nt":
            subprocess.Popen(["explorer", path])

    # =====================================================
    # PLOT SETUP
    # =====================================================

    def init_plot(self):

        pg.setConfigOption("background", "w")
        pg.setConfigOption("foreground", "k")

        self.plot = pg.GraphicsLayoutWidget()
        self.left_layout.addWidget(self.plot)

        # EMG
        self.emg_plot = self.plot.addPlot(title="EMG")
        self.emg_curve = self.emg_plot.plot(pen="k")

        # ANGLE
        self.plot.nextRow()
        self.angle_plot = self.plot.addPlot(title="Angle")
        self.angle_curve = self.angle_plot.plot(pen="b")

        # LOAD
        self.plot.nextRow()
        self.load_plot = self.plot.addPlot(title="Load")
        self.load_curve = self.load_plot.plot(pen="r")

        # disable interaction
        for p in [self.emg_plot, self.angle_plot, self.load_plot]:
            p.setMouseEnabled(x=False, y=False)
            p.hideButtons()
            p.setMenuEnabled(False)

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

        if hasattr(self.lsl_thread, "reset_sync"):
            self.lsl_thread.reset_sync()

        # flush stale serial data
        if self.mcu_thread.ser is not None:

            try:
                self.mcu_thread.ser.reset_input_buffer()
                self.mcu_thread.ser.reset_output_buffer()

            except Exception as e:
                print(f"[MCU FLUSH ERROR] {e}")

        self.start_event.set()

        self.timer.start(30)

        print("[START]")

    def stop_recording(self):

        self.recording = False

        self.start_event.clear()

        # give LSL thread time to flush / stop pulling after recording
        time.sleep(0.3)

        self.timer.stop()

        print("[STOP]")
        print("Saved in:", self.folder)

    def is_recording(self):
        return self.recording

    # =====================================================
    # RESET
    # =====================================================

    def reset_buffers(self):

        # Reset EMG display-time axis
        self.emg_plot_sample_index = 0

        self.t_emg.clear()
        self.v_emg.clear()

        self.t_mcu.clear()
        self.angle.clear()
        self.load.clear()

        while not self.emg_queue.empty():
            try:
                self.emg_queue.get_nowait()
            except:
                break

        while not self.mcu_queue.empty():
            try:
                self.mcu_queue.get_nowait()
            except:
                break

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
    
    """
    def on_emg(self, t, v):

        if self.session_start is None:
            self.session_start = t
            print(f"[SESSION START] EMG @ {t:.6f}")

        # -------------------------------------------------
        # Real synchronized relative timestamp for CSV
        # -------------------------------------------------
        t_rel = t - self.session_start

        # CSV gets real timing
        self.emg_queue.put((t_rel, v))

        # -------------------------------------------------
        # Live plot only:
        # Use evenly spaced display time instead of real receive timestamps.
        # -------------------------------------------------
        plot_t = self.emg_plot_sample_index / self.emg_plot_fs
        self.emg_plot_sample_index += 1

        self.t_emg.append(plot_t)
        self.v_emg.append(v)

        self.trim_many(self.t_emg, self.v_emg, max_len=5000)
    """
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

        #print(load_norm)
        #print(load_norm, "norm")

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

    """
    def open_plotter(self):
        QMessageBox.information(
            self,
            "Disabled in debug build",
            "Saved-session plotting is disabled in this debug build."
        )
    """
    def open_plotter(self):

        data_dir = resource_path("data", writable=True)

        if not data_dir.exists():
            QMessageBox.warning(
                self,
                "Папка не найдена",
                f"Папка с данными не найдена:\n{data_dir}"
            )
            return

        selected_folder = QFileDialog.getExistingDirectory(
            self,
            "Выберите папку сеанса",
            str(data_dir),
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
    # CLEAN EXIT
    # =====================================================

    def closeEvent(self, event):

        self.recording = False
        self.start_event.clear()

        try:
            self.timer.stop()
        except:
            pass

        self.lsl_thread.stop()
        self.mcu_thread.stop()
        self.storage_thread.stop()
        self.udp_thread.stop()

        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainApp()
    window.show()
    sys.exit(app.exec_())