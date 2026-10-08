//! # morse_core: the Rust core of Mourse Decoder
//!
//! Modules, from pure to platform-specific:
//!
//! | Module      | Job                                              | Depends on |
//! |-------------|--------------------------------------------------|------------|
//! | [`timing`]  | State machine: press/release to dot, dash, letter | `std`      |
//! | [`trigger`] | Which key or mouse button is the Morse key?      | `std`      |
//! | [`engine`]  | Worker thread that handles events and pauses     | `std`      |
//! | `hooks`     | Global low-level hooks (feature `native-io`)     | `rdev`     |
//! | `injector`  | Type text into the active app (`native-io`)      | `enigo`    |
//! | `python`    | PyO3 bindings (feature `python`)                 | `pyo3`     |
//!
//! Everything error-prone (thresholds, pauses, key repeat, ordering) sits in modules
//! without OS or Python dependencies so it can be unit-tested deterministically.
//! The platform modules are thin adapters.

pub mod engine;
pub mod timing;
pub mod trigger;

#[cfg(feature = "native-io")]
pub mod hooks;
#[cfg(feature = "native-io")]
pub mod injector;

#[cfg(feature = "python")]
mod python;

/// Lock a mutex and keep using it even if it was poisoned.
///
/// Our state (an Option, a timestamp) stays consistent after a panic, and calling
/// `unwrap()` instead would let one failure in the hook callback disable the whole app.
pub(crate) fn lock_or_recover<T>(mutex: &std::sync::Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}
