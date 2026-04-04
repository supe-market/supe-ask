from __future__ import annotations

import json
from dataclasses import dataclass
from queue import Queue
from threading import Event, Lock, Thread
from typing import Callable

from .config import settings
from .redis_client import RedisClient


@dataclass
class EventSubscription:
    queue: Queue
    _close: Callable[[], None]

    def close(self) -> None:
        self._close()


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Queue]] = {}
        self._lock = Lock()
        self._redis = RedisClient.from_settings() if settings.event_bus_backend == "redis" else None

    def subscribe(self, run_id: str) -> EventSubscription:
        queue: Queue = Queue()
        with self._lock:
            self._subscribers.setdefault(run_id, []).append(queue)

        stop_event = Event()
        listener: Thread | None = None
        if self._redis:
            listener = Thread(
                target=self._redis.subscribe_forever,
                args=(self._channel(run_id), lambda message: self._handle_remote_message(run_id, queue, message), stop_event),
                daemon=True,
            )
            listener.start()

        def _close() -> None:
            stop_event.set()
            if listener and listener.is_alive():
                listener.join(timeout=1.0)
            self._unsubscribe_local(run_id, queue)

        return EventSubscription(queue=queue, _close=_close)

    def publish(self, run_id: str, event: dict) -> None:
        self._publish_local(run_id, event)
        if self._redis:
            try:
                self._redis.publish(self._channel(run_id), json.dumps(event, ensure_ascii=True))
            except Exception:
                return

    def _handle_remote_message(self, run_id: str, queue: Queue, payload: str) -> None:
        try:
            event = json.loads(payload)
        except Exception:
            return
        queue.put(event)

    def _publish_local(self, run_id: str, event: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers.get(run_id, []))
        for queue in subscribers:
            queue.put(event)

    def _unsubscribe_local(self, run_id: str, queue: Queue) -> None:
        with self._lock:
            subscribers = self._subscribers.get(run_id, [])
            self._subscribers[run_id] = [item for item in subscribers if item is not queue]
            if not self._subscribers[run_id]:
                self._subscribers.pop(run_id, None)

    def _channel(self, run_id: str) -> str:
        return f"ask:events:run:{run_id}"


event_bus = EventBus()
