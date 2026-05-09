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
mod dump;
mod input;
mod simulator;
mod snapshot;

use std::env;
use std::fs;
use std::path::Path;
use std::process::ExitCode;
use std::time::Instant;

struct Args {
    source: String,
    roms: Vec<(String, u64)>,
    zero_mem: bool,
    trace_pc: bool,
    max_steps: Option<u64>,
    /// On a graceful exit (halt / max-steps / --stop-marker), write the
    /// simulator state to this path so a later run can --load-snapshot
    /// and resume.
    save_snapshot: Option<String>,
    /// Load simulator state from this path at startup instead of
    /// initializing fresh. The .s file on the CLI must match the one
    /// that was active when the snapshot was taken.
    load_snapshot: Option<String>,
    /// If set, the main loop polls for this file path every ~1 M steps
    /// and exits cleanly when it appears. The stop-marker file is
    /// removed on detection. Pair with --save-snapshot to capture state
    /// from an external observer ("I see the login prompt; `touch
    /// /tmp/stop` to snap it").
    stop_marker: Option<String>,
    /// Hook the `AInput` primitive up to stdin so a guest running
    /// inside the MtG program (e.g. the mini-rv32ima harness that
    /// powers linux_boot) can receive interactive keystrokes. `AInput`
    /// returns 0xFFFF_FFFF when stdin has nothing pending, otherwise
    /// the next byte. No termios manipulation yet — stdin is still
    /// line-buffered by the host terminal.
    raw_input: bool,
    /// Instruction-counts at which to write a card-level state dump
    /// for handoff to mtgemu-claude. Sorted+deduped after parse.
    /// `0` means "before any instruction executes" (initial state).
    dump_at: Vec<u64>,
    /// Path template for `--dump-at` outputs. `{n}` is substituted with
    /// the instruction count. Required when `--dump-at` is given.
    dump_out: Option<String>,
}

fn parse_args() -> Result<Args, String> {
    let mut a = env::args();
    a.next();
    let mut source = None;
    let mut roms = Vec::new();
    let mut zero_mem = false;
    let mut trace_pc = false;
    let mut max_steps = None;
    let mut save_snapshot = None;
    let mut load_snapshot = None;
    let mut stop_marker = None;
    let mut raw_input = false;
    let mut dump_at: Vec<u64> = Vec::new();
    let mut dump_out: Option<String> = None;
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
        } else if arg == "--trace-pc" {
            trace_pc = true;
        } else if arg == "--max-steps" {
            let v = a.next().ok_or("--max-steps needs N")?;
            max_steps = Some(v.parse::<u64>().map_err(|e| e.to_string())?);
        } else if arg == "--save-snapshot" {
            save_snapshot = Some(a.next().ok_or("--save-snapshot needs PATH")?);
        } else if arg == "--load-snapshot" {
            load_snapshot = Some(a.next().ok_or("--load-snapshot needs PATH")?);
        } else if arg == "--stop-marker" {
            stop_marker = Some(a.next().ok_or("--stop-marker needs PATH")?);
        } else if arg == "--raw-input" {
            raw_input = true;
        } else if arg == "--dump-at" {
            let v = a.next().ok_or("--dump-at needs N")?;
            dump_at.push(v.parse::<u64>().map_err(|e| e.to_string())?);
        } else if arg == "--dump-out" {
            dump_out = Some(a.next().ok_or("--dump-out needs PATH")?);
        } else if arg.starts_with("--") {
            return Err(format!("unknown option: {}", arg));
        } else if source.is_none() {
            source = Some(arg);
        } else {
            return Err(format!("unexpected positional arg: {}", arg));
        }
    }
    if !dump_at.is_empty() {
        if dump_out.is_none() {
            return Err("--dump-at requires --dump-out PATH".into());
        }
        let path = dump_out.as_ref().unwrap();
        if dump_at.len() > 1 && !path.contains("{n}") {
            return Err(
                "--dump-out template must contain {n} when multiple --dump-at are given".into(),
            );
        }
    }
    dump_at.sort_unstable();
    dump_at.dedup();
    Ok(Args {
        source: source.ok_or("missing source file")?,
        roms,
        zero_mem,
        trace_pc,
        max_steps,
        save_snapshot,
        load_snapshot,
        stop_marker,
        raw_input,
        dump_at,
        dump_out,
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
    if args.trace_pc {
        sim.enable_pc_hist();
    }
    if args.raw_input {
        sim.input_queue = Some(input::InputQueue::new());
        eprintln!("[ursa-rs] --raw-input: AInput will pull bytes from stdin");
    }

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

    // If the user asked for a resume, overwrite the fresh-init state with
    // whatever the snapshot recorded. The snapshot load clears `memory`
    // and then repopulates it, so any --rom data we just inserted gets
    // discarded — which is exactly what we want, because the guest has
    // long since mutated those bytes.
    if let Some(path) = &args.load_snapshot {
        let t_load = Instant::now();
        if let Err(e) = snapshot::load(&mut sim, path) {
            eprintln!("ursa-rs: failed to load snapshot {:?}: {}", path, e);
            return ExitCode::from(2);
        }
        eprintln!(
            "[ursa-rs] loaded snapshot {:?} ({} memory entries) in {} ms",
            path,
            sim.memory.len(),
            t_load.elapsed().as_millis()
        );
    }

    // Shared helper: flush stdout, print final stats, optionally save
    // snapshot, optionally dump PC hist, and return the given exit code.
    // Declared as a closure-like sequence inline inside the match arms
    // below (Rust's borrow of `sim` makes hoisting to a closure painful).

    let t0 = Instant::now();
    let mut steps: u64 = 0;
    let mut last_tick = t0;
    // Sorted + deduped at parse time. Cursor advances as we hit each
    // requested instruction count, so the per-step check is O(1).
    let dump_at = args.dump_at.clone();
    let dump_template = args.dump_out.clone();
    let mut dump_cursor: usize = 0;
    // --dump-at 0 means "before any instruction has executed". Drain the
    // cursor of any zero entries up front so the in-loop check only ever
    // looks at strictly positive counts.
    while dump_cursor < dump_at.len() && dump_at[dump_cursor] == 0 {
        do_dump(&sim, 0, dump_template.as_ref().unwrap());
        dump_cursor += 1;
    }
    loop {
        if let Some(m) = args.max_steps {
            if steps >= m {
                let elapsed = t0.elapsed();
                flush_output(&mut sim);
                println!("\n[ursa-rs] hit --max-steps={}", m);
                eprintln!(
                    "[ursa-rs] {} steps in {:.3}s ({:.0} steps/s)",
                    steps,
                    elapsed.as_secs_f64(),
                    (steps as f64) / elapsed.as_secs_f64()
                );
                if args.trace_pc {
                    dump_pc_hist(&sim);
                }
                maybe_save_snapshot(&sim, &args.save_snapshot, "max-steps");
                report_input_stats(&sim);
                return ExitCode::SUCCESS;
            }
        }
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
                if args.trace_pc {
                    dump_pc_hist(&sim);
                }
                maybe_save_snapshot(&sim, &args.save_snapshot, "halt");
                report_input_stats(&sim);
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
        // After completing instruction `steps`, check whether the user
        // asked for a dump at exactly this count. Multiple counts may
        // coincide (after dedup they don't, but the loop handles it
        // anyway).
        while dump_cursor < dump_at.len() && dump_at[dump_cursor] == steps {
            do_dump(&sim, steps, dump_template.as_ref().unwrap());
            dump_cursor += 1;
        }
        // Periodic progress tick — cheap modulo check, printed at most
        // once every 3 wall-clock seconds. Also the spot where we poll
        // for the stop-marker so signal-free "save and exit" works.
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
            if let Some(marker) = &args.stop_marker {
                if Path::new(marker).exists() {
                    flush_output(&mut sim);
                    eprintln!(
                        "[ursa-rs] stop-marker {:?} detected at {} steps, \
                         saving and exiting",
                        marker, steps
                    );
                    maybe_save_snapshot(&sim, &args.save_snapshot, "stop-marker");
                    report_input_stats(&sim);
                    // Best-effort cleanup: leave the marker in place on
                    // failure so the next retry still sees it.
                    let _ = fs::remove_file(marker);
                    return ExitCode::SUCCESS;
                }
            }
        }
    }
}

fn do_dump(sim: &simulator::Simulator, instr_count: u64, template: &str) {
    let path = dump::substitute_template(template, instr_count);
    let t = Instant::now();
    match dump::write_dump(sim, instr_count, &path) {
        Ok(entries) => eprintln!(
            "[ursa-rs] dumped state at instr_count={} to {:?} ({} memory entries) in {} ms",
            instr_count,
            path,
            entries,
            t.elapsed().as_millis()
        ),
        Err(e) => eprintln!(
            "[ursa-rs] dump at instr_count={} to {:?} FAILED: {}",
            instr_count, path, e
        ),
    }
}

fn report_input_stats(sim: &simulator::Simulator) {
    if sim.input_queue.is_some() {
        eprintln!(
            "[ursa-rs] AInput delivered {} byte(s) from stdin during this run",
            sim.ainput_bytes_delivered
        );
    }
}

fn maybe_save_snapshot(
    sim: &simulator::Simulator,
    path_opt: &Option<String>,
    reason: &str,
) {
    let Some(path) = path_opt else { return };
    let t = Instant::now();
    match snapshot::save(sim, path) {
        Ok(()) => eprintln!(
            "[ursa-rs] saved snapshot to {:?} ({} memory entries) in {} ms (reason: {})",
            path,
            sim.memory.len(),
            t.elapsed().as_millis(),
            reason
        ),
        Err(e) => eprintln!(
            "[ursa-rs] snapshot save to {:?} FAILED: {} (reason: {})",
            path, e, reason
        ),
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

fn dump_pc_hist(sim: &simulator::Simulator) {
    eprintln!("[ursa-rs] pc histogram (top 20 buckets, 1024 instructions each):");
    let mut pairs: Vec<(usize, u64)> = sim
        .pc_hist
        .iter()
        .enumerate()
        .filter(|(_, &c)| c > 0)
        .map(|(i, &c)| (i, c))
        .collect();
    pairs.sort_by_key(|&(_, c)| std::cmp::Reverse(c));
    for (i, (bucket, count)) in pairs.iter().take(20).enumerate() {
        let pc_start = bucket * 1024;
        let pc_end = pc_start + 1023;
        eprintln!(
            "  #{:2}  bucket pc={}..{}  hits={}",
            i + 1,
            pc_start,
            pc_end,
            count
        );
    }
}
