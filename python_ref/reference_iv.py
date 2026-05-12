"""
Reference implied-volatility pipeline.

Stage 1: load CBOE CSV, filter, merge with T-bill rates and SPY dividend.
Stage 2: compute reference IV with SciPy (Brent) and with a Python bisection
         that exactly mirrors the bit-by-bit logic of the FPGA HLS kernel.
Stage 3: emit two binary blobs:
            - input.bin  : per-option struct read by the host driver and DMA'd to PL
            - golden.bin : per-option reference sigma to compare FPGA output against

Run from project root:
    python python_ref/reference_iv.py \
        --csv /path/to/CBOE_sample_20160502-20160506.csv \
        --rate 0.0022 \
        --div  0.0213 \
        --out  ./build
"""
from __future__ import annotations
import argparse
import struct
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm
from scipy.optimize import brentq


# -------------------------------------------------------------- Black-Scholes
def bs_price(S: float, K: float, T: float, r: float, q: float,
             sigma: float, is_call: bool) -> float:
    """Standard BS with continuous dividend yield q. Equations 1-4 of paper."""
    if sigma <= 0.0 or T <= 0.0:
        # degenerate cases — return intrinsic
        intrinsic = max(S - K, 0.0) if is_call else max(K - S, 0.0)
        return intrinsic
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if is_call:
        return S * np.exp(-q * T) * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    else:
        return K * np.exp(-r * T) * norm.cdf(-d2) - S * np.exp(-q * T) * norm.cdf(-d1)


def iv_brent(S, K, T, r, q, market_price, is_call,
             lo: float = 1e-4, hi: float = 5.0, tol: float = 1e-6) -> float:
    """High-precision reference using Brent's method on the loss function."""
    f = lambda sig: bs_price(S, K, T, r, q, sig, is_call) - market_price
    try:
        f_lo = f(lo)
        f_hi = f(hi)
        if f_lo * f_hi > 0:
            return float('nan')  # no sign change — no root in bracket
        return brentq(f, lo, hi, xtol=tol, maxiter=100)
    except (ValueError, RuntimeError):
        return float('nan')


def iv_multisection(S, K, T, r, q, market_price, is_call,
                    n_sections: int = 8, threshold: float = 1e-4,
                    lo: float = 0.01, hi: float = 2.99,
                    max_iter: int = 64) -> tuple[float, int]:
    """
    Mirrors Algorithm 2 from the paper exactly. Returns (sigma, iter_count).
    The FPGA HLS kernel implements this same logic — bit-equivalent at float
    precision modulo erf approximation.
    """
    f = lambda sig: bs_price(S, K, T, r, q, sig, is_call) - market_price

    sigma_lo, sigma_hi = lo, hi
    f_lo, f_hi = f(sigma_lo), f(sigma_hi)
    if f_lo * f_hi > 0:
        return float('nan'), 0

    iters = 0
    while (sigma_hi - sigma_lo) > threshold and iters < max_iter:
        gap = (sigma_hi - sigma_lo) / n_sections
        # n_sections+1 evaluation points including endpoints
        sigmas = [sigma_lo + i * gap for i in range(n_sections + 1)]
        fs = [f(s) for s in sigmas]
        # find adjacent pair with sign change (lines 7-13 of Algorithm 2)
        for i in range(n_sections):
            if fs[i] * fs[i + 1] <= 0:
                sigma_lo, sigma_hi = sigmas[i], sigmas[i + 1]
                break
        iters += 1

    # pick endpoint with smaller |loss| (lines 15-19)
    if abs(f(sigma_lo)) <= abs(f(sigma_hi)):
        return sigma_lo, iters
    return sigma_hi, iters


# ---------------------------------------------------------- CSV preprocessing
def parse_quote_datetime(s: str) -> datetime:
    """'02MAY16:09:35:00' -> datetime."""
    return datetime.strptime(s, "%d%b%y:%H:%M:%S")


def load_and_clean(csv_path: Path, max_rows: int | None = None) -> pd.DataFrame:
    print(f"[load] reading {csv_path} ...", file=sys.stderr)
    df = pd.read_csv(csv_path, nrows=max_rows)
    print(f"[load] {len(df):,} rows raw", file=sys.stderr)

    # SPY only. VIX/VXX need Black-76 (futures-based), out of scope.
    # SPX cash-settled with American-style differences — exclude to keep the
    # underlying universe homogeneous and the methodology defensible.
    df = df[df.underlying_symbol == 'SPY'].copy()

    # Parse times and dates
    df['quote_dt'] = df.quote_datetime.apply(parse_quote_datetime)
    df['expiry_dt'] = pd.to_datetime(df.expiration)

    # Time to expiry in years (calendar days / 365). Note: must use bracket
    # access — df.T is the DataFrame transpose attribute and shadows columns.
    df['ttm'] = (df.expiry_dt - df.quote_dt).dt.total_seconds() / (365.0 * 86400.0)

    # Reject expired/zero-time options (would-be intrinsic)
    df = df[df['ttm'] > 1.0 / 365.0]  # at least 1 day to expiry

    # Underlying mid-price
    df['S'] = 0.5 * (df.underlying_bid + df.underlying_ask)

    # Option mid-price preferred to last trade (less stale)
    df['mid'] = 0.5 * (df.best_bid + df.best_ask)

    # Reject crossed/locked quotes and zero-bid options (no signal)
    df = df[(df.best_bid > 0) & (df.best_ask >= df.best_bid) & (df.S > 0)]

    df['is_call'] = (df.option_type == 'C')

    # Reject deep ITM/OTM where mid-price is essentially intrinsic — IV is
    # numerically unstable there and would dominate the error stats
    intrinsic = np.where(df.is_call,
                         np.maximum(df.S - df.strike, 0),
                         np.maximum(df.strike - df.S, 0))
    df = df[df.mid > intrinsic + 0.01]  # at least 1 cent of time value

    print(f"[load] {len(df):,} rows after cleaning", file=sys.stderr)
    return df.reset_index(drop=True)


# ------------------------------------------- Hybrid CPU constants for the PL
def precompute_constants(row, r: float, q: float) -> dict:
    """
    Per Fig. 6 of the paper: CPU computes the transcendental constants once,
    FPGA receives them and only does add/mul/div/erf in the inner loop.
    """
    S, K, T = row.S, row.strike, row['ttm']
    is_call = bool(row.is_call)

    log_SK = float(np.log(S / K))
    sqrt_T = float(np.sqrt(T))
    disc_K = float(K * np.exp(-r * T))   # Ke^(-rT) — appears in price formula
    disc_S = float(S * np.exp(-q * T))   # Se^(-qT) — appears in price formula
    rqT = float((r - q) * T)             # appears in d1 numerator
    return dict(
        log_SK=log_SK, sqrt_T=sqrt_T, disc_K=disc_K, disc_S=disc_S, rqT=rqT,
        market_price=float(row.mid), is_call=is_call,
        S=float(S), K=float(K), T=float(T), r=float(r), q=float(q),
    )


# ------------------------------------------------------- Binary serialization
# Layout per option (must match host_driver/pynq_runner.py and HLS top-level):
#   float log_SK      offset 0
#   float sqrt_T      offset 4
#   float disc_K      offset 8
#   float disc_S      offset 12
#   float rqT         offset 16
#   float market_px   offset 20
#   uint32 is_call    offset 24   (1 = call, 0 = put)
#   uint32 _pad       offset 28   (alignment to 32B)
# = 32 bytes per option, AXI4-Stream-friendly.
OPTION_RECORD_FMT = "<6f I I"
assert struct.calcsize(OPTION_RECORD_FMT) == 32


def serialize(rows: list[dict], out_path: Path):
    with out_path.open("wb") as fh:
        for r in rows:
            fh.write(struct.pack(
                OPTION_RECORD_FMT,
                r['log_SK'], r['sqrt_T'], r['disc_K'], r['disc_S'], r['rqT'],
                r['market_price'], 1 if r['is_call'] else 0, 0,
            ))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', required=True, type=Path)
    ap.add_argument('--rate', type=float, default=0.0027,
                    help='Flat risk-free rate. May 2016 4-week T-bill ≈ 0.27%%')
    ap.add_argument('--div', type=float, default=0.0213,
                    help='Flat continuous dividend yield. SPY May 2016 ≈ 2.13%%')
    ap.add_argument('--max-rows', type=int, default=20000,
                    help='Cap dataset size for first-pass testing')
    ap.add_argument('--out', type=Path, default=Path('./build'))
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)

    df = load_and_clean(args.csv, max_rows=args.max_rows)

    print("[ref] computing reference IV with Brent + multi-section ...",
          file=sys.stderr)
    records = []
    iv_brent_list = []
    iv_ms_list = []
    iters_list = []
    for i, row in df.iterrows():
        c = precompute_constants(row, r=args.rate, q=args.div)
        iv_b = iv_brent(c['S'], c['K'], c['T'], args.rate, args.div,
                        c['market_price'], c['is_call'])
        iv_m, n_iter = iv_multisection(c['S'], c['K'], c['T'], args.rate,
                                        args.div, c['market_price'],
                                        c['is_call'])
        iv_brent_list.append(iv_b)
        iv_ms_list.append(iv_m)
        iters_list.append(n_iter)
        records.append(c)
        if (i + 1) % 5000 == 0:
            print(f"  ... {i + 1:,}/{len(df):,}", file=sys.stderr)

    df['iv_brent'] = iv_brent_list
    df['iv_multisection'] = iv_ms_list
    df['ms_iters'] = iters_list

    # Drop unconverged — paper §IV.B notes some options blow up when sigma → 0
    valid = df.iv_brent.notna() & df.iv_multisection.notna()
    print(f"[ref] {valid.sum():,}/{len(df):,} options converged on both methods",
          file=sys.stderr)

    # Sanity: multi-section vs Brent — should agree to within ~1e-4
    diff = (df.loc[valid, 'iv_multisection'] - df.loc[valid, 'iv_brent']).abs()
    print(f"[ref] multi-section vs Brent  max|diff| = {diff.max():.2e}  "
          f"mean|diff| = {diff.mean():.2e}", file=sys.stderr)
    print(f"[ref] iterations  mean = {df.loc[valid, 'ms_iters'].mean():.2f}  "
          f"max = {df.loc[valid, 'ms_iters'].max()}", file=sys.stderr)

    # Filter to converged set for FPGA
    valid_records = [records[i] for i in range(len(records)) if valid.iloc[i]]
    valid_golden = df.loc[valid, 'iv_multisection'].astype(np.float32).values

    # Write inputs and golden outputs
    serialize(valid_records, args.out / 'input.bin')
    valid_golden.tofile(args.out / 'golden.bin')

    # Audit CSV — easier to debug than two binary blobs
    df.loc[valid, ['underlying_symbol', 'quote_datetime', 'strike',
                   'option_type', 'ttm', 'S', 'mid',
                   'iv_brent', 'iv_multisection', 'ms_iters']].to_csv(
        args.out / 'reference.csv', index=False)

    print(f"[done] wrote {args.out / 'input.bin'} "
          f"({(args.out / 'input.bin').stat().st_size:,} B)", file=sys.stderr)
    print(f"[done] wrote {args.out / 'golden.bin'} "
          f"({(args.out / 'golden.bin').stat().st_size:,} B)", file=sys.stderr)
    print(f"[done] wrote {args.out / 'reference.csv'}", file=sys.stderr)


if __name__ == '__main__':
    main()
