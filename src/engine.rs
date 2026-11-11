//! Worker thread that brings the [`MorseTimer`] state machine to life.
//!
//! Data flow:
//!
//! ```text
//!  Hook-Thread (rdev)  ──Press/Release(Instant)──▶  mpsc-Kanal  ──▶  Worker
//!  Python feed_*()     ──────────────────────────▶      │            │
//!                                                        │  recv_timeout(Deadline)
//!                                                        ▼            ▼
//!                                                   MorseTimer ──▶ Sink (Callback)
//! ```
//!
//! A thread with a channel instead of a mutex around the timer, because the hook
//! callback must return very fast on Windows (otherwise it removes the low-level hook
//! after `LowLevelHooksTimeout`), and `Sender::send` doesn't block. The worker sleeps
//! with `recv_timeout` until the next pause deadline, or blocks entirely without an open
//! sequence (0% CPU, no polling interval). The Python callback (GIL!) runs on the worker
//! and never delays the hook.

use std::io;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::thread::{self, JoinHandle};
use std::time::Instant;

use crate::timing::{MorseTimer, Symbol, TimerOutput, TimingConfig};

/// Messages to the worker.
#[derive(Debug, Clone)]
pub enum WorkerMessage {
    /// Key pressed. The timestamp is taken when the event is *captured* (in the hook),
    /// not when it is processed, so channel latency doesn't distort the measurement.
    Press(Instant),
    /// Release whose symbol follows from the press duration (one Morse key).
    Release(Instant),
    /// Release of a key that always means the same symbol (separate dot and dash keys).
    ReleaseAs(Instant, Symbol),
    Configure(TimingConfig),
    Reset,
    /// Switches the measuring of press times and gaps on or off (see [`run_worker_with`]).
    /// It sits in the same queue as the key events, so it takes effect exactly between the
    /// events sent before and after it (the timestamp is the moment of the switch), and the worker answers with
    /// [`TimerOutput::Measuring`] at that place of the output stream.
    SetMeasuring(bool, Instant),
    Shutdown,
}

/// Processing loop. Blocks until `Shutdown` arrives or all senders are gone.
///
/// A free function so tests can call it synchronously with a prepared channel.
pub fn run_worker<F>(rx: Receiver<WorkerMessage>, config: TimingConfig, sink: F)
where
    F: FnMut(TimerOutput),
{
    run_worker_with(rx, config, false, sink)
}

/// Like [`run_worker`]; with `measure` it also reports [`TimerOutput::Gap`] when a press
/// arrives and [`TimerOutput::Press`] right before the symbol of a release. Symbols,
/// letters and word gaps come out exactly as without it. `measure` is the start value,
/// [`WorkerMessage::SetMeasuring`] changes it while running.
pub fn run_worker_with<F>(
    rx: Receiver<WorkerMessage>,
    config: TimingConfig,
    measure: bool,
    mut sink: F,
) where
    F: FnMut(TimerOutput),
{
    let mut measure = measure;
    let mut timer = MorseTimer::new(config);

    loop {
        // 1) wait for the next message, at most until the deadline
        let message = match timer.next_deadline() {
            // no open sequence: block indefinitely (0% CPU)
            None => match rx.recv() {
                Ok(m) => Some(m),
                Err(_) => break, // all senders dropped, shut down cleanly
            },
            Some(deadline) => {
                let now = Instant::now();
                if deadline <= now {
                    // deadline already passed. IMPORTANT: first check whether events are
                    // still waiting in the queue. If the worker lags (say the Python callback
                    // waits for the GIL), the next press can already be queued although it
                    // really happened before the deadline, and polling with `Instant::now()`
                    // would split letters wrongly. `on_press(at)` catches up due pauses using
                    // the *event* timestamp, which is correct. If the queue is empty or
                    // disconnected (`None`), polling is right.
                    rx.try_recv().ok()
                } else {
                    match rx.recv_timeout(deadline - now) {
                        Ok(m) => Some(m),
                        Err(RecvTimeoutError::Timeout) => None,
                        Err(RecvTimeoutError::Disconnected) => break,
                    }
                }
            }
        };

        // 2) handle the message or the timeout
        match message {
            None => {
                // poll(now) with now >= deadline clears the condition, so no busy loop here
                for out in timer.poll(Instant::now()) {
                    sink(out);
                }
            }
            Some(WorkerMessage::Press(at)) => {
                // a repeated press of a held key is no new press and has no gap
                let gap = if measure && !timer.is_pressed() {
                    timer.silence_before(at)
                } else {
                    None
                };
                for out in timer.on_press(at) {
                    sink(out);
                }
                if let Some(gap) = gap {
                    sink(TimerOutput::Gap(gap));
                }
            }
            Some(WorkerMessage::Release(at)) => {
                let held = timer.held_for(at).filter(|_| measure);
                if let Some(symbol) = timer.on_release(at) {
                    if let Some(held) = held {
                        sink(TimerOutput::Press(held));
                    }
                    sink(TimerOutput::Symbol(symbol));
                }
            }
            Some(WorkerMessage::ReleaseAs(at, fixed)) => {
                let held = timer.held_for(at).filter(|_| measure);
                if let Some(symbol) = timer.on_release_as(at, fixed) {
                    if let Some(held) = held {
                        sink(TimerOutput::Press(held));
                    }
                    sink(TimerOutput::Symbol(symbol));
                }
            }
            Some(WorkerMessage::Configure(cfg)) => timer.set_config(cfg),
            Some(WorkerMessage::Reset) => timer.reset(),
            Some(WorkerMessage::SetMeasuring(on, at)) => {
                // pauses that ran out before the switch (by its own timestamp, like every event, so a
                // lagging worker does not cut a letter early) still belong to the old setting
                for out in timer.poll(at) {
                    sink(out);
                }
                measure = on;
                sink(TimerOutput::Measuring(on)); // the marker the controller sorts letters by
            }
            // a half-keyed sequence is dropped on stop on purpose, a letter typed
            // afterwards would be a surprise
            Some(WorkerMessage::Shutdown) => break,
        }
    }
}

/// Owns the worker thread. `Drop` shuts it down cleanly.
pub struct EngineHandle {
    tx: Sender<WorkerMessage>,
    thread: Option<JoinHandle<()>>,
}

impl EngineHandle {
    /// Starts the worker. `sink` is called on the worker thread.
    pub fn spawn<F>(config: TimingConfig, sink: F) -> io::Result<Self>
    where
        F: FnMut(TimerOutput) + Send + 'static,
    {
        Self::spawn_with(config, false, sink)
    }

    /// Like [`Self::spawn`]; with `measure` the sink also gets press durations and gaps.
    pub fn spawn_with<F>(config: TimingConfig, measure: bool, sink: F) -> io::Result<Self>
    where
        F: FnMut(TimerOutput) + Send + 'static,
    {
        let (tx, rx) = mpsc::channel();
        let thread = thread::Builder::new()
            .name("morse-worker".into()) // easier debugging and profiling
            .spawn(move || run_worker_with(rx, config, measure, sink))?;
        Ok(Self {
            tx,
            thread: Some(thread),
        })
    }

    /// A copy of the sender, e.g. for the hook thread.
    pub fn sender(&self) -> Sender<WorkerMessage> {
        self.tx.clone()
    }

    /// Sends a message. False if the worker is no longer running.
    pub fn send(&self, message: WorkerMessage) -> bool {
        self.tx.send(message).is_ok()
    }

    /// Stops the worker and waits for it (idempotent).
    pub fn stop(&mut self) {
        let _ = self.tx.send(WorkerMessage::Shutdown);
        if let Some(thread) = self.thread.take() {
            // don't rethrow panics from the sink (say a bug in the callback), stopping
            // must always succeed
            let _ = thread.join();
        }
    }

    pub fn is_running(&self) -> bool {
        self.thread.as_ref().is_some_and(|t| !t.is_finished())
    }
}

impl Drop for EngineHandle {
    fn drop(&mut self) {
        self.stop();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    fn ms(v: u64) -> Duration {
        Duration::from_millis(v)
    }

    /// Short thresholds so tests run fast.
    fn fast_config() -> TimingConfig {
        TimingConfig::from_millis(20, 60, 0).unwrap()
    }

    #[test]
    fn queued_events_are_judged_by_event_time_not_processing_time() {
        // simulates a lagging worker: all events are 10 s in the past and sit in the
        // queue together. The symbols were really only 5 ms apart, so exactly one letter
        // ".-" (A) must come out, not "." and "-" (E, T).
        let (tx, rx) = mpsc::channel();
        let past = Instant::now() - ms(10_000);
        tx.send(WorkerMessage::Press(past)).unwrap();
        tx.send(WorkerMessage::Release(past + ms(5))).unwrap(); // dot
        tx.send(WorkerMessage::Press(past + ms(10))).unwrap();
        tx.send(WorkerMessage::Release(past + ms(50))).unwrap(); // dash
                                                                 // no Shutdown: the worker must handle the overdue deadline first and then ends
                                                                 // because the sender is dropped
        drop(tx);

        let mut outputs = Vec::new();
        run_worker(rx, fast_config(), |o| outputs.push(o));
        assert_eq!(
            outputs,
            vec![
                TimerOutput::Symbol(Symbol::Dot),
                TimerOutput::Symbol(Symbol::Dash),
                TimerOutput::Letter(".-".into()),
            ]
        );
    }

    #[test]
    fn measuring_reports_press_before_symbol_and_gap_at_the_next_press() {
        let (tx, rx) = mpsc::channel();
        let past = Instant::now() - ms(10_000);
        tx.send(WorkerMessage::Press(past)).unwrap();
        tx.send(WorkerMessage::Press(past + ms(10))).unwrap(); // key repeat: no gap, no change
        tx.send(WorkerMessage::Release(past + ms(50))).unwrap(); // dash (threshold 20)
        tx.send(WorkerMessage::Press(past + ms(80))).unwrap();
        tx.send(WorkerMessage::Release(past + ms(85))).unwrap(); // dot
        drop(tx);
        let mut outputs = Vec::new();
        run_worker_with(rx, fast_config(), true, |o| outputs.push(o));
        assert_eq!(
            outputs,
            vec![
                TimerOutput::Press(ms(50)),
                TimerOutput::Symbol(Symbol::Dash),
                TimerOutput::Gap(ms(30)),
                TimerOutput::Press(ms(5)),
                TimerOutput::Symbol(Symbol::Dot),
                TimerOutput::Letter("-.".into()),
            ]
        );
    }

    #[test]
    fn measuring_is_off_by_default_and_the_stream_has_only_the_old_events() {
        let (tx, rx) = mpsc::channel();
        let past = Instant::now() - ms(10_000);
        tx.send(WorkerMessage::Press(past)).unwrap();
        tx.send(WorkerMessage::Press(past + ms(10))).unwrap();
        tx.send(WorkerMessage::Release(past + ms(50))).unwrap();
        tx.send(WorkerMessage::Press(past + ms(80))).unwrap();
        tx.send(WorkerMessage::Release(past + ms(85))).unwrap();
        drop(tx);
        let mut outputs = Vec::new();
        run_worker(rx, fast_config(), |o| outputs.push(o));
        assert_eq!(
            outputs,
            vec![
                TimerOutput::Symbol(Symbol::Dash),
                TimerOutput::Symbol(Symbol::Dot),
                TimerOutput::Letter("-.".into()),
            ]
        );
    }

    #[test]
    fn set_measuring_switches_between_events_of_one_session() {
        let (tx, rx) = mpsc::channel();
        let past = Instant::now() - ms(10_000);
        let key = |press: u64, release: u64| {
            [
                WorkerMessage::Press(past + ms(press)),
                WorkerMessage::Release(past + ms(release)),
            ]
        };
        let send =
            |messages: [WorkerMessage; 2]| messages.into_iter().for_each(|m| tx.send(m).unwrap());
        send(key(0, 50)); // off: symbol only
        tx.send(WorkerMessage::SetMeasuring(true, past + ms(55)))
            .unwrap();
        send(key(80, 85)); // on: gap 30 and press 5
        tx.send(WorkerMessage::SetMeasuring(false, past + ms(90)))
            .unwrap();
        send(key(100, 150)); // off again
        drop(tx);
        let mut outputs = Vec::new();
        run_worker(rx, fast_config(), |o| outputs.push(o));
        assert_eq!(
            outputs,
            vec![
                TimerOutput::Symbol(Symbol::Dash),
                TimerOutput::Measuring(true),
                TimerOutput::Gap(ms(30)),
                TimerOutput::Press(ms(5)),
                TimerOutput::Symbol(Symbol::Dot),
                TimerOutput::Measuring(false),
                TimerOutput::Symbol(Symbol::Dash),
                TimerOutput::Letter("-.-".into()),
            ]
        );
    }

    #[test]
    fn a_key_held_while_measuring_starts_still_reports_its_press_time_at_release() {
        // the Python side resets the engine when it switches measuring on, so a press that began
        // before the switch normally does not exist; without the reset the worker reports it
        let (tx, rx) = mpsc::channel();
        let past = Instant::now() - ms(10_000);
        tx.send(WorkerMessage::Press(past)).unwrap();
        tx.send(WorkerMessage::SetMeasuring(true, past + ms(10)))
            .unwrap();
        tx.send(WorkerMessage::Release(past + ms(50))).unwrap();
        drop(tx);
        let mut outputs = Vec::new();
        run_worker(rx, fast_config(), |o| outputs.push(o));
        assert_eq!(
            outputs,
            vec![
                TimerOutput::Measuring(true),
                TimerOutput::Press(ms(50)),
                TimerOutput::Symbol(Symbol::Dash),
                TimerOutput::Letter("-".into()),
            ]
        );
    }

    #[test]
    fn a_letter_that_is_due_when_measuring_switches_comes_out_before_the_marker() {
        // the pause of "." ran out 10 s ago, but the switch is already in the queue: the letter belongs
        // to the old setting, so it has to precede the marker (and not be eaten by a following reset)
        let (tx, rx) = mpsc::channel();
        let past = Instant::now() - ms(10_000);
        tx.send(WorkerMessage::Press(past)).unwrap();
        tx.send(WorkerMessage::Release(past + ms(5))).unwrap();
        tx.send(WorkerMessage::SetMeasuring(true, past + ms(500)))
            .unwrap();
        tx.send(WorkerMessage::Reset).unwrap();
        drop(tx);
        let mut outputs = Vec::new();
        run_worker(rx, fast_config(), |o| outputs.push(o));
        assert_eq!(
            outputs,
            vec![
                TimerOutput::Symbol(Symbol::Dot),
                TimerOutput::Letter(".".into()),
                TimerOutput::Measuring(true),
            ]
        );
    }

    #[test]
    fn a_letter_started_after_the_switch_is_after_the_marker() {
        let (tx, rx) = mpsc::channel();
        let past = Instant::now() - ms(10_000);
        tx.send(WorkerMessage::SetMeasuring(true, past)).unwrap();
        tx.send(WorkerMessage::Press(past)).unwrap();
        tx.send(WorkerMessage::Release(past + ms(5))).unwrap();
        drop(tx);
        let mut outputs = Vec::new();
        run_worker(rx, fast_config(), |o| outputs.push(o));
        assert_eq!(outputs[0], TimerOutput::Measuring(true));
        assert!(outputs.contains(&TimerOutput::Letter(".".into())));
    }

    #[test]
    fn a_reset_with_measuring_on_forgets_the_gap_and_the_open_letter() {
        let (tx, rx) = mpsc::channel();
        let past = Instant::now() - ms(10_000);
        tx.send(WorkerMessage::SetMeasuring(true, past)).unwrap();
        tx.send(WorkerMessage::Press(past)).unwrap();
        tx.send(WorkerMessage::Release(past + ms(50))).unwrap();
        tx.send(WorkerMessage::Reset).unwrap();
        tx.send(WorkerMessage::Press(past + ms(90))).unwrap(); // no earlier release: no gap
        tx.send(WorkerMessage::Release(past + ms(95))).unwrap();
        drop(tx);
        let mut outputs = Vec::new();
        run_worker(rx, fast_config(), |o| outputs.push(o));
        assert_eq!(
            outputs,
            vec![
                TimerOutput::Measuring(true),
                TimerOutput::Press(ms(50)),
                TimerOutput::Symbol(Symbol::Dash),
                TimerOutput::Press(ms(5)),
                TimerOutput::Symbol(Symbol::Dot),
                TimerOutput::Letter(".".into()),
            ]
        );
    }

    #[test]
    fn measuring_leaves_symbols_and_letters_unchanged() {
        let run = |measure: bool| {
            let (tx, rx) = mpsc::channel();
            let past = Instant::now() - ms(10_000);
            for (press, release) in [(0, 5), (10, 50), (400, 405)] {
                tx.send(WorkerMessage::Press(past + ms(press))).unwrap();
                tx.send(WorkerMessage::Release(past + ms(release))).unwrap();
            }
            drop(tx);
            let mut outputs = Vec::new();
            run_worker_with(rx, fast_config(), measure, |o| outputs.push(o));
            outputs
        };
        let plain: Vec<_> = run(true)
            .into_iter()
            .filter(|o| !matches!(o, TimerOutput::Press(_) | TimerOutput::Gap(_)))
            .collect();
        assert_eq!(plain, run(false));
    }

    #[test]
    fn measuring_reports_nothing_for_the_first_press_or_a_release_without_press() {
        let (tx, rx) = mpsc::channel();
        let past = Instant::now() - ms(10_000);
        tx.send(WorkerMessage::Release(past)).unwrap(); // started while held
        tx.send(WorkerMessage::Press(past + ms(5))).unwrap();
        drop(tx);
        let mut outputs = Vec::new();
        run_worker_with(rx, fast_config(), true, |o| outputs.push(o));
        assert!(outputs.is_empty());
    }

    #[test]
    fn separate_keys_give_fixed_symbols() {
        // a long press on the dot key stays a dot, a short one on the dash key a dash
        let (tx, rx) = mpsc::channel();
        let past = Instant::now() - ms(10_000);
        tx.send(WorkerMessage::Press(past)).unwrap();
        tx.send(WorkerMessage::ReleaseAs(past + ms(500), Symbol::Dot))
            .unwrap();
        tx.send(WorkerMessage::Press(past + ms(510))).unwrap();
        tx.send(WorkerMessage::ReleaseAs(past + ms(515), Symbol::Dash))
            .unwrap();
        drop(tx);
        let mut outputs = Vec::new();
        run_worker(rx, fast_config(), |o| outputs.push(o));
        assert_eq!(
            outputs,
            vec![
                TimerOutput::Symbol(Symbol::Dot),
                TimerOutput::Symbol(Symbol::Dash),
                TimerOutput::Letter(".-".into()),
            ]
        );
    }

    #[test]
    fn shutdown_discards_pending_sequence() {
        let (tx, rx) = mpsc::channel();
        let now = Instant::now();
        tx.send(WorkerMessage::Press(now)).unwrap();
        tx.send(WorkerMessage::Release(now + ms(5))).unwrap();
        tx.send(WorkerMessage::Shutdown).unwrap();
        let mut outputs = Vec::new();
        run_worker(rx, TimingConfig::default(), |o| outputs.push(o));
        assert_eq!(outputs, vec![TimerOutput::Symbol(Symbol::Dot)]);
    }

    #[test]
    fn queued_reset_is_processed_before_overdue_poll() {
        // consequence of the queue-first rule: a reset already in the queue is handled
        // before the overdue poll, so no letter comes out
        let (tx, rx) = mpsc::channel();
        let past = Instant::now() - ms(10_000);
        tx.send(WorkerMessage::Press(past)).unwrap();
        tx.send(WorkerMessage::Release(past + ms(5))).unwrap();
        tx.send(WorkerMessage::Reset).unwrap();
        drop(tx);
        let mut outputs = Vec::new();
        run_worker(rx, fast_config(), |o| outputs.push(o));
        assert_eq!(outputs, vec![TimerOutput::Symbol(Symbol::Dot)]);
    }

    #[test]
    fn reset_before_deadline_prevents_letter() {
        let (tx, rx) = mpsc::channel();
        let now = Instant::now();
        // the deadline is 10 s away, so the reset surely arrives first
        let cfg = TimingConfig::from_millis(200, 10_000, 0).unwrap();
        tx.send(WorkerMessage::Press(now)).unwrap();
        tx.send(WorkerMessage::Release(now + ms(5))).unwrap();
        tx.send(WorkerMessage::Reset).unwrap();
        drop(tx);
        let mut outputs = Vec::new();
        run_worker(rx, cfg, |o| outputs.push(o));
        assert_eq!(outputs, vec![TimerOutput::Symbol(Symbol::Dot)]);
    }

    #[test]
    fn configure_message_changes_thresholds() {
        let (tx, rx) = mpsc::channel();
        let now = Instant::now();
        // threshold 1000 ms, so a 500 ms press would be a dot ...
        tx.send(WorkerMessage::Configure(
            TimingConfig::from_millis(1000, 10_000, 0).unwrap(),
        ))
        .unwrap();
        tx.send(WorkerMessage::Press(now)).unwrap();
        tx.send(WorkerMessage::Release(now + ms(500))).unwrap();
        tx.send(WorkerMessage::Shutdown).unwrap();
        let mut outputs = Vec::new();
        run_worker(rx, TimingConfig::default(), |o| outputs.push(o));
        assert_eq!(outputs, vec![TimerOutput::Symbol(Symbol::Dot)]);
    }

    #[test]
    fn spawned_engine_emits_letter_after_real_pause() {
        // end to end with a real thread and a real clock
        let collected = Arc::new(Mutex::new(Vec::new()));
        let sink_store = Arc::clone(&collected);
        let mut engine =
            EngineHandle::spawn(fast_config(), move |o| sink_store.lock().unwrap().push(o))
                .unwrap();
        assert!(engine.is_running());

        let t0 = Instant::now();
        engine.send(WorkerMessage::Press(t0));
        engine.send(WorkerMessage::Release(t0 + ms(40))); // 20 ms or more is a dash
                                                          // wait generously (60 ms pause plus thread scheduling slack for CI)
        thread::sleep(ms(400));
        engine.stop();

        let outputs = collected.lock().unwrap().clone();
        assert_eq!(
            outputs,
            vec![
                TimerOutput::Symbol(Symbol::Dash),
                TimerOutput::Letter("-".into())
            ]
        );
        assert!(!engine.is_running());
    }

    #[test]
    fn stop_is_idempotent_and_drop_is_safe() {
        let mut engine = EngineHandle::spawn(TimingConfig::default(), |_| {}).unwrap();
        engine.stop();
        engine.stop();
        assert!(
            !engine.send(WorkerMessage::Reset),
            "nach Stop nimmt niemand mehr Nachrichten an"
        );
        drop(engine); // must not hang or panic
    }

    #[test]
    fn panicking_sink_does_not_break_stop() {
        let mut engine = EngineHandle::spawn(fast_config(), |_| panic!("Bug im Callback")).unwrap();
        let t0 = Instant::now();
        engine.send(WorkerMessage::Press(t0));
        engine.send(WorkerMessage::Release(t0 + ms(1)));
        thread::sleep(ms(100));
        engine.stop(); // join() returns Err(panic), which is swallowed
        assert!(!engine.is_running());
    }
}
