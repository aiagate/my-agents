import json
import os
import queue
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def emit(message: str) -> None:
    sys.stdout.write(f"{message}\n")


DELAY_MS = 1500


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
) -> tuple[int, dict[str, object], float]:
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    started = time.perf_counter()
    try:
        connection.request(
            method,
            path,
            body=body,
            headers={"Content-Type": "application/json"} if body else {},
        )
        response = connection.getresponse()
        payload = response.read()
        elapsed = time.perf_counter() - started
        try:
            parsed = json.loads(payload or b"{}")
        except json.JSONDecodeError:
            parsed = {}
        return response.status, parsed, elapsed
    finally:
        connection.close()


def wait_health(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"server exited early: {process.returncode}")
        try:
            status, payload, _ = request(port, "GET", "/health")
            if status == 200 and payload["status"] == "ok":
                return
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError("server did not become healthy")


def wait_until[T](
    description: str, probe: Callable[[], T | None], *, timeout: float = 15.0
) -> T:
    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {description}")
        time.sleep(0.05)


class SseReader:
    def __init__(self, port: int) -> None:
        self._connection = HTTPConnection("127.0.0.1", port, timeout=10)
        self._connection.request("GET", "/stream?conversation_id=default")
        self._response = self._connection.getresponse()
        self._items: queue.Queue[dict[str, object]] = queue.Queue()
        self._lines: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            while True:
                line = self._response.readline()
                if not line:
                    return
                text = line.decode("utf-8")
                self._lines.append(text)
                if text.startswith("data: "):
                    self._items.put(json.loads(text[6:]))
        except OSError, ValueError:
            return

    def wait_for(self, predicate, *, timeout: float = 15.0) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"SSE item not received; lines={self._lines[-20:]}"
                )
            try:
                item = self._items.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if predicate(item):
                return item

    def empty(self) -> bool:
        return self._items.empty()

    def drain(self) -> None:
        while True:
            try:
                self._items.get_nowait()
            except queue.Empty:
                return

    def close(self) -> None:
        self._response.close()
        self._connection.close()


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path

    def rows(self, sql: str, parameters: tuple[object, ...] = ()) -> list[tuple]:
        if not self._path.exists():
            return []
        with closing(sqlite3.connect(self._path)) as connection:
            try:
                return connection.execute(sql, parameters).fetchall()
            except sqlite3.OperationalError:
                return []

    def scalar(self, sql: str, parameters: tuple[object, ...] = ()) -> object | None:
        rows = self.rows(sql, parameters)
        return None if not rows else rows[0][0]

    def event(self, event_id: str) -> tuple | None:
        rows = self.rows(
            "SELECT status, delivery, failure_reason, surfaced_at FROM events "
            "WHERE event_id = ?",
            (event_id,),
        )
        return None if not rows else rows[0]


def post_event(
    port: int, payload: dict[str, object]
) -> tuple[int, dict[str, object], float]:
    return request(port, "POST", "/events", json.dumps(payload).encode())


def main() -> int:
    port = free_port()
    with tempfile.TemporaryDirectory(prefix="my-agents-e2e-") as tmp:
        root = Path(tmp)
        database = Database(root / "chats.db")
        env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "MAGPIE_MODEL": "local-e2e-model",
            "MAGPIE_BASE_URL": "http://127.0.0.1:9/v1",
            "MAGPIE_API_KEY": "local-e2e-key",
            "MEMORY_PATH": str(root),
            "HOME": str(root / "home"),
            "XDG_STATE_HOME": str(root / "state"),
            "CHAT_BACKEND": "local",
            "SERVER_HOST": "127.0.0.1",
            "SERVER_PORT": str(port),
            "EVENT_ANALYSIS_DELAY_MS": str(DELAY_MS),
            "PROACTIVE_USER_ACTIVE_MINUTES": "0",
            "PROACTIVE_COOLDOWN_MINUTES": "60",
            "PROACTIVE_DAILY_CAP": "100",
            "QUIET_HOURS_START": "0",
            "QUIET_HOURS_END": "0",
            "SUMMARY_EVERY_TURNS": "100",
        }
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from my_agents import main; raise SystemExit(main())",
                "serve",
            ],
            cwd=REPO,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        reader: SseReader | None = None
        try:
            wait_health(port, process)
            emit(f"health=ok port={port} delay_ms={DELAY_MS}")

            status, index, _ = request(port, "GET", "/")
            assert status == 200
            emit(f"index_status={status}")

            reader = SseReader(port)
            time.sleep(0.1)

            proactive: dict[str, object] = {
                "name": "temperature.threshold_exceeded",
                "data": {
                    "sensor_id": "kitchen",
                    "celsius": 33.2,
                    "threshold_celsius": 30.0,
                },
                "idempotency_key": "proactive-key",
            }
            status, accepted, accept_elapsed = post_event(port, proactive)
            event_id = str(accepted["event_id"])
            chat_started = time.perf_counter()
            chat_status, chat, _ = request(
                port,
                "POST",
                "/chat",
                json.dumps({"message": "通常チャット中"}).encode(),
            )
            chat_elapsed = time.perf_counter() - chat_started
            emit(
                f"proactive_status={status} event_id={event_id} "
                f"accept_elapsed_ms={accept_elapsed * 1000:.1f} "
                f"chat_status={chat_status} chat_elapsed_ms={chat_elapsed * 1000:.1f}"
            )
            assert status == 202
            assert accept_elapsed < 0.5
            assert chat_status == 200
            assert chat_elapsed < DELAY_MS / 1000

            item = reader.wait_for(
                lambda value: (
                    value.get("source_event_id") == event_id
                    and value.get("delivery") == "push"
                )
            )
            message = str(item["content"])
            assert "evt_" not in message
            assert "[event:" not in message
            assert "UNTRUSTED" not in message
            assert "{" not in message
            assert "}" not in message
            assert len(message.splitlines()) <= 3
            emit(f"proactive_message={message!r}")

            proactive_row = wait_until(
                "proactive completion", lambda: database.event(event_id)
            )
            assert proactive_row[0] == "completed"
            assert proactive_row[1] == "proactive"
            assert proactive_row[3] is not None
            turn = database.rows(
                "SELECT origin, source_event_id, content FROM dialogue_turns "
                "WHERE source_event_id = ?",
                (event_id,),
            )
            assert len(turn) == 1
            assert turn[0][0] == "proactive"
            assert turn[0][2] == message
            emit(f"proactive_turn={turn[0][:2]}")

            status, duplicate, _ = post_event(port, proactive)
            emit(
                f"duplicate_status={status} duplicate={duplicate['duplicate']} "
                f"event_id={duplicate['event_id']}"
            )
            assert status == 202
            assert duplicate["duplicate"] is True
            assert duplicate["event_id"] == event_id
            assert (
                database.scalar(
                    "SELECT COUNT(*) FROM events WHERE idempotency_key = ?",
                    ("proactive-key",),
                )
                == 1
            )

            reader.drain()
            ambient: dict[str, object] = {
                "name": "email.received",
                "data": {
                    "sender": "alerts@example.com",
                    "subject": "定例のお知らせ",
                    "body": "次回の定例は金曜日です。",
                },
                "idempotency_key": "ambient-key",
            }
            status, ambient_accepted, ambient_elapsed = post_event(port, ambient)
            ambient_id = str(ambient_accepted["event_id"])

            def ambient_done() -> tuple | None:
                row = database.event(ambient_id)
                return (
                    row
                    if row is not None and row[0] in {"completed", "failed"}
                    else None
                )

            ambient_row = wait_until("ambient completion", ambient_done)
            emit(
                f"ambient_status={status} event_id={ambient_id} "
                f"accept_elapsed_ms={ambient_elapsed * 1000:.1f} "
                f"delivery={ambient_row[1]}"
            )
            assert status == 202
            assert ambient_row[0] == "completed"
            assert ambient_row[1] == "ambient"
            assert ambient_row[3] is None
            time.sleep(0.2)
            assert all(item["source_event_id"] != ambient_id for item in [])

            status, chat, _ = request(
                port,
                "POST",
                "/chat",
                json.dumps({"message": "背景イベントを教えて"}).encode(),
            )
            reply = str(chat["reply"])
            emit(f"ambient_chat_status={status} reply={reply!r}")
            assert status == 200
            assert ambient_id in reply
            surfaced = wait_until(
                "ambient surfaced_at",
                lambda: database.scalar(
                    "SELECT surfaced_at FROM events WHERE event_id = ?",
                    (ambient_id,),
                ),
            )
            assert surfaced is not None

            status, _, _ = request(port, "POST", "/events", b"{")
            emit(f"malformed_json_status={status}")
            assert status == 400
            status, _, _ = post_event(port, {"name": "Invalid Name"})
            emit(f"schema_violation_status={status}")
            assert status == 422

            fault: dict[str, object] = {
                "name": "spike.worker_failure",
                "data": {"reason": "injected analyzer failure"},
                "idempotency_key": "fault-key",
            }
            status, fault_accepted, fault_elapsed = post_event(port, fault)
            fault_id = str(fault_accepted["event_id"])

            def fault_done() -> tuple | None:
                row = database.event(fault_id)
                return row if row is not None and row[0] == "failed" else None

            failed = wait_until("failed event", fault_done)
            failure_item = reader.wait_for(
                lambda value: value.get("event_id") == fault_id
            )
            emit(
                f"fault_status={status} event_id={fault_id} "
                f"accept_elapsed_ms={fault_elapsed * 1000:.1f} "
                f"failure_kind={failure_item['kind']} process_alive={process.poll() is None}"
            )
            assert status == 202
            assert failed[0] == "failed"
            assert "injected analyzer failure" in str(failed[2])
            assert failure_item["kind"] == "event_failed"
            assert process.poll() is None

            status, timeline, _ = request(port, "GET", "/timeline?limit=50")
            raw_items = timeline.get("items")
            assert isinstance(raw_items, list)
            kinds = {str(item["kind"]) for item in raw_items if isinstance(item, dict)}
            assert status == 200
            assert {"turn", "event", "event_failed"} <= kinds
            status, event_detail, _ = request(port, "GET", f"/events/{event_id}")
            assert status == 200
            assert event_detail["delivery"] == "proactive"
            status, memory, _ = request(port, "GET", "/memory")
            assert status == 200
            status, curated, _ = request(
                port, "POST", "/curate", json.dumps({}).encode()
            )
            assert status == 200
            emit(
                f"timeline_kinds={sorted(kinds)} memory_chars={memory['chars']} "
                f"curated={curated}"
            )

            raw_rows = database.scalar(
                "SELECT COUNT(*) FROM dialogue_turns WHERE content LIKE '%[event:%' "
                "OR content LIKE '%UNTRUSTED%' OR content LIKE '%\"name\"%'"
            )
            event_rows = database.rows(
                "SELECT event_id, status, delivery, surfaced_at FROM events ORDER BY received_at"
            )
            emit(f"db_raw_event_content={raw_rows} db_events={event_rows}")
            assert raw_rows == 0
            assert {str(row[1]) for row in event_rows} == {"completed", "failed"}
            assert {str(row[2]) for row in event_rows} == {"proactive", "ambient"}

            status, reset, _ = request(port, "POST", "/reset")
            emit(f"reset_status={status} deleted_turns={reset['deleted_turns']}")
            assert status == 200
            assert int(str(reset["deleted_turns"])) >= 4

            reader.close()
            reader = None
            process.terminate()
            exit_code = process.wait(timeout=20)
            emit(f"process_exit_code={exit_code}")
            assert exit_code in (0, -15)
        finally:
            if reader is not None:
                reader.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
