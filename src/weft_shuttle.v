// One Weft shuttle: a small cycle-exact processor with its own divider,
// FIFOs, line coder (finite state transducer) and CRC unit.
// The behaviour follows docs/spec.md. Comments point at the spec where a
// choice is not obvious from the code.

`default_nettype none

module weft_shuttle (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        en,          // shuttle enable from the host
    input  wire        restart,     // one cycle restart pulse from the host

    input  wire [15:0] pins,        // synchronized pin inputs
    input  wire [15:0] pins_prev,   // pins one cycle earlier

    output wire [5:0]  pc_out,      // instruction fetch address
    input  wire [23:0] instr,       // imem[pc_out]

    input  wire [7:0]  irq_flags,
    output reg  [7:0]  irq_set,
    output reg  [7:0]  irq_clr,

    // host configuration writes
    input  wire        cfg_we,
    input  wire [5:0]  cfg_addr,
    input  wire        fst_we,
    input  wire [4:0]  fst_addr,
    input  wire [7:0]  wdata,

    // host FIFO access
    input  wire        tx_push,
    input  wire        rx_pop,
    output wire        tx_full,
    output wire        rx_empty,
    output wire [7:0]  rx_head,
    output wire [2:0]  rx_level,

    output reg  [15:0] pv,
    output reg  [15:0] pe
);

    // ------------------------------------------------------------------
    // Configuration registers
    // ------------------------------------------------------------------
    reg [15:0] div_int;
    reg [7:0]  div_frac;
    reg [3:0]  in_base, out_base, set_base, set_count, side_base;
    reg [1:0]  side_count;
    reg        side_pindir;
    reg        out_right, in_right, autopull, autopush, fst_diff;
    reg [5:0]  pull_thresh, push_thresh;
    reg [5:0]  wrap_bot, wrap_top, start_pc;
    reg [3:0]  jmp_pin;
    reg        sync_en;
    reg [3:0]  sync_pin;
    reg [15:0] sync_phase;
    reg [3:0]  fst_init;
    reg [1:0]  crc_src;
    reg        crc_init_ones, crc_xorout_ones;
    reg [5:0]  crc_width;
    reg [31:0] crc_poly, crc_check;
    reg [15:0] match_mask, match_val;

    always @(posedge clk) begin
        if (!rst_n) begin
            div_int <= 16'd1;  div_frac <= 8'd0;
            in_base <= 0; out_base <= 0; set_base <= 0; set_count <= 0;
            side_base <= 0; side_count <= 0; side_pindir <= 0;
            out_right <= 0; in_right <= 0; autopull <= 0; autopush <= 0; fst_diff <= 0;
            pull_thresh <= 6'd8; push_thresh <= 6'd8;
            wrap_bot <= 0; wrap_top <= 0; start_pc <= 0; jmp_pin <= 0;
            sync_en <= 0; sync_pin <= 0; sync_phase <= 0; fst_init <= 0;
            crc_src <= 0; crc_init_ones <= 0; crc_xorout_ones <= 0;
            crc_width <= 0; crc_poly <= 0; crc_check <= 0;
            match_mask <= 0; match_val <= 0;
        end else if (cfg_we) begin
            case (cfg_addr)
                6'h00: div_int[7:0]   <= wdata;
                6'h01: div_int[15:8]  <= wdata;
                6'h02: div_frac       <= wdata;
                6'h03: in_base        <= wdata[3:0];
                6'h04: out_base       <= wdata[3:0];
                6'h05: set_base       <= wdata[3:0];
                6'h06: set_count      <= wdata[3:0];
                6'h07: side_base      <= wdata[3:0];
                6'h08: begin side_count <= wdata[1:0]; side_pindir <= wdata[2]; end
                6'h09: begin out_right <= wdata[0]; in_right <= wdata[1];
                             autopull  <= wdata[2]; autopush <= wdata[3];
                             fst_diff  <= wdata[4]; end
                6'h0A: pull_thresh    <= wdata[5:0];
                6'h0B: push_thresh    <= wdata[5:0];
                6'h0C: wrap_bot       <= wdata[5:0];
                6'h0D: wrap_top       <= wdata[5:0];
                6'h0E: start_pc       <= wdata[5:0];
                6'h0F: jmp_pin        <= wdata[3:0];
                6'h10: sync_en        <= wdata[0];
                6'h11: sync_pin       <= wdata[3:0];
                6'h12: sync_phase[7:0]  <= wdata;
                6'h13: sync_phase[15:8] <= wdata;
                6'h14: fst_init       <= wdata[3:0];
                6'h15: begin crc_src <= wdata[1:0]; crc_init_ones <= wdata[2];
                             crc_xorout_ones <= wdata[3]; end
                6'h16: crc_width      <= wdata[5:0];
                6'h18: crc_poly[7:0]    <= wdata;
                6'h19: crc_poly[15:8]   <= wdata;
                6'h1A: crc_poly[23:16]  <= wdata;
                6'h1B: crc_poly[31:24]  <= wdata;
                6'h1C: crc_check[7:0]   <= wdata;
                6'h1D: crc_check[15:8]  <= wdata;
                6'h1E: crc_check[23:16] <= wdata;
                6'h1F: crc_check[31:24] <= wdata;
                6'h20: match_mask[7:0]  <= wdata;
                6'h21: match_mask[15:8] <= wdata;
                6'h22: match_val[7:0]   <= wdata;
                6'h23: match_val[15:8]  <= wdata;
                default: ;
            endcase
        end
    end

    // Line coder table: 32 entries of 7 bits {flag, xfer, out, next[3:0]}.
    reg [6:0] fst_tab [0:31];
    integer t;
    always @(posedge clk) begin
        if (!rst_n) begin
            for (t = 0; t < 32; t = t + 1) fst_tab[t] <= 7'd0;
        end else if (fst_we) begin
            fst_tab[fst_addr] <= wdata[6:0];
        end
    end

    // ------------------------------------------------------------------
    // Small helpers
    // ------------------------------------------------------------------
    function [31:0] mask_n;            // low n bits set, n in 1..32
        input [5:0] n;
        begin
            mask_n = (n == 6'd32) ? 32'hFFFF_FFFF : ((32'd1 << n) - 32'd1);
        end
    endfunction

    function [31:0] therm;             // low n bits set, n in 1..32, as compares
        input [5:0] n;
        integer j;
        begin
            for (j = 0; j < 32; j = j + 1) therm[j] = ({26'd0, n} > j);
        end
    endfunction

    function [5:0] count32;            // a 6 bit count field: 0 and 33 to 63 mean 32
        input [5:0] c;
        begin
            count32 = (c == 6'd0 || c > 6'd32) ? 6'd32 : c;
        end
    endfunction

    function [15:0] rotl16;
        input [15:0] v;
        input [3:0]  s;
        reg   [31:0] w;
        begin
            w = {v, v} << s;
            rotl16 = w[31:16];
        end
    endfunction

    function [15:0] rotr16;
        input [15:0] v;
        input [3:0]  s;
        reg   [31:0] w;
        begin
            w = {v, v} >> s;
            rotr16 = w[15:0];
        end
    endfunction

    function [31:0] rev32;
        input [31:0] v;
        integer k;
        begin
            for (k = 0; k < 32; k = k + 1) rev32[k] = v[31 - k];
        end
    endfunction

    // ------------------------------------------------------------------
    // FIFOs (4 entries of 8 bits each)
    // ------------------------------------------------------------------
    reg [7:0] txf [0:3];
    reg [7:0] rxf [0:3];
    reg [1:0] tx_rd, tx_wr, rx_rd, rx_wr;
    reg [2:0] tx_cnt, rx_cnt;

    assign tx_full  = (tx_cnt == 3'd4);
    wire   tx_empty = (tx_cnt == 3'd0);
    wire   rx_full  = (rx_cnt == 3'd4);
    assign rx_empty = (rx_cnt == 3'd0);
    assign rx_head  = rxf[rx_rd];
    assign rx_level = rx_cnt;
    wire [7:0] tx_head = txf[tx_rd];

    // ------------------------------------------------------------------
    // Shuttle state
    // ------------------------------------------------------------------
    reg [5:0]  pc;
    reg [15:0] x, y;
    reg [31:0] osr, isr;
    reg [5:0]  osr_left, isr_cnt;
    reg [4:0]  dly;
    reg [3:0]  fst;
    reg        fst_err;
    // The CRC register is kept left aligned (crc_al[31] is bit W-1 of the
    // spec register) so the feedback bit is always bit 31. This avoids a
    // 32 to 1 mux per CRC step and gives the same visible behaviour.
    reg [31:0] crc_al;
    reg [7:0]  crc_pend_bits;          // bits waiting to enter the CRC, in feed order
    reg [3:0]  crc_pend_n;             // how many of them (0 to 8)

    assign pc_out = pc;

    // ------------------------------------------------------------------
    // Divider with edge resync
    // ------------------------------------------------------------------
    wire        sync_edge = sync_en & (pins[sync_pin] ^ pins_prev[sync_pin]);
    wire        tick;

    weft_divider divider (
        .clk(clk), .rst_n(rst_n), .en(en), .restart(restart), .sync_edge(sync_edge),
        .div_int(div_int), .div_frac(div_frac), .sync_phase(sync_phase), .tick(tick)
    );

    // ------------------------------------------------------------------
    // Decode
    // ------------------------------------------------------------------
    wire [3:0] op     = instr[23:20];
    wire [4:0] f_dly  = instr[19:15];
    wire [2:0] f_side = instr[14:12];
    wire [3:0] f_a    = instr[11:8];
    wire [5:0] f_n    = count32(instr[5:0]);
    wire [5:0] f_addr = instr[5:0];

    localparam OP_JMP = 4'd0, OP_WAIT = 4'd1, OP_IN = 4'd2, OP_OUT = 4'd3,
               OP_PUSH = 4'd4, OP_PULL = 4'd5, OP_MOV = 4'd6, OP_SET = 4'd7,
               OP_IRQ = 4'd8, OP_CTL = 4'd9;

    wire attempt = tick & (dly == 5'd0);   // the instruction is tried this cycle

    wire [5:0]  pull_th  = count32(pull_thresh);
    wire [5:0]  push_th  = count32(push_thresh);
    wire [5:0]  crc_w    = (crc_width == 6'd0 || crc_width > 6'd32) ? 6'd32 : crc_width;
    wire [31:0] crc_mask = mask_n(crc_w);
    wire [5:0]  crc_sh   = 6'd32 - crc_w;               // 0 when W = 32
    wire [31:0] crc      = crc_al >> crc_sh;             // spec view of the register
    wire [31:0] crc_init = crc_init_ones ? (32'hFFFF_FFFF << crc_sh) : 32'd0;
    wire [31:0] poly_al  = (crc_poly & crc_mask) << crc_sh;
    wire [31:0] crc_xo   = crc_xorout_ones ? crc_mask : 32'd0;
    wire [31:0] crc_res  = crc ^ crc_xo;

    // Line coder entries for the current state.
    wire [6:0] fe0   = fst_tab[{fst, 1'b0}];
    wire [6:0] fe1   = fst_tab[{fst, 1'b1}];
    // The next step needs no data bit when both entries agree and take none.
    wire       fpend = (fe0 == fe1) & ~fe0[5];

    // Jump condition
    reg cond;
    always @(*) begin
        case (f_a)
            4'd0:  cond = 1'b1;
            4'd1:  cond = (x == 16'd0);
            4'd2:  cond = (x != 16'd0);
            4'd3:  cond = (y == 16'd0);
            4'd4:  cond = (y != 16'd0);
            4'd5:  cond = (osr_left != 6'd0) | (autopull & ~tx_empty) | fpend;   // MORE
            4'd6:  cond = pins[jmp_pin];
            4'd7:  cond = ~pins[jmp_pin];
            4'd8:  cond = (osr_left != 6'd0);
            4'd9:  cond = ~tx_empty;
            4'd10: cond = ((pins & match_mask) == match_val);
            4'd11: cond = ((pins & match_mask) != match_val);
            4'd12: cond = (crc == (crc_check & crc_mask));
            4'd13: cond = fst_err;
            4'd14: cond = fpend;
            default: cond = ~rx_full;
        endcase
    end

    // Wait condition
    reg wait_val;
    always @(*) begin
        case (instr[6:5])
            2'd0: wait_val = pins[instr[3:0]];
            2'd1: wait_val = pins[in_base + instr[3:0]];
            2'd2: wait_val = irq_flags[instr[2:0]];
            default: wait_val = 1'b0;
        endcase
    end
    wire wait_ok = (wait_val == instr[7]);

    // ------------------------------------------------------------------
    // IN data path
    // ------------------------------------------------------------------
    wire [15:0] pins_rot = rotr16(pins, in_base);
    wire [6:0]  rx_e     = pins[in_base] ? fe1 : fe0;    // receive line coder step
    wire        in_fst   = (f_a == 4'd6);

    reg [31:0] in_raw;
    always @(*) begin
        case (f_a)
            4'd0:    in_raw = {pins_rot, pins_rot};       // pin numbers wrap mod 16
            4'd1:    in_raw = {16'd0, x};
            4'd2:    in_raw = {16'd0, y};
            4'd4:    in_raw = isr;
            4'd5:    in_raw = osr;
            4'd6:    in_raw = {31'd0, rx_e[4]};
            default: in_raw = 32'd0;
        endcase
    end

    wire [5:0]  in_n     = in_fst ? 6'd1 : f_n;
    wire        in_do    = in_fst ? rx_e[5] : 1'b1;      // does a bit enter the ISR?
    wire [31:0] in_d     = in_raw & therm(in_n);

    // ------------------------------------------------------------------
    // OUT data path, first half
    // ------------------------------------------------------------------
    wire        out_fst   = (f_a == 4'd7);
    wire        need_pull = autopull & (osr_left == 6'd0) & ~(out_fst & fpend);
    wire [31:0] pulled    = out_right ? {24'd0, tx_head} : {tx_head, 24'd0};
    wire [31:0] osr_src   = need_pull ? pulled : osr;
    wire [5:0]  left_src  = need_pull ? pull_th : osr_left;
    wire        tx_b      = out_right ? osr_src[0] : osr_src[31];
    wire [6:0]  tx_e      = tx_b ? fe1 : fe0;            // transmit line coder step
    wire [5:0]  out_n     = out_fst ? 6'd1 : f_n;
    wire        out_do    = out_fst ? tx_e[5] : 1'b1;    // is a data bit consumed?

    // ------------------------------------------------------------------
    // Two shared barrel shifters. IN and OUT never run in the same cycle,
    // so one shifter moves the ISR or the OSR by n, and the other lines
    // data up by 32 - n. A left shift is a right shift between two bit
    // reversals, which are only wiring plus a row of muxes.
    // ------------------------------------------------------------------
    wire        is_out   = (op == OP_OUT);
    wire [5:0]  sh_n     = is_out ? out_n : in_n;
    wire [5:0]  sh_k     = 6'd32 - sh_n;                 // 0 when n = 32

    wire        s1_left  = is_out ? ~out_right : ~in_right;
    wire [31:0] s1_a     = is_out ? osr_src : isr;
    wire [31:0] s1_b     = (s1_left ? rev32(s1_a) : s1_a) >> sh_n;
    wire [31:0] s1       = s1_left ? rev32(s1_b) : s1_b;  // isr or osr shifted by n

    wire [31:0] s2_a     = is_out ? osr_src : in_d;
    wire [31:0] s2_b     = (is_out ? s2_a : rev32(s2_a)) >> sh_k;
    wire [31:0] s2       = is_out ? s2_b : rev32(s2_b);   // osr >> (32-n) or in_d << (32-n)

    wire [31:0] isr_sh   = in_right ? (s1 | s2) : (s1 | in_d);
    wire [6:0]  isr_sum  = {1'b0, isr_cnt} + {1'b0, in_n};
    wire [5:0]  isr_cnt_sh = (isr_sum > 7'd32) ? 6'd32 : isr_sum[5:0];
    wire        in_push  = in_do & autopush & (isr_cnt_sh >= push_th);
    wire [7:0]  in_byte  = in_right ? isr_sh[31:24] : isr_sh[7:0];
    // feed order: right shift d[0], d[1] ...; left shift d[n-1], d[n-2] ...
    wire [31:0] in_d_hi  = s2;

    wire [31:0] out_d     = out_right ? (osr_src & therm(out_n)) : s2;
    wire [31:0] osr_sh    = s1;
    wire [5:0]  left_sh   = (left_src > out_n) ? (left_src - out_n) : 6'd0;

    // ------------------------------------------------------------------
    // MOV source
    // ------------------------------------------------------------------
    reg [31:0] mov_v;
    always @(*) begin
        case (instr[3:0])
            4'd0: mov_v = {16'd0, pins_rot};
            4'd1: mov_v = {16'd0, x};
            4'd2: mov_v = {16'd0, y};
            4'd4: mov_v = isr;
            4'd5: mov_v = osr;
            4'd6: mov_v = crc_res;
            4'd7: mov_v = {25'd0, rx_full, tx_empty, fst_err, fst};
            default: mov_v = 32'd0;
        endcase
        case (instr[7:6])
            2'd1: mov_v = ~mov_v;
            2'd2: mov_v = rev32(mov_v);
            default: ;
        endcase
    end
    // CRC laid out so that r[W-1] leaves first.
    wire [31:0] crc_tx = out_right ? (rev32(crc_res) >> (6'd32 - crc_w))
                                   : (crc_res << crc_sh);

    // ------------------------------------------------------------------
    // Stall
    // ------------------------------------------------------------------
    wire push_go = ~instr[1] | (isr_cnt >= push_th);    // PUSH iffull test
    wire pull_go = ~instr[1] | (osr_left == 6'd0);      // PULL ifempty test

    reg stall;
    always @(*) begin
        case (op)
            OP_WAIT: stall = ~wait_ok;
            OP_IN:   stall = in_push & rx_full;
            OP_OUT:  stall = need_pull & tx_empty;
            OP_PUSH: stall = push_go & rx_full & instr[0];
            OP_PULL: stall = pull_go & tx_empty & instr[0];
            default: stall = 1'b0;
        endcase
    end
    wire exec = attempt & ~stall;

    // ------------------------------------------------------------------
    // Pin writes. A write of n bits starting at base: for n > 16 the
    // upper bits land on the same pins again and win.
    // ------------------------------------------------------------------
    function [31:0] pin_write;          // returns {mask, data} before rotation
        input [31:0] d;
        input [5:0]  n;
        integer j;
        reg [15:0] m, v;
        begin
            for (j = 0; j < 16; j = j + 1) begin
                m[j] = ({26'd0, n} > j);
                v[j] = ({26'd0, n} > j + 16) ? d[j + 16] : d[j];
            end
            pin_write = {m, v};
        end
    endfunction

    // One shared main pin write (OUT or SET to PINS or PINDIRS). Side-set
    // has its own, because it lands in the same cycle as the main write.
    wire [5:0]  set_n   = (set_count > 4'd8) ? 6'd8 : {2'd0, set_count};
    wire [31:0] mw      = pin_write(is_out ? out_d : {24'd0, instr[7:0]}, is_out ? out_n : set_n);
    wire [3:0]  mw_base = is_out ? out_base : set_base;
    wire [15:0] mw_m    = rotl16(mw[31:16], mw_base);
    wire [15:0] mw_v    = rotl16(mw[15:0], mw_base);

    // ------------------------------------------------------------------
    // Next state
    // ------------------------------------------------------------------
    reg [5:0]  pc_n;
    reg [15:0] x_n, y_n, pv_n, pe_n;
    reg [31:0] osr_n, isr_n;
    reg [5:0]  osr_left_n, isr_cnt_n;
    reg [4:0]  dly_n;
    reg [3:0]  fst_n;
    reg        fst_err_n;
    reg        jump;
    reg [5:0]  jump_to;
    reg        tx_pop, rx_push;
    reg [7:0]  rx_data;
    reg [7:0]  feed_bits;
    reg [3:0]  feed_n;
    reg        crc_reset;
    reg [31:0] pw;
    reg [15:0] wm, wv;
    integer    k;

    always @(*) begin
        pc_n = pc;  x_n = x;  y_n = y;  pv_n = pv;  pe_n = pe;
        osr_n = osr;  isr_n = isr;  osr_left_n = osr_left;  isr_cnt_n = isr_cnt;
        dly_n = dly;  fst_n = fst;  fst_err_n = fst_err;
        jump = 1'b0;  jump_to = f_addr;
        tx_pop = 1'b0;  rx_push = 1'b0;  rx_data = 8'd0;
        feed_bits = 8'd0;  feed_n = 4'd0;  crc_reset = 1'b0;
        irq_set = 8'd0;  irq_clr = 8'd0;
        pw = 32'd0;  wm = 16'd0;  wv = 16'd0;

        if (tick && dly != 5'd0)
            dly_n = dly - 5'd1;

        if (exec) begin
            case (op)
                OP_JMP: begin
                    jump = cond;
                    if (f_a == 4'd2) x_n = x - 16'd1;
                    if (f_a == 4'd4) y_n = y - 16'd1;
                end
                OP_WAIT: begin
                    if (instr[6:5] == 2'd2 && instr[7])
                        irq_clr[instr[2:0]] = 1'b1;
                end
                OP_IN: begin
                    if (in_fst) begin
                        fst_n = rx_e[3:0];
                        if (rx_e[6]) fst_err_n = 1'b1;
                    end
                    if (in_do) begin
                        if (in_push) begin
                            rx_push = 1'b1;
                            rx_data = in_byte;
                            isr_n = 32'd0;
                            isr_cnt_n = 6'd0;
                        end else begin
                            isr_n = isr_sh;
                            isr_cnt_n = isr_cnt_sh;
                        end
                        if (crc_src == 2'd2) begin
                            for (k = 0; k < 8; k = k + 1)
                                feed_bits[k] = in_right ? in_d[k] : in_d_hi[31 - k];
                            feed_n = (in_n > 6'd8) ? 4'd8 : in_n[3:0];
                        end
                    end
                end
                OP_OUT: begin
                    if (need_pull) tx_pop = 1'b1;
                    if (out_fst) begin
                        fst_n = tx_e[3:0];
                        if (tx_e[6]) fst_err_n = 1'b1;
                        pv_n[out_base] = tx_e[4];
                        if (fst_diff) pv_n[out_base + 4'd1] = ~tx_e[4];
                        if (out_do) begin
                            osr_n = osr_sh;
                            osr_left_n = left_sh;
                        end else begin
                            osr_n = osr_src;
                            osr_left_n = left_src;
                        end
                    end else begin
                        osr_n = osr_sh;
                        osr_left_n = left_sh;
                        case (f_a)
                            4'd0: pv_n = (pv & ~mw_m) | (mw_v & mw_m);
                            4'd1: x_n = out_d[15:0];
                            4'd2: y_n = out_d[15:0];
                            4'd4: pe_n = (pe & ~mw_m) | (mw_v & mw_m);
                            4'd5: begin jump = 1'b1; jump_to = out_d[5:0]; end
                            4'd6: begin isr_n = out_d; isr_cnt_n = out_n; end
                            default: ;
                        endcase
                    end
                    if (crc_src == 2'd1 && out_do) begin
                        for (k = 0; k < 8; k = k + 1)
                            feed_bits[k] = out_right ? osr_src[k] : osr_src[31 - k];
                        feed_n = (out_n > 6'd8) ? 4'd8 : out_n[3:0];
                    end
                end
                OP_PUSH: begin
                    if (push_go) begin
                        if (!rx_full) begin
                            rx_push = 1'b1;
                            rx_data = in_right ? isr[31:24] : isr[7:0];
                        end
                        isr_n = 32'd0;
                        isr_cnt_n = 6'd0;
                    end
                end
                OP_PULL: begin
                    if (pull_go) begin
                        if (!tx_empty) begin
                            tx_pop = 1'b1;
                            osr_n = pulled;
                        end else begin
                            osr_n = {16'd0, x};
                        end
                        osr_left_n = pull_th;
                    end
                end
                OP_MOV: begin
                    case (f_a)
                        4'd0: x_n = mov_v[15:0];
                        4'd1: y_n = mov_v[15:0];
                        4'd2: begin isr_n = mov_v; isr_cnt_n = 6'd0; end
                        4'd3: begin
                            if (instr[3:0] == 4'd6) begin
                                osr_n = crc_tx;
                                osr_left_n = crc_w;
                            end else begin
                                osr_n = mov_v;
                                osr_left_n = 6'd32;
                            end
                        end
                        4'd4: begin jump = 1'b1; jump_to = mov_v[5:0]; end
                        default: ;
                    endcase
                end
                OP_SET: begin
                    case (f_a)
                        4'd0: pv_n = (pv & ~mw_m) | (mw_v & mw_m);
                        4'd1: x_n = {8'd0, instr[7:0]};
                        4'd2: y_n = {8'd0, instr[7:0]};
                        4'd4: pe_n = (pe & ~mw_m) | (mw_v & mw_m);
                        default: ;
                    endcase
                end
                OP_IRQ: begin
                    if (instr[4]) irq_clr[instr[2:0]] = 1'b1;
                    else          irq_set[instr[2:0]] = 1'b1;
                end
                OP_CTL: begin
                    if (instr[0]) crc_reset = 1'b1;
                    if (instr[1]) begin fst_n = fst_init; fst_err_n = 1'b0; end
                end
                default: ;
            endcase

            dly_n = f_dly;
            if (jump)                 pc_n = jump_to;
            else if (pc == wrap_top)  pc_n = wrap_bot;
            else                      pc_n = pc + 6'd1;
        end

        // Side-set is applied on every attempt, stalled or not, and wins.
        if (attempt && side_count != 2'd0) begin
            pw = pin_write({29'd0, f_side}, {4'd0, side_count});
            wm = rotl16(pw[31:16], side_base);
            wv = rotl16(pw[15:0], side_base);
            if (side_pindir) pe_n = (pe_n & ~wm) | (wv & wm);
            else             pv_n = (pv_n & ~wm) | (wv & wm);
        end
    end

    // CRC step for up to 8 bits, used one cycle after the feed.
    function [31:0] crc_step;
        input [31:0] c;
        input [7:0]  bits;
        input [3:0]  n;
        input [31:0] poly;
        integer j;
        reg fb;
        begin
            crc_step = c;
            for (j = 0; j < 8; j = j + 1) begin
                if (j < n) begin
                    fb = crc_step[31] ^ bits[j];
                    crc_step = (crc_step << 1) ^ (fb ? poly : 32'd0);
                end
            end
        end
    endfunction

    // ------------------------------------------------------------------
    // Registers
    // ------------------------------------------------------------------
    always @(posedge clk) begin
        if (!rst_n) begin
            pv <= 16'd0;
            pe <= 16'd0;
        end else begin
            pv <= pv_n;
            pe <= pe_n;
        end
    end

    always @(posedge clk) begin
        if (!rst_n || restart) begin
            pc <= start_pc;
            x <= 16'd0;  y <= 16'd0;
            osr <= 32'd0;  isr <= 32'd0;
            osr_left <= 6'd0;  isr_cnt <= 6'd0;
            dly <= 5'd0;
            fst <= fst_init;
            fst_err <= 1'b0;
            crc_al <= crc_init;
            crc_pend_bits <= 8'd0;
            crc_pend_n <= 4'd0;
        end else begin
            pc <= pc_n;  x <= x_n;  y <= y_n;
            osr <= osr_n;  isr <= isr_n;
            osr_left <= osr_left_n;  isr_cnt <= isr_cnt_n;
            dly <= dly_n;
            fst <= fst_n;
            fst_err <= fst_err_n;
            if (crc_reset)
                crc_al <= crc_init;
            else if (crc_pend_n != 4'd0)
                crc_al <= crc_step(crc_al, crc_pend_bits, crc_pend_n, poly_al);
            crc_pend_bits <= feed_bits;
            crc_pend_n <= feed_n;
        end
    end

    // FIFO pointers. Host and shuttle sides can act in the same cycle.
    always @(posedge clk) begin
        if (!rst_n || restart) begin
            tx_rd <= 0; tx_wr <= 0; tx_cnt <= 0;
            rx_rd <= 0; rx_wr <= 0; rx_cnt <= 0;
        end else begin
            if (tx_push && !tx_full) begin
                txf[tx_wr] <= wdata;
                tx_wr <= tx_wr + 2'd1;
            end
            if (tx_pop) tx_rd <= tx_rd + 2'd1;
            tx_cnt <= tx_cnt + {2'd0, (tx_push && !tx_full)} - {2'd0, tx_pop};

            if (rx_push) begin
                rxf[rx_wr] <= rx_data;
                rx_wr <= rx_wr + 2'd1;
            end
            if (rx_pop && !rx_empty) rx_rd <= rx_rd + 2'd1;
            rx_cnt <= rx_cnt + {2'd0, rx_push} - {2'd0, (rx_pop && !rx_empty)};
        end
    end

`ifdef FORMAL_SHUTTLE
    // Invariants that hold for every program, every configuration and every
    // pin and host input, proved by induction.
    reg f_init = 1'b1;
    always @(posedge clk) f_init <= 1'b0;
    always @(*) if (f_init) assume (!rst_n);
    always @(*) begin
        if (!f_init) begin
            // The FIFOs never overflow or underflow, and the pointers agree
            // with the counts.
            assert (tx_cnt <= 3'd4);
            assert (rx_cnt <= 3'd4);
            assert (tx_wr == tx_rd + tx_cnt[1:0]);
            assert (rx_wr == rx_rd + rx_cnt[1:0]);
            assert (!(tx_pop && tx_empty));
            assert (!(rx_push && rx_full));
            // The shift register counts stay within 32 bits.
            assert (osr_left <= 6'd32);
            assert (isr_cnt <= 6'd32);
            // At most 8 CRC bits wait for the next cycle.
            assert (crc_pend_n <= 4'd8);
            // An instruction only runs on a tick with no delay left.
            assert (!exec || (tick && dly == 5'd0));
        end
    end
`endif

endmodule
