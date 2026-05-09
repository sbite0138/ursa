//! Card-level state dump for handoff to mtgemu-claude.
//!
//! ursa-rs simulates the MtG ISA at the abstract level (registers,
//! memory, flag, output) — it doesn't model individual cards. mtgemu-
//! claude does the opposite: it drives Forge through the actual MtG
//! rules engine, card by card. To bridge "fast forward to point X with
//! ursa-rs, then resume detailed simulation in mtgemu-claude" we emit
//! state in mtgemu-claude's vocabulary (card names, controllers, P/T,
//! +1/+1 counters) rather than ursa-rs's vocabulary (register indices,
//! u64 memory cells).
//!
//! Static board layout (Worship, Mirror Gallery, the 36 Crusades, Dream
//! Fighters, etc.) is mtgemu-claude's responsibility — it owns
//! GameStateSetup.java and we don't try to mirror that here. The dump
//! captures only what *changes* during execution, since per spec §10.3
//! all derived state (timestamps, layered P/T, phase-out chains) is
//! re-derivable from the primitive register/memory/flag state at
//! between-instruction boundaries.
//!
//! Memory is the one thing that can run into millions of entries
//! (mini-rv32ima boot uses ~16 MB), so it goes to a sibling binary file
//! rather than inline JSON.

use std::fs;
use std::io::{BufWriter, Write};
use std::path::PathBuf;

use crate::simulator::Simulator;

/// Spec §2: the program-pointer permanent on Alice's battlefield is one
/// of these basic lands; index = current PC mod 12.
const PC_BASIC_LANDS: [&str; 12] = [
    "Plains",
    "Island",
    "Swamp",
    "Mountain",
    "Forest",
    "Wastes",
    "Snow-Covered Plains",
    "Snow-Covered Island",
    "Snow-Covered Swamp",
    "Snow-Covered Mountain",
    "Snow-Covered Forest",
    "Snow-Covered Wastes",
];

const FORMAT_VERSION: u32 = 1;
const BIN_FORMAT_TAG: &str = "v1_sparse_u64_pairs_le";

/// Replace `{n}` in a path template with the given instruction count.
pub fn substitute_template(template: &str, instr_count: u64) -> String {
    template.replace("{n}", &instr_count.to_string())
}

/// Derive the sidecar binary-memory path from the JSON path: strip a
/// trailing `.json` (if present) and append `.mem.bin`. Predictable and
/// preserves any instruction-count digits the caller embedded.
fn bin_path_for(json_path: &str) -> PathBuf {
    let stem = json_path.strip_suffix(".json").unwrap_or(json_path);
    PathBuf::from(format!("{}.mem.bin", stem))
}

pub fn write_dump(sim: &Simulator, instr_count: u64, json_path: &str) -> Result<u64, String> {
    let bin_path = bin_path_for(json_path);
    let entry_count = write_memory_bin(sim, &bin_path)?;

    let bin_basename = bin_path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| bin_path.to_string_lossy().into_owned());

    let json = build_json(sim, instr_count, &bin_basename, entry_count);
    fs::write(json_path, json).map_err(|e| format!("write {:?}: {}", json_path, e))?;
    Ok(entry_count)
}

fn write_memory_bin(sim: &Simulator, path: &std::path::Path) -> Result<u64, String> {
    let mut entries: Vec<(u64, u64)> = sim.memory.iter().map(|(&a, &v)| (a, v)).collect();
    entries.sort_unstable_by_key(|&(a, _)| a);
    let f = fs::File::create(path).map_err(|e| format!("create {:?}: {}", path, e))?;
    let mut w = BufWriter::with_capacity(1 << 20, f);
    let mut buf = [0u8; 16];
    for (a, v) in &entries {
        buf[..8].copy_from_slice(&a.to_le_bytes());
        buf[8..].copy_from_slice(&v.to_le_bytes());
        w.write_all(&buf).map_err(|e| format!("write {:?}: {}", path, e))?;
    }
    w.flush().map_err(|e| format!("flush {:?}: {}", path, e))?;
    Ok(entries.len() as u64)
}

fn build_json(sim: &Simulator, instr_count: u64, bin_file: &str, entry_count: u64) -> String {
    let mut s = String::with_capacity(4096);
    s.push_str("{\n");
    s.push_str(&format!("  \"format_version\": {},\n", FORMAT_VERSION));
    s.push_str(&format!("  \"instruction_count\": {},\n", instr_count));

    // Program pointer: the basic land currently on Alice's battlefield.
    let pc_idx = sim.pc.rem_euclid(PC_BASIC_LANDS.len() as i64) as usize;
    let pc_card = PC_BASIC_LANDS[pc_idx];
    s.push_str("  \"program_pointer\": {\n");
    s.push_str(&format!("    \"card_name\": \"{}\",\n", pc_card));
    s.push_str("    \"controller\": \"Alice\",\n");
    s.push_str("    \"zone\": \"Battlefield\",\n");
    s.push_str(&format!("    \"pc_index\": {}\n", sim.pc));
    s.push_str("  },\n");

    // 12 register Joraga Warcallers on Bob's battlefield.
    s.push_str("  \"registers\": [\n");
    for (i, &v) in sim.registers.iter().enumerate() {
        let comma = if i + 1 < sim.registers.len() { "," } else { "" };
        s.push_str(&format!(
            "    {{\"index\": {}, \"card_name\": \"Joraga Warcaller\", \"controller\": \"Bob\", \"zone\": \"Battlefield\", \"p1p1_counters\": {}}}{}\n",
            i, v, comma
        ));
    }
    s.push_str("  ],\n");

    // Flag bit: encoded by Storm Crow's zone (Hand = flag 1, Library = 0).
    let storm_crow_zone = if sim.flag { "Hand" } else { "Library" };
    s.push_str("  \"flag\": {\n");
    s.push_str(&format!("    \"value\": {},\n", sim.flag));
    s.push_str("    \"card_name\": \"Storm Crow\",\n");
    s.push_str("    \"controller\": \"Bob\",\n");
    s.push_str(&format!("    \"zone\": \"{}\"\n", storm_crow_zone));
    s.push_str("  },\n");

    // Memory: card metadata + sidecar binary file. Each populated cell
    // corresponds to one Mouse token on Alice's battlefield with
    //   +1/+1 counters = address, base power = base toughness = value.
    s.push_str("  \"memory\": {\n");
    s.push_str("    \"card_name\": \"Mouse\",\n");
    s.push_str("    \"controller\": \"Alice\",\n");
    s.push_str("    \"zone\": \"Battlefield\",\n");
    s.push_str("    \"is_token\": true,\n");
    s.push_str("    \"encoding\": {\"address_field\": \"p1p1_counters\", \"value_field\": \"power_and_toughness\"},\n");
    s.push_str(&format!("    \"binary_file\": \"{}\",\n", bin_file));
    s.push_str(&format!("    \"binary_format\": \"{}\",\n", BIN_FORMAT_TAG));
    s.push_str(&format!("    \"binary_record_size_bytes\": 16,\n"));
    s.push_str(&format!("    \"entry_count\": {}\n", entry_count));
    s.push_str("  },\n");

    // Output stream produced so far (Output opcode).
    s.push_str("  \"output_stream\": [");
    for (i, &v) in sim.output.iter().enumerate() {
        if i > 0 {
            s.push_str(", ");
        }
        s.push_str(&v.to_string());
    }
    s.push_str("],\n");

    // Return stack (CallFwd / CallBwd / Return).
    s.push_str("  \"return_stack\": [");
    for (i, &v) in sim.return_stack.iter().enumerate() {
        if i > 0 {
            s.push_str(", ");
        }
        s.push_str(&v.to_string());
    }
    s.push_str("],\n");

    // Internal state-machine bits. Should be false between instructions
    // for a well-behaved program; included so a consumer can detect
    // mid-NUMBUILD / mid-flag-combine snapshots and refuse to load.
    s.push_str("  \"internal_state\": {\n");
    s.push_str(&format!(
        "    \"is_flag_combining\": {},\n",
        sim.is_flag_combining
    ));
    s.push_str(&format!(
        "    \"is_num_building\": {}\n",
        sim.is_num_building
    ));
    s.push_str("  }\n");

    s.push_str("}\n");
    s
}
