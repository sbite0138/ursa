//! MtG assembler. Parses a linked .s file (as produced by mtg-link.py)
//! and produces a Program ready to hand to the Simulator.
//!
//! Mirrors the Python ursa/src/assembler.py semantics closely so that
//! any .s the Python path accepts produces an identical instruction
//! stream here. Anything that doesn't match is a porting bug.

use std::collections::HashMap;

const GLOBALS_BASE: u64 = 1024;

/// Dense integer opcode enum. Replaces the Python string-based
/// instruction name in the hot path; mapping lives in OPNAMES below.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum Op {
    Move,
    Zero,
    NumBuild,
    Add,
    Add1,
    SubCond,
    Sub1Cond,
    Mult,
    Divide,
    SetF,
    SetNF,
    FIsZero,
    FLess,
    Halve,
    JumpFwd,
    JumpBwd,
    JumpFwdNF,
    JumpBwdNF,
    JumpFwdF,
    JumpBwdF,
    Store,
    Load,
    Output,
    AInput,
    BInput,
    CallFwd,
    CallBwd,
    CallBwdR,
    Return,
}

fn parse_op(s: &str) -> Option<Op> {
    Some(match s {
        "Move" => Op::Move,
        "Zero" => Op::Zero,
        "NumBuild" => Op::NumBuild,
        "Add" => Op::Add,
        "Add1" => Op::Add1,
        "SubCond" => Op::SubCond,
        "Sub1Cond" => Op::Sub1Cond,
        "Mult" => Op::Mult,
        "Divide" => Op::Divide,
        "SetF" => Op::SetF,
        "SetNF" => Op::SetNF,
        "FIsZero" => Op::FIsZero,
        "FLess" => Op::FLess,
        "Halve" => Op::Halve,
        "JumpFwd" => Op::JumpFwd,
        "JumpBwd" => Op::JumpBwd,
        "JumpFwdNF" => Op::JumpFwdNF,
        "JumpBwdNF" => Op::JumpBwdNF,
        "JumpFwdF" => Op::JumpFwdF,
        "JumpBwdF" => Op::JumpBwdF,
        "Store" => Op::Store,
        "Load" => Op::Load,
        "Output" => Op::Output,
        "AInput" => Op::AInput,
        "BInput" => Op::BInput,
        "CallFwd" => Op::CallFwd,
        "CallBwd" => Op::CallBwd,
        "CallBwdR" => Op::CallBwdR,
        "Return" => Op::Return,
        _ => return None,
    })
}

#[derive(Debug)]
pub struct Instruction {
    pub op: Op,
    /// Textual args, as parsed from the .s. Retained so fixup_jumps can
    /// resolve labels. Empty by the time simulator runs.
    pub args: Vec<String>,
    /// Pre-parsed numeric args (reg index or immediate). Filled in by
    /// fixup_jumps after label resolution. The simulator hot path reads
    /// only this.
    pub iargs: [u64; 3],
}

impl Instruction {
    fn new(op: Op, args: Vec<String>) -> Self {
        Self {
            op,
            args,
            iargs: [0; 3],
        }
    }
}

pub struct Program {
    pub instructions: Vec<Instruction>,
    pub labels: HashMap<String, usize>,
    pub globals: HashMap<String, u64>,
    pub global_init: HashMap<u64, u8>,
    data_fixups: Vec<(u64, String, i64)>,
}

impl Program {
    fn new() -> Self {
        Self {
            instructions: Vec::new(),
            labels: HashMap::new(),
            globals: HashMap::new(),
            global_init: HashMap::new(),
            data_fixups: Vec::new(),
        }
    }

    fn init_memory_long(&mut self, addr: u64, value: u64) {
        for i in 0..4 {
            self.global_init
                .insert(addr + i, ((value >> (i * 8)) & 0xFF) as u8);
        }
    }

    fn init_memory_byte(&mut self, addr: u64, value: u64) {
        self.global_init.insert(addr, (value & 0xFF) as u8);
    }

    fn resolve_data_fixups(&mut self) -> Result<(), String> {
        let fixups = std::mem::take(&mut self.data_fixups);
        for (addr, symbol, offset) in fixups {
            let target = if let Some(&a) = self.globals.get(&symbol) {
                (a as i64 + offset) as u64
            } else if let Some(&idx) = self.labels.get(&symbol) {
                (idx as i64 + offset) as u64
            } else {
                return Err(format!(".long references unknown symbol '{}'", symbol));
            };
            self.init_memory_long(addr, target);
        }
        Ok(())
    }

    fn patch_numbuild_quad(&mut self, index: usize, value: u64) {
        // 8 base-12 digits, LSB first then reversed → 4 NumBuild pairs.
        let mut digits = [0u64; 8];
        let mut v = value;
        for d in digits.iter_mut() {
            *d = v % 12;
            v /= 12;
        }
        digits.reverse();
        for i in 0..4 {
            let ins = &mut self.instructions[index - 4 + i];
            ins.args = vec![
                format!("#{}", digits[i * 2]),
                format!("#{}", digits[i * 2 + 1]),
            ];
        }
    }

    fn function_entry_index(&self, instr_index: usize) -> usize {
        let mut best: usize = 0;
        for (name, &idx) in &self.labels {
            if name.starts_with('.') {
                continue;
            }
            if idx <= instr_index && idx > best {
                best = idx;
            }
        }
        best
    }

    pub fn fixup_jumps(&mut self) {
        // Snapshot original opcodes (we may flip Fwd ↔ Bwd below based
        // on sign of the resolved distance).
        let n = self.instructions.len();
        for index in 0..n {
            let op = self.instructions[index].op;
            let is_jump = matches!(
                op,
                Op::JumpFwd
                    | Op::JumpBwd
                    | Op::JumpFwdNF
                    | Op::JumpBwdNF
                    | Op::JumpFwdF
                    | Op::JumpBwdF
            );
            let is_call = matches!(op, Op::CallFwd | Op::CallBwd | Op::CallBwdR);
            let is_return = op == Op::Return;
            if !(is_jump || is_call || is_return) {
                continue;
            }
            if is_jump || is_call {
                let target_label = self.instructions[index].args[0].clone();
                let target_index = *self
                    .labels
                    .get(&target_label)
                    .unwrap_or_else(|| panic!("undefined label: {}", target_label));
                let target_value = target_index as i64 - index as i64 - 1;
                let new_op = if is_jump {
                    let flip_to_bwd = target_value < 0
                        && matches!(op, Op::JumpFwd | Op::JumpFwdNF | Op::JumpFwdF);
                    let flip_to_fwd = target_value > 0
                        && matches!(op, Op::JumpBwd | Op::JumpBwdNF | Op::JumpBwdF);
                    if flip_to_bwd {
                        match op {
                            Op::JumpFwd => Op::JumpBwd,
                            Op::JumpFwdNF => Op::JumpBwdNF,
                            Op::JumpFwdF => Op::JumpBwdF,
                            _ => op,
                        }
                    } else if flip_to_fwd {
                        match op {
                            Op::JumpBwd => Op::JumpFwd,
                            Op::JumpBwdNF => Op::JumpFwdNF,
                            Op::JumpBwdF => Op::JumpFwdF,
                            _ => op,
                        }
                    } else {
                        op
                    }
                } else {
                    // call
                    if target_value < 0 && op == Op::CallFwd {
                        Op::CallBwdR
                    } else if target_value > 0
                        && (op == Op::CallBwd || op == Op::CallBwdR)
                    {
                        Op::CallFwd
                    } else {
                        op
                    }
                };
                let magnitude = target_value.unsigned_abs();
                let ins = &mut self.instructions[index];
                ins.op = new_op;
                ins.args = vec!["r0".to_string()];
                self.patch_numbuild_quad(index, magnitude);
            } else {
                // Return
                let entry_index = self.function_entry_index(index);
                let target_value = (index - entry_index) as u64;
                let ins = &mut self.instructions[index];
                ins.args = vec!["r0".to_string()];
                self.patch_numbuild_quad(index, target_value);
            }
        }

        // Pre-parse every instruction's args into integers so the
        // simulator hot path doesn't have to touch strings.
        for ins in self.instructions.iter_mut() {
            for (i, a) in ins.args.iter().enumerate() {
                if i >= 3 {
                    break;
                }
                if a.is_empty() {
                    ins.iargs[i] = 0;
                } else if a.starts_with('r') {
                    ins.iargs[i] = a[1..].parse().unwrap_or(0);
                } else if a.starts_with('#') {
                    ins.iargs[i] = a[1..].parse().unwrap_or(0);
                } else {
                    ins.iargs[i] = 0;
                }
            }
        }
    }
}

fn split_sym_offset(tok: &str) -> (String, i64) {
    let b = tok.as_bytes();
    for (i, &c) in b.iter().enumerate() {
        if i == 0 {
            continue;
        }
        if c == b'+' || c == b'-' {
            let sym = tok[..i].to_string();
            let offset = parse_int(&tok[i..]).unwrap_or(0);
            return (sym, offset);
        }
    }
    (tok.to_string(), 0)
}

fn parse_int(tok: &str) -> Option<i64> {
    let (sign, rest) = if let Some(r) = tok.strip_prefix('-') {
        (-1, r)
    } else if let Some(r) = tok.strip_prefix('+') {
        (1, r)
    } else {
        (1, tok)
    };
    let val = if let Some(h) = rest.strip_prefix("0x").or_else(|| rest.strip_prefix("0X")) {
        i64::from_str_radix(h, 16).ok()?
    } else {
        rest.parse::<i64>().ok()?
    };
    Some(sign * val)
}

pub fn parse(source: &str) -> Result<Program, String> {
    let mut program = Program::new();
    let mut in_data = false;
    let mut data_cursor: u64 = GLOBALS_BASE;

    for raw in source.lines() {
        let mut line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        // strip ; comment
        if let Some(idx) = line.find(';') {
            line = line[..idx].trim();
            if line.is_empty() {
                continue;
            }
        }
        let first = line.split_whitespace().next().unwrap_or("");

        // Section directives
        if first == ".text" {
            in_data = false;
            continue;
        }
        if first == ".data" || first == ".bss" {
            in_data = true;
            continue;
        }
        if first == ".section" {
            let rest = line[".section".len()..].trim();
            in_data = !(rest == ".text" || rest.starts_with(".text,") || rest.starts_with(".text "));
            continue;
        }

        // .comm directive (BSS global in any section)
        if first == ".comm" {
            let payload = line[".comm".len()..].trim();
            let parts: Vec<&str> = payload.split(',').map(|s| s.trim()).collect();
            let name = parts[0].to_string();
            let size: u64 = parts[1].parse().map_err(|e: std::num::ParseIntError| e.to_string())?;
            if parts.len() >= 3 {
                let align: u64 = parts[2].parse().map_err(|e: std::num::ParseIntError| e.to_string())?;
                if align > 0 && data_cursor % align != 0 {
                    data_cursor += align - (data_cursor % align);
                }
            }
            if program.globals.contains_key(&name) {
                return Err(format!("duplicate global: {}", name));
            }
            program.globals.insert(name, data_cursor);
            data_cursor += size;
            continue;
        }

        if in_data {
            if line.ends_with(':') {
                let name = line[..line.len() - 1].trim().to_string();
                if program.globals.contains_key(&name) {
                    return Err(format!("duplicate global: {}", name));
                }
                program.globals.insert(name, data_cursor);
                continue;
            }
            let mut it = line.splitn(2, char::is_whitespace);
            let directive = it.next().unwrap_or("");
            let rest = it.next().unwrap_or("").trim();
            match directive {
                ".long" => {
                    let tok = rest.split_whitespace().next().unwrap_or("");
                    if let Some(v) = parse_int(tok) {
                        program.init_memory_long(data_cursor, v as u64);
                    } else {
                        let (sym, off) = split_sym_offset(tok);
                        program.data_fixups.push((data_cursor, sym, off));
                    }
                    data_cursor += 4;
                }
                ".byte" => {
                    let tok = rest.split_whitespace().next().unwrap_or("");
                    let v = parse_int(tok).unwrap_or(0);
                    program.init_memory_byte(data_cursor, v as u64);
                    data_cursor += 1;
                }
                ".p2align" => {
                    let tok = rest.split(',').next().unwrap_or("").trim();
                    let pow2: u32 = tok.parse().unwrap_or(0);
                    let align = 1u64 << pow2;
                    if align > 0 && data_cursor % align != 0 {
                        data_cursor += align - (data_cursor % align);
                    }
                }
                _ => {}
            }
            continue;
        }

        // .text instruction parsing
        if line.ends_with(':') {
            let name = line[..line.len() - 1].trim().to_string();
            if program.labels.contains_key(&name) {
                return Err(format!("duplicate label: {}", name));
            }
            program.labels.insert(name, program.instructions.len());
            continue;
        }

        let mut it = line.splitn(2, char::is_whitespace);
        let name = it.next().unwrap_or("");
        let argstr = it.next().unwrap_or("");
        let op = match parse_op(name) {
            Some(o) => o,
            None => {
                if name.starts_with('.') {
                    continue; // metadata directive we don't care about
                }
                return Err(format!("unknown instruction '{}'", name));
            }
        };
        let args: Vec<String> = if argstr.trim().is_empty() {
            Vec::new()
        } else {
            argstr.split(',').map(|s| s.trim().to_string()).collect()
        };
        program.instructions.push(Instruction::new(op, args));
    }

    program.resolve_data_fixups()?;
    Ok(program)
}
