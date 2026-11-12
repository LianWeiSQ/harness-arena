"""Cancel an in-flight urllib request when its owning task stops.

Only TCP/TLS establishment has a network timeout. Model generation has no
independent wall-clock/idle limit; the task controller owns its lifetime.
"""
from __future__ import annotations

import http.client
import socket
import threading
import urllib.request


class RequestControl:
    def __init__(self, stop_reason, connect_timeout=30):
        self.stop_reason = stop_reason
        self.connect_timeout = connect_timeout
        self.reason = None
        self.phase = "connecting"
        self.done = threading.Event()
        self.connections = []
        self.sockets = []
        self.lock = threading.Lock()

    def __enter__(self):
        self.worker = threading.Thread(target=self._watch, name="model-request-control", daemon=True)
        self.worker.start()
        return self

    def __exit__(self, *_):
        self.done.set()
        self.worker.join()
        # HTTPResponse owns the stream's final close; do not close a file from
        # this thread while readline holds its lock. shutdown wakes the reader.

    def _watch(self):
        while not self.done.wait(.1):
            self.reason = self.reason or self.stop_reason()
            if self.reason:
                with self.lock:
                    sockets = [*self.sockets, *(c.sock for c in self.connections if c.sock)]
                for sock in sockets:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

    def _connection(self, base):
        control = self

        class Connection(base):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                with control.lock:
                    control.connections.append(self)

            def connect(self):
                control.reason = control.reason or control.stop_reason()
                if control.reason:
                    raise OSError("Owning task stopped")
                self.timeout = control.connect_timeout
                super().connect()
                with control.lock:
                    control.sockets.append(self.sock)
                if control.reason:
                    self.close()
                    raise OSError("Owning task stopped")
                self.sock.settimeout(None)
                self.timeout = None
                control.phase = "response"

        return Connection

    def opener(self, *handlers):
        http_connection = self._connection(http.client.HTTPConnection)
        https_connection = self._connection(http.client.HTTPSConnection)

        class HTTPHandler(urllib.request.HTTPHandler):
            def http_open(self, req):
                return self.do_open(http_connection, req)

        class HTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(https_connection, req, context=self._context)

        return urllib.request.build_opener(*handlers, HTTPHandler(), HTTPSHandler())
