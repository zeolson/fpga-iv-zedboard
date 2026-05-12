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
    └── BOARD_BRINGUP.md         # SD-card image, Ethernet, SSH setup
```

### Software

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
  https://www.pynq.io/board.html. 

### Data (one-time)

You will need to source data. You also need:

- **Risk-free rate.** Use 0.0022 (0.22%) flat for May 2016. Source: FRED
  series DTB4WK averaged across the week of 2016-05-02 to 2016-05-06.
  
- **SPY dividend yield.** Use 0.0213 (2.13%) flat for May 2016. Source:
  SPY's trailing-12-month distribution rate from State Street fact sheet,
  May 2016. 

These two numbers are CLI arguments to `reference_iv.py`. 
