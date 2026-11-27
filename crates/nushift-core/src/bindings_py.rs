use pyo3::prelude::*;

use crate::rules::{
    daily_cap_minutes as rust_daily_cap, required_break_minutes as rust_required_break,
    validate_staff_day, DayValidationInput,
};

#[pyfunction]
fn required_break_minutes(work_minutes: i32) -> i32 {
    rust_required_break(work_minutes)
}

#[pyfunction]
fn daily_cap_minutes(daily_hours: f64, overtime_allowed: bool, has_36_agreement: bool) -> i32 {
    rust_daily_cap(daily_hours, overtime_allowed, has_36_agreement)
}

#[pyfunction]
fn validate_staff_day_json(payload_json: &str) -> PyResult<String> {
    let input: DayValidationInput = serde_json::from_str(payload_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid JSON: {e}")))?;
    let res = validate_staff_day(&input);
    serde_json::to_string(&res)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("Serialize error: {e}")))
}

#[pymodule]
fn nushift_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(required_break_minutes, m)?)?;
    m.add_function(wrap_pyfunction!(daily_cap_minutes, m)?)?;
    m.add_function(wrap_pyfunction!(validate_staff_day_json, m)?)?;
    Ok(())
}
