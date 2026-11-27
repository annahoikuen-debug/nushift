pub mod rules;

#[cfg(feature = "python")]
mod bindings_py;

#[cfg(feature = "wasm")]
mod bindings_wasm;

pub use rules::{
    daily_cap_minutes, required_break_minutes, validate_staff_day, DayValidationInput,
    DayValidationIssue, DayValidationResult,
};
