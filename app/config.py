"""config.py — every deployment-specific setting, in one place.

Each setting is read, in order, from:
  1. the environment (systemd `EnvironmentFile=`, docker-compose `environment:`);
  2. optional files: `$SPAN_VIEWER_CONFIG` (TOML, or KEY=VALUE lines when it
     ends in .env), else whichever of `/etc/span-viewer/config.toml` and
     `/etc/span-viewer/span-viewer.env` exist (the .env wins). Keys are the env
     var names, upper or lower case (see config.example.toml). The .env form is
     systemd's EnvironmentFile format, so one file can feed the units and also
     scripts run by hand, which don't get the units' environment;
  3. the default below. No default names a host: a setting that needs one is
     off until it's set.

Circuit roles (appliance signatures, EV, duty-cycle circuits, the sub-panel's
name) come from circuits.yaml; see circuits.example.yaml and `circuits()`.

Stdlib only (plus PyYAML for circuits.yaml), so the ingesters can import it
without pulling in the web app.
"""

from __future__ import annotations

import logging
import os
import tomllib
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlsplit

log = logging.getLogger('config')

REPO_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIGS = (Path('/etc/span-viewer/config.toml'),
                    Path('/etc/span-viewer/span-viewer.env'))
_DEFAULT_CIRCUITS = Path('/etc/span-viewer/circuits.yaml')
# Before DATABASE_URL existed the app read the span_app password from this
# file and connected to localhost; it stays the fallback.
_LEGACY_DB_PW = Path('/root/.span-viewer-db-password')


def _read_env_file(p: Path) -> dict:
    """KEY=VALUE lines (systemd EnvironmentFile): # comments, optional quotes."""
    out = {}
    for line in p.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        out[k.strip()] = v
    return out


def _read(p: Path) -> dict:
    if p.suffix == '.env':
        data = _read_env_file(p)
    else:
        with p.open('rb') as f:
            data = tomllib.load(f)
    return {str(k).upper(): v for k, v in data.items()}


@lru_cache(maxsize=1)
def _file() -> dict:
    path = os.environ.get('SPAN_VIEWER_CONFIG')
    if path:
        p = Path(path)
        if not p.is_file():
            log.warning(f'SPAN_VIEWER_CONFIG={path} not found; using env and defaults')
            return {}
        return _read(p)
    out: dict = {}
    for p in _DEFAULT_CONFIGS:
        try:
            if p.is_file():
                out.update(_read(p))
        except PermissionError:
            log.warning(f'{p} is not readable; skipping it')
    return out


def get(name: str, default: str | None = None) -> str | None:
    """A setting as a string: env, then the config file, then `default`.
    An empty string counts as unset."""
    v = os.environ.get(name)
    if v is None or v == '':
        v = _file().get(name)
    if v is None or v == '':
        return default
    return str(v)


def get_float(name: str, default: float | None = None) -> float | None:
    v = get(name)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        log.warning(f'{name}={v!r} is not a number; ignoring it')
        return default


# ---- Settings -------------------------------------------------------------

@lru_cache(maxsize=1)
def tz() -> str:
    """IANA zone for every "local day" in the SQL and on the pages (UTC when
    unset or not a zone name). It's interpolated into SQL, so it must be one
    that zoneinfo knows."""
    from zoneinfo import ZoneInfo
    name = get('TZ', 'UTC')
    try:
        ZoneInfo(name)
    except Exception:  # noqa: BLE001 - ':/etc/localtime', typos, '' ...
        log.warning(f'TZ={name!r} is not an IANA zone name; using UTC')
        return 'UTC'
    if "'" in name:
        return 'UTC'
    return name


def database_url() -> str:
    """libpq conninfo or URL. Falls back to span_app on localhost with the
    password in /root/.span-viewer-db-password (the pre-config behaviour)."""
    url = get('DATABASE_URL')
    if url:
        return url
    if _LEGACY_DB_PW.is_file():
        pw = _LEGACY_DB_PW.read_text().strip()
        return f'postgresql://span_app:{quote(pw, safe="")}@localhost/span'
    raise RuntimeError('DATABASE_URL is not set (and no legacy password file)')


def span_host() -> str | None:
    """The panel's address (IP or name, no scheme). Overrides the address in
    the credentials files when set; unset, those files' addresses are used."""
    h = get('SPAN_HOST')
    if not h:
        return None
    return urlsplit(h).hostname if '//' in h else h.strip('/')


def span_base_url(fallback: str | None = None) -> str | None:
    """http://SPAN_HOST for the panel's REST API, else `fallback`."""
    h = span_host()
    return f'http://{h}' if h else (fallback.rstrip('/') if fallback else None)


def mqtt_url() -> dict | None:
    """MQTT_URL (mqtt://[user:pass@]host[:port][/topic_prefix]) parsed into
    the shape of mqtt-credentials.json, or None when unset. A user and
    password in the URL win; otherwise MQTT_USERNAME / MQTT_PASSWORD."""
    url = get('MQTT_URL')
    if not url:
        return None
    u = urlsplit(url if '//' in url else f'mqtt://{url}')
    if not u.hostname:
        log.warning('MQTT_URL has no host; MQTT publishing stays off')
        return None
    return {
        'host': u.hostname,
        'port': u.port or 1883,
        'username': u.username or get('MQTT_USERNAME'),
        'password': u.password or get('MQTT_PASSWORD'),
        # None: keep the credentials file's prefix, else the publisher's 'span'.
        'topic_prefix': (u.path or '').strip('/') or get('MQTT_TOPIC_PREFIX'),
    }


def ntfy() -> tuple[str, str] | None:
    """(NTFY_URL, NTFY_TOPIC), or None when either is unset: no pushes."""
    url, topic = get('NTFY_URL'), get('NTFY_TOPIC')
    if not url or not topic:
        return None
    return url.rstrip('/'), topic


def app_url() -> str | None:
    """The app's public URL, for links in pushes. Unset means no link."""
    u = get('APP_URL')
    return u.rstrip('/') if u else None


def forwarded_allow_ips() -> str:
    """Proxies trusted for X-Forwarded-*; uvicorn's own default when unset."""
    return get('FORWARDED_ALLOW_IPS', '127.0.0.1')


def vm_url() -> str | None:
    """VictoriaMetrics base URL for the optional DB-vs-VM cross-check on
    /health. Off (None) unless set. The series it reads are VM_SOLAR_SERIES
    and VM_LOAD_SERIES (watts, one sample per scrape)."""
    u = get('VM_URL')
    return u.rstrip('/') if u else None


def vm_series() -> dict:
    return {
        'solar': get('VM_SOLAR_SERIES', 'span_panel_solar_power_w'),
        'load':  get('VM_LOAD_SERIES', 'span_panel_load_power_w'),
    }


def redirects() -> dict[str, str]:
    """REDIRECTS: paths the app answers with a 301 to another URL, e.g. pages
    a deployment has moved elsewhere. In config.toml a table
    (`[REDIRECTS]` with `"/old" = "https://..."`); in the environment or a
    .env file, comma-separated `/old=https://...` pairs. Empty when unset."""
    raw = os.environ.get('REDIRECTS') or _file().get('REDIRECTS')
    if not raw:
        return {}
    if isinstance(raw, dict):
        pairs = raw.items()
    else:
        pairs = (p.split('=', 1) for p in str(raw).split(',') if '=' in p)
    out = {}
    for path, url in pairs:
        path, url = str(path).strip(), str(url).strip()
        if path.startswith('/') and url:
            out[path.rstrip('/') or '/'] = url
        else:
            log.warning(f'REDIRECTS: ignoring {path!r} -> {url!r}')
    return out


def flat_rate() -> dict | None:
    """The optional flat tariff: RATE_IMPORT_PER_KWH ($ per imported kWh) and
    RATE_EXPORT_CREDIT_PER_KWH ($ credited per exported kWh; unset means the
    same as import, i.e. 1:1 net metering). None when no import rate is set."""
    imp = get_float('RATE_IMPORT_PER_KWH')
    if imp is None:
        return None
    exp = get_float('RATE_EXPORT_CREDIT_PER_KWH', imp)
    return {'import_per_kwh': imp, 'export_credit_per_kwh': exp}


# ---- circuits.yaml --------------------------------------------------------

_EMPTY_CIRCUITS = {
    'feedthrough_name': 'Sub-panel',
    'appliances': [],
    'ev': {'space_ids': [], 'name_match': ['%charge%', '%EV%'], 'threshold_kw': 1.5},
    'duty_cycle': [],
}


def circuits_path() -> Path | None:
    path = get('CIRCUITS_FILE')
    p = Path(path) if path else _DEFAULT_CIRCUITS
    if p.is_file():
        return p
    if path:
        log.warning(f'CIRCUITS_FILE={path} not found; no circuit roles configured')
    return None


@lru_cache(maxsize=1)
def circuits() -> dict:
    """circuits.yaml, with every section present. Missing file: no
    appliances, no duty-cycle circuits, EV found by name."""
    out = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
           for k, v in _EMPTY_CIRCUITS.items()}
    p = circuits_path()
    if p is None:
        return out
    import yaml
    data = yaml.safe_load(p.read_text(encoding='utf-8')) or {}
    if data.get('feedthrough_name'):
        out['feedthrough_name'] = str(data['feedthrough_name'])
    out['appliances'] = [_appliance(a) for a in data.get('appliances') or []]
    ev = data.get('ev') or {}
    out['ev'] = {
        'space_ids': [str(s) for s in ev.get('space_ids') or []],
        'name_match': list(ev.get('name_match') or _EMPTY_CIRCUITS['ev']['name_match']),
        'threshold_kw': float(ev.get('threshold_kw', 1.5)),
    }
    out['duty_cycle'] = [_duty(d) for d in data.get('duty_cycle') or []]
    return out


def _date_str(v) -> str | None:
    return None if v is None else str(v)   # YAML reads 2026-05-01 as a date


def _appliance(a: dict) -> dict:
    p = dict(a)
    for k in ('key', 'label', 'name_match'):
        if not p.get(k):
            raise ValueError(f'circuits.yaml appliance needs {k!r}: {a}')
    p['threshold_kw'] = float(p['threshold_kw'])
    p['min_kwh'] = float(p.get('min_kwh', 0.0))
    if 'secondary_kw' in p:
        p['secondary_kw'] = float(p['secondary_kw'])
    if 'since_date' in p:
        p['since_date'] = _date_str(p['since_date'])
    p.setdefault('glyph', '●')
    p.setdefault('color', 'blue')
    p.setdefault('note', '')
    return p


def _duty(d: dict) -> dict:
    """A duty-cycle circuit: on when a 10-min bucket averages on_kw or more,
    or (with standby_w and on_w) on-time = (energy - standby) / (on - standby)."""
    p = dict(d)
    if not p.get('key') or not (p.get('space_id') or p.get('name_match')):
        raise ValueError(f'circuits.yaml duty_cycle entry needs key and space_id or name_match: {d}')
    p.setdefault('label', p['key'])
    p['since'] = _date_str(p.get('since'))
    for k in ('on_kw', 'standby_w', 'on_w'):
        if p.get(k) is not None:
            p[k] = float(p[k])
    if p.get('on_kw') is None:
        if p.get('on_w') is not None:
            # Halfway between standby and on is "on" for a bucket.
            p['on_kw'] = (p['on_w'] + (p.get('standby_w') or 0.0)) / 2000.0
        else:
            p['on_kw'] = 0.1
    return p


def duty_circuit(key: str) -> dict | None:
    return next((d for d in circuits()['duty_cycle'] if d['key'] == key), None)


def reload() -> None:
    """Drop the cached config file and circuits.yaml (tests)."""
    _file.cache_clear()
    circuits.cache_clear()
    tz.cache_clear()
