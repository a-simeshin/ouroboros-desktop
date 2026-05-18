# Plan: Полное удаление интеграции с Claude Code (SDK-слой) из ouroboros

## Task Description

Убрать из runtime-агента всю интеграцию с Claude Code, построенную поверх Python-пакета `claude-agent-sdk`. Это продолжение линии упрощения runtime после `d17e24a` (remove PyInstaller/launcher) и `775fa39` (remove Playwright). Сейчас интеграция состоит из двух runtime-инструментов и поддерживающего SDK-слоя:

1. **`claude_code_edit`** — делегирование правок кода в Claude Code SDK (edit-mode, `gateways/claude_code.py::run_edit`, обёртка в `tools/shell.py::_claude_code_edit`).
2. **`claude_advisory_review`** (`advisory_pre_review`) — пред-коммитное advisory-ревью через read-only SDK (`gateways/claude_code.py::run_readonly`, модуль `tools/claude_advisory_review.py`, 1957 LOC), завязанное на `AdvisoryReviewState` и пред-коммитный гейт в `commit_gate.py` (`_check_advisory_freshness`, `_invalidate_advisory`, фаза `advisory_gate`).
3. **Поддерживающий SDK-слой**: `ouroboros/claude_runtime.py` (валидация/repair SDK), SDK-хелперы в `ouroboros/platform_layer.py` (`ClaudeRuntimeState`, `_find_sdk_package_path`, `_find_bundled_cli`, `_probe_cli_version`, `_detect_legacy_user_site_sdk`, `resolve_claude_runtime`), runtime-зависимость `claude-agent-sdk` (`pyproject.toml`/`requirements.txt`), настройка `CLAUDE_CODE_MODEL` + её UI-контролы и серверные endpoints `/api/claude-code/status|install`.

**Решения заказчика (зафиксированы интервью):**
- Scope: **полностью весь SDK-слой** (оба инструмента + gateway + claude_runtime + platform_layer SDK-хелперы + зависимость + `CLAUDE_CODE_MODEL`).
- Advisory: **без замены**. Commit gate теряет advisory-слой полностью. **Блокирующая ревью-триада (`OUROBOROS_REVIEW_MODELS` / `OUROBOROS_REVIEW_ENFORCEMENT=advisory|blocking`) сохраняется без изменений** — это отдельная машинерия.
- Каталог `.claude/` на диске и его dev-тулинг (hooks/agents/commands) **не трогаем**. Runtime-данные advisory фактически лежат в плоскости данных `~/Ouroboros/data/state/advisory_review.json` (не в `.claude/`), поэтому удаление кода advisory не конфликтует с решением «не трогать `.claude/`».
- `CLAUDE_CODE_MODEL`: **удалить полностью** (config defaults, env-проброс, settings UI JS + DOM, серверные endpoints, install/repair runtime UI).

## Objective

После выполнения плана:
- В реестре инструментов агента отсутствуют `claude_code_edit` и `claude_advisory_review` (`advisory_pre_review`); схемы, отдаваемые модели, их не содержат.
- `repo_commit` работает **без advisory-гейта**: отсутствие фразы advisory-ревью не блокирует коммит; параметр `skip_advisory_pre_review` удалён; блокирующая ревью-триада продолжает работать без регрессий.
- Модули `ouroboros/gateways/claude_code.py`, `ouroboros/claude_runtime.py`, `ouroboros/tools/claude_advisory_review.py` удалены; SDK-хелперы в `platform_layer.py` сняты.
- Ни один runtime-путь кода не импортирует `claude_agent_sdk`; чистая установка без `[claude-sdk]` extra импортирует пакет `ouroboros` без ошибок.
- `claude-agent-sdk` отсутствует в `requirements.txt` и `pyproject.toml` (dependencies, `claude-sdk` extra, `all` extra).
- `CLAUDE_CODE_MODEL` отсутствует в `config.py`, env-пробросе, `web/modules/settings*.js`, документации; серверные endpoints `/api/claude-code/*` и блок Claude-runtime-статуса удалены.
- `review_state.py` хирургически очищен от advisory-машинерии (`advisory_runs`/`AdvisoryRunRecord`/freshness/`invalidate_advisory_after_mutation`), при этом **сохранена** блокирующая машинерия (`CommitAttemptRecord`, `ObligationItem`, `CommitReadinessDebtItem`, blocking_history, severity-тег `advisory_findings` на attempt-записях).
- Целевая спека `openspec/specs/ouroboros/ouroboros.md` и `docs/` приведены в соответствие.
- Все тесты зелёные; удалённые/рефакторенные тесты приведены в порядок.

## Problem Statement

Интеграция с Claude Code SDK — крупнейшая из трёх удаляемых интеграций (~5000+ LOC через ~30 файлов) и самая связная: advisory-ревью вплетено в пред-коммитный гейт, а `_invalidate_advisory` вызывается из ~14 точек во всех edit-инструментах (`git.py` ×5, `shell.py` ×2, `git_pr.py` ×6, `commit_gate.py`). Это создаёт три риска:

- **Терминологическая перегрузка слова «advisory».** Существуют ТРИ разных понятия с этим словом, и только одно удаляется:
  1. `claude_advisory_review` / `advisory_pre_review` / `AdvisoryReviewState.advisory_runs` / freshness — **УДАЛЯЕТСЯ** (это и есть Claude Code advisory).
  2. `CommitAttemptRecord.advisory_findings` — severity-тег («advisory» vs «critical») находок **блокирующей** ревью-триады, хранится на attempt-записях, используется `agent_task_pipeline.py` и `commit_gate.py` синтезом — **СОХРАНЯЕТСЯ**.
  3. `OUROBOROS_REVIEW_ENFORCEMENT=advisory|blocking` и glossary «Ревью-триада … режим advisory|blocking» — режим применения **блокирующей триады** (рекомендовать vs блокировать), не Claude SDK — **СОХРАНЯЕТСЯ**.
  Ошибочное удаление (2) или (3) сломает блокирующую триаду — главный риск плана.
- **`review_state.py` (1965 LOC) — общий модуль.** Advisory и блокирующая машинерия живут в одном `AdvisoryReviewState`. Требуется хирургический тримминг, не удаление файла.
- **`_invalidate_advisory` рассыпан по edit-инструментам.** Удаление функции требует синхронной зачистки всех call-sites без поломки потока коммита.

## Solution Approach

Применяется проверенный на `remove-playwright`/`remove-pyinstaller` подход **extract-before-delete + поэтапные верификационные шлюзы**, с дополнительной первой фазой **«map & isolate»** для разделения advisory ↔ blocking в `review_state.py`/`commit_gate.py`/`agent_task_pipeline.py`.

Ключевые архитектурные решения:

1. **`review_state.py` — хирургический тримминг.** Удаляются строго advisory-only символы: `AdvisoryRunRecord`, поле `advisory_runs`, свойства/методы `runs`/`latest`/`filter_advisory_runs`/`is_fresh`/`find_by_hash`/`add_run`/`mark_stale`/`mark_all_stale*`/`mark_repo_stale`, `invalidate_advisory_after_mutation`, `advisory_stale` evidence-ветка. **КРИТИЧНО — KEEP:** `_STATE_RELPATH = "state/advisory_review.json"` и `_LOCK_RELPATH = "locks/advisory_review.lock"` НЕ удаляются — этот файл хранит НЕ только `advisory_runs`, но и блокирующий ledger (`attempts`, `blocking_history`, `open_obligations`, `commit_readiness_debts`); удаление constant сломает persistence блокирующей триады. Имя файла историческое («advisory_review.json»), но он общий. Сохраняются также: `CommitAttemptRecord` (включая поле `advisory_findings` как severity-тег), `ObligationItem`, `CommitReadinessDebtItem`, `add_blocking_attempt`/`_upsert_blocking_history`/обязательства/долги, `load_state`/`save`-инфраструктура. Удалить только advisory-поля схемы при сохранении файла/`load_state`.
2. **`commit_gate.py` — снятие advisory-гейта, сохранение blocking-гейта.** Удаляются `_invalidate_advisory`, `_check_advisory_freshness`, ветка `_attempt_phase` `no_advisory → advisory_gate`. Сохраняются `_record_commit_attempt`, blocking-синтез (`load_state as _ls_synth`), obligations/debts-рендеринг.
3. **`_invalidate_advisory` call-sites.** Все ~14 вызовов в `git.py`/`shell.py`/`git_pr.py`/`commit_gate.py` удаляются вместе со связанными «advisory stale» сообщениями. Поток коммита упрощается: после мутации worktree не требуется повторный advisory-прогон.
4. **`agent_task_pipeline.py`** — блок построения task-continuation guidance по `state.advisory_runs`/`advisory_status`/`last_stale_reason` (строки ~541–582) удаляется; рендер `item.advisory_findings` (severity-тег блокирующей триады, строки ~662–671) **сохраняется**.
5. **`claude_runtime.py` НЕ переселяется** (в отличие от прошлого extract в remove-pyinstaller, где его создавали): здесь он удаляется целиком — единственные потребители (`claude_code_edit`/advisory/server status block) удаляются вместе с ним. Верификация «нет других импортеров» — отдельный шаг.
6. **`platform_layer.py`** — режутся только Claude-SDK-символы (`ClaudeRuntimeState`, `_find_sdk_package_path`, `_find_bundled_cli`, `_probe_cli_version`, `_detect_legacy_user_site_sdk`, `resolve_claude_runtime`). Все runtime-критичные символы (file locks, kill_pid_tree, container detect, `_hidden_run`) — KEEP.
7. **server.py** — удаляются блок Claude-runtime статуса (`resolve_claude_runtime()`/`status_label()`/message_map), импорт `get_last_stderr`, HTTP-endpoints `/api/claude-code/status` и `/api/claude-code/install`.
8. **Поведение при запросе удалённого инструмента** — спец-deprecation не вводится: применяется штатная обработка неизвестного инструмента (как в remove-playwright). Fallback отсутствует.
9. **Каталог `.claude/` не трогаем** — ни файлы, ни пути в коде, кроме случаев, где код Python явно строит путь к advisory-файлу (он в data-плоскости, не в `.claude/`). Миграция данных не требуется (in-place файлы перестают записываться, существующие игнорируются).

## Relevant Files

Use these files to complete the task:

**Удаляются целиком:**
- `ouroboros/gateways/claude_code.py` — SDK transport adapter (463 LOC). Корень интеграции.
- `ouroboros/claude_runtime.py` — SDK validation/repair (106 LOC).
- `ouroboros/tools/claude_advisory_review.py` — advisory tool (1957 LOC).

**Хирургический тримминг (advisory-only вырезается, blocking сохраняется):**
- `ouroboros/review_state.py` (1965 LOC) — `AdvisoryRunRecord`/`advisory_runs`/freshness/`invalidate_advisory_after_mutation`; KEEP blocking ledger.
- `ouroboros/tools/commit_gate.py` (526 LOC) — `_invalidate_advisory`, `_check_advisory_freshness`, `advisory_gate` фаза; KEEP `_record_commit_attempt`/blocking-синтез.
- `ouroboros/review_evidence.py` — сериализация `AdvisoryRunRecord`/`advisory_runs` (строки ~34–161).
- `ouroboros/agent_task_pipeline.py` — advisory-guidance блок (~541–582); KEEP `advisory_findings`-рендер (~662–671).

**Точечная зачистка ссылок:**
- `ouroboros/tools/shell.py` — `_claude_code_edit`, ToolEntry `claude_code_edit`, `_invalidate_advisory` (строки ~352, ~524), импорт из commit_gate.
- `ouroboros/tools/git.py` — параметр `skip_advisory_pre_review` (×3 сигнатуры), `_check_advisory_freshness` call, `_invalidate_advisory` ×5, «advisory stale» сообщения, JSON-schema поле `skip_advisory_pre_review`.
- `ouroboros/tools/git_pr.py` — `_invalidate_advisory` ×6, импорт.
- `ouroboros/tools/registry.py` — `claude_code_edit` в `CORE_TOOL_NAMES`/PROTECTED/revert guard (~375, ~505, ~1263, ~1344–1346), `claude_advisory_review` в списке tool-модулей (~562), поле `_review_advisory` (~467 — проверить advisory-only).
- `ouroboros/tool_capabilities.py` — `claude_code_edit` (~36).
- `ouroboros/safety.py` — `claude_code_edit: POLICY_CHECK` (~165) + комментарии (~21, ~164, ~583–584, ~624).
- `ouroboros/platform_layer.py` — SDK-хелперы (~420–560).
- `ouroboros/tools/review_helpers.py` — весь блок «Advisory SDK diagnostic helpers» (~994–1024, включая `importlib.metadata.version("claude-agent-sdk")` ~1017 и `from ouroboros.platform_layer import resolve_claude_runtime` ~1023–1024) + форматирование `sdk_version` (~1256) + ссылка (~1203).
- `ouroboros/config.py` — `CLAUDE_CODE_MODEL` default (~54), env-проброс (~783).
- `ouroboros/server_runtime.py` — `"CLAUDE_CODE_MODEL"` в migration-списке `keys` (~92; функция вокруг строк 87–104, `_RETIRED_MODEL_DEFAULT_REPLACEMENTS` ~55 проверить на ссылку).
- `ouroboros/context_compaction.py` — `"claude_code_edit": "prompt"` (~115).
- `ouroboros/loop_tool_execution.py` — `claude_code_edit` min-timeout (~68); весь status-каскад `⚠️ CLAUDE_CODE_*` (~157–164: `CLAUDE_CODE_TIMEOUT→timeout`, `CLAUDE_CODE_INSTALL_ERROR→install_error`, `CLAUDE_CODE_UNAVAILABLE→unavailable`, `CLAUDE_CODE_→claude_code_error`).
- `server.py` — Claude-runtime status block (~340–385), `/api/claude-code/status`, `/api/claude-code/install`, импорт `get_last_stderr` (~361).
- `web/modules/settings.js` — `s-claude-code-model`, `CLAUDE_CODE_MODEL` (244–246, 311–312, 342, 396, 521, 742, 751).
- `web/modules/settings_ui.js` — `settings-claude-code-panel`/`btn-claude-code-install`/`settings-claude-code-status`/`settings-claude-code-copy` (192–196).

**Зависимости/CI/доки/спека:**
- `pyproject.toml` — `claude-agent-sdk` в `dependencies` (~39), `claude-sdk` extra (~58), `all` extra (~65).
- `requirements.txt` — строка `claude-agent-sdk>=0.1.60`.
- `.github/workflows/ci.yml` — проверить отсутствие claude-специфичных шагов (grep дал 0; верифицировать).
- `docs/ARCHITECTURE.md` (~122 упоминания), `docs/DEVELOPMENT.md` (~14). `BIBLE.md` — case-insensitive grep по «claude code/claude_code/claude-agent» даёт **0** совпадений; файл вне скоупа правок (если builder найдёт упоминание — править только фактическое, не принципы P0–P12).
- `openspec/specs/ouroboros/ouroboros.md` — секции 421 (claude_code_edit revert guard), 491 (AdvisoryReviewState), 593–595 (Claude Code gateway), 634 (AdvisoryReviewState data model), 803 (tool policy), 820 (Claude runtime subprocess timeout). KEEP: 76 (glossary Ревью-триада), 689 (OUROBOROS_REVIEW_ENFORCEMENT).

**Тесты (удаляются):** `tests/test_claude_code_gateway.py`, `tests/test_claude_runtime.py`, `tests/test_advisory_observability.py`, `tests/test_advisory_preflight.py`, `tests/test_advisory_workflow.py`, `tests/test_advisory_workflow_ext.py`, `tests/test_max_tokens_constants.py` (SDK-coupled AST-проверка gateway-константы).

**Тесты (рефактор импортов/моков):** `tests/test_commit_gate.py`, `tests/test_reviewed_commit_workflow.py`, `tests/test_review_anti_thrashing.py`, `tests/test_review_calibration.py`, `tests/test_review_fidelity.py`, `tests/test_block1_review_pipeline.py`, `tests/test_scope_review.py`, `tests/test_agent_task_pipeline.py`, `tests/test_safety_policy.py`, `tests/test_tool_policy.py`, `tests/test_onboarding_wizard.py`, `tests/test_smoke.py`, `tests/test_startup_hygiene.py`, `tests/test_pr_tools.py`, `tests/test_context.py`, `tests/test_budget_tracking.py`, `tests/test_skill_exec.py`, `tests/test_runtime_mode_gating.py`, `tests/test_repo_read_limits.py`, `tests/test_phase7_pipeline.py`.

### New Files
- `tests/` — расширение `tests/test_post_refactor_integration.py` (см. Test Infrastructure); новых production-файлов не создаётся.

## Implementation Phases

### Phase 1: Foundation — Map & Isolate
Картирование границы advisory ↔ blocking в `review_state.py`/`commit_gate.py`/`agent_task_pipeline.py`/`review_evidence.py`. Builder составляет точный inventory advisory-only символов и фиксирует «KEEP-list» (blocking). Гейт: `validator` подтверждает, что список не задевает блокирующую триаду.

### Phase 2: Core Implementation — Removal
1. Снятие advisory-инструмента и гейта (`claude_advisory_review.py` delete; `commit_gate.py` trim; `_invalidate_advisory` call-sites; `git.py`/`git_pr.py`/`shell.py` зачистка; `review_state.py`/`review_evidence.py`/`agent_task_pipeline.py` trim).
2. Снятие `claude_code_edit` + SDK-слоя (`shell.py` `_claude_code_edit`/ToolEntry; `gateways/claude_code.py` delete; `claude_runtime.py` delete; `platform_layer.py` trim; `registry.py`/`tool_capabilities.py`/`safety.py`/`context_compaction.py`/`loop_tool_execution.py`/`review_helpers.py` зачистка).
3. Снятие config/UI/server (`config.py` `CLAUDE_CODE_MODEL` + env; `server.py` status block + endpoints; `web/modules/settings*.js`).

### Phase 3: Integration & Polish
Зависимости (`pyproject.toml`/`requirements.txt`/CI), документация (`ARCHITECTURE.md`/`DEVELOPMENT.md`/`BIBLE.md`), спека (`ouroboros.md`), per-layer тесты, финальная валидация (импорт без SDK, ruff, полный pytest, `check_test_layers.py`).

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- You're responsible for deploying the right team members with the right context to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members to the building, validating, testing tasks.
  - You're a high-level director, not a builder. You validate work and keep the team on track.
  - Communication via Task* tools.
- Take note of the session id of each team member.

### Team Members

- Builder
  - Name: builder-core
  - Role: Картирование границы advisory↔blocking и все production-удаления (Phase 1 + Phase 2).
  - Agent Type: builder
  - Resume: true
- Builder
  - Name: builder-surface
  - Role: Config/UI/server-слой + зависимости/CI/доки/спека (Phase 2.3 + Phase 3 non-test).
  - Agent Type: builder
  - Resume: true
- Builder
  - Name: builder-tests-unit
  - Role: Unit-тесты — удаление SDK-coupled, рефактор импортов/моков, новые unit-кейсы.
  - Agent Type: builder
  - Resume: true
- Builder
  - Name: builder-tests-integration
  - Role: Integration happy-path слой (расширение `tests/test_post_refactor_integration.py`).
  - Agent Type: builder
  - Resume: true
- Validator
  - Name: validator-final
  - Role: Read-only верификация: KEEP-list блокирующей триады не задет, acceptance criteria, прогон всех runner-команд, `check_test_layers.py`.
  - Agent Type: validator
  - Resume: true

## Testing Strategy

Test pyramid ratio: **80% unit / 15% integration-API / 5% UI e2e**

### Unit Tests (80%)
- `tests/test_commit_gate.py` — рефактор: убрать advisory-freshness/`_invalidate_advisory` кейсы; добавить кейс «repo_commit без advisory-гейта проходит», «блокирующий гейт по-прежнему срабатывает».
- `tests/test_review_calibration.py` / `test_review_fidelity.py` / `test_review_anti_thrashing.py` / `test_block1_review_pipeline.py` / `test_scope_review.py` — рефактор: убрать advisory-моки, подтвердить, что блокирующая триада не регрессировала.
- `tests/test_agent_task_pipeline.py` — рефактор: убрать advisory-guidance ожидания; добавить кейс «`advisory_findings` severity-тег блокирующей триады по-прежнему рендерится».
- `tests/test_safety_policy.py` / `test_tool_policy.py` — рефактор: `claude_code_edit` отсутствует в политике; защищённая поверхность не сломана.
- `tests/test_onboarding_wizard.py` — рефактор: `OUROBOROS_REVIEW_ENFORCEMENT=advisory|blocking` валидация сохранена (это не Claude SDK).
- `tests/test_smoke.py` / `test_startup_hygiene.py` — рефактор: импорт `ouroboros` без `claude_agent_sdk` не падает.
- Новый unit-кейс: реестр инструментов не содержит `claude_code_edit`/`claude_advisory_review`/`advisory_pre_review`; `CLAUDE_CODE_MODEL` отсутствует в `config.SETTINGS_DEFAULTS` и env-пробросе.
- Удаление: `test_claude_code_gateway.py`, `test_claude_runtime.py`, `test_advisory_observability.py`, `test_advisory_preflight.py`, `test_advisory_workflow.py`, `test_advisory_workflow_ext.py`, `test_max_tokens_constants.py`.

### Integration / API Tests (15%)
Расширение `tests/test_post_refactor_integration.py` (in-process, без внешних сервисов, SDK отсутствует):
- `test_repo_commit_without_advisory_gate` — `repo_commit` на чистом временном репо проходит без требования advisory_pre_review (нет `skip_advisory_pre_review`).
- `test_blocking_review_triad_still_functions` — блокирующая ревью-триада (`OUROBOROS_REVIEW_ENFORCEMENT`) по-прежнему доступна и срабатывает.
- `test_tool_registry_excludes_claude_tools` — собранный набор инстру(полный и core_only) не содержит `claude_code_edit`/`claude_advisory_review`.
- `test_import_without_claude_agent_sdk` — импорт ключевых модулей при отсутствии `claude_agent_sdk` в `sys.modules` не падает; `gateways/claude_code.py`/`claude_runtime.py` отсутствуют.

### UI E2E Tests (5%)
**Skipped — нет критичного UI-флоу.** Изменение UI — удаление одного поля `CLAUDE_CODE_MODEL` и панели Claude-runtime из настроек. Существующие UI-smoke тесты (`test_ui_smoke_playwright.py`, `test_smoke_e2e_post_refactor.py`) не трогаются и не зависят от удаляемой интеграции. Отсутствие контрола проверяется на unit/integration уровне (статический grep web/modules + реестр настроек).

## Test Infrastructure (User-Declared)

### Unit Layer (Python)
- **Files glob:** `tests/test_*.py`
- **Infra signature (regex, optional for unit):** `n/a`
- **Happy-path scenarios (≥1 named):**
  - `tests/test_commit_gate.py::test_repo_commit_passes_without_advisory_gate`
  - `tests/test_agent_task_pipeline.py::test_blocking_advisory_findings_still_rendered`
  - `tests/test_tool_policy.py::test_claude_code_edit_absent_from_policy`
- **Runner command:** `uv run pytest -q tests/test_commit_gate.py tests/test_agent_task_pipeline.py tests/test_tool_policy.py tests/test_safety_policy.py tests/test_onboarding_wizard.py tests/test_smoke.py`
- **Realism rationale:** Проект чисто Python с файловым JSON-состоянием; unit-тесты прямым вызовом функций — нативный и единственный реалистичный unit-уровень этого репо (нет ORM/контейнеров для unit).

### Integration Layer (Python) — MANDATORY, never Skipped
- **Files glob:** `tests/test_post_refactor_integration.py`
- **Infra signature (regex, ≥1 match per file):** `def test_import_without_claude_agent_sdk|sys\.modules|tmp_path|repo_commit`
- **Happy-path scenarios (≥1 named):**
  - `tests/test_post_refactor_integration.py::test_repo_commit_without_advisory_gate`
  - `tests/test_post_refactor_integration.py::test_blocking_review_triad_still_functions`
  - `tests/test_post_refactor_integration.py::test_tool_registry_excludes_claude_tools`
  - `tests/test_post_refactor_integration.py::test_import_without_claude_agent_sdk`
- **Runner command:** `uv run pytest -q tests/test_post_refactor_integration.py`
- **Realism rationale:** У репо нет БД/брокеров — наивысший реалистичный integration-уровень это in-process прогон реального tool-pipeline на временном git-репо с физически отсутствующим `claude_agent_sdk`; это в точности повторяет паттерн прошлых удалений (remove-pyinstaller/remove-playwright) и проверяет именно сохранённое поведение (commit без advisory, живая блокирующая триада).

### E2E Layer (Python/JS) — optional; required only if frontend detected
- **Status:** `Skipped — no UI flow change in this change (single settings field + status panel removed; verified at unit/integration level via static grep of web/modules + settings registry)`

## Step by Step Tasks

- IMPORTANT: Execute every step in order, top to bottom. Each task maps directly to a `TaskCreate` call.
- Before you start, run `TaskCreate` to create the initial task list that all team members can see and execute.

### 1. Map & isolate advisory↔blocking boundary
- **Task ID**: map-isolate
- **Depends On**: none
- **Assigned To**: builder-core
- **Agent Type**: builder
- **Stack**: Python typing pytest python-patterns
- **Parallel**: false
- **Tests**: Нет кода — артефакт inventory; покрывается `validate-all` через KEEP-list проверку.
- Через Serena (`find_symbol`/`find_referencing_symbols`) построить точный inventory advisory-only символов в `review_state.py`, `commit_gate.py`, `review_evidence.py`, `agent_task_pipeline.py`.
- Зафиксировать KEEP-list блокирующей триады: `CommitAttemptRecord` (включая поле `advisory_findings` как severity-тег), `ObligationItem`, `CommitReadinessDebtItem`, `attempts`/`blocking_history`/`open_obligations`/`commit_readiness_debts` поля, **`_STATE_RELPATH`/`_LOCK_RELPATH` константы (общий файл блокирующего ledger)**, `OUROBOROS_REVIEW_ENFORCEMENT`, glossary «Ревью-триада».
- Записать inventory + KEEP-list как комментарий-чеклист в начало `specs/remove-claude-code-integration.md` (приложение) — не менять production-код в этой задаче.

### 2. Remove advisory tool, gate, and ledger machinery
- **Task ID**: remove-advisory
- **Depends On**: map-isolate
- **Assigned To**: builder-core
- **Agent Type**: builder
- **Stack**: Python typing python-patterns python-errors
- **Parallel**: false
- **Tests**: Unit: `test_commit_gate.py` (repo_commit без гейта), `test_agent_task_pipeline.py` (advisory_findings severity-тег сохранён). Integration: `test_repo_commit_without_advisory_gate`, `test_blocking_review_triad_still_functions`.
- Удалить `ouroboros/tools/claude_advisory_review.py` целиком.
- `commit_gate.py`: удалить `_invalidate_advisory` (включая его внутренний self-call `from ouroboros.review_state import invalidate_advisory_after_mutation` на ~272–273), `_check_advisory_freshness`, ветку `_attempt_phase` `no_advisory→advisory_gate`; сохранить `_record_commit_attempt`/blocking-синтез (`load_state as _ls_synth`).
- Зачистить все call-sites `_invalidate_advisory` в `git.py` (×5), `shell.py` (run_shell ~352), `git_pr.py` (×6); убрать связанные «advisory stale» сообщения.
- `git.py`: убрать параметр `skip_advisory_pre_review` (×3 сигнатуры + JSON-schema поле ~1696), вызов `_check_advisory_freshness`, ветку `_advisory_bypassed`.
- `review_state.py`: хирургически вырезать advisory-only (`AdvisoryRunRecord`, `advisory_runs`, `runs`/`latest`/`filter_advisory_runs`/`is_fresh`/`find_by_hash`/`add_run`/`mark_stale`/`mark_all_stale*`/`mark_repo_stale`, `invalidate_advisory_after_mutation`, `advisory_stale` evidence-ветку); **НЕ удалять `_STATE_RELPATH`/`_LOCK_RELPATH`** — общий файл блокирующего ledger (`attempts`/`blocking_history`/`open_obligations`/`commit_readiness_debts`); сохранить KEEP-list из `map-isolate` дословно.
- `review_evidence.py`: убрать сериализацию `AdvisoryRunRecord`/`advisory_runs`.
- `agent_task_pipeline.py`: убрать advisory-guidance блок (~541–582); сохранить `advisory_findings`-рендер (~662–671).
- Запустить `uv run python -c "import ouroboros.tools.commit_gate, ouroboros.review_state, ouroboros.tools.git, ouroboros.tools.git_pr, ouroboros.agent_task_pipeline"` — без ошибок.

### 3. Remove claude_code_edit tool and SDK support layer
- **Task ID**: remove-sdk-layer
- **Depends On**: remove-advisory
- **Assigned To**: builder-core
- **Agent Type**: builder
- **Stack**: Python typing python-patterns python-errors
- **Parallel**: false
- **Tests**: Unit: `test_tool_policy.py`/`test_safety_policy.py` (claude_code_edit отсутствует). Integration: `test_tool_registry_excludes_claude_tools`, `test_import_without_claude_agent_sdk`.
- `shell.py`: удалить `_claude_code_edit`, ToolEntry `claude_code_edit`, импорт `_invalidate_advisory`, advisory-инвалидацию (~524), импорт `resolve_claude_code_model`/`run_edit`.
- Удалить `ouroboros/gateways/claude_code.py` и `ouroboros/claude_runtime.py` целиком.
- `platform_layer.py`: вырезать `ClaudeRuntimeState`, `_find_sdk_package_path`, `_find_bundled_cli`, `_probe_cli_version`, `_detect_legacy_user_site_sdk`, `resolve_claude_runtime` (~420–560); KEEP все runtime-критичные символы.
- `registry.py`: убрать `claude_code_edit` из `CORE_TOOL_NAMES`/PROTECTED/revert-guard (~375/505/1263/1344–1346), `claude_advisory_review` из списка tool-модулей (~562), поле `_review_advisory` если advisory-only (иначе KEEP).
- `tool_capabilities.py`: убрать `claude_code_edit` (~36).
- `safety.py`: убрать `claude_code_edit: POLICY_CHECK` (~165) и привести комментарии (~21/164/583–584/624) — формулировки об «claude_code_edit revert guard» снять; защищённая поверхность под runtime-режимом не меняется.
- `context_compaction.py` (~115) — убрать `"claude_code_edit": "prompt"`.
- `loop_tool_execution.py`: убрать `claude_code_edit` min-timeout (~68) И весь status-каскад `⚠️ CLAUDE_CODE_*` целиком (~157–164: `CLAUDE_CODE_TIMEOUT→timeout`, `CLAUDE_CODE_INSTALL_ERROR→install_error`, `CLAUDE_CODE_UNAVAILABLE→unavailable`, `CLAUDE_CODE_→claude_code_error`) — не оставлять мёртвых веток.
- `review_helpers.py`: удалить весь блок «Advisory SDK diagnostic helpers» (~994–1024, включая `importlib.metadata.version("claude-agent-sdk")` и `from ouroboros.platform_layer import resolve_claude_runtime`), форматирование `sdk_version` (~1256) и ссылку (~1203); проверить, что блок не используется блокирующей триадой (он shared с `claude_advisory_review.py` — удаляемым).
- Запустить `uv run python -c "import ouroboros.tools.shell, ouroboros.tools.registry, ouroboros.safety, ouroboros.platform_layer, ouroboros.tool_capabilities, ouroboros.context_compaction, ouroboros.loop_tool_execution"` — без ошибок.

### 4. Remove CLAUDE_CODE_MODEL, server endpoints, and settings UI
- **Task ID**: remove-config-ui-server
- **Depends On**: remove-sdk-layer
- **Assigned To**: builder-surface
- **Agent Type**: builder
- **Stack**: Python typing pyproject vanilla JavaScript DOM ES modules settings
- **Parallel**: false
- **Tests**: Unit: новый кейс «CLAUDE_CODE_MODEL отсутствует в config.SETTINGS_DEFAULTS, env-пробросе и server_runtime migration keys». Integration: `test_tool_registry_excludes_claude_tools` (косвенно — settings registry).
- `config.py`: удалить `"CLAUDE_CODE_MODEL"` из `SETTINGS_DEFAULTS` (~54) и из списка env-проброса (~783).
- `server_runtime.py`: удалить `"CLAUDE_CODE_MODEL"` из migration-списка `keys` (~92, функция вокруг 87–104); проверить `_RETIRED_MODEL_DEFAULT_REPLACEMENTS` (~55) на ссылку и снять при наличии. Это закрывает acceptance #7 / validation-grep #4 (иначе план провалит собственный гейт).
- `server.py`: удалить блок Claude-runtime статуса (~340–385: `resolve_claude_runtime`/`status_label`/message_map), импорт `get_last_stderr` (~361), HTTP-endpoints `/api/claude-code/status` и `/api/claude-code/install` (и связанный install/repair handler).
- `web/modules/settings.js`: удалить `s-claude-code-model` apply/collect (~396/521), claude-code panel/button/status/fetch (~244–246/311–312/342/742/751).
- `web/modules/settings_ui.js`: удалить DOM `settings-claude-code-panel`/`btn-claude-code-install`/`settings-claude-code-status`/`settings-claude-code-copy` (~192–196).
- Прогнать существующий settings-тест/линт (`uv run pytest -q tests/test_settings_runtime_regressions.py tests/test_settings_hot_reload.py`) — зелёные.

### 5. Strip dependencies, CI, docs, and target spec
- **Task ID**: cleanup-deps-docs-spec
- **Depends On**: remove-config-ui-server
- **Assigned To**: builder-surface
- **Agent Type**: builder
- **Stack**: Python pyproject ruff
- **Parallel**: false
- **Tests**: Integration: `test_import_without_claude_agent_sdk` (зависимость снята). Покрывается `validate-all`.
- `pyproject.toml`: убрать `claude-agent-sdk>=0.1.60` из `dependencies` (~39); убрать/опустошить `claude-sdk` extra (~58); убрать из `all` extra (~65).
- `requirements.txt`: убрать строку `claude-agent-sdk>=0.1.60`.
- `.github/workflows/ci.yml`: верифицировать отсутствие claude-специфичных шагов (grep=0); если найдены — снять.
- `docs/ARCHITECTURE.md` (~122 упоминания), `docs/DEVELOPMENT.md` (~14): вычистить описания Claude Code gateway/claude_code_edit/advisory_pre_review/AdvisoryReviewState advisory-машинерии/`CLAUDE_CODE_MODEL`; сохранить описания блокирующей ревью-триады. `BIBLE.md` — 0 совпадений, только верифицировать, НЕ редактировать (особенно не трогать принципы P0–P12).
- `openspec/specs/ouroboros/ouroboros.md`: правка секций 421/491/593–595/634/803/820 (снять Claude Code gateway, claude_code_edit revert guard, AdvisoryReviewState advisory-runs, Claude runtime subprocess timeout); **KEEP** 76 (glossary Ревью-триада), 689 (OUROBOROS_REVIEW_ENFORCEMENT). Точная синхронизация спеки делается через `/openspec-propose` (см. отчёт), здесь — фактическая правка текста.

### 6. Unit tests
- **Task ID**: unit-tests
- **Depends On**: remove-advisory, remove-sdk-layer, remove-config-ui-server, cleanup-deps-docs-spec
- **Assigned To**: builder-tests-unit
- **Agent Type**: builder
- **Stack**: Python pytest pytest-mock unit test structure
- **Parallel**: true
- Удалить SDK-coupled тест-файлы: `test_claude_code_gateway.py`, `test_claude_runtime.py`, `test_advisory_observability.py`, `test_advisory_preflight.py`, `test_advisory_workflow.py`, `test_advisory_workflow_ext.py`, `test_max_tokens_constants.py`.
- Рефактор импортов/моков `claude_agent_sdk`/`gateways.claude_code`/advisory в: `test_commit_gate.py`, `test_reviewed_commit_workflow.py`, `test_review_anti_thrashing.py`, `test_review_calibration.py`, `test_review_fidelity.py`, `test_block1_review_pipeline.py`, `test_scope_review.py`, `test_agent_task_pipeline.py`, `test_safety_policy.py`, `test_tool_policy.py`, `test_onboarding_wizard.py`, `test_smoke.py`, `test_startup_hygiene.py`, `test_pr_tools.py`, `test_context.py`, `test_budget_tracking.py`, `test_skill_exec.py`, `test_runtime_mode_gating.py`, `test_repo_read_limits.py`, `test_phase7_pipeline.py`.
- Добавить unit-кейсы из `### Unit Layer`: `test_repo_commit_passes_without_advisory_gate`, `test_blocking_advisory_findings_still_rendered`, `test_claude_code_edit_absent_from_policy`, «CLAUDE_CODE_MODEL отсутствует».
- Следовать существующим тест-паттернам репо (reference: `tests/test_commit_gate.py`, `tests/test_tool_policy.py`).
- Runner: `uv run pytest -q tests/test_commit_gate.py tests/test_agent_task_pipeline.py tests/test_tool_policy.py tests/test_safety_policy.py tests/test_onboarding_wizard.py tests/test_smoke.py` — зелёные.

### 7. Integration tests — MANDATORY
- **Task ID**: integration-tests
- **Depends On**: remove-advisory, remove-sdk-layer, remove-config-ui-server, cleanup-deps-docs-spec
- **Assigned To**: builder-tests-integration
- **Agent Type**: builder
- **Stack**: Python pytest integration test structure
- **Parallel**: false
- Расширить `tests/test_post_refactor_integration.py` сценариями из `### Integration Layer (Python)`:
  - `test_repo_commit_without_advisory_gate` — in-process `repo_commit` на `tmp_path` git-репо проходит без advisory.
  - `test_blocking_review_triad_still_functions` — блокирующая триада (`OUROBOROS_REVIEW_ENFORCEMENT`) доступна/срабатывает.
  - `test_tool_registry_excludes_claude_tools` — полный и core_only наборы без `claude_code_edit`/`claude_advisory_review`.
  - `test_import_without_claude_agent_sdk` — при отсутствии `claude_agent_sdk` импорт ключевых модулей не падает; `gateways/claude_code.py`/`claude_runtime.py` отсутствуют.
- Снять/инвертировать унаследованный из remove-pyinstaller сценарий, утверждавший «claude_runtime importable» (теперь модуль отсутствует).
- Имена тест-методов = именам сценариев из User-Declared блока (для fuzzy-grep в `check_test_layers.py`).
- Runner: `uv run pytest -q tests/test_post_refactor_integration.py` — зелёные, ≥4 теста.

### 8. Final validation
- **Task ID**: validate-all
- **Depends On**: map-isolate, remove-advisory, remove-sdk-layer, remove-config-ui-server, cleanup-deps-docs-spec, unit-tests, integration-tests
- **Assigned To**: validator-final
- **Agent Type**: validator
- **Stack**: Python pytest ruff python-patterns
- **Parallel**: false
- Подтвердить KEEP-list из `map-isolate` не задет: блокирующая триада (`CommitAttemptRecord.advisory_findings` severity-тег, `OUROBOROS_REVIEW_ENFORCEMENT`, obligations/debts/blocking_history) сохранена и работает.
- Выполнить дословно Runner-команды из `## Test Infrastructure (User-Declared)` для каждого non-Skipped слоя; распарсить вывод pytest на «N passed» (N ≥ числа заявленных сценариев слоя).
- Запустить `check_test_layers.py` post-build hook и подтвердить прохождение integration-слоя.
- Прогнать полный регресс и lint (см. Validation Commands).
- Проверить acceptance criteria по списку.

## Acceptance Criteria

1. Агент запрашивает `claude_code_edit` или `claude_advisory_review`/`advisory_pre_review` → инструмент отсутствует в реестре и в схемах модели (core_only и полный набор); штатная обработка неизвестного инструмента; fallback отсутствует.
2. `repo_commit` на чистом репо проходит без требования advisory-ревью; параметр `skip_advisory_pre_review` отсутствует в сигнатуре и JSON-schema; ветка `advisory_gate` не достижима.
3. Блокирующая ревью-триада не регрессировала: `OUROBOROS_REVIEW_ENFORCEMENT=advisory|blocking` работает, `CommitAttemptRecord.advisory_findings` (severity-тег) рендерится в `agent_task_pipeline`, obligations/debts/blocking_history сохранены.
4. Чистая установка без `[claude-sdk]` extra: `uv run python -c "import ouroboros, ouroboros.tools.shell, ouroboros.tools.registry, ouroboros.tools.commit_gate, ouroboros.review_state, server"` без ошибок и без `claude_agent_sdk` в `sys.modules`.
5. `ouroboros/gateways/claude_code.py`, `ouroboros/claude_runtime.py`, `ouroboros/tools/claude_advisory_review.py` отсутствуют; grep `claude_agent_sdk` по `ouroboros/`/`server.py` (без тестов) даёт 0 в runtime-путях.
6. `claude-agent-sdk` отсутствует в `requirements.txt` и в `pyproject.toml` (dependencies, `claude-sdk` extra, `all` extra).
7. `CLAUDE_CODE_MODEL` отсутствует в `config.SETTINGS_DEFAULTS`, env-пробросе, `server_runtime.py` migration-`keys`, `web/modules/settings.js`/`settings_ui.js`; endpoints `/api/claude-code/status|install` и блок Claude-runtime статуса в `server.py` удалены.
8. `docs/ARCHITECTURE.md`/`DEVELOPMENT.md`/`BIBLE.md` и `openspec/specs/ouroboros/ouroboros.md` не описывают Claude Code gateway/claude_code_edit/advisory_pre_review/AdvisoryReviewState-advisory/`CLAUDE_CODE_MODEL`; описания блокирующей триады (glossary 76, OUROBOROS_REVIEW_ENFORCEMENT 689) сохранены.
9. `ruff check .` чист; полный `uv run pytest -q` зелёный; integration-слой через `check_test_layers.py` проходит (≥4 сценария).
10. `.claude/` каталог на диске не изменён (git status не показывает изменений в `.claude/`).

## Validation Commands

Execute these commands to validate the task is complete:

- `uv run python -c "import ouroboros, ouroboros.tools.shell, ouroboros.tools.registry, ouroboros.tools.commit_gate, ouroboros.review_state, ouroboros.agent_task_pipeline, server"` — импорт без `claude-agent-sdk` не падает.
- `! python -c "import sys; sys.modules['claude_agent_sdk']=None; import ouroboros.tools.shell, ouroboros.tools.commit_gate"` ← должно пройти (SDK физически не нужен). (В отчёте: запускать как обычную команду без `!`.)
- `test ! -f ouroboros/gateways/claude_code.py && test ! -f ouroboros/claude_runtime.py && test ! -f ouroboros/tools/claude_advisory_review.py && echo DELETED_OK` — удалённые модули отсутствуют.
- `! grep -rIn --include='*.py' 'claude_agent_sdk\|gateways.claude_code\|claude_advisory_review\|_invalidate_advisory\|skip_advisory_pre_review\|CLAUDE_CODE_MODEL' ouroboros/ server.py` — 0 совпадений в runtime-коде.
- `! grep -n 'claude-agent-sdk' requirements.txt pyproject.toml` — зависимость снята.
- `! grep -rn 's-claude-code-model\|settings-claude-code-panel\|api/claude-code' web/modules/` — UI/endpoint-ссылки сняты.
- `uv run ruff check .` — линт чист.
- `uv run pytest -q tests/test_commit_gate.py tests/test_agent_task_pipeline.py tests/test_tool_policy.py tests/test_safety_policy.py tests/test_onboarding_wizard.py tests/test_smoke.py` — unit-слой зелёный.
- `uv run pytest -q tests/test_post_refactor_integration.py` — integration-слой зелёный (≥4 теста).
- `uv run pytest -q` — полный регресс зелёный.
- `uv run --script .claude/hooks/validators/check_test_layers.py --plan specs/remove-claude-code-integration.md` (или штатный вызов post-build hook) — integration-слой подтверждён.
- `git status --porcelain .claude/` — пусто (каталог `.claude/` не изменён).

## Notes

- Самый высокий риск — терминологическая перегрузка «advisory» (см. Problem Statement): удалять строго Claude-SDK-advisory (`advisory_pre_review`/`AdvisoryRunRecord`/`advisory_runs`/freshness), НЕ трогать `CommitAttemptRecord.advisory_findings` (severity-тег блокирующей триады) и `OUROBOROS_REVIEW_ENFORCEMENT`. Фаза `map-isolate` + KEEP-list + `validator-final` — обязательные шлюзы против этой ошибки.
- `review_state.py` — общий модуль; только хирургический тримминг, файл не удаляется.
- Версионирование/CHANGELOG: bump `VERSION` (текущая `5.8.1`) и запись в `README.md`/CHANGELOG-секцию делается в `cleanup-deps-docs-spec` по конвенции репо (ср. предыдущие removal-коммиты).
- Новых библиотек не требуется; зависимости только удаляются.
- Точную синхронизацию `openspec/specs/ouroboros/ouroboros.md` рекомендуется провести через `/openspec-propose` (артефакты OpenSpec change) после ревью этого плана.

## Appendix A: Advisory↔Blocking Inventory & KEEP-list (map-isolate output)

**Produced by `map-isolate` (builder-core) via Serena.** Line numbers VERIFIED against the working tree on branch `remove-claude-code-integration` (commit `775fa39`). The `remove-advisory` builder may execute the removal mechanically from this appendix. Append-only — no production code changed in this task.

### A.0 Three "advisory" concepts — disambiguation matrix

| Concept | Verbatim symbols | Files | Verdict |
|---|---|---|---|
| (1) Claude SDK advisory pre-review | `claude_advisory_review` / `advisory_pre_review` / `AdvisoryRunRecord` / `advisory_runs` / freshness / `_invalidate_advisory` / `skip_advisory_pre_review` / `advisory_gate` / `last_stale_*` | per A.1/A.3 | **REMOVE** |
| (2) Blocking-triad finding severity tag | `CommitAttemptRecord.advisory_findings` (field), `_normalize_advisory_entries`, `agent_task_pipeline` render, `review_evidence._attempt_to_dict` / `_continuation_to_dict` `advisory_findings` | `review_state.py:205`, `commit_gate.py:44-51,185-191`, `agent_task_pipeline.py:662-672`, `review_evidence.py:145,254` | **KEEP** |
| (3) Blocking-triad enforcement mode | `OUROBOROS_REVIEW_ENFORCEMENT="advisory"\|"blocking"`, glossary "Ревью-триада" | `config.py:69,131,363-364,788,821-822`, `onboarding_wizard.py:223-224,280,387`, `ouroboros.md:76,689` | **KEEP** |

Note: the literal string `"advisory"` is the *default value* of concept (3) and the *default `phase=`* of `AdvisoryRunRecord` (concept 1). They are unrelated. Removing (1) must not touch the `config.py`/`onboarding_wizard.py`/`ouroboros.md` occurrences.

### A.1 REMOVE inventory — advisory-only symbols (verified line ranges)

`F` = file. Kind: C=class, F=func, M=method, P=property, A=attr/field, B=branch, K=constant.

| # | File | Symbol | Lines | Kind | Referencing symbols (non-test) — exact |
|---|---|---|---|---|---|
| R1 | `ouroboros/review_state.py` | `AdvisoryRunRecord` | 162–186 | C | `review_state._record_from_dict` (1210–1235), `_save_state_unlocked` (1396–1397), `_load_state_unlocked` (1284–1286); `commit_gate._check_advisory_freshness` (349,403); `agent_task_pipeline` via `state.advisory_runs`; `claude_advisory_review.py` (deleted whole) |
| R2 | `ouroboros/review_state.py` | `AdvisoryReviewState.advisory_runs` (field) | 227 | A | every R3–R15 method; `_load_state_unlocked` (1284,1316), `_save_state_unlocked` (1396–1397), `review_evidence.py:34,38`, `agent_task_pipeline.py:541,551`, `format_status_section:1619`, `_build_commit_readiness_debt_observations:734` |
| R3 | `ouroboros/review_state.py` | `AdvisoryReviewState.runs` (property getter+setter) | 240–247 | P | `context.py:810` (`advisory_state.runs` gate — **see A.4 cross-file**), `_save_state_unlocked` indirectly; test `runs.append`/`runs=` |
| R4 | `ouroboros/review_state.py` | `AdvisoryReviewState.latest` | 249–250 | M | `claude_advisory_review.py` (deleted) — no non-test runtime caller outside deleted module |
| R5 | `ouroboros/review_state.py` | `AdvisoryReviewState.filter_advisory_runs` | 288–313 | M | `review_evidence.py:38`, `commit_gate.py:397,452`, `format_status_section:1619`, `_build_commit_readiness_debt_observations:734`, `claude_advisory_review.py` (deleted) |
| R6 | `ouroboros/review_state.py` | `AdvisoryReviewState.find_by_hash` | 347–362 | M | `review_evidence.py:51`, `commit_gate.py:451`, `is_fresh` (365), `claude_advisory_review.py` (deleted) |
| R7 | `ouroboros/review_state.py` | `AdvisoryReviewState.is_fresh` | 364–366 | M | `commit_gate.py:380,429`, `claude_advisory_review.py` (deleted) |
| R8 | `ouroboros/review_state.py` | `AdvisoryReviewState.add_run` | 368–381 | M | `commit_gate.py:403`, `claude_advisory_review.py` (deleted) |
| R9 | `ouroboros/review_state.py` | `AdvisoryReviewState.mark_stale` | 383–387 | M | `git.py:234` (`_mark_failed_bypass_advisory_stale`) |
| R10 | `ouroboros/review_state.py` | `AdvisoryReviewState.mark_all_stale_except` | 389–394 | M | `add_run` (373, self) |
| R11 | `ouroboros/review_state.py` | `AdvisoryReviewState.mark_all_stale` | 396–397 | M | `_mark_advisory_stale_locked` (1576), `invalidate_advisory_after_mutation._mutate` (1599) |
| R12 | `ouroboros/review_state.py` | `AdvisoryReviewState.mark_repo_stale` | 399–431 | M | `mark_all_stale` (397, self), `invalidate_advisory_after_mutation._mutate` (1601) |
| R13 | `ouroboros/review_state.py` | `AdvisoryReviewState.advisory_runs[-_MAX_RUN_HISTORY:]` cap | 375–376 | B | inside `add_run` (R8) — removed with R8 |
| R14 | `ouroboros/review_state.py` | fields `last_stale_from_edit_ts` / `last_stale_reason` / `last_stale_repo_key` | 236–238 | A | `add_run` (378–380), `mark_repo_stale` (427–429), `_build_commit_readiness_debt_observations` (682–694 = R20 branch), `on_successful_commit` (1078–1080, 1108–1111 — clears; trim lines with fields, **keep method**), `format_status_section` (1668–1672 = R19 branch), `_load_state_unlocked` (1372–1374), `_save_state_unlocked` (1405–1407), `git.py:235–237`, `review_evidence.py:66,81–82`, `agent_task_pipeline.py:578–582` |
| R15 | `ouroboros/review_state.py` | `compute_snapshot_hash` (+ inner `_record_digest`) | 1516–1556 | F | `agent_task_pipeline.py:537`, `review_evidence.py:31`, `git.py:226` (`_mark_failed_bypass_advisory_stale`), `commit_gate.py:362`, `claude_advisory_review.py` (deleted). **All callers are advisory-only → safe to delete with them.** |
| R16 | `ouroboros/review_state.py` | `mark_advisory_stale_after_edit` | 1561–1568 | F | tests only (`test_advisory_*`) — no non-test runtime caller |
| R17 | `ouroboros/review_state.py` | `_mark_advisory_stale_locked` | 1571–1575 | F | `mark_advisory_stale_after_edit` (1564, self) |
| R18 | `ouroboros/review_state.py` | `invalidate_advisory_after_mutation` (+ inner `_mutate`) | 1578–1609 | F | `commit_gate._invalidate_advisory` (272–273) — sole non-test caller |
| R19 | `ouroboros/review_state.py` | `format_status_section` advisory portion: advisory_runs loop + stale-from-edit block | 1617–1652 (advisory_runs render), 1668–1673 (stale-from-edit block) | B | function is **MIXED** — see A.3 surgical note; KEEP attempts/obligations/debts render |
| R20 | `ouroboros/review_state.py` | `_build_commit_readiness_debt_observations` `advisory_stale` evidence branch | 682–694 | B | inside KEEP method (621–762); delete ONLY this `if self.last_stale_from_edit_ts` block |
| R21 | `ouroboros/review_state.py` | `_record_from_dict` | 1210–1235 | F | `_load_state_unlocked:1286` — delete with advisory_runs deserialization |
| R22 | `ouroboros/review_state.py` | `_resolve_mutation_repo_keys` (+ inner `_record`) | 1915–1936 | F | `invalidate_advisory_after_mutation:1588` (R18) — sole caller |
| R23 | `ouroboros/review_state.py` | `_build_invalidation_reason` | 1939–1959 | F | `invalidate_advisory_after_mutation:1591` (R18) — sole caller |
| R24 | `ouroboros/review_state.py` | constant `_MAX_RUN_HISTORY = 10` | 40 | K | `add_run` (375–376, R13) only |
| R25 | `ouroboros/review_state.py` | constant `_DEFAULT_ADVISORY_TOOL_NAME = "advisory_pre_review"` | 45 | K | `AdvisoryRunRecord` default (177), `_record_from_dict` (1227) only |
| R26 | `ouroboros/review_state.py` | module docstring advisory lines | 6–8, 11 | doc | trim "advisory_runs" / "runs alias" mentions; keep "attempts" line |
| R27 | `ouroboros/tools/commit_gate.py` | `_invalidate_advisory` (+ self-import of `invalidate_advisory_after_mutation`) | 264–280 | F | `git.py` ×5 (986,1001,1013,1093,1178), `shell.py` ×2 (352,524), `git_pr.py` ×6 (370,575,598,647,750,959) — see A.2 |
| R28 | `ouroboros/tools/commit_gate.py` | `_check_advisory_freshness` (+ inner `_render_obligations`,`_render_debts`,`_mutate`) | 346–525 | F | `git.py:338` (`_run_reviewed_stage_cycle`, `advisory_err =`) — sole non-test caller |
| R29 | `ouroboros/tools/commit_gate.py` | `_attempt_phase` `no_advisory→advisory_gate` branch | 31–33 (the `if status=="blocked": if block_reason=="no_advisory": return "advisory_gate"`) | B | inside KEEP `_attempt_phase` (28–41); delete only the `no_advisory` arm |
| R30 | `ouroboros/tools/commit_gate.py` | module docstring lines naming `_invalidate_advisory` / `_check_advisory_freshness` | 1–8 | doc | trim |
| R31 | `ouroboros/tools/review_evidence.py` | `_run_to_dict` (+ `_RESPONDED_STATUSES`) | 157–229 | F+K | `collect_review_evidence:86` only — advisory-only serializer |
| R32 | `ouroboros/review_evidence.py` | `collect_review_evidence` advisory portions | 22,31,34,38,51,66,72–73,80–82,86–87,98,103,106 | B | function **MIXED** — keep attempts/obligations/debts/continuations; strip advisory_runs/current_run/advisory_status/stale_* keys |
| R33 | `ouroboros/agent_task_pipeline.py` | `build_review_context` advisory-guidance block | 541 (`not state.advisory_runs`), 550–557 (`current_run` loop), 559–583 (Live-gate advisory_status/stale_marker lines) | B | function **MIXED** — keep obligations/debts/continuations + `item.advisory_findings` render (662–672) + `format_status_section` call (687, but see R19) |
| R34 | `ouroboros/tools/git.py` | `_mark_failed_bypass_advisory_stale` | 212–240 | F | `git.py:409` only (in `_advisory_bypassed` test-preflight block) |
| R35 | `ouroboros/tools/git.py` | `skip_advisory_pre_review` param (×3 signatures) + JSON-schema field | 250, 540, 1258 (signatures); 593, 1349 (pass-through); 1696 (JSON schema) | A | callers of `repo_commit`/`_repo_commit`/`_run_reviewed_stage_cycle` |
| R36 | `ouroboros/tools/git.py` | `_check_advisory_freshness` call + `no_advisory` block | 338–358 | B | the `advisory_err = _check_advisory_freshness(...)` + `if advisory_err:` block returning `block_reason="no_advisory"` |
| R37 | `ouroboros/tools/git.py` | `_advisory_bypassed` block | 377–460 (approx; `_advisory_bypassed = ...` at 377, `if/elif _advisory_bypassed` at 380/415, related preflight) | B | verify exact end; `OUROBOROS_PREFLIGHT_DIFF_AWARE` / test-preflight logic — coordinate with `remove-advisory` task wording |
| R38 | `ouroboros/tools/git.py` | `block_reason=="no_advisory"` check | 1207 | B | `if outcome.get("block_reason") == "no_advisory":` — dead after R29/R36 |
| R39 | `ouroboros/tools/git.py` | imports `_check_advisory_freshness` (31), `_invalidate_advisory` (33) | 30–33 | import | from `commit_gate` import block |
| R40 | `ouroboros/tools/shell.py` | `from ouroboros.tools.commit_gate import _invalidate_advisory` | 20 | import | — |
| R41 | `ouroboros/tools/git_pr.py` | `from ouroboros.tools.commit_gate import _invalidate_advisory` | 93 | import | — |

### A.2 `_invalidate_advisory` call-site list (13 total — exhaustive, exact lines)

| # | File | Enclosing symbol | Call line |
|---|---|---|---|
| 1 | `ouroboros/tools/git.py` | `_repo_write` | **986** |
| 2 | `ouroboros/tools/git.py` | `_repo_write` | **1001** |
| 3 | `ouroboros/tools/git.py` | `_repo_write` | **1013** |
| 4 | `ouroboros/tools/git.py` | `_str_replace_editor` | **1093** |
| 5 | `ouroboros/tools/git.py` | `_repo_write_commit` | **1178** |
| 6 | `ouroboros/tools/shell.py` | `_run_shell` | **352** |
| 7 | `ouroboros/tools/shell.py` | `_claude_code_edit` (whole func deleted in remove-sdk-layer) | **524** |
| 8 | `ouroboros/tools/git_pr.py` | `_rollback_failed_amend` | **370** |
| 9 | `ouroboros/tools/git_pr.py` | `_cherry_pick_pr_commits` | **575** |
| 10 | `ouroboros/tools/git_pr.py` | `_cherry_pick_pr_commits` | **598** |
| 11 | `ouroboros/tools/git_pr.py` | `_cherry_pick_pr_commits` | **646–647** |
| 12 | `ouroboros/tools/git_pr.py` | `_stage_adaptations` | **749–750** |
| 13 | `ouroboros/tools/git_pr.py` | `_stage_pr_merge` | **958–959** |

Plus the import statements: `git.py:33`, `shell.py:20`, `git_pr.py:93`. Definition: `commit_gate.py:264–280` (R27). The plan's "~14" count = 13 call-sites + 1 definition. (No `_invalidate_advisory` reference exists in any other `ouroboros/` file or `server.py` — verified by repo-wide grep.)

### A.3 KEEP-list — blocking triad (must NOT be touched; verified)

| # | File | Symbol | Lines | Why KEEP (1 line) | Disambiguation vs advisory twin |
|---|---|---|---|---|---|
| K1 | `ouroboros/review_state.py` | `_STATE_RELPATH = "state/advisory_review.json"` | 37 | Shared blocking-ledger file path (`attempts`/`blocking_history`/`open_obligations`/`commit_readiness_debts` persist here). Name is historical. | Used by `_load_state_unlocked:1275` / `_save_state_unlocked:1389`; deleting breaks ALL persistence incl. blocking |
| K2 | `ouroboros/review_state.py` | `_LOCK_RELPATH = "locks/advisory_review.lock"` | 38 | Lock for the shared ledger file; protects blocking writes. | Referenced by `acquire_review_state_lock`/`release_review_state_lock`; not advisory-specific |
| K3 | `ouroboros/review_state.py` | `CommitAttemptRecord` (whole class incl. field `advisory_findings`@205) | 187–219 | Blocking-triad attempt ledger; `advisory_findings` is the severity tag ("advisory" vs "critical") of triad findings — concept (2), NOT Claude SDK. | NOT `AdvisoryRunRecord` (R1). Field name collides with concept (1) but is unrelated. |
| K4 | `ouroboros/review_state.py` | `ObligationItem` | 121–136 | Blocking-triad open-obligation ledger item. | — |
| K5 | `ouroboros/review_state.py` | `CommitReadinessDebtItem` | 139–159 | Blocking-triad commit-readiness debt ledger item. | — |
| K6 | `ouroboros/review_state.py` | `AdvisoryReviewState` class shell + fields `attempts`(229)/`last_commit_attempt`(230)/`blocking_history`(231)/`open_obligations`(232)/`next_obligation_seq`(233)/`commit_readiness_debts`(234)/`next_commit_readiness_debt_seq`(235)/`state_version`(227) | 222–1166 (class kept; only R2–R14 advisory members removed) | Container for the blocking ledger. Class name is historical; do not rename/delete the class. | Remove ONLY advisory members; keep all blocking methods below |
| K7 | `ouroboros/review_state.py` | blocking methods: `latest_attempt`,`latest_attempt_for`,`get_active_attempts`,`filter_attempts`,`next_attempt_number`,`add_blocking_attempt`,`record_attempt`,`_upsert_attempt`,`_upsert_blocking_history`,`_allocate_obligation_id`,`_hydrate_obligation`,`_coalesce_open_obligations`,`_touch_obligation`,`_allocate_commit_readiness_debt_id`,`_hydrate_commit_readiness_debt`,`_build_commit_readiness_debt_observations`(minus R20),`_synthesize_missing_debts_from_observations`,`_sync_commit_readiness_debts`,`get_open_commit_readiness_debts`,`_update_obligations_from_attempt`,`resolve_obligations`,`clear_resolved_obligations`,`get_open_obligations`,`get_blocking_history`,`on_successful_commit`(minus R14 field-clears),`expire_stale_attempts` | 251–286, 314–345, 432–1166 | Core blocking-triad ledger logic. | `filter_attempts`≠`filter_advisory_runs`(R5); `latest_attempt`≠`latest`(R4) |
| K8 | `ouroboros/review_state.py` | `load_state`/`save_state`/`update_state`/`_load_state_unlocked`/`_save_state_unlocked`/`acquire_review_state_lock`/`release_review_state_lock`/`_sync_compat_views`/`_commit_attempt_from_dict`/`_obligation_from_dict`/`_commit_readiness_debt_from_dict` | 1238–1512 (region) | Persistence infra shared by blocking ledger. Trim ONLY advisory_runs/runs/last_stale_* serialization lines (R14/R21), keep function bodies. | `_record_from_dict`(R21) is advisory; `_commit_attempt_from_dict` is blocking |
| K9 | `ouroboros/review_state.py` | `make_repo_key`,`discover_repo_root`,`_repo_scope_matches`,`_repo_scope_exact_match_exists`,`_utc_now`,`_coerce_int`,`_parse_iso_ts`,`_attempt_identity_tuple`,`_infer_next_prefixed_sequence`,`_normalize_findings`,`_merge_attempt`,`_infer_phase`,`_dedupe_strings`,`_commit_readiness_debts_view`,`_stable_digest`,`_make_obligation_fingerprint`, constants `_STATE_SCHEMA_VERSION`/`_MAX_ATTEMPT_HISTORY`/`_MAX_BLOCKING_HISTORY`/`_MAX_COMMIT_READINESS_DEBTS`/`_DEFAULT_TOOL_NAME`/`_LEGACY_CURRENT_REPO_KEY`/`_REVIEW_ATTEMPT_TTL_SEC`/`_REVIEW_ATTEMPT_GRACE_SEC`/`_OPEN_COMMIT_READINESS_DEBT_STATUSES`/`_CANONICAL_OBLIGATION_ITEM_RE`/`_SNAPSHOT_EXCLUDE_PATHS`/`_MAX_RUN_HISTORY`(→R24 only one removed) | various | Shared helpers used by blocking ledger. `_LEGACY_CURRENT_REPO_KEY` is used by both — KEEP (blocking `filter_attempts`/obligations rely on it). | `_DEFAULT_TOOL_NAME="repo_commit"`(KEEP) ≠ `_DEFAULT_ADVISORY_TOOL_NAME`(R25) |
| K10 | `ouroboros/tools/commit_gate.py` | `_record_commit_attempt`(+inner `_mutate`), `_normalize_advisory_entries`, `_list_or_default`, `_continuation_source`, `_attempt_accepts_reviewing_update`, `_mark_review_attempt_late`, `_check_overlapping_review_attempt`, `_current_review_tool_name`, `_attempt_phase`(minus R29 arm) | 24–261 (minus R27/R29) | Blocking-triad attempt recording + synthesis (`load_state as _ls_synth`@114). `_normalize_advisory_entries` normalizes the severity-tag findings (concept 2). | `_normalize_advisory_entries`/`advisory_findings`@185 = concept (2) KEEP; `_invalidate_advisory`(R27)/`_check_advisory_freshness`(R28) = concept (1) REMOVE |
| K11 | `ouroboros/agent_task_pipeline.py` | `build_review_context` obligations/debts/continuations render + `item.advisory_findings` render | 585–612 (debts/obligations), 614–685 (continuations incl. `advisory_finding` render at **662–672**) | Blocking-triad continuity context incl. severity-tag findings (concept 2). | `item.advisory_findings`@662 (concept 2, KEEP) ≠ `state.advisory_runs`@541/551 (concept 1, R33) |
| K12 | `ouroboros/review_evidence.py` | `collect_review_evidence` attempts/obligations/debts/continuations portions; `_attempt_to_dict`(incl. `advisory_findings`@145); `_obligation_to_dict`; `_continuation_to_dict`(incl. `advisory_findings`@254); `_debt_to_dict`; `format_review_evidence_for_prompt` | 10–155 (minus R32 advisory keys), 232–273 | Blocking-triad evidence serialization incl. severity-tag findings. | `_run_to_dict`(R31)=concept 1 REMOVE; `_attempt_to_dict.advisory_findings`@145 = concept 2 KEEP |
| K13 | `ouroboros/config.py` | `OUROBOROS_REVIEW_ENFORCEMENT` default+parse+env-pass | 69,131,363–364,788,821–822 | Blocking-triad enforcement mode (concept 3). Default value literal is `"advisory"` — unrelated to Claude SDK. | NOT Claude SDK. Do NOT touch under any advisory removal step. |
| K14 | `ouroboros/onboarding_wizard.py` | `OUROBOROS_REVIEW_ENFORCEMENT` handling | 223–224,280,387 | Onboarding sets blocking-triad mode (concept 3). | — |
| K15 | `openspec/specs/ouroboros/ouroboros.md` | glossary "Ревью-триада" (76), `OUROBOROS_REVIEW_ENFORCEMENT` (689) | 76, 689 | Blocking-triad spec (concepts 2/3). | Plan §"Relevant Files" already flags these KEEP |

### A.4 Cross-file caveat for `remove-advisory` (NOT in the 4 task files but breaks build if missed)

- **`ouroboros/context.py:807–818`** — `build_llm_messages` calls `from ouroboros.review_state import load_state, format_status_section`; line **810** gates on `advisory_state.runs or advisory_state.last_commit_attempt`. The `.runs` property (R3) is removed. The `remove-advisory` builder MUST change line 810's condition to drop `advisory_state.runs` (e.g. gate on `advisory_state.last_commit_attempt or advisory_state.attempts or open obligations`) AND ensure `format_status_section` still renders the KEEP blocking ledger. This file is outside the 4 inventoried files but is a hard runtime dependency on R3 — flagged here so the removal does not break `import ouroboros.context` / context building.
- **`format_status_section` (R19) & `collect_review_evidence` (R32) & `build_review_context` (R33) are MIXED functions** — they must be surgically trimmed (advisory_runs/last_stale render lines deleted), NOT deleted wholesale, because `agent_task_pipeline.py:687` and `context.py:811` and `review_evidence` consumers still need the blocking-ledger portion.
- **`on_successful_commit` (K7) lines 1078–1080 & 1108–1111** clear `last_stale_*` fields (R14). When R14 fields are removed these specific lines become dead and must be deleted *with* the fields, but the method itself is KEEP.
- **`AdvisoryReviewState` / `_STATE_RELPATH`/`_LOCK_RELPATH` names are historical** — the class and both constants are the SHARED blocking ledger. Renaming or deleting any of them is out of scope and would break blocking persistence (K1/K2/K6).

### A.5 Summary counts

- Advisory-only REMOVE entries: **41** (R1–R41) covering `review_state.py` (R1–R26), `commit_gate.py` (R27–R30), `review_evidence.py` (R31–R32), `agent_task_pipeline.py` (R33), `git.py` (R34–R39), `shell.py` import (R40), `git_pr.py` import (R41).
- `_invalidate_advisory` call-sites: **13** invocations (+3 imports +1 definition) — A.2 exhaustive.
- KEEP-list entries: **15** groups (K1–K15); critical anti-regression anchors: `CommitAttemptRecord.advisory_findings` (concept 2), `OUROBOROS_REVIEW_ENFORCEMENT` (concept 3), `_STATE_RELPATH`/`_LOCK_RELPATH`, blocking ledger methods.
- MIXED functions requiring surgical trim (not deletion): `format_status_section` (R19), `collect_review_evidence` (R32), `build_review_context` (R33), `_build_commit_readiness_debt_observations` (R20 branch only), `_attempt_phase` (R29 arm only), `on_successful_commit` (R14 field-clear lines only), `_load_state_unlocked`/`_save_state_unlocked` (R14/R21 serialization lines only).
- Cross-file non-task-file hard dependency: `context.py:810` (`advisory_state.runs`) — flagged in A.4.

### A.6 Ambiguity notes (potential misclassification points)

1. **`advisory_findings`** appears as BOTH `CommitAttemptRecord.advisory_findings` (KEEP, concept 2, severity tag) and inside `AdvisoryRunRecord.items` rendering. Grep for `advisory_findings` alone will hit KEEP code — the removal must scope by symbol, not by substring.
2. **`"advisory"` string literal**: default of `OUROBOROS_REVIEW_ENFORCEMENT` (K13, KEEP) and default `phase=`/`tool_name=` of `AdvisoryRunRecord` (R1, REMOVE). Substring grep `'advisory'` is unsafe for mechanical removal.
3. **`AdvisoryReviewState` class name** itself contains "Advisory" but is the shared container (K6, KEEP). Do not delete the class; only its advisory members (R2–R14).
4. **`_STATE_RELPATH`/`_LOCK_RELPATH`** file paths literally contain `advisory_review` but are the blocking ledger store (K1/K2, KEEP). Highest-risk false positive — explicitly do NOT remove.
5. **`compute_snapshot_hash` (R15)**: classified REMOVE because every one of its 5 callers is advisory-only (verified). If a future blocking caller were added, this would need re-evaluation — at `775fa39` it is safe to delete.
6. **`_LEGACY_CURRENT_REPO_KEY`**: used by both advisory (`AdvisoryRunRecord` default) and blocking (`filter_attempts`, obligations repo-scoping). Classified KEEP (K9) — removing it would break blocking repo-scoping. The `AdvisoryRunRecord` default usage disappears with R1 but the constant stays.
