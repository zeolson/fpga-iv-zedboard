# =============================================================================
# build_zedboard.tcl — Vivado project builder for the Zedboard IV system.
#
# Usage:
#   cd vivado
#   vivado -mode batch -source build_zedboard.tcl
#
# Prerequisites:
#   - Vivado 2020.2 or newer with Zedboard board files installed
#     (Boards: digilentinc.com:zedboard:part0:1.1)
#   - The HLS IP zip from hls_kernel/iv_kernel_proj/solution1/impl/ip/ has
#     been extracted to ./ip_repo/iv_kernel_v1_0/
# =============================================================================

set proj_name "iv_zedboard"
set proj_dir  "./${proj_name}"
set part      "xc7z020clg484-1"
set board     "digilentinc.com:zedboard:part0:1.1"
set ip_repo   "./ip_repo"

# Clean previous run
file delete -force $proj_dir

create_project $proj_name $proj_dir -part $part
set_property board_part $board [current_project]

# Add custom IP repository (the HLS-generated kernel)
set_property ip_repo_paths $ip_repo [current_project]
update_ip_catalog

# -----------------------------------------------------------------------------
# Block design
# -----------------------------------------------------------------------------
create_bd_design "iv_top"

# Zynq Processing System with Zedboard preset (DDR pins, UART, SD, clocks all
# wired correctly for the board out of the box)
create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 ps7
apply_bd_automation -rule xilinx.com:bd_rule:processing_system7 \
    -config {make_external "FIXED_IO, DDR" \
             apply_board_preset "1" \
             Master "Disable" Slave "Disable"} [get_bd_cells ps7]

# Enable: FCLK_CLK0 @100MHz, M_AXI_GP0 (control), S_AXI_HP0 (DMA into DDR)
set_property -dict [list \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ {100} \
    CONFIG.PCW_USE_S_AXI_HP0 {1} \
    CONFIG.PCW_S_AXI_HP0_DATA_WIDTH {64} \
    CONFIG.PCW_USE_M_AXI_GP0 {1} \
] [get_bd_cells ps7]

# IV kernel IP (from HLS)
create_bd_cell -type ip -vlnv xilinx.com:hls:iv_kernel:1.0 iv_kernel_0

# AXI DMA — moves data between DDR and the kernel's AXI4-Stream ports.
# MM2S streams option records into the kernel; S2MM streams sigma results
# back to DDR. No scatter-gather (simpler, cheaper, fine for our buffer size).
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:7.1 axi_dma_0
set_property -dict [list \
    CONFIG.c_include_sg          {0} \
    CONFIG.c_sg_length_width     {26} \
    CONFIG.c_m_axi_mm2s_data_width {64} \
    CONFIG.c_m_axis_mm2s_tdata_width {256} \
    CONFIG.c_mm2s_burst_size     {16} \
    CONFIG.c_m_axi_s2mm_data_width {64} \
    CONFIG.c_s_axis_s2mm_tdata_width {32} \
    CONFIG.c_s2mm_burst_size     {16} \
] [get_bd_cells axi_dma_0]

# Connect streaming side
connect_bd_intf_net [get_bd_intf_pins axi_dma_0/M_AXIS_MM2S] \
                    [get_bd_intf_pins iv_kernel_0/in_stream]
connect_bd_intf_net [get_bd_intf_pins iv_kernel_0/out_stream] \
                    [get_bd_intf_pins axi_dma_0/S_AXIS_S2MM]

# AXI Interconnects + clocks/resets generated automatically by the next call.
# This single command does ~25 GUI clicks: hooks up the control AXI-Lite of
# both the DMA and the IV kernel to PS M_AXI_GP0, hooks up the DMA's two
# memory-mapped masters to PS S_AXI_HP0, and creates the AXI Interconnect
# IP, the Processor System Reset block, and all clocks.
apply_bd_automation -rule xilinx.com:bd_rule:axi4 \
    -config {Master "/ps7/M_AXI_GP0" Clk "Auto"} \
    [get_bd_intf_pins iv_kernel_0/s_axi_control]
apply_bd_automation -rule xilinx.com:bd_rule:axi4 \
    -config {Master "/ps7/M_AXI_GP0" Clk "Auto"} \
    [get_bd_intf_pins axi_dma_0/S_AXI_LITE]
apply_bd_automation -rule xilinx.com:bd_rule:axi4 \
    -config {Master "/axi_dma_0/M_AXI_MM2S" Slave "/ps7/S_AXI_HP0" \
             intc_ip "New AXI Interconnect" Clk_xbar "Auto" \
             Clk_master "Auto" Clk_slave "Auto"} \
    [get_bd_intf_pins ps7/S_AXI_HP0]
apply_bd_automation -rule xilinx.com:bd_rule:axi4 \
    -config {Master "/axi_dma_0/M_AXI_S2MM" Slave "/ps7/S_AXI_HP0" \
             intc_ip "Auto" Clk_xbar "Auto" \
             Clk_master "Auto" Clk_slave "Auto"} \
    [get_bd_intf_pins axi_dma_0/M_AXI_S2MM]

# Connect DMA interrupts to the PS (optional but useful — lets the host poll
# IRQ instead of busy-looping on status registers)
set_property -dict [list CONFIG.PCW_USE_FABRIC_INTERRUPT {1} \
                         CONFIG.PCW_IRQ_F2P_INTR {1}] [get_bd_cells ps7]
create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat:2.1 irq_concat
set_property CONFIG.NUM_PORTS {2} [get_bd_cells irq_concat]
connect_bd_net [get_bd_pins axi_dma_0/mm2s_introut] [get_bd_pins irq_concat/In0]
connect_bd_net [get_bd_pins axi_dma_0/s2mm_introut] [get_bd_pins irq_concat/In1]
connect_bd_net [get_bd_pins irq_concat/dout]        [get_bd_pins ps7/IRQ_F2P]

regenerate_bd_layout
validate_bd_design
save_bd_design

# Wrap the block design in HDL and set as top
make_wrapper -files [get_files ${proj_dir}/${proj_name}.srcs/sources_1/bd/iv_top/iv_top.bd] -top
add_files -norecurse ${proj_dir}/${proj_name}.srcs/sources_1/bd/iv_top/hdl/iv_top_wrapper.v
update_compile_order -fileset sources_1
set_property top iv_top_wrapper [current_fileset]

# -----------------------------------------------------------------------------
# Synthesis + implementation + bitstream
# -----------------------------------------------------------------------------
launch_runs synth_1 -jobs 4
wait_on_run synth_1
launch_runs impl_1 -to_step write_bitstream -jobs 4
wait_on_run impl_1

# Export hardware for the host driver (.xsa contains bitstream + HW handoff)
write_hw_platform -fixed -include_bit -force \
    -file ${proj_dir}/iv_top.xsa

puts "==============================================================="
puts "Build complete."
puts "Bitstream: ${proj_dir}/${proj_name}.runs/impl_1/iv_top_wrapper.bit"
puts "Hardware handoff: ${proj_dir}/iv_top.xsa"
puts "==============================================================="
