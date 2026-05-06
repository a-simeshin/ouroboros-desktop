# Plan: K8s Deployment Readiness — WebUI-Only Toggle, Tools Whitelist, Git Auto-Sync

## Task Description

Подготовить ouroboros-desktop к развёртыванию в Kubernetes как stateless-сервис с PVC под репозиторий. Три независимых, но связанных по validation-циклу изменения:

1. **Telegram off toggle** — добавить config-driven флаг `WEBUI_ONLY` (default `false` для backwards-compat). Когда `true`: TG-секцию в Settings UI скрыть, мост в `supervisor/message_bus.py::TelegramBridge.configure_from_settings` не активировать (early return до `_restart_telegram_polling`), любые входящие сообщения с непустым `telegram_chat_id` логировать как warning и обрабатывать как `chat_id=0`. Ничего не удалять — переключаемо одним параметром.
2. **Tools whitelist** — переменная `OUROBOROS_TOOLS_ENABLED=git,shell,core,...` фильтрует доступные тулы в `Registry`. Если переменная пустая → текущее поведение (все автодискаверенные тулы). Если задана → только перечисленные плюс защищённые core-тулы.
3. **Git remote auto-sync** — заменить GitHub-only `configure_remote(repo_slug, token)` на generic `configure_remote_url(remote_url, username, password)` для любого HTTPS git-сервера (GitHub, GitLab, Gitea, Bitbucket Server, любой private). Backward-compat: текущий `configure_remote(slug, token)` сохраняется как тонкая обёртка над `configure_remote_url`. Добавить `bootstrap_remote_sync()` (pull на старте), shutdown-хук через FastAPI lifespan/`on_event("shutdown")` с push при graceful shutdown, реальный retry с экспоненциальным backoff в `push_to_remote()`.

## Objective

После применения плана:
- WebUI запускается без Telegram-зависимостей при `WEBUI_ONLY=true`, при этом Telegram-функциональность полностью сохранена и активируется одним флагом для desktop-сценария.
- Operator может ограничить поверхность тулов через env (нужно для безопасного k8s-деплоя).
- При старте сервера (после `ensure_repo_present()`) агент делает `git fetch origin && git pull --ff-only`. При graceful shutdown через FastAPI lifespan (или SIGTERM) делает `commit "shutdown sync"` (если dirty) + `git push` с retry. Push-фейлы ретраятся до 3 попыток с backoff между попытками `[1s, 2s]` (формула `backoff_base ** attempt`, sleep после attempt 0 и 1, финальный attempt 2 без sleep — total wall-clock retry budget 3s). Поддержан любой HTTPS git URL через `OUROBOROS_GIT_REMOTE_URL` + `OUROBOROS_GIT_USERNAME` + `OUROBOROS_GIT_PASSWORD`.
- Plain `pytest` (без флагов) на машине без Docker по-прежнему зелёный — новые Testcontainers-тесты помечены маркером `docker` и исключены из default run.

## Problem Statement

Текущее состояние (verified via grep):
- `ouroboros/config.py` — константа `SETTINGS_DEFAULTS` (line 41), `apply_settings_to_env` env_keys (line 746). Нет флага `WEBUI_ONLY`, нет `OUROBOROS_GIT_*`, нет `OUROBOROS_TOOLS_ENABLED`.
- `supervisor/message_bus.py` — `TelegramBridge.configure_from_settings` (line 154) → `_restart_telegram_polling` (line 197). Это **реальная точка старта Telegram-моста**, вызывается из `server.py:1261` `get_bridge().configure_from_settings(current)`.
- `server.py` — `telegram_chat_id` обрабатывается в owner-message loop (line 457, 485) — это **внутренняя очередь сообщений**, не HTTP-handler. HTTP 400 здесь невозможен; правильное действие — игнорировать поле + warning лог.
- `ouroboros/tools/registry.py` — `CORE_TOOL_NAMES` (line 318), `_FROZEN_TOOL_MODULES` (line 348), `ToolRegistry._load_modules()` (line 358), `available_tools()` (line 393). В frozen-build 21 модуль, в non-frozen — все автодискаверенные.
- `supervisor/git_ops.py` — `ensure_repo_present()` (line 333), `_create_rescue_snapshot()` (line 462), `checkout_and_reset()` (line 523), `configure_remote(slug, token)` (line 981), `push_to_remote()` (line 1022), `migrate_remote_credentials()` (line 1042-1066, GitHub-specific).
- `server.py` — `git_ops_module.ensure_repo_present()` вызывается из `:633`. `configure_remote+migrate_remote_credentials` зовутся из `:1267-1273` (post-settings-change). Uvicorn запускается через `uvicorn.Server(config)` (line 1818) — устанавливает свой SIGTERM/SIGINT handler.
- `pyproject.toml` — `[tool.pytest.ini_options]` имеет marker `integration` ("requires real provider API keys"), `addopts = "-q --tb=short -m 'not integration'"`. Testcontainers НЕ установлен.
- Существующие тесты `configure_remote` контракта: `tests/test_commit_gate.py::test_configure_remote_uses_clean_url`, `::test_migrate_remote_credentials_uses_configure_remote`, `tests/test_git_ops_recovery.py::test_configure_remote_adds_origin_even_when_managed_remote_exists`, `tests/test_phase7_pipeline.py::test_uses_configure_remote`. Backwards-compat shim обязан сохранить их green.

Боль:
- В k8s pod может быть evicted в любой момент → агент потерял изменения.
- Без auto-pull другие копии (или CI) расходятся с локальной репликой.
- GitHub-lock мешает развернуть на private GitLab/Gitea/Bitbucket.
- Telegram-секция мозолит глаза в headless-сценарии и теоретически может стартовать мост при наличии токена в env.

## Solution Approach

**Принцип**: переключаемые флаги, не удаление. Все изменения обратимы.

**Telegram toggle** — гейт **в реальной точке старта**:
- Новое поле в `SETTINGS_DEFAULTS`: `"WEBUI_ONLY": False`. Регистрация в `apply_settings_to_env` env_keys (line 746).
- В `supervisor/message_bus.py::TelegramBridge.configure_from_settings` (line 154): early return ДО вызова `_restart_telegram_polling()` если `settings.get("WEBUI_ONLY") is True`. Лог `[WEBUI_ONLY] Telegram bridge disabled by config`.
- В `server.py:457` (owner-message loop): если `WEBUI_ONLY=True` и `telegram_chat_id != 0` → перезаписать `telegram_chat_id = 0` и logger.warning (`[WEBUI_ONLY] Ignoring telegram_chat_id={value} from incoming message`). Это безопасно — фактически сообщение проходит как Web.
- В `web/modules/settings_ui.js`: обернуть TG-секцию `if (!state.settings.WEBUI_ONLY) { renderTelegramSection(...) }`. Найти секцию по якорю DOM-элемента (телеграм input field id), не по line range.
- В `web/modules/settings.js`: TG-поля в settings-DTO рендерить только если `!settings.WEBUI_ONLY`.
- В `ouroboros/onboarding_wizard.py`: TG-страницу wizard'а пропускать если `WEBUI_ONLY=True`.

**Tools whitelist**:
- В `ToolRegistry.__init__` после `self._load_modules()` прочитать `os.environ.get("OUROBOROS_TOOLS_ENABLED", "")`, распарсить через `[s.strip() for s in raw.split(",") if s.strip()]`.
- Если whitelist непустой: фильтровать `self._tools` оставив только `name in whitelist OR name in CORE_TOOL_NAMES OR name in ("list_available_tools", "enable_tools")`.
- Лог `[Registry] Whitelist active: N of M tools enabled (...)` через `logging.getLogger(__name__).info(...)`. Если whitelist пустой → текущее поведение, логи нет.
- AC формулировка: "все автодискаверенные тулы доступны при пустом whitelist" (а не "30 тулов" — frozen-build 21).

**Git remote generic + auto-sync**:
- Новые env (через `SETTINGS_DEFAULTS` + `apply_settings_to_env` env_keys list line 746): `OUROBOROS_GIT_REMOTE_URL`, `OUROBOROS_GIT_USERNAME`, `OUROBOROS_GIT_PASSWORD`.
- Новая функция `configure_remote_url(remote_url: str, username: str, password: str) -> Tuple[bool, str]` в `supervisor/git_ops.py`. URL-encode username/password через `urllib.parse.quote(safe="")`. Запись в `.git/credentials` строки `https://USER:PASS@HOST/PATH`. `git config credential.helper store`. `git remote set-url origin <remote_url>`. Чувствительные значения **не логировать** (только URL без auth).
- Backwards-compat shim: `configure_remote(repo_slug, token)` строит URL `https://github.com/{slug}.git` и зовёт `configure_remote_url(url, "x-access-token", token)`. Сигнатура и `(bool, str)` контракт сохранены — существующие тесты `test_commit_gate.py::test_configure_remote_uses_clean_url`, `test_git_ops_recovery.py::test_configure_remote_adds_origin_even_when_managed_remote_exists` остаются green.
- `migrate_remote_credentials()` (line 1042+, GitHub-specific) **не трогаем** — новый generic путь использует `configure_remote_url` напрямую и обходит миграцию (она не нужна для свежей конфигурации generic URL).
- Новый хелпер `resolve_remote_config(settings) -> Optional[Tuple[url, user, pass]]`: приоритет `OUROBOROS_GIT_REMOTE_URL` > `GITHUB_TOKEN+GITHUB_REPO`. None если ничего не задано.
- Новый модуль `ouroboros/git_sync.py`:
  - `bootstrap_remote_sync(settings) -> Tuple[str, str]`: если remote не задан → `("skipped", "no remote configured")`. Иначе `configure_remote_url(...)` → `_acquire_git_lock` → `git fetch origin BRANCH_DEV` → `git pull --ff-only origin BRANCH_DEV`. На non-FF: `_create_rescue_snapshot()` + warning, не падать. На сетевой фейл: warning + skip.
  - `shutdown_push_sync(settings) -> Tuple[str, str]`: `_acquire_git_lock(timeout=30)`. На lock-таймаут — log error + return. `git status --porcelain` → если dirty: `git add -A && git commit -m "shutdown sync"`. `push_to_remote(retries=3, backoff_base=2.0)`. Возврат итогового статуса.
  - `register_shutdown_handler(app)`: использует **FastAPI/Starlette `app.add_event_handler("shutdown", coro)`** — chains правильно с uvicorn graceful shutdown, не конкурирует с его SIGTERM handler. Внутри coro: `threading.Timer` на 25-секундный таймаут вокруг `shutdown_push_sync` (НЕ `signal.alarm` — Windows-несовместимо).
- В `server.py:633`: после `git_ops_module.ensure_repo_present()` вызвать `bootstrap_remote_sync(load_settings())`. Логировать результат.
- В `server.py` ранее `uvicorn.Server(config)` (line 1818): зарегистрировать shutdown handler через `app.add_event_handler("shutdown", _shutdown_push_coro)` где `_shutdown_push_coro` — async wrapper над `shutdown_push_sync`.
- `push_to_remote(branch=None, push_tags=True, retries=3, backoff_base=2.0)`:
  - Цикл `for attempt in range(retries): result = subprocess.run(...); if result.returncode == 0: return (True, "..."); if attempt < retries-1: time.sleep(backoff_base ** attempt)`.
  - Sleep series при `backoff_base=2.0`: attempt 0 → 1s, attempt 1 → 2s. (После attempt 2 уже return False без sleep.) Total wall-clock budget: 3s между retries.
  - **Не ретраить non-FF** (parsing stderr на `! [rejected]` / `non-fast-forward` / `fetch first`) — это требует rescue snapshot, не retry.

**Pytest marker для Docker-зависимых тестов**:
- В `pyproject.toml` добавить marker `docker: requires Docker daemon (Testcontainers integration tests)`.
- Изменить `addopts` на `-q --tb=short -m 'not integration and not docker'` — plain `pytest` остаётся зелёным на машинах без Docker.
- Все Testcontainers-тесты помечать `@pytest.mark.docker` плюс `@pytest.mark.skipif(not _docker_available(), reason="docker daemon unavailable")`.
- Runner для integration: `pytest -m docker tests/integration/ -v --tb=short`.

## Relevant Files

Use these files to complete the task:

**Config & settings**:
- `ouroboros/config.py` — `SETTINGS_DEFAULTS` (line 41), `apply_settings_to_env` env_keys (line 746).
- `ouroboros/onboarding_wizard.py` — wizard секция Telegram (TG-страницу пропустить когда `WEBUI_ONLY=true`).

**Telegram toggle**:
- `supervisor/message_bus.py` — `TelegramBridge.configure_from_settings` (line 154), `_restart_telegram_polling` (line 197). **Реальная точка старта моста.**
- `server.py` — owner-message loop (line 457, 485): обработка `telegram_chat_id` при `WEBUI_ONLY=true`. Call site `get_bridge().configure_from_settings(current)` (line 1261).
- `web/modules/settings_ui.js` — TG-секция UI.
- `web/modules/settings.js` — TG-поля settings-DTO.
- `web/modules/chat.js` — обработка `source=='telegram'` (минимальная правка для UI consistency, опционально).

**Tools whitelist**:
- `ouroboros/tools/registry.py` — `CORE_TOOL_NAMES` (line 318), `_FROZEN_TOOL_MODULES` (line 348), `ToolRegistry.__init__`, `_load_modules` (line 358), `available_tools` (line 393).

**Git sync**:
- `supervisor/git_ops.py` — `ensure_repo_present` (line 333), `_create_rescue_snapshot` (line 462), `checkout_and_reset` (line 523), `configure_remote` (line 981), `push_to_remote` (line 1022), `migrate_remote_credentials` (line 1042+, не трогаем).
- `ouroboros/launcher_bootstrap.py` — `_mark_bootstrap_pin_pending`, init helpers (без правок, контекстный файл).
- `ouroboros/tools/git.py` — `_auto_push` (line 558), `_acquire_git_lock` (line 596), `_pull_from_remote` (line 1336).
- `server.py` — `git_ops_module.ensure_repo_present()` call site (line 633), `uvicorn.Server` startup (line 1818), `configure_remote+migrate_remote_credentials` post-settings-change (line 1267-1273).

**Tests** (existing patterns + contracts to preserve):
- `tests/conftest.py` — fixture skeleton.
- `tests/test_chat_js_contracts.py` — pattern для контракт-тестов UI.
- `tests/test_commit_gate.py` — `test_configure_remote_uses_clean_url` (line 277), `test_migrate_remote_credentials_uses_configure_remote` (line 412). **Должны остаться green** после shim.
- `tests/test_git_ops_recovery.py` — `test_configure_remote_adds_origin_even_when_managed_remote_exists` (line 288). **Должен остаться green**.
- `tests/test_phase7_pipeline.py` — `test_uses_configure_remote` (line 776). **Должен остаться green**.
- `pyproject.toml` — `[tool.pytest.ini_options]` markers + addopts (line 60-78).

### New Files

- `ouroboros/git_sync.py` — `bootstrap_remote_sync()`, `shutdown_push_sync()`, `register_shutdown_handler(app)`, `resolve_remote_config(settings)`.
- `tests/test_webui_only_toggle.py` — unit тесты Telegram-gate (mock `TelegramBridge`).
- `tests/test_tools_whitelist.py` — unit тесты Registry whitelist.
- `tests/test_git_sync_unit.py` — unit тесты `configure_remote_url`, retry logic, shutdown handler, bootstrap_remote_sync.
- `tests/integration/__init__.py` — integration test package.
- `tests/integration/conftest.py` — Testcontainers gitea fixture с `INSTALL_LOCK=true` + admin user через `docker exec`.
- `tests/integration/test_git_sync_integration.py` — Testcontainers integration tests (помечены `@pytest.mark.docker`).

## Implementation Phases

### Phase 1: Foundation
- Расширить `SETTINGS_DEFAULTS` (WEBUI_ONLY + git generic + tools whitelist).
- Зарегистрировать новые ключи в `apply_settings_to_env` env_keys (line 746).
- Создать заготовку `ouroboros/git_sync.py` (пустые функции с `NotImplementedError`).
- Подготовить `tests/integration/` директорию + conftest скелет.
- Добавить marker `docker` в `pyproject.toml`, обновить `addopts`.
- Добавить dev-зависимость `testcontainers>=4.0` + `pytest-mock` в `[project.optional-dependencies].test`.

### Phase 2: Core Implementation
- **Параллельно** (3 трека независимы):
  - Track A: Telegram toggle gate в `supervisor/message_bus.py:configure_from_settings` + UI-скрытие.
  - Track B: Tools whitelist (`Registry.__init__` фильтр + лог).
  - Track C: Git sync (configure_remote_url + bootstrap_remote_sync + shutdown handler через `app.add_event_handler` + retry + `resolve_remote_config`).

### Phase 3: Integration & Polish
- Написать unit-тесты per-layer.
- Поднять Testcontainers gitea с правильным admin-bootstrap (`INSTALL_LOCK=true` + `docker exec gitea admin user create`).
- Написать integration-тесты для git-sync.
- Запустить `validate-all`: pytest unit + pytest -m docker integration + `check_test_layers.py` + контрактные регрессии.

## Team Orchestration

- Operator как team lead: оркестрирует через `Task*` тулы и `Agent`.
- Никакого прямого редактирования кода team lead — всё через builder агентов.

### Team Members

- Builder
  - Name: builder-config
  - Role: Phase 1 + Phase 2 Track A — foundation (config + git_sync скелет + pyproject) и Telegram toggle (configure_from_settings gate, owner-loop normalization, UI скрытие)
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: builder-tools
  - Role: Phase 2 Track B — Tools whitelist filter в Registry с защитой CORE_TOOL_NAMES
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: builder-git
  - Role: Phase 2 Track C — Git sync (configure_remote_url + backwards-compat shim, push retry, bootstrap_remote_sync, shutdown handler через app.add_event_handler, resolve_remote_config)
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: builder-unit-tests
  - Role: Phase 3 — unit тесты для всех трёх трека (15 сценариев)
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: builder-integration-tests
  - Role: Phase 3 — Testcontainers gitea conftest (INSTALL_LOCK + docker exec admin user create) + integration-тесты git-sync (5 сценариев)
  - Agent Type: builder
  - Resume: true

- Validator
  - Name: validator-final
  - Role: Запуск всех runner commands (unit + integration), верификация acceptance criteria, регрессионная проверка существующих 60+ тестов
  - Agent Type: validator
  - Resume: false

## Testing Strategy

Test pyramid ratio: **80% unit / 15% integration-API / 5% UI e2e**

### Unit Tests (80%)

**Telegram toggle**:
- `tests/test_webui_only_toggle.py::test_webui_only_default_false` — default не ломает Desktop сценарий.
- `tests/test_webui_only_toggle.py::test_telegram_bridge_skipped_when_webui_only` — mock `TelegramBridge`: при `WEBUI_ONLY=true` → `_restart_telegram_polling` НЕ вызвана.
- `tests/test_webui_only_toggle.py::test_telegram_chat_id_normalized_to_zero_when_webui_only` — owner-loop переписывает `telegram_chat_id != 0` в `0` + warning.

**Tools whitelist**:
- `tests/test_tools_whitelist.py::test_no_whitelist_loads_all_tools` — без env переменной все автодискаверенные тулы доступны.
- `tests/test_tools_whitelist.py::test_whitelist_filters_to_subset` — `OUROBOROS_TOOLS_ENABLED="git,shell"` оставляет 2 + core-protected.
- `tests/test_tools_whitelist.py::test_whitelist_protects_core` — `core` всегда в `available_tools()` независимо от whitelist.
- `tests/test_tools_whitelist.py::test_whitelist_logs_active_filter` — лог содержит "Whitelist active".

**Git sync**:
- `tests/test_git_sync_unit.py::test_configure_remote_url_writes_credentials` — формирует `.git/credentials` с url-encoded username/password.
- `tests/test_git_sync_unit.py::test_configure_remote_backwards_compat_github` — старый `configure_remote(slug, token)` всё ещё работает через shim (delegates to configure_remote_url).
- `tests/test_git_sync_unit.py::test_configure_remote_url_does_not_log_password` — capsys: пароль не появляется в log output.
- `tests/test_git_sync_unit.py::test_push_to_remote_retries_on_failure` — 2 фейла + 1 успех = 3 попытки, общий результат `True`. `mocker.patch('time.sleep')` фиксирует sleep series [1, 2].
- `tests/test_git_sync_unit.py::test_push_to_remote_gives_up_after_retries` — все retries фейлятся → `False, "push failed after 3 retries"`.
- `tests/test_git_sync_unit.py::test_push_to_remote_does_not_retry_on_non_ff` — stderr содержит `! [rejected] non-fast-forward` → 1 попытка, не retry.
- `tests/test_git_sync_unit.py::test_shutdown_push_sync_commits_dirty` — handler делает commit + push при dirty repo.
- `tests/test_git_sync_unit.py::test_shutdown_push_sync_skips_clean` — handler ничего не делает при clean repo.
- `tests/test_git_sync_unit.py::test_bootstrap_remote_sync_skipped_no_remote` — если remote URL не задан → no-op `("skipped", ...)`.
- `tests/test_git_sync_unit.py::test_bootstrap_remote_sync_non_ff_creates_rescue` — non-FF pull → `_create_rescue_snapshot` вызвана, не падает.
- `tests/test_git_sync_unit.py::test_resolve_remote_config_prefers_generic_url` — оба источника заданы → выбран `OUROBOROS_GIT_REMOTE_URL`.
- `tests/test_git_sync_unit.py::test_resolve_remote_config_falls_back_to_github` — только `GITHUB_TOKEN+GITHUB_REPO` → собирает GitHub URL.

**Backwards-compat regression** (запускаются как часть unit suite, но НЕ переписываются — проверка что shim сохранил контракт):
- `tests/test_commit_gate.py::test_configure_remote_uses_clean_url` (existing).
- `tests/test_commit_gate.py::test_migrate_remote_credentials_uses_configure_remote` (existing).
- `tests/test_git_ops_recovery.py::test_configure_remote_adds_origin_even_when_managed_remote_exists` (existing).
- `tests/test_phase7_pipeline.py::test_uses_configure_remote` (existing).

### Integration / API Tests (15%)

Используем Testcontainers + gitea. Бутстрап через `INSTALL_LOCK=true` + `docker exec <container> gitea admin user create --admin --username testuser --password testpass --email t@t.local`. Образ pinned `gitea/gitea:1.21`:

- `tests/integration/test_git_sync_integration.py::test_bootstrap_pull_from_gitea` — поднять gitea, создать repo через REST API (с basic auth от admin user), push initial commit с локальной машины через раздельную работающую копию, выполнить `bootstrap_remote_sync(settings)`, проверить что репо подтянулось.
- `tests/integration/test_git_sync_integration.py::test_shutdown_push_to_gitea` — сделать локальный коммит, вызвать `shutdown_push_sync(settings)`, проверить что коммит появился в gitea remote через REST API `GET /api/v1/repos/.../commits`.
- `tests/integration/test_git_sync_integration.py::test_push_retry_on_transient_failure` — gitea приостановить (`docker stop`) → push фейлит → запустить снова в отдельном thread → retry проходит. Тест проверяет что итоговый push success после transient error.
- `tests/integration/test_git_sync_integration.py::test_generic_https_url_authentication` — конфигурация через `OUROBOROS_GIT_REMOTE_URL` + `OUROBOROS_GIT_USERNAME` + `OUROBOROS_GIT_PASSWORD` (без GitHub-специфики): `configure_remote_url` пишет credentials, `git push` против gitea работает.
- `tests/integration/test_git_sync_integration.py::test_full_lifecycle_pull_modify_push` — полный happy-path с замером времени: bootstrap → агент коммитит → `shutdown_push_sync` → push успешен → второй процесс pull → видит изменения. Asserts wall-clock ≤30s (AC bound).

### UI E2E Tests (5%)

E2E layer **Skipped** — обоснование в `### E2E Layer` ниже.

## Test Infrastructure (User-Declared)

### Unit Layer (Python)
- **Files glob:** `tests/test_*.py` (top-level `tests/`, exclusive of `tests/integration/**`)
- **Infra signature (regex, optional for unit):** `n/a`
- **Happy-path scenarios (≥1 named):**
  - `tests/test_webui_only_toggle.py::test_telegram_bridge_skipped_when_webui_only`
  - `tests/test_tools_whitelist.py::test_whitelist_filters_to_subset`
  - `tests/test_git_sync_unit.py::test_push_to_remote_retries_on_failure`
  - `tests/test_git_sync_unit.py::test_shutdown_push_sync_commits_dirty`
  - `tests/test_git_sync_unit.py::test_configure_remote_url_writes_credentials`
- **Runner command:** `pytest tests/test_webui_only_toggle.py tests/test_tools_whitelist.py tests/test_git_sync_unit.py -v`
- **Realism rationale:** Pytest — каноничный test runner проекта (`pyproject.toml [tool.pytest.ini_options]`); все три модуля изолированы (mock subprocess через pytest-mock, mock filesystem через `tmp_path`), бьют только в чистую логику Python. Никаких Docker-зависимостей.

### Integration Layer (Python) — MANDATORY, never Skipped
- **Files glob:** `tests/integration/test_*.py`
- **Infra signature (regex, ≥1 match per file):** `from testcontainers\.|DockerContainer.*gitea|@pytest\.mark\.docker`
- **Happy-path scenarios (≥1 named):**
  - `tests/integration/test_git_sync_integration.py::test_bootstrap_pull_from_gitea`
  - `tests/integration/test_git_sync_integration.py::test_shutdown_push_to_gitea`
  - `tests/integration/test_git_sync_integration.py::test_generic_https_url_authentication`
  - `tests/integration/test_git_sync_integration.py::test_full_lifecycle_pull_modify_push`
- **Runner command:** `pytest -m docker tests/integration/ -v --tb=short`
- **Realism rationale:** Testcontainers + gitea (image `gitea/gitea:1.21`, ~80MB) — настоящий git server в Docker, говорящий по HTTPS как production GitLab/Gitea/private. Самый высокий уровень realism, доступный без внешних зависимостей: проверяем именно тот HTTPS-стек который пойдёт в k8s (credentials через `.git/credentials`, `git fetch/pull/push` через subprocess, реальные сетевые ошибки для retry-логики). Бутстрап через `INSTALL_LOCK=true` env + `docker exec gitea admin user create` — стандартная практика для headless-инициализации Gitea без UI-installer. H2/моки тут не подходят — git protocol handshake невозможно осмысленно мокать. Маркер `@pytest.mark.docker` плюс новый `addopts = "-m 'not integration and not docker'"` гарантируют что plain `pytest` остаётся зелёным на машинах без Docker.

### E2E Layer (Python/Web)
- **Status:** Skipped — no e2e runner exists in repo (no Playwright/Cypress/Selenide installed); UI changes are minimal (toggle visibility of one settings section + одна wizard страница), covered by contract-level tests via `tests/test_chat_js_contracts.py` pattern. Adding Playwright purely for one CSS-display change is disproportionate. Если в будущем добавится Playwright инфра — E2E сценарий: "WebUI loads → settings panel does not show Telegram section when WEBUI_ONLY=true".

## Step by Step Tasks

- IMPORTANT: Execute every step in order, top to bottom. Each task maps directly to a `TaskCreate` call.
- Before you start, run `TaskCreate` to create the initial task list that all team members can see and execute.

### 1. Foundation: расширить config + scaffolding + pyproject marker
- **Task ID**: foundation-scaffolding
- **Depends On**: none
- **Assigned To**: builder-config
- **Agent Type**: builder
- **Stack**: python pyproject pydantic-settings
- **Parallel**: false
- **Tests**: Unit: проверка `config.SETTINGS_DEFAULTS` содержит `WEBUI_ONLY` (default `False`), `OUROBOROS_GIT_REMOTE_URL`/`_USERNAME`/`_PASSWORD` (default `""`), `OUROBOROS_TOOLS_ENABLED` (default `""`). Проверка `apply_settings_to_env` пробрасывает их в os.environ.
- Добавить в `ouroboros/config.py` константу `SETTINGS_DEFAULTS` (line 41): новые ключи `"WEBUI_ONLY": False`, `"OUROBOROS_GIT_REMOTE_URL": ""`, `"OUROBOROS_GIT_USERNAME": ""`, `"OUROBOROS_GIT_PASSWORD": ""`, `"OUROBOROS_TOOLS_ENABLED": ""`.
- Зарегистрировать новые ключи в `apply_settings_to_env` env_keys list (`ouroboros/config.py` line 746) — чтобы они пробрасывались в `os.environ` для subprocess git-вызовов.
- Создать `ouroboros/git_sync.py` со скелетами функций `bootstrap_remote_sync(settings)`, `shutdown_push_sync(settings)`, `register_shutdown_handler(app)`, `resolve_remote_config(settings)` (raise `NotImplementedError` пока).
- Создать `tests/integration/__init__.py` (пустой).
- В `pyproject.toml` `[tool.pytest.ini_options]`:
  - В `markers` добавить `"docker: requires Docker daemon (Testcontainers integration tests)"`.
  - Изменить `addopts = "-q --tb=short -m 'not integration and not docker'"`.
- В `pyproject.toml` `[project.optional-dependencies]` добавить секцию `test = ["testcontainers>=4.0", "pytest-mock>=3.10", "requests"]`. Установить через `uv pip install -e ".[test]"`.

### 2. Phase 2 Track A: Telegram toggle gate в message_bus + UI скрытие
- **Task ID**: telegram-toggle
- **Depends On**: foundation-scaffolding
- **Assigned To**: builder-config
- **Agent Type**: builder
- **Stack**: python config FastAPI startup
- **Parallel**: true
- **Tests**: Unit: `tests/test_webui_only_toggle.py` — 3 сценария (default false, bridge skip, chat_id normalize). Integration: smoke в `test_full_lifecycle_pull_modify_push` (стартует с `WEBUI_ONLY=true` и не зовёт TelegramBridge).
- В `supervisor/message_bus.py::TelegramBridge.configure_from_settings` (line 154): early return ДО блока `if ... self._restart_telegram_polling()` (line 170) если `bool(settings.get("WEBUI_ONLY"))`. Лог `[WEBUI_ONLY] Telegram bridge disabled by config`.
- В `server.py` owner-message loop (line 457): после `telegram_chat_id = int(msg.get("telegram_chat_id") or 0)` добавить проверку: если `_settings.get("WEBUI_ONLY")` (через current settings snapshot, доступ через global или re-read `load_settings()`) и `telegram_chat_id != 0` → `logger.warning("[WEBUI_ONLY] Ignoring telegram_chat_id=%d from incoming message", telegram_chat_id); telegram_chat_id = 0`.
- В `web/modules/settings_ui.js`: найти TG-секцию (по якорю input id `telegram-bot-token` или аналогичному), обернуть рендер `if (!state.settings.WEBUI_ONLY) { ... }`.
- В `web/modules/settings.js`: TG-поля DTO рендерить только если `!settings.WEBUI_ONLY`.
- В `ouroboros/onboarding_wizard.py`: TG-страницу wizard'а пропускать если `WEBUI_ONLY=True` (искать секцию по строке "telegram" или "TELEGRAM_BOT_TOKEN" внутри wizard steps).
- НЕ изменять `server.py:1267-1273` (configure_remote+migrate) — Track C.

### 3. Phase 2 Track B: Tools whitelist filter в Registry
- **Task ID**: tools-whitelist
- **Depends On**: foundation-scaffolding
- **Assigned To**: builder-tools
- **Agent Type**: builder
- **Stack**: python config registry
- **Parallel**: true
- **Tests**: Unit: `tests/test_tools_whitelist.py` — все 4 unit-сценария (no-whitelist, filter-subset, protect-core, log-active).
- В `ouroboros/tools/registry.py::ToolRegistry.__init__`: после `self._load_modules()` (line 358) прочитать env через `os.environ.get("OUROBOROS_TOOLS_ENABLED", "")`. Распарсить `[s.strip() for s in raw.split(",") if s.strip()]`.
- Если whitelist непустой: фильтровать `self._tools` (или эквивалентную внутреннюю dict) оставив только `name in whitelist OR name in CORE_TOOL_NAMES OR name in ("list_available_tools", "enable_tools")`. `CORE_TOOL_NAMES` импортируется из того же модуля (line 318).
- Лог `[Registry] Whitelist active: N of M tools enabled (...)` через `logging.getLogger(__name__).info(...)` ровно один раз при init.
- Если whitelist пустой → текущее поведение (всё доступно), лога нет.
- НЕ менять `_FROZEN_TOOL_MODULES` (line 348) — фильтр работает после _load_modules независимо от frozen/non-frozen ветви.

### 4. Phase 2 Track C step 1: configure_remote_url + backwards-compat shim + resolve_remote_config
- **Task ID**: git-remote-generic
- **Depends On**: foundation-scaffolding
- **Assigned To**: builder-git
- **Agent Type**: builder
- **Stack**: python git subprocess credentials
- **Parallel**: true
- **Tests**: Unit: `tests/test_git_sync_unit.py::test_configure_remote_url_writes_credentials`, `::test_configure_remote_backwards_compat_github`, `::test_configure_remote_url_does_not_log_password`, `::test_resolve_remote_config_prefers_generic_url`, `::test_resolve_remote_config_falls_back_to_github`. Регрессия: `tests/test_commit_gate.py::test_configure_remote_uses_clean_url`, `::test_migrate_remote_credentials_uses_configure_remote`, `tests/test_git_ops_recovery.py::test_configure_remote_adds_origin_even_when_managed_remote_exists`, `tests/test_phase7_pipeline.py::test_uses_configure_remote` — должны остаться green без изменений.
- В `supervisor/git_ops.py`: добавить функцию `configure_remote_url(remote_url: str, username: str, password: str) -> Tuple[bool, str]`. URL-encode username/password через `urllib.parse.quote(s, safe="")`. Запись в `.git/credentials` строки `f"https://{quoted_user}:{quoted_pass}@{host_path}"` (где `host_path` извлекается из remote_url через `urllib.parse.urlparse`). `git config credential.helper store`. `git remote set-url origin <remote_url>` (или `git remote add origin` если отсутствует). Лог только URL без auth (через `urlparse._replace(netloc=parsed.hostname)`).
- Изменить `configure_remote(repo_slug: str, token: str)` (line 981): теперь делегирует `configure_remote_url(f"https://github.com/{repo_slug}.git", "x-access-token", token)`. Сохранить `(bool, str)` контракт.
- Добавить `resolve_remote_config(settings: dict) -> Optional[Tuple[str, str, str]]` в `ouroboros/git_sync.py`:
  - Если `settings.get("OUROBOROS_GIT_REMOTE_URL")` непустой → return `(remote_url, username or "x-access-token", password or "")`. Username default — `x-access-token` для совместимости с GitHub-style PAT.
  - Иначе если `settings.get("GITHUB_TOKEN")` и `settings.get("GITHUB_REPO")` → return `(f"https://github.com/{repo}.git", "x-access-token", token)`.
  - Иначе `None`.
- НЕ трогать `migrate_remote_credentials()` (line 1042+) — generic путь обходит её, она остаётся для legacy GitHub миграции.

### 5. Phase 2 Track C step 2: push_to_remote retry logic
- **Task ID**: git-push-retry
- **Depends On**: git-remote-generic
- **Assigned To**: builder-git
- **Agent Type**: builder
- **Stack**: python git subprocess retry
- **Parallel**: false
- **Tests**: Unit: `tests/test_git_sync_unit.py::test_push_to_remote_retries_on_failure`, `::test_push_to_remote_gives_up_after_retries`, `::test_push_to_remote_does_not_retry_on_non_ff`. Все mock'ируют `subprocess.run` и `time.sleep` через `mocker`.
- Расширить сигнатуру `push_to_remote(branch=None, push_tags=True, retries=3, backoff_base=2.0)` в `supervisor/git_ops.py` (line 1022).
- Цикл:
  ```python
  for attempt in range(retries):
      result = subprocess.run([...], capture_output=True, text=True)
      if result.returncode == 0:
          return (True, result.stdout.strip())
      stderr = result.stderr or ""
      if any(s in stderr for s in ("! [rejected]", "non-fast-forward", "fetch first")):
          return (False, f"non-fast-forward, manual rescue required: {stderr}")
      if attempt < retries - 1:
          time.sleep(backoff_base ** attempt)
  return (False, f"push failed after {retries} retries: {stderr}")
  ```
- Sleep series при `backoff_base=2.0`, `retries=3`: attempt 0 → sleep 1s, attempt 1 → sleep 2s, attempt 2 → no sleep (final). Total wall-clock retry budget: 3s между попытками.
- Сохранить совместимость с существующими call sites (`_auto_push` в `tools/git.py:558`, `server.py:1267-1273` контекст).

### 6. Phase 2 Track C step 3: bootstrap_remote_sync()
- **Task ID**: git-bootstrap-pull
- **Depends On**: git-remote-generic
- **Assigned To**: builder-git
- **Agent Type**: builder
- **Stack**: python startup git
- **Parallel**: false
- **Tests**: Unit: `tests/test_git_sync_unit.py::test_bootstrap_remote_sync_skipped_no_remote`, `::test_bootstrap_remote_sync_non_ff_creates_rescue`. Integration: `tests/integration/test_git_sync_integration.py::test_bootstrap_pull_from_gitea`.
- Реализовать `bootstrap_remote_sync(settings: dict) -> Tuple[str, str]` в `ouroboros/git_sync.py`:
  - `cfg = resolve_remote_config(settings)`. Если `None` → `return ("skipped", "no remote configured")`.
  - Распаковать `(url, user, password)` → `configure_remote_url(url, user, password)`. Если фейл → log warning, `return ("error", reason)`, не падать.
  - `_acquire_git_lock(timeout=60)` (использовать helper из `ouroboros/tools/git.py:596`). Если lock не взять → log warning, return.
  - `subprocess.run(["git", "fetch", "origin", BRANCH_DEV], ...)`. На фейл → log warning, return.
  - `subprocess.run(["git", "pull", "--ff-only", "origin", BRANCH_DEV], ...)`.
  - На non-FF (returncode != 0 + stderr содержит `non-fast-forward`/`diverged`/`refusing to merge`): вызвать `_create_rescue_snapshot(branch=BRANCH_DEV, reason="bootstrap_pull_non_ff")` из `supervisor/git_ops.py:462` + warning log `[git_sync] non-FF pull, rescue saved`. Не падать. Return `("rescue", "non-ff pull, rescue snapshot created")`.
  - На сетевой/transient фейл: warning + skip. Return `("error", reason)`.
  - На успех: return `("ok", "pulled HEAD: <sha>")`.
- В `server.py` после `git_ops_module.ensure_repo_present()` (line 633) добавить:
  ```python
  from ouroboros.git_sync import bootstrap_remote_sync
  status, reason = bootstrap_remote_sync(load_settings())
  log.info("[git_sync] bootstrap: %s — %s", status, reason)
  ```

### 7. Phase 2 Track C step 4: shutdown push через FastAPI lifespan/on_event
- **Task ID**: git-shutdown-push
- **Depends On**: git-push-retry
- **Assigned To**: builder-git
- **Agent Type**: builder
- **Stack**: python fastapi asyncio git
- **Parallel**: false
- **Tests**: Unit: `tests/test_git_sync_unit.py::test_shutdown_push_sync_commits_dirty`, `::test_shutdown_push_sync_skips_clean`. Integration: `tests/integration/test_git_sync_integration.py::test_shutdown_push_to_gitea`, `::test_full_lifecycle_pull_modify_push`.
- Реализовать `shutdown_push_sync(settings: dict) -> Tuple[str, str]` в `ouroboros/git_sync.py`:
  - `cfg = resolve_remote_config(settings)`. Если `None` → return `("skipped", "no remote configured")`.
  - `_acquire_git_lock(timeout=30)`. Если lock не взять → log error + return `("error", "lock timeout")`.
  - `git status --porcelain` → если dirty: `git add -A && git -c user.email=... -c user.name=... commit -m "shutdown sync"`. Использовать существующие env-vars или fallback `ouroboros@local`.
  - Распаковать `(url, user, password)` → `configure_remote_url(...)` (на случай если remote ещё не сконфигурирован).
  - `push_to_remote(branch=BRANCH_DEV, retries=3, backoff_base=2.0)`.
  - Лог итогового статуса. Return `(status, reason)`.
- Реализовать `register_shutdown_handler(app)` в `ouroboros/git_sync.py`:
  ```python
  def register_shutdown_handler(app):
      async def _shutdown_push_coro():
          import threading
          from ouroboros.config import load_settings
          settings = load_settings()
          done = threading.Event()
          result = {}
          def _run():
              try:
                  result["status"], result["reason"] = shutdown_push_sync(settings)
              finally:
                  done.set()
          t = threading.Thread(target=_run, daemon=True)
          t.start()
          if not done.wait(timeout=25):
              log.warning("[git_sync] shutdown_push timed out after 25s")
              return
          log.info("[git_sync] shutdown_push: %s — %s", result.get("status"), result.get("reason"))
      app.add_event_handler("shutdown", _shutdown_push_coro)
  ```
- В `server.py` найти место создания FastAPI/Starlette `app` (там же где маршруты регистрируются) и добавить `register_shutdown_handler(app)` ПЕРЕД `uvicorn.Server(config)` (line 1818). FastAPI `shutdown` event автоматически срабатывает при graceful shutdown uvicorn — он сам обработает SIGTERM правильно.
- НЕ использовать `signal.signal(SIGTERM, ...)` напрямую — конфликтует с uvicorn's own handler. НЕ использовать `signal.alarm` — Windows-несовместимо.

### 8. Write Unit Tests
- **Task ID**: unit-tests
- **Depends On**: telegram-toggle, tools-whitelist, git-bootstrap-pull, git-shutdown-push, git-push-retry
- **Assigned To**: builder-unit-tests
- **Agent Type**: builder
- **Stack**: python pytest pytest-mock unit
- **Parallel**: true
- Написать `tests/test_webui_only_toggle.py`: 3 сценария. Mock `TelegramBridge` через `mocker.patch('supervisor.message_bus.TelegramBridge._restart_telegram_polling')`. Для chat_id normalization — собрать fake `msg` dict, вызвать обработчик через unit-test (или вынести логику в helper-функцию для testability).
- Написать `tests/test_tools_whitelist.py`: 4 сценария. Использовать `monkeypatch.setenv("OUROBOROS_TOOLS_ENABLED", "...")` + повторное создание `ToolRegistry` (force re-init).
- Написать `tests/test_git_sync_unit.py`: 11 сценариев. Mock `subprocess.run` через `mocker.patch('subprocess.run')` с `side_effect=[CompletedProcess(returncode=1, stderr=b"net err"), CompletedProcess(returncode=0)]`. Mock `time.sleep` через `mocker.patch('time.sleep')`. Использовать `tmp_path` для fake `.git/` структуры. Для `configure_remote_url_does_not_log_password` — capsys assert.
- Все тесты следуют существующему паттерну `tests/test_*.py` (см. `tests/conftest.py`).
- **НЕ переписывать** существующие тесты `tests/test_commit_gate.py`, `tests/test_git_ops_recovery.py`, `tests/test_phase7_pipeline.py` — они должны остаться green как regression-проверка backwards-compat.
- Запустить `pytest tests/test_webui_only_toggle.py tests/test_tools_whitelist.py tests/test_git_sync_unit.py -v` — все должны пройти.
- Запустить `pytest tests/test_commit_gate.py tests/test_git_ops_recovery.py tests/test_phase7_pipeline.py -v` — backwards-compat regression. Все должны остаться green.

### 9. Write Integration Tests — MANDATORY
- **Task ID**: integration-tests
- **Depends On**: telegram-toggle, tools-whitelist, git-bootstrap-pull, git-shutdown-push, git-push-retry
- **Assigned To**: builder-integration-tests
- **Agent Type**: builder
- **Stack**: python pytest testcontainers integration httpx
- **Parallel**: false
- Создать `tests/integration/conftest.py`:
  - `_docker_available()` хелпер: проверка `docker info` через subprocess, return bool.
  - Fixture `gitea_container` (scope=session):
    - `DockerContainer("gitea/gitea:1.21").with_exposed_ports(3000).with_env("INSTALL_LOCK", "true").with_env("USER_UID", "1000").with_env("USER_GID", "1000")`. `start()`.
    - Wait for HTTP `GET /` returns 200 (через `wait_for_logs` или `requests` polling).
    - Создать admin: `docker_client = container.get_docker_client(); container.exec("gitea admin user create --admin --username testuser --password testpass --email t@t.local")` (или эквивалент через `docker exec`).
    - Создать repo через REST API: `requests.post(f"{base_url}/api/v1/user/repos", auth=("testuser","testpass"), json={"name": "test-repo", "auto_init": True})`.
    - Yield dict `{"base_url": "...", "username": "testuser", "password": "testpass", "repo_url": f"{base_url}/testuser/test-repo.git"}`.
    - Cleanup в finalizer.
  - Применить к каждому тесту `pytestmark = [pytest.mark.docker, pytest.mark.skipif(not _docker_available(), reason="docker daemon unavailable")]`.
- Создать `tests/integration/test_git_sync_integration.py` с 5 сценариями из Testing Strategy. Каждый сценарий выставляет `OUROBOROS_GIT_REMOTE_URL` на gitea instance, использует `tmp_path` как локальный repo, вызывает `bootstrap_remote_sync` / `shutdown_push_sync` / `configure_remote_url`.
- `test_full_lifecycle_pull_modify_push` дополнительно меряет `time.monotonic()` start/end и assert `< 30` секунд.
- Запустить `pytest -m docker tests/integration/ -v --tb=short` — все 5 должны пройти. Каждый сценарий ≤30 сек.
- Запустить `pytest` (без флагов) — должен пройти как раньше (новые тесты skip из-за `addopts = "-m 'not integration and not docker'"`).
- Если Docker недоступен в CI → автоматически skip через `skipif(not _docker_available())`.

### 10. Final Validation
- **Task ID**: validate-all
- **Depends On**: unit-tests, integration-tests
- **Assigned To**: validator-final
- **Agent Type**: validator
- **Stack**: python pytest testcontainers integration unit
- **Parallel**: false
- Запустить `pytest` (без флагов) — full default suite зелёный, новые Testcontainers-тесты автоматически skip.
- Запустить `pytest tests/test_webui_only_toggle.py tests/test_tools_whitelist.py tests/test_git_sync_unit.py -v` — 18 unit-тестов pass.
- Запустить `pytest tests/test_commit_gate.py tests/test_git_ops_recovery.py tests/test_phase7_pipeline.py -v` — backwards-compat regression green (4 контрактных теста существующих).
- Запустить `pytest -m docker tests/integration/ -v --tb=short` — 5 integration pass. Wall-clock ≤30s per test.
- Запустить `python -m py_compile ouroboros/git_sync.py ouroboros/config.py supervisor/message_bus.py supervisor/git_ops.py ouroboros/tools/registry.py server.py` — компилируется без ошибок.
- Проверить regex `from testcontainers\.|DockerContainer.*gitea|@pytest\.mark\.docker` встречается в `tests/integration/test_git_sync_integration.py` (Infra signature).
- Проверить что все 4 happy-path сценария из `### Integration Layer (Python)` присутствуют в `tests/integration/test_git_sync_integration.py` по именам.
- Запустить `python .claude/hooks/validators/check_test_layers.py` — pass.
- Smoke 1 (Telegram off): `WEBUI_ONLY=true OUROBOROS_TOOLS_ENABLED="git,shell,core" python server.py &` затем `curl localhost:8000/health`. Grep логов: должен быть `[WEBUI_ONLY] Telegram bridge disabled by config` и `[Registry] Whitelist active`.
- Smoke 2 (graceful shutdown): `kill -TERM <pid>` → grep логов на `[git_sync] shutdown_push:`. Время от SIGTERM до exit ≤30s.
- Smoke 3 (default Desktop): `python server.py` без env — поведение Telegram и Tools остаётся прежним (no breaking changes).
- Acceptance criteria check: все пункты ниже выполнены.

## Acceptance Criteria

- [ ] `WEBUI_ONLY=true` → `TelegramBridge.configure_from_settings` не вызывает `_restart_telegram_polling`, лог `[WEBUI_ONLY] Telegram bridge disabled by config` присутствует.
- [ ] `WEBUI_ONLY=true` → owner-message loop с `telegram_chat_id != 0` логирует warning и нормализует к `0` (продолжает обработку как Web).
- [ ] `WEBUI_ONLY=true` → секция Telegram скрыта в Settings UI и в onboarding wizard (TG-страница пропускается).
- [ ] `WEBUI_ONLY=false` (default) → текущее Desktop-поведение полностью сохранено, никаких регрессий.
- [ ] `OUROBOROS_TOOLS_ENABLED="git,shell"` → `Registry.available_tools()` возвращает `["git", "shell"]` плюс защищённые (`CORE_TOOL_NAMES + ["list_available_tools", "enable_tools"]`).
- [ ] Без `OUROBOROS_TOOLS_ENABLED` → все автодискаверенные тулы доступны (текущее поведение, не зависит от frozen/non-frozen режима).
- [ ] `OUROBOROS_GIT_REMOTE_URL=https://gitlab.example.com/team/repo.git` + `OUROBOROS_GIT_USERNAME=user` + `OUROBOROS_GIT_PASSWORD=pass` → `configure_remote_url()` корректно конфигурирует origin и `.git/credentials` (URL-encoded), `git push` работает.
- [ ] `GITHUB_TOKEN+GITHUB_REPO` (старый путь) — не сломан, продолжает работать через backwards-compat shim. Все 4 существующих контрактных теста (`test_commit_gate.py`, `test_git_ops_recovery.py`, `test_phase7_pipeline.py`) остаются green.
- [ ] При старте сервера (если remote сконфигурирован) выполняется `git fetch + git pull --ff-only`. Non-FF создаёт rescue snapshot и не падает.
- [ ] При graceful shutdown (FastAPI `on_event("shutdown")`, триггерится uvicorn'ом из SIGTERM) сервер делает commit (если dirty) + push с retry. Завершается за ≤30 сек (с 25s timeout на shutdown_push).
- [ ] `push_to_remote()` ретраит 3 раза с backoff series `[1s, 2s]` (sleep между попытками 0→1, 1→2; финальный attempt 2 без sleep) при transient failure. Non-FF (`! [rejected]`/`non-fast-forward`/`fetch first` в stderr) не ретраится.
- [ ] `OUROBOROS_GIT_PASSWORD` НЕ появляется в log output (capsys assert в unit-тесте).
- [ ] Все 18 unit-тестов pass.
- [ ] Все 5 integration-тестов с Testcontainers gitea pass.
- [ ] Plain `pytest` (без флагов) на машине без Docker остаётся зелёным — новые Testcontainers-тесты skip через `addopts = "-m 'not integration and not docker'"`.
- [ ] `check_test_layers.py` pass.

## Validation Commands

Execute these commands to validate the task is complete:

- `pytest` — full default suite, no regressions, новые Testcontainers-тесты skip.
- `pytest tests/test_webui_only_toggle.py tests/test_tools_whitelist.py tests/test_git_sync_unit.py -v` — unit layer pass (18 tests).
- `pytest tests/test_commit_gate.py tests/test_git_ops_recovery.py tests/test_phase7_pipeline.py -v` — backwards-compat regression green.
- `pytest -m docker tests/integration/ -v --tb=short` — integration layer pass (требует Docker).
- `python -m py_compile ouroboros/git_sync.py ouroboros/config.py supervisor/message_bus.py supervisor/git_ops.py ouroboros/tools/registry.py server.py` — компиляция.
- `python .claude/hooks/validators/check_test_layers.py` — test layers verification.
- Smoke: `WEBUI_ONLY=true OUROBOROS_TOOLS_ENABLED="git,shell,core" python server.py &; sleep 3; curl localhost:8000/health; kill -TERM $!` → grep логов на `[WEBUI_ONLY]`, `Whitelist active`, `[git_sync] shutdown_push`.

## Notes

- Новая зависимость: `testcontainers>=4.0`, `pytest-mock>=3.10`, `requests` в `[project.optional-dependencies].test`. Установка: `uv pip install -e ".[test]"`. **Не в основные dependencies** — нужна только для разработки/CI.
- Docker должен быть доступен локально и в CI для integration-слоя. На машинах без Docker — автоматический skip через marker `docker` + `skipif(not _docker_available())`.
- `terminationGracePeriodSeconds` в k8s deployment — рекомендация ≥60 сек (документация для оператора, не часть кода).
- PVC под `OUROBOROS_REPO_DIR` — обязательно. Без PVC: каждый рестарт = re-clone, теряется локальная история. Тоже документация.
- `OUROBOROS_GIT_PASSWORD` в Secret. Не пишем в логи — verified в unit-тесте.
- Backwards compatibility: ВСЕ существующие настройки и пути продолжают работать. Никаких миграций. Существующие 4 контрактных теста для `configure_remote(slug, token)` остаются green без изменений.
- Webhook endpoint `/api/git/pull` (упоминался в анализе как опциональный) — **не входит в этот план**. Можно добавить отдельным change-request.
- Удаление Telegram кода (Approach B из анализа) — **не входит в этот план**. Текущий план оставляет Telegram функциональность нетронутой; toggle позволяет её отключить. Approach B — отдельный future cleanup.
- `migrate_remote_credentials()` (GitHub-specific, line 1042+) **не обобщается** — generic путь использует `configure_remote_url` напрямую и не нуждается в миграции legacy URL form.
- `signal.signal(SIGTERM, ...)` НЕ используется — конфликтует с uvicorn. Используется `app.add_event_handler("shutdown", coro)` который uvicorn вызывает в graceful shutdown lifecycle.
- `signal.alarm` НЕ используется — Windows-несовместим. Используется `threading.Timer`/`threading.Event.wait(timeout=25)` для бюджета shutdown_push.
- Frozen-build Tool count (21 модуль через `_FROZEN_TOOL_MODULES`) vs non-frozen (все автодискаверенные) — AC формулирует как "все автодискаверенные тулы", чтобы работало в обоих режимах.
