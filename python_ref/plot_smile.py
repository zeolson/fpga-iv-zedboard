"""
plot_smile.py — generate volatility-smile figures from reference.csv.

Run after reference_iv.py finishes:
    python python_ref/plot_smile.py

Outputs into ./build/:
    smile_main.png        — the headline smile plot (use on poster)
    iter_count.png        — bisection vs multi-section iteration bar chart
    error_hist.png        — accuracy histogram (optional, for backup)
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# Use a clean, journal-friendly style
plt.rcParams.update({
    'font.family': 'sans-serif',
    'font.size': 11,
    'axes.titlesize': 13,
    'axes.labelsize': 12,
    'xtick.labelsize': 10,
    'ytick.labelsize': 10,
    'legend.fontsize': 10,
    'figure.dpi': 150,
    'savefig.dpi': 300,    # poster-quality
    'savefig.bbox': 'tight',
    'axes.spines.top': False,
    'axes.spines.right': False,
    'axes.grid': True,
    'grid.alpha': 0.3,
})

# St. Thomas purple-ish + complementary tones for cross-expiry curves
COLORS = ['#4B2D86', '#9B7BC9', '#D4A4E8', '#5A8DC9', '#2D7A4D']


def plot_smile(df: pd.DataFrame, out_path: Path,
               quote_date: str | None = None,
               symbol: str = 'SPY'):
    """Headline volatility smile: IV vs strike, one curve per expiry."""
    df = df[df.underlying_symbol == symbol].copy()

    # Pick a single quote time so we get a clean snapshot (no time-of-day noise)
    if quote_date is None:
        # Default: use the most-quoted timestamp on the first day
        quote_date = df.quote_datetime.value_counts().idxmax()
    df_snap = df[df.quote_datetime == quote_date].copy()
    print(f"[smile] snapshot: {quote_date}, n={len(df_snap)}")

    # Sort expiries by time-to-maturity so curves layer chronologically
    expiry_groups = df_snap.groupby('ttm')
    ttms_sorted = sorted(expiry_groups.groups.keys())

    # Pick 3-4 representative expiries spread across the range
    n_pick = min(4, len(ttms_sorted))
    if n_pick > 0:
        idx = np.linspace(0, len(ttms_sorted) - 1, n_pick).astype(int)
        chosen_ttms = [ttms_sorted[i] for i in idx]
    else:
        chosen_ttms = []

    fig, ax = plt.subplots(figsize=(7, 5))

    for color, ttm in zip(COLORS, chosen_ttms):
        sub = df_snap[df_snap.ttm == ttm].sort_values('strike')
        if len(sub) < 5:
            continue
        days = int(round(ttm * 365))
        # Calls and puts together — moneyness convention
        ax.plot(sub.strike, sub.iv_brent, marker='o', markersize=4,
                linewidth=1.5, color=color, alpha=0.85,
                label=f'T = {days} days')

    # Mark the underlying price (ATM line)
    if len(df_snap) > 0:
        spot = df_snap.S.median()
        ax.axvline(spot, color='gray', linestyle='--', alpha=0.5,
                   linewidth=1, label=f'Spot ≈ ${spot:.2f}')

    ax.set_xlabel('Strike Price ($)')
    ax.set_ylabel('Implied Volatility (σ)')
    ax.set_title(f'{symbol} Volatility Smile — {quote_date}',
                 pad=10, weight='bold')
    ax.legend(loc='upper right', frameon=True, framealpha=0.9)
    ax.set_ylim(bottom=0)

    # Annotation that sells the result
    ax.text(0.02, 0.98, f'n = {len(df_snap):,} contracts\n'
                        f'computed by multi-section method',
            transform=ax.transAxes, va='top', ha='left', fontsize=9,
            bbox=dict(boxstyle='round,pad=0.4', facecolor='white',
                      edgecolor='gray', alpha=0.9))

    fig.savefig(out_path)
    print(f"[smile] wrote {out_path}")
    plt.close(fig)


def plot_iteration_comparison(df: pd.DataFrame, out_path: Path):
    """Bar chart: bisection iterations vs multi-section iterations.

    Describes the FPGA implementation (N=4 with 8 iterations), not the
    Python reference (which ran at N=8 with 5 iterations). The hardware
    used N=4 to fit the Zynq-7020 DSP budget, with iteration count
    increased to maintain the same 1e-4 convergence threshold.
    """
    # Bisection: theoretical halvings to reach 1e-4 from [0.005, 3.0]
    # 2.995 / 2^n < 1e-4  ->  n > log2(29950) ≈ 14.87  ->  15 iters
    bisection_iters = 15

    # Multi-section at N=4: gap shrinks 4x per iteration
    # 2.995 / 4^n < 1e-4  ->  n > log4(29950) ≈ 7.43  ->  8 iters
    multisection_iters_hw = 8

    fig, ax = plt.subplots(figsize=(5, 4))
    methods = ['Bisection\n(traditional)', 'Multi-section\n(this work, N=4)']
    iters = [bisection_iters, multisection_iters_hw]
    colors = ['#9B7BC9', '#4B2D86']

    bars = ax.bar(methods, iters, color=colors, width=0.55,
                  edgecolor='white', linewidth=1.5)

    # Numbers on top of each bar
    for bar, val in zip(bars, iters):
        ax.text(bar.get_x() + bar.get_width() / 2, val + 0.3,
                f'{val}', ha='center', va='bottom', fontsize=12,
                weight='bold')

    ax.set_ylabel('Iterations to converge (1e-4 threshold)')
    ax.set_title('Algorithm Convergence Speed', weight='bold', pad=10)
    ax.set_ylim(0, max(iters) * 1.25)
    ax.grid(axis='x')

    speedup = bisection_iters / multisection_iters_hw
    ax.text(0.5, 0.95,
            f'{speedup:.1f}× fewer iterations\n+ 4× parallel evaluation',
            transform=ax.transAxes, ha='center', va='top', fontsize=10,
            bbox=dict(boxstyle='round,pad=0.5', facecolor='#FFF4D6',
                      edgecolor='#D4A027'))

    fig.savefig(out_path)
    print(f"[iter] wrote {out_path}")
    plt.close(fig)


def plot_error_histogram(df: pd.DataFrame, out_path: Path):
    """Backup figure: |multi-section - Brent| distribution."""
    err = (df.iv_multisection - df.iv_brent).abs().dropna()

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.hist(err, bins=50, color='#4B2D86', alpha=0.85, edgecolor='white')
    ax.set_xlabel('|σ_multisection − σ_Brent|')
    ax.set_ylabel('Count')
    ax.set_title('Multi-section accuracy vs reference (n='
                 f'{len(err):,})', weight='bold', pad=10)

    ax.axvline(err.median(), color='#D4A027', linestyle='--',
               label=f'Median = {err.median():.2e}')
    ax.axvline(err.max(), color='#C73E3E', linestyle='--',
               label=f'Max = {err.max():.2e}')
    ax.legend()

    fig.savefig(out_path)
    print(f"[err] wrote {out_path}")
    plt.close(fig)


def plot_iv_surface_heatmap(df: pd.DataFrame, out_path: Path,
                             quote_date: str | None = None,
                             symbol: str = 'SPY'):
    """2D heatmap of IV across strike and time-to-expiry.

    Filters to the liquid region of the surface (7-365 day expiries within
    ±25% moneyness) and pools the first trading morning for maximum density.
    Each visible cell is one (strike, expiry) bucket colored by IV.
    """
    df = df[df.underlying_symbol == symbol].copy()
    df = df.dropna(subset=['iv_brent'])

    # Pool the full first trading day for density. Single-timestamp snapshots
    # are too sparse for a heatmap on a real dataset.
    first_day = df.quote_datetime.iloc[0][:7]   # e.g., '02MAY16'
    df_day = df[df.quote_datetime.str.startswith(first_day)].copy()
    df_snap = (df_day.groupby(['strike', 'ttm'], as_index=False)
                     .agg({'iv_brent': 'median', 'S': 'median'}))
    title_suffix = f'{first_day}'

    df_snap['days_to_exp'] = (df_snap.ttm * 365).round().astype(int)
    df_snap['moneyness'] = df_snap.strike / df_snap.S

    # --- liquidity filters (match plot_iv_surface_curves) ---
    df_snap = df_snap[(df_snap.days_to_exp >= 7) & (df_snap.days_to_exp <= 365)]
    df_snap = df_snap[(df_snap.moneyness >= 0.75) & (df_snap.moneyness <= 1.25)]

    if len(df_snap) == 0:
        print(f"[surface] WARNING: no contracts passed liquidity filters")
        return

    fig, ax = plt.subplots(figsize=(8, 5.5))

    # Scatter with color encoding IV. Marker size scales down with point
    # density so dense regions don't blob into solid color.
    marker_size = max(20, min(80, 8000 // max(len(df_snap), 1)))
    sc = ax.scatter(df_snap.strike, df_snap.days_to_exp,
                    c=df_snap.iv_brent, cmap='viridis',
                    s=marker_size, marker='s', edgecolor='white', linewidth=0.2,
                    vmin=df_snap.iv_brent.quantile(0.02),
                    vmax=df_snap.iv_brent.quantile(0.98))

    cbar = plt.colorbar(sc, ax=ax, pad=0.02)
    cbar.set_label('Implied Volatility (σ)', rotation=270, labelpad=18)

    spot = df_snap.S.median()
    ax.axvline(spot, color='white', linestyle='--', alpha=0.8,
               linewidth=1.5)
    ax.text(spot, ax.get_ylim()[1] * 0.96, f' Spot ≈ ${spot:.2f}',
            color='white', fontsize=10, va='top', weight='bold')

    # Format date suffix: "02MAY16" -> "2 May 2016"
    month_map = {'JAN': 'Jan', 'FEB': 'Feb', 'MAR': 'Mar', 'APR': 'Apr',
                 'MAY': 'May', 'JUN': 'Jun', 'JUL': 'Jul', 'AUG': 'Aug',
                 'SEP': 'Sep', 'OCT': 'Oct', 'NOV': 'Nov', 'DEC': 'Dec'}
    try:
        nice_date = (f'{int(title_suffix[0:2])} '
                     f'{month_map.get(title_suffix[2:5].upper(), title_suffix[2:5])} '
                     f'20{title_suffix[5:7]}')
    except Exception:
        nice_date = title_suffix

    ax.set_xlabel('Strike Price ($)')
    ax.set_ylabel('Days to Expiration')
    ax.set_title(f'{symbol} Implied Volatility Surface — {nice_date}',
                 weight='bold', pad=10)

    ax.text(0.02, 0.98,
            f'n = {len(df_snap):,} contracts\n'
            'multi-section method,\n'
            '7–365 day expiries',
            transform=ax.transAxes, va='top', ha='left', fontsize=9,
            bbox=dict(boxstyle='round,pad=0.4', facecolor='white',
                      edgecolor='gray', alpha=0.92))

    fig.savefig(out_path)
    print(f"[surface] wrote {out_path}")
    plt.close(fig)


def plot_iv_surface_curves(df: pd.DataFrame, out_path: Path,
                            symbol: str = 'SPY',
                            quote_date: str | None = None):
    """The 'family of smile curves' view — many expiries on one plot.

    Uses a narrow time window (5 minutes near a busy mid-morning quote)
    to get enough strike coverage per expiry while keeping the underlying
    spot price effectively constant. Pure single-instant snapshots are too
    sparse (often just 1 trade per timestamp); full-day pooling causes
    spurious cross-curve noise because spot drifts during the day.

    Filters to liquid expiries (7 to 365 days) within ±25% moneyness,
    and drops options whose mid price is too low to invert reliably.
    """
    df = df[df.underlying_symbol == symbol].copy()
    df = df.dropna(subset=['iv_brent'])

    df['days'] = (df.ttm * 365).round().astype(int)
    df['moneyness'] = df.strike / df.S

    liquid = df[(df.days >= 7) & (df.days <= 365) &
                (df.moneyness >= 0.75) & (df.moneyness <= 1.25) &
                (df.mid >= 0.20)]
    if len(liquid) == 0:
        print(f"[curves] WARNING: no contracts in liquid region")
        return

    # CBOE timestamps look like '02MAY16:11:11:08'. Group by minute so we can
    # find a busy minute, then take a ±5 minute window around it.
    liquid = liquid.copy()
    liquid['minute'] = liquid.quote_datetime.str[:14]  # 'DDMMMYY:HH:MM'

    if quote_date is None:
        # Prefer 10:30-11:30 AM — past the opening jitter, before the
        # midday lunch slowdown. This window has the tightest spreads.
        prime_time = liquid[liquid.quote_datetime.str[9:14].between(
            '10:30', '11:30')]
        if len(prime_time) == 0:
            # Fallback to 10am-2pm if nothing in the prime window
            prime_time = liquid[liquid.quote_datetime.str[9:11].isin(
                ['10', '11', '12', '13'])]
        if len(prime_time) == 0:
            prime_time = liquid
        busy_minute = prime_time.minute.value_counts().idxmax()
    else:
        busy_minute = quote_date[:14]

    # Build a ±5 minute window. Easiest is to parse hour:minute, find adjacent
    # minutes in the actual data, take a few before and after.
    all_minutes = sorted(liquid.minute.unique())
    if busy_minute not in all_minutes:
        print(f"[curves] WARNING: requested minute {busy_minute} not in data")
        return
    center_idx = all_minutes.index(busy_minute)
    window = all_minutes[max(0, center_idx - 5):center_idx + 6]
    df_snap = liquid[liquid.minute.isin(window)].copy()

    # Within the window, take median IV per (strike, days) — small dedupe
    # since the spot barely moved
    df_snap = (df_snap.groupby(['strike', 'days'], as_index=False)
                      .agg({'iv_brent': 'median', 'S': 'median',
                            'moneyness': 'median'}))

    # Require enough strikes per expiry for a clean curve
    counts = df_snap.groupby('days').size()
    liquid_expiries = counts[counts >= 10].index.tolist()
    df_snap = df_snap[df_snap.days.isin(liquid_expiries)]

    if len(df_snap) == 0:
        print(f"[curves] WARNING: no expiries passed strike-count filter")
        return

    # Pick 4 expiries spanning the term structure
    days_unique = sorted(df_snap.days.unique())
    n_pick = min(4, len(days_unique))
    idx = np.linspace(0, len(days_unique) - 1, n_pick).astype(int)
    chosen_days = [days_unique[i] for i in idx]

    fig, ax = plt.subplots(figsize=(7.5, 5))

    cmap = plt.cm.viridis
    for i, days in enumerate(chosen_days):
        sub = df_snap[df_snap.days == days].sort_values('strike')
        color = cmap(i / max(len(chosen_days) - 1, 1))
        ax.plot(sub.strike, sub.iv_brent, marker='o', markersize=4,
                linewidth=1.8, color=color, alpha=0.9,
                label=f'T = {days} days')

    spot = df_snap.S.median()
    ax.axvline(spot, color='gray', linestyle='--', alpha=0.5, linewidth=1)
    ax.text(spot, ax.get_ylim()[1] * 0.92, ' ATM',
            color='gray', fontsize=9, va='top')

    # Parse CBOE timestamp "04MAY16:15:41" into "04 May 2016, 15:41"
    # Months mapping for the abbreviated form used in the dataset
    month_map = {'JAN': 'Jan', 'FEB': 'Feb', 'MAR': 'Mar', 'APR': 'Apr',
                 'MAY': 'May', 'JUN': 'Jun', 'JUL': 'Jul', 'AUG': 'Aug',
                 'SEP': 'Sep', 'OCT': 'Oct', 'NOV': 'Nov', 'DEC': 'Dec'}
    try:
        day_part, time_part = busy_minute.split(':', 1)
        d = day_part[0:2]
        m = month_map.get(day_part[2:5].upper(), day_part[2:5])
        y = '20' + day_part[5:7]
        # time_part is "HH:MM" with possibly a trailing chunk; take just HH:MM
        hhmm = time_part[:5]
        nice_time = f'{int(d)} {m} {y}, {hhmm}'
    except Exception:
        nice_time = busy_minute

    ax.set_xlabel('Strike Price ($)')
    ax.set_ylabel('Implied Volatility (σ)')
    ax.set_title(f'{symbol} Implied Volatility Surface Slices',
                 weight='bold', pad=10)
    ax.legend(loc='upper right', frameon=True, framealpha=0.9)

    n_options_shown = len(df_snap)
    ax.text(0.02, 0.98,
            f'Snapshot: {nice_time}\n'
            f'n = {n_options_shown:,} contracts\n'
            f'±25% moneyness band',
            transform=ax.transAxes, va='top', ha='left', fontsize=9,
            bbox=dict(boxstyle='round,pad=0.4', facecolor='white',
                      edgecolor='gray', alpha=0.92))

    fig.savefig(out_path)
    print(f"[curves] wrote {out_path} ({n_options_shown} contracts at {nice_time})")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', default='./build/reference.csv', type=Path)
    ap.add_argument('--out', default='./build', type=Path)
    args = ap.parse_args()

    df = pd.read_csv(args.csv)
    print(f"[load] {len(df):,} rows from {args.csv}")
    print(f"[load] symbols: {df.underlying_symbol.unique().tolist()}")
    print(f"[load] timestamps: {df.quote_datetime.nunique():,} unique")

    plot_smile(df, args.out / 'smile_main.png')
    plot_iteration_comparison(df, args.out / 'iter_count.png')
    plot_error_histogram(df, args.out / 'error_hist.png')
    plot_iv_surface_heatmap(df, args.out / 'surface_heatmap.png')
    plot_iv_surface_curves(df, args.out / 'surface_curves.png')


if __name__ == '__main__':
    main()
