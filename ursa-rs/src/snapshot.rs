//! Binary snapshot of Simulator state, for save/resume of long boots.
//!
//! Not a general-purpose serializer — deliberately tied to the in-memory
//! layout of [`crate::simulator::Simulator`] so it stays small and fast to
//! write / read even for the 16 MB-guest-RAM linux_boot snapshots (~150 MB
//! of (u64, u64) entries at login time).
//!
//! Format (all integers little-endian):
//!
//!   offset | size | field
//!   -------+------+-------------------------------------------------
//!    0     | 8    | magic b"URSAMTG1"
//!    8     | 4    | version (= 1)
//!   12     | 1    | flags bitfield: bit0=flag, bit1=is_num_building,
//!         |      |                  bit2=is_flag_combining
//!   13     | 3    | padding
//!   16     | 8    | pc (i64)
//!   24     | 8    | program.instructions.len() (integrity check)
//!   32     | 96   | registers[0..12] (u64 × 12)
//!  128     | 8    | memory entry count (u64)
//!  136     | 8    | return_stack entry count (u64)
//!  144+    | 16×N | memory entries (addr u64, val u64)
//!   ...    | 8×M  | return_stack entries (i64)
//!
//! `load` refuses to apply a snapshot whose recorded program-instruction
//! count differs from the currently-loaded program — you must resume
//! against the same .s file the snapshot was taken from.

use std::fs::File;
use std::io::{self, BufReader, BufWriter, Read, Write};

use crate::simulator::Simulator;

const MAGIC: &[u8; 8] = b"URSAMTG1";
const VERSION: u32 = 1;

fn read_u64<R: Read>(r: &mut R) -> io::Result<u64> {
    let mut b = [0u8; 8];
    r.read_exact(&mut b)?;
    Ok(u64::from_le_bytes(b))
}

fn read_i64<R: Read>(r: &mut R) -> io::Result<i64> {
    let mut b = [0u8; 8];
    r.read_exact(&mut b)?;
    Ok(i64::from_le_bytes(b))
}

pub fn save(sim: &Simulator, path: &str) -> io::Result<()> {
    let f = File::create(path)?;
    let mut w = BufWriter::with_capacity(1 << 20, f);

    w.write_all(MAGIC)?;
    w.write_all(&VERSION.to_le_bytes())?;
    let flags = (sim.flag as u8)
        | ((sim.is_num_building as u8) << 1)
        | ((sim.is_flag_combining as u8) << 2);
    w.write_all(&[flags, 0, 0, 0])?;
    w.write_all(&sim.pc.to_le_bytes())?;
    w.write_all(&(sim.program.instructions.len() as u64).to_le_bytes())?;
    for &r in &sim.registers {
        w.write_all(&r.to_le_bytes())?;
    }
    w.write_all(&(sim.memory.len() as u64).to_le_bytes())?;
    w.write_all(&(sim.return_stack.len() as u64).to_le_bytes())?;

    // Emit memory entries in arbitrary HashMap order — the loader
    // reconstructs a HashMap regardless of insertion order.
    for (&addr, &val) in &sim.memory {
        w.write_all(&addr.to_le_bytes())?;
        w.write_all(&val.to_le_bytes())?;
    }

    for &entry in &sim.return_stack {
        w.write_all(&entry.to_le_bytes())?;
    }

    w.flush()?;
    Ok(())
}

pub fn load(sim: &mut Simulator, path: &str) -> io::Result<()> {
    let f = File::open(path)?;
    let mut r = BufReader::with_capacity(1 << 20, f);

    let mut magic = [0u8; 8];
    r.read_exact(&mut magic)?;
    if &magic != MAGIC {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "snapshot magic mismatch: got {:?}, expected {:?}",
                magic, MAGIC
            ),
        ));
    }

    let mut buf4 = [0u8; 4];
    r.read_exact(&mut buf4)?;
    let version = u32::from_le_bytes(buf4);
    if version != VERSION {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "snapshot version {} not supported (expected {})",
                version, VERSION
            ),
        ));
    }

    let mut flags_pad = [0u8; 4];
    r.read_exact(&mut flags_pad)?;
    sim.flag = (flags_pad[0] & 0b001) != 0;
    sim.is_num_building = (flags_pad[0] & 0b010) != 0;
    sim.is_flag_combining = (flags_pad[0] & 0b100) != 0;

    sim.pc = read_i64(&mut r)?;
    let prog_insns = read_u64(&mut r)?;
    let expected = sim.program.instructions.len() as u64;
    if prog_insns != expected {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "snapshot was taken against a program with {} instructions \
                 but the one loaded now has {}. You must --load-snapshot \
                 against the same .s file that was active when the snapshot \
                 was written.",
                prog_insns, expected
            ),
        ));
    }

    for i in 0..12 {
        sim.registers[i] = read_u64(&mut r)?;
    }
    let mem_count = read_u64(&mut r)?;
    let stk_count = read_u64(&mut r)?;

    sim.memory.clear();
    sim.memory.reserve(mem_count as usize);
    for _ in 0..mem_count {
        let addr = read_u64(&mut r)?;
        let val = read_u64(&mut r)?;
        sim.memory.insert(addr, val);
    }

    sim.return_stack.clear();
    sim.return_stack.reserve(stk_count as usize);
    for _ in 0..stk_count {
        sim.return_stack.push(read_i64(&mut r)?);
    }

    Ok(())
}
