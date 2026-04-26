"""
Hybrid forecasting + DRL trading adapter core.

This module intentionally keeps the implementation lightweight and self-contained:
- A small Transformer encoder forecasts next-hour log-return per symbol
- A Dueling Double-DQN agent decides portfolio exposure (cash / half / full)
- Execution is a simple cross-sectional basket: top-k symbols by forecast at each hour

It is designed to integrate with `training_pipeline.py` splits and reporting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import math
import random

import numpy as np

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
except Exception as e:  # pragma: no cover
    torch = None  # type: ignore
    nn = None  # type: ignore
    F = None  # type: ignore
    _TORCH_IMPORT_ERROR = e
else:
    _TORCH_IMPORT_ERROR = None


def _require_torch() -> None:
    if torch is None or nn is None or F is None:
        raise RuntimeError(
            "PyTorch is required for hybrid_tft_ddqn. "
            "Install it (e.g. `pip install torch`) or run using the project venv "
            f"(`.venv/bin/python`). Import error: {_TORCH_IMPORT_ERROR}"
        )


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _robust_zfit(x: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """
    RobustScaler-like fit: center by median, scale by IQR (p75 - p25).
    Returns (center, scale). Falls back safely when scale is tiny.
    """
    med = np.nanmedian(x, axis=0)
    q1 = np.nanpercentile(x, 25, axis=0)
    q3 = np.nanpercentile(x, 75, axis=0)
    iqr = (q3 - q1).astype(np.float32)
    iqr[iqr < 1e-8] = 1.0
    return med.astype(np.float32), iqr


def _robust_z(x: np.ndarray, center: np.ndarray, scale: np.ndarray) -> np.ndarray:
    return ((x - center) / scale).astype(np.float32)


def _log_returns(p: np.ndarray) -> np.ndarray:
    p = np.asarray(p, dtype=np.float64)
    p = np.maximum(p, 1e-12)
    r = np.zeros_like(p, dtype=np.float64)
    r[1:] = np.log(p[1:] / p[:-1])
    return r


def _rolling_mean_std(x: np.ndarray, w: int) -> Tuple[np.ndarray, np.ndarray]:
    """
    O(n) rolling mean/std with prefix sums; returns arrays same length as x.
    For indices < w-1, outputs nan.
    """
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    out_m = np.full(n, np.nan, dtype=np.float64)
    out_s = np.full(n, np.nan, dtype=np.float64)
    if w <= 1:
        out_m[:] = x
        out_s[:] = 0.0
        return out_m, out_s
    ps = np.cumsum(np.nan_to_num(x, nan=0.0), dtype=np.float64)
    ps2 = np.cumsum(np.nan_to_num(x * x, nan=0.0), dtype=np.float64)
    for i in range(w - 1, n):
        j0 = i - w + 1
        s1 = ps[i] - (ps[j0 - 1] if j0 > 0 else 0.0)
        s2 = ps2[i] - (ps2[j0 - 1] if j0 > 0 else 0.0)
        m = s1 / w
        v = max(0.0, (s2 / w) - m * m)
        out_m[i] = m
        out_s[i] = math.sqrt(v)
    return out_m, out_s


def build_features_from_ctx(
    ctx,
    symbols: Sequence[str],
    *,
    start: datetime,
    end: datetime,
    windows: Sequence[int] = (6, 24, 168),
    observed_times_only: bool = True,
) -> Tuple[List[datetime], np.ndarray, np.ndarray, List[str]]:
    """
    Build a per-time, per-symbol feature tensor and next-step return targets.

    Returns:
    - times: list[datetime] on the shared ref grid within [start, end]
    - X: shape (T, S, F)
    - y: shape (T, S) next-hour log return (at t+1) aligned to X[t]
    - feature_names
    """
    start = _utc(start)
    end = _utc(end)
    times_all: List[datetime] = list(ctx.ref_times or [])
    if not times_all:
        raise ValueError("ctx.ref_times is empty")

    i0 = 0
    while i0 < len(times_all) and _utc(times_all[i0]) < start:
        i0 += 1
    i1 = len(times_all) - 1
    while i1 >= 0 and _utc(times_all[i1]) > end:
        i1 -= 1
    if i1 <= i0 + 2:
        raise ValueError("Not enough points in requested window")

    times = [_utc(t) for t in times_all[i0 : i1 + 1]]
    syms = list(symbols)

    # Base series: forward-filled closes aligned to ref_times.
    closes = []
    for s in syms:
        arr = ctx.closes_ffill.get(s)
        if arr is None:
            raise ValueError(f"Missing closes_ffill for {s}")
        closes.append(np.asarray(arr[i0 : i1 + 1], dtype=np.float64))
    P = np.stack(closes, axis=1)  # (T, S)

    # Optional: drop hours with no observed bars in any symbol (forward-filled flat hours).
    # This is important because many assets (equities/ETFs) do not trade 24/7; keeping all
    # hours creates long runs of zero returns that swamp learning and produce 0.0 legs.
    if observed_times_only:
        observed_any = np.zeros(len(times), dtype=bool)
        tset_by_sym = []
        for s in syms:
            ser = ctx.cache.get(s) or []
            tset = { _utc(t) for (t, _px) in ser if _utc(t) >= start and _utc(t) <= end }
            tset_by_sym.append(tset)
        for i, t in enumerate(times):
            if any(t in ts for ts in tset_by_sym):
                observed_any[i] = True
        # Keep only observed hours; require at least 2 points.
        keep_idx = np.where(observed_any)[0]
        if len(keep_idx) >= 2:
            times = [times[i] for i in keep_idx]
            P = P[keep_idx, :]

    # Targets: next-hour log return.
    R = np.zeros_like(P, dtype=np.float64)
    for j in range(P.shape[1]):
        R[:, j] = _log_returns(P[:, j])
    y = np.full((len(times), len(syms)), np.nan, dtype=np.float32)
    y[:-1, :] = R[1:, :].astype(np.float32)

    # Features per symbol.
    feat_list: List[np.ndarray] = []
    feat_names: List[str] = []

    # Raw returns at t.
    feat_list.append(R.astype(np.float32))
    feat_names.append("log_ret_1h")

    # Rolling stats on returns.
    for w in windows:
        m = np.full_like(R, np.nan, dtype=np.float64)
        s = np.full_like(R, np.nan, dtype=np.float64)
        for j in range(R.shape[1]):
            mj, sj = _rolling_mean_std(R[:, j], int(w))
            m[:, j] = mj
            s[:, j] = sj
        feat_list.append(m.astype(np.float32))
        feat_names.append(f"log_ret_mean_{w}h")
        feat_list.append(s.astype(np.float32))
        feat_names.append(f"log_ret_std_{w}h")

    # Time features (cyclical): hour of day, day of week.
    hod = np.array([t.hour for t in times], dtype=np.float32)
    dow = np.array([t.weekday() for t in times], dtype=np.float32)
    hod_sin = np.sin(2 * np.pi * hod / 24.0).astype(np.float32)
    hod_cos = np.cos(2 * np.pi * hod / 24.0).astype(np.float32)
    dow_sin = np.sin(2 * np.pi * dow / 7.0).astype(np.float32)
    dow_cos = np.cos(2 * np.pi * dow / 7.0).astype(np.float32)
    # Broadcast to symbols.
    for name, vec in (
        ("hod_sin", hod_sin),
        ("hod_cos", hod_cos),
        ("dow_sin", dow_sin),
        ("dow_cos", dow_cos),
    ):
        feat_list.append(np.repeat(vec[:, None], len(syms), axis=1))
        feat_names.append(name)

    # Stack into (T, S, F)
    X = np.stack(feat_list, axis=2).astype(np.float32)
    # Make model inputs always finite; early rolling windows produce NaNs by design.
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
    y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
    return times, X, y, feat_names


if nn is not None:

    class SmallTransformerForecaster(nn.Module):
        """
        Per-symbol sequence model: consumes last L steps of features and predicts next-step return.

        Input: (B, L, F)
        Output: (B,) predicted return
        """

        def __init__(
            self, *, n_features: int, d_model: int = 64, nhead: int = 4, nlayers: int = 2, dropout: float = 0.1
        ):
            super().__init__()
            self.inp = nn.Linear(n_features, d_model)
            enc_layer = nn.TransformerEncoderLayer(
                d_model=d_model,
                nhead=nhead,
                dim_feedforward=4 * d_model,
                dropout=dropout,
                batch_first=True,
                activation="gelu",
                norm_first=True,
            )
            self.enc = nn.TransformerEncoder(enc_layer, num_layers=nlayers)
            self.out = nn.Linear(d_model, 1)

        def forward(self, x: "torch.Tensor") -> "torch.Tensor":
            h = self.inp(x)
            h = self.enc(h)
            # Use last token representation.
            z = h[:, -1, :]
            return self.out(z).squeeze(-1)


    class DuelingQNet(nn.Module):
        def __init__(self, *, in_dim: int, n_actions: int, hidden: int = 128):
            super().__init__()
            self.fc1 = nn.Linear(in_dim, hidden)
            self.fc2 = nn.Linear(hidden, hidden)
            self.val = nn.Linear(hidden, 1)
            self.adv = nn.Linear(hidden, n_actions)

        def forward(self, x: "torch.Tensor") -> "torch.Tensor":
            h = F.gelu(self.fc1(x))
            h = F.gelu(self.fc2(h))
            v = self.val(h)
            a = self.adv(h)
            # Q = V + (A - mean(A))
            return v + (a - a.mean(dim=1, keepdim=True))

else:

    class SmallTransformerForecaster:  # type: ignore
        def __init__(self, *args, **kwargs):
            _require_torch()

    class DuelingQNet:  # type: ignore
        def __init__(self, *args, **kwargs):
            _require_torch()


@dataclass
class Transition:
    s: np.ndarray
    a: int
    r: float
    s2: np.ndarray
    done: bool


class ReplayBuffer:
    def __init__(self, capacity: int = 200_000):
        self.capacity = int(capacity)
        self.buf: List[Transition] = []
        self.i = 0

    def add(self, tr: Transition) -> None:
        if len(self.buf) < self.capacity:
            self.buf.append(tr)
        else:
            self.buf[self.i] = tr
        self.i = (self.i + 1) % self.capacity

    def sample(self, batch: int, rng: random.Random) -> List[Transition]:
        batch = min(int(batch), len(self.buf))
        idx = [rng.randrange(0, len(self.buf)) for _ in range(batch)]
        return [self.buf[i] for i in idx]

    def __len__(self) -> int:
        return len(self.buf)


def _equity_step(equity: float, basket_ret: float, exposure: float, cost_bps: float) -> float:
    """
    Simple equity update with transaction cost proxy.
    - basket_ret is log-return approx; we apply exp for multiplicative update.
    - cost is applied proportional to exposure changes elsewhere; here we treat as constant per-step bps.
    """
    equity *= float(math.exp(exposure * float(basket_ret)))
    # Cost is applied outside based on turnover; kept here for backward compat.
    if cost_bps and float(cost_bps) > 0:
        equity *= float(1.0 - float(cost_bps) * 1e-4)
    return float(equity)


def _max_drawdown(equity_curve: Sequence[float]) -> float:
    peak = -1e9
    mdd = 0.0
    for e in equity_curve:
        peak = max(peak, float(e))
        if peak > 0:
            mdd = max(mdd, (peak - float(e)) / peak)
    return float(mdd)


def train_forecaster(
    X: np.ndarray,
    y: np.ndarray,
    *,
    lookback: int,
    train_idx: np.ndarray,
    val_idx: np.ndarray,
    seed: int,
    d_model: int = 64,
    nhead: int = 4,
    nlayers: int = 2,
    dropout: float = 0.1,
    lr: float = 3e-4,
    batch_size: int = 256,
    max_epochs: int = 10,
    patience: int = 2,
    device: str = "cpu",
) -> Dict[str, object]:
    _require_torch()

    rng = np.random.default_rng(int(seed))
    T, S, Fdim = X.shape

    def make_samples(t_idx: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        xs = []
        ys = []
        for t in t_idx:
            if t < lookback or t >= T - 1:
                continue
            xw = X[t - lookback + 1 : t + 1, :, :]  # (L, S, F)
            yt = y[t, :]  # (S,)
            if not np.isfinite(xw).all():
                continue
            if not np.isfinite(yt).all():
                continue
            xs.append(xw)
            ys.append(yt)
        if not xs:
            raise ValueError("No samples after filtering; consider lowering lookback/min_points.")
        Xs = np.stack(xs, axis=0)  # (N, L, S, F)
        Ys = np.stack(ys, axis=0)  # (N, S)
        # Flatten across symbols: (N*S, L, F) -> predict return per symbol.
        Xf = Xs.reshape((-1, lookback, Fdim))
        Yf = Ys.reshape((-1,))
        return Xf.astype(np.float32), Yf.astype(np.float32)

    Xtr, ytr = make_samples(train_idx)
    Xva, yva = make_samples(val_idx)

    model = SmallTransformerForecaster(n_features=Fdim, d_model=d_model, nhead=nhead, nlayers=nlayers, dropout=dropout).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=float(lr))

    def run_epoch(Xb: np.ndarray, yb: np.ndarray, train: bool) -> float:
        model.train(train)
        idx = np.arange(len(Xb))
        if train:
            rng.shuffle(idx)
        losses = []
        for i0 in range(0, len(idx), int(batch_size)):
            part = idx[i0 : i0 + int(batch_size)]
            xb = torch.tensor(Xb[part], device=device)
            yt = torch.tensor(yb[part], device=device)
            pred = model(xb)
            loss = F.smooth_l1_loss(pred, yt)
            if train:
                opt.zero_grad(set_to_none=True)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
            losses.append(float(loss.detach().cpu().item()))
        return float(np.mean(losses)) if losses else float("inf")

    best = float("inf")
    best_state = None
    bad = 0
    hist = {"train_loss": [], "val_loss": []}
    for _ep in range(int(max_epochs)):
        tl = run_epoch(Xtr, ytr, train=True)
        vl = run_epoch(Xva, yva, train=False)
        hist["train_loss"].append(tl)
        hist["val_loss"].append(vl)
        if vl < best - 1e-6:
            best = vl
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            bad = 0
        else:
            bad += 1
            if bad >= int(patience):
                break
    if best_state is not None:
        model.load_state_dict(best_state)

    return {"model": model, "history": hist, "best_val_loss": float(best)}


def forecasts_from_model(
    model: SmallTransformerForecaster,
    X: np.ndarray,
    *,
    lookback: int,
    idx: np.ndarray,
    device: str = "cpu",
) -> np.ndarray:
    _require_torch()

    model.eval()
    T, S, Fdim = X.shape
    out = np.full((T, S), np.nan, dtype=np.float32)
    with torch.no_grad():
        for t in idx:
            if t < lookback or t >= T - 1:
                continue
            xw = X[t - lookback + 1 : t + 1, :, :]  # (L, S, F)
            if not np.isfinite(xw).all():
                xw = np.nan_to_num(xw, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
            Xf = xw.transpose(1, 0, 2).reshape((S, lookback, Fdim))  # (S, L, F)
            pred = model(torch.tensor(Xf, device=device)).detach().cpu().numpy().astype(np.float32)
            out[t, :] = pred
    return out


def train_ddqn_exposure_agent(
    forecasts: np.ndarray,
    realized_next_ret: np.ndarray,
    *,
    train_idx: np.ndarray,
    val_idx: np.ndarray,
    top_k: int,
    seed: int,
    cost_bps: float = 0.5,
    dd_penalty: float = 0.5,
    gamma: float = 0.995,
    lr: float = 2e-4,
    batch_size: int = 256,
    warmup_steps: int = 2000,
    train_steps: int = 25_000,
    target_update: int = 500,
    eps_start: float = 1.0,
    eps_end: float = 0.05,
    eps_decay_steps: int = 20_000,
    device: str = "cpu",
) -> Dict[str, object]:
    _require_torch()

    rng = random.Random(int(seed))
    np_rng = np.random.default_rng(int(seed))

    actions = np.array([0.0, 0.5, 1.0], dtype=np.float32)
    n_actions = len(actions)

    def state_at(t: int, equity: float, peak: float, prev_expo: float) -> np.ndarray:
        f = forecasts[t, :]
        # Summary stats of cross-sectional forecasts.
        finite = np.isfinite(f)
        if not finite.any():
            mx = mn = mu = sd = 0.0
        else:
            ff = f[finite].astype(np.float64)
            mx = float(np.max(ff))
            mn = float(np.min(ff))
            mu = float(np.mean(ff))
            sd = float(np.std(ff))
        dd = 0.0
        if peak > 0:
            dd = float((peak - equity) / peak)
        return np.array([mx, mn, mu, sd, float(dd), float(prev_expo)], dtype=np.float32)

    in_dim = 6
    q = DuelingQNet(in_dim=in_dim, n_actions=n_actions).to(device)
    q_t = DuelingQNet(in_dim=in_dim, n_actions=n_actions).to(device)
    q_t.load_state_dict(q.state_dict())
    opt = torch.optim.AdamW(q.parameters(), lr=float(lr))

    rb = ReplayBuffer(capacity=200_000)

    # Create an index sequence we can iterate in-order (time-series).
    tr_seq = np.array([t for t in train_idx if 0 <= t < forecasts.shape[0] - 2], dtype=int)
    tr_seq.sort()
    va_seq = np.array([t for t in val_idx if 0 <= t < forecasts.shape[0] - 2], dtype=int)
    va_seq.sort()

    def basket_ret_at(t: int) -> float:
        f = forecasts[t, :]
        r_next = realized_next_ret[t, :]
        finite = np.isfinite(f) & np.isfinite(r_next)
        if finite.sum() < 1:
            return 0.0
        idx = np.argsort(f[finite])[::-1]
        chosen = np.where(finite)[0][idx[: int(top_k)]]
        if len(chosen) == 0:
            return 0.0
        return float(np.mean(r_next[chosen].astype(np.float64)))

    def eval_policy(seq: np.ndarray) -> Dict[str, float]:
        equity = 1.0
        peak = 1.0
        prev_expo = 0.0
        curve = []
        for t in seq:
            s = state_at(int(t), equity, peak, prev_expo)
            with torch.no_grad():
                qa = q(torch.tensor(s[None, :], device=device)).detach().cpu().numpy()[0]
            a = int(np.argmax(qa))
            expo = float(actions[a])
            bret = basket_ret_at(int(t))
            turnover = abs(float(expo) - float(prev_expo))
            eq2 = equity * float(math.exp(expo * float(bret)))
            if cost_bps and float(cost_bps) > 0 and turnover > 0:
                eq2 *= float(1.0 - float(cost_bps) * 1e-4 * float(turnover))
            equity = float(eq2)
            peak = max(peak, equity)
            prev_expo = expo
            curve.append(equity)
        mdd = _max_drawdown(curve) if curve else 0.0
        tot = float(curve[-1] / curve[0] - 1.0) if len(curve) >= 2 else 0.0
        return {"val_total_return": tot, "val_max_drawdown": float(mdd)}

    steps = 0
    t_ptr = 0
    equity = 1.0
    peak = 1.0
    prev_expo = 0.0

    best_score = -1e9
    best_state = None
    history = {"val_total_return": [], "val_max_drawdown": []}

    def eps_at(k: int) -> float:
        if k >= int(eps_decay_steps):
            return float(eps_end)
        a = float(k) / float(max(1, int(eps_decay_steps)))
        return float(eps_start + a * (eps_end - eps_start))

    while steps < int(train_steps):
        if t_ptr >= len(tr_seq):
            t_ptr = 0
            equity = 1.0
            peak = 1.0
            prev_expo = 0.0
        t = int(tr_seq[t_ptr])
        t_ptr += 1

        s = state_at(t, equity, peak, prev_expo)
        eps = eps_at(steps)
        if rng.random() < eps:
            a = rng.randrange(0, n_actions)
        else:
            with torch.no_grad():
                qa = q(torch.tensor(s[None, :], device=device)).detach().cpu().numpy()[0]
            a = int(np.argmax(qa))

        expo = float(actions[a])
        bret = basket_ret_at(t)
        turnover = abs(float(expo) - float(prev_expo))
        equity2 = equity * float(math.exp(expo * float(bret)))
        if cost_bps and float(cost_bps) > 0 and turnover > 0:
            equity2 *= float(1.0 - float(cost_bps) * 1e-4 * float(turnover))
        equity2 = float(equity2)
        peak2 = max(peak, equity2)
        dd2 = float((peak2 - equity2) / peak2) if peak2 > 0 else 0.0

        # Reward: PnL (equity change) minus drawdown penalty.
        r = float((equity2 / equity) - 1.0) - float(dd_penalty) * float(dd2)

        done = bool(t_ptr >= len(tr_seq))
        s2 = state_at(t + 1, equity2, peak2, expo)
        rb.add(Transition(s=s, a=int(a), r=float(r), s2=s2, done=done))

        equity, peak, prev_expo = equity2, peak2, expo

        if len(rb) >= int(warmup_steps):
            batch = rb.sample(int(batch_size), rng)
            Sb = torch.tensor(np.stack([tr.s for tr in batch], axis=0), device=device)
            Ab = torch.tensor([tr.a for tr in batch], device=device, dtype=torch.int64)
            Rb = torch.tensor([tr.r for tr in batch], device=device, dtype=torch.float32)
            S2b = torch.tensor(np.stack([tr.s2 for tr in batch], axis=0), device=device)
            Db = torch.tensor([1.0 if tr.done else 0.0 for tr in batch], device=device, dtype=torch.float32)

            with torch.no_grad():
                # Double DQN: action from online net, value from target net.
                a2 = torch.argmax(q(S2b), dim=1)
                q2 = q_t(S2b).gather(1, a2[:, None]).squeeze(1)
                yb = Rb + float(gamma) * (1.0 - Db) * q2

            qb = q(Sb).gather(1, Ab[:, None]).squeeze(1)
            loss = F.smooth_l1_loss(qb, yb)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(q.parameters(), 1.0)
            opt.step()

        if steps % int(target_update) == 0 and steps > 0:
            q_t.load_state_dict(q.state_dict())

        # Periodic validation
        if steps % 2000 == 0 and steps > 0 and len(va_seq) > 0:
            ev = eval_policy(va_seq)
            history["val_total_return"].append(float(ev["val_total_return"]))
            history["val_max_drawdown"].append(float(ev["val_max_drawdown"]))
            score = float(ev["val_total_return"]) - 0.5 * float(ev["val_max_drawdown"])
            if score > best_score:
                best_score = score
                best_state = {k: v.detach().cpu().clone() for k, v in q.state_dict().items()}

        steps += 1

    if best_state is not None:
        q.load_state_dict(best_state)

    return {
        "q_net": q,
        "actions": actions,
        "history": history,
    }

