from assembler import parse_file
from simulator import Simulator


def main():
    import sys

    if len(sys.argv) != 2:
        print("Usage: python main.py <source_file>")
        sys.exit(1)

    source_file = sys.argv[1]
    program = parse_file(source_file)
    program.fixup_jumps()
    simulator = Simulator(program)

    try:
        while True:
            simulator.step()
    except StopIteration:
        print("Program finished.")
        print("Output:")
        for char in simulator.output:
            print(chr(char), end="")
        print()
    except Exception as e:
        print(f"Error during simulation: {e}")


if __name__ == "__main__":
    main()
