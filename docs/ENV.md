# Переменные окружения (ENV) для настройки агента

Справочник всех ENV-параметров Ouroboros. Источник истины — `ouroboros/config.py`
(`SETTINGS_DEFAULTS`, секция путей) плюс точечные `os.environ.get(...)` в модулях.

## Два класса параметров

| Класс | Где задаётся | Поведение |
|-------|--------------|-----------|
| **Settings-ключ** | UI / `data/settings.json` **или** ENV | При старте `apply_settings_to_env()` экспортирует значение из `settings.json` в `os.environ`. Пустое значение → ключ **удаляется** из окружения (внешний ENV не «просвечивает»). Для подмены значения правьте `settings.json`, а не ENV. |
| **ENV-only** | Только ENV | Не входит в `settings.json`, читается напрямую из окружения. Это операционные/инфраструктурные ручки (пути, режим контейнера, лимиты раундов). |

🔱 — параметр специфичен для форка `joi-lab/ouroboros-desktop` (k8s/headless), в upstream отсутствует.

---

## 1. Пути и каталоги (ENV-only)

| Переменная | Default | Назначение |
|------------|---------|------------|
| `OUROBOROS_APP_ROOT` | `~/Ouroboros` | Корень установки. От него считаются repo/data. |
| `OUROBOROS_REPO_DIR` | `<APP_ROOT>/repo` | Git-чекаут кода агента. При старте `server.py` делает fail-fast, если каталог отсутствует или не git-репозиторий. |
| `OUROBOROS_DATA_DIR` | `<APP_ROOT>/data` | Данные: settings, skills, логи, состояние. |
| `OUROBOROS_SETTINGS_PATH` | `<DATA_DIR>/settings.json` | Путь к файлу настроек. |
| `OUROBOROS_PID_FILE` | `<APP_ROOT>/ouroboros.pid` | PID-файл процесса. |
| `OUROBOROS_PORT_FILE` | `<DATA_DIR>/state/server_port` | Файл с актуальным портом (для extension_loader). |
| `DRIVE_ROOT` | `~/Ouroboros/data` | Корень рабочего «диска» агента (health-проверки, файловые инструменты). |

---

## 2. Сеть и доступ к Web UI

| Переменная | Класс | Default | Назначение |
|------------|-------|---------|------------|
| `OUROBOROS_SERVER_HOST` | settings | `127.0.0.1` | Интерфейс прослушивания HTTP-сервера. Для контейнера/k8s — `0.0.0.0`. |
| `OUROBOROS_SERVER_PORT` | ENV-only | `8765` | Порт HTTP-сервера. |
| `OUROBOROS_NETWORK_PASSWORD` | settings | `""` | Пароль доступа к Web UI/API. Пусто — без аутентификации. |
| 🔱 `WEBUI_ONLY` | settings | `False` | `True` → скрывает Telegram-UI и **не поднимает** `TelegramBridge`. Headless/k8s-режим без Telegram. Требует рестарта. |

---

## 3. Ключи провайдеров LLM (settings)

| Переменная | Default | Назначение |
|------------|---------|------------|
| `ANTHROPIC_API_KEY` | `""` | Ключ Anthropic (Claude). |
| `OPENROUTER_API_KEY` | `""` | Ключ OpenRouter. |
| `OPENAI_API_KEY` | `""` | Ключ OpenAI. |
| `OPENAI_BASE_URL` | `""` | Кастомный base URL для OpenAI-совместимого эндпоинта (legacy). |
| `OPENAI_COMPATIBLE_API_KEY` | `""` | Ключ для произвольного OpenAI-совместимого провайдера. |
| `OPENAI_COMPATIBLE_BASE_URL` | `""` | Base URL этого провайдера. |
| `CLOUDRU_FOUNDATION_MODELS_API_KEY` | `""` | Ключ Cloud.ru Foundation Models. |
| `CLOUDRU_FOUNDATION_MODELS_BASE_URL` | `https://foundation-models.api.cloud.ru/v1` | Эндпоинт Cloud.ru. |

Минимум один из `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` / `OPENAI_API_KEY` /
`OPENAI_COMPATIBLE_API_KEY` / `CLOUDRU_FOUNDATION_MODELS_API_KEY` обязателен.

---

## 4. Модели (settings)

| Переменная | Default | Назначение |
|------------|---------|------------|
| `OUROBOROS_MODEL` | `anthropic/claude-opus-4.6` | Основная модель агента. |
| `OUROBOROS_MODEL_CODE` | `anthropic/claude-opus-4.6` | Модель для кодовых задач. |
| `OUROBOROS_MODEL_LIGHT` | `anthropic/claude-sonnet-4.6` | Лёгкая/дешёвая модель. |
| `OUROBOROS_MODEL_FALLBACK` | `anthropic/claude-sonnet-4.6` | Фолбэк при сбое основной. |
| `CLAUDE_CODE_MODEL` | `claude-opus-4-6[1m]` | Модель для Claude Code-инструментов. |
| `OUROBOROS_WEBSEARCH_MODEL` | `gpt-5.2` | Модель веб-поиска. |
| `OUROBOROS_REVIEW_MODELS` | `openai/gpt-5.5,google/gemini-3.1-pro-preview,anthropic/claude-opus-4.6` | Список моделей триад-ревью (через запятую, с префиксом провайдера). |
| `OUROBOROS_SCOPE_REVIEW_MODEL` | `openai/gpt-5.5` | Одиночный блокирующий scope-ревьюер (после триад-ревью). |

---

## 5. Бюджет, лимиты, таймауты

| Переменная | Класс | Default | Назначение |
|------------|-------|---------|------------|
| `TOTAL_BUDGET` | settings | `10.0` | Общий бюджет в USD. |
| `OUROBOROS_PER_TASK_COST_USD` | settings | `20.0` | Лимит стоимости одной задачи (USD). |
| `OUROBOROS_MAX_WORKERS` | settings | `5` | Максимум параллельных воркеров. |
| `OUROBOROS_SOFT_TIMEOUT_SEC` | settings | `600` | Мягкий таймаут задачи (сек). |
| `OUROBOROS_HARD_TIMEOUT_SEC` | settings | `1800` | Жёсткий таймаут задачи (сек). |
| `OUROBOROS_TOOL_TIMEOUT_SEC` | settings | `600` | Таймаут одного вызова инструмента (сек). |
| `OUROBOROS_MAX_ROUNDS` | ENV-only | `200` | Максимум раундов основного цикла. |
| `OUROBOROS_EVO_COST_THRESHOLD` | settings | `0.10` | Порог стоимости (USD) для запуска эволюционного слоя. |

---

## 6. Режим работы и pre-commit review

| Переменная | Класс | Default | Назначение |
|------------|-------|---------|------------|
| `OUROBOROS_RUNTIME_MODE` | settings | `advanced` | `light` (без само-модификации репо) \| `advanced` (эволюционный слой) \| `pro` (запись в защищённое ядро, под триад+scope-гейтом). Понижение/повышение пинится на старте; агент не может само-эскалироваться. |
| `OUROBOROS_REVIEW_ENFORCEMENT` | settings | `advisory` | `advisory` (предупреждение) \| `blocking` (блокирует коммит при провале ревью). |
| `OUROBOROS_PREFLIGHT_DIFF_AWARE` | ENV-only | `true` | `true` → preflight учитывает diff. |
| `OUROBOROS_PRE_PUSH_TESTS` | ENV-only | `1` | `1` → прогон тестов перед push. Любое другое значение отключает. |
| `OUROBOROS_AGENT_PYTHON` | ENV-only | `sys.executable` → `python3` | Интерпретатор Python для подпроцессов агента. |

### Reasoning effort (settings) — `none` \| `low` \| `medium` \| `high`

| Переменная | Default |
|------------|---------|
| `OUROBOROS_EFFORT_TASK` | `medium` |
| `OUROBOROS_EFFORT_EVOLUTION` | `high` |
| `OUROBOROS_EFFORT_REVIEW` | `medium` |
| `OUROBOROS_EFFORT_SCOPE_REVIEW` | `high` |
| `OUROBOROS_EFFORT_CONSCIOUSNESS` | `low` |
| `OUROBOROS_INITIAL_REASONING_EFFORT` | — | Legacy-алиас для task/chat effort. |

---

## 7. 🔱 Git auto-sync (форк)

Bootstrap-pull при старте, push при остановке. Любой HTTPS git-сервер
(GitHub/GitLab/Gitea/Bitbucket). Приоритет: `OUROBOROS_GIT_REMOTE_URL` →
legacy `GITHUB_TOKEN`+`GITHUB_REPO`.

| Переменная | Класс | Default | Назначение |
|------------|-------|---------|------------|
| `OUROBOROS_GIT_REMOTE_URL` | settings | `""` | HTTPS-URL удалённого репозитория. Имеет приоритет над `GITHUB_TOKEN`/`GITHUB_REPO`. |
| `OUROBOROS_GIT_USERNAME` | settings | `""` | Имя пользователя. Пусто → `x-access-token` (PAT-стиль). |
| `OUROBOROS_GIT_PASSWORD` | settings | `""` | Пароль/токен. |
| `GITHUB_TOKEN` | settings | `""` | Legacy: PAT для GitHub. |
| `GITHUB_REPO` | settings | `""` | Legacy: `<owner>/<repo>` → `https://github.com/<repo>.git`. |
| `GITHUB_USER` | ENV-only | `""` | Используется в evolution-статистике. |
| `GITHUB_BRANCH` | ENV-only | `ouroboros` | Ветка для evolution-статистики. |
| `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` | ENV-only | — | Идентичность автора коммитов auto-sync. |
| `GIT_COMMITTER_NAME` / `GIT_COMMITTER_EMAIL` | ENV-only | — | Идентичность коммиттера (фолбэк для author). |

---

## 8. 🔱 Whitelist инструментов (форк)

| Переменная | Класс | Default | Назначение |
|------------|-------|---------|------------|
| `OUROBOROS_TOOLS_ENABLED` | settings | `""` | Список разрешённых инструментов через запятую. Пусто = **все** автообнаруженные (текущее поведение). Когда задан — экспонируются только перечисленные **плюс** защищённое ядро (`CORE_TOOL_NAMES`, `list_available_tools`, `enable_tools`). |

---

## 9. Локальная модель (llama-cpp-python, settings)

| Переменная | Default | Назначение |
|------------|---------|------------|
| `LOCAL_MODEL_SOURCE` | `""` | Источник модели (HF repo / путь). |
| `LOCAL_MODEL_FILENAME` | `""` | Имя файла модели (GGUF). |
| `LOCAL_MODEL_PORT` | `8766` | Порт локального сервера модели. |
| `LOCAL_MODEL_N_GPU_LAYERS` | `0` | Слоёв на GPU (0 = CPU). |
| `LOCAL_MODEL_CONTEXT_LENGTH` | `16384` | Длина контекста. |
| `LOCAL_MODEL_CHAT_FORMAT` | `""` | Формат чат-шаблона. |
| `USE_LOCAL_MAIN` | `False` | Использовать локальную модель как основную. |
| `USE_LOCAL_CODE` | `False` | Локальная для кодовых задач. |
| `USE_LOCAL_LIGHT` | `False` | Локальная как лёгкая. |
| `USE_LOCAL_FALLBACK` | `False` | Локальная как фолбэк. |

---

## 10. A2A (Agent-to-Agent протокол, settings)

Выключен по умолчанию; переключение требует рестарта.

| Переменная | Default | Назначение |
|------------|---------|------------|
| `A2A_ENABLED` | `False` | Включить A2A. |
| `A2A_HOST` | `127.0.0.1` | Хост A2A-сервера. |
| `A2A_PORT` | `18800` | Порт A2A-сервера. |
| `A2A_AGENT_NAME` | `""` | Имя агента в A2A. |
| `A2A_AGENT_DESCRIPTION` | `""` | Описание агента. |
| `A2A_MAX_CONCURRENT` | `3` | Максимум параллельных A2A-задач. |
| `A2A_TASK_TTL_HOURS` | `24` | TTL A2A-задачи (часы). |

---

## 11. Telegram (settings)

| Переменная | Default | Назначение |
|------------|---------|------------|
| `TELEGRAM_BOT_TOKEN` | `""` | Токен Telegram-бота. |
| `TELEGRAM_CHAT_ID` | `""` | ID чата для уведомлений. |

> При `WEBUI_ONLY=True` Telegram-мост не поднимается даже при заданном токене.

---

## 12. Фоновый режим / consciousness (ENV-only)

| Переменная | Default | Назначение |
|------------|---------|------------|
| `OUROBOROS_BG_MAX_ROUNDS` | `5` | Максимум раундов фоновой работы (settings-ключ). |
| `OUROBOROS_BG_WAKEUP_MIN` | `30` | Мин. интервал пробуждения (сек, settings-ключ). |
| `OUROBOROS_BG_WAKEUP_MAX` | `7200` | Макс. интервал пробуждения (сек, settings-ключ). |
| `OUROBOROS_BG_BUDGET_PCT` | `10` | Доля общего бюджета (%) на фоновую активность. |

---

## 13. Маркетплейс скиллов / каталог

| Переменная | Класс | Default | Назначение |
|------------|-------|---------|------------|
| `OUROBOROS_SKILLS_REPO_PATH` | settings | `""` | Доп. корень внешнего репозитория скиллов (свой git-чекаут). Сканируется поверх `data/skills/`. Ouroboros сам его не клонирует/пуллит. |
| `OUROBOROS_CLAWHUB_REGISTRY_URL` | settings | `https://clawhub.ai/api/v1` | URL реестра ClawHub. |
| `OUROBOROS_HUB_CATALOG_URL` | settings | `https://raw.githubusercontent.com/joi-lab/OuroborosHub/main/catalog.json` | URL каталога OuroborosHub. |
| `OUROBOROS_FILE_BROWSER_DEFAULT` | settings | `""` | Стартовый каталог файлового браузера UI. |

---

## 14. Служебные / инфраструктурные (ENV-only)

| Переменная | Default | Назначение |
|------------|---------|------------|
| `OUROBOROS_CONTAINER` | — | `1` → явно пометить запуск как контейнерный (влияет на platform_layer). |
| `OUROBOROS_DESKTOP_MODE` | `""` | Непусто → desktop-режим контекста. |
| `OUROBOROS_WORKER_START_METHOD` | platform default | Метод старта воркеров multiprocessing (`spawn`/`fork`). |
| `OUROBOROS_RUNTIME_MODE` (см. §6) | `advanced` | — |
| `OUROBOROS_BOOT_RUNTIME_MODE` | — | **Внутренний**: пин boot-baseline runtime-режима, экспортируется автоматически для наследования подпроцессами. Вручную задавать не нужно. |
| `USER` | `unknown` | Системный пользователь (профилирование окружения). |

---

## Пример: headless k8s-деплой (форк)

```env
OUROBOROS_SERVER_HOST=0.0.0.0
OUROBOROS_SERVER_PORT=8765
OUROBOROS_NETWORK_PASSWORD=<сильный-пароль>
WEBUI_ONLY=true
OUROBOROS_REPO_DIR=/app/repo
OUROBOROS_DATA_DIR=/app/data
ANTHROPIC_API_KEY=<ключ>
OUROBOROS_GIT_REMOTE_URL=https://gitlab.example.com/team/agent.git
OUROBOROS_GIT_USERNAME=ci-bot
OUROBOROS_GIT_PASSWORD=<deploy-token>
OUROBOROS_TOOLS_ENABLED=run_shell,git_commit,claude_code_edit
GIT_AUTHOR_NAME=Ouroboros Bot
GIT_AUTHOR_EMAIL=bot@example.com
```

> Помни: для **settings-ключей** значение из `data/settings.json` при старте
> перезаписывает ENV (а пустое — стирает его из окружения). В k8s-деплое либо
> монтируй заранее заполненный `settings.json`, либо убедись, что эти ключи в
> `settings.json` пусты, чтобы ENV из манифеста сработал.
