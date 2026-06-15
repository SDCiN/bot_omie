# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Bot Omie is a Python RPA bot that automates extraction of financial reports from Omie ERP (Playwright + Firefox), processes the downloaded Excel files, persists data to PostgreSQL (schema `omie`), and archives the originals to a corporate network drive. It has a Tkinter GUI (`app/gui.py`) and a CLI entry point (`app/main.py`).

## Commands

```bash
# Install dependencies
pip install -r requirements.txt
playwright install firefox

# Run via GUI (recommended). Has a 10-second inactivity timer that
# auto-starts extraction if the user doesn't interact — for scheduled runs.
python app/gui.py

# Run extraction directly (requires auth.json)
python app/main.py

# Discover selectors when the Omie UI changes
# (opens visible Firefox + Playwright Inspector using the saved session)
python app/tools/get_selectors.py
```

There is no test suite, linter, or build system. Testing is manual — see `GUIA_TESTES.md` (partially outdated: it still references MySQL-era table names).

## Architecture

Modular ETL flow:

```
GUI (gui.py) → Orchestrator (main.py) → Auth (auth.py) + Playwright (Firefox, sync API)
                                        ↓
                          Excel processing (actions/process_excel/process_excel.py)
                                        ↓
                          DB insert (actions/upsert_data/upsert_*.py) → PostgreSQL schema `omie` (db/db.py)
                                        ↓  (archiving is called from INSIDE the upsert handlers, after commit)
                          File archive (utils.py) → network drive (hardcoded Z:\ path)
```

### Key Modules

- **`app/main.py`** — Orchestrator. Navigates Omie, extracts each report by `data-slug`, retries up to `MAX_RETRIES=3`, then processes and saves. Currently launches the browser with `headless=False` (visible) in `run_extraction()`.
- **`app/gui.py`** — Tkinter GUI: auth status, report checkboxes, log viewer (custom logging handler), progress bar. Extraction runs in a daemon thread that imports `run_extraction` from `main`.
- **`app/auth.py`** — Hybrid auth (see flow below). Exports `OMIE_URL`, `DOWNLOADS_DIR`, `AUTH_STATE_FILE`.
- **`app/actions/process_excel/process_excel.py`** — Reads Excel with dynamic header-row detection (Omie exports have metadata rows above the header), drops fully-empty rows/columns.
- **`app/actions/upsert_data/upsert_*.py`** — Three handlers: `upsert_contas_a_pagar.py`, `upsert_notas_faturadas.py`, `upsert_notas_debito.py`. Each creates its table dynamically from the DataFrame dtypes and bulk-inserts via `psycopg2.extras.execute_values()`.
- **`app/db/db.py`** — Lazy `ThreadedConnectionPool`; on first use creates schema `omie` and trigger function `set_updated_at()`.
- **`app/utils.py`** — `arquivar_arquivo()` moves the processed file to the network drive (renamed to the table name); `REDE_DESTINO` is **hardcoded** here, not an env var.

### Report Configuration — duplicated in two places

The report list exists in **both** `RELATORIOS` in `main.py` and `self.relatorios` in `gui.py`; they must be kept in sync. Fields:

- `nome_menu` — display name
- `data_slug` — Omie's `data-slug` attribute; the unique selector used to click the report
- `arquivo` — filename the download is saved as in `app/downloads/`
- `tabela` — matching key only: the GUI passes entries without `upsert_handler`, and `main.py` back-fills `upsert_handler`/`data_slug` by matching `tabela` against `RELATORIOS`
- `upsert_handler` — function reference (in `main.py` only)

The actual PostgreSQL table name is **not** `tabela` — it is the lowercase `TABLE_NAME` constant inside each upsert handler (`a_pagar`, `nf_faturadas`, `notas_debito`).

### Deliberate behavior that looks like a bug

- The fixed `time.sleep(300)` (5 min) after clicking "Executar" in `extrair_relatorio_omie()` is an explicit user requirement — do not replace it with selector polling.
- The "Executar" button selector is `get_by_role("button", name=" Executar")` — the leading space is real.
- `normalize_column_name()` only strips whitespace; DB column names intentionally keep the exact Excel header text (quoted identifiers), per user request. The snake_case docstring is stale.
- Despite the name, the `upsert_data` handlers do **append-only bulk INSERTs** — no `ON CONFLICT`, no truncate. Tables grow on every run. Table creation is `CREATE TABLE IF NOT EXISTS`, so changed Excel columns will not alter an existing table.

### Known dead code

`navegar_para_financas()` in `main.py` contains an unreachable duplicate of its own body after the final `return` (roughly lines 314–412). Edit only the live portion above it.

### Authentication Flow

1. First run: "Primeira Configuração" in GUI → visible browser → manual login + 2FA → user must press ENTER **in the terminal** (`input()` in `primeira_configuracao()`, even when launched from the GUI) → session saved to `auth.json` (project root, gitignored)
2. Subsequent runs: cookies restored from `auth.json` via `storage_state`
3. If redirected to login (or "Acessar" never appears): `realizar_login()` auto-fills `OMIE_USER`/`OMIE_PASSWORD` from `.env`, verifies login, and re-saves `auth.json`

### Navigation Gotchas

- Clicking "Acessar" on the Omie portal **opens a new tab** — `navegar_para_financas()` returns `(success, new_page)` and all subsequent work must use the returned page.
- The reports menu only appears after hovering the `paid` link; the bot re-hovers before each report.
- `fechar_popups()` dismisses Omie notification/announcement modals (buttons "Depois", "Fechar", "Agora não").

## Environment Variables

Loaded with python-dotenv; the file lives at `app/.env` (gitignored).

- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` — PostgreSQL connection
- `DB_POOL_MIN`, `DB_POOL_MAX` — connection pool size (defaults 2 / 5)
- `OMIE_URL` (default `https://app.omie.com.br`), `OMIE_USER`, `OMIE_PASSWORD` — Omie portal and login-fallback credentials
- `AUTO_CLOSE` — `"true"` closes the GUI 5s after extraction (and exits `main.py` when run directly)

The network archive destination is **not** an env var — it is the hardcoded `REDE_DESTINO` constant in `app/utils.py`.

## Language & Conventions

- All UI text, logs, and most comments/docs are in **Brazilian Portuguese**
- Commit messages follow conventional commits in Portuguese (e.g., `fix: ajustar seletor de acesso`)
- Playwright **synchronous API** only (no async); browser is **Firefox** (not Chromium)
- No type hints in most of the codebase
- Logging goes to both console and `omie_bot.log` (configured in `main.py` and `db/db.py`)

## Other Documentation

- `DOCUMENTACAO_PROJETO_bot_omie.md` — detailed technical documentation (PT-BR)
- `GUIA_TESTES.md` — manual test guide (table names are outdated from the MySQL era)
- `MIGRATION_GUIDE_PYTHON_BOTS.md`, `prd_migracao.md` — MySQL→PostgreSQL migration notes
