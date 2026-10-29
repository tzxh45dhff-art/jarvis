"""
Keep-alive HTTPS for model calls.

`urllib.request.urlopen` opens a new TCP + TLS connection for every request.
Measured on this Mac's network (Oct 2026): 0.6-2.6 s of connect and handshake
before a single byte of the request is sent - on every model call, several per
task. A pooled session reuses the open connection, so only the first call pays.

`urlopen` here is a drop-in for the urllib one: same Request in, a response
that iterates lines, reads, closes and works as a context manager, and the same
`urllib.error.HTTPError` (with .code, .headers and .read()) on 4xx/5xx - so the
callers' retry and error handling is unchanged.
"""
from __future__ import annotations

import io
import threading
import time
import urllib.error
import urllib.request

_SESSION = None
_LOCK = threading.Lock()
_LAST_USE: dict[str, float] = {}


def session():
    global _SESSION
    with _LOCK:
        if _SESSION is None:
            import requests
            from requests.adapters import HTTPAdapter
            s = requests.Session()
            ad = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
            s.mount("https://", ad)
            s.mount("http://", ad)
            _SESSION = s
        return _SESSION


# A socket read timeout only fires when the server goes completely silent. A
# stream that stays open trickling keep-alive bytes but no content never trips
# it: a model call once sat 17 minutes like that, with the engine frozen behind
# it. So a watchdog also ends a stream after IDLE seconds without a data line,
# or TOTAL seconds overall; the caller's retry then takes over.
STREAM_IDLE = 75.0
STREAM_TOTAL = 300.0


class StreamStalled(TimeoutError):
    pass


class _Response:
    def __init__(self, r, idle: float | None = None, total: float | None = None):
        self._r = r
        self.status = r.status_code
        self.headers = r.headers
        self._idle = idle if idle is not None else STREAM_IDLE
        self._total = total if total is not None else STREAM_TOTAL
        self._start = self._last = time.time()
        self._done = threading.Event()
        self.killed = ""
        threading.Thread(target=self._watch, daemon=True, name="stream-watchdog").start()

    def _watch(self) -> None:
        while not self._done.wait(1.0):
            now = time.time()
            if now - self._last > self._idle:
                self._kill(f"no data for {self._idle:.0f}s")
            elif now - self._start > self._total:
                self._kill(f"stream exceeded {self._total:.0f}s")
            if self.killed:
                return

    def _kill(self, why: str) -> None:
        self.killed = why
        import socket
        try:                         # unblock a read stuck in recv()
            sock = self._r.raw._connection.sock
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self._r.close()
        except Exception:
            pass

    def __iter__(self):
        try:
            for line in self._lines():
                if self.killed:
                    break
                if line and not line.startswith(b":"):     # SSE comments are keep-alives
                    self._last = time.time()
                yield (line or b"") + b"\n"
        except Exception:
            if not self.killed:
                raise
        finally:
            self._done.set()
        if self.killed:
            raise StreamStalled(self.killed)

    def _lines(self):
        """Lines as soon as their bytes arrive. iter_lines(chunk_size=1024)
        held each line until a whole kilobyte had come in (delaying the
        first words of every reply), and chunk_size=None reads to the end of
        a response that is not chunk-encoded."""
        raw = self._r.raw
        buf = b""
        while True:
            chunk = raw.read1(8192) if hasattr(raw, "read1") else raw.read(1024)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.rstrip(b"\r")
        if buf:
            yield buf.rstrip(b"\r")

    def read(self, n: int = -1) -> bytes:
        return self._r.content if n is None or n < 0 else self._r.raw.read(n)

    def close(self) -> None:
        self._done.set()
        self._r.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def urlopen(req: urllib.request.Request, timeout: float = 120) -> _Response:
    url = req.full_url
    method = req.get_method()
    headers = dict(req.header_items())
    r = session().request(method, url, data=req.data, headers=headers,
                          timeout=(10, timeout), stream=True)
    host = url.split("/")[2] if "//" in url else url
    _LAST_USE[host] = time.time()
    if r.status_code >= 400:
        body = r.content
        r.close()
        raise urllib.error.HTTPError(url, r.status_code, r.reason or "error",
                                     r.headers, io.BytesIO(body))
    return _Response(r)


def keep_warm(url: str, headers: dict, every: float = 45.0) -> None:
    """Keep a pooled connection open while idle with a token-free request, so
    the next real call does not pay the handshake again."""
    host = url.split("/")[2]

    def loop():
        while True:
            time.sleep(every / 3)
            if time.time() - _LAST_USE.get(host, 0) < every:
                continue
            try:
                r = session().get(url, headers=headers, timeout=(10, 20))
                r.close()
                _LAST_USE[host] = time.time()
            except Exception:
                pass

    threading.Thread(target=loop, daemon=True, name=f"keepalive-{host}").start()
