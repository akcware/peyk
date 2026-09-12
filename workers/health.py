"""Tiny HTTP health endpoint for the workers process (Lightsail / any orchestrator probes it).
GET /health -> 200 {"ok": true, ...} when the DB answers and every loop has beaten recently; 503 otherwise.
No framework: asyncio streams, one request per connection."""
from __future__ import annotations

import asyncio
import json
import time

from core import db
from core.log import get_logger

log = get_logger("workers.health")

_beats: dict[str, float] = {}
STALE_AFTER_S = 300.0


def beat(loop_name: str) -> None:
    _beats[loop_name] = time.monotonic()


async def status() -> tuple[bool, dict]:
    now = time.monotonic()
    loops = {name: round(now - t, 1) for name, t in _beats.items()}
    stale = [n for n, age in loops.items() if age > STALE_AFTER_S]
    db_ok = True
    try:
        async with db.connection() as conn:
            await conn.execute("select 1")
    except Exception as e:  # noqa: BLE001
        db_ok = False
        log.warning("health.db_failed", error=str(e))
    ok = db_ok and not stale
    return ok, {"ok": ok, "db": db_ok, "loops_age_s": loops, "stale": stale}


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        path = line.split()[1].decode() if len(line.split()) > 1 else "/"
        while (await asyncio.wait_for(reader.readline(), timeout=5)).strip():
            pass
        if path.startswith("/health"):
            ok, body = await status()
            code = "200 OK" if ok else "503 Service Unavailable"
            payload = json.dumps(body).encode()
        else:
            code, payload = "404 Not Found", b"{}"
        writer.write(f"HTTP/1.1 {code}\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload)
        await writer.drain()
    except Exception as e:  # noqa: BLE001 - a bad probe request must not affect the workers
        log.debug("health.request_failed", error=str(e))
    finally:
        writer.close()


async def serve(port: int) -> None:
    server = await asyncio.start_server(_handle, host="0.0.0.0", port=port)
    log.info("health.listening", port=port)
    async with server:
        await server.serve_forever()
