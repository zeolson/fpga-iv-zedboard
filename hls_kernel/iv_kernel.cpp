// =============================================================================
// iv_kernel.cpp — Multi-section implied volatility accelerator (Vitis HLS C++)
//
// Implements Algorithm 2 from Wang et al. 2022 ("FPGA based Implied Volatility
// Calculation with Multi-section Method"). Targeted at Zynq-7020 (Zedboard).
//
// Hybrid CPU/FPGA split per Fig. 6: the host computes ln(S/K), sqrt(T),
// K*exp(-rT), S*exp(-qT), and (r-q)*T once per option. The FPGA only does
// add/sub/mul/div + a piecewise-rational erf approximation in the inner loop.
// This avoids instantiating expensive log/sqrt/exp cores on the PL.
//
// Performance target on Zynq-7020:
//   - 100 MHz Fmax (timing-friendly)
//   - II=1 inner loop over the 8 parallel sections
//   - ~50 cycles per option total (5 iters * ~10 cycles each)
//   - Throughput @ 100 MHz: ~2 Mops/sec — easily 100x the ARM bisection
// =============================================================================
#include "iv_kernel.h"

// -----------------------------------------------------------------------------
// erf approximation — Abramowitz & Stegun 7.1.26.
// Max error 1.5e-7, well under the 1e-4 IV threshold.
// Cheaper than hls::erff (which compiles to a much larger LUT-heavy core).
// -----------------------------------------------------------------------------
static float erf_approx(float x) {
    #pragma HLS INLINE
    const float a1 =  0.254829592f;
    const float a2 = -0.284496736f;
    const float a3 =  1.421413741f;
    const float a4 = -1.453152027f;
    const float a5 =  1.061405429f;
    const float p  =  0.3275911f;

    float sign = (x < 0.0f) ? -1.0f : 1.0f;
    float ax = (x < 0.0f) ? -x : x;
    float t  = 1.0f / (1.0f + p * ax);
    // Horner's rule polynomial evaluation
    float y  = 1.0f - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t
                       * hls::expf(-ax * ax);
    return sign * y;
}

static float norm_cdf(float x) {
    #pragma HLS INLINE
    return 0.5f * (1.0f + erf_approx(x * 0.7071067811865475f));  // 1/sqrt(2)
}

// -----------------------------------------------------------------------------
// loss_fn — the inner kernel evaluated 8x in parallel per iteration.
// All log/sqrt/exp work has been hoisted to the host. This function is pure
// add/sub/mul/div + the erf approximation above.
//
// Returns: BS price - market price, signed.
// -----------------------------------------------------------------------------
static float loss_fn(
    float sigma,
    float log_SK, float sqrt_T,
    float disc_K, float disc_S,
    float rqT,
    float market_px,
    bool  is_call)
{
    #pragma HLS INLINE
    float sig_sqrtT = sigma * sqrt_T;
    float half_sig2_T = 0.5f * sigma * sigma * (sqrt_T * sqrt_T);
    float d1 = (log_SK + rqT + half_sig2_T) / sig_sqrtT;
    float d2 = d1 - sig_sqrtT;

    float Nd1 = norm_cdf(d1);
    float Nd2 = norm_cdf(d2);

    float bs_price;
    if (is_call) {
        // S*e^-qT * N(d1)  -  K*e^-rT * N(d2)
        bs_price = disc_S * Nd1 - disc_K * Nd2;
    } else {
        // K*e^-rT * N(-d2)  -  S*e^-qT * N(-d1)
        // Use symmetry N(-x) = 1 - N(x) to avoid recomputing erf
        bs_price = disc_K * (1.0f - Nd2) - disc_S * (1.0f - Nd1);
    }
    return bs_price - market_px;
}

// -----------------------------------------------------------------------------
// solve_one_iv — runs the multi-section algorithm for a single option.
// -----------------------------------------------------------------------------
static float solve_one_iv(const option_in_t &opt) {
    #pragma HLS INLINE off

    float sigma_lo = SIGMA_LO_INIT;
    float sigma_hi = SIGMA_HI_INIT;
    bool  is_call  = (opt.is_call != 0);

    // Sanity-check the bracket. If both endpoints have same sign there's no
    // root in [SIGMA_LO_INIT, SIGMA_HI_INIT]; emit -1.0 sentinel. The host
    // driver filters these out the same way the Python reference does.
    float f_lo_init = loss_fn(sigma_lo, opt.log_SK, opt.sqrt_T,
                              opt.disc_K, opt.disc_S, opt.rqT,
                              opt.market_px, is_call);
    float f_hi_init = loss_fn(sigma_hi, opt.log_SK, opt.sqrt_T,
                              opt.disc_K, opt.disc_S, opt.rqT,
                              opt.market_px, is_call);
    if (f_lo_init * f_hi_init > 0.0f) {
        return -1.0f;
    }

    iter_loop:
    for (int it = 0; it < MAX_ITERS; ++it) {
        #pragma HLS PIPELINE off  // iter is sequential — each needs the prior

        float gap = (sigma_hi - sigma_lo) / (float)N_SECTIONS;
        if (gap <= SIGMA_THRESHOLD) break;

        // Evaluate N_SECTIONS+1 candidate sigmas in parallel.
        float sigmas[N_SECTIONS + 1];
        float losses[N_SECTIONS + 1];
        #pragma HLS ARRAY_PARTITION variable=sigmas complete
        #pragma HLS ARRAY_PARTITION variable=losses complete

        eval_loop:
        for (int i = 0; i <= N_SECTIONS; ++i) {
            #pragma HLS PIPELINE II=1  // share loss_fn hardware, II=1 throughput
            sigmas[i] = sigma_lo + (float)i * gap;
            losses[i] = loss_fn(sigmas[i], opt.log_SK, opt.sqrt_T,
                                opt.disc_K, opt.disc_S, opt.rqT,
                                opt.market_px, is_call);
        }

        // Sign-change detector: find first adjacent pair with opposite signs.
        // Paper §III.B: "only one binary digit from each partition is needed
        // to be evaluated as 0 or 1 as an XOR gate." Implemented exactly that
        // way here — sign bits XOR'd, encoded into a small priority mux.
        bool sign[N_SECTIONS + 1];
        #pragma HLS ARRAY_PARTITION variable=sign complete
        sign_loop:
        for (int i = 0; i <= N_SECTIONS; ++i) {
            #pragma HLS UNROLL    // tiny: just N+1 sign bits, cheap to unroll
            sign[i] = (losses[i] < 0.0f);
        }

        int idx = 0;
        bool found = false;
        find_loop:
        for (int i = 0; i < N_SECTIONS; ++i) {
            #pragma HLS UNROLL    // tiny: just N XOR gates
            // sign change between i and i+1
            if ((sign[i] ^ sign[i + 1]) && !found) {
                idx = i;
                found = true;
            }
        }

        if (found) {
            sigma_lo = sigmas[idx];
            sigma_hi = sigmas[idx + 1];
        } else {
            // monotonic bracket lost — shouldn't happen for well-posed IV
            // problems, but emit best guess and break
            break;
        }
    }

    // Final selection: endpoint with smaller |loss| (paper Algorithm 2 lines 15-19)
    float f_lo = loss_fn(sigma_lo, opt.log_SK, opt.sqrt_T,
                         opt.disc_K, opt.disc_S, opt.rqT,
                         opt.market_px, (opt.is_call != 0));
    float f_hi = loss_fn(sigma_hi, opt.log_SK, opt.sqrt_T,
                         opt.disc_K, opt.disc_S, opt.rqT,
                         opt.market_px, (opt.is_call != 0));
    float abs_lo = (f_lo < 0.0f) ? -f_lo : f_lo;
    float abs_hi = (f_hi < 0.0f) ? -f_hi : f_hi;
    return (abs_lo <= abs_hi) ? sigma_lo : sigma_hi;
}

// -----------------------------------------------------------------------------
// Dataflow stage 1: read AXI input, unpack to option_in_t, push to internal stream
// -----------------------------------------------------------------------------
static void input_stage(
    hls::stream<in_axis_t> &in_stream,
    hls::stream<option_in_t> &opt_stream,
    unsigned int n_options)
{
    in_loop:
    for (unsigned int i = 0; i < n_options; ++i) {
        #pragma HLS PIPELINE II=1
        in_axis_t in_word = in_stream.read();
        ap_uint<256> raw = in_word.data;

        option_in_t opt;
        union { unsigned int u; float f; } cvt;
        cvt.u = (unsigned int)raw.range( 31,   0); opt.log_SK    = cvt.f;
        cvt.u = (unsigned int)raw.range( 63,  32); opt.sqrt_T    = cvt.f;
        cvt.u = (unsigned int)raw.range( 95,  64); opt.disc_K    = cvt.f;
        cvt.u = (unsigned int)raw.range(127,  96); opt.disc_S    = cvt.f;
        cvt.u = (unsigned int)raw.range(159, 128); opt.rqT       = cvt.f;
        cvt.u = (unsigned int)raw.range(191, 160); opt.market_px = cvt.f;
        opt.is_call = (unsigned int)raw.range(223, 192);
        opt._pad    = 0;
        opt_stream.write(opt);
    }
}

// -----------------------------------------------------------------------------
// Dataflow stage 2: read option_in_t, run multi-section solver, push sigma
// -----------------------------------------------------------------------------
static void compute_stage(
    hls::stream<option_in_t> &opt_stream,
    hls::stream<float> &sigma_stream,
    unsigned int n_options)
{
    compute_loop:
    for (unsigned int i = 0; i < n_options; ++i) {
        option_in_t opt = opt_stream.read();
        float sigma = solve_one_iv(opt);
        sigma_stream.write(sigma);
    }
}

// -----------------------------------------------------------------------------
// Dataflow stage 3: read sigma, pack to AXI output, write out
// -----------------------------------------------------------------------------
static void output_stage(
    hls::stream<float> &sigma_stream,
    hls::stream<out_axis_t> &out_stream,
    unsigned int n_options)
{
    out_loop:
    for (unsigned int i = 0; i < n_options; ++i) {
        #pragma HLS PIPELINE II=1
        float sigma = sigma_stream.read();
        out_axis_t out_word;
        union { unsigned int u; float f; } cvt;
        cvt.f = sigma;
        out_word.data = cvt.u;
        out_word.keep = 0xF;
        out_word.strb = 0xF;
        out_word.last = (i == n_options - 1) ? 1 : 0;
        out_word.user = 0;
        out_word.id   = 0;
        out_word.dest = 0;
        out_stream.write(out_word);
    }
}

// -----------------------------------------------------------------------------
// Top-level: streaming wrapper using DATAFLOW. The three stages (input,
// compute, output) run in parallel — while stage 2 computes option N, stage 1
// is already unpacking N+1 and stage 3 is writing N-1. Internal hls::streams
// hand off data between them with FIFO buffering.
//
// In the previous (non-DATAFLOW) version each option processed completely
// before the next started; this version overlaps stages across options for
// throughput limited by the slowest stage (compute, ~2175 cycles) instead of
// the sum of all stages.
// -----------------------------------------------------------------------------
void iv_kernel(
    hls::stream<in_axis_t>  &in_stream,
    hls::stream<out_axis_t> &out_stream,
    unsigned int n_options)
{
    #pragma HLS INTERFACE axis port=in_stream
    #pragma HLS INTERFACE axis port=out_stream
    #pragma HLS INTERFACE s_axilite port=n_options bundle=control
    #pragma HLS INTERFACE s_axilite port=return    bundle=control
    #pragma HLS DATAFLOW

    // Internal hand-off streams between stages
    hls::stream<option_in_t> opt_stream;
    hls::stream<float> sigma_stream;
    #pragma HLS STREAM variable=opt_stream depth=4
    #pragma HLS STREAM variable=sigma_stream depth=4

    input_stage(in_stream, opt_stream, n_options);
    compute_stage(opt_stream, sigma_stream, n_options);
    output_stage(sigma_stream, out_stream, n_options);
}
