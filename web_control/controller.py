"""Local tractor control state, a durable counter ledger, and one serial worker.

Imports and status reads do not open USB. Only the worker owns the serial port.
The board's START/DONE responses describe transmission, not a tractor ACK.
"""

from collections import deque
from datetime import datetime
import fcntl
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import select
import tempfile
import termios
import threading
import time
import tty


ACTIONS = {
    "tower_left": ("TL", "Башня влево"), "tower_right": ("TR", "Башня вправо"),
    "boom_up": ("BU", "Поднять стрелу"), "boom_down": ("BD", "Опустить стрелу"),
    "left_forward": ("LF", "Левая гусеница вперёд"),
    "left_backward": ("LB", "Левая гусеница назад"),
    "right_forward": ("RF", "Правая гусеница вперёд"),
    "right_backward": ("RB", "Правая гусеница назад"),
    "forward": ("FF", "Обе гусеницы вперёд"),
}
LEASE_SECONDS = 0.350
KEEP_SECONDS = 0.025
PROTOCOLS = {"tractorctl-v1", "web-control-v1"}
ROOT = Path(__file__).resolve().parents[1]


class ControlError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class BusyError(OSError):
    pass


class MissingError(OSError):
    pass


class ProtocolError(RuntimeError):
    pass


def fields(line):
    parts = line.split(",")
    values = {}
    for item in parts[1:]:
        if "=" not in item:
            raise ProtocolError("Некорректный ответ платы")
        key, value = item.split("=", 1)
        if not key or key in values:
            raise ProtocolError("Повтор поля в ответе платы")
        values[key] = value
    return parts[0], values


def integer(values, name, maximum):
    value = values.get(name, "")
    if not re.fullmatch(r"[0-9]+", value) or not 0 <= int(value) <= maximum:
        raise ProtocolError("Некорректное поле " + name)
    return int(value)


class CounterStore:
    """Reserve eight counters durably before a chunk may be armed/fired.

    next_counter is an unbounded ordinal; its low byte is transmitted. Wrapping
    the radio's eight-bit counter is normal, replaying an old reservation is not.
    A corrupt ledger is never replaced with the initial value.
    """
    def __init__(self, path, initial_next=0xF6):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_fd = os.open(str(self.path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                self.data = json.loads(self.path.read_text())
                value = self.data.get("next_counter")
                if (self.data.get("schema_version") != 1 or type(value) is not int or value < 0
                        or not isinstance(self.data.get("reservations"), list)):
                    raise ValueError("Повреждён журнал счётчиков")
                previous = None
                for record in self.data["reservations"]:
                    ordinal = record.get("ordinal") if isinstance(record, dict) else None
                    if (type(ordinal) is not int or ordinal < 0 or record.get("size") != 8
                            or (previous is not None and ordinal != previous + 8)
                            or record.get("counter_hex") != format(ordinal & 255, "02x")):
                        raise ValueError("Нарушена последовательность журнала счётчиков")
                    previous = ordinal
                if previous is not None and value != previous + 8:
                    raise ValueError("Следующий счётчик не соответствует журналу")
            else:
                if type(initial_next) is not int or initial_next < 0:
                    raise ValueError("Не задан начальный счётчик")
                self.data = {"schema_version": 1, "next_counter": initial_next, "reservations": []}
        except BlockingIOError:
            self.close()
            raise BusyError("Журнал счётчиков уже используется другим пультом")
        except Exception:
            self.close()
            raise

    def reserve(self, action, token):
        start = self.data["next_counter"]
        data = {"schema_version": 1, "next_counter": start + 8,
                "reservations": (self.data["reservations"] + [{
                    "ordinal": start, "counter_hex": format(start & 255, "02x"),
                    "size": 8, "action": action, "token": token,
                    "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                }])[-256:]}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".state-", delete=False) as output:
                temporary = output.name
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            temporary = None
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self.data = data
            return start & 255
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def close(self):
        if getattr(self, "lock_fd", None) is not None:
            os.close(self.lock_fd)
            self.lock_fd = None


class MemoryCounterStore:
    def __init__(self, initial_next=0xF6):
        self.next_counter = initial_next

    def reserve(self, action, token):
        start = self.next_counter
        self.next_counter += 8
        return start & 255

    def close(self):
        pass


def discover_ports():
    return sorted(set(glob.glob("/dev/cu.usbserial-*") + glob.glob("/dev/cu.wchusbserial*")))


class UsbSerial:
    def __init__(self, port):
        self.fd = None
        self.lock_fd = None
        self.buffer = bytearray()
        digest = hashlib.sha256(os.path.realpath(port).encode()).hexdigest()[:20]
        lock_path = os.path.join(tempfile.gettempdir(), "rfprobe-{}.lock".format(digest))
        try:
            self.lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BusyError("USB-порт уже занят другим приложением")
            self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            if hasattr(termios, "TIOCEXCL"):
                fcntl.ioctl(self.fd, termios.TIOCEXCL)
            tty.setraw(self.fd)
            settings = termios.tcgetattr(self.fd)
            settings[4] = settings[5] = termios.B115200
            settings[2] &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
            settings[2] |= termios.CS8 | termios.CLOCAL | termios.CREAD
            termios.tcsetattr(self.fd, termios.TCSANOW, settings)
        except Exception:
            self.close()
            raise

    def write(self, command):
        data = (command + "\n").encode("ascii")
        if os.write(self.fd, data) != len(data):
            raise OSError("Неполная запись в USB-порт")

    def read_lines(self):
        if select.select([self.fd], [], [], 0)[0]:
            data = os.read(self.fd, 4096)
            if not data:
                raise MissingError("Подключите RF-Nano к USB")
            self.buffer.extend(data)
        if len(self.buffer) > 16384:
            raise ProtocolError("Слишком длинный ответ платы")
        lines = []
        while b"\n" in self.buffer:
            raw, _, rest = self.buffer.partition(b"\n")
            self.buffer[:] = rest
            lines.append(raw.rstrip(b"\r").decode("ascii"))
        return lines

    def close(self):
        for name in ("fd", "lock_fd"):
            value = getattr(self, name, None)
            if value is not None:
                os.close(value)
                setattr(self, name, None)


class DemoSerial:
    """In-memory board; no filesystem device discovery/open operations."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lines = deque()
        self.armed = None
        self.active = None

    def write(self, command):
        parts = command.split()
        if parts[0] == "HELLO":
            self.lines.append("READY,protocol=tractorctl-v1,radio=ready,armed=no,active=no,max_duration_ms=1000,heartbeat_ms=100,max_frames=200")
        elif parts[0] == "PING":
            self.lines.append("PONG")
        elif parts[0] == "ARM":
            _, action, counter, token = parts
            self.armed = (action, counter, token)
            self.lines.append("ARMED,action={},counter={},token={}".format(action, counter, token))
        elif parts[0] == "FIRE":
            action, counter, token = self.armed
            if parts[1] != token:
                raise ProtocolError("Demo token mismatch")
            self.active = (action, token, self.clock())
            self.armed = None
            self.lines.append("START,action={},token={},duration_ms=1000".format(action, token))
        elif parts[0] == "STOP":
            self.finish("stop")
            self.armed = None
            self.lines.append("OK,stopped=1")

    def finish(self, reason):
        if self.active:
            _, token, started = self.active
            elapsed = min(1000, int((self.clock() - started) * 1000))
            self.lines.append("DONE,token={},reason={},frames={},elapsed_ms={},radio=off".format(
                token, reason, min(198, max(0, elapsed // 10 * 2)), elapsed))
            self.active = None

    def read_lines(self):
        if self.active and self.clock() - self.active[2] >= 1:
            self.finish("complete")
        lines = list(self.lines)
        self.lines.clear()
        return lines

    def close(self):
        self.active = None
        self.armed = None


class Controller:
    def __init__(self, mode="hardware", state_file=None, serial_port=None,
                 clock=time.monotonic, port_finder=discover_ports, transport_factory=UsbSerial,
                 store_factory=None, nonce_factory=None, startup_delay=1.8):
        if mode not in ("hardware", "demo"):
            raise ValueError("mode must be hardware or demo")
        if (mode == "demo" and state_file is not None
                and Path(state_file).resolve() == (ROOT / "web_control/runtime/state.json").resolve()):
            raise ValueError("Demo mode requires an independent counter file")
        self.mode, self.clock = mode, clock
        self.state_file, self.serial_port = state_file, serial_port
        self.port_finder, self.transport_factory = port_finder, transport_factory
        self.store_factory, self.nonce_factory = store_factory, nonce_factory or (lambda: secrets.token_hex(4))
        self.startup_delay = 0 if mode == "demo" else startup_delay
        self.csrf_token = secrets.token_hex(32)
        self.lock = threading.RLock()
        self.wake, self.shutdown = threading.Event(), threading.Event()
        self.thread = None
        self.events, self.event_id = deque(maxlen=80), 0
        self.board = {"state": "missing", "message": "Ищем RF-Nano по USB…", "token": secrets.token_hex(16)}
        self.confirmed = False
        self.owner = self.action = None
        self.lease_until, self.revision = 0, 0
        self.sequences = {}
        self.transport = self.store = None
        self.phase = "disconnected"
        self.token = self.chunk_action = None
        self.phase_deadline = self.keep_at = self.ping_due = self.ping_deadline = 0
        self.next_detect = self.presence_due = 0
        self.demo_present = True
        self.memory_store = MemoryCounterStore()
        self._event("info", "Пульт запущен" if mode == "hardware" else "Демонстрационный режим")

    def _event(self, level, message):
        self.event_id += 1
        self.events.append({"id": self.event_id, "level": level, "message": message,
                            "time": datetime.now().astimezone().isoformat(timespec="seconds")})

    def _board(self, state, message, port=None):
        if self.board.get("state") != state or self.board.get("message") != message:
            self._event("error" if state == "error" else "info", message)
        self.board.update(state=state, message=message)
        if port is not None:
            self.board["port"] = port

    def _clear_hold(self):
        self.owner = self.action = None
        self.lease_until = 0
        self.revision += 1

    def _live(self):
        return (self.owner is not None and self.action is not None and self.confirmed
                and self.clock() < self.lease_until and self.board["state"] == "ready")

    def _expire(self):
        if self.owner is not None and self.clock() >= self.lease_until:
            self.board["token"] = secrets.token_hex(16)
            self._clear_hold()
            self._event("info", "Удержание завершено: связь с кнопкой потеряна")

    def _sequence(self, client_id, seq):
        if not isinstance(client_id, str) or not re.fullmatch(r"[0-9a-f]{32}", client_id):
            raise ControlError("Некорректный идентификатор пульта", 400)
        if type(seq) is not int or not 0 <= seq <= 2 ** 53 - 1:
            raise ControlError("Некорректный номер команды", 400)
        if seq <= self.sequences.get(client_id, -1):
            raise ControlError("Устаревшая команда")
        if len(self.sequences) >= 512 and client_id not in self.sequences:
            raise ControlError("Слишком много вкладок пульта", 429)
        self.sequences[client_id] = seq

    def _check_board_token(self, token):
        if (not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token)
                or not secrets.compare_digest(token, self.board["token"])):
            raise ControlError("Подключение изменилось. Обновите состояние пульта.")

    def set_power(self, confirmed, board_token):
        with self.lock:
            self._expire()
            self._check_board_token(board_token)
            if type(confirmed) is not bool:
                raise ControlError("Питание должно быть true или false", 400)
            if confirmed and self.board["state"] != "ready":
                raise ControlError("Сначала подключите готовую плату")
            if not confirmed and (self.confirmed or self.owner is not None):
                self.board["token"] = secrets.token_hex(16)
            self.confirmed = confirmed
            if not confirmed:
                self._clear_hold()
            self._event("info", "Питание трактора подтверждено" if confirmed else "Управление выключено")
            self.wake.set()

    def control(self, client_id, seq, action, board_token):
        with self.lock:
            self._expire()
            self._check_board_token(board_token)
            self._sequence(client_id, seq)
            if action is not None and action not in ACTIONS:
                raise ControlError("Неизвестное действие", 400)
            if self.owner is not None and self.owner != client_id:
                raise ControlError("Управление удерживается другой вкладкой")
            if action is None:
                self._clear_hold()
            else:
                if self.board["state"] != "ready" or not self.confirmed:
                    raise ControlError("Подтвердите подключение и питание трактора")
                if self.owner != client_id or self.action != action:
                    self.revision += 1
                self.owner, self.action = client_id, action
                self.lease_until = self.clock() + LEASE_SECONDS
            self.wake.set()

    def stop(self, client_id, seq):
        with self.lock:
            self._sequence(client_id, seq)
            if self.owner is not None and self.owner != client_id:
                self.board["token"] = secrets.token_hex(16)
            self._clear_hold()
            self._event("info", "Стоп")
            self.wake.set()

    def set_demo_board(self, present):
        if self.mode != "demo" or type(present) is not bool:
            raise ControlError("Доступно только в демонстрации", 400)
        with self.lock:
            self.demo_present = present
            self.next_detect = 0
            self.wake.set()

    def status(self):
        with self.lock:
            self._expire()
            active = self.phase == "active"
            return {"mode": self.mode, "board": dict(self.board),
                    "tractor": {"confirmed": self.confirmed},
                    "control": {"enabled": self.board["state"] == "ready" and self.confirmed,
                                "action": self.action, "owner": self.owner,
                                "state": "active" if active else ("stopping" if self.phase == "stopping" else
                                          "arming" if self._live() else "idle"),
                                "transmitting": active, "feedback": "transmission_only"},
                    "events": list(self.events), "csrf_token": self.csrf_token,
                    "commands": [{"id": name, "label": value[1]} for name, value in ACTIONS.items()]}

    def _close_connection(self, state, message):
        if self.transport is not None:
            try:
                if self.phase in ("arming", "starting", "active", "stopping"):
                    self.transport.write("STOP")
            except Exception:
                pass
            try:
                self.transport.close()
            except Exception:
                pass
        self.transport = None
        if self.store is not None and self.store is not self.memory_store:
            try:
                self.store.close()
            except Exception:
                pass
        self.store = None
        self.confirmed = False
        self._clear_hold()
        self.board["token"] = secrets.token_hex(16)
        self.token = self.chunk_action = None
        self.phase = "disconnected"
        self._board(state, message)
        self.next_detect = self.clock() + (5 if state == "firmware_required" else 1)

    def _make_store(self):
        if self.store_factory:
            return self.store_factory()
        if self.mode == "demo" and self.state_file is None:
            return self.memory_store
        initial = 0xF6
        if self.mode == "hardware":
            config_path = ROOT / "web_control/config.json"
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise ValueError("Не удалось прочитать конфигурацию {}: {}".format(config_path, error)) from error
            if type(config) is not dict or set(config) != {"initial_counter_hex"}:
                raise ValueError("В {} требуется только поле initial_counter_hex".format(config_path))
            text = config["initial_counter_hex"]
            if not isinstance(text, str) or not re.fullmatch(r"[0-9a-f]{2}", text):
                raise ValueError("В {} initial_counter_hex должен содержать две строчные hex-цифры".format(config_path))
            initial = int(text, 16)
        return CounterStore(self.state_file or ROOT / "web_control/runtime/state.json", initial)

    def _connect(self):
        now = self.clock()
        if now < self.next_detect:
            return
        self.next_detect = now + 0.5
        if self.mode == "demo":
            if not self.demo_present:
                self._board("missing", "Демонстрационная плата отключена")
                return
            port = "demo"
            transport = DemoSerial(self.clock)
        else:
            ports = self.port_finder()
            if self.serial_port:
                ports = [self.serial_port] if self.serial_port in ports else []
            if not ports:
                self._board("missing", "Подключите RF-Nano к USB")
                return
            if len(ports) != 1:
                self._board("error", "Найдено несколько USB-плат. Выберите порт при запуске.")
                return
            port = ports[0]
            transport = self.transport_factory(port)
        self.transport = transport
        self.board["token"] = secrets.token_hex(16)
        self.confirmed = False
        self._clear_hold()
        self._board("connecting", "Подключаем RF-Nano…", port)
        self.phase = "handshake"
        self.hello_at, self.hello_sent = now + self.startup_delay, False
        self.phase_deadline = self.hello_at + 1.2
        self.presence_due = now + 0.25

    def _request_stop(self):
        if self.phase in ("arming", "starting", "active"):
            self.stop_needs_done = self.phase in ("starting", "active")
            self.stop_done = self.stop_ok = False
            self.phase = "stopping"
            self.phase_deadline = self.clock() + 0.6
            self.transport.write("STOP")

    def _idle(self):
        self.phase = "idle"
        self.token = self.chunk_action = None
        self.ping_deadline = 0
        self.ping_due = self.clock() + 1

    def _handle(self, line):
        if not line:
            return
        if self.phase == "handshake" and line.startswith("ERR,"):
            self._close_connection("firmware_required", "Нужна прошивка веб-пульта")
            return
        kind, data = fields(line)
        if kind == "PONG":
            self.ping_deadline = 0
            self.ping_due = self.clock() + 1
            return
        if kind == "BOOT":
            if self.phase != "handshake":
                raise ProtocolError("Плата перезапустилась; подтвердите питание заново")
            if data.get("protocol") not in PROTOCOLS:
                self._close_connection("firmware_required", "Нужна прошивка веб-пульта")
            return
        if self.phase == "handshake":
            if kind == "READY":
                if data.get("protocol") not in PROTOCOLS:
                    self._close_connection("firmware_required", "Нужна прошивка веб-пульта")
                    return
                if not self.hello_sent:
                    return
                try:
                    compatible = (data.get("radio") == "ready" and data.get("armed") == "no"
                                  and data.get("active") == "no"
                                  and integer(data, "max_duration_ms", 1000) == 1000
                                  and integer(data, "heartbeat_ms", 100) == 100
                                  and integer(data, "max_frames", 200) == 200)
                except ProtocolError:
                    compatible = False
                if not compatible:
                    self._close_connection("firmware_required", "Проверьте прошивку веб-пульта и радиомодуль")
                    return
                self.store = self._make_store()  # Serial exclusivity is already held.
                self._idle()
                self._board("ready", "RF-Nano подключён")
            elif kind == "ERR":
                self._close_connection("firmware_required", "Нужна прошивка веб-пульта")
            return
        if kind == "ERR":
            raise ProtocolError("Плата отклонила команду: " + line)
        if kind == "ARMED":
            if (data.get("token") != self.token or data.get("action") != ACTIONS[self.chunk_action][0]
                    or data.get("counter", "").lower() != format(self.chunk_counter, "02x")):
                raise ProtocolError("Ответ ARM не совпадает с текущей командой")
            if self.phase == "stopping":
                return
            if self.phase != "arming":
                raise ProtocolError("Неожиданный ответ ARM")
            if not self._live() or self.revision != self.chunk_revision:
                self._request_stop()
                return
            self.phase = "starting"  # Consume this FIRE before writing it.
            self.phase_deadline = self.clock() + 0.5
            self.transport.write("FIRE " + self.token)
        elif kind == "START":
            if (self.phase not in ("starting", "stopping") or data.get("token") != self.token
                    or data.get("action") != ACTIONS[self.chunk_action][0]
                    or integer(data, "duration_ms", 1000) != 1000):
                raise ProtocolError("Ответ START не совпадает с текущей командой")
            if self.phase == "starting":
                self.phase = "active"
                self.phase_deadline = self.clock() + 1.25
                self._event("info", "Передача: " + ACTIONS[self.chunk_action][1])
        elif kind == "DONE":
            if (self.phase not in ("starting", "active", "stopping") or data.get("token") != self.token
                    or data.get("radio") != "off"):
                raise ProtocolError("Ответ DONE не совпадает с текущей командой")
            integer(data, "frames", 200)
            integer(data, "elapsed_ms", 1000)
            if self.phase == "stopping":
                self.stop_done = True
            elif data.get("reason") == "complete":
                self._idle()
            else:
                raise ProtocolError("Передача прервана: " + data.get("reason", "unknown"))
        elif kind == "OK" and data.get("stopped") == "1" and self.phase == "stopping":
            self.stop_ok = True
        if self.phase == "stopping" and self.stop_ok and (self.stop_done or not self.stop_needs_done):
            self._idle()

    def _begin_chunk(self):
        action, revision = self.action, self.revision
        token = self.nonce_factory()
        if not re.fullmatch(r"[0-9a-f]{8}", token):
            raise ValueError("Некорректный токен команды")
        counter = self.store.reserve(ACTIONS[action][0], token)
        if not self._live() or self.action != action or self.revision != revision:
            return  # Reservation remains consumed even if a release raced the disk flush.
        self.token, self.chunk_action = token, action
        self.chunk_counter, self.chunk_revision = counter, revision
        self.phase = "arming"
        self.phase_deadline, self.keep_at = self.clock() + 0.5, self.clock() + KEEP_SECONDS
        self.ping_deadline = 0
        self.transport.write("ARM {} {:02x} {}".format(ACTIONS[action][0], counter, token))

    def step(self):
        """One nonblocking worker iteration; injectable clocks/transports aid tests."""
        with self.lock:
            self._expire()
            try:
                if self.transport is None:
                    self._connect()
                    return
                now = self.clock()
                if self.mode == "demo" and not self.demo_present:
                    self._close_connection("missing", "Демонстрационная плата отключена")
                    return
                if self.mode == "hardware" and now >= self.presence_due:
                    ports = self.port_finder()
                    if self.board.get("port") not in ports:
                        raise MissingError("Подключите RF-Nano к USB")
                    if not self.serial_port and len(ports) != 1:
                        raise OSError("Найдено несколько USB-плат; выберите порт")
                    self.presence_due = now + 0.25
                for line in self.transport.read_lines()[:128]:
                    self._handle(line)
                    if self.transport is None:
                        return
                now = self.clock()
                if self.phase == "handshake":
                    if not self.hello_sent and now >= self.hello_at:
                        self.transport.write("HELLO")
                        self.hello_sent = True
                    if now >= self.phase_deadline:
                        raise ProtocolError("Плата не ответила на HELLO")
                    return
                if self.phase in ("arming", "starting", "active"):
                    if not self._live() or self.revision != self.chunk_revision:
                        self._request_stop()
                        return
                    if now >= self.phase_deadline:
                        raise ProtocolError("Плата не завершила текущую команду")
                    if now >= self.keep_at:
                        self.transport.write("KEEP " + self.token)
                        self.keep_at = now + KEEP_SECONDS
                elif self.phase == "stopping":
                    if now >= self.phase_deadline:
                        raise ProtocolError("Нет подтверждения остановки платы")
                elif self.phase == "idle":
                    if self._live():
                        self._begin_chunk()
                    elif self.ping_deadline and now >= self.ping_deadline:
                        raise ProtocolError("Плата перестала отвечать")
                    elif not self.ping_deadline and now >= self.ping_due:
                        self.transport.write("PING")
                        self.ping_deadline = now + 0.8
            except BusyError as error:
                self._close_connection("busy", str(error))
            except MissingError as error:
                self._close_connection("missing", str(error))
            except Exception as error:
                self._close_connection("error", str(error))

    def start(self):
        if self.thread is not None:
            return
        def run():
            try:
                while not self.shutdown.is_set():
                    self.step()
                    self.wake.wait(0.005)
                    self.wake.clear()
            finally:
                with self.lock:
                    self._close_connection("missing", "Пульт остановлен")
        self.thread = threading.Thread(target=run, name="tractor-serial", daemon=True)
        self.thread.start()

    def close(self):
        self.shutdown.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        else:
            with self.lock:
                self._close_connection("missing", "Пульт остановлен")
