//! ursa-rs — a Rust reimplementation of the MtG simulator.
//!
//! Goal: drop-in replacement for `python3 ursa/src/main.py` that runs
//! a compiled-and-linked MtG assembly file. We need 10-100× the Python
//! version's throughput so full-system boots (Linux on mini-rv32ima)
//! finish in reasonable wall time.
//!
//! Usage matches the Python CLI:
//!     ursa-rs <source.s> [--rom PATH@ADDR ...] [--zero-mem]

mod assembler;
mod simulator;

use std::env;
use std::fs;
use std::process::ExitCode;
use std::time::Instant;

struct Args {
    source: String,
    roms: Vec<(String, u64)>,
    zero_mem: bool,
}

fn parse_args() -> Result<Args, String> {
    let mut a = env::args();
    a.next();
    let mut source = None;
    let mut roms = Vec::new();
    let mut zero_mem = false;
    while let Some(arg) = a.next() {
        if arg == "--rom" {
            let spec = a.next().ok_or("--rom needs PATH@ADDR")?;
            let (path, addr_s) = spec
                .rsplit_once('@')
                .ok_or(format!("--rom expects PATH@ADDR, got {}", spec))?;
            let addr = if let Some(rest) = addr_s.strip_prefix("0x") {
                u64::from_str_radix(rest, 16).map_err(|e| e.to_string())?
            } else {
                addr_s.parse::<u64>().map_err(|e| e.to_string())?
            };
            roms.push((path.to_string(), addr));
        } else if arg == "--zero-mem" {
            zero_mem = true;
        } else if arg.starts_with("--") {
            return Err(format!("unknown option: {}", arg));
        } else if source.is_none() {
            source = Some(arg);
        } else {
            return Err(format!("unexpected positional arg: {}", arg));
        }
    }
    Ok(Args {
        source: source.ok_or("missing source file")?,
        roms,
        zero_mem,
    })
}

fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("ursa-rs: {}", e);
            return ExitCode::from(2);
        }
    };

    let t_parse = Instant::now();
    let source = match fs::read_to_string(&args.source) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("ursa-rs: cannot read {}: {}", args.source, e);
            return ExitCode::from(2);
        }
    };

    let mut program = match assembler::parse(&source) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("ursa-rs: parse error: {}", e);
            return ExitCode::from(2);
        }
    };
    program.fixup_jumps();
    let parse_ms = t_parse.elapsed().as_millis();
    eprintln!(
        "[ursa-rs] parsed {} instructions in {} ms",
        program.instructions.len(),
        parse_ms
    );

    let mut sim = simulator::Simulator::new(program, args.zero_mem);

    for (path, addr) in &args.roms {
        let data = match fs::read(path) {
            Ok(d) => d,
            Err(e) => {
                eprintln!("ursa-rs: cannot read rom {}: {}", path, e);
                return ExitCode::from(2);
            }
        };
        for (i, b) in data.iter().enumerate() {
            sim.memory.insert(addr + i as u64, *b as u64);
        }
        eprintln!(
            "[ursa-rs] preloaded {} bytes from {:?} at 0x{:08x}",
            data.len(),
            path,
            addr
        );
    }

    let t0 = Instant::now();
    let mut steps: u64 = 0;
    let mut last_tick = t0;
    loop {
        match sim.step() {
            simulator::StepResult::Ok => {}
            simulator::StepResult::Halt => {
                let elapsed = t0.elapsed();
                flush_output(&mut sim);
                println!("\nProgram finished.");
                eprintln!(
                    "[ursa-rs] {} steps in {:.3}s ({:.0} steps/s)",
                    steps,
                    elapsed.as_secs_f64(),
                    (steps as f64) / elapsed.as_secs_f64()
                );
                return ExitCode::SUCCESS;
            }
            simulator::StepResult::Err(e) => {
                let elapsed = t0.elapsed();
                flush_output(&mut sim);
                println!("\nError during simulation: {}", e);
                eprintln!("[ursa-rs] {} steps in {:.3}s", steps, elapsed.as_secs_f64());
                return ExitCode::from(1);
            }
        }
        steps += 1;
        // Periodic progress tick — cheap modulo check, printed at most
        // once every 3 wall-clock seconds.
        if steps & 0xFFFFF == 0 {
            let now = Instant::now();
            if now.duration_since(last_tick).as_secs_f64() >= 3.0 {
                flush_output(&mut sim);
                eprintln!(
                    "[ursa-rs-tick] {} steps, {:.0} steps/s, mem={}",
                    steps,
                    (steps as f64) / t0.elapsed().as_secs_f64(),
                    sim.memory.len()
                );
                last_tick = now;
            }
        }
    }
}

fn flush_output(sim: &mut simulator::Simulator) {
    use std::io::Write;
    if !sim.output.is_empty() {
        let bytes: Vec<u8> = sim.output.iter().map(|&c| c as u8).collect();
        std::io::stdout().write_all(&bytes).ok();
        std::io::stdout().flush().ok();
        sim.output.clear();
    }
}
