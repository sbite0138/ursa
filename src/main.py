import sys
import time

from assembler import parse_file
from simulator import Simulator


def main():
    if len(sys.argv) != 2:
        print("Usage: python main.py <source_file>")
        sys.exit(1)

    source_file = sys.argv[1]
    program = parse_file(source_file)
    program.fixup_jumps()
    simulator = Simulator(program)

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
