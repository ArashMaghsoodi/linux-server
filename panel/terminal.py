import errno
import json
import logging
import os
import secrets
import signal
import struct
import threading
import time
from queue import Empty, Full, Queue
from urllib.parse import urlsplit

from flask import jsonify, request, session
from flask_sock import Sock
from simple_websocket import ConnectionClosed


TOKEN_TTL_SECONDS = 30
MAX_MESSAGE_LENGTH = 65536
MAX_TERMINAL_SESSIONS = 4
TERMINAL_ROWS = (5, 80)
TERMINAL_COLUMNS = (20, 200)
_terminal_tokens = {}
_terminal_tokens_lock = threading.Lock()
_terminal_sessions = threading.BoundedSemaphore(MAX_TERMINAL_SESSIONS)
_active_terminal_sessions = {}
_active_terminal_sessions_lock = threading.Lock()
_logger = logging.getLogger(__name__)


def parse_terminal_message(raw_message):
    if not isinstance(raw_message, str) or len(raw_message) > MAX_MESSAGE_LENGTH:
        raise ValueError("Invalid terminal message")
    try:
        message = json.loads(raw_message)
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid terminal message") from error
    if not isinstance(message, dict):
        raise ValueError("Invalid terminal message")
    if message.get("type") == "input":
        data = message.get("data")
        if not isinstance(data, str) or len(data.encode("utf-8")) > MAX_MESSAGE_LENGTH:
            raise ValueError("Invalid terminal input")
        return "input", data
    if message.get("type") == "resize":
        columns = message.get("cols")
        rows = message.get("rows")
        if type(columns) is not int or type(rows) is not int:
            raise ValueError("Invalid terminal dimensions")
        return (
            "resize",
            min(max(columns, TERMINAL_COLUMNS[0]), TERMINAL_COLUMNS[1]),
            min(max(rows, TERMINAL_ROWS[0]), TERMINAL_ROWS[1]),
        )
    raise ValueError("Unsupported terminal message")


def is_same_origin(origin, host):
    if not isinstance(origin, str) or not origin:
        return False
    try:
        parsed = urlsplit(origin)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.netloc)
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and parsed.netloc.lower() == host.lower()
        )
    except ValueError:
        return False


def _session_identifier():
    identifier = session.get("terminal_session_id")
    if not identifier:
        identifier = secrets.token_urlsafe(32)
        session["terminal_session_id"] = identifier
    return identifier


def issue_terminal_token(session_identifier):
    token = secrets.token_urlsafe(32)
    expires_at = time.monotonic() + TOKEN_TTL_SECONDS
    with _terminal_tokens_lock:
        expired = [
            existing for existing, (_, deadline) in _terminal_tokens.items()
            if deadline <= time.monotonic()
        ]
        for existing in expired:
            _terminal_tokens.pop(existing, None)
        _terminal_tokens[token] = (session_identifier, expires_at)
    return token


def consume_terminal_token(token, session_identifier):
    with _terminal_tokens_lock:
        record = _terminal_tokens.get(token)
        if record is None:
            return False
        expected_session, expires_at = record
        if expires_at <= time.monotonic():
            _terminal_tokens.pop(token, None)
            return False
        if not secrets.compare_digest(expected_session, session_identifier):
            return False
        _terminal_tokens.pop(token, None)
        return True


def close_terminal_session(session_identifier):
    if not isinstance(session_identifier, str) or not session_identifier:
        return False
    with _active_terminal_sessions_lock:
        stop_event = _active_terminal_sessions.get(session_identifier)
    if stop_event is None:
        return False
    stop_event.set()
    return True


def _reject_terminal(websocket, message):
    try:
        websocket.send(json.dumps({"type": "error", "message": message}))
    except ConnectionClosed:
        return
    websocket.close()


def register_terminal_routes(app, login_required):
    sock = Sock(app)
    sock_options = app.config.setdefault("SOCK_SERVER_OPTIONS", {})
    sock_options.setdefault("max_message_size", MAX_MESSAGE_LENGTH)
    sock_options.setdefault("ping_interval", 25)

    @app.get("/api/terminal/token")
    @login_required
    def terminal_token():
        return jsonify(
            token=issue_terminal_token(_session_identifier()),
            supported=os.name == "posix",
        )

    @sock.route("/api/terminal/socket")
    def terminal_socket(websocket):
        if not session.get("logged_in"):
            _logger.warning("Rejected unauthenticated terminal WebSocket")
            _reject_terminal(websocket, "Terminal authorization failed.")
            return
        if not is_same_origin(request.headers.get("Origin"), request.host):
            _logger.warning("Rejected terminal WebSocket with an invalid origin")
            _reject_terminal(websocket, "Terminal authorization failed.")
            return

        session_identifier = session.get("terminal_session_id")
        if not session_identifier:
            _logger.warning("Rejected terminal WebSocket without a session identifier")
            _reject_terminal(websocket, "Terminal authorization failed.")
            return
        try:
            auth_message = websocket.receive(timeout=5)
            auth = json.loads(auth_message) if isinstance(auth_message, str) else {}
        except (ConnectionClosed, ValueError, TypeError):
            _logger.warning("Rejected terminal WebSocket with invalid authentication")
            _reject_terminal(websocket, "Terminal authorization failed.")
            return
        if (
            not isinstance(auth, dict)
            or auth.get("type") != "auth"
            or not isinstance(auth.get("token"), str)
            or not consume_terminal_token(auth["token"], session_identifier)
        ):
            _logger.warning("Rejected terminal WebSocket with invalid authentication")
            _reject_terminal(websocket, "Terminal authorization failed.")
            return
        if not _terminal_sessions.acquire(blocking=False):
            _reject_terminal(websocket, "All terminal sessions are currently in use.")
            return
        stop_event = threading.Event()
        with _active_terminal_sessions_lock:
            if session_identifier in _active_terminal_sessions:
                _terminal_sessions.release()
                _reject_terminal(
                    websocket,
                    "This panel session already has a terminal connection.",
                )
                return
            _active_terminal_sessions[session_identifier] = stop_event

        try:
            _run_terminal_session(websocket, stop_event)
        finally:
            with _active_terminal_sessions_lock:
                if _active_terminal_sessions.get(session_identifier) is stop_event:
                    _active_terminal_sessions.pop(session_identifier, None)
            _terminal_sessions.release()


def _start_shell():
    if os.name != "posix":
        raise OSError("Interactive terminal sessions require a Linux host.")

    import pty

    shell = "/bin/bash"
    if not os.path.isfile(shell):
        raise OSError("The host shell is not available.")
    pid, master_fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(os.path.expanduser("~"))
            environment = os.environ.copy()
            environment["TERM"] = "xterm-256color"
            os.execvpe(shell, [shell, "--login"], environment)
        except OSError as error:
            os.write(2, f"Could not start shell: {error}\r\n".encode("utf-8", "replace"))
            os._exit(127)
    try:
        _resize_terminal(master_fd, 80, 24)
    except OSError:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os.close(master_fd)
        raise
    return pid, master_fd


def _resize_terminal(master_fd, columns, rows):
    import fcntl
    import termios

    fcntl.ioctl(
        master_fd,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", rows, columns, 0, 0),
    )


def _read_terminal_output(master_fd, output_queue, stopped):
    while not stopped.is_set():
        try:
            output = os.read(master_fd, 4096)
        except OSError as error:
            if error.errno == errno.EIO:
                return
            raise
        if not output:
            return
        while not stopped.is_set():
            try:
                output_queue.put(output, timeout=0.1)
                break
            except Full:
                continue


def _write_terminal_input(master_fd, data):
    remaining = memoryview(data.encode("utf-8"))
    while remaining:
        written = os.write(master_fd, remaining)
        remaining = remaining[written:]


def _stop_shell(pid, master_fd, reader_stopped):
    reader_stopped.set()
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 2
    process_reaped = False
    while time.monotonic() < deadline:
        if not process_reaped:
            try:
                waited_pid, _ = os.waitpid(pid, os.WNOHANG)
                process_reaped = bool(waited_pid)
            except ChildProcessError:
                process_reaped = True
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if not process_reaped:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
    try:
        os.close(master_fd)
    except OSError:
        pass


def _run_terminal_session(websocket, stop_event):
    if stop_event.is_set():
        return
    try:
        pid, master_fd = _start_shell()
    except OSError:
        _logger.exception("Could not start the terminal shell")
        websocket.send(json.dumps({
            "type": "error",
            "message": "Terminal is unavailable on this host.",
        }))
        return
    output_queue = Queue(maxsize=32)
    reader_stopped = threading.Event()
    reader_finished = threading.Event()
    reader_failed = threading.Event()

    def read_output():
        try:
            _read_terminal_output(master_fd, output_queue, reader_stopped)
        except OSError:
            reader_failed.set()
            _logger.exception("Terminal output reader failed")
        finally:
            reader_finished.set()

    reader = threading.Thread(
        target=read_output,
        daemon=True,
    )
    reader.start()
    try:
        websocket.send(json.dumps({"type": "ready"}))
        process_status = None
        while not stop_event.is_set():
            try:
                raw_message = websocket.receive(timeout=0.05)
            except ConnectionClosed:
                break
            if raw_message is not None:
                try:
                    message = parse_terminal_message(raw_message)
                    if message[0] == "input":
                        _write_terminal_input(master_fd, message[1])
                    else:
                        _resize_terminal(master_fd, message[1], message[2])
                except ValueError:
                    websocket.send(json.dumps({
                        "type": "error",
                        "message": "Invalid terminal message.",
                    }))
                    websocket.close()
                    break

            while True:
                try:
                    output = output_queue.get_nowait()
                except Empty:
                    break
                websocket.send(json.dumps({
                    "type": "output",
                    "data": output.decode("utf-8", "replace"),
                }))

            if reader_failed.is_set():
                websocket.send(json.dumps({
                    "type": "error",
                    "message": "Terminal output stopped unexpectedly.",
                }))
                break

            if process_status is None:
                waited_pid, status = os.waitpid(pid, os.WNOHANG)
                if waited_pid:
                    process_status = os.waitstatus_to_exitcode(status)
            if process_status is not None and reader_finished.is_set() and output_queue.empty():
                websocket.send(json.dumps({
                    "type": "exit",
                    "code": process_status,
                }))
                break
    except OSError:
        _logger.exception("Terminal session I/O failed")
        try:
            websocket.send(json.dumps({
                "type": "error",
                "message": "Terminal I/O failed.",
            }))
        except (ConnectionClosed, OSError):
            pass
    finally:
        _stop_shell(pid, master_fd, reader_stopped)
        reader.join(timeout=1)
