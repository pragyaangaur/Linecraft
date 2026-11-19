// Fractional clock divider with edge resync, one per shuttle.
// See "Clock divider and edge resync" in docs/spec.md. The tick period is
// N or N + 1 cycles, with N = max(DIV_INT, 1), chosen by the carry out of an
// 8 bit accumulator, so the average period is N + DIV_FRAC / 256 cycles with
// no cumulative drift. formal/divider.sv proves that for every setting.

`default_nettype none

module weft_divider (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        en,
    input  wire        restart,
    input  wire        sync_edge,    // an edge on the resync pin this cycle
    input  wire [15:0] div_int,
    input  wire [7:0]  div_frac,
    input  wire [15:0] sync_phase,
    output wire        tick
);

    reg [15:0] cnt;
    reg [7:0]  acc;
    wire [15:0] div_n   = (div_int == 16'd0) ? 16'd1 : div_int;
    wire [8:0]  acc_sum = {1'b0, acc} + {1'b0, div_frac};

    assign tick = en & ~sync_edge & (cnt == 16'd0);

    always @(posedge clk) begin
        if (!rst_n || !en || restart) begin
            cnt <= 16'd0;
            acc <= 8'd0;
        end else if (sync_edge) begin
            cnt <= sync_phase;
            acc <= 8'd0;
        end else if (cnt == 16'd0) begin
            acc <= acc_sum[7:0];
            cnt <= div_n - 16'd1 + {15'd0, acc_sum[8]};
        end else begin
            cnt <= cnt - 16'd1;
        end
    end

`ifdef FORMAL_DIVIDER
    // Zero cumulative drift. With the configuration held and no resync, let
    // f_elapsed count cycles and f_target add 256 * N + DIV_FRAC on every
    // tick. Then 256 * (f_elapsed + cnt) + acc == f_target on every cycle,
    // so tick k falls exactly at floor(k * (N + DIV_FRAC / 256)) cycles and
    // the error never grows. The equality is proved by induction for every
    // DIV_INT and DIV_FRAC, together with the bound cnt <= N, which says no
    // period is longer than N + 1 cycles.
    reg        f_init = 1'b1;
    reg [47:0] f_elapsed, f_target;
    reg [15:0] f_int;
    reg [7:0]  f_frac;
    always @(posedge clk) begin
        f_init <= 1'b0;
        if (f_init) begin
            f_int  <= div_int;
            f_frac <= div_frac;
        end
    end
    always @(*) begin
        if (f_init) assume (!rst_n);
        else begin
            assume (rst_n);
            assume (en);
            assume (!restart);
            assume (!sync_edge);
            assume (div_int == f_int);
            assume (div_frac == f_frac);
        end
    end
    always @(posedge clk) begin
        if (!rst_n) begin
            f_elapsed <= 48'd0;
            f_target  <= 48'd0;
        end else begin
            f_elapsed <= f_elapsed + 48'd1;
            if (tick) f_target <= f_target + {24'd0, div_n, div_frac};
        end
    end
    always @(*) begin
        if (!f_init) begin
            assert ({f_elapsed[39:0] + {24'd0, cnt}, 8'd0} + {40'd0, acc} == f_target);
            assert (cnt <= div_n);
        end
    end
`endif

endmodule
