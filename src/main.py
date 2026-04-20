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
    args = parser.parse_args()

    program = parse_file(args.source)
    program.fixup_jumps()
    simulator = Simulator(program)

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
    try:
        while True:
            simulator.step()
            steps += 1
    except StopIteration:
        elapsed = time.perf_counter() - t0
        print("Program finished.")
        print("Output:")
        for char in simulator.output:
            print(chr(char), end="")
        print()
        print(f"[ursa] {steps:,} steps in {elapsed:.3f}s ({steps/elapsed:,.0f} steps/s)", file=sys.stderr)
    except Exception as e:
        elapsed = time.perf_counter() - t0
        print(f"Error during simulation: {e}")
        print(f"[ursa] {steps:,} steps in {elapsed:.3f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
