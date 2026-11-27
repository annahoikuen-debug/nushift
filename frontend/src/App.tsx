import { useEffect, useState } from "react";
import { loadDraft, saveDraft, type GridState } from "./db";
import { validateStaffRow } from "./validator";

const SYMBOL_COLORS: Record<string, { bg: string; fg: string }> = {
  早: { bg: "#dbeafe", fg: "#1e40af" },
  日: { bg: "#dcfce7", fg: "#166534" },
  遅: { bg: "#fef3c7", fg: "#92400e" },
  休: { bg: "#f1f5f9", fg: "#94a3b8" },
};

export default function App() {
  const [grid, setGrid] = useState<GridState | null>(null);
  const [history, setHistory] = useState<GridState[]>([]);
  const [pinMode, setPinMode] = useState(false);
  const [statusText, setStatusText] = useState("読み込み中...");

  useEffect(() => {
    (async () => {
      const saved = await loadDraft().catch(() => null);
      if (saved) {
        setGrid(saved);
        setStatusText(`ローカル復元済み (${new Date(saved.updatedAt).toLocaleTimeString()})`);
        return;
      }
      await fetchSample();
    })();
  }, []);

  async function fetchSample() {
    setStatusText("サンプル読込中...");
    const res = await fetch("/api/v1/sample-grid?days_count=14");
    const data = await res.json();
    const next: GridState = { ...data, updatedAt: new Date().toISOString() };
    setGrid(next);
    await saveDraft(next).catch(() => {});
    setStatusText("初期データ読込完了（自動保存ON）");
  }

  function commit(next: GridState) {
    if (grid) setHistory((prev) => [...prev.slice(-19), grid]);
    const stamped = { ...next, updatedAt: new Date().toISOString() };
    setGrid(stamped);
    saveDraft(stamped)
      .then(() => setStatusText(`自動保存済み (${new Date().toLocaleTimeString()})`))
      .catch(() => setStatusText("ローカル保存に失敗"));
  }

  function handleCellClick(staffIdx: number, day: string) {
    if (!grid) return;
    const staff = grid.staff[staffIdx];
    const isPinned = Boolean(staff.pinned[day]);
    if (pinMode) {
      const nextStaff = grid.staff.map((s, idx) =>
        idx === staffIdx ? { ...s, pinned: { ...s.pinned, [day]: !isPinned } } : s,
      );
      commit({ ...grid, staff: nextStaff });
      return;
    }
    if (isPinned) return;
    const symbols = grid.patterns.map((p) => p.symbol);
    const cur = staff.assignments[day] ?? "休";
    const nextSym = symbols[(symbols.indexOf(cur) + 1) % symbols.length];
    const nextStaff = grid.staff.map((s, idx) =>
      idx === staffIdx ? { ...s, assignments: { ...s.assignments, [day]: nextSym } } : s,
    );
    commit({ ...grid, staff: nextStaff });
  }

  function handleUndo() {
    if (!history.length) return;
    const prev = history[history.length - 1];
    setHistory((h) => h.slice(0, -1));
    setGrid(prev);
    saveDraft(prev).catch(() => {});
    setStatusText("1手戻しました");
  }

  if (!grid) {
    return <div style={{ padding: 24 }}>{statusText}</div>;
  }

  return (
    <div style={{ maxWidth: 1280, margin: "0 auto", padding: 20 }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12, marginBottom: 16 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 20 }}>⚡ NuShift 月間シフトエディタ</h1>
          <div style={{ fontSize: 12, color: "#475569", marginTop: 4 }}>{statusText}</div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button onClick={handleUndo} disabled={!history.length} style={btnStyle}>↩ 元に戻す</button>
          <button
            onClick={() => setPinMode((v) => !v)}
            style={{ ...btnStyle, background: pinMode ? "#4f46e5" : "#fff", color: pinMode ? "#fff" : "#0f172a" }}
          >
            {pinMode ? "🔒 ピン留めモード中" : "🔒 ピン留め切替"}
          </button>
          <button onClick={fetchSample} style={btnStyle}>↻ サンプルリセット</button>
        </div>
      </header>

      <div style={{ overflowX: "auto", background: "#fff", borderRadius: 12, border: "1px solid #e2e8f0" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
          <thead>
            <tr style={{ background: "#f1f5f9" }}>
              <th style={{ padding: 8, textAlign: "left", minWidth: 120, borderBottom: "1px solid #cbd5e1" }}>職員</th>
              {grid.days.map((d) => (
                <th key={d} style={{ padding: "6px 4px", borderBottom: "1px solid #cbd5e1", minWidth: 40 }}>{d.slice(5)}</th>
              ))}
              <th style={{ padding: 8, borderBottom: "1px solid #cbd5e1", minWidth: 90 }}>出勤/時間</th>
              <th style={{ padding: 8, borderBottom: "1px solid #cbd5e1", minWidth: 150 }}>法令チェック</th>
            </tr>
          </thead>
          <tbody>
            {grid.staff.map((s, sIdx) => {
              const summary = validateStaffRow(s, grid.days, grid.patterns);
              return (
                <tr key={s.staff_id} style={{ borderBottom: "1px solid #f1f5f9" }}>
                  <td style={{ padding: 8, fontWeight: 600 }}>
                    {s.name}
                    <div style={{ fontSize: 10, color: "#64748b", fontWeight: 400 }}>{s.role}</div>
                  </td>
                  {grid.days.map((d) => {
                    const sym = s.assignments[d] ?? "休";
                    const pinned = Boolean(s.pinned[d]);
                    const c = SYMBOL_COLORS[sym] ?? SYMBOL_COLORS["休"];
                    return (
                      <td key={d} style={{ padding: 2, textAlign: "center" }}>
                        <button
                          type="button"
                          onClick={() => handleCellClick(sIdx, d)}
                          title={pinned ? "固定済み" : "クリックで勤務記号を切替"}
                          style={{
                            width: "100%",
                            padding: "6px 2px",
                            borderRadius: 6,
                            border: pinned ? "2px solid #4f46e5" : "1px solid transparent",
                            background: c.bg,
                            color: c.fg,
                            fontWeight: 700,
                            cursor: "pointer",
                          }}
                        >
                          {pinned ? `🔒${sym}` : sym}
                        </button>
                      </td>
                    );
                  })}
                  <td style={{ padding: 8, textAlign: "center", fontVariantNumeric: "tabular-nums" }}>
                    {summary.workDays}日 / {summary.totalHours}h
                  </td>
                  <td style={{ padding: 8 }}>
                    {summary.issues.length === 0 ? (
                      <span style={{ color: "#15803d", fontWeight: 600 }}>✓ 適法</span>
                    ) : (
                      <span style={{ color: "#b91c1c", fontWeight: 600 }}>{summary.issues[0]}</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const btnStyle: React.CSSProperties = {
  padding: "8px 12px",
  borderRadius: 8,
  border: "1px solid #cbd5e1",
  background: "#fff",
  cursor: "pointer",
  fontSize: 12,
  fontWeight: 600,
};
