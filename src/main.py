import argparse
import sys
import time

from assembler import parse_file
from simulator import Simulator


def parse_rom_spec(spec):
    """`PATH@ADDR` → (path, int_addr). ADDR in decimal or 0x hex."""
    if "@" not in spec:
        raise argparse.ArgumentTypeError(
            f"--rom expects PATH@ADDR, got {spec!r}"
        )
    path, addr_s = spec.rsplit("@", 1)
    return path, int(addr_s, 0)


def main():
    parser = argparse.ArgumentParser(description="ursa — MtG simulator")
    parser.add_argument("source", help="linked .s file to run")
    parser.add_argument(
        "--rom",
        action="append",
        default=[],
        type=parse_rom_spec,
        metavar="PATH@ADDR",
        help=(
            "Preload PATH into guest memory starting at ADDR before the "
            "simulator starts. May be given multiple times. Lets huge "
            "initial memory images (Linux kernel, rootfs, etc.) bypass "
            "the cost of embedding them as .long data in the .s."
        ),
    )
    parser.add_argument(
        "--zero-mem",
        action="store_true",
        help=(
            "Return 0 for reads from uninitialized memory instead of "
            "raising. Needed for full-system boots where the kernel "
            "touches arbitrary BSS ranges it hasn't written yet; leave "
            "off for small test programs where an uninitialized read is "
            "almost certainly a bug."
        ),
    )
    args = parser.parse_args()

    program = parse_file(args.source)
    program.fixup_jumps()
    simulator = Simulator(program, mem_default_zero=args.zero_mem)

    # Preload any --rom payloads straight into the simulator's memory
    # dict (same dict .comm / .long writes land in). Each byte of the
    # file becomes one memory cell, matching MtG's byte-per-cell layout.
    for path, base_addr in args.rom:
        with open(path, "rb") as f:
            data = f.read()
        for i, b in enumerate(data):
            simulator.memory[base_addr + i] = b
        print(
            f"[ursa] preloaded {len(data):,} bytes from {path!r} "
            f"at 0x{base_addr:08x}",
            file=sys.stderr,
        )

    steps = 0
    t0 = time.perf_counter()
    last_tick = t0
    # Periodic progress line every ~3s of wall time so long-running
    # programs (full-system boots) show they're alive. The check adds
    # a few ns per step — negligible compared to step cost.
    TICK_EVERY_STEPS = 100000

    def flush_output_stream():
        # Stream whatever's been emitted so far to stdout (without a
        # trailing newline, since Output is already a byte stream).
        buf = simulator.output
        if buf:
            sys.stdout.write("".join(chr(c) for c in buf))
            sys.stdout.flush()
            buf.clear()

    try:
        while True:
            simulator.step()
            steps += 1
            if steps % TICK_EVERY_STEPS == 0:
                now = time.perf_counter()
                if now - last_tick >= 3.0:
                    flush_output_stream()
                    print(
                        f"[ursa-tick] {steps:,} steps, "
                        f"{steps / (now - t0):,.0f} steps/s, "
                        f"mem={len(simulator.memory):,}",
                        file=sys.stderr,
                        flush=True,
                    )
                    last_tick = now
    except StopIteration:
        elapsed = time.perf_counter() - t0
        flush_output_stream()
        print("\nProgram finished.")
        print(f"[ursa] {steps:,} steps in {elapsed:.3f}s ({steps/elapsed:,.0f} steps/s)", file=sys.stderr)
    except Exception as e:
        elapsed = time.perf_counter() - t0
        flush_output_stream()
        print(f"\nError during simulation: {e}")
        print(f"[ursa] {steps:,} steps in {elapsed:.3f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
