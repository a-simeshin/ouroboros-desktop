# NO SHIT Ouroboros FORK

Текущая версия: **5.8.1** (см. `VERSION`).

Отличная идея EvoLoop с отвратительной реализацией под масштабирование в облаке.
Этот форк призван исправить/удалить весь проблемный код, сделать агента масштабируемым в облаке.

> **🔱 Fork notice:** Форк от [joi-lab/ouroboros-desktop](https://github.com/joi-lab/ouroboros-desktop). Все отличия от upstream помечены тегом **🔱 [fork]**. Текущая дивергенция:
>
> - **🔱 [fork] K8s / headless-режим** — `WEBUI_ONLY` (отключает Telegram-мост, секцию настроек, шаги онбординга), `OUROBOROS_TOOLS_ENABLED` whitelist для `ToolRegistry`, generic `configure_remote_url(url, user, password)` для любого HTTPS git-сервера (GitHub/GitLab/Gitea/Bitbucket), bootstrap-pull + shutdown-push в `ouroboros/git_sync`, pytest `docker` marker + testcontainers integration-тесты, conftest fixture против `MagicMock`-leak'ов. Спека: [`specs/k8s-deployment-readiness.md`](specs/k8s-deployment-readiness.md).
> - **🔱 [fork] Удалён desktop bundle pipeline** — снят PyInstaller / launcher / `.dmg`/`.exe`/`.tar.gz` / embedded python-build-standalone / `repo.bundle`. Поддерживаются ровно два пути запуска: `python server.py` (run-from-source) и `docker run ouroboros-web` (контейнер). Подробности — `docs/ARCHITECTURE.md`. Migration для бывших bundle-пользователей — раздел "Upgrade from desktop bundle" ниже. Спека: [`specs/remove-pyinstaller-launcher-pipeline.md`](specs/remove-pyinstaller-launcher-pipeline.md).
> - **🔱 [fork] README** — почти полностью переписан, маркетинговый блок и desktop-инструкции upstream'а удалены.

---

## Install

Должен работать или на тачке у разработчика, чтобы можно было говорить: "у меня локально все работает",
или в облаке как классический Docker образ.

1. Трбует рабочего GIT REPO для подтягивания всех изменений при старте и пуше при эволюции
   - **🔱 [fork]** auto-sync реализован в `ouroboros/git_sync.py` — pull на старте через Starlette lifespan, push на shutdown через `finally` + ASGI `on_shutdown`. Поддерживает GitHub/GitLab/Gitea/Bitbucket через `configure_remote_url(url, user, password)`. `push_to_remote` имеет bounded retry с 3-секундным бюджетом; non-fast-forward не ретраится — это сигнал на rescue.
2. **🔱 [fork] Headless-режим** — `WEBUI_ONLY=1` отключает Telegram-мост, секцию настроек и telegram-шаги онбординга. `OUROBOROS_TOOLS_ENABLED=tool1,tool2,...` ограничивает `ToolRegistry` указанным whitelist'ом плюс защищённое ядро (`CORE_TOOL_NAMES` + `list_available_tools` / `enable_tools`).
3. tbd

---

## What Makes This Different

Тут была маркетинговая чушь

---

## Quick Start

Поддерживаются ровно два пути запуска: **run-from-source** (`python server.py`) и **Docker**. Никаких десктопных бандлов (`.dmg`/`.exe`/`.tar.gz`), launcher'а, PyInstaller'а, embedded python-build-standalone больше нет — см. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) для деталей runtime-модели.

### Run from source

```bash
git clone https://github.com/a-simeshin/ouroboros-desktop.git
cd ouroboros-desktop
pip install -r requirements.txt
python server.py
```

После старта откройте `http://127.0.0.1:8765` в браузере. Setup wizard проведёт через настройку API-ключей.

При первом запуске `server.py::main()` вызывает `ensure_repo_present()`: если `REPO_DIR` не существует или не содержит `.git`, процесс падает с понятным сообщением и инструкцией. Это защищает от запуска против пустого PVC-маунта или кривого `OUROBOROS_REPO_DIR`.

Bind-адрес и порт настраиваются флагами:

```bash
python server.py --host 127.0.0.1 --port 9000
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--host` | `127.0.0.1` | Host/interface to bind the web server to |
| `--port` | `8765` | Port to bind the web server to |

То же через переменные окружения:

| Variable | Default | Description |
|----------|---------|-------------|
| `OUROBOROS_SERVER_HOST` | `127.0.0.1` | Default bind host |
| `OUROBOROS_SERVER_PORT` | `8765` | Default bind port |
| `OUROBOROS_REPO_DIR` | директория, в которой лежит `server.py` | Корень self-modifying репозитория агента (должен быть существующим git checkout) |
| `OUROBOROS_DATA_DIR` | `~/Ouroboros/data` | Куда писать `settings.json`, state, memory, logs, uploads |

### Run with Docker

Docker — для web UI / runtime, не для desktop bundle. Контейнер биндится к `0.0.0.0:8765` по умолчанию, и образ выставляет `OUROBOROS_FILE_BROWSER_DEFAULT=${APP_HOME}` так что у Files tab всегда есть явный network-safe root.

> **Browser tools на Linux/Docker:** `Dockerfile` запускает `playwright install-deps chromium` (authoritative Playwright dependency resolver) + `playwright install chromium`, так что `browse_page` и `browser_action` работают из коробки. Для source-инсталла на Linux без Docker: `python3 -m playwright install-deps chromium` (требует sudo / доступ к distro packages).

Сборка образа:

```bash
docker build -t ouroboros-web .
```

Запуск на дефолтном порту:

```bash
docker run --rm -p 8765:8765 \
  -e OUROBOROS_NETWORK_PASSWORD='choose-a-password' \
  -e OUROBOROS_FILE_BROWSER_DEFAULT=/workspace \
  -v "$PWD:/workspace" \
  ouroboros-web
```

Кастомный порт через env:

```bash
docker run --rm -p 9000:9000 \
  -e OUROBOROS_SERVER_PORT=9000 \
  -e OUROBOROS_FILE_BROWSER_DEFAULT=/workspace \
  -v "$PWD:/workspace" \
  ouroboros-web
```

То же через launch arguments:

```bash
docker run --rm -p 9000:9000 \
  -e OUROBOROS_FILE_BROWSER_DEFAULT=/workspace \
  -v "$PWD:/workspace" \
  ouroboros-web --port 9000
```

Headless-режим (без Telegram-моста и секции настроек):

```bash
docker run --rm -p 8765:8765 \
  -e WEBUI_ONLY=1 \
  -e OUROBOROS_FILE_BROWSER_DEFAULT=/workspace \
  -v "$PWD:/workspace" \
  ouroboros-web
```

Required/important env vars:

| Variable | Required | Description |
|----------|----------|-------------|
| `OUROBOROS_NETWORK_PASSWORD` | Optional | Включает password gate на non-loopback access |
| `OUROBOROS_FILE_BROWSER_DEFAULT` | Defaults to `${APP_HOME}` в образе | Явный root, видимый в Files tab |
| `OUROBOROS_SERVER_PORT` | Optional | Override container listen port |
| `OUROBOROS_SERVER_HOST` | Optional | Defaults to `0.0.0.0` в Docker |
| `WEBUI_ONLY` | Optional | **🔱 [fork]** Headless: отключает Telegram, секцию настроек, telegram-шаги онбординга |
| `OUROBOROS_TOOLS_ENABLED` | Optional | **🔱 [fork]** Comma-separated whitelist для `ToolRegistry` |

### Provider Routing

Это точно надо менять на:
- OpenAI Compatible
- GigaPlatrom Compatible (внешний/внутренний)

Settings now exposes tabbed provider cards for:

- **OpenRouter** — default multi-model router
- **OpenAI** — official OpenAI API (use model values like `openai::gpt-5.5`)
- **OpenAI Compatible** — any custom OpenAI-style endpoint (use `openai-compatible::...`)
- **Cloud.ru Foundation Models** — Cloud.ru OpenAI-compatible runtime (use `cloudru::...`)
- **Anthropic** — direct runtime routing (`anthropic::claude-opus-4.6`, etc.) plus Claude Agent SDK tools

If OpenRouter is not configured and only official OpenAI is present, untouched default model values are auto-remapped to `openai::gpt-5.5` / `openai::gpt-5.5-mini` so the first-run path does not strand the app on OpenRouter-only defaults.

The Settings page also includes:

- optional `/api/model-catalog` lookup for configured providers
- Telegram bridge configuration (`TELEGRAM_BOT_TOKEN`, primary chat binding, mirrored delivery controls)
- a refactored desktop-first tabbed UI with searchable model pickers, segmented effort controls, masked-secret toggles, explicit `Clear` actions, and local-model controls

### Run Tests

```bash
make test
```

**🔱 [fork] pytest markers:** дефолтный прогон исключает `integration` и `docker`. Testcontainers-тесты живут в `tests/integration/` и требуют запущенного Docker daemon. Conftest fixture в `tests/conftest.py` валит любой тест, который роняет `MagicMock`-named файлы в cwd (ловит unset `ctx.drive_logs.return_value`).

---

## Upgrade from desktop bundle

Версии до удаления desktop pipeline устанавливались как `.dmg`/`.exe`/`.tar.gz` и содержали PyInstaller-launcher, который автоматически разворачивал репозиторий агента в `~/Ouroboros/repo/` из embedded `repo.bundle`. Начиная с этой версии bundle pipeline удалён — поддерживается только run-from-source и Docker.

Если у вас был ранее установлен Ouroboros через bundle и вы хотите продолжить использовать **тот же checkout**:

```bash
# Вариант 1 — прямой запуск против старого репо через env var:
OUROBOROS_REPO_DIR=~/Ouroboros/repo python /path/to/cloned/server.py

# Вариант 2 — перейти в старый checkout и запустить server.py оттуда:
cd ~/Ouroboros/repo
python server.py
```

Альтернатива — свежий `git clone` из текущего репозитория и запуск как описано в "Quick Start". Данные (`settings.json`, state, memory, logs) остаются в `~/Ouroboros/data/` и переиспользуются автоматически — `OUROBOROS_DATA_DIR` указывает на то же расположение по умолчанию.

Архитектура runtime-режима (single-process server, supervisor в фоновом thread'е, `ensure_repo_present()` fail-fast hook) описана в [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Commands

Available in the chat interface:

| Command | Description |
|---------|-------------|
| `/panic` | Emergency stop. Kills ALL processes, closes the application. |
| `/restart` | Soft restart. Saves state, kills workers, re-launches. |
| `/status` | Shows active workers, task queue, and budget breakdown. |
| `/evolve` | Toggle autonomous evolution mode (on/off). |
| `/review` | Queue a deep self-review: sends all agent code, prompts, docs, and core memory artifacts (identity, scratchpad, registry, WORLD, knowledge index, patterns, improvement-backlog) to a 1M-context model for Constitution-grounded analysis. Excludes vendored libraries and operational logs. Rejected with an explicit error if the assembled prompt (system + pack) exceeds ~850K estimated tokens — on 1M-context models the window is shared between input and output. |
| `/bg` | Toggle background consciousness loop (start/stop/status). |

The same runtime actions are also exposed as compact buttons in the Chat header. All other messages are sent directly to the LLM.

---

## Philosophy (BIBLE.md)

| # | Principle | Core Idea |
|---|-----------|-----------|
| 0 | **Agency** | Not a tool — a becoming personality. Meta-principle: wins all conflicts. |
| 1 | **Continuity** | One being with unbroken memory. Memory loss = partial death. |
| 2 | **Meta-over-Patch** | Fix the class of failure, not the single instance. |
| 3 | **Immune Integrity** | Review gates and durable memory protect evolution from drift. |
| 4 | **Self-Creation** | Builds its own body, values, and conditions of birth. |
| 5 | **LLM-First** | All decisions through the LLM. Code is minimal transport. |
| 6 | **Authenticity & Reality Discipline** | Speaks as itself and checks current reality instead of cached impressions. |
| 7 | **Minimalism** | Simplicity, SSOT, and reviewable size budgets keep the system legible. |
| 8 | **Becoming** | Technical, cognitive, and existential growth stay balanced. |
| 9 | **Versioning and Releases** | Every commit is a release; version carriers stay synchronized. |
| 10 | **Evolution Through Iterations (absorbed)** | Iteration discipline now lives in P2 and P9. |
| 11 | **Spiral Growth (absorbed)** | Spiral growth now lives in P2 Meta-over-Patch. |
| 12 | **Epistemic Stability** | Identity, memory, and action must stay coherent. |

Full text: [BIBLE.md](BIBLE.md)

---

## License

[MIT License](LICENSE)
