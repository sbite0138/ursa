from assembler import Instruction, Label, Program, INSTRUCTIONS, MACRO_INSTRUCTIONS


class Simulator:
    def __init__(self, program: Program, mem_default_zero: bool = False):
        self.program = program
        self.registers = [0] * 12
        self.flag = False
        self.is_flag_combining = False
        self.is_num_building = False
        self.pc = 0  # Program counter
        self.memory = {}
        # When True, reads from uninitialized memory return 0 instead of
        # raising. Real kernels (Linux, xv6, etc.) rely on standard BSS
        # semantics and touch large uninitialized regions; the explicit
        # error is useful for debugging small programs but gets in the
        # way for full-system boots. Toggled via ursa main.py --zero-mem.
        self.mem_default_zero = mem_default_zero
        self.output = []
        # Return-address stack pushed by Call* and popped by Return. The MtG
        # spec specifies the pushed value as an "S" used in Return's
        # PC += max(0, S - 3·Z') formula (where Z' is the callee's
        # entry-to-return instruction distance); for simulation purposes we
        # just save the straight-line return address. A Return with an empty
        # stack halts the program — that's how _start exits.
        self.return_stack = []
        self.registers[1] = 128
        self.registers[2] = 128
        # Pre-populate memory with any global initialisers from the .data
        # section (parsed by the assembler). Globals live above the stack
        # at addresses >= GLOBALS_BASE.
        for addr, byte_val in program.global_init.items():
            self.memory[addr] = byte_val
        # Start execution at the `_start` label if defined, otherwise at the
        # beginning of the program. Clang / LLVM don't guarantee _start is
        # first in the asm — the user typically defines helpers alongside it.
        if "_start" in program.labels:
            self.pc = program.labels["_start"]

    def getImm(self, instr: Instruction, index: int) -> int:
        if index >= len(instr.args):
            raise ValueError(
                f"Instruction {instr.name} expects at least {index+1} arguments, got {len(instr.args)}"
            )
        arg = instr.args[index]
        if not arg.startswith("#"):
            raise ValueError(f"Expected immediate value starting with '#', got {arg}")
        try:
            return int(arg[1:])
        except ValueError:
            raise ValueError(f"Invalid immediate value: {arg}")

    def getRegIndex(self, instr: Instruction, index: int) -> int:
        if index >= len(instr.args):
            raise ValueError(
                f"Instruction {instr.name} expects at least {index+1} arguments, got {len(instr.args)}"
            )
        arg = instr.args[index]
        if not arg.startswith("r"):
            raise ValueError(f"Expected register starting with 'R', got {arg}")
        try:
            reg_index = int(arg[1:])
            if reg_index < 0 or reg_index >= len(self.registers):
                raise ValueError(f"Register index out of bounds: {reg_index}")
            return reg_index
        except ValueError:
            raise ValueError(f"Invalid register: {arg}")

    def step(self):
        # Hot path. Keeps per-step cost down by:
        #   (1) reading pre-parsed integer args from `instr.iargs` so we
        #       never re-parse "rN"/"#N" strings (profile showed this
        #       was ~50% of time);
        #   (2) aliasing `self.registers` / `self.program.instructions`
        #       / `self.memory` into locals so each uses one bytecode op
        #       instead of two attribute lookups per access;
        #   (3) dropping the `instr.name in INSTRUCTIONS` membership
        #       check — an unknown name falls through to the else
        #       branch below, same user-visible error.
        regs = self.registers
        instrs = self.program.instructions
        n_instrs = len(instrs)
        pc = self.pc
        if pc >= n_instrs:
            raise StopIteration("End of program")
        instr = instrs[pc]
        name = instr.name
        a = instr.iargs
        mem = self.memory
        flag = self.flag
        is_num_building = self.is_num_building
        is_flag_combining = self.is_flag_combining

        next_is_flag_combining = False
        next_is_num_building = False

        if name == "NumBuild":
            imm = a[0] * 12 + a[1]
            if is_num_building:
                regs[0] = regs[0] * 144 + imm
            else:
                regs[0] = imm
            next_is_num_building = True
        elif name == "Add":
            regs[a[0]] += regs[a[1]]
        elif name == "SubCond":
            d = a[0]; s = a[1]
            vd = regs[d]; vs = regs[s]
            if vd >= vs:
                regs[d] = vd - vs
                flag = False
            else:
                flag = True
        elif name == "Mult":
            regs[a[0]] *= regs[a[1]]
        elif name == "Move":
            regs[a[0]] = regs[a[1]]
        elif name == "Load":
            addr = regs[a[1]]
            v = mem.get(addr)
            if v is None:
                if not self.mem_default_zero:
                    raise ValueError(f"Memory read from uninitialized address: {addr}")
                v = 0
            regs[a[0]] = v
        elif name == "Store":
            mem[regs[a[1]]] = regs[a[0]]
        elif name == "Add1":
            regs[a[0]] += 1
        elif name == "Sub1Cond":
            d = a[0]
            if regs[d] > 0:
                regs[d] -= 1
                flag = False
            else:
                flag = True
        elif name == "Zero":
            regs[a[0]] = 0
        elif name == "Divide":
            d = a[0]
            quot, rem = divmod(regs[d], regs[0])
            regs[d] = rem
            regs[6] = quot
            flag = quot != 0
        elif name == "SetF":
            regs[a[0]] = 1 if flag else 0
        elif name == "SetNF":
            regs[a[0]] = 0 if flag else 1
        elif name == "FIsZero":
            cmp = regs[a[0]] == 0
            flag = (flag or cmp) if is_flag_combining else cmp
            next_is_flag_combining = True
        elif name == "FLess":
            # Spec: "11 Y Z | flag = (rZ < rY)". Op order is (Y, Z), so
            # flag = (regs[second] < regs[first]).
            cmp = regs[a[1]] < regs[a[0]]
            flag = (flag or cmp) if is_flag_combining else cmp
            next_is_flag_combining = True
        elif name == "Halve":
            d = a[0]
            flag = (regs[d] & 1) == 1
            regs[d] >>= 1
        elif name == "JumpFwd":
            pc += regs[a[0]]
            if not (0 <= pc < n_instrs):
                raise AssertionError(f"Jump out of bounds: dst_pc={pc}")
        elif name == "JumpBwd":
            pc -= regs[a[0]]
            if not (0 <= pc < n_instrs):
                raise AssertionError(f"Jump out of bounds: dst_pc={pc}")
        elif name == "JumpFwdNF":
            if not flag:
                pc += regs[a[0]]
                if not (0 <= pc < n_instrs):
                    raise AssertionError(f"Jump out of bounds: dst_pc={pc}")
        elif name == "JumpBwdNF":
            if not flag:
                pc -= regs[a[0]]
                if not (0 <= pc < n_instrs):
                    raise AssertionError(f"Jump out of bounds: dst_pc={pc}")
        elif name == "JumpFwdF":
            if flag:
                pc += regs[a[0]]
                if not (0 <= pc < n_instrs):
                    raise AssertionError(f"Jump out of bounds: dst_pc={pc}")
        elif name == "JumpBwdF":
            if flag:
                pc -= regs[a[0]]
                if not (0 <= pc < n_instrs):
                    raise AssertionError(f"Jump out of bounds: dst_pc={pc}")
        elif name == "Output":
            self.output.append(regs[a[0]])
        elif name == "CallFwd":
            self.return_stack.append(pc + 1)
            pc += regs[a[0]]
        elif name == "CallBwd" or name == "CallBwdR":
            self.return_stack.append(pc + 1)
            pc -= regs[a[0]]
        elif name == "Return":
            if not self.return_stack:
                raise StopIteration("Program returned")
            pc = self.return_stack.pop() - 1
        elif name == "AInput" or name == "BInput":
            who = "Alice" if name == "AInput" else "Bob"
            try:
                line = input(f"{who} input> ").strip()
                value = int(line)
            except (EOFError, ValueError):
                value = 0
            regs[a[0]] = value & 0xFFFFFFFF
        else:
            if name not in INSTRUCTIONS and name not in MACRO_INSTRUCTIONS:
                raise ValueError(f"Unknown instruction: {name}")
            raise ValueError(f"Unhandled instruction: {name}")

        self.pc = pc + 1
        self.flag = flag
        self.is_num_building = next_is_num_building
        self.is_flag_combining = next_is_flag_combining
