"""
pynq_runner.py — runs on the Zedboard's ARM core under PYNQ Linux.

Workflow:
  1. SCP this file + iv_top.bit + iv_top.hwh + build/input.bin + build/golden.bin
     to the Zedboard (default user xilinx, password xilinx).
  2. ssh xilinx@<zedboard-ip>
  3. sudo python3 pynq_runner.py

Outputs:
  - results.csv  : per-option (sigma_fpga, sigma_golden, abs_err)
  - timing.txt   : FPGA total/throughput/per-option, ARM bisection comparison

Requires PYNQ ≥ 2.7 image flashed to the Zedboard SD card.
"""
import argparse
import os
import struct
import time
from pathlib import Path

import numpy as np
from pynq import Overlay, allocate

# Must match the layout in python_ref/reference_iv.py
OPTION_RECORD_FMT = "<6f I I"
OPTION_RECORD_SIZE = struct.calcsize(OPTION_RECORD_FMT)  # = 32 bytes


def load_options(input_path: Path, golden_path: Path):
    raw = input_path.read_bytes()
    n = len(raw) // OPTION_RECORD_SIZE
    options = np.frombuffer(raw, dtype=np.uint8).reshape(n, OPTION_RECORD_SIZE)
    golden = np.fromfile(golden_path, dtype=np.float32)
    assert len(golden) == n, f"input has {n} options but golden has {len(golden)}"
    return options, golden


def run_fpga(overlay, options: np.ndarray, golden: np.ndarray):
    n = options.shape[0]
    print(f"[fpga] streaming {n:,} options")

    # Allocate physically-contiguous DMA-friendly buffers in DDR. PYNQ's
    # allocate() pins the pages and gives us a numpy view onto them.
    in_buf  = allocate(shape=(n, OPTION_RECORD_SIZE), dtype=np.uint8)
    out_buf = allocate(shape=(n,), dtype=np.float32)

    # Copy host data into pinned buffer
    in_buf[:] = options
    in_buf.flush()  # ensure CPU caches are written back to DDR before DMA reads

    dma  = overlay.axi_dma_0
    iv   = overlay.iv_kernel_0

    # Configure the kernel: set n_options register and start.
    # AXI-Lite register map per Vitis HLS convention:
    #   0x00 = control (bit 0 = ap_start, bit 1 = ap_done, bit 2 = ap_idle)
    #   0x10 = n_options
    iv.write(0x10, n)

    t0 = time.perf_counter_ns()
    iv.write(0x00, 0x1)  # ap_start

    # Kick off both DMA channels. recvchannel must be armed BEFORE sendchannel
    # transfers complete, otherwise the kernel back-pressures and stalls.
    dma.recvchannel.transfer(out_buf)
    dma.sendchannel.transfer(in_buf)

    dma.sendchannel.wait()
    dma.recvchannel.wait()
    t1 = time.perf_counter_ns()

    out_buf.invalidate()  # ensure CPU view sees DMA-written data
    fpga_sigma = np.array(out_buf, dtype=np.float32, copy=True)

    in_buf.freebuffer()
    out_buf.freebuffer()

    elapsed_s = (t1 - t0) / 1e9
    return fpga_sigma, elapsed_s


def run_arm_bisection(options: np.ndarray, n_check: int = 1000):
    """Reference bisection on the ARM core for the speedup baseline."""
    from math import erf, exp, log, sqrt
    print(f"[arm] running bisection on {n_check:,} options for baseline timing")

    SQRT_HALF = 0.7071067811865475

    def norm_cdf(x):
        return 0.5 * (1.0 + erf(x * SQRT_HALF))

    def loss(sigma, log_SK, sqrt_T, disc_K, disc_S, rqT, mkt, is_call):
        if sigma <= 0:
            return -mkt
        sig_sqrtT = sigma * sqrt_T
        d1 = (log_SK + rqT + 0.5 * sigma * sigma * sqrt_T * sqrt_T) / sig_sqrtT
        d2 = d1 - sig_sqrtT
        if is_call:
            return disc_S * norm_cdf(d1) - disc_K * norm_cdf(d2) - mkt
        return disc_K * (1 - norm_cdf(d2)) - disc_S * (1 - norm_cdf(d1)) - mkt

    n = min(n_check, options.shape[0])
    sigmas = np.zeros(n, dtype=np.float32)

    t0 = time.perf_counter_ns()
    for i in range(n):
        rec = struct.unpack(OPTION_RECORD_FMT, options[i].tobytes())
        log_SK, sqrt_T, disc_K, disc_S, rqT, mkt, is_call, _ = rec
        is_call = bool(is_call)
        lo, hi = 0.005, 3.0
        f_lo = loss(lo, log_SK, sqrt_T, disc_K, disc_S, rqT, mkt, is_call)
        f_hi = loss(hi, log_SK, sqrt_T, disc_K, disc_S, rqT, mkt, is_call)
        if f_lo * f_hi > 0:
            sigmas[i] = -1.0
            continue
        # Bisection (NOT multi-section — that's the FPGA's job)
        for _ in range(40):
            mid = 0.5 * (lo + hi)
            f_mid = loss(mid, log_SK, sqrt_T, disc_K, disc_S, rqT, mkt, is_call)
            if f_lo * f_mid < 0:
                hi, f_hi = mid, f_mid
            else:
                lo, f_lo = mid, f_mid
            if (hi - lo) < 1e-4:
                break
        sigmas[i] = 0.5 * (lo + hi)
    t1 = time.perf_counter_ns()
    elapsed_s = (t1 - t0) / 1e9
    return sigmas, elapsed_s, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bitfile', default='./iv_top.bit')
    ap.add_argument('--input',   default='./input.bin', type=Path)
    ap.add_argument('--golden',  default='./golden.bin', type=Path)
    ap.add_argument('--arm-check', type=int, default=2000,
                    help='Number of options to time on ARM for the speedup ratio')
    args = ap.parse_args()

    print(f"[init] loading overlay {args.bitfile}")
    overlay = Overlay(args.bitfile)
    print(f"[init] IPs in overlay: {list(overlay.ip_dict.keys())}")

    options, golden = load_options(args.input, args.golden)

    fpga_sigma, fpga_s = run_fpga(overlay, options, golden)

    # ---- accuracy ----
    valid = (fpga_sigma >= 0) & np.isfinite(golden)
    err = np.abs(fpga_sigma[valid] - golden[valid])
    print(f"[acc] {valid.sum():,}/{len(golden):,} options returned a valid sigma")
    print(f"[acc] max  |err vs Brent ref| = {err.max():.3e}")
    print(f"[acc] mean |err vs Brent ref| = {err.mean():.3e}")
    print(f"[acc] within 5e-4: {(err < 5e-4).sum():,}/{valid.sum():,} "
          f"({100.0*(err<5e-4).mean():.2f}%)")

    # ---- speed ----
    n = len(golden)
    print(f"[time] FPGA total       = {fpga_s*1000:.3f} ms for {n:,} options")
    print(f"[time] FPGA per-option  = {fpga_s/n*1e6:.3f} us")
    print(f"[time] FPGA throughput  = {n/fpga_s/1e6:.2f} M options/sec")

    arm_sigma, arm_s, n_arm = run_arm_bisection(options, n_check=args.arm_check)
    arm_per_opt = arm_s / n_arm
    fpga_per_opt = fpga_s / n
    speedup = arm_per_opt / fpga_per_opt
    print(f"[time] ARM bisection per-option = {arm_per_opt*1e6:.3f} us "
          f"(over {n_arm:,} samples)")
    print(f"[time] Speedup FPGA vs ARM = {speedup:.1f}x")

    # ---- dump CSV ----
    with open('results.csv', 'w') as f:
        f.write('idx,sigma_fpga,sigma_golden,abs_err\n')
        for i in range(n):
            f.write(f'{i},{fpga_sigma[i]:.6f},{golden[i]:.6f},'
                    f'{abs(fpga_sigma[i]-golden[i]):.6e}\n')

    with open('timing.txt', 'w') as f:
        f.write(f'n_options={n}\n')
        f.write(f'fpga_total_s={fpga_s}\n')
        f.write(f'fpga_per_option_s={fpga_per_opt}\n')
        f.write(f'arm_per_option_s={arm_per_opt}\n')
        f.write(f'speedup={speedup}\n')

    print('[done] wrote results.csv, timing.txt')


if __name__ == '__main__':
    main()
