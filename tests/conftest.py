"""Shared fixtures. The page tests need a database: DATABASE_URL pointing at
one with the schema applied and scripts/sample_data.py run (CI does this; see
.gitlab-ci.yml). Without DATABASE_URL they're skipped."""

import os
import sys
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
