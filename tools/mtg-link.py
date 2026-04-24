#!/usr/bin/env python3
"""mtg-link: Text-based linker for MtG assembly files.

Merges multiple .s files produced by `clang --target=mtg` into a single
.s file that ursa can assemble and simulate. Also resolves each
`NumBuildAddr <sym>` meta-instruction (the only non-native opcode the
LLVM backend still emits) into four concrete NumBuild digit pairs, so
ursa sees a purely-native instruction stream.

Usage:
    python3 mtg-link.py file1.s file2.s ... -o output.s
"""

import argparse
import re
import sys


# Runtime base address for .data / .bss globals. Must match
# ursa/src/assembler.py's GLOBALS_BASE — they jointly define the ABI
# between mtg-link (address assignment) and ursa (memory layout).
GLOBALS_BASE = 1024


def _asciz_byte_count(operand, nul):
    """Count the bytes a `.asciz "…"` / `.ascii "…"` produces.

    Mirrors the escape-sequence decoder in ursa-rs's assembler so label
    addresses stay in sync with the ursa side. Returns the payload
    length plus 1 (for the trailing NUL) when `nul` is True.
    """
    operand = operand.strip()
    start = operand.find('"')
    if start < 0:
        return 0
    end = operand.rfind('"')
    if end <= start:
        return 0
    body = operand[start + 1:end]
    out = 0
    i = 0
    while i < len(body):
        c = body[i]
        if c != '\\':
            out += len(c.encode('utf-8'))
            i += 1
            continue
        i += 1
        if i >= len(body):
            break
        esc = body[i]
        i += 1
        if esc in ('n', 't', 'r', '0', '\\', '"', "'", 'a', 'b', 'f', 'v'):
            out += 1
        elif esc == 'x':
            # up to 2 hex digits
            consumed = 0
            while consumed < 2 and i < len(body) and body[i] in "0123456789abcdefABCDEF":
                i += 1
                consumed += 1
            out += 1
        elif esc in "01234567":
            # up to 3 octal digits (first already consumed as `esc`)
            consumed = 1
            while consumed < 3 and i < len(body) and body[i] in "01234567":
                i += 1
                consumed += 1
            out += 1
        else:
            # Unknown escape: copy both characters, matching GAS.
            out += 1 + len(esc.encode('utf-8'))
    if nul:
        out += 1
    return out


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
        """Rename file-local labels/references to file-specific names.

        Covers the two local-label conventions Clang emits:
        - `.L<name>` — generic local labels (basic-block targets, func-end
          markers, compiler-generated helpers)
        - `.str[.N]` — string-literal symbols in .rodata. Each TU minted
          its own `.str` et al., so if two files both use string literals
          they'd collide in ursa's global-label table.
        """
        line = re.sub(r"\.L(\w+)", prefix + "L\\1", line)
        line = re.sub(r"\.str(\.\d+)?\b", prefix + "str\\1", line)
        return line

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


def compute_data_addresses(all_data, merged_comms):
    """Return {symbol -> runtime byte address} for every .data / .comm global.

    Mirrors ursa/src/assembler.py's data-cursor logic exactly: .data
    blocks are walked in file order (same order they'll appear in our
    linked output), `.long` advances the cursor by 4, `.byte` by 1,
    `.p2align N` rounds up to the next 1<<N boundary; labels capture
    the current cursor. `.comm` globals are placed after all .data
    content, in insertion order of merged_comms, each respecting its
    alignment.
    """
    addrs = {}
    cursor = GLOBALS_BASE

    for block in all_data:
        for line in block:
            stripped = line.strip()
            if not stripped:
                continue
            # Strip inline comments to match ursa's parser
            bare = stripped.split(";")[0].strip()
            if not bare:
                continue
            if bare.endswith(":"):
                name = bare[:-1].strip()
                addrs[name] = cursor
                continue
            parts = bare.split(None, 1)
            if not parts:
                continue
            directive = parts[0]
            if directive == ".long" or directive == ".4byte":
                cursor += 4
            elif directive == ".byte":
                cursor += 1
            elif directive in (".short", ".2byte", ".hword"):
                # Keep in sync with ursa-rs's assembler: 2-byte LE integer.
                # Clang emits .short for CRC lookup tables at -O1, so
                # accounting for the size here is essential to keep later
                # label addresses correct.
                cursor += 2
            elif directive in (".zero", ".space"):
                n = int(parts[1].split(",")[0])
                cursor += n
            elif directive == ".p2align":
                pow2 = int(parts[1].split(",")[0])
                align = 1 << pow2
                if cursor % align != 0:
                    cursor += align - (cursor % align)
            elif directive in (".asciz", ".ascii"):
                # Measure the string literal so subsequent labels get the
                # right address. Mirror the escape-sequence handling in
                # ursa-rs's assembler. Clang emits .asciz for every
                # string literal, so getting this wrong makes every
                # format-string pointer off by the combined length of all
                # preceding strings.
                operand = parts[1] if len(parts) > 1 else ""
                cursor += _asciz_byte_count(operand, nul=(directive == ".asciz"))
            # Other directives (metadata) don't move the cursor.

    for name, (size, align) in merged_comms.items():
        if cursor % align != 0:
            cursor += align - (cursor % align)
        addrs[name] = cursor
        cursor += size

    return addrs


def expand_numbuildaddr(text_lines, addrs):
    """Replace each `NumBuildAddr <sym>` with 4 `NumBuild #X, #Y` pairs.

    The 4 pairs encode the symbol's resolved address as 4 base-144
    digits (MSB first), matching the shape ursa's fixup_jumps used to
    produce. Each NumBuild is written with the same leading whitespace
    as the original line so downstream tooling preserves formatting.
    """
    out = []
    for line in text_lines:
        stripped = line.strip()
        bare = stripped.split(";")[0].strip()
        if not bare or not bare.startswith("NumBuildAddr"):
            out.append(line)
            continue
        # NumBuildAddr is a unary pseudo: "NumBuildAddr <sym>".
        parts = bare.split(None, 1)
        if len(parts) < 2:
            raise ValueError(f"NumBuildAddr needs a symbol operand: {line!r}")
        sym = parts[1].strip()
        if sym not in addrs:
            raise ValueError(
                f"NumBuildAddr: unknown global '{sym}' "
                f"(known: {sorted(addrs)[:8]}{'...' if len(addrs) > 8 else ''})")
        addr = addrs[sym]
        # base-144 digits, MSB first.
        pairs = []
        v = addr
        for _ in range(4):
            pairs.append(v % 144)
            v //= 144
        pairs.reverse()
        indent = line[: len(line) - len(line.lstrip())]
        for d in pairs:
            out.append(f"{indent}NumBuild\t #{d // 12}, #{d % 12}")
    return out


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

    # Resolve NumBuildAddr: compute every global's final address from
    # the .data / .comm layout, then rewrite each NumBuildAddr
    # occurrence into 4 concrete NumBuild digit pairs. After this,
    # ursa sees only native instructions.
    global_addrs = compute_data_addresses(all_data, merged_comms)
    num_numbuildaddr = sum(
        1 for block in all_text for line in block
        if line.strip().split(";")[0].strip().startswith("NumBuildAddr")
    )
    all_text = [expand_numbuildaddr(block, global_addrs) for block in all_text]

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
          f"{len(merged_comms)} .comm symbols, "
          f"{num_numbuildaddr} NumBuildAddr resolved)",
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
