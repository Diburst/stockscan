"""Black-Scholes option pricing + an analytic strike-from-delta solver.

Pure functions, no DB, no pandas — scalars in, scalars out. This is the
options-pricing math the analysis page leans on to translate a stock's
realized volatility into concrete, tradeable strike suggestions.

What lives here
---------------
  * :func:`d1`, :func:`d2` — the standard Black-Scholes auxiliary terms.
  * :func:`price` — European call/put fair value.
  * :func:`greeks` — Δ (delta), Θ (theta, per-calendar-day), 𝜈 (vega,
    per 1 vol point), Γ (gamma). Enough to frame a suggested strike for
    an options trader without dragging in a full greeks engine.
  * :func:`strike_for_delta` — the inverse problem: given a *target*
    delta, solve **analytically** for the strike that produces it. No
    root-finding loop — for a European option the delta-to-strike map
    inverts in closed form via the normal quantile (see the derivation
    in that function's docstring).
  * :func:`suggest_strike` — the convenience wrapper the analysis layer
    calls: takes a spot, an annualised vol *in percent*, days-to-expiry,
    a signed target delta, and returns a fully-populated
    :class:`StrikeQuote`.

Important modelling note
------------------------
We do **not** have an option chain, so there is no implied volatility to
feed the model. The analysis layer passes **realized** historical
volatility (the same 21-day HV that drives the expected-move bands) as a
proxy for IV. Black-Scholes itself is agnostic to where σ comes from;
the caller owns that approximation and the UI is labelled accordingly.

Conventions
-----------
  * ``sigma`` and ``r`` here are **decimals** (0.30 = 30% vol, 0.04 =
    4% rate), annualised. :func:`suggest_strike` accepts vol in *percent*
    for ergonomics at the call site and converts internally.
  * ``t`` is time to expiry in **years** (30 calendar days = 30/365).
  * ``kind`` is ``"call"`` or ``"put"`` throughout.
  * Call delta ∈ (0, 1); put delta ∈ (−1, 0). Pass the target delta with
    its natural sign (a 20-delta put is ``-0.20``); :func:`suggest_strike`
    is forgiving and applies the sign from ``kind`` if you pass the
    magnitude.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

# Standard normal — cdf N(x) and inverse cdf (quantile) N⁻¹(p). Stdlib,
# no scipy dependency. NormalDist().inv_cdf is the Acklam-grade quantile
# we need to invert delta → strike.
_N = NormalDist()
_DAYS_PER_YEAR = 365.0


def _norm_cdf(x: float) -> float:
    return _N.cdf(x)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF (quantile). Clamps p off the 0/1 edges."""
    eps = 1e-12
    p = min(1.0 - eps, max(eps, p))
    return _N.inv_cdf(p)


# ---------------------------------------------------------------------------
# Core Black-Scholes
# ---------------------------------------------------------------------------
def d1(s: float, k: float, t: float, r: float, sigma: float) -> float:
    """Black-Scholes d₁ = [ln(S/K) + (r + σ²/2)·t] / (σ·√t)."""
    return (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))


def d2(s: float, k: float, t: float, r: float, sigma: float) -> float:
    """Black-Scholes d₂ = d₁ − σ·√t."""
    return d1(s, k, t, r, sigma) - sigma * math.sqrt(t)


def price(s: float, k: float, t: float, r: float, sigma: float, kind: str) -> float:
    """European option fair value under Black-Scholes.

    Call = S·N(d₁) − K·e^(−r·t)·N(d₂);
    Put  = K·e^(−r·t)·N(−d₂) − S·N(−d₁).
    """
    _validate(s, k, t, sigma)
    _d1 = d1(s, k, t, r, sigma)
    _d2 = _d1 - sigma * math.sqrt(t)
    disc = math.exp(-r * t)
    if kind == "call":
        return s * _norm_cdf(_d1) - k * disc * _norm_cdf(_d2)
    if kind == "put":
        return k * disc * _norm_cdf(-_d2) - s * _norm_cdf(-_d1)
    raise ValueError(f"kind must be 'call' or 'put', got {kind!r}")


@dataclass(frozen=True, slots=True)
class Greeks:
    """The greeks subset we surface for a suggested strike.

    ``theta`` is per **calendar day** (the raw per-year theta divided by
    365) — that's the decay number a trader reads off a screen. ``vega``
    is per **one** volatility point (a 1% change in σ), i.e. raw vega / 100.
    """

    delta: float
    gamma: float
    theta: float  # per calendar day
    vega: float  # per 1 vol point (1%)


def greeks(s: float, k: float, t: float, r: float, sigma: float, kind: str) -> Greeks:
    """Δ, Γ, Θ (per calendar day), 𝜈 (per 1 vol point) for a European option."""
    _validate(s, k, t, sigma)
    sqrt_t = math.sqrt(t)
    _d1 = d1(s, k, t, r, sigma)
    _d2 = _d1 - sigma * sqrt_t
    pdf_d1 = _norm_pdf(_d1)
    disc = math.exp(-r * t)

    gamma = pdf_d1 / (s * sigma * sqrt_t)
    vega_raw = s * pdf_d1 * sqrt_t  # per 1.0 (100 vol points) change in σ

    if kind == "call":
        delta = _norm_cdf(_d1)
        theta_raw = -(s * pdf_d1 * sigma) / (2.0 * sqrt_t) - r * k * disc * _norm_cdf(_d2)
    elif kind == "put":
        delta = _norm_cdf(_d1) - 1.0
        theta_raw = -(s * pdf_d1 * sigma) / (2.0 * sqrt_t) + r * k * disc * _norm_cdf(-_d2)
    else:
        raise ValueError(f"kind must be 'call' or 'put', got {kind!r}")

    return Greeks(
        delta=delta,
        gamma=gamma,
        theta=theta_raw / _DAYS_PER_YEAR,
        vega=vega_raw / 100.0,
    )


def strike_for_delta(
    s: float,
    t: float,
    r: float,
    sigma: float,
    target_delta: float,
    kind: str,
) -> float:
    """Solve **analytically** for the strike with the given Black-Scholes delta.

    For a European option the delta is a strictly monotonic function of
    the strike, and it inverts in closed form — no Newton loop needed.

    Call: Δ = N(d₁)  ⇒  d₁ = N⁻¹(Δ).
    Put:  Δ = N(d₁) − 1  ⇒  d₁ = N⁻¹(Δ + 1)  (Δ is negative for a put).

    With d₁ pinned, invert the definition of d₁ for K:

        d₁ = [ln(S/K) + (r + σ²/2)·t] / (σ·√t)
        ⇒ ln(S/K) = d₁·σ·√t − (r + σ²/2)·t
        ⇒ K = S · exp( (r + σ²/2)·t − d₁·σ·√t )

    A 20-delta call lands **above** spot (OTM call), a −20-delta put lands
    **below** spot (OTM put) — exactly the short strikes a premium-seller
    or strangle-buyer reaches for.
    """
    _validate_inputs(s, t, sigma)
    if kind == "call":
        if not 0.0 < target_delta < 1.0:
            raise ValueError(f"call delta must be in (0, 1), got {target_delta}")
        d1_target = _norm_ppf(target_delta)
    elif kind == "put":
        if not -1.0 < target_delta < 0.0:
            raise ValueError(f"put delta must be in (-1, 0), got {target_delta}")
        d1_target = _norm_ppf(target_delta + 1.0)
    else:
        raise ValueError(f"kind must be 'call' or 'put', got {kind!r}")

    sqrt_t = math.sqrt(t)
    return s * math.exp((r + 0.5 * sigma * sigma) * t - d1_target * sigma * sqrt_t)


# ---------------------------------------------------------------------------
# High-level convenience wrapper
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class StrikeQuote:
    """A fully-resolved strike suggestion at a target delta + expiry.

    ``vol_pct`` and ``rate_pct`` record the assumptions that produced the
    quote so the UI can be honest about the model inputs. ``pct_otm`` is
    signed relative to spot (positive = above spot).
    """

    kind: str  # 'call' | 'put'
    target_delta: float  # signed, as requested
    days_to_expiry: int
    spot: float
    strike: float
    pct_otm: float  # signed % distance of strike from spot
    delta: float  # realised delta at the solved strike (≈ target)
    price: float  # BS fair value (per share)
    gamma: float
    theta: float  # per calendar day
    vega: float  # per 1 vol point
    vol_pct: float  # annualised vol used (percent)
    rate_pct: float  # annualised risk-free rate used (percent)


def suggest_strike(
    *,
    spot: float,
    vol_pct: float,
    days_to_expiry: int,
    target_delta: float,
    kind: str,
    rate: float = 0.04,
) -> StrikeQuote:
    """Suggest a strike at ``target_delta`` and ``days_to_expiry`` for ``spot``.

    ``vol_pct`` is the **annualised** volatility in *percent* (e.g. 28.5 for
    28.5%) — the analysis layer passes the 21-day realized HV here as an IV
    proxy. ``target_delta`` may be passed signed or as a magnitude; the sign
    is normalised from ``kind`` (calls positive, puts negative) so callers
    can't accidentally request an impossible delta.
    """
    if spot <= 0:
        raise ValueError("spot must be positive")
    if vol_pct <= 0:
        raise ValueError("vol_pct must be positive")
    if days_to_expiry <= 0:
        raise ValueError("days_to_expiry must be positive")

    sigma = vol_pct / 100.0
    t = days_to_expiry / _DAYS_PER_YEAR

    # Normalise the sign from kind so a caller passing 0.20 for a put still
    # gets the −0.20 put strike rather than a ValueError.
    mag = abs(target_delta)
    signed_delta = mag if kind == "call" else -mag

    k = strike_for_delta(spot, t, rate, sigma, signed_delta, kind)
    g = greeks(spot, k, t, rate, sigma, kind)
    px = price(spot, k, t, rate, sigma, kind)

    return StrikeQuote(
        kind=kind,
        target_delta=signed_delta,
        days_to_expiry=days_to_expiry,
        spot=round(spot, 4),
        strike=round(k, 2),
        pct_otm=round((k - spot) / spot * 100.0, 2),
        delta=round(g.delta, 4),
        price=round(px, 2),
        gamma=round(g.gamma, 6),
        theta=round(g.theta, 4),
        vega=round(g.vega, 4),
        vol_pct=round(vol_pct, 2),
        rate_pct=round(rate * 100.0, 2),
    )


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
def _validate_inputs(s: float, t: float, sigma: float) -> None:
    if s <= 0:
        raise ValueError("spot must be positive")
    if t <= 0:
        raise ValueError("time to expiry must be positive")
    if sigma <= 0:
        raise ValueError("sigma must be positive")


def _validate(s: float, k: float, t: float, sigma: float) -> None:
    _validate_inputs(s, t, sigma)
    if k <= 0:
        raise ValueError("strike must be positive")
