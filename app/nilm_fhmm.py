"""Factorial HMM disaggregation for mixed-circuit NILM.

Given a per-second power signal on a circuit and a set of "candidate
appliances" (each with an estimated on-power and frequency), find the
most likely sequence of on/off states per appliance over the window.

Model:
  - Each appliance k is a 2-state HMM (off=0 W, on=mu_k W).
  - The joint state at time t is the bit-vector s_t ∈ {0,1}^K.
  - Observation: y_t = sum_k s_t[k] * mu_k + N(0, sigma^2).
  - Transitions are factored: P(s_t | s_{t-1}) = prod_k P(s_t[k] | s_{t-1}[k]).

We discover candidate appliances by clustering Phase 1's events (already
done in app.queries.discovered_loads_for_circuit). Each cluster becomes
one candidate appliance with:
  - mu_k = cluster's mean amplitude
  - p_on→off = 1 / median_on_duration_samples
  - p_off→on = total_active_samples / total_idle_samples

Viterbi inference over the joint state space (2^K) is exact and fast for
K ≤ 6 — exactly the size we have on mixed kitchen / sub-panel / garage
circuits.

This is intentionally a small NumPy module; we don't pull in NILMTK or
hmmlearn. The math is ~50 lines; the rest is data wrangling.
"""

from __future__ import annotations


try:
    import numpy as np
except ImportError:  # pragma: no cover — handled at page render time
    np = None  # type: ignore[assignment]

from . import config
from .db import pool
from psycopg.rows import dict_row


# Max number of candidate appliances per circuit. 2^7 = 128 joint states
# starts to feel sluggish in pure Python; we cap at 6 → 64 states.
MAX_APPLIANCES = 6

# Per-sample observation noise. Standard deviation in watts. Tuned by eye —
# 3-s SPAN samples have ~10-30 W jitter on a quiet circuit, so 50 W is a
# conservative ceiling that doesn't over-constrain inference.
OBS_NOISE_W = 50.0


def fit_appliances_from_clusters(clusters: list[dict]) -> list[dict]:
    """Take Phase 1's cluster summaries and produce FHMM appliance params.

    Each appliance gets: name, mu_w, p_on (steady-state on probability),
    on_persistence (geometric mean on-duration in 3-s samples), and
    off_persistence (same for off). Falls back to mild priors if data
    is too sparse to estimate cleanly."""
    appliances = []
    for c in clusters[:MAX_APPLIANCES]:
        mu_w = float(c['avg_amplitude_w'])
        med_dur_min = float(c['median_duration_min'])
        # Convert minutes to 3-s samples (20 samples/min)
        on_samples = max(1.0, med_dur_min * 20.0)
        # off persistence: total inactive time / number of events.
        # Approximated from cluster count over window if data exists, else 600 s.
        count = max(1, int(c['count']))
        total_min = float(c['total_min'])
        # Total window minutes is unknown here, but the FHMM only needs
        # relative persistences. Use a heuristic: events per day = count / days
        # is not visible at this layer; instead use the ratio of on:off time.
        # The page passes us the explicit `window_total_samples`, so this
        # function just returns *ratios*; absolute transition probabilities
        # are set later in build_fhmm_params.
        off_samples = on_samples * 5.0  # safe prior: typically 5x more off than on
        if total_min > 0:
            # crude refinement: if cluster total minutes are known, assume
            # ~half the window otherwise tells us roughly the on:off ratio.
            pass
        appliances.append({
            'name':          f"{int(round(mu_w))} W " + (c['hypothesis'][:40]),
            'amplitude_bin_w': c['amplitude_bin_w'],
            'duration_label': c.get('duration_label', ''),
            'mu_w':          mu_w,
            'on_samples':    on_samples,
            'off_samples':   off_samples,
            'count':         count,
            'hypothesis':    c['hypothesis'],
        })
    return appliances


def build_transition_matrix(on_samples: float, off_samples: float) -> 'np.ndarray':
    """2x2 transition matrix [from][to] for one appliance.
    P(stay_off) = 1 - 1/off_samples
    P(stay_on)  = 1 - 1/on_samples
    """
    p_off_to_on = 1.0 / max(off_samples, 1.0)
    p_on_to_off = 1.0 / max(on_samples, 1.0)
    return np.array([
        [1.0 - p_off_to_on, p_off_to_on],     # from off
        [p_on_to_off,       1.0 - p_on_to_off],  # from on
    ])


def viterbi_fhmm(observations: 'np.ndarray',
                  appliances: list[dict],
                  obs_noise_w: float = OBS_NOISE_W) -> 'np.ndarray':
    """Run Viterbi over the joint state space of a factorial HMM.

    observations: shape (T,), power in watts.
    appliances:   list of {mu_w, on_samples, off_samples}.
    Returns: shape (T, K) binary array — state of each appliance at each t."""
    K = len(appliances)
    T = len(observations)
    if K == 0 or T == 0:
        return np.zeros((T, max(K, 0)), dtype=np.int8)
    if K > MAX_APPLIANCES:
        raise ValueError(f'too many appliances ({K} > {MAX_APPLIANCES})')

    n_states = 1 << K  # 2^K joint states
    mu = np.array([a['mu_w'] for a in appliances])
    state_bits = np.array([
        [(s >> k) & 1 for k in range(K)]
        for s in range(n_states)
    ], dtype=np.int8)
    # Predicted observation for each joint state = sum of on appliances' mu_w.
    state_pred = state_bits @ mu  # (n_states,)

    # Pre-compute transition log-probs per appliance, then aggregate.
    log_trans_per_app = [
        np.log(np.clip(build_transition_matrix(
            a['on_samples'], a['off_samples']), 1e-12, 1.0))
        for a in appliances
    ]
    # Joint-state transitions: log P(s'→s) = sum_k log P(s'_k → s_k).
    # We pre-build the full (n_states × n_states) matrix once.
    log_trans = np.zeros((n_states, n_states))
    for s_prev in range(n_states):
        for s_curr in range(n_states):
            lp = 0.0
            for k in range(K):
                lp += log_trans_per_app[k][
                    (s_prev >> k) & 1, (s_curr >> k) & 1]
            log_trans[s_prev, s_curr] = lp

    # Emission log-prob: Gaussian centered at state_pred[s] with sigma=obs_noise_w.
    # log_emit(t, s) = -0.5 * ((y_t - mu_s) / sigma)^2 + const_we_drop
    inv_two_sigma2 = 1.0 / (2.0 * obs_noise_w * obs_noise_w)
    # log_emit shape (T, n_states)
    diffs = observations[:, None] - state_pred[None, :]
    log_emit = -(diffs * diffs) * inv_two_sigma2

    # Initial state: assume all off
    log_init = np.full(n_states, -np.inf)
    log_init[0] = 0.0

    # Viterbi forward
    delta = log_init + log_emit[0]
    psi = np.zeros((T, n_states), dtype=np.int32)
    for t in range(1, T):
        # delta_new[s] = max_prev (delta[prev] + log_trans[prev, s]) + log_emit[t, s]
        candidate = delta[:, None] + log_trans  # (n_states, n_states)
        psi[t] = np.argmax(candidate, axis=0)
        delta = candidate[psi[t], np.arange(n_states)] + log_emit[t]

    # Backtrack
    best_path = np.zeros(T, dtype=np.int32)
    best_path[-1] = int(np.argmax(delta))
    for t in range(T - 2, -1, -1):
        best_path[t] = psi[t + 1, best_path[t + 1]]

    # Decompose joint state into per-appliance bits
    return state_bits[best_path]  # (T, K)


def attribute_to_appliances(observations: 'np.ndarray',
                             state_path: 'np.ndarray',
                             appliances: list[dict],
                             sample_period_s: float = 3.0) -> dict:
    """Given the inferred state path, compute per-appliance kWh attribution.

    For each appliance k:
      on_seconds_k = count of t where state_path[t, k] == 1, × sample_period
      kwh_k = mu_k * on_seconds_k / 3_600_000

    "Other" bucket captures the residual = observed_kwh - sum(attributed).
    """
    K = state_path.shape[1] if state_path.ndim == 2 else 0
    T = len(observations)
    total_kwh = float(observations.sum() * sample_period_s / 3_600_000.0)
    rows = []
    attributed_kwh = 0.0
    for k in range(K):
        on_count = int(state_path[:, k].sum())
        on_seconds = on_count * sample_period_s
        kwh = (appliances[k]['mu_w'] * on_seconds) / 3_600_000.0
        attributed_kwh += kwh
        rows.append({
            'name':       appliances[k]['name'],
            'mu_w':       appliances[k]['mu_w'],
            'hypothesis': appliances[k]['hypothesis'],
            'on_seconds': on_seconds,
            'on_minutes': on_seconds / 60.0,
            'kwh':        kwh,
            'share_pct':  (kwh / total_kwh * 100.0) if total_kwh > 0 else 0.0,
            'duration_label': appliances[k].get('duration_label', ''),
            'amplitude_bin_w': appliances[k]['amplitude_bin_w'],
        })
    residual_kwh = max(0.0, total_kwh - attributed_kwh)
    rows.sort(key=lambda r: r['kwh'], reverse=True)
    return {
        'total_kwh':        total_kwh,
        'attributed_kwh':   attributed_kwh,
        'residual_kwh':     residual_kwh,
        'residual_share_pct': (residual_kwh / total_kwh * 100.0) if total_kwh > 0 else 0.0,
        'rows':             rows,
        'sample_period_s':  sample_period_s,
        'total_samples':    T,
    }


def fhmm_attribute_circuit(space_id: str, hours: int = 24,
                            min_step_w: float = 200.0,
                            obs_noise_w: float = OBS_NOISE_W) -> dict:
    """End-to-end: pull space_live samples for one circuit + recent window,
    discover candidate appliances from Phase 1 clusters, run Viterbi, and
    return the per-appliance kWh attribution.

    Returns a dict with appliances, attribution rows, and the inferred
    state timeline (subsampled to ~600 points for charting).
    """
    if np is None:
        return {
            'error': 'NumPy not installed — pip install numpy in the LXC venv.',
            'space_id': space_id,
        }
    # Import here to avoid circular import at module load.
    from .queries import discovered_loads_for_circuit

    # Use a slightly longer window for cluster discovery than for attribution.
    # 7 days gives stable cluster mu_w estimates even if the attribution
    # window is only the last 24 h.
    discovery_days = max(int(hours / 24), 7)
    clusters_detail = discovered_loads_for_circuit(
        space_id, days=discovery_days, min_step_w=min_step_w)

    # Pull samples for the attribution window
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT name FROM spaces WHERE space_id = %s;", (space_id,))
        sp = cur.fetchone()
        if sp:
            space_name = sp['name']
        elif space_id == '__feedthrough__':
            space_name = config.circuits()['feedthrough_name']
        else:
            space_name = space_id
        cur.execute(
            """
            SELECT ts, power_w
            FROM space_live
            WHERE space_id = %s
              AND ts >= NOW() - %s::interval
            ORDER BY ts;
            """,
            (space_id, f'{hours} hours'),
        )
        rows = cur.fetchall()
        # While the connection is still open, also check whether this
        # space_id has EVER appeared in space_live. The Sub-panel real
        # space (`b7755…`) is in `spaces` but not exposed by SPAN's local
        # API at the circuit level — only the feedthrough aggregate is.
        # We need this signal to surface a precise error message below.
        if not rows:
            cur.execute(
                "SELECT 1 FROM space_live WHERE space_id = %s LIMIT 1;",
                (space_id,),
            )
            has_ever_been_written = cur.fetchone() is not None

    if not rows:
        if not has_ever_been_written and space_id != '__feedthrough__':
            err = (f"'{space_name}' isn't exposed by SPAN's local API at the "
                   f"per-circuit level — the panel only meters the feedthrough "
                   f"aggregate for sub-panel circuits. Pick a different circuit, "
                   f"or use 'Sub panel - Under sink & upstairs & attic & smokes' for the lug total.")
        else:
            err = (f"No space_live samples in the last {hours} h. The poller "
                   f"writes one row per circuit every 3 s; check /health if "
                   f"this persists.")
        return {
            'error': err,
            'space_id': space_id, 'space_name': space_name,
        }

    timestamps = [r['ts'] for r in rows]
    observations = np.array(
        [float(r['power_w'] or 0.0) for r in rows], dtype=np.float64)
    sample_period_s = 3.0
    if len(timestamps) > 1:
        # Estimate actual sample spacing — if the poller hiccuped, this
        # might be > 3 s on average. Used only for kWh integration.
        dt = (timestamps[-1] - timestamps[0]).total_seconds() / max(len(timestamps) - 1, 1)
        if 1.0 < dt < 10.0:
            sample_period_s = dt

    appliances = fit_appliances_from_clusters(clusters_detail['clusters'])
    if not appliances:
        return {
            'error': 'No Phase-1 clusters discovered yet on this circuit. The detector needs at least a handful of repeating events.',
            'space_id': space_id, 'space_name': space_name,
            'sample_count': len(observations),
        }

    # Refine off_samples now that we know the actual window size.
    # off_samples per appliance ≈ (T - count * on_samples) / count
    T = len(observations)
    for a in appliances:
        total_on = a['count'] * a['on_samples']
        off_total = max(T - total_on, a['count'])
        a['off_samples'] = max(off_total / max(a['count'], 1), 10.0)

    state_path = viterbi_fhmm(observations, appliances,
                               obs_noise_w=obs_noise_w)
    attribution = attribute_to_appliances(
        observations, state_path, appliances,
        sample_period_s=sample_period_s)

    # Subsample state_path for charting (~600 points max)
    chart_n = min(600, T)
    stride = max(1, T // chart_n)
    chart_times    = [timestamps[i].isoformat() for i in range(0, T, stride)]
    chart_observed = [float(observations[i]) for i in range(0, T, stride)]
    chart_states = []
    for k in range(len(appliances)):
        chart_states.append({
            'name': appliances[k]['name'],
            'mu_w': appliances[k]['mu_w'],
            'series': [int(state_path[i, k]) for i in range(0, T, stride)],
        })

    return {
        'space_id':       space_id,
        'space_name':     space_name,
        'hours':          hours,
        'sample_count':   T,
        'sample_period_s': sample_period_s,
        'appliances':     appliances,
        'attribution':    attribution,
        'chart': {
            'ts':       chart_times,
            'observed': chart_observed,
            'states':   chart_states,
        },
    }
