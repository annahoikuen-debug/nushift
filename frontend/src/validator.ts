import type { ShiftPatternDef, StaffRow } from "./db";

export interface StaffSummary {
  workDays: number;
  totalHours: number;
  maxConsecutive: number;
  issues: string[];
}

export function validateStaffRow(
  staff: StaffRow,
  days: string[],
  patterns: ShiftPatternDef[],
): StaffSummary {
  const patternMap = new Map(patterns.map((p) => [p.symbol, p]));
  let workDays = 0;
  let totalMinutes = 0;
  let curConsec = 0;
  let maxConsec = 0;
  const issues: string[] = [];

  for (const d of days) {
    const sym = staff.assignments[d] ?? "休";
    const pat = patternMap.get(sym);
    const workMin = pat?.work_minutes ?? 0;
    const breakMin = pat?.break_minutes ?? 0;

    if (workMin > 0) {
      workDays += 1;
      totalMinutes += workMin;
      curConsec += 1;
      if (curConsec > maxConsec) maxConsec = curConsec;

      const reqBreak = workMin > 480 ? 60 : workMin > 360 ? 45 : 0;
      if (breakMin < reqBreak) {
        issues.push(`${d}: 休憩不足(${breakMin}m < ${reqBreak}m)`);
      }

      const base = Math.round(staff.daily_hours * 60);
      const cap =
        !staff.overtime_allowed || !staff.has_36_agreement
          ? Math.min(base, 480)
          : Math.min(Math.round(base * 1.25), 600);
      if (workMin > cap) {
        issues.push(`${d}: 1日上限超過(${workMin}m > ${cap}m)`);
      }
    } else {
      curConsec = 0;
    }
  }

  if (staff.max_consecutive_days > 0 && maxConsec > staff.max_consecutive_days) {
    issues.push(`連続勤務${maxConsec}日 > 上限${staff.max_consecutive_days}日`);
  }

  return {
    workDays,
    totalHours: Math.round((totalMinutes / 60) * 10) / 10,
    maxConsecutive: maxConsec,
    issues,
  };
}
