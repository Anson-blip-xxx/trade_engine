"""Isolated loopback WSGI host for the TradingView V2 signal ingress.

The process has no exchange credentials or trading ports. Nginx terminates TLS
and limits request size before forwarding to this loopback-only service.
"""

import os
import stat
from pathlib import Path
from socketserver import ThreadingMixIn
from threading import BoundedSemaphore
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from services.v2_signal_ingress import create_application


class _BoundedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    _slots = BoundedSemaphore(8)
    block_on_close = True

    request_queue_size = 16

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)
        return connection, address


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, _format, *_args):
        # Never log a URL, request body, or headers containing caller material.
        pass


def load_config(environ):
    directory = environ.get("CREDENTIALS_DIRECTORY", "")
    if not directory or not Path(directory).is_absolute():
        raise ValueError("systemd credential directory required")
    credential = Path(directory) / "tradingview-webhook-secret"
    details = credential.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_IMODE(details.st_mode) not in {0o400, 0o440, 0o600}
        or not 32 <= details.st_size <= 513
    ):
        raise ValueError("protected webhook credential required")
    secret = credential.read_text(encoding="ascii").rstrip("\n")
    if "\n" in secret or not 32 <= len(secret.encode("ascii")) <= 512:
        raise ValueError("invalid webhook credential")
    config = dict(environ)
    config["V2_TV_WEBHOOK_SECRET"] = secret
    return config


def main():
    app = create_application(environ=load_config(os.environ))
    with make_server(
        "127.0.0.1",
        18081,
        app,
        server_class=_BoundedServer,
        handler_class=_QuietHandler,
    ) as server:
        server.serve_forever(poll_interval=0.25)


if __name__ == "__main__":
    main()
