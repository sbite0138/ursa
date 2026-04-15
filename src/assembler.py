INSTRUCTIONS = [
    "Move",
    "Zero",
    "NumBuild",
    "Add",
    "Add1",
    "SubCond",
    "Sub1Cond",
    "Mult",
    "Divide",
    "SetF",
    "SetNF",
    "FIsZero",
    "FLess",
    "Halve",
    "JumpFwd",
    "JumpBwd",
    "JumpFwdNF",
    "JumpBwdNF",
    "JumpFwdF",
    "JumpBwdF",
    "Store",
    "Load",
    "Output",
    "AInput",
    "BInput",
    "CallFwd",
    "CallBwd",
    "CallBwdR",
    "Return",
]

MACRO_INSTRUCTIONS = [
    "ADD_IMM_MACRO",
    "ADD_MACRO",
    "NUMBUILD_MACRO",
    "LOADBYTEWISE_MACRO",
    "STOREBYTEWISE_MACRO",
    "RET_PSEUDO",
    #
    "EQ_MACRO",
    "NEQ_MACRO",
    "LT_MACRO",
    "GT_MACRO",
    "DIV_MACRO",
    "REM_MACRO",
    # NumBuildAddr <sym> is a meta-instruction emitted by the LLVM
    # backend's MOVEADDR_MACRO expansion. fixup_jumps replaces each
    # occurrence with the 4 NumBuild digit pairs that encode the
    # symbol's runtime address into r0.
    "NumBuildAddr",
]

# Where global variables (data section objects) start in MtG memory.
# The stack lives below 128 (registers[1] / registers[2] start there in
# the simulator), so picking 0x400 = 1024 leaves a comfortable gap.
GLOBALS_BASE = 1024

INSTRUCTION_TO_OPCODE = {
    "Move": [5, -1, -1],
    "Zero": [5, -2, -2],
    "NumBuild": [10, -1, -1],
    "Add": [0, -1, -1],
    "Add1": [4, 1, -1],
    "SubCond": [1, -1, -1],
    "Sub1Cond": [1, -2, -2],
    "Mult": [6, -1, -1],
    "Divide": [4, 6, -1],
    "SetF": [2, -1, 2],
    "SetNF": [2, -1, 3],
    "FIsZero": [11, -2, -2],
    "FLess": [11, -1, -1],
    "Halve": [4, 2, -1],
    "JumpFwd": [3, 0, -1],
    "JumpBwd": [3, 1, -1],
    "JumpFwdNF": [3, 2, -1],
    "JumpBwdNF": [3, 3, -1],
    "JumpFwdF": [3, 4, -1],
    "JumpBwdF": [3, 5, -1],
    "Store": [8, -1, -1],
    "Load": [9, -1, -1],
    "Output": [4, 3, -1],
    "AInput": [2, -1, 0],
    "BInput": [2, -1, 1],
    "Return": [3, 7, -1],
    "CallFwd": [3, 6, 0],
    "CallBwd": [3, 6, 1],
    "CallBwdR": [3, 6, 2],
}

NUMBER_TO_CARD = {
    0: "Plains",
    1: "Island",
    2: "Swamp",
    3: "Mountain",
    4: "Forest",
    5: "Wastes",
    6: "Snow-Covered Plains",
    7: "Snow-Covered Island",
    8: "Snow-Covered Swamp",
    9: "Snow-Covered Mountain",
    10: "Snow-Covered Forest",
    11: "Snow-Covered Wastes",
}


class Instruction:
    def __init__(self, name, args):
        self.name = name
        self.args = args

    def __repr__(self):
        return f"Instruction(name={self.name}, args={self.args})"


class Label:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"Label(name={self.name})"


class Program:
    def __init__(self):
        self.instructions = []
        self.labels = {}
        # Global symbols defined in the .data section: name -> address.
        # The address is a byte offset into MtG memory; values are
        # initialised by the simulator before execution starts.
        self.globals = {}
        # Pre-execution memory image: address -> byte. Populated from
        # .long / .byte directives parsed in the .data section.
        self.global_init = {}

    def add_instruction(self, instruction):
        self.instructions.append(instruction)

    def add_label(self, label):
        if label.name in self.labels:
            raise ValueError(f"Duplicate label: {label.name}")
        self.labels[label.name] = len(self.instructions)

    def add_global(self, name, addr):
        if name in self.globals:
            raise ValueError(f"Duplicate global: {name}")
        self.globals[name] = addr

    def init_memory_long(self, addr, value):
        # Little-endian 32-bit byte split.
        for i in range(4):
            self.global_init[addr + i] = (value >> (i * 8)) & 0xFF

    def init_memory_byte(self, addr, value):
        self.global_init[addr] = value & 0xFF

    def __repr__(self):
        return f"Program(instructions={self.instructions}, labels={self.labels})"

    def _patch_numbuild_pair(self, index, value):
        # value must be non-negative. Split into 4 base-12 digits and write
        # into the 2 NumBuild instructions at index-2 and index-1.
        assert value >= 0
        numbuild_args = []
        v = value
        for _ in range(4):
            numbuild_args.append("#" + str(v % 12))
            v //= 12
        numbuild_args.reverse()
        self.instructions[index - 2].args = [numbuild_args[0], numbuild_args[1]]
        self.instructions[index - 1].args = [numbuild_args[2], numbuild_args[3]]

    def _function_entry_index(self, instr_index):
        # Return the instruction-stream index of the nearest label at or
        # before instr_index that doesn't look like a compiler-generated
        # local MBB label (convention: local labels start with "."). That is
        # the function entry point per the LLVM AsmPrinter.
        best = 0
        for name, idx in self.labels.items():
            if name.startswith("."):
                continue
            if idx <= instr_index and idx > best:
                best = idx
        return best

    def expand_global_addr_pseudos(self):
        # Replace each `NumBuildAddr <sym>` with 4 NumBuild digit pairs
        # whose base-144 encoding produces the symbol's runtime address
        # in r0. This is done before fixup_jumps so the new instructions
        # are part of the linear stream that fixup_jumps walks.
        new_instrs = []
        relabel = {}  # old index -> new index, for label fix-up below.
        for old_idx, instr in enumerate(self.instructions):
            relabel[old_idx] = len(new_instrs)
            if instr.name == "NumBuildAddr":
                if not instr.args:
                    raise ValueError("NumBuildAddr needs a symbol operand")
                sym = instr.args[0]
                if sym not in self.globals:
                    raise ValueError(f"NumBuildAddr: unknown global '{sym}'")
                addr = self.globals[sym]
                # Encode `addr` as 4 base-144 digits, MSB first, mirroring
                # how MOVEIMM_MACRO expands a constant.
                pairs = []
                v = addr
                for _ in range(4):
                    pairs.append(v % 144)
                    v //= 144
                pairs.reverse()
                for d in pairs:
                    new_instrs.append(
                        Instruction("NumBuild",
                                    ["#" + str(d // 12), "#" + str(d % 12)]))
            else:
                new_instrs.append(instr)
        # Recompute label positions: every label that pointed at old_idx
        # now points at relabel[old_idx]. (The next instruction's new
        # position, since labels are stored as "instruction index of the
        # next instruction".)
        new_labels = {}
        for name, old_pos in self.labels.items():
            new_labels[name] = relabel.get(old_pos, len(new_instrs))
        self.instructions = new_instrs
        self.labels = new_labels

    def fixup_jumps(self):
        # Resolve global-address pseudos first so the resulting NumBuilds
        # are present when we later look up label / call positions.
        self.expand_global_addr_pseudos()
        # MtG's Jump / Call / Return all use the "NumBuild NumBuild <op>"
        # pattern where the two NumBuilds materialize a magnitude into r0
        # and <op> consumes r0 as a PC-relative displacement (Jump/Call) or
        # an instruction-count offset from the function entry (Return).
        # LLVM emits the NumBuilds as "#0, #0" placeholders; here we fill in
        # the real value based on the final program layout — the same role a
        # real assembler/linker would play for targets like RISC-V.
        JUMP_NAMES = {
            "JumpFwd",
            "JumpBwd",
            "JumpFwdNF",
            "JumpBwdNF",
            "JumpFwdF",
            "JumpBwdF",
        }
        CALL_NAMES = {"CallFwd", "CallBwd", "CallBwdR"}
        for index, instr in enumerate(self.instructions):
            if instr.name in JUMP_NAMES:
                assert (
                    self.instructions[index - 1].name == "NumBuild"
                    and self.instructions[index - 2].name == "NumBuild"
                )
                target_label = instr.args[0]
                if target_label not in self.labels:
                    raise ValueError(f"Undefined label: {target_label}")
                target_index = self.labels[target_label]
                target_value = target_index - index - 1
                if target_value < 0 and instr.name.startswith("JumpFwd"):
                    # Flip Fwd → Bwd, preserving the NF / F / "" suffix.
                    instr.name = "JumpBwd" + instr.name[len("JumpFwd"):]
                elif target_value > 0 and instr.name.startswith("JumpBwd"):
                    instr.name = "JumpFwd" + instr.name[len("JumpBwd"):]
                # The hardware subtracts r0 on JumpBwd, so encode the magnitude.
                if target_value < 0:
                    target_value = -target_value
                instr.args = ["r0"]
                self._patch_numbuild_pair(index, target_value)
            elif instr.name in CALL_NAMES:
                assert (
                    self.instructions[index - 1].name == "NumBuild"
                    and self.instructions[index - 2].name == "NumBuild"
                )
                target_label = instr.args[0]
                if target_label not in self.labels:
                    raise ValueError(f"Undefined call target: {target_label}")
                target_index = self.labels[target_label]
                target_value = target_index - index - 1
                # Flip CallFwd → CallBwdR if the target is behind us. We
                # never emit CallBwd (only CallBwdR), since BwdR is safe
                # for all backward calls including self-recursion (see the
                # MtG spec notes in the backend README).
                if target_value < 0 and instr.name == "CallFwd":
                    instr.name = "CallBwdR"
                elif target_value > 0 and instr.name in ("CallBwd", "CallBwdR"):
                    instr.name = "CallFwd"
                if target_value < 0:
                    target_value = -target_value
                instr.args = ["r0"]
                self._patch_numbuild_pair(index, target_value)
            elif instr.name == "Return":
                assert (
                    self.instructions[index - 1].name == "NumBuild"
                    and self.instructions[index - 2].name == "NumBuild"
                )
                instr.args = ["r0"]
                # Z' for Return is the instruction-count distance from this
                # Return back to its containing function's entry label.
                entry_index = self._function_entry_index(index)
                target_value = index - entry_index
                self._patch_numbuild_pair(index, target_value)

    def to_assembly(self):
        lines = []
        for instr in self.instructions:
            lines.append(f"{instr.name} " + ", ".join(instr.args))
        for label, idx in self.labels.items():
            lines.insert(idx, f"{label}:")
        return "\n".join(lines)

    def args_to_numbers(self, args):
        numbers = []
        for arg in args:
            if arg.startswith("r"):
                reg_num = int(arg[1:])
                if not (0 <= reg_num <= 11):
                    raise ValueError(f"Register out of range: {arg}")
                numbers.append(reg_num)
            elif arg.startswith("#"):
                imm_value = int(arg[1:])
                if not (0 <= imm_value <= 11):
                    raise ValueError(f"Immediate value out of range: {arg}")
                numbers.append(imm_value)
            else:
                raise ValueError(f"Invalid argument: {arg}")
        return numbers

    def to_MTG_cards(self):
        cards = []
        for instr in self.instructions:
            opcode = INSTRUCTION_TO_OPCODE.get(instr.name)
            arg_numbers = self.args_to_numbers(instr.args)
            if opcode is None:
                raise ValueError(f"Unknown instruction: {instr.name}")
            for i in range(3):
                # TODO: Make the implementation a bit more refined
                if opcode[i] == -1:
                    opcode[i] = arg_numbers[0]
                    arg_numbers.pop(0)
                elif opcode[i] == -2:
                    opcode[i] = arg_numbers[0]

            # print(f"Encoding {instr} as {opcode} (args: {arg_numbers})")
            for code in opcode:
                card_name = NUMBER_TO_CARD.get(code)
                if card_name is None:
                    raise ValueError(f"Unknown opcode number: {code}")
                cards.append(card_name)
        return cards


def parse_line(line):
    # trim comments Foo ; this is a comment
    line = line.split(";")[0].strip()
    if not line:
        return None
    if line.endswith(":"):
        return Label(line[:-1].strip())
    # instructions like Foo arg1, arg2
    parts = line.split(None, 1)
    name = parts[0]
    args = []
    if len(parts) > 1:
        args = [arg.strip() for arg in parts[1].split(",")]
    if name not in INSTRUCTIONS and name not in MACRO_INSTRUCTIONS:
        if not name.startswith("."):
            raise ValueError(f"Unknown instruction '{name}'")
        else:
            return None
    return Instruction(name, args)


def parse_file(file_path):
    with open(file_path, "r") as file:
        lines = file.readlines()

    program = Program()
    # Section state. We walk the asm top-to-bottom and switch between
    # `.text` (default) and `.data`. Inside `.data`, labels become
    # global-symbol declarations and `.long` / `.byte` directives
    # populate the initial memory image.
    in_data = False
    data_cursor = GLOBALS_BASE

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Strip inline comments before peeking at directive type.
        stripped = line.split(";")[0].strip()
        if not stripped:
            continue

        # Section directives. ".text" puts us in instruction-stream
        # mode; ".data", ".bss", or any explicit ".section <other>"
        # puts us in data mode. .bss is reached via
        # `.section .bss,"aw",@nobits` rather than a bare `.bss`.
        first = stripped.split(None, 1)[0]
        if first == ".text":
            in_data = False
            continue
        if first == ".data" or first == ".bss":
            in_data = True
            continue
        if first == ".section":
            rest = stripped[len(".section"):].strip()
            in_data = not (rest == ".text" or rest.startswith(".text,") or
                           rest.startswith(".text "))
            continue

        if in_data:
            # Label inside .data → global symbol declaration. We assign
            # the symbol the current data_cursor address.
            if stripped.endswith(":"):
                name = stripped[:-1].strip()
                program.add_global(name, data_cursor)
                continue
            # `.long N` (4 bytes), `.byte N` (1 byte). Other directives
            # (.size, .type, .globl, .p2align, .ident, .section, .addrsig*)
            # are metadata we don't need to act on.
            parts = stripped.split(None, 1)
            directive = parts[0]
            if directive == ".long":
                value = int(parts[1].split()[0], 0)
                program.init_memory_long(data_cursor, value)
                data_cursor += 4
            elif directive == ".byte":
                value = int(parts[1].split()[0], 0)
                program.init_memory_byte(data_cursor, value)
                data_cursor += 1
            elif directive == ".p2align":
                # Pad data_cursor up to the requested alignment so the
                # next label sits on a 2^N boundary.
                pow2 = int(parts[1].split(",")[0])
                align = 1 << pow2
                if data_cursor % align != 0:
                    data_cursor += align - (data_cursor % align)
            # Everything else in .data is metadata — ignore.
            continue

        # .text content goes through the regular instruction parser.
        parsed_line = parse_line(line)
        if parsed_line:
            if isinstance(parsed_line, Instruction):
                program.add_instruction(parsed_line)
            elif isinstance(parsed_line, Label):
                program.add_label(parsed_line)

    return program


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python assembler.py <source_file>")
        sys.exit(1)

    source_file = sys.argv[1]
    parsed = parse_file(source_file)
    parsed.fixup_jumps()
    # assembly = parsed.to_assembly()
    # print(assembly)
    cards = parsed.to_MTG_cards()
    print(f"Total cards: {len(cards)}")
    for card in cards:
        print(card)
