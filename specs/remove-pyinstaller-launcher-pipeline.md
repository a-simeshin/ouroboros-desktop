# Plan: Remove PyInstaller / desktop-launcher pipeline (keep run-from-source + docker/k8s)

## Task Description

Удалить из проекта Ouroboros весь desktop-deployment pipeline (PyInstaller bundling в `.dmg/.tar.gz/.zip`, immutable PyWebView shell `launcher.py`, embedded `python-build-standalone`, `repo.bundle` extraction, two-process model). Оставить только два пути запуска: **run-from-source** (`python server.py`) и **docker/k8s** (`docker run` + WEBUI_ONLY headless mode).

Стратегия: **extract-before-delete**. Утилитарные функции в `ouroboros/launcher_bootstrap.py` (Claude SDK validation, skill seeding) активно используются `server.py` и `platform_layer.py` — их сначала переселяем в новые/расширенные модули, потом удаляем bundle-bootstrap код. Аналогично `platform_layer.py` оказался cross-platform runtime layer (file locks, kill_pid_tree, subprocess kwargs, container env detect), используется 20+ модулями — режем только launcher-only символы (Win job-control + embedded-python helpers).

## Objective

После выполнения плана проект:
- Имеет ровно два supported deployment paths: `python server.py` и `docker run ouroboros-web`
- `launcher.py`, `launcher_bootstrap.py`, `Ouroboros.spec`, build-скрипты для трёх платформ, embedded python-standalone helpers — удалены
- `platform_layer.py` и `config.py` очищены от launcher-only символов
- CI убрал tier `build` + `release-preflight` (PyInstaller matrix); сохранил quick-test, full-test, integration, docker
- Тесты, которые тестировали удалённый код — удалены или рефакторены
- README/ARCHITECTURE.md актуализированы — секция "Two-process model" заменена на single-process server runtime
- pyproject.toml убрал `[project.optional-dependencies] build` и pytest-маркеры `portable_detail`, `ui_browser_docker`
- Все существующие функциональные тесты (`make test`) проходят
- Новые smoke-тесты подтверждают: `python server.py` стартует standalone, `/health` отвечает 200, REPO_DIR fail-fast работает, docker image билдится и запускается

## Problem Statement

Текущая архитектура — desktop-first: launcher (immutable PyWebView shell) спавнит server.py как subprocess, оба завязаны на embedded `python-build-standalone` interpreter, который PyInstaller бандлит в `.app/.exe/.tar.gz`. Эта модель:

1. **Тянет ~3500 LOC специфичного кода** (launcher.py 1026 LOC + launcher_bootstrap.py 961 LOC + Ouroboros.spec + три build-скрипта + два python-standalone download-скрипта + repo.bundle generator)
2. **Удваивает CI стоимость** — три отдельных матричных шарда на macOS/Linux/Windows с codesign + опциональным notarization
3. **Не нужна для server-side развёртывания** — для docker/k8s WEBUI_ONLY режима launcher и bundle-extraction избыточны
4. **Усложняет разработку** — изменения в server.py требуют понимания two-process model, exit code 42/99, PORT_FILE coordination, PID lock semantics
5. **Конфликтует с k8s** — single-machine assumptions (PID lock, embedded interpreter, ~/Ouroboros/repo bootstrap из bundle) не транслируются в pod-based runtime

Текущий проект уже движется к WebUI-only / k8s-friendly архитектуре (`WEBUI_ONLY` toggle, tools whitelist, generic git remote — commit `aaed355`). Удаление desktop pipeline — следующий шаг той же траектории.

## Solution Approach

**Восемь последовательных фаз** с верификационным шлюзом после каждой:

1. **Extract** — скопировать чистые утилиты из `launcher_bootstrap.py` в новые/расширенные модули (`ouroboros/claude_runtime.py` для SDK validation; расширение `ouroboros/skill_loader.py` для skill seeding). Старые символы остаются — это backwards-compat-only шаг для безопасной миграции consumers.

2. **Rewire imports** — обновить consumers (`server.py`, `platform_layer.py`, тесты) на новые пути. После этого launcher_bootstrap.py становится "висящим" — никто его не импортирует кроме launcher.py.

3. **Server startup hook** — добавить в `server.py::main()` минимальный `ensure_repo_present()` с **fail-fast** semantics: если **server-local `REPO_DIR`** (определён в `server.py:53` как `pathlib.Path(os.environ.get("OUROBOROS_REPO_DIR", pathlib.Path(__file__).parent))`) отсутствует или не git checkout — `raise SystemExit` с инструкцией. Без auto-init и магии — explicit error. **Важно:** проверяем именно server-local `REPO_DIR`, НЕ `config.REPO_DIR` — у них разные дефолты (`config.REPO_DIR = APP_ROOT/"repo"` = `~/Ouroboros/repo/`, `server.REPO_DIR = pathlib.Path(__file__).parent` = dir of server.py). Без launcher для unsetted env эти два значения расходятся; в run-from-source и docker сценариях корректное значение — server-local. Reconciliation `config.REPO_DIR` vs `server.REPO_DIR` — отдельный latent bug, выходит за scope этого плана (см. Notes).

4. **Delete desktop artifacts** — снести launcher.py, launcher_bootstrap.py, Ouroboros.spec, build.sh/build_linux.sh/build_windows.ps1, requirements-launcher.txt, entitlements.plist, scripts/download_python_standalone.{sh,ps1}, scripts/build_repo_bundle.py, scripts/pyi_rth_pythonnet.py, assets/icon.icns, assets/icon.ico. Удалить obsolete тесты (test_packaging_sync, test_launcher_sync, test_packaging_assets, test_build_repo_bundle, test_build_scripts).

5. **Trim platform_layer.py** — удалить только launcher-only символы: `assign_pid_to_job`, `close_job`, `create_kill_on_close_job`, `terminate_job`, `resume_process`, `embedded_python_candidates`, `embedded_pip`, `git_install_hint`, `open_path_external`. ВСЁ остальное (file locks, kill helpers, subprocess kwargs, claude runtime resolver, container detect) — KEEP, это runtime-критичный layer.

6. **Trim config.py** — `PANIC_EXIT_CODE`, `RESTART_EXIT_CODE`, `acquire_pid_lock`, `release_pid_lock` — DELETE (launcher-only). `PORT_FILE` — **KEEP**: используется `server.py:58, 1169, 2174` + `extension_loader.py:920-923` (НЕ launcher-only). Server.py определяет exit codes локально (`server.py:84-85`) и os._exit'ит через них для self-restart — это валидная семантика для systemd/docker restart policy, остаётся.

7. **Refactor mixed-concern tests** — `test_runtime_mode_elevation.py` имеет 5 launcher-импортов в конце файла (тесты `_request_runtime_mode_change`, `_request_skill_key_grant` — launcher-only PyWebView dialogs). Удалить эти 5 тестов, оставить тесты config/server/tools chokepoint (~75% файла). `test_marketplace_skill_loader_migration.py`, `test_per_skill_version_resync.py`, `test_claude_code_gateway.py` — поменять импорты с `from ouroboros.launcher_bootstrap import` на новые пути.

8. **CI / Docs / pyproject** — выкинуть build/release-preflight/release jobs из `.github/workflows/ci.yml`. README.md убрать секции про .dmg/.exe download. docs/ARCHITECTURE.md переписать "Two-process model" секцию. pyproject.toml убрать `build` extra и `portable_detail`/`ui_browser_docker` markers. Review prompts/SYSTEM.md и prompts/SAFETY.md на упоминания launcher.

## Relevant Files

### Файлы для удаления (Phase 4):
- `launcher.py` (1026 LOC) — immutable PyWebView shell
- `ouroboros/launcher_bootstrap.py` (961 LOC) — bundle extraction + утилиты (после extract)
- `Ouroboros.spec` (147 LOC) — PyInstaller spec
- `build.sh` (229 LOC) — macOS DMG build + codesign + notarize
- `build_linux.sh` (65 LOC) — Linux .tar.gz build
- `build_windows.ps1` (89 LOC) — Windows .zip build
- `requirements-launcher.txt` (3 LOC) — pywebview/pythonnet
- `entitlements.plist` — macOS entitlements
- `scripts/download_python_standalone.sh`, `scripts/download_python_standalone.ps1`
- `scripts/build_repo_bundle.py` (401 LOC) — repo.bundle generator
- `scripts/pyi_rth_pythonnet.py` — Windows runtime hook
- `assets/icon.icns`, `assets/icon.ico` — app icons (screenshots в `assets/` оставить)
- `tests/test_packaging_sync.py`, `tests/test_launcher_sync.py`, `tests/test_packaging_assets.py`, `tests/test_build_repo_bundle.py`, `tests/test_build_scripts.py`

### Файлы для модификации:
- `ouroboros/platform_layer.py` (924 LOC) — удалить launcher-only символы (~150 LOC)
- `ouroboros/config.py` (842 LOC) — удалить `PANIC_EXIT_CODE`, `RESTART_EXIT_CODE`, `acquire_pid_lock`, `release_pid_lock` (~30 LOC)
- `server.py` — добавить `ensure_repo_present()` в main() (вызов до `start_supervisor()`/`uvicorn.run()`); обновить импорты с launcher_bootstrap на новые модули (lines 1228, 1978)
- `ouroboros/skill_loader.py` — расширить функциями skill seeding (~327 LOC из launcher_bootstrap.py: `_read_skill_manifest_version`, `_record_skill_upgrade_migration`, `_reseed_native_skill_in_place`, `_per_skill_version_resync`, `_seed_skills_into`, `ensure_data_skills_seeded`, `cleanup_orphaned_seed_markers`)
- `tests/test_runtime_mode_elevation.py` — удалить 5 launcher-only тестов (5x `import launcher` на строках 403, 418, 479, 556, 605)
- `tests/test_marketplace_skill_loader_migration.py` — обновить импорты
- `tests/test_per_skill_version_resync.py` — обновить импорты
- `tests/test_claude_code_gateway.py` — обновить импорты (BootstrapContext → ClaudeRuntimeContext, verify_claude_runtime, _CLAUDE_SDK_MIN_VERSION, _version_tuple)
- `pyproject.toml` — drop `[project.optional-dependencies] build`, drop markers `portable_detail`, `ui_browser_docker`
- `.github/workflows/ci.yml` — удалить jobs `build`, `release-preflight`, `release`; обновить `paths:` фильтры
- `README.md` — убрать секции про desktop downloads, оставить run-from-source + docker quick start
- `docs/ARCHITECTURE.md` (2238 LOC) — переписать "Two-process model" секцию (single-process server)
- `prompts/SYSTEM.md`, `prompts/SAFETY.md` — review на упоминания launcher; обновить если найдены

### New Files
- `ouroboros/claude_runtime.py` — Claude SDK validation: `_CLAUDE_SDK_BASELINE`, `_CLAUDE_SDK_MIN_VERSION`, `_version_tuple`, `verify_claude_runtime`, `ClaudeRuntimeContext` (упрощённый аналог `BootstrapContext` без bundle-полей)
- `tests/test_claude_runtime.py` — unit-тесты для нового модуля
- `tests/test_server_startup_hook.py` — unit-тесты для `ensure_repo_present()`
- `tests/test_post_refactor_integration.py` — 4 integration сценария

## Implementation Phases

### Phase 1: Foundation (Extract before delete)

Цель: вынести reusable утилиты из `launcher_bootstrap.py` в новые модули, не ломая существующих consumers. После фазы новые модули существуют, старые ссылки работают.

- Создать `ouroboros/claude_runtime.py` — переместить `_CLAUDE_SDK_BASELINE`, `_CLAUDE_SDK_MIN_VERSION`, `_version_tuple`, `verify_claude_runtime`. Заменить параметр `BootstrapContext` на новый `ClaudeRuntimeContext` (только `embedded_python` field, без `data_dir`, `repo_dir`, `bundle_root`).
- Расширить `ouroboros/skill_loader.py` — переместить `_read_skill_manifest_version`, `_record_skill_upgrade_migration`, `_reseed_native_skill_in_place`, `_per_skill_version_resync`, `_seed_skills_into`, `ensure_data_skills_seeded`, `cleanup_orphaned_seed_markers`.
- В `launcher_bootstrap.py` оставить тонкие re-export'ы: `from ouroboros.claude_runtime import _CLAUDE_SDK_BASELINE` и т.д. (TEMPORARY shim — удалится в Phase 4).

### Phase 2: Core Implementation (Rewire + Server hook)

- Обновить consumers:
  - `server.py:1228` `from ouroboros.launcher_bootstrap import _CLAUDE_SDK_BASELINE` → `from ouroboros.claude_runtime import _CLAUDE_SDK_BASELINE`
  - `server.py:1978` `from ouroboros.launcher_bootstrap import ensure_data_skills_seeded` → `from ouroboros.skill_loader import ensure_data_skills_seeded`
  - `ouroboros/platform_layer.py:621, 632` `from ouroboros.launcher_bootstrap import` → `from ouroboros.claude_runtime import`
  - `tests/test_marketplace_skill_loader_migration.py`, `tests/test_per_skill_version_resync.py`, `tests/test_claude_code_gateway.py` — обновить импорты
- Добавить `server.py::ensure_repo_present()` — использует **module-level `REPO_DIR`** (server.py:53), НЕ импортирует `config.REPO_DIR`:
  ```python
  def ensure_repo_present() -> None:
      """Fail-fast if server-local REPO_DIR is missing or not a git checkout.

      For docker/k8s: REPO_DIR must come from image content or PVC mount.
      For run-from-source: user clones the repo before running server.py.
      Override via OUROBOROS_REPO_DIR env (existing escape hatch from server.py:53).
      """
      if not REPO_DIR.exists() or not (REPO_DIR / ".git").is_dir():
          raise SystemExit(
              f"REPO_DIR not found at {REPO_DIR}.\n"
              f"For docker/k8s: ensure image content or PVC mount populates this path.\n"
              f"For run-from-source: clone the agent repo before running server.py, "
              f"or set OUROBOROS_REPO_DIR=<path-to-existing-checkout>."
          )
  ```
  Вызвать из `main()` ДО запуска uvicorn / supervisor thread.
- Запустить `pytest tests/test_smoke.py` + `ruff check` — sanity проверка.

### Phase 3: Integration & Polish (Delete + Trim + CI/Docs)

- Удалить файлы из секции "Файлы для удаления" (Phase 4 в общем плане).
- Удалить launcher-only символы из `platform_layer.py`: `assign_pid_to_job`, `close_job`, `create_kill_on_close_job`, `terminate_job`, `resume_process`, `embedded_python_candidates`, `embedded_pip`, `git_install_hint`, `open_path_external`.
- Удалить из `config.py`: `PANIC_EXIT_CODE`, `RESTART_EXIT_CODE`, `acquire_pid_lock`, `release_pid_lock`. **KEEP** `PORT_FILE` (используется extension_loader + server + 3 тест-модулями).
- Обновить `tests/test_runtime_mode_elevation.py` — удалить 5 launcher-импортов и связанные тесты. Сохранить тесты config/server/tools chokepoint.
- Обновить `.github/workflows/ci.yml` — удалить jobs `build`, `release-preflight`, `release`; обновить `paths:` (убрать `build.sh`, `build_linux.sh`, `build_windows.ps1`, `launcher.py` из watch-листов).
- Обновить `pyproject.toml` — drop `build` extra, drop `portable_detail`, `ui_browser_docker` markers.
- Обновить `README.md`, `docs/ARCHITECTURE.md`, review `prompts/SYSTEM.md` + `prompts/SAFETY.md`.
- Запустить полную валидацию: `make test`, `ruff check`, docker build smoke.

## Team Orchestration

- Я (планер) — высокоуровневый директор, не пишу код напрямую. Делегирую через `Agent` + `Task*` инструменты.
- Каждая фаза = один или несколько builder-агентов с резюмированием контекста через `SendMessage` для последовательных подзадач.
- Validator вызывается после Phase 8 для финального gate.

### Team Members

- Builder
  - Name: builder-extract
  - Role: Phase 1 — extract Claude SDK + skill seeding утилиты в новые/расширенные модули. Создать `ouroboros/claude_runtime.py`, расширить `ouroboros/skill_loader.py`.
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: builder-rewire
  - Role: Phase 2 — обновить импорты consumers (server.py, platform_layer.py, 3 тест-модуля); добавить `ensure_repo_present()` в server.py main().
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: builder-cleanup
  - Role: Phase 3-5 — удалить desktop artifacts (launcher.py, build scripts, Ouroboros.spec, etc.); trim platform_layer.py + config.py; refactor test_runtime_mode_elevation.py.
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: builder-ci-docs
  - Role: Phase 6+8 — обновить .github/workflows/ci.yml, pyproject.toml, README.md, docs/ARCHITECTURE.md, review prompts/.
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: test-builder-unit
  - Role: Написать unit-тесты для `claude_runtime.py`, расширенного `skill_loader.py`, `ensure_repo_present()`.
  - Agent Type: builder
  - Resume: false

- Builder
  - Name: test-builder-integration
  - Role: Написать 4 integration scenarios: server boot standalone, repo missing fail-fast, claude_runtime.verify_claude_runtime importable, skill_loader.ensure_data_skills_seeded post-migration.
  - Agent Type: builder
  - Resume: false

- Builder
  - Name: test-builder-e2e
  - Role: Написать Playwright smoke против /health после refactor (single test, минимальный).
  - Agent Type: builder
  - Resume: false

- Validator
  - Name: validator-final
  - Role: Финальная валидация всех acceptance criteria + verification commands.
  - Agent Type: validator
  - Resume: false

## Testing Strategy

Test pyramid ratio: **80% unit / 15% integration-API / 5% UI e2e**

### Unit Tests (80%)
- `tests/test_claude_runtime.py` — для нового модуля:
  - `test_version_tuple_parses_pep440()` — pre-release / dev versions
  - `test_version_tuple_handles_invalid_input()` — graceful fallback
  - `test_verify_claude_runtime_passes_when_sdk_meets_baseline()` — mock pip + version
  - `test_verify_claude_runtime_fails_when_sdk_below_baseline()` — triggers upgrade path
  - `test_verify_claude_runtime_handles_missing_sdk()` — pip install attempted
  - `test_claude_runtime_context_no_bundle_fields()` — отличия от старого BootstrapContext
- `tests/test_skill_loader.py` (расширить существующий) — добавить тесты для миграированных функций:
  - `test_ensure_data_skills_seeded_creates_seed_dir()` — fresh data dir
  - `test_ensure_data_skills_seeded_skips_when_marker_present()` — idempotency
  - `test_per_skill_version_resync_upgrades_outdated()` — version comparison
  - `test_read_skill_manifest_version_handles_missing_file()` — graceful fallback
  - `test_seed_skills_into_copies_native_skills()` — directory walk
  - `test_cleanup_orphaned_seed_markers_removes_stale()` — marker cleanup
- `tests/test_server_startup_hook.py` — для `ensure_repo_present()`:
  - `test_ensure_repo_present_passes_when_repo_dir_exists()` — happy path
  - `test_ensure_repo_present_passes_when_git_subdir_exists()` — valid git checkout
  - `test_ensure_repo_present_raises_systemexit_when_missing()` — fail-fast
  - `test_ensure_repo_present_error_message_mentions_repo_dir_path()` — actionable error
  - `test_ensure_repo_present_error_message_mentions_data_dir_env()` — env override hint

### Integration / API Tests (15%)
- `tests/test_post_refactor_integration.py` — 4 mandatory сценария:
  - `test_server_boots_standalone_and_health_returns_200()` — subprocess `python server.py --port <free>` (cwd = repo root) + httpx GET /health (timeout 30s, no launcher in process tree). REPO_DIR resolves correctly (server-local default = `pathlib.Path(__file__).parent` = repo root).
  - `test_server_fails_fast_when_repo_dir_missing()` — subprocess `python server.py` с **`OUROBOROS_REPO_DIR=<tmp_path>`** env override (existing escape hatch на `server.py:53`). tmp_path не содержит `.git` — server должен выйти с exit code != 0 + stderr содержит `"REPO_DIR not found"`. **НЕ** манипулируем HOME — это не работает, server.py берёт REPO_DIR из своего `__file__`.parent если env unset.
  - `test_claude_runtime_module_importable_post_migration()` — import + `verify_claude_runtime` callable; symbols `_CLAUDE_SDK_BASELINE`, `_CLAUDE_SDK_MIN_VERSION`, `_version_tuple` accessible
  - `test_skill_loader_ensure_data_skills_seeded_post_migration()` — `from ouroboros.skill_loader import ensure_data_skills_seeded` + invoke against temp data dir + verify seeding correctness

### UI E2E Tests (5%)
- `tests/test_smoke_e2e_post_refactor.py` (Playwright, marker `ui_browser`):
  - `test_health_endpoint_loads_in_browser()` — Playwright Chromium → http://127.0.0.1:8765/health → assert response 200 + content "ok" (или эквивалент)

## Test Infrastructure (User-Declared)

### Unit Layer (Python)
- **Files glob:** `tests/test_*.py` — конкретно новые: `tests/test_claude_runtime.py`, `tests/test_skill_loader.py` (extended), `tests/test_server_startup_hook.py`
- **Infra signature (regex, optional for unit):** `import pytest|from pytest_mock import|monkeypatch\.|tmp_path` — стандартный pytest pattern проекта
- **Happy-path scenarios (≥1 named):**
  - `tests/test_claude_runtime.py::test_verify_claude_runtime_passes_when_sdk_meets_baseline`
  - `tests/test_skill_loader.py::test_ensure_data_skills_seeded_creates_seed_dir`
  - `tests/test_server_startup_hook.py::test_ensure_repo_present_passes_when_repo_dir_exists`
- **Runner command:** `python3 -m pytest tests/test_claude_runtime.py tests/test_skill_loader.py tests/test_server_startup_hook.py -q --tb=short`
- **Realism rationale:** Pytest + pytest-mock + tmp_path — это уже существующий test stack проекта (`pyproject.toml [tool.pytest.ini_options]`); никакой новой инфраструктуры не вводим.

### Integration Layer (Python)  — MANDATORY, never Skipped
- **Files glob:** `tests/test_post_refactor_integration.py`
- **Infra signature (regex, ≥1 match per file):** `subprocess\.(Popen|run)|httpx\.(Client|get|AsyncClient)|tempfile\.TemporaryDirectory` — реальный subprocess server.py + httpx HTTP client против него
- **Happy-path scenarios (≥1 named):**
  - `test_server_boots_standalone_and_health_returns_200` — covers main "run-from-source" use-case
  - `test_server_fails_fast_when_repo_dir_missing` — covers `ensure_repo_present()` contract (subprocess + `OUROBOROS_REPO_DIR=<tmp>` env override → SystemExit with stderr message)
  - `test_claude_runtime_module_importable_post_migration` — covers extract correctness
  - `test_skill_loader_ensure_data_skills_seeded_post_migration` — covers extract correctness
- **Runner command:** `python3 -m pytest tests/test_post_refactor_integration.py -q --tb=short -m "not browser"`
- **Realism rationale:** Реальный subprocess `python server.py` + httpx HTTP client — максимальная realism для проекта без вынесения в Docker (Docker integration отдельно через CI tier `docker`); никакого моккинга process boundary, тест воспроизводит точный production runtime поведение.

### E2E Layer (Python + Playwright)  — Enabled
- **Status:** Enabled
- **Files glob:** `tests/test_smoke_e2e_post_refactor.py`
- **Infra signature (regex, ≥1 match per file):** `from playwright|import playwright|sync_playwright\(\)|@pytest\.mark\.ui_browser`
- **Happy-path scenarios (≥1 named):**
  - `test_health_endpoint_loads_in_browser` — Playwright Chromium GET http://127.0.0.1:8765/health, assert 200 + body
- **Runner command:** `python3 -m pytest tests/test_smoke_e2e_post_refactor.py -m ui_browser -q --tb=short`
- **Realism rationale:** Playwright уже опциональная зависимость проекта (`[project.optional-dependencies] browser`), маркер `ui_browser` уже зарегистрирован в pyproject.toml — реиспользуем существующий setup; реальный browser против реального server даёт полный E2E happy-path.

## Step by Step Tasks

- IMPORTANT: Execute every step in order, top to bottom. Each task maps directly to a `TaskCreate` call.
- Before you start, run `TaskCreate` to create the initial task list that all team members can see and execute.

### 1. Extract Claude runtime utilities
- **Task ID**: extract-claude-runtime
- **Depends On**: none
- **Assigned To**: builder-extract
- **Agent Type**: builder
- **Stack**: python python-patterns layout typing io errors logging dataclass
- **Parallel**: true (с extract-skill-seeding)
- **Tests**: Unit: `tests/test_claude_runtime.py` — все 6 тестов из Testing Strategy (см. unit-tests task ниже)
- Создать новый модуль `ouroboros/claude_runtime.py`
- Перенести в него из `ouroboros/launcher_bootstrap.py`: `_CLAUDE_SDK_BASELINE`, `_CLAUDE_SDK_MIN_VERSION`, `_version_tuple`, `verify_claude_runtime` (полные тела функций со строк 403-490 launcher_bootstrap.py)
- Создать новый dataclass `ClaudeRuntimeContext(embedded_python: pathlib.Path)` — упрощённый аналог `BootstrapContext` без bundle-полей. `verify_claude_runtime` адаптировать под новый context type.
- В `launcher_bootstrap.py` заменить оригинальные определения на re-exports: `from ouroboros.claude_runtime import _CLAUDE_SDK_BASELINE, _CLAUDE_SDK_MIN_VERSION, _version_tuple, verify_claude_runtime` (TEMPORARY shim). `BootstrapContext` остаётся в launcher_bootstrap.py пока (он используется bundle-кодом, который снесём в Phase 4).
- Запустить `python3 -m pytest tests/test_claude_code_gateway.py -q -k "claude_sdk or version_tuple or verify_claude"` — убедиться что existing tests проходят с новым модулем (через shim).

### 2. Extract skill seeding utilities
- **Task ID**: extract-skill-seeding
- **Depends On**: none
- **Assigned To**: builder-extract
- **Agent Type**: builder
- **Stack**: python python-patterns layout typing io errors logging pathlib
- **Parallel**: true (с extract-claude-runtime)
- **Tests**: Unit: `tests/test_skill_loader.py` extension — все 6 новых тестов из Testing Strategy
- В `ouroboros/skill_loader.py` добавить функции из `ouroboros/launcher_bootstrap.py` (строки 492-906): `_read_skill_manifest_version`, `_record_skill_upgrade_migration`, `_reseed_native_skill_in_place`, `_per_skill_version_resync`, `_seed_skills_into`, `ensure_data_skills_seeded`, `cleanup_orphaned_seed_markers`
- Сохранить точные сигнатуры и поведение — это lift-and-shift, не рефакторинг
- В `launcher_bootstrap.py` заменить оригинальные определения на re-exports: `from ouroboros.skill_loader import ensure_data_skills_seeded, _per_skill_version_resync, _read_skill_manifest_version, ...` (TEMPORARY shim)
- Запустить `python3 -m pytest tests/test_marketplace_skill_loader_migration.py tests/test_per_skill_version_resync.py -q --tb=short` — existing tests должны проходить через shim

### 3. Rewire consumer imports
- **Task ID**: rewire-imports
- **Depends On**: extract-claude-runtime, extract-skill-seeding
- **Assigned To**: builder-rewire
- **Agent Type**: builder
- **Stack**: python python-patterns layout typing
- **Parallel**: false
- **Tests**: Полный smoke прогон `make test` после этой задачи — все imports должны резолвиться
- Обновить `server.py:1228` — `from ouroboros.launcher_bootstrap import _CLAUDE_SDK_BASELINE` → `from ouroboros.claude_runtime import _CLAUDE_SDK_BASELINE`
- Обновить `server.py:1978` — `from ouroboros.launcher_bootstrap import ensure_data_skills_seeded` → `from ouroboros.skill_loader import ensure_data_skills_seeded`
- Обновить `ouroboros/platform_layer.py:621, 632` — `from ouroboros.launcher_bootstrap import _CLAUDE_SDK_MIN_VERSION, _version_tuple` → `from ouroboros.claude_runtime import _CLAUDE_SDK_MIN_VERSION, _version_tuple`
- Обновить `tests/test_marketplace_skill_loader_migration.py` (3x) — `from ouroboros.launcher_bootstrap import ensure_data_skills_seeded` → `from ouroboros.skill_loader import ensure_data_skills_seeded`
- Обновить `tests/test_per_skill_version_resync.py` (11x) — все `from ouroboros.launcher_bootstrap import _per_skill_version_resync` / `_read_skill_manifest_version` → `from ouroboros.skill_loader import ...`
- Обновить `tests/test_claude_code_gateway.py` (10x) — `from ouroboros.launcher_bootstrap import` → `from ouroboros.claude_runtime import`. Учесть что `BootstrapContext` → `ClaudeRuntimeContext` (если тесты используют его поля — адаптировать).
- Удалить TEMPORARY re-export shims из `launcher_bootstrap.py` (Phase 1 их добавлял для безопасности).
- Запустить `make test` — все 137+ тест-модулей должны проходить кроме известных launcher-only (test_packaging_*, test_build_*, test_launcher_*).

### 4. Add server startup hook
- **Task ID**: add-server-hook
- **Depends On**: rewire-imports
- **Assigned To**: builder-rewire
- **Agent Type**: builder
- **Stack**: python python-patterns errors logging Path pathlib
- **Parallel**: false
- **Tests**: Unit: `tests/test_server_startup_hook.py` — все 5 тестов из Testing Strategy
- В `server.py` добавить функцию `ensure_repo_present()`. Использует **module-level `REPO_DIR`** уже определённый на `server.py:53` (server-local; defaults to `pathlib.Path(__file__).parent`, override via `OUROBOROS_REPO_DIR` env). НЕ импортировать `config.REPO_DIR` — у него другие defaults:
  ```python
  def ensure_repo_present() -> None:
      """Fail-fast if server-local REPO_DIR is missing or not a git checkout.

      For docker/k8s: REPO_DIR must come from PVC mount or image content.
      For run-from-source: user clones the repo before running server.py.
      Override path via OUROBOROS_REPO_DIR env (existing escape hatch from server.py:53).
      """
      if not REPO_DIR.exists() or not (REPO_DIR / ".git").is_dir():
          raise SystemExit(
              f"REPO_DIR not found at {REPO_DIR}.\n"
              f"For docker/k8s: ensure image content or PVC mount populates this path.\n"
              f"For run-from-source: clone the agent repo before running server.py, "
              f"or set OUROBOROS_REPO_DIR=<path-to-existing-checkout>."
          )
  ```
- Вызвать `ensure_repo_present()` в `server.py::main()` ДО `start_supervisor()` / `uvicorn.run()` вызова
- Запустить новые unit-тесты: `python3 -m pytest tests/test_server_startup_hook.py -q --tb=short`

### 5. Delete desktop artifacts
- **Task ID**: delete-desktop-artifacts
- **Depends On**: add-server-hook
- **Assigned To**: builder-cleanup
- **Agent Type**: builder
- **Stack**: python python-patterns layout
- **Parallel**: false
- **Tests**: После удаления — `make test` должен проходить (без удалённых тестов в селекции)
- Удалить файлы: `launcher.py`, `ouroboros/launcher_bootstrap.py`, `Ouroboros.spec`, `build.sh`, `build_linux.sh`, `build_windows.ps1`, `requirements-launcher.txt`, `entitlements.plist`, `scripts/download_python_standalone.sh`, `scripts/download_python_standalone.ps1`, `scripts/build_repo_bundle.py`, `scripts/pyi_rth_pythonnet.py`, `assets/icon.icns`, `assets/icon.ico`
- Удалить obsolete тесты: `tests/test_packaging_sync.py`, `tests/test_launcher_sync.py`, `tests/test_packaging_assets.py`, `tests/test_build_repo_bundle.py`, `tests/test_build_scripts.py`
- Запустить `make test` для подтверждения отсутствия broken imports

### 6. Trim platform_layer.py
- **Task ID**: trim-platform-layer
- **Depends On**: delete-desktop-artifacts
- **Assigned To**: builder-cleanup
- **Agent Type**: builder
- **Stack**: python python-patterns layout typing
- **Parallel**: true (с trim-config)
- **Tests**: `make test` после удаления — `tests/test_platform_guard.py` должен проходить (известные launcher-only assertions могут потребовать обновления)
- Удалить из `ouroboros/platform_layer.py` следующие символы:
  - `assign_pid_to_job` (line 864)
  - `close_job` (line 896)
  - `create_kill_on_close_job` (line 834)
  - `terminate_job` (line 886)
  - `resume_process` (line 906)
  - `embedded_python_candidates` (line 426)
  - `embedded_pip` (line 439)
  - `git_install_hint` (line 772)
  - `open_path_external` (line 49) — **DELETE confirmed**: grep подтвердил, единственные потребители — `launcher.py:65, 966` (оба удаляются в Phase 4). Никаких active consumers вне launcher.
- Удалить связанные internal helpers (Win job-control wrappers, ctypes structs если применимо), которые становятся unreachable после удаления exports
- НЕ ТРОГАТЬ: file locks, kill_pid_tree, kill_process_tree, terminate_process_tree, force_kill_pid, kill_process_on_port, subprocess_*_kwargs, merge_hidden_kwargs, pid_lock_acquire/release, ClaudeRuntimeState, resolve_claude_runtime, is_container_env, get_system_memory, get_cpu_info, create_new_session, file_lock_*, file_unlock — это runtime-критичный API
- Обновить `tests/test_platform_guard.py` если он assertion'ит на удалённые символы

### 7. Trim config.py
- **Task ID**: trim-config
- **Depends On**: delete-desktop-artifacts
- **Assigned To**: builder-cleanup
- **Agent Type**: builder
- **Stack**: python python-patterns layout typing
- **Parallel**: true (с trim-platform-layer)
- **Tests**: `make test` после удаления; точечно `tests/test_extension_loader.py` (использует PORT_FILE — должен пройти, так как PORT_FILE остаётся)
- Удалить из `ouroboros/config.py`:
  - `PANIC_EXIT_CODE`
  - `RESTART_EXIT_CODE`
  - `acquire_pid_lock` (compat wrapper around platform_layer.pid_lock_acquire)
  - `release_pid_lock` (compat wrapper around platform_layer.pid_lock_release)
- **KEEP** `PORT_FILE` — единственный внешний потребитель `config.PORT_FILE` это `ouroboros/extension_loader.py:920` (+ 3 тест-модуля моккают через `monkeypatch.setattr(cfg, "PORT_FILE", ...)`). `server.py` определяет СОБСТВЕННЫЙ локальный `PORT_FILE` на line 58 — он НЕ потребляет `config.PORT_FILE`. Удаление `config.PORT_FILE` сломало бы extension_loader.py — KEEP.
- **KEEP** все остальное: `REPO_DIR`, `DATA_DIR`, `SETTINGS_PATH`, `AGENT_SERVER_PORT`, `apply_settings_to_env`, `load_settings`, `save_settings`, `read_version`, `normalize_runtime_mode`
- `server.py:84-85` оставить — `RESTART_EXIT_CODE = 42`, `PANIC_EXIT_CODE = 99` определены локально для self-restart семантики (полезно для systemd `Restart=on-failure` + docker `--restart=on-failure`); это не launcher-coupled

### 8. Refactor mixed-concern tests
- **Task ID**: refactor-runtime-mode-tests
- **Depends On**: delete-desktop-artifacts
- **Assigned To**: builder-cleanup
- **Agent Type**: builder
- **Stack**: python pytest pytest-mock unit test structure
- **Parallel**: true (с trim-platform-layer, trim-config)
- **Tests**: `python3 -m pytest tests/test_runtime_mode_elevation.py -q --tb=short`
- Открыть `tests/test_runtime_mode_elevation.py`. Удалить эти 5 launcher-only test functions (определены на строках 402, 417, 468, 552, 604):
  - `test_launcher_runtime_mode_bridge_saves_after_confirmation` (line 402)
  - `test_launcher_skill_key_grant_validates_review_and_manifest` (line 417)
  - `test_launcher_skill_key_grant_supports_extensions` (line 468)
  - `test_launcher_skill_key_grant_handles_reconcile_http_error` (line 552)
  - `test_launcher_skill_key_grant_rejects_instruction_skill` (line 604)
- Все остальные тесты (`test_save_settings_*`, `test_data_write_*`, `test_merge_settings_payload_*`, `test_set_tool_timeout_*`, `test_onboarding_can_set_initial_runtime_mode_pro`, `test_light_mode_*`, `test_elevation_indicators_*`, `test_files_api_*`, `test_run_shell_blocks_*`, `test_save_settings_consent_*`, `test_initialize_baseline_*`) — KEEP, они проверяют config/server/tools chokepoint, не launcher
- File-level docstring (строка 17 примерно) содержит "launcher / wizard paths can set any initial mode" — удалить упоминание launcher, оставить только wizard
- Также удалить связанные helper-функции (если есть) которые использовались ТОЛЬКО удалёнными 5 тестами — после удаления запустить `python3 -m pyflakes tests/test_runtime_mode_elevation.py` для отлова orphaned helpers

### 9. Update CI workflow
- **Task ID**: update-ci
- **Depends On**: delete-desktop-artifacts
- **Assigned To**: builder-ci-docs
- **Agent Type**: builder
- **Stack**: python python-patterns layout
- **Parallel**: true (с update-pyproject, update-docs)
- **Tests**: GitHub Actions YAML lint через `actionlint` (если есть) ИЛИ ручной просмотр; реальная валидация — после push в test branch
- Открыть `.github/workflows/ci.yml`
- Удалить целиком jobs: `build`, `release-preflight`, `release` (jobs ниже `release-preflight` тоже выкинуть — это GitHub Release upload)
- Из `paths:` фильтра on `push`/`pull_request` триггеров убрать: `'build.sh'`, `'build_linux.sh'`, `'build_windows.ps1'`, `'launcher.py'`, `'Ouroboros.spec'`
- KEEP jobs: `quick-test`, `full-test`, `integration-test`, и docker-related ступень (line 188, 211 — `docker build -t ouroboros-web:test .`)
- Verify `needs:` graph чистый — после удаления `release-preflight` ничего не должно ссылаться на этот job

### 10. Update pyproject.toml
- **Task ID**: update-pyproject
- **Depends On**: delete-desktop-artifacts
- **Assigned To**: builder-ci-docs
- **Agent Type**: builder
- **Stack**: python pyproject ruff python-patterns layout
- **Parallel**: true (с update-ci, update-docs)
- **Tests**: `python3 -c "import tomllib; tomllib.load(open('pyproject.toml','rb'))"` — TOML parses
- Удалить из `[project.optional-dependencies]`:
  - `build = ["pyinstaller>=6.0"]`
- Из `all = [...]` массива убрать `pyinstaller>=6.0` если присутствует
- Удалить из `markers` в `[tool.pytest.ini_options]`:
  - `"portable_detail: portable/build artifact detail checks; ..."`
  - `"ui_browser_docker: launches UI smoke checks against Docker runtime; ..."`
- Из `addopts` строки убрать `not portable_detail and not ui_browser_docker` (если присутствуют)
- KEEP остальные markers: `integration`, `browser`, `ui_browser`, `docker`

### 11. Update docs and prompts
- **Task ID**: update-docs
- **Depends On**: delete-desktop-artifacts
- **Assigned To**: builder-ci-docs
- **Agent Type**: builder
- **Stack**: python python-patterns layout
- **Parallel**: true (с update-ci, update-pyproject)
- **Tests**: Markdown lint optional; manual review
- `README.md` — удалить секции про .dmg/.exe/.tar.gz download, codesign, notarization. Заменить на:
  - Quick start с `python server.py`
  - Quick start с `docker run`
  - Ссылка на ARCHITECTURE.md для деталей
  - **Раздел "Upgrade from desktop bundle"** — параграф для существующих пользователей которые ставили через .dmg/.exe (см. Notes для текста: указать `OUROBOROS_REPO_DIR=~/Ouroboros/repo` или `cd ~/Ouroboros/repo && python server.py`)
- Добавить параграф в CHANGELOG.md (или эквивалент release notes) про breaking change: desktop bundle pipeline удалён, миграция через env var или git clone
- `docs/ARCHITECTURE.md` — переписать секцию "Two-process model" (строки около начала файла) на single-process model:
  - server.py теперь main process, supervisor в фоновом thread'е
  - Удалить параграфы про launcher / immutable shell / restart на code 42 (или переформулировать как "process exit codes for systemd/docker restart policy")
  - Удалить параграфы про embedded python-standalone / repo.bundle bootstrap
  - Добавить секцию про `ensure_repo_present()` fail-fast semantics
- `prompts/SYSTEM.md`, `prompts/SAFETY.md` — grep на "launcher", "PyInstaller", ".app", ".dmg", "bundle"; обновить найденные параграфы или удалить
- Запустить `grep -ri "launcher\|PyInstaller\|\.dmg\|\.exe.*ouroboros\|repo\.bundle" docs/ prompts/ README.md` — убедиться что остались только intentional references (например, упоминания в changelog)

### 12. Write unit tests
- **Task ID**: unit-tests
- **Depends On**: extract-claude-runtime, extract-skill-seeding, add-server-hook
- **Assigned To**: test-builder-unit
- **Agent Type**: builder
- **Stack**: python pytest pytest-mock unit test structure fixtures parametrize
- **Parallel**: true (с integration-tests если фикстуры не пересекаются)
- Написать `tests/test_claude_runtime.py` с тестами:
  - `test_version_tuple_parses_pep440` (parametrize: "0.1.60", "0.2.0a1", "1.0.0")
  - `test_version_tuple_handles_invalid_input` (parametrize: "", "not-a-version")
  - `test_verify_claude_runtime_passes_when_sdk_meets_baseline` (mocker.patch on subprocess.run)
  - `test_verify_claude_runtime_fails_when_sdk_below_baseline`
  - `test_verify_claude_runtime_handles_missing_sdk`
  - `test_claude_runtime_context_no_bundle_fields` (assert dataclass fields)
- Расширить `tests/test_skill_loader.py` тестами из Testing Strategy
- Написать `tests/test_server_startup_hook.py` с 5 тестами из Testing Strategy
- Все тесты должны использовать `tmp_path` для filesystem isolation, `monkeypatch` для config overrides, `mocker` (pytest-mock) для subprocess моков

### 13. Write integration tests
- **Task ID**: integration-tests
- **Depends On**: extract-claude-runtime, extract-skill-seeding, add-server-hook, delete-desktop-artifacts
- **Assigned To**: test-builder-integration
- **Agent Type**: builder
- **Stack**: python pytest integration httpx asgitransport subprocess test data integration
- **Parallel**: false
- Создать `tests/test_post_refactor_integration.py` с 4 mandatory сценариями (см. Test Infrastructure (User-Declared) → Integration Layer):
  - `test_server_boots_standalone_and_health_returns_200` — subprocess.Popen + httpx.get с retry до timeout 30s
  - `test_server_fails_fast_when_repo_dir_missing` — subprocess `python server.py` с env `OUROBOROS_REPO_DIR=<tmp_path без .git>`; проверить exit code != 0 + stderr содержит "REPO_DIR not found"
  - `test_claude_runtime_module_importable_post_migration` — subprocess `python -c "from ouroboros.claude_runtime import _CLAUDE_SDK_BASELINE; ..."` или прямой import + assertions
  - `test_skill_loader_ensure_data_skills_seeded_post_migration` — invoke `ensure_data_skills_seeded()` против tmp data dir, verify seeding output (file count, marker file)
- Имена тестов должны точно соответствовать сценариям выше (для check_test_layers.py fuzzy grep)
- Использовать pytest fixtures для port allocation (free TCP port via socket bind)

### 14. Write E2E smoke tests
- **Task ID**: e2e-tests
- **Depends On**: extract-claude-runtime, extract-skill-seeding, add-server-hook, delete-desktop-artifacts
- **Assigned To**: test-builder-e2e
- **Agent Type**: builder
- **Stack**: python pytest playwright e2e ui_browser browser
- **Parallel**: false
- Создать `tests/test_smoke_e2e_post_refactor.py` с маркером `@pytest.mark.ui_browser` и одним тестом:
  - `test_health_endpoint_loads_in_browser`
- Использовать `from playwright.sync_api import sync_playwright` или существующий fixture pattern из `tests/test_ui_smoke_playwright.py`
- Сценарий: запустить subprocess server.py → poll /health через httpx до ready → Playwright Chromium navigate to http://127.0.0.1:<port>/health → assert response.status == 200 + textContent contains "ok"
- Cleanup: kill subprocess в teardown

### 15. Final validation
- **Task ID**: validate-all
- **Depends On**: extract-claude-runtime, extract-skill-seeding, rewire-imports, add-server-hook, delete-desktop-artifacts, trim-platform-layer, trim-config, refactor-runtime-mode-tests, update-ci, update-pyproject, update-docs, unit-tests, integration-tests, e2e-tests
- **Assigned To**: validator-final
- **Agent Type**: validator
- **Stack**: python pytest unit integration e2e python-patterns layout
- **Parallel**: false
- Проверить, что все удалённые файлы действительно отсутствуют (см. Acceptance Criteria § 1)
- Запустить `make test` — все unit-тесты должны проходить
- Запустить `python3 -m pytest tests/test_claude_runtime.py tests/test_skill_loader.py tests/test_server_startup_hook.py -q` — новые unit-тесты проходят
- Запустить `python3 -m pytest tests/test_post_refactor_integration.py -q -m "not browser"` — все 4 integration сценария проходят и runner output содержит "4 passed"
- Запустить `python3 -m pytest tests/test_smoke_e2e_post_refactor.py -m ui_browser -q` — E2E smoke проходит
- Запустить `ruff check .` — no errors
- Запустить `docker build -t ouroboros-web:validate .` — image билдится
- Запустить `docker run --rm -d -p 8765:8765 --name ouroboros-validate ouroboros-web:validate` + `curl http://127.0.0.1:8765/health` → 200 OK; затем `docker stop ouroboros-validate`
- Прогнать `grep -rn "launcher\|PyInstaller\|repo\.bundle\|build_repo_bundle\|python-standalone\|Ouroboros\.spec" --include="*.py" --include="*.yml" --include="*.toml" --include="*.sh" --exclude-dir=specs --exclude-dir=.git --exclude-dir=node_modules .` → результат должен быть пустой (или только в archived test comments). Plan-файл (specs/) и CHANGELOG исключаются явно через `--exclude-dir`.
- Run `check_test_layers.py` post-build hook (если интегрирован) — verify все 3 layer scenarios actually executed
- Final report: total LOC removed, files removed count, files modified count, all tests pass status

## Acceptance Criteria

1. **Удалённые файлы отсутствуют:**
   - `launcher.py`, `ouroboros/launcher_bootstrap.py`, `Ouroboros.spec`, `build.sh`, `build_linux.sh`, `build_windows.ps1`, `requirements-launcher.txt`, `entitlements.plist`, `scripts/download_python_standalone.sh`, `scripts/download_python_standalone.ps1`, `scripts/build_repo_bundle.py`, `scripts/pyi_rth_pythonnet.py`, `assets/icon.icns`, `assets/icon.ico`
   - `tests/test_packaging_sync.py`, `tests/test_launcher_sync.py`, `tests/test_packaging_assets.py`, `tests/test_build_repo_bundle.py`, `tests/test_build_scripts.py`

2. **Новые файлы существуют и importable:**
   - `ouroboros/claude_runtime.py` — экспортирует `_CLAUDE_SDK_BASELINE`, `_CLAUDE_SDK_MIN_VERSION`, `_version_tuple`, `verify_claude_runtime`, `ClaudeRuntimeContext`
   - `tests/test_claude_runtime.py`, `tests/test_server_startup_hook.py`, `tests/test_post_refactor_integration.py`, `tests/test_smoke_e2e_post_refactor.py`

3. **`ouroboros/skill_loader.py` расширен** функциями `ensure_data_skills_seeded`, `_per_skill_version_resync`, `_read_skill_manifest_version`, `_record_skill_upgrade_migration`, `_reseed_native_skill_in_place`, `_seed_skills_into`, `cleanup_orphaned_seed_markers`

4. **`server.py::main()` вызывает `ensure_repo_present()`** ДО запуска uvicorn / supervisor

5. **`ensure_repo_present()` fail-fast semantics:** при отсутствии REPO_DIR — `SystemExit` с сообщением, упоминающим путь и инструкцию (docker/k8s + run-from-source варианты)

6. **`platform_layer.py` очищен** от launcher-only символов: нет definitions для `assign_pid_to_job`, `close_job`, `create_kill_on_close_job`, `terminate_job`, `resume_process`, `embedded_python_candidates`, `embedded_pip`, `git_install_hint`, `open_path_external` (если последний не используется elsewhere)

7. **`config.py` очищен** от launcher-only: нет `PANIC_EXIT_CODE`, `RESTART_EXIT_CODE`, `acquire_pid_lock`, `release_pid_lock`. **`PORT_FILE` сохранён.**

8. **CI workflow** не содержит jobs `build`, `release-preflight`, `release`. `paths:` фильтры не упоминают удалённые файлы.

9. **`pyproject.toml`** не содержит `[project.optional-dependencies] build`, не содержит `portable_detail`/`ui_browser_docker` markers

10. **`README.md`** не содержит инструкций по download .dmg/.exe; содержит quick-start для `python server.py` + `docker run`

11. **Все тесты проходят:**
    - `make test` — exit 0
    - `python3 -m pytest tests/test_post_refactor_integration.py -q -m "not browser"` — 4 passed
    - `python3 -m pytest tests/test_smoke_e2e_post_refactor.py -m ui_browser -q` — 1 passed
    - `ruff check .` — exit 0

12. **Docker image билдится и работает:**
    - `docker build -t ouroboros-web:test .` — exit 0
    - Container starts, /health returns 200

13. **Никаких active references** на удалённый код: `grep -rn "from ouroboros.launcher_bootstrap\|^import launcher\b\|repo\.bundle\|python-standalone\|Ouroboros\.spec" --include="*.py" --include="*.toml" --include="*.yml" --include="*.sh" --exclude-dir=specs --exclude-dir=.git .` → empty

## Validation Commands

Execute these commands to validate the task is complete:

```bash
# 1. Удалённые файлы отсутствуют (должно вернуть пустой список)
ls launcher.py ouroboros/launcher_bootstrap.py Ouroboros.spec build.sh build_linux.sh build_windows.ps1 requirements-launcher.txt entitlements.plist 2>&1 | grep -v "No such" || echo "OK: all files removed"

# 2. Новые модули существуют и importable
python3 -c "from ouroboros.claude_runtime import _CLAUDE_SDK_BASELINE, verify_claude_runtime, ClaudeRuntimeContext; print('OK')"
python3 -c "from ouroboros.skill_loader import ensure_data_skills_seeded, _per_skill_version_resync; print('OK')"
python3 -c "from server import ensure_repo_present; print('OK')"

# 3. Lint проходит
ruff check .

# 4. Unit-тесты
python3 -m pytest tests/test_claude_runtime.py tests/test_server_startup_hook.py -q --tb=short

# 5. Skill loader тесты
python3 -m pytest tests/test_skill_loader.py tests/test_marketplace_skill_loader_migration.py tests/test_per_skill_version_resync.py -q --tb=short

# 6. Refactored runtime mode тесты
python3 -m pytest tests/test_runtime_mode_elevation.py -q --tb=short

# 7. Claude code gateway (после rewire)
python3 -m pytest tests/test_claude_code_gateway.py -q --tb=short

# 8. Integration scenarios (mandatory layer)
python3 -m pytest tests/test_post_refactor_integration.py -q --tb=short -m "not browser"

# 9. E2E smoke
python3 -m pytest tests/test_smoke_e2e_post_refactor.py -m ui_browser -q --tb=short

# 10. Полный smoke прогон
make test

# 11. Никаких active references (исключая plan-файл в specs/)
test -z "$(grep -rn 'from ouroboros.launcher_bootstrap' --include='*.py' --exclude-dir=specs --exclude-dir=.git .)" && echo "OK: no launcher_bootstrap imports"
test -z "$(grep -rn '^import launcher\b\|^from launcher import' --include='*.py' --exclude-dir=specs --exclude-dir=.git .)" && echo "OK: no launcher imports"

# 12. Docker build smoke
docker build -t ouroboros-web:validate .
docker run --rm -d -p 8765:8765 --name ouroboros-validate ouroboros-web:validate
sleep 5 && curl -fsS http://127.0.0.1:8765/health && echo "OK: health 200"
docker stop ouroboros-validate

# 13. pyproject валиден
python3 -c "import tomllib; cfg = tomllib.load(open('pyproject.toml','rb')); assert 'build' not in cfg['project']['optional-dependencies'], 'build extra still present'; print('OK')"

# 14. CI workflow не содержит build/release jobs
test -z "$(grep -E '^\s+(build|release-preflight|release):' .github/workflows/ci.yml)" && echo "OK: CI cleaned"
```

## Notes

- **Порядок ключевой:** Phase 1 (extract) ДО Phase 2 (rewire) ДО Phase 3-5 (delete/trim). Inversion порядка ломает existing imports.
- **`open_path_external`** в `platform_layer.py` — нужен grep на использование вне launcher.py перед удалением. Если только launcher.py — DELETE; иначе KEEP.
- **`server.py:84-85`** определяет exit codes (`RESTART_EXIT_CODE = 42`, `PANIC_EXIT_CODE = 99`) **локально** — это валидная семантика для systemd `Restart=on-failure` + docker `--restart=on-failure:N` policy. Не удаляем.
- **`PORT_FILE`** в `config.py` остаётся: используется server.py + extension_loader.py (3 теста).
- **No new dependencies** — все нужные (`pytest`, `pytest-mock`, `httpx`, `playwright`) уже в `pyproject.toml`.
- **Backwards compat:** plan не сохраняет backwards compat для удалённых файлов — это intentional breaking change для сборки. Run-from-source и docker workflow не затронуты.
- **OpenSpec:** проект не имеет инициализированного `openspec/` directory (есть только CLI installed). Step 13 OpenSpec Propose будет skipped.
- **Memory:** перед стартом плана записан memory entry `project_data_layout_migration.md` про отложенную миграцию `~/Ouroboros/` → cluster-friendly путь (отдельный change, не в этом плане).
- **k8s spec не трогаем** — `specs/k8s-deployment-readiness.md` и `specs/k8s-corporate-analysis.md` остаются в проекте как отдельные документы будущих изменений.
- **Time estimate:** Phase 1-2 (~30 min builder), Phase 3-5 (~45 min builder), Phase 6-8 (~30 min builder), tests writing (~45 min), validation (~15 min). Total ~3h sequential, ~2h with parallelism.
- **Migration mitigation для существующих PyInstaller-bundle users.** В Phase 11 (update-docs) добавить параграф в `README.md` (раздел "Upgrade from desktop bundle") и в CHANGELOG / release notes:
  > Версии до X.Y устанавливались как .dmg/.exe/.tar.gz и содержали launcher, который автоматически разворачивал репо в `~/Ouroboros/repo/`. Начиная с этой версии desktop bundle pipeline удалён. Если у вас был ранее установлен Ouroboros через bundle и вы хотите продолжить использовать **тот же checkout**: запускайте `OUROBOROS_REPO_DIR=~/Ouroboros/repo python /path/to/server.py` или `cd ~/Ouroboros/repo && python server.py`. Альтернатива — git clone с нуля.
- **REPO_DIR latent bug.** В коде существует расхождение: `server.py:53` определяет `REPO_DIR` локально с дефолтом `pathlib.Path(__file__).parent`, а `ouroboros/config.py:27` имеет своё `REPO_DIR` с дефолтом `APP_ROOT/"repo"`. Оба honor `OUROBOROS_REPO_DIR` env var, но без launcher (который раньше явно устанавливал env) дефолты расходятся. Этот плана **не reconciliation'ит** — фиксируется в memory как pending follow-up; `ensure_repo_present()` использует server-local значение (правильное для run-from-source и docker). Reconciliation = отдельный change request.
