//! PyO3 bridge.
//!
//! Exports the Python module `mourse_decoder._morse_core` with:
//!
//! * `MorseEngine`: starts/stops the worker and the global hook and calls a Python
//!   callback `callback(kind: str, payload: str)` for every event, where `kind` is
//!   one of "symbol", "letter", "word_gap".
//! * `inject_text(text)`, `inject_backspaces(n)`: native text injection.
//! * `begin_capture()`, `poll_capture()`, `cancel_capture()`: read the next key or
//!   mouse button for the key binding.
//! * `classify_duration(ms, threshold_ms)`, `available_triggers()`, `hook_error()`:
//!   helpers for the UI and tests.
//!
//! One callback with a `kind` string instead of three callbacks: the FFI boundary stays
//! small and a new event type needs no signature change.

use std::sync::Mutex;
use std::time::{Duration, Instant};

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;

use crate::engine::{EngineHandle, WorkerMessage};
use crate::lock_or_recover;
use crate::timing::{classify, Symbol, TimerOutput, TimingConfig};
use crate::trigger::TriggerKey;

/// Mutable state behind a mutex.
///
/// PyO3 requires `Sync` for `#[pyclass]` because Python objects can be used from
/// several threads (tray, Tk main loop, ...), so methods take `&self` and lock inside.
struct EngineState {
    config: TimingConfig,
    trigger: TriggerKey,
    handle: Option<EngineHandle>,
    hooked: bool,
}

#[pyclass(name = "MorseEngine", module = "mourse_decoder._morse_core")]
pub struct PyMorseEngine {
    state: Mutex<EngineState>,
}

fn config_from_ms(dot: u64, letter: u64, word: u64) -> PyResult<TimingConfig> {
    // Rust error to Python ValueError with a German message
    TimingConfig::from_millis(dot, letter, word).map_err(|e| PyValueError::new_err(e.to_string()))
}

fn parse_trigger(name: &str) -> PyResult<TriggerKey> {
    name.parse::<TriggerKey>()
        .map_err(|e| PyValueError::new_err(e.to_string()))
}

/// Translate a Rust output into the (kind, payload) pair for Python.
fn to_python_event(output: TimerOutput) -> (&'static str, String) {
    match output {
        TimerOutput::Symbol(s) => ("symbol", s.as_char().to_string()),
        TimerOutput::Letter(seq) => ("letter", seq),
        TimerOutput::WordGap => ("word_gap", String::new()),
    }
}

#[pymethods]
impl PyMorseEngine {
    #[new]
    #[pyo3(signature = (dot_threshold_ms = 200, letter_gap_ms = 600, word_gap_ms = 0, trigger = "ScrollLock"))]
    fn new(
        dot_threshold_ms: u64,
        letter_gap_ms: u64,
        word_gap_ms: u64,
        trigger: &str,
    ) -> PyResult<Self> {
        Ok(Self {
            state: Mutex::new(EngineState {
                config: config_from_ms(dot_threshold_ms, letter_gap_ms, word_gap_ms)?,
                trigger: parse_trigger(trigger)?,
                handle: None,
                hooked: false,
            }),
        })
    }

    /// Starts the worker thread.
    ///
    /// `use_hook=False` starts only the worker without the global hook; input then
    /// comes through `feed_press()` / `feed_release()` (tests, headless).
    #[pyo3(signature = (callback, use_hook = true))]
    fn start(&self, callback: Py<PyAny>, use_hook: bool) -> PyResult<()> {
        let mut state = lock_or_recover(&self.state);
        if state.handle.is_some() {
            return Err(PyRuntimeError::new_err("MorseEngine läuft bereits"));
        }

        // the sink runs on the Rust worker thread and has to attach to the interpreter
        // (take the GIL) to call Python
        let sink = move |output: TimerOutput| {
            let (kind, payload) = to_python_event(output);
            Python::attach(|py| {
                if let Err(err) = callback.call1(py, (kind, payload)) {
                    // an exception in the callback must not kill the worker;
                    // write_unraisable prints it like a __del__ error (or hands it to
                    // sys.unraisablehook, which tests can use)
                    err.write_unraisable(py, None);
                }
            });
        };

        let handle = EngineHandle::spawn(state.config, sink).map_err(|e| {
            PyRuntimeError::new_err(format!("Worker-Thread konnte nicht starten: {e}"))
        })?;

        #[cfg(feature = "native-io")]
        if use_hook {
            crate::hooks::install_route(state.trigger, handle.sender()).map_err(|e| {
                PyRuntimeError::new_err(format!("Input-Hook konnte nicht starten: {e}"))
            })?;
        }
        #[cfg(not(feature = "native-io"))]
        if use_hook {
            return Err(PyRuntimeError::new_err(
                "Ohne Feature 'native-io' gebaut: keine globalen Hooks verfügbar",
            ));
        }

        state.hooked = use_hook;
        state.handle = Some(handle);
        Ok(())
    }

    /// Stops the worker and detaches the hook (idempotent).
    fn stop(&self, py: Python<'_>) {
        let handle = {
            let mut state = lock_or_recover(&self.state);
            #[cfg(feature = "native-io")]
            if state.hooked {
                crate::hooks::clear_route();
            }
            state.hooked = false;
            state.handle.take()
        };
        // critical: join the worker WITHOUT holding the GIL. The worker may be waiting in
        // `Python::attach` for the GIL to run the callback, and if we held it while
        // joining, both threads would wait for each other (deadlock)
        py.detach(move || drop(handle));
    }

    /// New thresholds; applied immediately, even while running.
    #[pyo3(signature = (dot_threshold_ms, letter_gap_ms, word_gap_ms = 0))]
    fn configure(
        &self,
        dot_threshold_ms: u64,
        letter_gap_ms: u64,
        word_gap_ms: u64,
    ) -> PyResult<()> {
        let config = config_from_ms(dot_threshold_ms, letter_gap_ms, word_gap_ms)?;
        let mut state = lock_or_recover(&self.state);
        state.config = config;
        if let Some(handle) = &state.handle {
            handle.send(WorkerMessage::Configure(config));
        }
        Ok(())
    }

    /// Discards a half-keyed sequence (e.g. on an alphabet change).
    fn reset(&self) {
        if let Some(handle) = &lock_or_recover(&self.state).handle {
            handle.send(WorkerMessage::Reset);
        }
    }

    #[getter]
    fn trigger(&self) -> String {
        lock_or_recover(&self.state).trigger.name().to_string()
    }

    #[setter]
    fn set_trigger(&self, name: &str) -> PyResult<()> {
        let trigger = parse_trigger(name)?;
        let mut state = lock_or_recover(&self.state);
        state.trigger = trigger;
        #[cfg(feature = "native-io")]
        if state.hooked {
            crate::hooks::set_trigger(trigger);
        }
        Ok(())
    }

    /// While true, the trigger's normal function (typing a space, clicking) is hidden from
    /// other apps and only the decoded text gets typed. The toggle shortcut can flip it.
    #[getter]
    fn block_input(&self) -> bool {
        #[cfg(feature = "native-io")]
        {
            crate::hooks::blocking()
        }
        #[cfg(not(feature = "native-io"))]
        {
            false
        }
    }

    #[setter]
    fn set_block_input(&self, on: bool) {
        #[cfg(feature = "native-io")]
        crate::hooks::set_blocking(on);
        #[cfg(not(feature = "native-io"))]
        let _ = on;
    }

    /// Morse input on/off. While off, nothing is blocked. The shortcut flips it.
    #[getter]
    fn active(&self) -> bool {
        #[cfg(feature = "native-io")]
        {
            crate::hooks::active()
        }
        #[cfg(not(feature = "native-io"))]
        {
            true
        }
    }

    #[setter]
    fn set_active(&self, on: bool) {
        #[cfg(feature = "native-io")]
        crate::hooks::set_active(on);
        #[cfg(not(feature = "native-io"))]
        let _ = on;
    }

    /// Shortcut such as "Ctrl+Shift+Pause" that turns Morse input on or off; "" removes it.
    #[setter]
    fn set_block_shortcut(&self, spec: &str) -> PyResult<()> {
        #[cfg(feature = "native-io")]
        {
            crate::hooks::set_shortcut(spec).map_err(PyValueError::new_err)
        }
        #[cfg(not(feature = "native-io"))]
        {
            let _ = spec;
            Ok(())
        }
    }

    #[getter]
    fn is_running(&self) -> bool {
        lock_or_recover(&self.state)
            .handle
            .as_ref()
            .is_some_and(EngineHandle::is_running)
    }

    #[getter]
    fn dot_threshold_ms(&self) -> u128 {
        lock_or_recover(&self.state)
            .config
            .dot_threshold
            .as_millis()
    }

    #[getter]
    fn letter_gap_ms(&self) -> u128 {
        lock_or_recover(&self.state).config.letter_gap.as_millis()
    }

    #[getter]
    fn word_gap_ms(&self) -> u128 {
        lock_or_recover(&self.state)
            .config
            .word_gap
            .map_or(0, |d| d.as_millis())
    }

    /// Manual key press (no hook), timestamped now.
    fn feed_press(&self) -> PyResult<()> {
        self.feed(WorkerMessage::Press(Instant::now()))
    }

    /// Manual release (no hook), timestamped now.
    fn feed_release(&self) -> PyResult<()> {
        self.feed(WorkerMessage::Release(Instant::now()))
    }

    fn __repr__(&self) -> String {
        let state = lock_or_recover(&self.state);
        format!(
            "MorseEngine(trigger={}, dot_threshold_ms={}, letter_gap_ms={}, word_gap_ms={}, running={})",
            state.trigger,
            state.config.dot_threshold.as_millis(),
            state.config.letter_gap.as_millis(),
            state.config.word_gap.map_or(0, |d| d.as_millis()),
            state.handle.is_some()
        )
    }
}

impl PyMorseEngine {
    fn feed(&self, message: WorkerMessage) -> PyResult<()> {
        let state = lock_or_recover(&self.state);
        match &state.handle {
            Some(handle) if handle.send(message) => Ok(()),
            _ => Err(PyRuntimeError::new_err("MorseEngine ist nicht gestartet")),
        }
    }
}

/// Classifies a duration; exposed for tests and the settings UI.
#[pyfunction]
fn classify_duration(duration_ms: f64, threshold_ms: f64) -> PyResult<&'static str> {
    if !(duration_ms.is_finite() && threshold_ms.is_finite())
        || duration_ms < 0.0
        || threshold_ms <= 0.0
    {
        return Err(PyValueError::new_err(
            "Dauer muss ≥ 0 und Schwelle > 0 sein",
        ));
    }
    let symbol = classify(
        Duration::from_secs_f64(duration_ms / 1000.0),
        Duration::from_secs_f64(threshold_ms / 1000.0),
    );
    Ok(match symbol {
        Symbol::Dot => ".",
        Symbol::Dash => "-",
    })
}

/// All valid trigger names (source of truth for the UI).
#[pyfunction]
fn available_triggers() -> Vec<String> {
    TriggerKey::ALL
        .iter()
        .map(|t| t.name().into_owned())
        .collect()
}

/// Starts "wait for input": the next key press or click anywhere on the system is
/// captured (see `poll_capture`), extra keys and Mouse 4/5 included.
#[pyfunction]
fn begin_capture() -> PyResult<()> {
    #[cfg(feature = "native-io")]
    {
        crate::hooks::begin_capture().map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }
    #[cfg(not(feature = "native-io"))]
    {
        Err(PyRuntimeError::new_err("Ohne Feature 'native-io' gebaut"))
    }
}

/// Cancels "wait for input".
#[pyfunction]
fn cancel_capture() {
    #[cfg(feature = "native-io")]
    crate::hooks::cancel_capture();
}

/// Trigger name of the captured input (once), or `None` while nothing was pressed.
#[pyfunction]
fn poll_capture() -> Option<String> {
    #[cfg(feature = "native-io")]
    {
        crate::hooks::take_capture()
    }
    #[cfg(not(feature = "native-io"))]
    {
        None
    }
}

/// Error message of the hook thread, or `None`.
#[pyfunction]
fn hook_error() -> Option<String> {
    #[cfg(feature = "native-io")]
    {
        crate::hooks::hook_error()
    }
    #[cfg(not(feature = "native-io"))]
    {
        Some("Ohne Feature 'native-io' gebaut".to_string())
    }
}

/// Types text into the active app. Releases the GIL meanwhile because SendInput/X11
/// can take a few milliseconds.
#[pyfunction]
fn inject_text(py: Python<'_>, text: String) -> PyResult<()> {
    #[cfg(feature = "native-io")]
    {
        py.detach(move || crate::injector::inject_text(&text))
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }
    #[cfg(not(feature = "native-io"))]
    {
        let _ = (py, text);
        Err(PyRuntimeError::new_err("Ohne Feature 'native-io' gebaut"))
    }
}

/// Deletes `count` characters left of the cursor.
#[pyfunction]
fn inject_backspaces(py: Python<'_>, count: u32) -> PyResult<()> {
    #[cfg(feature = "native-io")]
    {
        py.detach(move || crate::injector::inject_backspaces(count))
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }
    #[cfg(not(feature = "native-io"))]
    {
        let _ = (py, count);
        Err(PyRuntimeError::new_err("Ohne Feature 'native-io' gebaut"))
    }
}

/// Module init. The name must match `module-name` in pyproject.toml.
#[pymodule]
#[pyo3(name = "_morse_core")]
fn morse_core_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyMorseEngine>()?;
    m.add_function(wrap_pyfunction!(classify_duration, m)?)?;
    m.add_function(wrap_pyfunction!(available_triggers, m)?)?;
    m.add_function(wrap_pyfunction!(hook_error, m)?)?;
    m.add_function(wrap_pyfunction!(begin_capture, m)?)?;
    m.add_function(wrap_pyfunction!(cancel_capture, m)?)?;
    m.add_function(wrap_pyfunction!(poll_capture, m)?)?;
    m.add_function(wrap_pyfunction!(inject_text, m)?)?;
    m.add_function(wrap_pyfunction!(inject_backspaces, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add("NATIVE_IO", cfg!(feature = "native-io"))?;
    Ok(())
}
