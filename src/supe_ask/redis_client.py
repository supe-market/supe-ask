from __future__ import annotations

import socket
import ssl
import time
from dataclasses import dataclass
from threading import Event
from types import TracebackType
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from .config import settings


class RedisCommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class RedisConfig:
    host: str
    port: int
    db: int = 0
    username: str = ""
    password: str = ""
    use_ssl: bool = False
    socket_timeout_seconds: float = 1.0


def _settings_to_redis_config() -> RedisConfig | None:
    if settings.redis_url:
        parsed = urlparse(settings.redis_url)
        if not parsed.hostname:
            return None
        return RedisConfig(
            host=parsed.hostname,
            port=parsed.port or (6380 if parsed.scheme == "rediss" else 6379),
            db=int((parsed.path or "/0").lstrip("/") or 0),
            username=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            use_ssl=parsed.scheme == "rediss",
        )
    if settings.redis_host:
        return RedisConfig(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            username=settings.redis_username,
            password=settings.redis_password,
        )
    return None


def get_redis_config() -> RedisConfig | None:
    return _settings_to_redis_config()


def _encode_command(*parts: str | int) -> bytes:
    encoded_parts = [str(part).encode("utf-8") for part in parts]
    chunks = [f"*{len(encoded_parts)}\r\n".encode("utf-8")]
    for item in encoded_parts:
        chunks.append(f"${len(item)}\r\n".encode("utf-8"))
        chunks.append(item)
        chunks.append(b"\r\n")
    return b"".join(chunks)


def _read_line(stream) -> bytes:
    line = stream.readline()
    if not line:
        raise ConnectionError("Redis connection closed")
    if not line.endswith(b"\r\n"):
        raise ConnectionError("Malformed Redis response")
    return line[:-2]


def _parse_response(stream) -> Any:
    prefix = stream.read(1)
    if not prefix:
        raise ConnectionError("Redis connection closed")
    if prefix == b"+":
        return _read_line(stream).decode("utf-8")
    if prefix == b"-":
        raise RedisCommandError(_read_line(stream).decode("utf-8"))
    if prefix == b":":
        return int(_read_line(stream))
    if prefix == b"$":
        length = int(_read_line(stream))
        if length == -1:
            return None
        payload = stream.read(length)
        stream.read(2)
        return payload.decode("utf-8")
    if prefix == b"*":
        length = int(_read_line(stream))
        if length == -1:
            return None
        return [_parse_response(stream) for _ in range(length)]
    raise ConnectionError(f"Unsupported Redis response prefix: {prefix!r}")


class RedisConnection:
    def __init__(self, config: RedisConfig) -> None:
        self._config = config
        self._socket: socket.socket | ssl.SSLSocket | None = None
        self._stream = None

    def __enter__(self) -> "RedisConnection":
        raw_socket = socket.create_connection(
            (self._config.host, self._config.port),
            timeout=self._config.socket_timeout_seconds,
        )
        raw_socket.settimeout(self._config.socket_timeout_seconds)
        if self._config.use_ssl:
            context = ssl.create_default_context()
            self._socket = context.wrap_socket(raw_socket, server_hostname=self._config.host)
            self._socket.settimeout(self._config.socket_timeout_seconds)
        else:
            self._socket = raw_socket
        self._stream = self._socket.makefile("rwb")
        if self._config.password:
            if self._config.username:
                self.execute("AUTH", self._config.username, self._config.password)
            else:
                self.execute("AUTH", self._config.password)
        if self._config.db:
            self.execute("SELECT", self._config.db)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._stream is not None:
            self._stream.close()
        if self._socket is not None:
            self._socket.close()

    def execute(self, *parts: str | int) -> Any:
        if self._stream is None:
            raise RuntimeError("Redis connection is not open")
        self._stream.write(_encode_command(*parts))
        self._stream.flush()
        return _parse_response(self._stream)

    def read_response(self) -> Any:
        if self._stream is None:
            raise RuntimeError("Redis connection is not open")
        return _parse_response(self._stream)


class RedisClient:
    def __init__(self, config: RedisConfig) -> None:
        self._config = config

    @classmethod
    def from_settings(cls) -> "RedisClient | None":
        config = get_redis_config()
        if not config:
            return None
        return cls(config)

    def get(self, key: str) -> str | None:
        with RedisConnection(self._config) as connection:
            response = connection.execute("GET", key)
        return str(response) if response is not None else None

    def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        with RedisConnection(self._config) as connection:
            connection.execute("SET", key, value, "EX", ttl_seconds)

    def publish(self, channel: str, message: str) -> int:
        with RedisConnection(self._config) as connection:
            response = connection.execute("PUBLISH", channel, message)
        return int(response or 0)

    def subscribe_forever(self, channel: str, on_message: Callable[[str], None], stop_event: Event) -> None:
        backoff_seconds = 0.25
        while not stop_event.is_set():
            try:
                with RedisConnection(self._config) as connection:
                    connection.execute("SUBSCRIBE", channel)
                    while not stop_event.is_set():
                        try:
                            payload = connection.read_response()
                        except socket.timeout:
                            continue
                        if not isinstance(payload, list) or len(payload) < 3:
                            continue
                        message_type = str(payload[0])
                        if message_type == "subscribe":
                            continue
                        if message_type == "message" and str(payload[1]) == channel:
                            on_message(str(payload[2]))
            except socket.timeout:
                continue
            except Exception:
                if stop_event.is_set():
                    return
                time.sleep(backoff_seconds)
