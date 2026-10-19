"""Database access for the FastAPI app.

Pool lives for the lifetime of the process; FastAPI's lifespan creates it
on startup and closes it on shutdown.

Lifespan also owns the /live background poller — see app.live_store for
the cache + panel_live writes."""

import asyncio
import logging
from contextlib import asynccontextmanager

from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

from . import config

log = logging.getLogger('db')

_pool: ConnectionPool | None = None


def init_pool() -> ConnectionPool:
    """Create the connection pool. Idempotent.

    `client_encoding=UTF8` is critical: the cluster was init'd while the LXC's
    LANG was C, so server_encoding is SQL_ASCII. Without overriding the
    client side, psycopg can't tell what encoding to decode TEXT columns
    with and returns raw bytes. Forcing UTF-8 client-side works because
    our stored data IS UTF-8-clean (SPAN's API returns UTF-8 JSON)."""
    global _pool
    if _pool is None:
        # DATABASE_URL, else span_app@localhost with the legacy password file.
        conninfo = make_conninfo(config.database_url(),
                                 options='-c client_encoding=UTF8')
        _pool = ConnectionPool(conninfo, min_size=1, max_size=4, open=True)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@asynccontextmanager
async def lifespan(app):
    """FastAPI lifespan — opens the DB pool + starts the /live poller on
    startup, cancels the poller + closes the pool on shutdown."""
    init_pool()
    # Import lazily so the module-level live_store side effects
    # don't fire before the DB pool exists.
    from . import live_store, mqtt_publisher, span_ebus
    poller = asyncio.create_task(live_store.poller_loop(),
                                 name='live_store.poller')
    try:
        yield
    finally:
        poller.cancel()
        try:
            await poller
        except (asyncio.CancelledError, Exception) as e:  # noqa: BLE001
            if not isinstance(e, asyncio.CancelledError):
                log.warning(f'live_store poller exited with error: {e}')
        # Flush LWT-style "offline" + drop the MQTT thread so systemd's
        # SIGTERM doesn't have to wait on it.
        mqtt_publisher.shutdown()
        span_ebus.shutdown()
        close_pool()


def pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError('DB pool not initialized — did lifespan run?')
    return _pool
