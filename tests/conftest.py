"""Shared fixtures. The page tests need a database: DATABASE_URL pointing at
one with the schema applied and scripts/sample_data.py run (CI does this; see
.gitlab-ci.yml). Without DATABASE_URL they're skipped.

`fake_db` scripts the app's SQL for the pure-logic tests, which run anywhere.
`txn` is a real connection whose writes are always rolled back, for the few
tests that need real SQL; they're skipped without DATABASE_URL too."""

import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Never read a real deployment's /etc/span-viewer files from the tests.
os.environ.setdefault('SPAN_VIEWER_CONFIG', str(ROOT / 'tests' / 'no-such-config.toml'))
os.environ.setdefault('CIRCUITS_FILE', str(ROOT / 'circuits.example.yaml'))
os.environ.setdefault('SPAN_LIVE_SOURCE', 'off')
os.environ.setdefault('TZ', 'America/New_York')


@pytest.fixture(scope='session')
def client():
    if not os.environ.get('DATABASE_URL'):
        pytest.skip('DATABASE_URL not set: no database to test the pages against')
    from contextlib import asynccontextmanager

    from fastapi.testclient import TestClient

    from app import db
    from app.main import app

    # The DB pool only: no live poller, MQTT publisher or eBus subscription.
    @asynccontextmanager
    async def pool_only(_app):
        db.init_pool()
        try:
            yield
        finally:
            db.close_pool()

    app.router.lifespan_context = pool_only
    with TestClient(app) as c:
        yield c


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.db.calls.append((sql, params))
        assert self.db.results, f'unscripted query: {sql}'
        result = self.db.results.pop(0)
        self.rows = list(result() if callable(result) else result)

    def executemany(self, sql, rows):
        self.db.calls.append((sql, list(rows)))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeDb:
    """A pool and a connection in one. Each execute() answers with the next
    result set, in order; a result may be a callable returning the rows, for
    rows only known once the code under test has run."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    @contextmanager
    def connection(self):
        yield self

    def cursor(self, **_kwargs):
        return FakeCursor(self)

    @property
    def sql(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def fake_db(monkeypatch):
    """fake_db(rows1, rows2, ...): app.queries (or `module=`) reads these
    instead of a database. Every scripted result must be used."""
    from app import queries
    installed = []

    def install(*results, module=queries):
        db = FakeDb(results)
        monkeypatch.setattr(module, 'pool', lambda: db)
        installed.append(db)
        return db

    yield install
    for db in installed:
        assert not db.results, f'{len(db.results)} scripted results were never queried'


class _OneConnection:
    """Stands in for the pool, without the commit a pooled connection does."""

    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def connection(self):
        yield self.conn


@pytest.fixture
def txn(monkeypatch):
    """A real connection (DATABASE_URL) that app.queries reads through, in a
    transaction that is rolled back whatever happens, so the sample data the
    smoke tests read is untouched. Write rows dated well before the sample
    data's history (its --days 400 starts in the last two years)."""
    url = os.environ.get('DATABASE_URL')
    if not url:
        pytest.skip('DATABASE_URL not set: no database to test the SQL against')
    import psycopg

    from app import queries

    conn = psycopg.connect(url, autocommit=False)
    monkeypatch.setattr(queries, 'pool', lambda: _OneConnection(conn))
    # The tests' DST dates are New York's, whatever TZ the run has.
    monkeypatch.setattr(queries, 'TZ', 'America/New_York')
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
