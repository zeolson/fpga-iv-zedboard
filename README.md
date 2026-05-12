# Zedboard implied-volatility accelerator — implementation guide

This is the implementation that goes with the Opus research forum
presentation. The system replicates Wang et al. (2022) — multi-section
implied-volatility calculation — on a Zedboard (Zynq-7020) instead of
their Intel Arria 10. Vivado HLS path, IEEE-754 single-precision floats,
hybrid CPU/FPGA architecture.

## Repository layout

```
zedboard_iv/
├── python_ref/
│   └── reference_iv.py          # CSV → input.bin/golden.bin, Brent reference
├── hls_kernel/
│   ├── iv_kernel.h              # Top-level interface contract
│   ├── iv_kernel.cpp            # Multi-section IV core (HLS C++)
│   ├── tb_iv_kernel.cpp         # csim/cosim testbench
│   └── run_hls.tcl              # Vitis HLS automation
├── vivado/
│   └── build_zedboard.tcl       # Block design + bitstream automation
├── host_driver/
│   └── pynq_runner.py           # On-board host driver (PYNQ Linux)
└── docs/
    ├── DAY_BY_DAY.md            # 7-day execution plan
    └── BOARD_BRINGUP.md         # SD-card image, Ethernet, SSH setup
```

## What you need (one-time host setup)

### Software (on your laptop)

- **Vivado + Vitis HLS 2020.2 or newer** — `xilinx_unified_2020.2_*.bin`
  installer from xilinx.com/AMD. Pick the WebPACK edition (free for
  Zynq-7020). ~50 GB disk.
- **Zedboard board files**:
  ```
  git clone https://github.com/Digilent/vivado-boards
  cp -r vivado-boards/new/board_files/* \
        /tools/Xilinx/Vivado/2020.2/data/boards/board_files/
  ```
- **Python 3.10+** with `pandas`, `numpy`, `scipy` for the reference.

### Hardware (one-time)

- **Zedboard** + 12 V power supply
- **microSD card ≥ 8 GB** (Class 10 or better)
- **micro-USB cable** (UART console, board → laptop)
- **Ethernet cable** (board ↔ router or board ↔ laptop direct)
- **PYNQ image for Zedboard 2.7** (SD card image), download from
  https://www.pynq.io/board.html. The image is what makes step 7 of the
  day-by-day plan tractable — without PYNQ you'd be writing a baremetal C
  driver and dealing with cache flushes by hand.

### Data (one-time)

You already have the CBOE CSV. You also need:

- **Risk-free rate.** Use 0.0022 (0.22%) flat for May 2016. Source: FRED
  series DTB4WK averaged across the week of 2016-05-02 to 2016-05-06.
  Free, no Bloomberg.
- **SPY dividend yield.** Use 0.0213 (2.13%) flat for May 2016. Source:
  SPY's trailing-12-month distribution rate from State Street fact sheet,
  May 2016. Free.

These two numbers are CLI arguments to `reference_iv.py`. Document them
in your report as the "flat-rate simplification" — that's what every
academic paper does too unless they have a Bloomberg terminal.

## Build & run

```
# Host: generate input.bin + golden.bin from the CSV
cd zedboard_iv
python python_ref/reference_iv.py \
    --csv /path/to/CBOE_sample_20160502-20160506.csv \
    --rate 0.0022 --div 0.0213 \
    --max-rows 20000 \
    --out ./build

# Host: run Vitis HLS to produce the IP zip
cd hls_kernel
vitis_hls -f run_hls.tcl

# Host: extract the IP zip into ../vivado/ip_repo/
mkdir -p ../vivado/ip_repo
unzip iv_kernel_proj/solution1/impl/ip/*.zip -d ../vivado/ip_repo/iv_kernel_v1_0/

# Host: build the Vivado project + bitstream
cd ../vivado
vivado -mode batch -source build_zedboard.tcl

# Board: copy artifacts and run
scp iv_zedboard/iv_zedboard.runs/impl_1/iv_top_wrapper.bit \
    xilinx@<board-ip>:~/iv_top.bit
scp iv_zedboard/iv_zedboard.gen/sources_1/bd/iv_top/hw_handoff/iv_top.hwh \
    xilinx@<board-ip>:~/iv_top.hwh
scp ../build/input.bin ../build/golden.bin \
    ../host_driver/pynq_runner.py xilinx@<board-ip>:~/

ssh xilinx@<board-ip>
sudo python3 pynq_runner.py
```

You'll see something like:

```
[acc] within 5e-4: 19,873/19,994 (99.40%)
[time] FPGA total       = 12.4 ms for 19,994 options
[time] FPGA per-option  = 0.621 us
[time] FPGA throughput  = 1.61 M options/sec
[time] ARM bisection per-option = 87.2 us
[time] Speedup FPGA vs ARM = 140.4x
```

Day-by-day plan in `docs/DAY_BY_DAY.md`.
