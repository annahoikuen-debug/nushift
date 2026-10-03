"""UI のステップ定義（番号・見出し・導線文言の単一情報源）。

このモジュールは「画面のステップ」を 1 か所で定義する。UI/UX 改善 案1 として
新設したもので、次の 3 つを 1 か所に集約目的是、モードごとの表示が
食い違わないようにすることである。

* 進捗チップに出す短い名前（``label``）
* タブの H3 見出し（``title``）
* 他の画面からこのステップを指すときの呼び方（``tab_ref``）

``streamlit_app.py`` から ``sidebar.py`` までのすべての表示がここを参照する。
**番号リテラルを他のファイルへ書かないこと。** ここを直せば全画面が直る。

色は・文言・モードの切替はすべてこのモジュールの外側の
:mod:`shiftai.ui.theme` が組み立てる（このモジュールは Streamlit を
import しないので、そのまま試験から呼べる）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Step:
    """1 つのステップ（タブ）の表示情報。"""

    index: int
    """上級者モード（5 タブ）での 0 始まり番号。"""

    key: str
    """モードに関係なく同じステップを指す機械可読な識別子。"""

    label: str
    """進捗チップに出す短い名前。先頭に丸数字を含む。"""

    title: str
    """タブの H3 見出しの文字列。先頭に算用数字と中黒を含む。"""

    tab_ref: str
    """他の画面からこのステップを参照するときの呼び方（例: タブ3）。"""


#: 上級者モード（5 タブ）のステップ。並び順がそのままタブ順になる。
EXPERT_STEPS: tuple[Step, ...] = (
    Step(0, "data", "① データ投入", "1. データ投入", "タブ1"),
    Step(1, "requirements", "② 必要人員", "2. 必要人員", "タブ2"),
    Step(2, "solve", "③ シフト作成", "3. シフト自動作成", "タブ3"),
    Step(3, "shift", "④ シフト表・微調整", "4. シフト表・微調整", "タブ4"),
    Step(4, "export", "⑤ 出力", "5. 出力", "タブ5"),
)

#: シンプルモード（3 タブ）のステップ。
#: ``key`` と ``title`` は :data:`EXPERT_STEPS` と一致させる。
#: ``label`` と ``tab_ref`` は 3 本構成の呼び方に変える。
SIMPLE_STEPS: tuple[Step, ...] = (
    Step(0, "data", "① データ", "1. データ投入", "タブ1"),
    Step(1, "solve", "② シフト作成", "3. シフト自動作成", "タブ2"),
    Step(2, "export", "③ 出力", "5. 出力", "タブ3"),
)

#: 上級者モードの番号からシンプルモードの番号への書像。
#: キーは 0 から 4 のすべてを含む（欠けるとモードの切替で壊れる）。
#: 値.simple モードの並びの範囲（0 から 2）に入る。
SIMPLE_INDEX: dict[int, int] = {0: 0, 1: 1, 2: 1, 3: 1, 4: 2}


def steps_for_mode(simple: bool) -> tuple[Step, ...]:
    """simple が真なら :data:`SIMPLE_STEPS`、偽なら :data:`EXPERT_STEPS` を返す。"""
    return SIMPLE_STEPS if simple else EXPERT_STEPS


def step_by_key(key: str) -> Step:
    """key から上級者モード基準の Step を返す。見つからない場合は ValueError。

    例外にするのは、参照側が(mode, index) の取り違えを黙って通さないため。
    """
    for step in EXPERT_STEPS:
        if step.key == key:
            return step
    raise ValueError(f"未知のステップ key です: {key}")
