from assembler import Instruction, Label, Program, INSTRUCTIONS, MACRO_INSTRUCTIONS


class Simulator:
    def __init__(self, program: Program):
        self.program = program
        self.registers = [0] * 12
        self.flag = False
        self.is_flag_combining = False
        self.is_num_building = False
        self.pc = 0  # Program counter
        self.memory = {}
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
        if self.pc >= len(self.program.instructions):
            raise StopIteration("End of program")
        instr = self.program.instructions[self.pc]
        # print(
        #     f"PC: {self.pc}, Executing: {instr}, Registers: {[(-(2**32-reg) if reg >= 2**31 else reg) for reg in self.registers]}, Flag: {self.flag}"
        # )
        # print(self.memory)
        next_is_flag_combining = False
        next_is_num_building = False

        if instr.name not in INSTRUCTIONS and instr.name not in MACRO_INSTRUCTIONS:
            raise ValueError(f"Unknown instruction: {instr.name}")
        if instr.name == "NumBuild":
            imm = self.getImm(instr, 0) * 12 + self.getImm(instr, 1)
            if self.is_num_building:
                self.registers[0] = self.registers[0] * 144 + imm
            else:
                self.registers[0] = imm
            next_is_num_building = True
        elif instr.name == "Move":
            dst = self.getRegIndex(instr, 0)
            src = self.getRegIndex(instr, 1)
            if dst == src:
                raise ValueError(
                    "Move instruction cannot have the same source and destination register"
                )
            self.registers[dst] = self.registers[src]
        elif instr.name == "Zero":
            dst = self.getRegIndex(instr, 0)
            self.registers[dst] = 0
        elif instr.name == "Add":
            dst = self.getRegIndex(instr, 0)
            src = self.getRegIndex(instr, 1)
            self.registers[dst] += self.registers[src]
        elif instr.name == "Add1":
            dst = self.getRegIndex(instr, 0)
            self.registers[dst] += 1
        elif instr.name == "SubCond":
            dst = self.getRegIndex(instr, 0)
            src = self.getRegIndex(instr, 1)
            if dst == src:
                raise ValueError(
                    "SubCond instruction cannot have the same source and destination register"
                )
            if self.registers[dst] >= self.registers[src]:
                self.registers[dst] -= self.registers[src]
                self.flag = False
            else:
                self.flag = True
        elif instr.name == "Sub1Cond":
            dst = self.getRegIndex(instr, 0)
            if self.registers[dst] > 0:
                self.registers[dst] -= 1
                self.flag = False
            else:
                self.flag = True
        elif instr.name == "Mult":
            dst = self.getRegIndex(instr, 0)
            src = self.getRegIndex(instr, 1)
            self.registers[dst] *= self.registers[src]
        elif instr.name == "Divide":
            dst = self.getRegIndex(instr, 0)
            assert (
                dst != 0 and dst != 6 and self.registers[0] != 0
            ), "Division by zero or invalid destination register"
            quot = self.registers[dst] // self.registers[0]
            rem = self.registers[dst] % self.registers[0]
            self.registers[dst] = rem
            self.registers[6] = quot
            self.flag = quot != 0
        elif instr.name == "SetF":
            dst = self.getRegIndex(instr, 0)
            self.registers[dst] = 1 if self.flag else 0
        elif instr.name == "SetNF":
            dst = self.getRegIndex(instr, 0)
            self.registers[dst] = 0 if self.flag else 1
        elif instr.name == "FIsZero":
            src = self.getRegIndex(instr, 0)
            if self.is_flag_combining:
                self.flag |= self.registers[src] == 0
            else:
                self.flag = self.registers[src] == 0
            next_is_flag_combining = True
        elif instr.name == "FLess":
            # Spec: "11 Y Z | flag = (rZ < rY)". The first operand is Y,
            # the second is Z, so flag ends up as (second < first).
            src0 = self.getRegIndex(instr, 0)
            src1 = self.getRegIndex(instr, 1)
            if src0 == src1:
                raise ValueError(
                    "FLess instruction cannot have the same source registers"
                )
            cmp = self.registers[src1] < self.registers[src0]
            if self.is_flag_combining:
                self.flag |= cmp
            else:
                self.flag = cmp
            next_is_flag_combining = True
        elif instr.name == "Halve":
            dst = self.getRegIndex(instr, 0)
            self.flag = self.registers[dst] % 2 == 1
            self.registers[dst] //= 2
        elif instr.name == "JumpFwd":
            src = self.getRegIndex(instr, 0)
            offset = self.registers[src]
            self.pc += offset
            assert 0 <= self.pc < len(self.program.instructions), "Jump out of bounds"
        elif instr.name == "JumpBwd":
            src = self.getRegIndex(instr, 0)
            offset = self.registers[src]
            self.pc -= offset
            assert 0 <= self.pc < len(self.program.instructions), "Jump out of bounds"
        elif instr.name == "JumpFwdNF":
            if not self.flag:
                src = self.getRegIndex(instr, 0)
                offset = self.registers[src]
                self.pc += offset
                assert (
                    0 <= self.pc < len(self.program.instructions)
                ), "Jump out of bounds"
        elif instr.name == "JumpBwdNF":
            if not self.flag:
                src = self.getRegIndex(instr, 0)
                offset = self.registers[src]
                self.pc -= offset
                assert (
                    0 <= self.pc < len(self.program.instructions)
                ), "Jump out of bounds"
        elif instr.name == "JumpFwdF":
            if self.flag:
                src = self.getRegIndex(instr, 0)
                offset = self.registers[src]
                self.pc += offset
                assert (
                    0 <= self.pc < len(self.program.instructions)
                ), "Jump out of bounds"
        elif instr.name == "JumpBwdF":
            if self.flag:
                src = self.getRegIndex(instr, 0)
                offset = self.registers[src]
                self.pc -= offset
                assert (
                    0 <= self.pc < len(self.program.instructions)
                ), "Jump out of bounds"
        elif instr.name == "Store":
            # Spec: "8 Y Z | mem[rZ] = rY" — first operand is the value,
            # second is the address.
            src0 = self.getRegIndex(instr, 0)
            src1 = self.getRegIndex(instr, 1)
            self.memory[self.registers[src1]] = self.registers[src0]
        elif instr.name == "Load":
            dst = self.getRegIndex(instr, 0)
            src = self.getRegIndex(instr, 1)
            addr = self.registers[src]
            if addr not in self.memory:
                raise ValueError(f"Memory read from uninitialized address: {addr}")
            self.registers[dst] = self.memory.get(addr, 0)
        elif instr.name == "Output":
            src = self.getRegIndex(instr, 0)
            self.output.append(self.registers[src])
            # print(f"Output: {self.registers[src]}")
        elif instr.name == "AInput" or instr.name == "BInput":
            dst = self.getRegIndex(instr, 0)
            who = "Alice" if instr.name == "AInput" else "Bob"
            try:
                line = input(f"{who} input> ").strip()
                value = int(line)
            except (EOFError, ValueError):
                value = 0
            self.registers[dst] = value & 0xFFFFFFFF
        elif instr.name == "CallFwd":
            src = self.getRegIndex(instr, 0)
            offset = self.registers[src]
            # Push the return address (next instruction) and jump forward.
            # step() appends +1 to pc after us, so we leave pc one short of
            # the target — mirroring how Jumps are handled.
            self.return_stack.append(self.pc + 1)
            self.pc += offset
        elif instr.name == "CallBwd" or instr.name == "CallBwdR":
            src = self.getRegIndex(instr, 0)
            offset = self.registers[src]
            self.return_stack.append(self.pc + 1)
            # The target may be instruction 0, so pc can legitimately hit -1
            # here — step() will roll it forward by 1 before the next fetch.
            self.pc -= offset
        elif instr.name == "Return":
            # Empty stack => returning from the outermost function (_start).
            # The program is done. Non-empty stack => pop and jump back.
            if not self.return_stack:
                raise StopIteration("Program returned")
            # step() adds +1 to pc at the end; subtract 1 so we land exactly
            # on the saved return address.
            self.pc = self.return_stack.pop() - 1
        elif instr.name == "ADD_IMM_MACRO":
            dst = self.getRegIndex(instr, 0)
            imm = self.getImm(instr, 1)
            self.registers[dst] += imm
        elif instr.name == "ADD_MACRO":
            dst = self.getRegIndex(instr, 0)
            src = self.getRegIndex(instr, 1)
            self.registers[dst] += self.registers[src]
        elif instr.name == "NUMBUILD_MACRO":
            imm = self.getImm(instr, 0)
            self.registers[0] = imm
        elif instr.name == "LOADBYTEWISE_MACRO":
            dst = self.getRegIndex(instr, 0)
            addr = self.getRegIndex(instr, 1)
            # print(f"Loading bytewise into {dst} from address in {self.registers[addr]}")
            val = 0
            for i in range(4):
                assert (
                    self.registers[addr] + i in self.memory
                ), f"Memory read from uninitialized address: {self.registers[addr] + i}"
                val |= self.memory.get(self.registers[addr] + i, 0) << (i * 8)
            self.registers[dst] = val
        elif instr.name == "STOREBYTEWISE_MACRO":
            src = self.getRegIndex(instr, 0)
            addr = self.getRegIndex(instr, 1)
            for i in range(4):
                self.memory[self.registers[addr] + i] = (
                    self.registers[src] >> (i * 8)
                ) & 0xFF
        elif instr.name == "GT_MACRO":
            dst = self.getRegIndex(instr, 0)
            src0 = self.getRegIndex(instr, 1)
            src1 = self.getRegIndex(instr, 2)
            self.registers[dst] = (
                1 if self.registers[src0] > self.registers[src1] else 0
            )
        elif instr.name == "EQ_MACRO":
            dst = self.getRegIndex(instr, 0)
            src0 = self.getRegIndex(instr, 1)
            src1 = self.getRegIndex(instr, 2)
            self.registers[dst] = (
                1 if self.registers[src0] == self.registers[src1] else 0
            )
        elif instr.name == "NEQ_MACRO":
            dst = self.getRegIndex(instr, 0)
            src0 = self.getRegIndex(instr, 1)
            src1 = self.getRegIndex(instr, 2)
            self.registers[dst] = (
                1 if self.registers[src0] != self.registers[src1] else 0
            )
        elif instr.name == "LT_MACRO":
            dst = self.getRegIndex(instr, 0)
            src0 = self.getRegIndex(instr, 1)
            src1 = self.getRegIndex(instr, 2)
            self.registers[dst] = (
                1 if self.registers[src0] < self.registers[src1] else 0
            )
        elif instr.name == "DIV_MACRO":
            dst = self.getRegIndex(instr, 0)
            src = self.getRegIndex(instr, 1)
            self.registers[dst] = self.registers[dst] // self.registers[src]
        elif instr.name == "REM_MACRO":
            dst = self.getRegIndex(instr, 0)
            src = self.getRegIndex(instr, 1)
            self.registers[dst] = self.registers[dst] % self.registers[src]

        elif instr.name == "RET_PSEUDO":
            raise StopIteration("Program returned")
        else:
            raise ValueError(f"Unhandled instruction: {instr.name}")
        self.pc += 1
        self.is_num_building = next_is_num_building
        self.is_flag_combining = next_is_flag_combining
