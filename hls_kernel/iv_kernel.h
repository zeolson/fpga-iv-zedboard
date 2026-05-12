#ifndef IV_KERNEL_H
#define IV_KERNEL_H

#include <ap_axi_sdata.h>
#include <hls_stream.h>
#include <hls_math.h>

// Number of parallel sigma evaluations per multi-section iteration.
// Picked to fit Zynq-7020 DSP budget (220 DSP48E1) at II=1 pipelining.
// N=8 unrolled overflows the chip (~1000 DSPs); N=4 pipelined fits with
// margin (~110 DSPs) and recovers the same throughput by pipelining the
// section evaluator instead of replicating it.
#define N_SECTIONS 4

// Convergence threshold: gap between sigma_lo and sigma_hi.
// Paper uses 1e-4 — same here.
#define SIGMA_THRESHOLD 1.0e-4f

// Search bracket. Paper uses [0.01, 2.99]; we use [0.005, 3.0] to allow
// slightly tighter low-end coverage of out-of-the-money puts.
#define SIGMA_LO_INIT 0.005f
#define SIGMA_HI_INIT 3.000f

// Maximum iterations. With N_SECTIONS=4, gap shrinks by 4x per iter.
// Starting gap = 3.0 - 0.005 ≈ 2.995. Iterations to reach 1e-4:
//   ceil(log_4(2.995 / 1e-4)) = ceil(7.42) = 8.
#define MAX_ITERS 10

// Per-option input struct, 32 bytes, packed by python_ref/reference_iv.py.
// All host-side transcendentals are precomputed here per Fig. 6 of the paper.
struct option_in_t {
    float log_SK;       // ln(S/K)
    float sqrt_T;       // sqrt(T)
    float disc_K;       // K * exp(-r*T)
    float disc_S;       // S * exp(-q*T)
    float rqT;          // (r-q) * T
    float market_px;    // option mid price
    unsigned int is_call;  // 1 = call, 0 = put
    unsigned int _pad;     // pad to 32B
};

// AXI4-Stream input: 256 bits = 32 bytes = exactly one option_in_t per beat.
typedef ap_axiu<256, 0, 0, 0> in_axis_t;
// AXI4-Stream output: 32 bits = one float (sigma) per beat.
typedef ap_axiu<32, 0, 0, 0> out_axis_t;

// Top-level HLS function. Vitis HLS will synthesize this to a Verilog IP
// with two AXI4-Stream interfaces and an AXI4-Lite control register set.
void iv_kernel(
    hls::stream<in_axis_t>  &in_stream,
    hls::stream<out_axis_t> &out_stream,
    unsigned int n_options
);

#endif
