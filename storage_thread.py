from PyQt5.QtCore import QThread
import csv
from queue import Queue, Empty


class StorageThread(QThread):

    def __init__(self, emg_q: Queue, mcu_q: Queue, folder_getter, recording_flag, start_event):
        super().__init__()

        self.emg_q = emg_q
        self.mcu_q = mcu_q

        self.folder_getter = folder_getter
        self.recording_flag = recording_flag
        self.start_event = start_event

        self.running = True
        self.active = False

        self.emg_file = None
        self.mcu_file = None
        self.emg_writer = None
        self.mcu_writer = None

        # Per cycle limits.
        # Prevent EMG from starving MCU writes.
        self.max_emg_rows_per_cycle = 500
        self.max_mcu_rows_per_cycle = 100

        self.cycles_since_flush = 0
        self.flush_every_cycles = 20

    def run(self):

        while self.running:

            if self.recording_flag():

                if not self.active:
                    self.open_files()
                    self.active = True

                self.flush_some()

            else:

                if self.active:
                    self.flush_all()
                    self.close_files()
                    self.active = False

            self.msleep(10)

    def open_files(self):

        folder = self.folder_getter()

        if folder is None:
            return

        folder.mkdir(parents=True, exist_ok=True)

        self.emg_file = open(folder / "emg.csv", "w", newline="", encoding="utf-8")
        self.mcu_file = open(folder / "mcu.csv", "w", newline="", encoding="utf-8")

        self.emg_writer = csv.writer(self.emg_file)
        self.mcu_writer = csv.writer(self.mcu_file)

        self.emg_writer.writerow([
            "lsl_timestamp_s",
            "pc_timestamp_s",
            "relative_time_s",
            "emg"
        ])
        self.mcu_writer.writerow([
            "pc_timestamp_s",
            "mcu_timestamp_us",
            "angle_raw",
            "angle_deg",
            "load_raw",
            "load_norm"
        ])
        self.emg_file.flush()
        self.mcu_file.flush()

        self.cycles_since_flush = 0

        print("[STORAGE] files opened")

    def flush_some(self):

        if self.emg_writer is None or self.mcu_writer is None:
            return

        # Drain limited EMG rows
        emg_written = 0

        while emg_written < self.max_emg_rows_per_cycle:
            try:
                lsl_ts, pc_ts, t_rel, v = self.emg_q.get_nowait()

                self.emg_writer.writerow([
                    lsl_ts,
                    pc_ts,
                    t_rel,
                    v
                ])
                emg_written += 1
            except Empty:
                break
            except Exception as e:
                print(f"[STORAGE EMG ERROR] {e}")
                break

        # Drain limited MCU rows
        mcu_written = 0

        while mcu_written < self.max_mcu_rows_per_cycle:
            try:
                pc_t, mcu_t, angle_raw, angle, load_raw, load_norm = self.mcu_q.get_nowait()
                self.mcu_writer.writerow([
                    pc_t,
                    mcu_t,
                    angle_raw,
                    angle,
                    load_raw,
                    load_norm
                ])
                mcu_written += 1
            except Empty:
                break
            except Exception as e:
                print(f"[STORAGE MCU ERROR] {e}")
                break

        self.cycles_since_flush += 1

        if self.cycles_since_flush >= self.flush_every_cycles:
            self.flush_files()
            self.cycles_since_flush = 0

    def flush_all(self):

        if self.emg_writer is None or self.mcu_writer is None:
            return

        while True:
            any_written = False

            try:
                while True:
                    lsl_ts, pc_ts, t_rel, v = self.emg_q.get_nowait()

                    self.emg_writer.writerow([
                        lsl_ts,
                        pc_ts,
                        t_rel,
                        v
                    ])
                    any_written = True
            except Empty:
                pass
            except Exception as e:
                print(f"[STORAGE EMG FLUSH_ALL ERROR] {e}")

            try:
                while True:
                    pc_t, mcu_t, angle_raw, angle, load_raw, load_norm = self.mcu_q.get_nowait()

                    self.mcu_writer.writerow([
                        pc_t,
                        mcu_t,
                        angle_raw,
                        angle,
                        load_raw,
                        load_norm
                    ])
                    any_written = True
            except Empty:
                pass
            except Exception as e:
                print(f"[STORAGE MCU FLUSH_ALL ERROR] {e}")

            if not any_written:
                break

        self.flush_files()

    def flush_files(self):

        try:
            if self.emg_file:
                self.emg_file.flush()

            if self.mcu_file:
                self.mcu_file.flush()

        except Exception as e:
            print(f"[STORAGE FLUSH ERROR] {e}")

    def close_files(self):

        try:
            self.flush_files()

            if self.emg_file:
                self.emg_file.close()

            if self.mcu_file:
                self.mcu_file.close()

            print("[STORAGE] files closed")

        except Exception as e:
            print(f"[STORAGE CLOSE ERROR] {e}")

        self.emg_file = None
        self.mcu_file = None
        self.emg_writer = None
        self.mcu_writer = None

    def stop(self):

        self.running = False

        if self.active:
            self.flush_all()
            self.close_files()
            self.active = False

        self.quit()
        self.wait(1000)