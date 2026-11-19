/*
 * Loom: a reprogrammable protocol emulator.
 * Copyright (c) 2026 Pragyaan Gaur
 * SPDX-License-Identifier: Apache-2.0
 *
 * Top level. It holds the input synchronizer, the host SPI port, the
 * global registers, the shared instruction memory and four shuttles,
 * and merges the shuttle pin outputs. See docs/spec.md.
 */

`default_nettype none

module tt_um_pragyaangaur_loom (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);

    localparam NS = 4;   // number of shuttles

    // ------------------------------------------------------------------
    // Two flop input synchronizer plus one more flop for edge detection
    // ------------------------------------------------------------------
    reg [15:0] sync1, pins, pins_prev;
    always @(posedge clk) begin
        if (!rst_n) begin
            sync1 <= 16'd0;
            pins <= 16'd0;
            pins_prev <= 16'd0;
        end else begin
            sync1 <= {uio_in, ui_in};
            pins <= sync1;
            pins_prev <= pins;
        end
    end

    // ------------------------------------------------------------------
    // Host port
    // ------------------------------------------------------------------
    wire        h_wr, h_rd_commit, miso;
    wire [14:0] h_addr, h_rd_addr, h_waddr;
    wire [7:0]  h_wdata;
    reg  [7:0]  h_rdata;

    loom_host host (
        .clk(clk), .rst_n(rst_n),
        .csn(pins[7]), .sck(pins[6]), .sck_prev(pins_prev[6]), .mosi(pins[5]),
        .miso(miso),
        .wr(h_wr), .rd_commit(h_rd_commit),
        .addr(h_addr), .rd_addr(h_rd_addr), .waddr(h_waddr),
        .wdata(h_wdata), .rdata(h_rdata)
    );

    // ------------------------------------------------------------------
    // Global registers
    // ------------------------------------------------------------------
    reg  [3:0] enable;
    reg  [3:0] restart;
    reg  [7:0] irq;
    wire [7:0] irq_set_s [0:NS-1];
    wire [7:0] irq_clr_s [0:NS-1];

    wire wr_global = h_wr && (h_waddr[14:8] == 7'd0);
    wire [7:0] host_irq_clr = (wr_global && h_waddr[7:0] == 8'h03) ? h_wdata : 8'd0;
    wire [7:0] host_irq_set = (wr_global && h_waddr[7:0] == 8'h04) ? h_wdata : 8'd0;
    wire [7:0] all_set = irq_set_s[0] | irq_set_s[1] | irq_set_s[2] | irq_set_s[3] | host_irq_set;
    wire [7:0] all_clr = irq_clr_s[0] | irq_clr_s[1] | irq_clr_s[2] | irq_clr_s[3] | host_irq_clr;

    always @(posedge clk) begin
        if (!rst_n) begin
            enable <= 4'd0;
            restart <= 4'd0;
            irq <= 8'd0;
        end else begin
            restart <= (wr_global && h_waddr[7:0] == 8'h02) ? h_wdata[3:0] : 4'd0;
            if (wr_global && h_waddr[7:0] == 8'h01) enable <= h_wdata[3:0];
            irq <= (irq & ~all_clr) | all_set;
        end
    end

    // ------------------------------------------------------------------
    // Instruction memory: 64 words of 24 bits, written a byte at a time
    // ------------------------------------------------------------------
    reg [23:0] imem [0:63];
    reg [15:0] istage;
    wire wr_imem = h_wr && (h_waddr[14:8] == 7'd4);
    always @(posedge clk) begin
        if (!rst_n) begin
            istage <= 16'd0;
        end else if (wr_imem) begin
            case (h_waddr[1:0])
                2'd0: istage[7:0]  <= h_wdata;
                2'd1: istage[15:8] <= h_wdata;
                2'd2: imem[h_waddr[7:2]] <= {h_wdata, istage};
                default: ;
            endcase
        end
    end

    // ------------------------------------------------------------------
    // Shuttles
    // ------------------------------------------------------------------
    wire [5:0]  s_pc   [0:NS-1];
    wire [15:0] s_pv   [0:NS-1];
    wire [15:0] s_pe   [0:NS-1];
    wire [NS-1:0] s_tx_full, s_rx_empty;
    wire [7:0]  s_rx_head [0:NS-1];

    genvar g;
    generate
        for (g = 0; g < NS; g = g + 1) begin : sh
            wire cfg_we = h_wr && (h_waddr[14:8] == 7'd1) && (h_waddr[7:6] == g);
            wire fst_we = h_wr && (h_waddr[14:8] == 7'd2) && (h_waddr[7:5] == g);
            wire fifo_wr = h_wr && (h_waddr == 15'h008 + g);
            wire fifo_rd = h_rd_commit && (h_rd_addr == 15'h008 + g);

            loom_shuttle u (
                .clk(clk), .rst_n(rst_n),
                .en(enable[g]), .restart(restart[g]),
                .pins(pins), .pins_prev(pins_prev),
                .pc_out(s_pc[g]), .instr(imem[s_pc[g]]),
                .irq_flags(irq), .irq_set(irq_set_s[g]), .irq_clr(irq_clr_s[g]),
                .cfg_we(cfg_we), .cfg_addr(h_waddr[5:0]),
                .fst_we(fst_we), .fst_addr(h_waddr[4:0]),
                .wdata(h_wdata),
                .tx_push(fifo_wr), .rx_pop(fifo_rd),
                .tx_full(s_tx_full[g]), .rx_empty(s_rx_empty[g]),
                .rx_head(s_rx_head[g]),
                .pv(s_pv[g]), .pe(s_pe[g])
            );
        end
    endgenerate

    // ------------------------------------------------------------------
    // Host read data (for the address the host port is about to read)
    // ------------------------------------------------------------------
    always @(*) begin
        h_rdata = 8'd0;
        if (h_addr[14:8] == 7'd0) begin
            case (h_addr[7:0])
                8'h00: h_rdata = 8'h4C;
                8'h01: h_rdata = {4'd0, enable};
                8'h03: h_rdata = irq;
                8'h05: h_rdata = {~s_rx_empty, ~s_tx_full};
                8'h08: h_rdata = s_rx_head[0];
                8'h09: h_rdata = s_rx_head[1];
                8'h0A: h_rdata = s_rx_head[2];
                8'h0B: h_rdata = s_rx_head[3];
                default: ;
            endcase
        end
    end

    // ------------------------------------------------------------------
    // Pin merge: the highest numbered shuttle with its enable bit set
    // owns a pin.
    // ------------------------------------------------------------------
    reg [15:0] out_v, out_e;
    integer s;
    always @(*) begin
        out_v = 16'd0;
        out_e = 16'd0;
        for (s = 0; s < NS; s = s + 1) begin
            out_v = (out_v & ~s_pe[s]) | (s_pv[s] & s_pe[s]);
            out_e = out_e | s_pe[s];
        end
    end

    assign uo_out  = {miso, out_v[6:0]};
    assign uio_out = out_v[15:8];
    assign uio_oe  = out_e[15:8];

    wire _unused = &{ena, 1'b0};

endmodule
