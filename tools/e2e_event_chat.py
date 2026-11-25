import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DELAY_MS = 1500


class Reader:
    def __init__(self, stream) -> None:
        self._stream = stream
        self._buffer = ""
        self._eof = False
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        while True:
            chunk = self._stream.read(1)
            if not chunk:
                with self._changed:
                    self._eof = True
                    self._changed.notify_all()
                return
            with self._changed:
                self._buffer += chunk
                self._changed.notify_all()

    def text(self) -> str:
        with self._lock:
            return self._buffer

    def wait_for(self, needle: str, *, start: int = 0, timeout: float = 15.0) -> int:
        deadline = time.monotonic() + timeout
        with self._changed:
            while needle not in self._buffer[start:]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"timed out waiting for {needle!r}; transcript:\n{self._buffer}"
                    )
                if self._eof and needle not in self._buffer[start:]:
                    raise AssertionError(
                        f"process ended before {needle!r}; transcript:\n{self._buffer}"
                    )
                self._changed.wait(remaining)
            return self._buffer.index(needle, start) + len(needle)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def post(port: int, body: bytes) -> tuple[int, dict[str, object], float]:
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    started = time.perf_counter()
    try:
        connection.request(
            "POST",
            "/events",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        elapsed = time.perf_counter() - started
        return response.status, payload, elapsed
    finally:
        connection.close()


def send(process: subprocess.Popen[str], line: str) -> None:
    assert process.stdin is not None
    process.stdin.write(line + "\n")
    process.stdin.flush()


def main() -> int:
    port = free_port()
    with tempfile.TemporaryDirectory(prefix="my-agents-e2e-") as tmp:
        env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "MAGPIE_MODEL": "local-e2e-model",
            "MAGPIE_BASE_URL": "http://127.0.0.1:9/v1",
            "MAGPIE_API_KEY": "local-e2e-key",
            "MEMORY_PATH": tmp,
            "HOME": str(Path(tmp) / "home"),
            "XDG_STATE_HOME": str(Path(tmp) / "state"),
            "CHAT_BACKEND": "local",
            "EVENT_WEBHOOK_PORT": str(port),
            "EVENT_ANALYSIS_DELAY_MS": str(DELAY_MS),
        }
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from my_agents import main; raise SystemExit(main())",
            ],
            cwd=REPO,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        reader = Reader(process.stdout)
        try:
            reader.wait_for("Event webhook listening on http://127.0.0.1:")
            reader.wait_for("会話を開始します。")

            mark = len(reader.text())
            send(process, "こんにちは")
            reader.wait_for("AI > ローカル応答: こんにちは", start=mark)

            event = {
                "name": "temperature.threshold_exceeded",
                "data": {
                    "sensor_id": "kitchen",
                    "celsius": 33.2,
                    "threshold_celsius": 30.0,
                },
            }
            mark = len(reader.text())
            status, accepted, accepted_elapsed = post(port, json.dumps(event).encode())
            event_id = str(accepted["event_id"])
            sys.stdout.write(
                f"webhook_status={status} event_id={event_id} "
                f"accept_elapsed_ms={accepted_elapsed * 1000:.1f} "
                f"worker_delay_ms={DELAY_MS}\n"
            )

            mark = len(reader.text())
            chat_started = time.perf_counter()
            send(process, "通常チャットは止まっていませんか")
            reader.wait_for(
                "AI > ローカル応答: 通常チャットは止まっていませんか", start=mark
            )
            chat_elapsed = time.perf_counter() - chat_started
            before_completion = reader.text()
            completion_present = f"[Event {event_id}: completed]" in before_completion
            sys.stdout.write(
                f"chat_during_worker=ok chat_elapsed_ms={chat_elapsed * 1000:.1f} "
                f"completion_present_while_pending={completion_present}\n"
            )
            assert accepted_elapsed < 0.5
            assert chat_elapsed < DELAY_MS / 1000
            assert not completion_present

            completion_mark = reader.wait_for(f"[Event {event_id}: completed]")
            sys.stdout.write(
                f"completion_notification=ok at_buffer_offset={completion_mark}\n"
            )

            mark = len(reader.text())
            send(process, "直近のイベントIDを教えて")
            reader.wait_for(f"直近のイベントは {event_id} です", start=mark)
            sys.stdout.write("followup_chat_references_event_id=ok\n")

            mark = len(reader.text())
            send(process, "/history 1")
            reader.wait_for(f"[event:{event_id}] completed", start=mark)
            sys.stdout.write("history_detail_contains_event_id=ok\n")

            status, malformed, _ = post(port, b"{")
            sys.stdout.write(f"malformed_json_status={status} body={malformed}\n")
            status, schema, _ = post(
                port, json.dumps({"name": "email.received", "data": {}}).encode()
            )
            sys.stdout.write(f"schema_violation_status={status} body={schema}\n")

            fault = {
                "name": "spike.worker_failure",
                "data": {"reason": "injected analyzer failure"},
            }
            mark = len(reader.text())
            status, fault_accepted, fault_elapsed = post(
                port, json.dumps(fault).encode()
            )
            fault_id = str(fault_accepted["event_id"])
            sys.stdout.write(
                f"fault_status={status} event_id={fault_id} "
                f"accept_elapsed_ms={fault_elapsed * 1000:.1f}\n"
            )
            reader.wait_for(f"[Event {fault_id}: failed]", start=mark)
            sys.stdout.write(
                f"failure_notification=ok process_alive={process.poll() is None}\n"
            )

            mark = len(reader.text())
            send(process, "/history 1")
            reader.wait_for(f"[event:{fault_id}] failed", start=mark)
            sys.stdout.write("history_detail_contains_failure_event=ok\n")

            send(process, "exit")
            exit_code = process.wait(timeout=20)
            sys.stdout.write(f"process_exit_code={exit_code}\n")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            sys.stdout.write("\n===== RAW TRANSCRIPT =====\n")
            sys.stdout.write(reader.text())
            sys.stdout.write("\n===== END TRANSCRIPT =====\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
