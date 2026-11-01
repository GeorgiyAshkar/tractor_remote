#!/usr/bin/env python3
"""Serve the local tractor UI and its localhost-only control API."""

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import urlsplit

try:
    from .controller import Controller, ControlError
except ImportError:
    from controller import Controller, ControlError


STATIC = Path(__file__).resolve().parent / "static"


class Handler(SimpleHTTPRequestHandler):
    server_version = "TractorLocal/1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, format, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(2)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        super().end_headers()

    def reply(self, status, body):
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def trusted(self, mutation=False):
        port = self.server.server_address[1]
        allowed = {"127.0.0.1:" + str(port), "localhost:" + str(port)}
        if self.headers.get("Host", "") not in allowed:
            self.reply(403, {"error": "Разрешён только локальный адрес пульта"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {"http://" + host for host in allowed}:
            self.reply(403, {"error": "Запрос от другого сайта запрещён"})
            return False
        if mutation:
            if origin not in {"http://" + host for host in allowed}:
                self.reply(403, {"error": "Нужен Origin локального пульта"})
                return False
            token = self.headers.get("X-CSRF-Token", "")
            if not re.fullmatch(r"[0-9a-f]{64}", token) or not secrets.compare_digest(token, self.server.controller.csrf_token):
                self.reply(403, {"error": "Некорректный CSRF-токен"})
                return False
        return True

    def do_GET(self):
        if not self.trusted():
            return
        path = urlsplit(self.path).path
        if path == "/api/status":
            self.reply(200, self.server.controller.status())
        elif path.startswith("/api/"):
            self.reply(404, {"error": "Неизвестный API"})
        else:
            # SimpleHTTPRequestHandler normalizes '..'; symlinks are checked
            # here as well so runtime/counter files cannot be served via static.
            resolved = Path(self.translate_path(self.path)).resolve()
            if STATIC.resolve() != resolved and STATIC.resolve() not in resolved.parents:
                self.reply(403, {"error": "Файл недоступен"})
                return
            if resolved.is_dir() and not (resolved / "index.html").is_file():
                self.reply(404, {"error": "Страница не найдена"})
                return
            super().do_GET()

    def do_POST(self):
        if not self.trusted(mutation=True):
            return
        try:
            length_text = self.headers.get("Content-Length", "")
            if self.headers.get("Transfer-Encoding") or not length_text.isdigit() or not 1 <= int(length_text) <= 4096:
                raise ControlError("Неверный размер запроса", 400)
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ControlError("Ожидается application/json", 415)
            data = json.loads(self.rfile.read(int(length_text)))
            if not isinstance(data, dict):
                raise ControlError("Ожидается JSON-объект", 400)
            control = self.server.controller
            path = urlsplit(self.path).path
            if path == "/api/power":
                control.set_power(data.get("confirmed"), data.get("board_token"))
            elif path == "/api/control":
                if "action" not in data:
                    raise ControlError("Не задано действие", 400)
                control.control(data.get("client_id"), data.get("seq"), data["action"], data.get("board_token"))
            elif path == "/api/stop":
                control.stop(data.get("client_id"), data.get("seq"))
            elif path == "/api/demo/board":
                control.set_demo_board(data.get("present"))
            else:
                raise ControlError("Неизвестный API", 404)
            self.reply(200, control.status())
        except ControlError as error:
            self.reply(error.status, {"error": str(error)})
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
            self.reply(400, {"error": "Некорректный JSON-запрос"})

    def do_OPTIONS(self):
        if self.trusted():
            self.reply(405, {"error": "CORS не поддерживается"})

    def do_HEAD(self):
        if self.trusted():
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, controller):
        if address[0] != "127.0.0.1":
            raise ValueError("The controller binds only to 127.0.0.1")
        self.controller = controller
        super().__init__(address, Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=["127.0.0.1"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--demo", action="store_true", help="No USB access or RF; default counters are in memory")
    parser.add_argument("--serial-port", help="Select one detected CH340 port if several exist")
    parser.add_argument("--state-file", type=Path, help="Override the counter ledger; demo uses memory unless supplied")
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be from 1 to 65535")
    try:
        control = Controller(mode="demo" if args.demo else "hardware", state_file=args.state_file,
                             serial_port=args.serial_port)
    except ValueError as error:
        parser.error(str(error))
    server = LocalServer((args.host, args.port), control)
    control.start()
    url = "http://127.0.0.1:{}/".format(server.server_address[1])
    print("Пульт: " + url, flush=True)
    if args.open_browser:
        import webbrowser
        threading.Timer(0.2, partial(webbrowser.open, url)).start()
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        control.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
