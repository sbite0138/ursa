#!/usr/bin/env python3
"""mtg-link: Text-based linker for MtG assembly files.

Merges multiple .s files produced by `clang --target=mtg` into a single
.s file that ursa can assemble and simulate.

Usage:
    python3 mtg-link.py file1.s file2.s ... -o output.s
"""

import argparse
import re
import sys


def parse_sections(lines, file_idx):
    """Split an assembly file into text lines, data lines, and .comm directives.

    Also renames local labels (.L...) to avoid collisions across files.
    """
    text_lines = []
    data_lines = []
    comms = {}  # name -> (size, align)
    in_data = False
    prefix = f".F{file_idx}_"

    def rename(line):
        """Rename .L-prefixed local labels/references to file-specific names."""
        return re.sub(r"\.L(\w+)", prefix + "L\\1", line)

    for line in lines:
        stripped = line.rstrip()
        bare = stripped.lstrip()

        # Skip empty lines and pure comments
        if not bare or bare.startswith("#"):
            continue

        # Strip inline comments for directive detection
        bare_no_comment = bare.split(";")[0].strip()
        if not bare_no_comment:
            continue

        first = bare_no_comment.split(None, 1)[0]

        # Section switching
        if first == ".text":
            in_data = False
            continue
        if first in (".data", ".bss"):
            in_data = True
            continue
        if first == ".section":
            rest = bare_no_comment[len(".section"):].strip()
            in_data = not (rest == ".text" or rest.startswith(".text,")
                          or rest.startswith(".text "))
            continue

        # .comm — can appear in any section
        if first == ".comm":
            parts = bare_no_comment[len(".comm"):].strip().split(",")
            name = parts[0].strip()
            size = int(parts[1].strip())
            align = int(parts[2].strip()) if len(parts) >= 3 else 1
            if name in comms:
                comms[name] = (max(size, comms[name][0]),
                               max(align, comms[name][1]))
            else:
                comms[name] = (size, align)
            continue

        # Skip metadata directives
        if first in (".file", ".size", ".type", ".globl", ".local", ".ident",
                      ".addrsig", ".addrsig_sym"):
            continue
        if first == ".section" or bare_no_comment.startswith('.section "'):
            continue

        if in_data:
            data_lines.append(rename(stripped))
        else:
            text_lines.append(rename(stripped))

    return text_lines, data_lines, comms


def extract_global_labels(lines):
    """Extract non-local label names (labels not starting with '.')."""
    labels = []
    for line in lines:
        stripped = line.strip()
        if stripped.endswith(":"):
            name = stripped[:-1].strip()
            # Skip comment-only lines that happen to have colons
            if ";" in name:
                name = name.split(";")[0].strip()
                if not name.endswith(":"):
                    continue
                name = name[:-1].strip()
            if name and not name.startswith("."):
                labels.append(name)
    return labels


def link(input_files, output_file):
    all_text = []
    all_data = []
    merged_comms = {}
    data_defined_symbols = set()
    seen_global_labels = {}  # name -> filename

    for file_idx, path in enumerate(input_files):
        with open(path) as f:
            lines = f.readlines()

        text_lines, data_lines, comms = parse_sections(lines, file_idx)

        # Check for duplicate global labels in .text
        for label in extract_global_labels(text_lines):
            if label in seen_global_labels:
                print(f"error: duplicate symbol '{label}' "
                      f"(in {seen_global_labels[label]} and {path})",
                      file=sys.stderr)
                sys.exit(1)
            seen_global_labels[label] = path

        # Track .data-defined symbols
        for label in extract_global_labels(data_lines):
            if label in seen_global_labels:
                print(f"error: duplicate symbol '{label}' "
                      f"(in {seen_global_labels[label]} and {path})",
                      file=sys.stderr)
                sys.exit(1)
            seen_global_labels[label] = path
            data_defined_symbols.add(label)

        all_text.append(text_lines)
        all_data.append(data_lines)

        # Merge .comm (max size, max align)
        for name, (size, align) in comms.items():
            if name in merged_comms:
                merged_comms[name] = (max(size, merged_comms[name][0]),
                                      max(align, merged_comms[name][1]))
            else:
                merged_comms[name] = (size, align)

    # .data definitions supersede .comm
    for sym in data_defined_symbols:
        merged_comms.pop(sym, None)

    # Write merged output
    with open(output_file, "w") as f:
        f.write("\t.text\n")
        for block in all_text:
            for line in block:
                f.write(line + "\n")

        # Only emit .data section if there's content
        if any(all_data) or merged_comms:
            f.write("\t.data\n")
            for block in all_data:
                for line in block:
                    f.write(line + "\n")
            for name, (size, align) in merged_comms.items():
                f.write(f"\t.comm {name},{size},{align}\n")

    print(f"[mtg-link] {len(input_files)} files -> {output_file} "
          f"({sum(len(b) for b in all_text)} text lines, "
          f"{sum(len(b) for b in all_data)} data lines, "
          f"{len(merged_comms)} .comm symbols)",
          file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Link multiple MtG assembly files into one")
    parser.add_argument("inputs", nargs="+", help="Input .s files")
    parser.add_argument("-o", "--output", required=True,
                        help="Output .s file")
    args = parser.parse_args()
    link(args.inputs, args.output)


if __name__ == "__main__":
    main()
