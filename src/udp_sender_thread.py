"""
udp_sender_thread.py

Forwards MCU sensor data to an external UDP listener while recording
is active. Used to feed the live JSON stream out to another app/process
without touching how the MCU data itself gets saved to CSV.
"""

from PyQt5.QtCore import QThread, pyqtSlot
import json
import socket


class UDPSenderThread(QThread):
    """
    Forwards MCU JSON lines out over UDP while a recording is active.

    Sits between MCUThread (which emits every raw serial line via its
    `raw` signal) and whatever external tool is listening on the given
    host/port. Only forwards while start_event is set, and only for
    lines that parse as JSON with type "input" — anything else (bad
    JSON, other message types) is silently dropped.
    """

    def __init__(self, start_event, udp_host="127.0.0.1", udp_port=4444):
        super().__init__()

        self.start_event = start_event

        self.udp_host = udp_host
        self.udp_port = udp_port

        self.running = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # =====================================================
    # MAIN LOOP
    # =====================================================

    def run(self):
        # Nothing to do here on a timer — all the real work happens in
        # on_data(), which is called from the MCU thread whenever a new
        # line comes in. This loop just keeps the QThread alive.
        while self.running:
            self.msleep(50)

    # =====================================================
    # DATA HANDLER
    # =====================================================

    @pyqtSlot(str)
    def on_data(self, line):

        if not self.start_event.is_set():
            return

        try:
            data = json.loads(line)
        except Exception:
            return

        if data.get("type") != "input":
            return

        try:
            self.sock.sendto(
                json.dumps(data).encode("utf-8"),
                (self.udp_host, self.udp_port)
            )
        except Exception:
            # Best-effort forwarding only — a dropped UDP packet (no
            # listener bound, network hiccup, etc.) should never affect
            # recording, so failures here are swallowed.
            pass

    # =====================================================
    # STOP
    # =====================================================

    def stop(self):
        self.running = False
        self.quit()
        self.wait()