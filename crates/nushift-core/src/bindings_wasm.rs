use wasm_bindgen::prelude::*;

use crate::rules::{
    daily_cap_minutes as rust_daily_cap, required_break_minutes as rust_required_break,
    validate_staff_day, DayValidationInput,
};

#[wasm_bindgen]
pub fn required_break_minutes(work_minutes: i32) -> i32 {
    rust_required_break(work_minutes)
}

#[wasm_bindgen]
pub fn daily_cap_minutes(daily_hours: f64, overtime_allowed: bool, has_36_agreement: bool) -> i32 {
    rust_daily_cap(daily_hours, overtime_allowed, has_36_agreement)
}

#[wasm_bindgen]
pub fn validate_staff_day_json(payload_json: &str) -> Result<String, JsValue> {
    let input: DayValidationInput = serde_json::from_str(payload_json)
        .map_err(|e| JsValue::from_str(&format!("Invalid JSON: {e}")))?;
    let res = validate_staff_day(&input);
    serde_json::to_string(&res).map_err(|e| JsValue::from_str(&format!("Serialize error: {e}")))
}
