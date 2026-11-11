"""Harness-side server lifecycle. Business services run real HTTP over loopback."""

import socket
import threading
import time

import uvicorn


class Server:
    def __init__(self, app, *, socket_path=None):
        self.app = app
        self.socket = socket.socket(socket.AF_UNIX if socket_path else socket.AF_INET)
        self.socket.bind(str(socket_path) if socket_path else ("127.0.0.1", 0))
        self.port = None if socket_path else self.socket.getsockname()[1]
        self.url = "http://localhost" if socket_path else f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=self.port or 0,
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
