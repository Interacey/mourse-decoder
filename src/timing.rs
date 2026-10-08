//! Timing logic as a pure state machine.
//!
//! Turns a stream of press/release timestamps into Morse symbols (`.`/`-`), finished
//! sequences (letters) and optional word gaps. It never reads the clock itself, every
//! time comes in as an `Instant`, so tests are exactly reproducible ("release exactly
//! 199 ms after press") and run in microseconds without real sleeps.
//!
//! Timeline (example ".-" = A, letter_gap = 600 ms):
//!
//! ```text
//! press      release   press           release              Deadline
//!   |---80ms---|  120ms  |-----300ms------|------600ms----------|
//!        "."                   "-"                        Letter(".-")
//! ```

use std::fmt;
use std::time::{Duration, Instant};

/// Upper limit for a single sequence.
///
/// The longest regular character has 7 symbols (`$` = `...-..-`). The cap protects
/// against unbounded growth if someone keys forever without pausing; 32 leaves room.
pub const MAX_SEQUENCE_LEN: usize = 32;

/// A single Morse symbol.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Symbol {
    Dot,
    Dash,
}

impl Symbol {
    /// Text form, as used in the JSON alphabets.
    pub fn as_char(self) -> char {
        match self {
            Symbol::Dot => '.',
            Symbol::Dash => '-',
        }
    }
}

/// Everything the state machine reports.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TimerOutput {
    /// A symbol was recognized (right after the release), useful for live feedback.
    Symbol(Symbol),
    /// A sequence is finished (pause >= letter_gap), so SOS arrives as three separate
    /// outputs `"..."`, `"---"`, `"..."`. Looking it up in the alphabet is left to Python.
    Letter(String),
    /// A longer pause (>= word_gap) after at least one letter.
    WordGap,
}

/// Error for an invalid configuration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConfigError {
    ZeroDotThreshold,
    ZeroLetterGap,
    WordGapNotLongerThanLetterGap {
        letter_gap_ms: u128,
        word_gap_ms: u128,
    },
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ConfigError::ZeroDotThreshold => write!(f, "dot_threshold_ms muss größer als 0 sein"),
            ConfigError::ZeroLetterGap => write!(f, "letter_gap_ms muss größer als 0 sein"),
            ConfigError::WordGapNotLongerThanLetterGap {
                letter_gap_ms,
                word_gap_ms,
            } => write!(
                f,
                "word_gap_ms ({word_gap_ms}) muss größer als letter_gap_ms ({letter_gap_ms}) sein \
                 oder 0 (deaktiviert)"
            ),
        }
    }
}

impl std::error::Error for ConfigError {}

/// Thresholds of the timing logic.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TimingConfig {
    /// Press shorter than this is a dot, otherwise a dash (default 200 ms).
    pub dot_threshold: Duration,
    /// Silence after the last release that finishes the sequence (default 600 ms).
    pub letter_gap: Duration,
    /// Optional word gap, `None` = off. Without one you can't enter spaces. ITU makes a
    /// letter gap 3 units and a word gap 7, so the frontend default is 600 * 7/3 = 1400 ms.
    pub word_gap: Option<Duration>,
}

impl Default for TimingConfig {
    fn default() -> Self {
        Self {
            dot_threshold: Duration::from_millis(200),
            letter_gap: Duration::from_millis(600),
            word_gap: None,
        }
    }
}

impl TimingConfig {
    /// Validating constructor.
    pub fn new(
        dot_threshold: Duration,
        letter_gap: Duration,
        word_gap: Option<Duration>,
    ) -> Result<Self, ConfigError> {
        if dot_threshold.is_zero() {
            return Err(ConfigError::ZeroDotThreshold);
        }
        if letter_gap.is_zero() {
            return Err(ConfigError::ZeroLetterGap);
        }
        if let Some(word_gap) = word_gap {
            // a word gap that isn't longer than the letter gap would fire before or with
            // the letter, which makes no sense, so reject it early
            if word_gap <= letter_gap {
                return Err(ConfigError::WordGapNotLongerThanLetterGap {
                    letter_gap_ms: letter_gap.as_millis(),
                    word_gap_ms: word_gap.as_millis(),
                });
            }
        }
        Ok(Self {
            dot_threshold,
            letter_gap,
            word_gap,
        })
    }

    /// Convenient constructor in milliseconds, as the Python side uses it.
    /// `word_gap_ms == 0` means off.
    pub fn from_millis(
        dot_threshold_ms: u64,
        letter_gap_ms: u64,
        word_gap_ms: u64,
    ) -> Result<Self, ConfigError> {
        let word_gap = (word_gap_ms > 0).then(|| Duration::from_millis(word_gap_ms));
        Self::new(
            Duration::from_millis(dot_threshold_ms),
            Duration::from_millis(letter_gap_ms),
            word_gap,
        )
    }
}

/// Classifies a press duration. `duration == threshold` counts as a dash.
pub fn classify(duration: Duration, threshold: Duration) -> Symbol {
    if duration < threshold {
        Symbol::Dot
    } else {
        Symbol::Dash
    }
}

/// The state machine itself.
#[derive(Debug, Clone)]
pub struct MorseTimer {
    config: TimingConfig,
    /// `Some(t)` while the key is held.
    pressed_at: Option<Instant>,
    /// Symbols collected for the current sequence, e.g. ".-".
    buffer: String,
    /// Time of the last release, the reference point for all pauses.
    last_release: Option<Instant>,
    /// True if a symbol arrived since the last word gap, so one may still be reported.
    word_gap_pending: bool,
}

impl MorseTimer {
    pub fn new(config: TimingConfig) -> Self {
        Self {
            config,
            pressed_at: None,
            buffer: String::new(),
            last_release: None,
            word_gap_pending: false,
        }
    }

    pub fn config(&self) -> TimingConfig {
        self.config
    }

    /// Take new thresholds without losing a half-keyed sequence.
    pub fn set_config(&mut self, config: TimingConfig) {
        self.config = config;
        if config.word_gap.is_none() {
            self.word_gap_pending = false;
        }
    }

    pub fn is_pressed(&self) -> bool {
        self.pressed_at.is_some()
    }

    /// The sequence collected so far that isn't finished yet.
    pub fn pending_sequence(&self) -> &str {
        &self.buffer
    }

    /// Full reset (e.g. on an alphabet change or when disabled).
    pub fn reset(&mut self) {
        self.pressed_at = None;
        self.buffer.clear();
        self.last_release = None;
        self.word_gap_pending = false;
    }

    /// The key was pressed.
    ///
    /// Returns outputs that were already due *before* this press. The worker thread may
    /// miss a deadline under heavy load and only see the next press, and without this
    /// catch-up the old sequence would merge with the new one ("." + "-" = "A" instead
    /// of "E", "T").
    pub fn on_press(&mut self, at: Instant) -> Vec<TimerOutput> {
        if self.pressed_at.is_some() {
            // key repeat: the OS sends KeyDown repeatedly while a key is held, and only the
            // first counts, otherwise every dash would be read as a chain of short dots
            return Vec::new();
        }
        let overdue = self.poll(at);
        self.pressed_at = Some(at);
        overdue
    }

    /// The key was released. Returns the recognized symbol.
    ///
    /// A release without a press (the engine started while the key was held) is ignored.
    pub fn on_release(&mut self, at: Instant) -> Option<Symbol> {
        let pressed_at = self.pressed_at.take()?;
        // saturating: guards against timestamps from different sources running backwards,
        // which then gives 0 ms, a dot
        let duration = at.saturating_duration_since(pressed_at);
        let symbol = classify(duration, self.config.dot_threshold);
        if self.buffer.len() < MAX_SEQUENCE_LEN {
            self.buffer.push(symbol.as_char());
        }
        self.last_release = Some(at);
        self.word_gap_pending = self.config.word_gap.is_some();
        Some(symbol)
    }

    /// Checks whether pauses have elapsed at time `now`.
    pub fn poll(&mut self, now: Instant) -> Vec<TimerOutput> {
        let mut out = Vec::new();
        if self.pressed_at.is_some() {
            // while the key is held there is no pause by definition
            return out;
        }
        let Some(last_release) = self.last_release else {
            return out;
        };
        let silence = now.saturating_duration_since(last_release);

        if !self.buffer.is_empty() && silence >= self.config.letter_gap {
            out.push(TimerOutput::Letter(std::mem::take(&mut self.buffer)));
        }
        if let Some(word_gap) = self.config.word_gap {
            // `buffer.is_empty()` guarantees the order letter, then word gap
            if self.word_gap_pending && self.buffer.is_empty() && silence >= word_gap {
                self.word_gap_pending = false;
                out.push(TimerOutput::WordGap);
            }
        }
        out
    }

    /// Next moment at which `poll` could return something.
    ///
    /// The worker sleeps exactly until this deadline instead of polling at a fixed
    /// interval, which gives 0% CPU when idle.
    pub fn next_deadline(&self) -> Option<Instant> {
        if self.pressed_at.is_some() {
            return None;
        }
        let last_release = self.last_release?;
        if !self.buffer.is_empty() {
            return last_release.checked_add(self.config.letter_gap);
        }
        match (self.word_gap_pending, self.config.word_gap) {
            (true, Some(word_gap)) => last_release.checked_add(word_gap),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ms(v: u64) -> Duration {
        Duration::from_millis(v)
    }

    /// Helper: simulates a key with a virtual clock.
    struct Keyer {
        timer: MorseTimer,
        base: Instant,
        now: Duration,
        outputs: Vec<TimerOutput>,
    }

    impl Keyer {
        fn new(config: TimingConfig) -> Self {
            Self {
                timer: MorseTimer::new(config),
                base: Instant::now(),
                now: Duration::ZERO,
                outputs: Vec::new(),
            }
        }
        fn at(&self) -> Instant {
            self.base + self.now
        }
        /// Holds `hold` ms and then waits `pause` ms (polling at the end).
        fn key(&mut self, hold: u64, pause: u64) {
            let at = self.at();
            self.outputs.extend(self.timer.on_press(at));
            self.now += ms(hold);
            let at = self.at();
            if let Some(s) = self.timer.on_release(at) {
                self.outputs.push(TimerOutput::Symbol(s));
            }
            self.wait(pause);
        }
        fn wait(&mut self, pause: u64) {
            self.now += ms(pause);
            let at = self.at();
            self.outputs.extend(self.timer.poll(at));
        }
        fn letters(&self) -> Vec<String> {
            self.outputs
                .iter()
                .filter_map(|o| match o {
                    TimerOutput::Letter(l) => Some(l.clone()),
                    TimerOutput::WordGap => Some(" ".into()),
                    _ => None,
                })
                .collect()
        }
    }

    #[test]
    fn classify_respects_threshold_boundary() {
        // under 200 is a dot, 200 or more a dash; test the boundary explicitly
        assert_eq!(classify(ms(0), ms(200)), Symbol::Dot);
        assert_eq!(classify(ms(199), ms(200)), Symbol::Dot);
        assert_eq!(classify(ms(200), ms(200)), Symbol::Dash);
        assert_eq!(classify(ms(5000), ms(200)), Symbol::Dash);
    }

    #[test]
    fn classify_has_sub_millisecond_resolution() {
        // 199.999 ms is still a dot, so nothing is rounded to ms
        let almost = Duration::from_micros(199_999);
        assert_eq!(classify(almost, ms(200)), Symbol::Dot);
    }

    #[test]
    fn single_dot_becomes_letter_after_gap() {
        let mut k = Keyer::new(TimingConfig::default());
        k.key(80, 599);
        assert!(
            k.letters().is_empty(),
            "599 ms < letter_gap: noch nichts fertig"
        );
        k.wait(1);
        assert_eq!(k.letters(), vec!["."]);
    }

    #[test]
    fn sos_is_split_into_three_letters() {
        let mut k = Keyer::new(TimingConfig::default());
        for _ in 0..3 {
            k.key(80, 100);
        }
        k.wait(600);
        for _ in 0..3 {
            k.key(300, 100);
        }
        k.wait(600);
        for _ in 0..3 {
            k.key(80, 100);
        }
        k.wait(600);
        assert_eq!(k.letters(), vec!["...", "---", "..."]);
    }

    #[test]
    fn symbols_are_reported_immediately_on_release() {
        let mut k = Keyer::new(TimingConfig::default());
        k.key(50, 0);
        k.key(250, 0);
        let symbols: Vec<_> = k
            .outputs
            .iter()
            .filter(|o| matches!(o, TimerOutput::Symbol(_)))
            .cloned()
            .collect();
        assert_eq!(
            symbols,
            vec![
                TimerOutput::Symbol(Symbol::Dot),
                TimerOutput::Symbol(Symbol::Dash)
            ]
        );
        assert_eq!(k.timer.pending_sequence(), ".-");
    }

    #[test]
    fn key_repeat_presses_are_ignored() {
        let mut timer = MorseTimer::new(TimingConfig::default());
        let t0 = Instant::now();
        timer.on_press(t0);
        // OS auto-repeat: more KeyDowns while held
        timer.on_press(t0 + ms(30));
        timer.on_press(t0 + ms(60));
        // measured from the FIRST press, so 300 ms is a dash
        assert_eq!(timer.on_release(t0 + ms(300)), Some(Symbol::Dash));
    }

    #[test]
    fn release_without_press_is_ignored() {
        let mut timer = MorseTimer::new(TimingConfig::default());
        assert_eq!(timer.on_release(Instant::now()), None);
        assert_eq!(timer.pending_sequence(), "");
        assert_eq!(timer.next_deadline(), None);
    }

    #[test]
    fn no_letter_while_key_is_held() {
        let mut timer = MorseTimer::new(TimingConfig::default());
        let t0 = Instant::now();
        timer.on_press(t0);
        timer.on_release(t0 + ms(50));
        timer.on_press(t0 + ms(100));
        // a very long dash, held 5 s, must not trigger a pause
        assert!(timer.poll(t0 + ms(5000)).is_empty());
        assert_eq!(timer.next_deadline(), None);
    }

    #[test]
    fn overdue_letter_is_flushed_on_next_press() {
        // the worker missed the deadline; the next press must still finish the old
        // sequence correctly
        let mut timer = MorseTimer::new(TimingConfig::default());
        let t0 = Instant::now();
        timer.on_press(t0);
        timer.on_release(t0 + ms(50));
        let out = timer.on_press(t0 + ms(2000));
        assert_eq!(out, vec![TimerOutput::Letter(".".into())]);
        timer.on_release(t0 + ms(2400));
        assert_eq!(timer.pending_sequence(), "-");
    }

    #[test]
    fn deadline_points_to_letter_gap_then_word_gap() {
        let cfg = TimingConfig::from_millis(200, 600, 1400).unwrap();
        let mut timer = MorseTimer::new(cfg);
        let t0 = Instant::now();
        assert_eq!(
            timer.next_deadline(),
            None,
            "Leerlauf: keine Deadline = 0 % CPU"
        );
        timer.on_press(t0);
        timer.on_release(t0 + ms(50));
        assert_eq!(timer.next_deadline(), Some(t0 + ms(650)));
        timer.poll(t0 + ms(650));
        assert_eq!(timer.next_deadline(), Some(t0 + ms(1450)));
        timer.poll(t0 + ms(1450));
        assert_eq!(timer.next_deadline(), None);
    }

    #[test]
    fn word_gap_is_emitted_once_after_letter() {
        let cfg = TimingConfig::from_millis(200, 600, 1400).unwrap();
        let mut k = Keyer::new(cfg);
        k.key(50, 650); // "." done
        k.wait(800); // 1450 ms of silence in total, so a word gap
        k.wait(5000); // more silence, but NO second word gap
        assert_eq!(k.letters(), vec![".", " "]);
    }

    #[test]
    fn letter_and_word_gap_in_one_poll_keep_order() {
        let cfg = TimingConfig::from_millis(200, 600, 1400).unwrap();
        let mut timer = MorseTimer::new(cfg);
        let t0 = Instant::now();
        timer.on_press(t0);
        timer.on_release(t0 + ms(50));
        // a single, very late poll must return both in the right order
        let out = timer.poll(t0 + ms(10_000));
        assert_eq!(
            out,
            vec![TimerOutput::Letter(".".into()), TimerOutput::WordGap]
        );
    }

    #[test]
    fn word_gap_disabled_by_default() {
        let mut k = Keyer::new(TimingConfig::default());
        k.key(50, 10_000);
        assert_eq!(k.letters(), vec!["."]);
    }

    #[test]
    fn sequence_length_is_capped() {
        let mut timer = MorseTimer::new(TimingConfig::default());
        let mut t = Instant::now();
        for _ in 0..(MAX_SEQUENCE_LEN + 10) {
            timer.on_press(t);
            t += ms(10);
            timer.on_release(t);
            t += ms(10);
        }
        assert_eq!(timer.pending_sequence().len(), MAX_SEQUENCE_LEN);
    }

    #[test]
    fn reset_clears_everything() {
        let mut timer = MorseTimer::new(TimingConfig::from_millis(200, 600, 1400).unwrap());
        let t0 = Instant::now();
        timer.on_press(t0);
        timer.on_release(t0 + ms(10));
        timer.on_press(t0 + ms(20));
        timer.reset();
        assert!(!timer.is_pressed());
        assert_eq!(timer.pending_sequence(), "");
        assert_eq!(timer.next_deadline(), None);
        assert!(timer.poll(t0 + ms(60_000)).is_empty());
    }

    #[test]
    fn set_config_keeps_pending_sequence() {
        let mut timer = MorseTimer::new(TimingConfig::default());
        let t0 = Instant::now();
        timer.on_press(t0);
        timer.on_release(t0 + ms(150)); // a dot with a 200 ms threshold
        timer.set_config(TimingConfig::from_millis(100, 300, 0).unwrap());
        assert_eq!(timer.pending_sequence(), ".");
        // the new pause (300 ms) applies immediately
        assert_eq!(
            timer.poll(t0 + ms(450)),
            vec![TimerOutput::Letter(".".into())]
        );
        // the new threshold (100 ms) applies to the next symbol
        timer.on_press(t0 + ms(500));
        assert_eq!(timer.on_release(t0 + ms(650)), Some(Symbol::Dash));
    }

    #[test]
    fn config_validation() {
        assert_eq!(
            TimingConfig::from_millis(0, 600, 0),
            Err(ConfigError::ZeroDotThreshold)
        );
        assert_eq!(
            TimingConfig::from_millis(200, 0, 0),
            Err(ConfigError::ZeroLetterGap)
        );
        assert!(matches!(
            TimingConfig::from_millis(200, 600, 600),
            Err(ConfigError::WordGapNotLongerThanLetterGap { .. })
        ));
        let ok = TimingConfig::from_millis(200, 600, 0).unwrap();
        assert_eq!(ok.word_gap, None);
        assert_eq!(TimingConfig::default(), ok);
    }

    #[test]
    fn config_error_messages_are_human_readable() {
        let err = TimingConfig::from_millis(200, 600, 500).unwrap_err();
        assert!(err.to_string().contains("word_gap_ms (500)"));
    }
}
