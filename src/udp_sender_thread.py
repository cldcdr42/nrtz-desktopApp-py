from PyQt5.QtCore import QThread, pyqtSlot
import json
import socket


class UDPSenderThread(QThread):

    def __init__(self, start_event, udp_host="127.0.0.1", udp_port=4444):
        super().__init__()
        self.start_event = start_event   # NEW

        self.udp_host = udp_host
        self.udp_port = udp_port

        self.running = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def run(self):
        while self.running:
            self.msleep(50)

    @pyqtSlot(str)
    def on_data(self, line):

        if not self.start_event.is_set():
            return

        try:
            data = json.loads(line)
        except:
            return

        if data.get("type") != "input":
            return

        try:
            self.sock.sendto(
                json.dumps(data).encode("utf-8"),
                (self.udp_host, self.udp_port)
            )
        except:
            pass

    def stop(self):
        self.running = False
        self.quit()
        self.wait()