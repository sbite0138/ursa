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
    "Store",
    "Load",
    "Output",
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
]

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
    "Store": [8, -1, -1],
    "Load": [9, -1, -1],
    "Output": [4, 3, -1],
    "Return": [3, 7, -1],
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

    def add_instruction(self, instruction):
        self.instructions.append(instruction)

    def add_label(self, label):
        if label.name in self.labels:
            raise ValueError(f"Duplicate label: {label.name}")
        self.labels[label.name] = len(self.instructions)

    def __repr__(self):
        return f"Program(instructions={self.instructions}, labels={self.labels})"

    def fixup_jumps(self):
        for index, instr in enumerate(self.instructions):
            if instr.name in ["JumpFwd", "JumpFwdNF", "JumpBwdNF"]:
                # print(f"Fixing up jump at {index}: {instr}")
                # print(
                #     f"previous instruction: {self.instructions[index-1]}, {self.instructions[index-2]}"
                # )
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
                    if instr.name == "JumpFwd":
                        instr.name = "JumpBwd"
                    else:
                        assert instr.name == "JumpFwdNF"
                        instr.name = "JumpBwdNF"
                    target_value = -target_value
                elif target_value > 0 and instr.name.startswith("JumpBwd"):
                    if instr.name == "JumpBwd":
                        instr.name = "JumpFwd"
                    else:
                        assert instr.name == "JumpBwdNF"
                        instr.name = "JumpFwdNF"
                instr.args = ["r0"]
                numbuild_args = []
                for _ in range(4):
                    numbuild_args.append("#" + str(target_value % 12))
                    target_value //= 12
                numbuild_args.reverse()
                self.instructions[index - 2].args = [numbuild_args[0], numbuild_args[1]]
                self.instructions[index - 1].args = [numbuild_args[2], numbuild_args[3]]
            elif instr.name == "Return":
                assert (
                    self.instructions[index - 1].name == "NumBuild"
                    and self.instructions[index - 2].name == "NumBuild"
                )
                instr.args = ["r0"]
                target_value = len(self.instructions)
                numbuild_args = []
                for _ in range(4):
                    numbuild_args.append("#" + str(target_value % 12))
                    target_value //= 12
                numbuild_args.reverse()
                self.instructions[index - 2].args = [numbuild_args[0], numbuild_args[1]]
                self.instructions[index - 1].args = [numbuild_args[2], numbuild_args[3]]

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
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):  # Ignore empty lines and comments
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
