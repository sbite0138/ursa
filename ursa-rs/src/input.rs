//! Async stdin-to-byte-queue bridge for raw-input mode.
//!
//! The MtG simulator hot path is single-threaded and executes one
//! primitive at a time. The `AInput` primitive needs to deliver (or
//! refuse to deliver, with a sentinel value) a byte on every invocation
//! without blocking. To do that without reinventing async I/O we spawn
//! an OS thread on startup whose entire job is to sit in a blocking
//! `read(stdin, 1..=N bytes)` and push whatever arrives into a shared
//! byte queue. The simulator just pops from that queue when the guest
//! executes `AInput`; if the queue is empty the simulator hands back
//! `0xFFFF_FFFF` (= -1 as i32) as the "no data" sentinel.
//!
//! This module deliberately does NOT touch termios. That means stdin is
//! still line-buffered unless the user has configured their shell
//! otherwise (e.g. via `stty raw`), and keys typed without pressing
//! Enter are held by the terminal. For a login prompt that's fine —
//! you type "root<Enter>" and the whole line lands in the queue at
//! once — but a full raw-mode path will eventually be needed for
//! interactive control characters (^C, arrow keys). Future work.

use std::collections::VecDeque;
use std::io::Read;
use std::sync::{Arc, Mutex};
use std::thread;

pub struct InputQueue {
    queue: Arc<Mutex<VecDeque<u8>>>,
}

impl InputQueue {
    /// Start the stdin reader thread and return a handle whose
    /// `pop_byte` is safe to call from the simulator hot path.
    ///
    /// The thread is deliberately never joined — on process exit the
    /// kernel reaps it. There's no clean way to cancel a blocking
    /// `read()` on stdin from another thread short of closing fd 0,
    /// and for the simulator's use case "leak until exit" is fine.
    pub fn new() -> Self {
        let queue = Arc::new(Mutex::new(VecDeque::new()));
        let qc = Arc::clone(&queue);
        thread::spawn(move || {
            let stdin = std::io::stdin();
            let mut handle = stdin.lock();
            let mut buf = [0u8; 256];
            loop {
                match handle.read(&mut buf) {
                    Ok(0) => break, // EOF — terminal closed or piped input ended
                    Ok(n) => {
                        if let Ok(mut q) = qc.lock() {
                            q.extend(buf[..n].iter().copied());
                        }
                    }
                    Err(_) => break,
                }
            }
        });
        InputQueue { queue }
    }

    /// Consume the next byte from the queue, or `None` if empty.
    pub fn pop_byte(&self) -> Option<u8> {
        self.queue.lock().ok().and_then(|mut q| q.pop_front())
    }
}
