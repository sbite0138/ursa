



INSTRUCTIONS=[
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
    "JumpFwdNF",
    "JumpBwdNF",
    "Store",
    "Load",
    "Output",
    "Return",
]

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
               print(f"Fixing up jump at {index}: {instr}")
               print(f"previous instruction: {self.instructions[index-1]}, {self.instructions[index-2]}")
               assert (self.instructions[index-1].name == "NumBuild" and self.instructions[index-2].name == "NumBuild")
            #    fix up numbuild to be the correct offset

def parse_line(line):
    # trim comments Foo ; this is a comment
    line = line.split(';')[0].strip()
    if not line:
        return None
    if line.endswith(':'):
        return Label(line[:-1].strip())
    # instructions like Foo arg1, arg2
    parts = line.split(None, 1)
    name = parts[0]
    args = []
    if len(parts) > 1:
        args = [arg.strip() for arg in parts[1].split(',')]
    if name not in INSTRUCTIONS:
        if not name.startswith('.'):
            print(f"Warning: Unknown instruction '{name}'")
        return None
    return Instruction(name, args)


def parse_file(file_path):
    with open(file_path, 'r') as file:
        lines = file.readlines()
    
    program = Program()
    for line in lines:
        line = line.strip()
        if line and not line.startswith('#'):  # Ignore empty lines and comments
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
    print(parsed)