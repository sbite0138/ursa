//! MtG simulator hot path. Mirrors Python simulator.py exactly for the
//! instructions we need; anything subtly different is a porting bug.

use std::collections::HashMap;

use crate::assembler::{Op, Program};

pub enum StepResult {
    Ok,
    Halt,
    Err(String),
}

pub struct Simulator {
    pub program: Program,
    pub registers: [u64; 12],
    pub flag: bool,
    pub is_flag_combining: bool,
    pub is_num_building: bool,
    pub pc: i64,
    pub memory: HashMap<u64, u64>,
    pub output: Vec<u64>,
    pub return_stack: Vec<i64>,
    pub mem_default_zero: bool,
    /// Debug: visit counter for ursa pc buckets (1024-instruction granularity).
    /// Enabled with --trace-pc. Zero when disabled so the hot path pays
    /// no cost.
    pub pc_hist: Vec<u64>,
    pub pc_hist_enabled: bool,
}

impl Simulator {
    pub fn new(program: Program, mem_default_zero: bool) -> Self {
        let mut memory = HashMap::with_capacity(64 * 1024);
        for (&addr, &byte) in &program.global_init {
            memory.insert(addr, byte as u64);
        }
        let mut registers = [0u64; 12];
        registers[1] = 128;
        registers[2] = 128;
        let start_pc = match program.labels.get("_start") {
            Some(&idx) => idx as i64,
            None => 0,
        };
        Self {
            program,
            registers,
            flag: false,
            is_flag_combining: false,
            is_num_building: false,
            pc: start_pc,
            memory,
            output: Vec::new(),
            return_stack: Vec::new(),
            mem_default_zero,
            pc_hist: Vec::new(),
            pc_hist_enabled: false,
        }
    }

    pub fn enable_pc_hist(&mut self) {
        let n = self.program.instructions.len();
        let buckets = (n + 1023) / 1024;
        self.pc_hist = vec![0; buckets];
        self.pc_hist_enabled = true;
    }

    #[inline]
    pub fn step(&mut self) -> StepResult {
        let instrs = &self.program.instructions;
        let n = instrs.len() as i64;
        if self.pc < 0 || self.pc >= n {
            return StepResult::Halt;
        }
        if self.pc_hist_enabled {
            let bucket = (self.pc as usize) / 1024;
            if bucket < self.pc_hist.len() {
                self.pc_hist[bucket] += 1;
            }
        }
        let instr = &instrs[self.pc as usize];
        let a = &instr.iargs;
        let mut pc = self.pc;
        let mut flag = self.flag;
        let mut next_num_building = false;
        let mut next_flag_combining = false;
        let regs = &mut self.registers;
        let mem = &mut self.memory;

        // Dispatch. We use wrapping_* arithmetic where the Python
        // version relies on Python's unbounded ints; MtG semantics
        // are arbitrary-precision but in practice values stay inside
        // u64 for everything we care about (the compiled emulator
        // REMs by 2^32 after multiplies, NumBuild results cap at 2^44
        // or so, etc.).
        match instr.op {
            Op::NumBuild => {
                let imm = a[0] * 12 + a[1];
                if self.is_num_building {
                    regs[0] = regs[0].wrapping_mul(144).wrapping_add(imm);
                } else {
                    regs[0] = imm;
                }
                next_num_building = true;
            }
            Op::Add => {
                regs[a[0] as usize] = regs[a[0] as usize].wrapping_add(regs[a[1] as usize]);
            }
            Op::SubCond => {
                let d = a[0] as usize;
                let s = a[1] as usize;
                let vd = regs[d];
                let vs = regs[s];
                if vd >= vs {
                    regs[d] = vd - vs;
                    flag = false;
                } else {
                    flag = true;
                }
            }
            Op::Mult => {
                regs[a[0] as usize] = regs[a[0] as usize].wrapping_mul(regs[a[1] as usize]);
            }
            Op::Move => {
                regs[a[0] as usize] = regs[a[1] as usize];
            }
            Op::Load => {
                let addr = regs[a[1] as usize];
                let v = match mem.get(&addr) {
                    Some(&v) => v,
                    None => {
                        if !self.mem_default_zero {
                            return StepResult::Err(format!(
                                "Memory read from uninitialized address: {}",
                                addr
                            ));
                        }
                        0
                    }
                };
                regs[a[0] as usize] = v;
            }
            Op::Store => {
                mem.insert(regs[a[1] as usize], regs[a[0] as usize]);
            }
            Op::Add1 => {
                regs[a[0] as usize] = regs[a[0] as usize].wrapping_add(1);
            }
            Op::Sub1Cond => {
                let d = a[0] as usize;
                if regs[d] > 0 {
                    regs[d] -= 1;
                    flag = false;
                } else {
                    flag = true;
                }
            }
            Op::Zero => {
                regs[a[0] as usize] = 0;
            }
            Op::Divide => {
                let d = a[0] as usize;
                let r0 = regs[0];
                if r0 == 0 {
                    return StepResult::Err(format!("Division by zero at pc={}", self.pc));
                }
                let dv = regs[d];
                let quot = dv / r0;
                let rem = dv % r0;
                regs[d] = rem;
                regs[6] = quot;
                flag = quot != 0;
            }
            Op::SetF => {
                regs[a[0] as usize] = if flag { 1 } else { 0 };
            }
            Op::SetNF => {
                regs[a[0] as usize] = if flag { 0 } else { 1 };
            }
            Op::FIsZero => {
                let cmp = regs[a[0] as usize] == 0;
                flag = if self.is_flag_combining { flag || cmp } else { cmp };
                next_flag_combining = true;
            }
            Op::FLess => {
                let cmp = regs[a[1] as usize] < regs[a[0] as usize];
                flag = if self.is_flag_combining { flag || cmp } else { cmp };
                next_flag_combining = true;
            }
            Op::Halve => {
                let d = a[0] as usize;
                flag = (regs[d] & 1) == 1;
                regs[d] >>= 1;
            }
            Op::JumpFwd => {
                pc = pc.wrapping_add(regs[a[0] as usize] as i64);
                if pc < 0 || pc >= n {
                    return StepResult::Err(format!("Jump out of bounds: dst_pc={}", pc));
                }
            }
            Op::JumpBwd => {
                pc = pc.wrapping_sub(regs[a[0] as usize] as i64);
                if pc < 0 || pc >= n {
                    return StepResult::Err(format!("Jump out of bounds: dst_pc={}", pc));
                }
            }
            Op::JumpFwdNF => {
                if !flag {
                    pc = pc.wrapping_add(regs[a[0] as usize] as i64);
                    if pc < 0 || pc >= n {
                        return StepResult::Err(format!("Jump out of bounds: dst_pc={}", pc));
                    }
                }
            }
            Op::JumpBwdNF => {
                if !flag {
                    pc = pc.wrapping_sub(regs[a[0] as usize] as i64);
                    if pc < 0 || pc >= n {
                        return StepResult::Err(format!("Jump out of bounds: dst_pc={}", pc));
                    }
                }
            }
            Op::JumpFwdF => {
                if flag {
                    pc = pc.wrapping_add(regs[a[0] as usize] as i64);
                    if pc < 0 || pc >= n {
                        return StepResult::Err(format!("Jump out of bounds: dst_pc={}", pc));
                    }
                }
            }
            Op::JumpBwdF => {
                if flag {
                    pc = pc.wrapping_sub(regs[a[0] as usize] as i64);
                    if pc < 0 || pc >= n {
                        return StepResult::Err(format!("Jump out of bounds: dst_pc={}", pc));
                    }
                }
            }
            Op::Output => {
                self.output.push(regs[a[0] as usize]);
            }
            Op::CallFwd => {
                self.return_stack.push(pc + 1);
                pc = pc.wrapping_add(regs[a[0] as usize] as i64);
            }
            Op::CallBwd | Op::CallBwdR => {
                self.return_stack.push(pc + 1);
                pc = pc.wrapping_sub(regs[a[0] as usize] as i64);
            }
            Op::Return => {
                match self.return_stack.pop() {
                    Some(ret) => {
                        pc = ret - 1;
                    }
                    None => return StepResult::Halt,
                }
            }
            Op::AInput | Op::BInput => {
                // Interactive input is not supported in ursa-rs right now
                // (we have no CLI prompt path yet); deliver 0.
                regs[a[0] as usize] = 0;
            }
        }

        self.pc = pc + 1;
        self.flag = flag;
        self.is_num_building = next_num_building;
        self.is_flag_combining = next_flag_combining;
        StepResult::Ok
    }
}
