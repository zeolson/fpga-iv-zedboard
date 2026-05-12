# =============================================================================
# run_hls.tcl — Vitis HLS automation script for the IV kernel.
#
# Usage from a shell with Vitis HLS in PATH:
#   cd hls_kernel
#   vitis_hls -f run_hls.tcl
#
# Produces: ./iv_kernel_proj/solution1/impl/ip/xilinx_com_hls_iv_kernel_*.zip
# That zip is the IP you import into Vivado in step 4 of the day-by-day plan.
# =============================================================================

open_project iv_kernel_proj
set_top iv_kernel
add_files iv_kernel.cpp -cflags "-std=c++14"
add_files iv_kernel.h
add_files -tb tb_iv_kernel.cpp -cflags "-std=c++14"

open_solution "solution1" -flow_target vivado

# Zynq-7020 on the Zedboard. Speed grade -1.
set_part {xc7z020clg484-1}

# 100 MHz target. The Zedboard has a free-running 100 MHz oscillator and
# the PS-PL clock can be programmed there exactly. Keep the clock_uncertainty
# wide so STA in Vivado doesn't fail on routing congestion.
create_clock -period 10 -name default
set_clock_uncertainty 1.5

# 1) C simulation: runs the HLS source as plain C++ against the Python golden.
csim_design -argv "../../../../build/input.bin ../../../../build/golden.bin"

# 2) C synthesis: HLS -> Verilog. Look in the report for II, latency, DSPs/LUTs.
csynth_design

# 3) RTL co-sim: re-runs the testbench against the synthesized RTL.
#    Slow (minutes) — comment out if iterating quickly.
cosim_design -argv "../../../../build/input.bin ../../../../build/golden.bin"

# 4) Package as a Vivado IP zip.
export_design -format ip_catalog -display_name "IV Multi-section Kernel"

exit
