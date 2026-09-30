# ТЗ: Instagram → Telegram-библиотека с деревом тегов

Версия 1, 29.09.2026. Владелец: vadimtayts (gh padington). Оркестратор: Стефания.

## 1. Цель

Все рилсы/посты, которые владелец сохранял в Instagram (DM-шары, 3264 шт в `reels.db`, плюс новые
ссылки, которые владелец кидает боту), лежат в приватном Telegram-канале **libinsta** в скачанном
виде, размечены тегами по дереву, и через бота @libinstabot их можно искать по дереву тегов и по тексту.

## 2. Архитектура (зафиксирована, проверена сквозным прогоном 29.09.2026)

> Изменение 30.09.2026: скачивалка переезжает в отдельный приватный Go-репо `padington/libinsta-dl` (свои секреты, свой runner на том же VPS). Python-код VPS в этом репо (`vps_*.py`, `Dockerfile.vps`) — референс, после переключения бэклога удаляется. Разметка остаётся здесь (Mac, Python). Поиск — вариант A (см. итерацию 4).

| Узел | Роль | Факты |
|---|---|---|
| **VPS** `vps-debian12` | Всё, что ходит в Instagram + постоянный бот-сервис | Стокгольм, Debian 12, 1 CPU / 2 GB RAM, python3.11, docker; **нет** ffmpeg, python3-venv, sudo. Self-hosted GitHub Actions runner репо `padington/tgbase`. Всё запускается в docker (`python:3.11-slim`). Instagram отсюда доступен, cookie-сессия работает (media/info и direct_v2/inbox проверены) |
| **Mac владельца** | Разметка (whisper-cli + ollama qwen2.5vl/llama3.2), `reels.db` — источник истины, скрипт-разметчик запускается вручную | `~/reels-catalog` (рабочая копия репо с данными, `.venv`, `media/`). Instagram с Mac **недоступен** |
| **Канал libinsta** | Хранилище видео/фото + подписи с тегами | `chat_id = -1004300487255`, бот @libinstabot — админ (постить, редактировать) |
| **GitHub** | код, workflows, секреты, индекс для поиска | `padington/catalog-ea269e956d1b` ветка `vps-download` (код VPS + локальный код), `padington/tgbase` (workflows: `ig-download.yml`, `libinsta-deploy.yml`, `ig-probe.yml`; секреты `IG_SESSION_JSON`, `LIBINSTA_BOT_TOKEN`; runner) |

Поток:
```
ссылка от владельца ──► @libinstabot (VPS) ──► IG download ──► sendVideo/sendMediaGroup ──► канал, подпись + #nonparsed
бэклог reels.db ──► пачки (GH Actions, VPS) ──────────────────────────────────────────────► канал, подпись + #nonparsed
Mac: parse_channel.py ──► посты с #nonparsed ──► скачать (MTProto) ──► ASR+VLM+LLM ──► теги по дереву ──► editMessageCaption (#теги вместо #nonparsed)
                     └──► index.json (message_id → теги, текст) ──► GitHub ──► бот на VPS: /tags дерево, /find текст ──► copyMessage
```

Секреты и авторизация:
- Instagram: **только** cookie-сессия instagrapi (`ig_session.json`, создана `login.py` + 2FA). Лежит в секрете `IG_SESSION_JSON` (base64). Пароля нет нигде. Если IG отвечает `login_required`/`challenge` — остановиться и сообщить владельцу; перелогин делает он.
- Telegram: токен бота в `~/reels-catalog/.env` (`TELEGRAM_BOT_TOKEN`, 600) и в секрете `LIBINSTA_BOT_TOKEN`. Локально грузить `set -a; . .env; set +a`. **Значение никогда не печатать** в вывод, логи, коммиты, issue.
- Секрет `TELEGRAM_BOT_TOKEN` в tgbase — токен ДРУГОГО бота, не трогать.
- Локальный разметчик читает канал по MTProto (Telethon): нужны `TG_API_ID`/`TG_API_HASH` (my.telegram.org) в `.env`. Сначала пробуем bot-режим Telethon (токен бота); если Telegram не даёт боту историю — user-сессия (телефон+код, интерактивно, делает владелец).

## 3. Контракты

### 3.1 Подпись поста в канале (≤ 1024 символа; загрузчик оставляет ≥ 150 символов под хэштеги, т.е. с `#nonparsed` ≤ 874)
```
<текст подписи IG, обрезанный по лимиту>

https://www.instagram.com/reel/<code>/
@<автор> · <YYYY-MM-DD> [· from <кто прислал в DM>]
#pk<digits> #dm #nonparsed                ← до разметки (30.09: pk + источник link|dm|backlog)
#pk<digits> #dm #cooking #pasta #italy    ← после разметки (категория первой, потом теги)
```
Последняя строка — только хэштеги через пробел: `#pk…` (id медиа IG, кликабельный, ключ дедупа и rebuild), источник (`#link` — запрос через бота, `#dm` — синк переписки, `#backlog` — миграция), затем `#nonparsed` либо теги. Разметчик заменяет только часть после `#pk… #src`. Всё выше не меняется (для `#dm` в строке «from …» — ещё дата шары в переписке).

### 3.2 Виды постов
- видео (`clips`, `feed` video, карусель с видео — первый видео-слайд): `sendVideo` **обязательно** с `width`, `height`, `duration` и `thumbnail` (jpeg ≤ 320 px, < 200 KB; брать из `image_versions2.candidates` IG) — без них Telegram ломает соотношение сторон (проверено).
- фото / фото-карусель: `sendMediaGroup` до 10 фото по URL с IG CDN, подпись на первом.
- недоступные (IG вернул пустой `items`): текстовое сообщение `⚠️ unavailable` + подпись по 3.1 + `#unavailable` (не `#nonparsed`).

### 3.3 `reels.db` (Mac) — новые колонки
`tg_message_id INTEGER, tg_file_id TEXT, tg_kind TEXT ('video'|'photos'|'text'), tg_posted_at INTEGER, tags_final TEXT (JSON), parsed_at INTEGER`.
Пост из ссылки (не из DM) добавляется в `reels` с `source='link'`.

### 3.4 Дерево тегов `tags_tree.yaml` (репо, ветка `vps-download`)
```yaml
- id: cooking            # уровень 1 = категория (26 существующих из categorize.py + при необходимости новые)
  title: Готовка
  children:
    - id: pasta
      title: Паста
      aliases: [pasta-recipe, паста, макароны]   # свободные теги LLM, которые схлопываются в этот узел
    - id: baking ...
```
Правила: id — `[a-z0-9_]+`, это и есть хэштег (`#pasta`). Глубина ≤ 3. Тег, не попавший ни в один узел, идёт в `#misc_<slug>` только если встретился ≥ 5 раз, иначе отбрасывается. Дерево строится один раз из существующих 1841 размеченных постов (итерация 3), потом правится руками.

### 3.5 `index.json` (для поиска, публикуется в GitHub)
```json
{"version": 1, "built_at": 1759180000, "tree": [...как tags_tree.yaml...],
 "posts": [{"m": 123, "k": "video", "t": ["cooking","pasta"], "d": "2026-06-13", "a": "author", "s": "первые 200 символов caption+transcript"}]}
```
Где хранить: приватный репо `padington/libinsta-index`, файл `index.json`, бот на VPS тянет его через GitHub API с токеном `GH_INDEX_TOKEN` (секрет) раз в 10 мин или по `/reload`.

## 4. Итерации

Каждая итерация — отдельная ветка `iter/<N>-<slug>` от `vps-download` в `padington/catalog-ea269e956d1b` (workflows — ветка `probe/ig-net` в `padington/tgbase`), PR в `vps-download` с описанием «что проверено и как». Тесты: `PYTHONPATH=. .venv/bin/python -m unittest discover -s tests` в `~/reels-catalog` должны быть зелёными; новые модули — с unit-тестами на чистых функциях (без сети).

### Итерация 1 — VPS-сервис «скачивалка по ссылкам» (`vps_service.py`, `Dockerfile.vps`, `libinsta-deploy.yml`)
Черновик уже в ветке. Довести:
1. Разбор ссылок из текста/подписи, `media_pk_from_code`, дедуп через `/data/seen.json`, ответ владельцу (✅ msg id / ❌ причина).
2. Все три вида постов по 3.2. `#nonparsed` в подписи.
3. Ограничение доступа: `OWNER_IDS` (repo variable `LIBINSTA_OWNER_IDS` в tgbase); пока пусто — принимать от всех, логировать id. Команды `/start`, `/status`, `/help`.
4. Устойчивость: throttle IG (429/challenge/login_required) → пауза 15 мин и сообщение владельцу; сервис не падает; лог в stdout; `restart unless-stopped`.
5. Деплой: `gh workflow run libinsta-deploy.yml --repo padington/tgbase --ref probe/ig-net`; проверить `docker logs`. Проверка «глазами»: отправить боту ссылку, увидеть пост в канале с правильным соотношением сторон.
6. Тесты: разбор ссылок, сборка подписи (лимит 1024, обрезка), выбор thumbnail, выбор медиа из carousel.

### Итерация 2 — бэклог: все 3264 из `reels.db` → канал
1. `backlog.py` (Mac): выбирает `pk` с `tg_message_id IS NULL`, пишет `batches/current.json` (по 150 pk: pk, shortcode, shared_by, caption из DM как fallback), коммитит в tgbase `probe/ig-net`, запускает `ig-download.yml`, ждёт, скачивает артефакт `results.jsonl`, записывает в `reels.db` (3.3). Цикл до конца бэклога; при `throttled` — пауза ≥ 1 ч, `DELAY` ×1.5.
2. `vps_download.py` использовать общий код с сервисом (вынести `post_media`, `caption_for` в `vps_common.py`), `#nonparsed`, фото и unavailable по 3.2, порядок — от новых к старым.
3. Отчёт по итогу: сколько sent/photos/unavailable/failed, список failed с причинами в `backlog_report.md`.
4. Не запускать полный прогон без явного ОК владельца — только пачку 150 для проверки, затем отчитаться.

### Итерация 3 — локальный разметчик `parse_channel.py` (Mac)
1. Telethon: подключиться к каналу, найти посты, у которых последняя строка подписи = `#nonparsed` (поиск `search='#nonparsed'` + проверка). Проверить bot-режим; если нельзя — user-сессия (`tg_user.session` в `.gitignore`).
2. Для каждого: скачать медиа в `media/<pk>.mp4` (pk — из ссылки в подписи; для фото — `media/<pk>/NN.jpg`), завести/обновить строку в `reels.db`, прогнать существующие стадии очереди (`extract_audio`, `transcribe`, `sample_frames`, `describe_frames`, `categorize`, `tags`; для фото — только `describe_frames` по самим фото).
3. Нормализация: `tags_tree.yaml` + `tag_normalize.py` (категория → узел 1 уровня, свободные теги → по aliases, остальное по правилу 3.4). Первую версию дерева собрать скриптом из 1841 уже размеченных постов (частоты, синонимы через llama3.2), положить в репо, показать владельцу.
4. `editMessageCaption`: заменить последнюю строку на хэштеги (3.1). Записать `tags_final`, `parsed_at`.
5. Собрать `index.json` (3.5) и запушить в `padington/libinsta-index` (создать приватный репо через `gh repo create`).
6. CLI: `parse_channel.py [--limit N] [--dry-run] [--only-index]`, идемпотентно, прерывание безопасно. Прогресс в stdout. Тесты на нормализацию и на замену последней строки подписи.

### Итерация 4 — поиск (РЕШЕНИЕ 30.09: вариант A, без бота)
Поиск — штатный Telegram: тап по хэштегу в канале. Дерево тегов — закреплённое сообщение в канале: иерархический список из `tags_tree.yaml`, каждый узел — кликабельный `#хэштег` с количеством постов; обновляется разметчиком вместе с индексом (`parse_channel.py --pin-tree`, `editMessageText` того же сообщения, id хранится в reels.db/meta). `index.json` остаётся для будущего бота поиска (отдельный проект, отдельный токен, если понадобится). Референс логики бота — draft PR #11 (не мержить).
Прежний текст итерации 4 (бот в `vps_service.py`) — отменён.

<details><summary>старый текст</summary>

1. Загрузка `index.json` из GitHub (секрет `GH_INDEX_TOKEN`), кэш в `/data`, `/reload`.
2. `/tags` — инлайн-клавиатура по дереву: уровень 1 → дети (с количеством постов) → список постов страницами по 5: `copyMessage` из канала владельцу + кнопки «дальше/назад/вверх».
3. `/find <слова>` — поиск по полю `s` и тегам (простое совпадение слов, без внешних БД), тот же вывод.
4. `/random [tag]`.
5. Тесты на навигацию по дереву и поиск (чистые функции над index.json-фикстурой).

</details>

### Итерация 5 — эксплуатация
1. Инкрементальный DM-scrape на VPS (`mode=inbox` → новые шары → те же пачки), по расписанию раз в сутки (`schedule` в workflow).
2. Обновить README обоих репо, описать секреты/переменные, ротацию IG-сессии.
3. Снести `ig-probe.yml`, слить `probe/ig-net` → `main` tgbase, `vps-download` → `main` catalog.

## 5. Правила для исполнителей
- Работать в клоне репо; **не** трогать `~/reels-catalog/reels.db` кроме как через свой код с бэкапом (`cp reels.db reels.db.bak-<date>` перед миграцией схемы).
- Секреты: только через env; в PR/issue/логах — никогда. `.env`, `*.session`, `*.db`, `media/` — в `.gitignore`.
- Перед PR: тесты зелёные, ручная проверка на 1–3 постах в канале, в описании PR — что именно проверено.
- Ничего массового (> 150 постов в канал, полный прогон разметки) без ОК владельца.
- Instagram трогать только с VPS. Bot API `getUpdates` использует только сервис на VPS — локально его не вызывать (конфликт long-polling).
