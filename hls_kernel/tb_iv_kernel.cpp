// =============================================================================
// tb_iv_kernel.cpp — Vitis HLS C-simulation testbench.
//
// Reads build/input.bin and build/golden.bin produced by python_ref/reference_iv.py,
// streams every option through iv_kernel, and reports max/mean error vs. golden.
//
// Run from the HLS project (Vitis HLS GUI: "Run C Simulation") or from CLI:
//     vitis_hls -p run_csim.tcl   (see hls_kernel/run_hls.tcl)
// =============================================================================
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <vector>
#include "iv_kernel.h"

int main(int argc, char **argv) {
    const char *input_path  = (argc > 1) ? argv[1] : "../../../../build/input.bin";
    const char *golden_path = (argc > 2) ? argv[2] : "../../../../build/golden.bin";

    // ---- load inputs ----
    FILE *fin = std::fopen(input_path, "rb");
    if (!fin) { std::fprintf(stderr, "cannot open %s\n", input_path); return 1; }
    std::fseek(fin, 0, SEEK_END);
    long input_bytes = std::ftell(fin);
    std::fseek(fin, 0, SEEK_SET);
    int n = (int)(input_bytes / sizeof(option_in_t));
    std::vector<option_in_t> options(n);
    std::fread(options.data(), sizeof(option_in_t), n, fin);
    std::fclose(fin);
    std::printf("[tb] loaded %d options from %s\n", n, input_path);

    // ---- load golden ----
    FILE *fg = std::fopen(golden_path, "rb");
    if (!fg) { std::fprintf(stderr, "cannot open %s\n", golden_path); return 1; }
    std::vector<float> golden(n);
    std::fread(golden.data(), sizeof(float), n, fg);
    std::fclose(fg);

    // ---- pack inputs into the AXI-stream type ----
    hls::stream<in_axis_t>  in_stream;
    hls::stream<out_axis_t> out_stream;
    for (int i = 0; i < n; ++i) {
        in_axis_t w;
        ap_uint<256> raw = 0;
        union { unsigned int u; float f; } cvt;
        cvt.f = options[i].log_SK;    raw.range( 31,   0) = cvt.u;
        cvt.f = options[i].sqrt_T;    raw.range( 63,  32) = cvt.u;
        cvt.f = options[i].disc_K;    raw.range( 95,  64) = cvt.u;
        cvt.f = options[i].disc_S;    raw.range(127,  96) = cvt.u;
        cvt.f = options[i].rqT;       raw.range(159, 128) = cvt.u;
        cvt.f = options[i].market_px; raw.range(191, 160) = cvt.u;
        raw.range(223, 192) = options[i].is_call;
        raw.range(255, 224) = 0;
        w.data = raw;
        w.keep = (ap_uint<32>)-1;
        w.strb = (ap_uint<32>)-1;
        w.last = (i == n - 1) ? 1 : 0;
        w.user = 0; w.id = 0; w.dest = 0;
        in_stream.write(w);
    }

    // ---- run DUT ----
    iv_kernel(in_stream, out_stream, (unsigned int)n);

    // ---- collect & compare ----
    int n_within_tol = 0;
    double max_abs_err = 0.0;
    double sum_abs_err = 0.0;
    for (int i = 0; i < n; ++i) {
        out_axis_t w = out_stream.read();
        union { unsigned int u; float f; } cvt;
        cvt.u = w.data;
        float fpga_sigma = cvt.f;
        if (fpga_sigma < 0.0f) continue;  // sentinel, no root in bracket
        double err = std::fabs(fpga_sigma - golden[i]);
        if (err < 5e-4) n_within_tol++;
        if (err > max_abs_err) max_abs_err = err;
        sum_abs_err += err;
    }
    double mean_err = sum_abs_err / (double)n;

    std::printf("[tb] %d/%d within 5e-4 of golden\n", n_within_tol, n);
    std::printf("[tb] max  |err| = %.3e\n", max_abs_err);
    std::printf("[tb] mean |err| = %.3e\n", mean_err);

    // PASS gate: 99% of options within 5e-4 (5x the 1e-4 threshold to allow
    // for erf approximation drift accumulating across iterations).
    bool pass = ((double)n_within_tol / (double)n > 0.99) && (max_abs_err < 5e-3);
    std::printf("[tb] %s\n", pass ? "PASS" : "FAIL");
    return pass ? 0 : 1;
}
