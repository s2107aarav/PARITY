"""Harness-side server lifecycle. Business services run real HTTP over loopback."""

import socket
import threading
import time

import uvicorn


class Server:
    def __init__(self, app):
        self.app = app
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=self.port,
                log_level="error",
                access_log=False,
                timeout_keep_alive=1,
                ws="none",
            )
        )
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True
        )
        self.thread.start()
        deadline = time.monotonic() + 8
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                self.close()
                raise RuntimeError("service did not start")
            time.sleep(0.01)

    def close(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("service failed to stop")
        self.socket.close()
        if hasattr(self.app.state, "store"):
            self.app.state.store.close()
