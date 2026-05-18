# Change: Полное удаление интеграции с Claude Code (SDK-слой агента)

> **Статус**: Архивировано
>
> **Дата создания**: 2026-05-18
>
> **Автор**: Artem Simeshin
>
> **Версия**: 1.0
>
> **Целевая спецификация**: openspec/specs/ouroboros/ouroboros.md
>
> **План реализации**: specs/remove-claude-code-integration.md

---

## 1. Предложение

### Цель изменения

Полностью убрать из runtime-агента интеграцию с Claude Code, построенную поверх Python-пакета `claude-agent-sdk`. Сейчас она состоит из двух инструментов и поддерживающего SDK-слоя:

- `claude_code_edit` — делегирование правок кода в Claude Code SDK (edit-mode, `gateways/claude_code.py::run_edit`).
- `claude_advisory_review` / `advisory_pre_review` — пред-коммитное advisory-ревью через read-only SDK (`gateways/claude_code.py::run_readonly`, `tools/claude_advisory_review.py`), завязанное на `AdvisoryReviewState` и пред-коммитный гейт в `commit_gate.py`.
- Поддерживающий слой: `claude_runtime.py`, SDK-хелперы в `platform_layer.py`, runtime-зависимость `claude-agent-sdk`, настройка `CLAUDE_CODE_MODEL` + UI и серверные endpoints `/api/claude-code/status|install`.

Мотивация (продолжение линии `d17e24a` remove-pyinstaller, `775fa39` remove-playwright):

- **Сокращение attack surface.** Делегирование исполнения SDK-субагенту с правкой файлов и доступом к сети — крупная и трудно контролируемая поверхность; auto-repair SDK через pip в рантайме — вектор compromise.
- **Упрощение runtime.** Advisory-ревью вплетено в commit gate и `_invalidate_advisory` вызывается из ~14 точек всех edit-инструментов — источник связности и хрупкости.
- **Вес/хрупкость.** Bundled CLI + версионные проверки/repair раздувают окружение и нестабильны в headless/k8s.

Замена **не предусмотрена** (подтверждено заказчиком). Блокирующая ревью-триада (`OUROBOROS_REVIEW_MODELS` / `OUROBOROS_REVIEW_ENFORCEMENT=advisory|blocking`) — **отдельная машинерия, сохраняется без изменений**.

### Инициатор

Владелец форка (Artem Simeshin).

### Затронутые компоненты

- [x] Инструменты агента: `claude_code_edit` (`tools/shell.py::_claude_code_edit`), `claude_advisory_review`/`advisory_pre_review` (`tools/claude_advisory_review.py` — удаляется целиком)
- [x] Транспорт: `ouroboros/gateways/claude_code.py` (удаляется целиком)
- [x] Валидация runtime: `ouroboros/claude_runtime.py` (удаляется целиком), SDK-хелперы `platform_layer.py` (`ClaudeRuntimeState`, `_find_sdk_package_path`, `_find_bundled_cli`, `_probe_cli_version`, `_detect_legacy_user_site_sdk`, `resolve_claude_runtime`)
- [x] Пред-коммитный гейт: `commit_gate.py` (`_invalidate_advisory`, `_check_advisory_freshness`, фаза `advisory_gate`), call-sites `_invalidate_advisory` в `git.py`/`git_pr.py`/`shell.py`, параметр `skip_advisory_pre_review`
- [x] Общий ledger: `review_state.py` (хирургический тримминг advisory-only; KEEP блокирующий ledger и `_STATE_RELPATH`/`_LOCK_RELPATH`), `review_evidence.py`, `agent_task_pipeline.py`, `review_helpers.py`
- [x] Реестр/политика: `tools/registry.py`, `tool_capabilities.py`, `safety.py`, `context_compaction.py`, `loop_tool_execution.py`
- [x] Конфигурация: `CLAUDE_CODE_MODEL` (`config.py`, env-проброс, `server_runtime.py` migration-keys, `web/modules/settings*.js`), endpoints `/api/claude-code/*` и Claude-runtime status block в `server.py`
- [x] Зависимости/CI: `requirements.txt`, `pyproject.toml` (dependencies, `claude-sdk` extra, `all` extra), `.github/workflows/ci.yml`
- [x] Тесты: удаление SDK-coupled (`test_claude_code_gateway.py`, `test_claude_runtime.py`, `test_advisory_*`, `test_max_tokens_constants.py`); рефактор импортов/моков в review/commit-тестах; расширение `test_post_refactor_integration.py`
- [x] Документация: `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`; спека `ouroboros.md`
- [ ] Каталог `.claude/` на диске и dev-тулинг (hooks/agents/commands) — **НЕ трогаем** (runtime-данные advisory в `~/Ouroboros/data/state/advisory_review.json`, не в `.claude/`)

### Приоритет

Высокий (безопасность runtime + упрощение развёртывания).

### Обратная совместимость

Нет. Breaking change для поверхности инструментов агента и пред-коммитного потока (`skip_advisory_pre_review` исчезает). Переходный период не предусмотрен.

### Флоу клиента (ADDED / MODIFIED / REMOVED)

Browser/SDK-инструменты — внутренний инструментарий агентского цикла, не клиентский HTTP/WS API. На уровне внешнего клиента веб-UI флоу не меняется, кроме удаления одного поля настроек.

#### ADDED / MODIFIED

Нет изменений.

#### REMOVED

| # | Шаг клиента | Endpoint / команда | Причина | Замена |
|---|-------------|---------------------|---------|--------|
| 1 | Агент делегирует правку кода SDK-субагенту | tool-вызов `claude_code_edit` | Сокращение attack surface, упрощение runtime | Удалён без замены |
| 2 | Агент запускает пред-коммитное advisory-ревью | tool-вызов `advisory_pre_review` / `claude_advisory_review` | То же; commit gate теряет advisory-слой | Удалён без замены |
| 3 | Пользователь выбирает модель Claude Code в настройках | settings-поле `CLAUDE_CODE_MODEL` + панель Repair Runtime | Интеграция удалена | Удалён без замены |
| 4 | Repair/статус Claude runtime из UI | `GET /api/claude-code/status`, `POST /api/claude-code/install` | То же | Удалён без замены |

---

## 2. Бизнес-логика

Удаляется поведенческая логика SDK-интеграции. Правила, действующие сейчас и снимаемые этим change:

**R1 (REMOVED). Делегированная правка кода через Claude Code SDK.**
- **Формулировка:** ЕСЛИ вызван `claude_code_edit(prompt, cwd?, budget?, validate?)` И `ANTHROPIC_API_KEY` задан — ТОГДА запустить SDK edit-mode с PreToolUse path-guard'ами, собрать changed_files/diff/usage, инвалидировать advisory; ИНАЧЕ вернуть `CLAUDE_CODE_UNAVAILABLE`.
- **После change:** инструмент удалён; делегированной правки кода нет.

**R2 (REMOVED). Пред-коммитный advisory-гейт.**
- **Формулировка:** ЕСЛИ вызван `repo_commit` И для текущего снапшота нет свежего matching advisory-прогона И не передан `skip_advisory_pre_review=True` И задан `ANTHROPIC_API_KEY` — ТОГДА блокировать коммит фразой `ADVISORY_PRE_REVIEW_REQUIRED`; иначе пропустить (audited bypass).
- **После change:** правило удалено. `repo_commit` не требует advisory-прогона; параметр `skip_advisory_pre_review` снят; ветка `_attempt_phase` `no_advisory→advisory_gate` недостижима. Блокирующая ревью-триада действует без изменений.

**R3 (REMOVED). Инвалидация advisory после мутации worktree.**
- **Формулировка:** ЕСЛИ edit-инструмент (`run_shell`, `claude_code_edit`, git-операции, PR-операции) изменил worktree — ТОГДА пометить ранее свежие advisory-прогоны stale (`_invalidate_advisory` → `invalidate_advisory_after_mutation`).
- **После change:** правило и ~14 call-sites удалены; freshness-машинерия в `review_state.py` снята.

**R4 (REMOVED). Валидация/repair Claude runtime baseline.**
- **Формулировка:** на старте проверить импорт `claude_agent_sdk`, его версию ≥ baseline и наличие bundled CLI; при провале — pip-repair; отрисовать статус-метку в UI/settings.
- **После change:** правило, `claude_runtime.py`, SDK-хелперы `platform_layer.py`, status block и endpoints `/api/claude-code/*` удалены.

---

## 3. Глоссарий

Целевая спека: термин «Ревью-триада» (glossary, строка 76) — **сохраняется без изменений** (это блокирующая триада `OUROBOROS_REVIEW_MODELS`, не Claude SDK). Термины «Claude Code gateway», «AdvisoryReviewState (advisory-runs/freshness)» — снимаются как деталь реализации.

### ADDED / MODIFIED

Нет изменений.

### REMOVED

Нет изменений на уровне доменного глоссария (снимаемые термины — детали реализации, не доменные контракты).

---

## 4. Бизнес-инварианты

Инварианты раздела 1.3 (видимость инструментов по whitelist/защищённому ядру, ограничение записи в защищённую поверхность, force-push запрещён) формулируются независимо от набора инструментов и **сохраняются**. Инвариант «защищённая поверхность под runtime-режимом» сохраняется; снимается лишь его частная реализация `claude_code_edit` post-execution revert guard (защита остаётся за счёт hardcoded sandbox реестра + `POLICY_*` + штатного commit-review).

Нет изменений.

---

## 5. Состояния и переходы (FSM)

SDK-инструменты не управляют жизненным циклом доменной сущности. Снимается недостижимость фазы `advisory_gate` в attempt-ledger; блокирующий attempt-FSM (`reviewing`/`blocked`/`accepted`) сохраняется.

Нет изменений доменного FSM.

---

## 6. Изменения моделей данных

Хранилище файловое (JSON в плоскости данных). `~/Ouroboros/data/state/advisory_review.json` — **общий** файл: хранит `advisory_runs` (снимается) И блокирующий ledger `attempts`/`blocking_history`/`open_obligations`/`commit_readiness_debts` (сохраняется). Файл и `_STATE_RELPATH`/`_LOCK_RELPATH` НЕ удаляются.

### ADDED / MODIFIED

Нет изменений.

### REMOVED

| Сущность / Поле | Причина удаления | Миграция |
|-----------------|------------------|----------|
| `AdvisoryRunRecord` (dataclass) + поле `AdvisoryReviewState.advisory_runs` | Advisory-ревью удаляется | Не требуется (in-place: поле перестаёт читаться/писаться; существующие файлы игнорируются) |
| Методы freshness: `runs`/`latest`/`filter_advisory_runs`/`is_fresh`/`find_by_hash`/`add_run`/`mark_stale`/`mark_all_stale*`/`mark_repo_stale`, `invalidate_advisory_after_mutation`, `advisory_stale` evidence-ветка | Те же | Не требуется |
| `ClaudeRuntimeState` (dataclass) | SDK-слой удаляется | Не требуется (in-memory) |

> **KEEP:** `CommitAttemptRecord` (вкл. поле `advisory_findings` как severity-тег блокирующей триады), `ObligationItem`, `CommitReadinessDebtItem`, blocking_history, `_STATE_RELPATH`/`_LOCK_RELPATH`.

---

## 7. Изменения интеграций

Claude Code SDK — внешняя runtime-интеграция (subprocess CLI, исходящие HTTPS к Anthropic API, pip-repair). В спеке `ouroboros.md` (раздел 2.5–2.6) задокументирована как «Claude Code gateway»; снимается.

### ADDED / MODIFIED

Нет изменений.

### REMOVED

| Интеграция | Причина | Замена |
|------------|---------|--------|
| «Claude Code gateway» (subprocess CLI `claude`, исходящие HTTPS к Anthropic API через `claude-agent-sdk`, pip-repair runtime) | Attack surface, упрощение runtime | Удалена без замены |
| Runtime-зависимость `claude-agent-sdk>=0.1.60` (`requirements.txt`, `pyproject.toml` dependencies/`claude-sdk`/`all`) | Та же | Удалена полностью |

---

## 7А. Обработчики (по триггерам)

### ADDED / MODIFIED

Нет изменений.

### REMOVED

| # | Триггер | Причина | Замена |
|---|---------|---------|--------|
| 1 | tool-call `claude_code_edit(prompt, cwd?, budget?, validate?)` | Attack surface, упрощение | Удалён без замены |
| 2 | tool-call `advisory_pre_review`/`claude_advisory_review(commit_message, skip_tests?)` | Commit gate теряет advisory-слой | Удалён без замены |
| 3 | пред-коммитный хук `_check_advisory_freshness` в `repo_commit` | Advisory-гейт снят | Удалён; блокирующий гейт сохраняется |
| 4 | post-mutation хук `_invalidate_advisory` (~14 call-sites в git/git_pr/shell/commit_gate) | Нет advisory-freshness | Удалён без замены |
| 5 | HTTP `GET /api/claude-code/status`, `POST /api/claude-code/install` | SDK-runtime удалён | Удалён без замены |
| 6 | startup-проверка `verify_claude_runtime` / Claude-runtime status block | То же | Удалён без замены |

**Поведение при запросе удалённого инструмента.** `claude_code_edit`/`advisory_pre_review` отсутствуют в реестре и в схемах модели; применяется штатная обработка неизвестного/недоступного инструмента (инвариант 1.3.1). Спец-deprecation и fallback не вводятся.

---

## 8. Обработка ошибок

Снимаемые инструменты возвращали человекочитаемые строки (`⚠️ CLAUDE_CODE_*`, `⚠️ ADVISORY_*`, `ADVISORY_PRE_REVIEW_REQUIRED`). После удаления эти строки исчезают вместе с инструментами; внешний HTTP/WS API сервиса (RFC 7807 / коды ошибок спеки) не затрагивается. Status-каскад `⚠️ CLAUDE_CODE_*→{timeout,install_error,unavailable,claude_code_error}` в `loop_tool_execution.py` снимается целиком.

Нет изменений контракта ошибок API.

---

## 9. Хедеры и метаданные

Нет изменений.

---

## 10. Валидация входящих значений

Снимаемые инструменты валидировали свои аргументы (`prompt` непустой, `budget`/`cwd`, `skip_tests`) — вне контракта внешнего API, удаляются вместе с инструментами. Валидация `OUROBOROS_REVIEW_ENFORCEMENT ∈ {advisory,blocking}` (onboarding_wizard) — **сохраняется** (это блокирующая триада).

Нет изменений API-валидации.

---

## 11. Влияние на безопасность

- **Авторизация/аутентификация**: без изменений (инструменты были `POLICY_CHECK`/`POLICY_SKIP`, без отдельной auth).
- **Сокращение attack surface (положительное)**: устраняется делегирование исполнения SDK-субагенту (правка файлов, доступ к сети), pip-repair runtime (исполнение внешнего кода), subprocess CLI.
- **Политика инструментов**: запись `claude_code_edit: POLICY_CHECK` и `claude_code_edit` revert-guard удаляются; защищённая поверхность остаётся под hardcoded sandbox реестра + `POLICY_*` + штатным commit-review (инвариант не ослаблен).
- **Регресс ревью**: блокирующая ревью-триада (`OUROBOROS_REVIEW_ENFORCEMENT`) сохраняется — пред-коммитный контроль качества не исчезает полностью, снимается лишь рекомендательный Claude-SDK-advisory-слой.

---

## 12. Миграция

Обратная совместимость не сохраняется (breaking change). Переходный период не предусмотрен.

### Шаги миграции данных

Миграции данных не требуется. Файл `state/advisory_review.json` сохраняется (блокирующий ledger), поле `advisory_runs` перестаёт читаться/писаться, существующие значения игнорируются. Технические шаги — см. план `specs/remove-claude-code-integration.md` (Step-by-Step Tasks 1–8).

### Обратная совместимость API

Внешний HTTP/WS API веб-UI не меняется, кроме удаления endpoints `/api/claude-code/*` и одного settings-поля. Поверхность инструментов агента меняется ломающе; запрос удалённого инструмента — штатная обработка неизвестного инструмента, без версионирования и fallback. `repo_commit(skip_advisory_pre_review=...)` — параметр удалён (вызов с ним → ошибка неизвестного аргумента по штатной обработке).

### План отката

1. Восстановить `gateways/claude_code.py`, `claude_runtime.py`, `tools/claude_advisory_review.py` из git-истории.
2. Вернуть `claude-agent-sdk` в `requirements.txt`/`pyproject.toml`.
3. Вернуть advisory-машинерию в `review_state.py`/`commit_gate.py`/`review_evidence.py`/`agent_task_pipeline.py`, call-sites `_invalidate_advisory`, `skip_advisory_pre_review`.
4. Вернуть `claude_code_edit` ToolEntry/политику, SDK-хелперы `platform_layer.py`, `CLAUDE_CODE_MODEL`, server status block/endpoints, UI.
5. Вернуть CI-шаги, удалённые тесты и документацию.

Риск потери данных при откате: отсутствует (advisory-поля additive; блокирующий ledger не затронут).

---

## 13. Логирование (новые / изменённые события)

Удаляемые модули писали служебные события через stdlib logging (claude-cli stderr, advisory-прогоны, SDK-repair). Они исчезают вместе с модулями; не входят в систему кодов логирования спеки (раздел 2.8). Новых/изменённых кодированных событий нет.

Нет изменений.

---

## 14. Мониторинг (новые / изменённые метрики)

SDK-интеграция кодированных метрик не экспортировала (только `llm_usage`-события с `provider=claude_agent_sdk` / `source=claude_code_edit` — перестают эмититься, отдельной метрики не было). Метрик не добавляется/не изменяется.

Нет изменений.

---

## 15. Изменения конфигурации

### ADDED / MODIFIED

Нет изменений.

### REMOVED

| Параметр | Причина | Миграция |
|----------|---------|----------|
| `CLAUDE_CODE_MODEL` (settings default `config.py`, env-проброс, `server_runtime.py` migration-`keys`, UI `web/modules/settings*.js`) | SDK-интеграция удалена | Удалить из настроек/окружения; действий по данным не требуется (неизвестный ключ игнорируется загрузчиком настроек) |

> `OUROBOROS_REVIEW_ENFORCEMENT`, `OUROBOROS_REVIEW_MODELS` — **НЕ меняются** (блокирующая триада). `ANTHROPIC_API_KEY` остаётся (используется обычным Anthropic-провайдером LLM-роутинга, не Claude Code SDK).

---

## 16. Рекомендации к критериям приёмки

| # | КОГДА | ТОГДА |
|---|-------|-------|
| 1 | Агент запрашивает `claude_code_edit` или `advisory_pre_review`/`claude_advisory_review` | Инструмент отсутствует в реестре и в схемах модели (core_only и полный набор); штатная обработка неизвестного инструмента; fallback отсутствует |
| 2 | `repo_commit` на чистом репо без advisory-прогона | Коммит проходит; фраза `ADVISORY_PRE_REVIEW_REQUIRED` не появляется; параметр `skip_advisory_pre_review` отсутствует в сигнатуре и JSON-schema |
| 3 | Прогон блокирующей ревью-триады (`OUROBOROS_REVIEW_ENFORCEMENT=blocking`) | Триада работает без регрессий; `CommitAttemptRecord.advisory_findings` (severity-тег) рендерится в `agent_task_pipeline`; obligations/debts/blocking_history сохранены |
| 4 | Чистая runtime-установка без `[claude-sdk]` extra; статический и рантайм-анализ импортов | `claude-agent-sdk` не установлен; ни один runtime-путь не импортирует `claude_agent_sdk`; модули `gateways/claude_code.py`/`claude_runtime.py`/`tools/claude_advisory_review.py` отсутствуют |
| 5 | Поиск `CLAUDE_CODE_MODEL`/`claude_agent_sdk` по `ouroboros/`+`server.py` и по `web/modules/`, `requirements.txt`, `pyproject.toml` | 0 совпадений в runtime-коде/зависимостях/UI; endpoints `/api/claude-code/*` и Claude-runtime status block в `server.py` удалены |
| 6 | Прогон CI quick-test без `[claude-sdk]` | Проходит без скрытых SDK-зависимостей; claude-специфичных шагов нет |
| 7 | Полный `pytest -q` + integration-слой `test_post_refactor_integration.py` | Зелёные; ≥4 integration-сценария (commit без advisory, живая блокирующая триада, реестр без claude-инструментов, импорт без SDK) |
| 8 | `git status --porcelain .claude/` | Пусто — каталог `.claude/` на диске не изменён |
| 9 | Регресс-проверка: попытка делегировать правку SDK-субагенту или принудить advisory | Возможность отсутствует на уровне набора инструментов; attack surface SDK снят полностью |
