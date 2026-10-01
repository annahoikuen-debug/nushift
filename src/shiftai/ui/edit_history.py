"""データ投入タブの編集履歴（Undo / Redo）。

**なぜ必要か**

``st.data_editor`` で表を直接編集すると、誤入力の巻き戻しは

* ブラウザの Ctrl+Z（Streamlit の rerun では復元されない）
* ファイルを開き直して再読込（編集内容が消える）

のどちらかでしかできない。主任保育士が「1 行だけ試した」あとに
「やっぱり元に戻したい」となるケースでは、毎回 CSV を出し直すことになる。

**設計**

* Streamlit に依存しない純ロジック（フレームを受け取り、更新済みの履歴を返す）。
* 履歴は**フレームのスナップショット**を保持する。差分（patch）を持たないので
  「どの表のどのセルを直したか」をあとから追跡できなくても、
  「戻す」こと自体は必ず成功する。
* 1 表 1 履歴（``children`` / ``staff`` / ``preferences`` を別々に管理）。
* 同じ内容の記録は取り込まない（rerun のたびに履歴が膨らむのを防ぐ）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import pandas as pd

__all__ = ["EditHistory", "HistoryEntry", "frame_fingerprint"]

DEFAULT_LIMIT = 25


def frame_fingerprint(frame: pd.DataFrame | None) -> str:
    """DataFrame の内容を指す短いハッシュを返す。

    ``pandas.util.hash_pandas_object`` は値のみを見るため、
    列名や列順が違うフレームが同じ hash にならないよう文字列化して混ぜる。
    """
    if frame is None:
        return "None"
    header = "|".join(map(str, frame.columns))
    try:
        digest = pd.util.hash_pandas_object(frame, index=True).values.tobytes()
    except TypeError:
        # 異種の値を混ぜた列でも落ちないようにする
        digest = frame.astype(str).to_csv(index=True).encode("utf-8")
    return hashlib.sha1(header.encode("utf-8") + b"|" + digest).hexdigest()


@dataclass(frozen=True)
class HistoryEntry:
    """履歴の 1 ステップ。"""

    kind: str
    frame: pd.DataFrame
    label: str

    def copy(self) -> HistoryEntry:
        """フレームをコピーした新しいエントリを返す。"""
        return HistoryEntry(self.kind, self.frame.copy(), self.label)


@dataclass
class EditHistory:
    """編集履歴（新しいものほど後ろに積む）。"""

    limit: int = DEFAULT_LIMIT
    _entries: list[HistoryEntry] = field(default_factory=list, repr=False)
    _cursor: int = field(default=-1, repr=False)

    # -- 状態参照 ---------------------------------------------------------

    @property
    def entries(self) -> tuple[HistoryEntry, ...]:
        return tuple(self._entries)

    @property
    def current(self) -> HistoryEntry | None:
        """いま表示しているはずの状態。"""
        if 0 <= self._cursor < len(self._entries):
            return self._entries[self._cursor]
        return None

    @property
    def can_undo(self) -> bool:
        return self._cursor > 0

    @property
    def can_redo(self) -> bool:
        return 0 <= self._cursor < len(self._entries) - 1

    @property
    def size(self) -> int:
        return len(self._entries)

    def undo_label(self) -> str:
        """「元に戻す」ボタンのラベル。戻れないときは ``None`` の表現。"""
        if not self.can_undo:
            return "元に戻す"
        return f"元に戻す（{self._entries[self._cursor - 1].label}）"

    def redo_label(self) -> str:
        if not self.can_redo:
            return "やり直す"
        return f"やり直す（{self._entries[self._cursor + 1].label}）"

    def trail(self) -> str:
        """履歴の俯瞰表示（UI のキャプション用）。"""
        if not self._entries:
            return "編集履歴はまだありません。"
        marks = []
        for i, entry in enumerate(self._entries):
            marks.append(f"**{entry.label}**" if i == self._cursor else entry.label)
        return " › ".join(marks)

    # -- 操作 -------------------------------------------------------------

    def record(self, kind: str, frame: pd.DataFrame, label: str) -> bool:
        """新しい状態を記録する。

        直前の状態と同じ内容なら何もしない。

        :returns: 実際に履歴へ追加したら True
        """
        if frame is None:
            return False
        current = self.current
        if current is not None and frame_fingerprint(current.frame) == frame_fingerprint(frame):
            return False
        del self._entries[self._cursor + 1 :]
        self._entries.append(HistoryEntry(kind, frame.copy(), label))
        self._cursor = len(self._entries) - 1
        self._trim()
        return True

    def undo(self) -> HistoryEntry | None:
        """1 つ前の状態に戻して、その内容を返す。"""
        if not self.can_undo:
            return None
        self._cursor -= 1
        return self._entries[self._cursor]

    def redo(self) -> HistoryEntry | None:
        """1 つ後ろに戻して、その内容を返す。"""
        if not self.can_redo:
            return None
        self._cursor += 1
        return self._entries[self._cursor]

    def clear(self) -> None:
        """履歴を空にする。"""
        self._entries.clear()
        self._cursor = -1

    def _trim(self) -> None:
        """履歴長を ``limit`` に収める（古いものから捨てる）。"""
        overflow = len(self._entries) - max(1, self.limit)
        if overflow <= 0:
            return
        del self._entries[:overflow]
        self._cursor -= overflow