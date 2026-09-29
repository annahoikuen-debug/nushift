# ---------------------------------------------------------------------------
# shiftai — 開発補助
#
# 【注意】このファイルのインデントは「スペース 4 つ」ではなく「タブ (TAB)」である。
#   スペースで書くと make が "missing separator" で落ちる。編集時は必ずタブを使うこと。
#
# 【Windows には make が入っていない】
#   Windows ネイティブ環境では make コマンドが無い。以下のどちらかで代用する。
#     - WSL / Git Bash で `wsl make help` または `make help`
#     - make を使わず、Makefile の各ターゲットに対応するコマンドを直接叩く
#         install      -> python -m venv .venv && .venv\Scripts\pip install -e .
#         test-fast    -> python -m pytest -m "not slow"
#         lint         -> python -m ruff check .
# ---------------------------------------------------------------------------

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Python インタプリタ。仮想環境を使うなら `make install` の後に
# `make test PY=.venv/bin/python` のように PY を上書きする。
PY    ?= python3
# 仮想環境の作成先（install / install-dev が作る）。
VENV  ?= .venv

# install / install-dev が使う pip（install で venv を作る関係で固定）。
VENV_PYTHON := $(VENV)/bin/python

# 走査で降りないディレクトリ。`make clean` の対象から外す。
PRUNE_NAMES := .git .venv venv env
PRUNE_ARGS  := $(foreach d,$(PRUNE_NAMES),-path ./$(d) -prune -o)

# `make clean` が消すもの。**リポジトリ内の相対パスのみ**であり、
# `.` や `~` や絶対パスは決して含めない（rm -rf の誤爆を防ぐ）。
# データの入った out/ sample/ template/ は clean では消さない（distclean 側の責務）。
# .streamlit/secrets.toml は GAS の SECRET を載せているため絶対に消さない。
CLEAN_DIRS  := .pytest_cache .ruff_cache .mypy_cache .benchmarks build dist htmlcov
CLEAN_FILES := .coverage coverage.xml
# __pycache__ と *.egg-info は任意階層にあるので find で集める。
# 遅延評価（=）にして、clean 以外のターゲットでは walk しない。
CLEAN_FOUND = $(shell find . $(PRUNE_ARGS) -type d \( -name '__pycache__' -o -name '*.egg-info' \) -print 2>/dev/null)
CLEAN_TARGETS := $(CLEAN_DIRS) $(CLEAN_FILES) $(CLEAN_FOUND)
# distclean で消すもの（shiftai solve / sample / template の生成先）。
DIRTY_DIRS := out sample template

# ---------------------------------------------------------------------------
# 環境構築
# ---------------------------------------------------------------------------
.PHONY: install
install: ## 仮想環境を作り、実行時依存を editable で入れる
	python3 -m venv $(VENV)
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(VENV_PYTHON) -m pip install -e .
	@echo
	@echo "完了。次のコマンドで起動できます:  make run PY=$(VENV_PYTHON)"

.PHONY: install-dev
install-dev: install ## install に加えて開発・テスト用依存（requirements-dev.txt）を入れる
	$(VENV_PYTHON) -m pip install -r requirements-dev.txt
	@echo
	@echo "完了。次のコマンドでテストできます:  make test-fast PY=$(VENV_PYTHON)"

# ---------------------------------------------------------------------------
# 起動
# ---------------------------------------------------------------------------
.PHONY: run
run: ## Streamlit アプリを起動する（streamlit run streamlit_app.py と同じ）
	$(PY) -m streamlit run streamlit_app.py

# ---------------------------------------------------------------------------
# CLI サブコマンド
# ---------------------------------------------------------------------------
.PHONY: presets
presets: ## 配置基準プリセット一覧を表示する
	$(PY) -m shiftai presets

.PHONY: sample
sample: ## サンプル CSV（園児・職員・希望休）を sample/ に出力する
	$(PY) -m shiftai sample --out sample

.PHONY: template
template: ## 空テンプレート CSV を template/ に出力する
	$(PY) -m shiftai template --out template

.PHONY: solve
solve: ## サンプルデータで 1 週間分をヘッドレス実行する（out/ に成果物）
	$(PY) -m shiftai solve --sample --out out --time-limit 60 --zip

# ---------------------------------------------------------------------------
# テスト
# ---------------------------------------------------------------------------
.PHONY: test
test: ## 全テストを走らせる（slow を含む / 数十秒〜数分かかる）
	$(PY) -m pytest

.PHONY: test-fast
test-fast: ## 速いテストだけ走らせる（-m "not slow" / 基準は約 412 passed・2 分前後）
	$(PY) -m pytest -m "not slow"

.PHONY: test-slow
test-slow: ## slow マーカーの統合テストだけ走らせる
	$(PY) -m pytest -m "slow"

.PHONY: test-parallel
test-parallel: ## 速いテストをコア数で並列実行する（pytest-xdist が必要）
	$(PY) -m pytest -m "not slow" -n auto

.PHONY: cov
cov: ## カバレッジ付きで全テストを走らせる
	$(PY) -m pytest --cov=shiftai --cov-report=term-missing

# ---------------------------------------------------------------------------
# lint / format
# ---------------------------------------------------------------------------
.PHONY: lint
lint: ## ruff で静的検査する
	$(PY) -m ruff check .

.PHONY: format
format: ## ruff で整形し、import 順など自動修正可能な指摘も直す
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

# ---------------------------------------------------------------------------
# 後片付け
# ---------------------------------------------------------------------------

# 削除前に必ずリポジトリのルートであることを確認する。
# 実行ディレクトリがずれていても、rm -rf がリポジトリ外へ逃げないための安全弁。
.PHONY: guard
guard:
	@test -f pyproject.toml || { echo "エラー: リポジトリのルートで実行してください（pyproject.toml がありません）" >&2; exit 1; }
	@test -f Makefile || { echo "エラー: リポジトリのルートで実行してください（Makefile がありません）" >&2; exit 1; }
	@case "$$PWD" in "/" | "$$HOME") echo "エラー: カレントディレクトリが安全ではありません ($$PWD)" >&2; exit 1;; esac

.PHONY: clean
clean: guard ## キャッシュ・ビルド成果物を削除する（先に削除対象を表示する / 出力物は残す）
	@echo "make clean — 以下の対象を削除します:"
	@for t in $(CLEAN_TARGETS); do echo "  - $$t"; done
	@for t in $(CLEAN_TARGETS); do if [ -e "$$t" ]; then rm -rf -- "$$t"; fi; done
	@echo "make clean — 完了"
	@echo "注: out/ sample/ template/ は残しています（消したい場合は make distclean）"

.PHONY: distclean
distclean: guard ## clean に加えて out/ sample/ template/ の生成物も削除する
	@echo "make distclean — 以下の対象を削除します:"
	@for t in $(CLEAN_TARGETS) $(DIRTY_DIRS); do echo "  - $$t"; done
	@for t in $(CLEAN_TARGETS) $(DIRTY_DIRS); do if [ -e "$$t" ]; then rm -rf -- "$$t"; fi; done
	@echo "make distclean — 完了"

# ---------------------------------------------------------------------------
# ヘルプ
# ---------------------------------------------------------------------------
.PHONY: help
help: ## この一覧を表示する（既定ターゲット）
	@echo "shiftai — 開発補助ターゲット"
	@echo ""
	@echo "使い方:  make <ターゲット> [PY=<python>]"
	@echo ""
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9_-]+:.*?## / {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""
	@echo "例:"
	@echo "  make install-dev              # venv を作って開発環境を整える"
	@echo "  make test-fast                # 速いテストだけ（基準 約412 passed / 2分）"
	@echo "  make lint                     # ruff check"
	@echo "  make run                      # Streamlit アプリを起動"
	@echo ""
	@echo "補足:"
	@echo "  * 仮想環境を作った後は PY=$(VENV_PYTHON) を付けるか、.venv を有効化してから使う。"
	@echo "  * Windows には make が入っていない。WSL / Git Bash を使うか、README の手順を直接叩くこと。"
