// Host SPI target (mode 0, MSB first). It turns SPI transactions into
// byte reads and writes on a simple internal bus. See "Host SPI port" in
// docs/spec.md. The SPI pins arrive already synchronized, together with
// their values one cycle earlier, so edges are found by comparing the two.

`default_nettype none

module weft_host (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        csn,         // synchronized CSn
    input  wire        sck,         // synchronized SCK
    input  wire        sck_prev,    // SCK one cycle earlier
    input  wire        mosi,        // synchronized MOSI
    output wire        miso,

    output reg         wr,          // one cycle write strobe
    output reg         rd_commit,   // one cycle strobe: the host is reading rd_addr
    output reg  [14:0] addr,        // current address (also the read address)
    output reg  [14:0] rd_addr,     // address of the byte now being shifted out
    output reg  [14:0] waddr,       // address for the write strobe
    output reg  [7:0]  wdata,
    input  wire [7:0]  rdata        // read data for `addr`, combinational
);

    reg [2:0] bitcnt;
    reg [1:0] phase;        // 0: first header byte, 1: second, 2: data bytes
    reg       rw;
    reg [6:0] sh_in;
    reg [7:0] sh_out;
    reg       load;         // load sh_out from rdata on the next cycle
    reg       pending;      // a loaded read byte whose side effect is not done

    wire rise = sck & ~sck_prev;
    wire fall = ~sck & sck_prev;
    wire [7:0] byte_in = {sh_in, mosi};
    // FIFO addresses 0x008 to 0x00B do not auto-increment.
    wire no_inc = (addr[14:2] == 13'd2);

    assign miso = sh_out[7];

    always @(posedge clk) begin
        wr <= 1'b0;
        rd_commit <= 1'b0;
        if (!rst_n || csn) begin
            bitcnt <= 3'd0;
            phase <= 2'd0;
            rw <= 1'b0;
            sh_out <= 8'd0;
            load <= 1'b0;
            pending <= 1'b0;
            if (!rst_n) begin
                addr <= 15'd0;
                rd_addr <= 15'd0;
                waddr <= 15'd0;
                sh_in <= 7'd0;
                wdata <= 8'd0;
            end
        end else begin
            if (load) begin
                // addr now holds the address to read
                load <= 1'b0;
                sh_out <= rdata;
                rd_addr <= addr;
                pending <= 1'b1;
                if (!no_inc) addr <= addr + 15'd1;
            end

            if (rise) begin
                sh_in <= byte_in[6:0];
                bitcnt <= bitcnt + 3'd1;
                // The host has started to clock a loaded read byte, so
                // its side effect (a FIFO pop) happens now.
                if (bitcnt == 3'd0 && pending) begin
                    rd_commit <= 1'b1;
                    pending <= 1'b0;
                end
                if (bitcnt == 3'd7) begin
                    case (phase)
                        2'd0: begin
                            rw <= byte_in[7];
                            addr[14:8] <= byte_in[6:0];
                            phase <= 2'd1;
                        end
                        2'd1: begin
                            addr[7:0] <= byte_in;
                            phase <= 2'd2;
                            if (!rw) load <= 1'b1;
                        end
                        default: begin
                            if (rw) begin
                                wr <= 1'b1;
                                wdata <= byte_in;
                                waddr <= addr;
                                if (!no_inc) addr <= addr + 15'd1;
                            end else begin
                                load <= 1'b1;
                            end
                        end
                    endcase
                end
            end else if (fall && bitcnt != 3'd0) begin
                sh_out <= {sh_out[6:0], 1'b0};
            end
        end
    end

endmodule
