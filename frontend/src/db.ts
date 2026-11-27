const DB_NAME = "nushift_editor_db";
const STORE_NAME = "drafts";
const DRAFT_KEY = "current_grid";

export interface ShiftPatternDef {
  symbol: string;
  label: string;
  work_minutes: number;
  break_minutes: number;
}

export interface StaffRow {
  staff_id: string;
  name: string;
  role: string;
  daily_hours: number;
  overtime_allowed: boolean;
  has_36_agreement: boolean;
  max_consecutive_days: number;
  assignments: Record<string, string>;
  pinned: Record<string, boolean>;
}

export interface GridState {
  days: string[];
  patterns: ShiftPatternDef[];
  staff: StaffRow[];
  updatedAt: string;
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains(STORE_NAME)) {
        req.result.createObjectStore(STORE_NAME);
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

export async function saveDraft(state: GridState): Promise<void> {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readwrite");
    tx.objectStore(STORE_NAME).put(state, DRAFT_KEY);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

export async function loadDraft(): Promise<GridState | null> {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readonly");
    const req = tx.objectStore(STORE_NAME).get(DRAFT_KEY);
    req.onsuccess = () => resolve((req.result as GridState) ?? null);
    req.onerror = () => reject(req.error);
  });
}
