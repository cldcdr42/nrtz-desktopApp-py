from PyQt5.QtCore import QThread
from queue import Queue, Empty
import threading
import csv


class StorageThread(QThread):
    """
    Fully generic multi-stream CSV storage worker.

    StorageThread itself has NO knowledge of what data sources exist —
    it only knows about whatever has been registered via register_stream()
    or auto-registered via push(). Adding a new data source in the future
    never requires editing this file.

    Two ways to feed data in:

    1) register_stream(key, queue, filename, header=...)
       Explicit setup, done once (usually at app startup). Best for
       high-rate producers (LSL, MCU) that already own a Queue and can
       call queue.put(row) directly without any lock overhead per sample.

    2) push(key, row, filename=None, header=None)
       Zero-config path. First call for a given `key` auto-creates the
       stream (file "{key}.csv" unless `filename` given, header either
       `header` or auto-generated as col_0..col_N-1 from len(row)).
       Best for low-rate / ad-hoc / future sources — a tiny lock is
       taken per call, negligible unless called at very high rates.

    Every stream, however it was added, gets its own separate CSV file
    in the session folder — no merging/alignment is attempted here.
    """

    def __init__(self, folder_getter, recording_flag):
        super().__init__()

        self.folder_getter = folder_getter
        self.recording_flag = recording_flag

        self.running = True
        self.active = False

        self._lock = threading.Lock()
        self.streams = {}          # key -> {queue, filename, header, max_rows_per_cycle}
        self._header_written = {}  # key -> bool

        self._files = {}
        self._writers = {}

        self.cycles_since_flush = 0
        self.flush_every_cycles = 20

    # =====================================================
    # PUBLIC API — the only things other code needs to call
    # =====================================================

    def register_stream(self, key, queue, filename, header=None, max_rows_per_cycle=500):
        """
        Explicitly register a data source. Safe to call any time,
        including after the thread has started, and even mid-recording
        (the stream's file will be created on the next write cycle).

        `header`: fixed list of column names, OR a zero-arg callable
        that returns the list (useful when the column count isn't known
        until a device connects, e.g. worker.header). If omitted, the
        header is auto-generated from the length of the first row seen.
        """
        with self._lock:
            self.streams[key] = {
                "queue": queue,
                "filename": filename,
                "header": header,
                "max_rows_per_cycle": max_rows_per_cycle,
            }
            self._header_written[key] = False

        print(f"[STORAGE] registered stream '{key}' -> {filename}")

    def push(self, key, row, filename=None, header=None):
        """
        Zero-config entry point. Any thread can call this directly with
        no prior setup. Auto-registers the stream on first use.
        """
        with self._lock:
            cfg = self.streams.get(key)

            if cfg is None:
                cfg = {
                    "queue": Queue(),
                    "filename": filename or f"{key}.csv",
                    "header": header,
                    "max_rows_per_cycle": 500,
                }
                self.streams[key] = cfg
                self._header_written[key] = False
                print(f"[STORAGE] auto-registered stream '{key}' -> {cfg['filename']}")

            q = cfg["queue"]

        q.put(row)

    def unregister_stream(self, key):
        """
        Stop writing a stream. Only safe to call between recordings
        (not mid-recording) — it does not close an already-open file.
        """
        with self._lock:
            self.streams.pop(key, None)
            self._header_written.pop(key, None)

    # =====================================================
    # MAIN LOOP
    # =====================================================

    def run(self):

        while self.running:

            if self.recording_flag():

                if not self.active:
                    self.open_all_files()
                    self.active = True
                else:
                    # Pick up any streams registered/pushed after
                    # recording already started.
                    self.open_new_files()

                self.flush_some()

            else:

                if self.active:
                    self.flush_all()
                    self.close_files()
                    self.active = False

            self.msleep(10)

    # =====================================================
    # FILE HANDLING
    # =====================================================

    def _snapshot_streams(self):
        with self._lock:
            return list(self.streams.items())

    def _open_file_for(self, key, cfg, folder):

        f = open(folder / cfg["filename"], "w", newline="", encoding="utf-8")
        writer = csv.writer(f)

        header = cfg["header"]
        if callable(header):
            header = header()

        if header is not None:
            writer.writerow(header)
            f.flush()
            self._header_written[key] = True
        else:
            self._header_written[key] = False  # written lazily from first row

        self._files[key] = f
        self._writers[key] = writer

    def open_all_files(self):

        folder = self.folder_getter()

        if folder is None:
            return

        folder.mkdir(parents=True, exist_ok=True)

        for key, cfg in self._snapshot_streams():
            self._open_file_for(key, cfg, folder)

        self.cycles_since_flush = 0

        print("[STORAGE] files opened:",
              [cfg["filename"] for _, cfg in self._snapshot_streams()])

    def open_new_files(self):
        """Open files for any stream registered/pushed after recording started."""

        folder = self.folder_getter()

        if folder is None:
            return

        for key, cfg in self._snapshot_streams():
            if key not in self._files:
                self._open_file_for(key, cfg, folder)
                print(f"[STORAGE] late-opened stream '{key}' -> {cfg['filename']}")

    def flush_some(self):

        for key, cfg in self._snapshot_streams():

            writer = self._writers.get(key)

            if writer is None:
                continue

            q = cfg["queue"]
            limit = cfg.get("max_rows_per_cycle", 500)
            written = 0

            while written < limit:
                try:
                    row = q.get_nowait()
                except Empty:
                    break
                except Exception as e:
                    print(f"[STORAGE {key} ERROR] {e}")
                    break

                if not self._header_written.get(key, True):
                    generic_header = [f"col_{i}" for i in range(len(row))]
                    writer.writerow(generic_header)
                    self._header_written[key] = True

                writer.writerow(row)
                written += 1

        self.cycles_since_flush += 1

        if self.cycles_since_flush >= self.flush_every_cycles:
            self.flush_files()
            self.cycles_since_flush = 0

    def flush_all(self):

        while True:

            any_written = False

            for key, cfg in self._snapshot_streams():

                writer = self._writers.get(key)

                if writer is None:
                    continue

                q = cfg["queue"]

                try:
                    while True:
                        row = q.get_nowait()

                        if not self._header_written.get(key, True):
                            generic_header = [f"col_{i}" for i in range(len(row))]
                            writer.writerow(generic_header)
                            self._header_written[key] = True

                        writer.writerow(row)
                        any_written = True
                except Empty:
                    pass
                except Exception as e:
                    print(f"[STORAGE {key} FLUSH_ALL ERROR] {e}")

            if not any_written:
                break

        self.flush_files()

    def flush_files(self):

        for key, f in self._files.items():
            try:
                f.flush()
            except Exception as e:
                print(f"[STORAGE FLUSH ERROR:{key}] {e}")

    def close_files(self):

        self.flush_files()

        for key, f in self._files.items():
            try:
                f.close()
            except Exception as e:
                print(f"[STORAGE CLOSE ERROR:{key}] {e}")

        self._files = {}
        self._writers = {}

        print("[STORAGE] files closed")

    def stop(self):

        self.running = False

        if self.active:
            self.flush_all()
            self.close_files()
            self.active = False

        self.quit()
        self.wait(1000)