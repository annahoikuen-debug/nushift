use serde::{Deserialize, Serialize};

pub const STATUTORY_DAILY_WORK_HOURS: f64 = 8.0;
pub const STATUTORY_MAX_DAILY_WORK_HOURS: f64 = 10.0;
pub const STATUTORY_OVERTIME_MULTIPLIER: f64 = 1.25;

/// 労働基準法第34条に基づく必要休憩分数（`statutory_rules.py` と同一式）。
pub fn required_break_minutes(work_minutes: i32) -> i32 {
    if work_minutes > 8 * 60 {
        60
    } else if work_minutes > 6 * 60 {
        45
    } else {
        0
    }
}

/// 1日の上限勤務分数を計算（`statutory_rules.py` と同一式）。
pub fn daily_cap_minutes(daily_hours: f64, overtime_allowed: bool, has_36_agreement: bool) -> i32 {
    let base = (daily_hours * 60.0).round() as i32;
    if !overtime_allowed || !has_36_agreement {
        return base.min((STATUTORY_DAILY_WORK_HOURS * 60.0) as i32);
    }
    let legal_cap = (STATUTORY_MAX_DAILY_WORK_HOURS * 60.0) as i32;
    let scaled = ((base as f64) * STATUTORY_OVERTIME_MULTIPLIER).round() as i32;
    scaled.min(legal_cap)
}

#[derive(Debug, Clone, Deserialize)]
pub struct DayValidationInput {
    pub staff_id: String,
    pub work_minutes: i32,
    pub break_minutes: i32,
    pub daily_hours: f64,
    pub overtime_allowed: bool,
    pub has_36_agreement: bool,
    pub consecutive_work_days: i32,
    pub max_consecutive_days: i32,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct DayValidationIssue {
    pub level: String,
    pub code: String,
    pub message: String,
    pub staff_id: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct DayValidationResult {
    pub ok: bool,
    pub issues: Vec<DayValidationIssue>,
}

/// 職員1日分の勤務データに対する高速法令・契約検証（Wasm / PyO3 共用）。
pub fn validate_staff_day(input: &DayValidationInput) -> DayValidationResult {
    let mut issues = Vec::new();

    let req_break = required_break_minutes(input.work_minutes);
    if input.work_minutes > 0 && input.break_minutes < req_break {
        issues.push(DayValidationIssue {
            level: "error".to_string(),
            code: "STATUTORY_BREAK_SHORTAGE".to_string(),
            message: format!(
                "実働{}分に対して休憩{}分が不足しています（法定必要休憩: {}分）。",
                input.work_minutes, input.break_minutes, req_break
            ),
            staff_id: input.staff_id.clone(),
        });
    }

    let cap = daily_cap_minutes(
        input.daily_hours,
        input.overtime_allowed,
        input.has_36_agreement,
    );
    if input.work_minutes > cap {
        issues.push(DayValidationIssue {
            level: "error".to_string(),
            code: "DAILY_CAP_EXCEEDED".to_string(),
            message: format!(
                "1日の勤務上限（{}分）を{}分超過しています。",
                cap,
                input.work_minutes - cap
            ),
            staff_id: input.staff_id.clone(),
        });
    }

    if input.max_consecutive_days > 0 && input.consecutive_work_days > input.max_consecutive_days {
        issues.push(DayValidationIssue {
            level: "warning".to_string(),
            code: "MAX_CONSECUTIVE_DAYS".to_string(),
            message: format!(
                "連続勤務{}日が上限（{}日）を超えています。",
                input.consecutive_work_days, input.max_consecutive_days
            ),
            staff_id: input.staff_id.clone(),
        });
    }

    let ok = !issues.iter().any(|i| i.level == "error");
    DayValidationResult { ok, issues }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_required_break_minutes() {
        assert_eq!(required_break_minutes(360), 0);
        assert_eq!(required_break_minutes(361), 45);
        assert_eq!(required_break_minutes(480), 45);
        assert_eq!(required_break_minutes(481), 60);
    }

    #[test]
    fn test_daily_cap_without_36_agreement() {
        assert_eq!(daily_cap_minutes(8.0, true, false), 480);
        assert_eq!(daily_cap_minutes(8.0, true, true), 600);
    }
}
