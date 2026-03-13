import os
import csv
import io
import json
import logging
import re
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from anthropic import Anthropic
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes,
)
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# ── State ────────────────────────────────────────────────
user_conversations: dict[int, list] = {}
# Multi-step state: {"state": "awaiting_inbox_details", "task": "...", "from": "..."}
user_states: dict[int, dict] = {}

# ── Config ────────────────────────────────────────────────
SHEET_URL = os.environ.get(
    "SHEET_URL",
    "https://docs.google.com/spreadsheets/d/12PGjDfUKdpo0oCPJXWIJXigEC78cIchhd2ySyfzJkc4/export?format=csv&gid=469759902"
)
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "")

CACHE_TTL = 300
sheet_cache: dict = {"data": None, "updated_at": None}
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# ── Prompts ───────────────────────────────────────────────
SYSTEM_PROMPT = """\
Ты — персональный трекер задач и проектов креативного отдела.
Помогаешь сотрудникам креатива и менеджерам других отделов видеть картину целиком и двигаться вперёд.

ТОНАЛЬНОСТЬ
Общайся как умный крутой коллега, а не как база данных.
Короткие фразы, живой язык, без канцелярита.
Никаких «Отлично! Ваш запрос обработан.» Можно шутить — уместно и без натяжки.

ФОРМАТИРОВАНИЕ
Только обычный текст — никаких *, **, #, _, ~, таблиц, вертикальных черт.
Пиши как сообщение в Telegram: короткие абзацы, пустая строка между блоками.
Без заголовков-секций. Максимум 5–7 строк на ответ. Если нужно больше — предложи развернуть.

ВМЕСТО СПИСКОВ — ИТОГИ
Сначала скажи главное: что в фокусе, что застряло, что закрыть сегодня. Детали — по запросу.
Плохо: «- Задача 1 в работе / - Задача 2 в работе»
Хорошо: «Из активного — 3 задачи, две по проекту X. Задача Y висит 5 дней без движения — стоит разобраться.»

ВОПРОСЫ О СТАТУСЕ
Любой вопрос типа «что в работе», «статус», «что делаем», «покажи задачи» — отвечай по данным текущего спринта.
Данные всегда есть в контексте. Никогда не говори «не знаю» или «нет данных».

ЕСЛИ ВОПРОС НЕ ПО ТЕМЕ
Одна фраза, без объяснений что умеешь / не умеешь.
Примеры: «Это не по моей части.» / «Спроси что-нибудь по делу.» Можно с иронией.

МЕНЕДЖЕРЫ И ОТВЕТСТВЕННЫЕ
Колонка From — постановщик задачи = менеджер проекта = ответственный.
Если спрашивают «кто менеджер», «кто ответственный», «с кем согласовывать» — смотри на From.
Если в комментарии (Com) написано уточнить у конкретного человека — упомяни его @ником в ответе.
{managers}
ГРУППИРОВКА И АНАЛИЗ
Сам группируй задачи по проектам или статусам — не жди, пока спросят.
Замечай паттерны: что накапливается, что давно не двигается, что срочно.
Никогда не делай выводов о том, что конкретный человек перегружен — только общая картина по отделу.

УТОЧНЯЙ, ЕСЛИ НУЖНО
Если запрос неоднозначный — один короткий вопрос, не несколько. Не додумывай молча.

ДАННЫЕ
Приоритеты: П1 ГОРИМ — сдать первым / П2 — обычный темп / П3 — не горит / Done — закрыто / cancel — неактуально
Колонки: Task — задача, Lid/Lid#2 — ответственные лиды, Priority — приоритет, From — постановщик/менеджер, DD — дедлайн, Com — комментарии
Сравнивай дедлайны с сегодняшней датой ({today}) — называй что горит, что на подходе.
Читай Com — там детали, всегда учитывай.

Отвечай на языке пользователя.\
"""

EXTRACT_TASK_PROMPT = """\
Пользователь хочет добавить задачу в спринт-таблицу. Извлеки данные из его сообщения.

Верни ТОЛЬКО валидный JSON (без markdown, без ```), со следующими полями:
- "task": название задачи/проекта (обязательно)
- "priority": приоритет — "П1 ГОРИМ", "П2" или "П3" (по умолчанию "П2")
- "dd": дедлайн в формате ДД.ММ.ГГГГ (если указан, иначе "")
- "lid": ответственный лид (если указан, иначе "")
- "lid2": второй лид (если указан, иначе "")
- "from": от кого задача (если указано, иначе "")
- "com": комментарий (если есть, иначе "")
"""

EXTRACT_INBOX_PROMPT = """\
Пользователь передаёт входящую задачу. Извлеки из сообщения:
1. Описание задачи (что нужно сделать)
2. От кого задача (имя/ник, если упомянуто)

Верни ТОЛЬКО валидный JSON без markdown:
{"task": "описание задачи", "from": "от кого или пустая строка"}
"""

# ── Managers: name → Telegram @username ───────────────────
MANAGERS: dict[str, str] = {
    # Менеджеры
    "Костя": "@sunshine_insomnia",
    "Костя Поляничев": "@sunshine_insomnia",
    "Поляничев": "@sunshine_insomnia",
    "Леша": "@cheeenoo",
    "Лёша": "@cheeenoo",
    "Алексей": "@cheeenoo",
    "Леша Ксенофонтов": "@cheeenoo",
    "Ксенофонтов": "@cheeenoo",
    "Света": "@Sveta_enjoy",
    "Андрей": "@funkitright",
    "Андрей Морозов": "@funkitright",
    "Морозов": "@funkitright",
    "Саша": "@alexa_moiseeva",
    "Саша Моисеева": "@alexa_moiseeva",
    "Моисеева": "@alexa_moiseeva",
    "Алена": "@romanovskaya_aln",
    "Алёна": "@romanovskaya_aln",
    "Маша": "@maryanash",
    "Илья": "@Daikon25",
    "Илья Викторович": "@Daikon25",
    "Роза": "@rosanna_oganyan",
    "Таня": "@TannyaT",
    "Марина": "@MarinaGlmzv",
    "Евгения": "@mevgeniia",
    "Женя": "@mevgeniia",
    "Лера": "@lmorgunova",
    # Сотрудники креатива
    "Олег": "@obsky",
    "Миша": "@mksktn",
    "Настя Девяткина": "@anastasia_9d10d",
    "Настя Арончик": "@aronchik_a",
    "Настя": "@anastasia_9d10d",
}

EXTRACT_COMMENT_PROMPT = """\
Пользователь хочет добавить или обновить комментарий к задаче в спринте.

Верни ТОЛЬКО валидный JSON без markdown:
{"task": "название задачи или проекта", "com": "текст комментария"}

Если название задачи неясно — верни task как пустую строку.
"""

# ── Intent detection ──────────────────────────────────────
UPDATE_COMMENT_RE = re.compile(
    r"^(добавь комментарий|обнови комментарий|добавь ком к|обнови ком к|добавь заметку к)",
    re.IGNORECASE
)
ADD_KEYWORDS = re.compile(
    r"^(добавь|добавить|создай|создать|запиши|новая задача|новый проект|внеси)",
    re.IGNORECASE
)
INBOX_KEYWORDS = re.compile(
    r"(входящ|задача от|есть задача|передай задачу|пришла задача)",
    re.IGNORECASE
)
# Group trigger: «Огент»/«огент» only (not «агент»)
OGET_RE = re.compile(r"[оО]гент[,!?.]?\s*")


# ── Sheet parsing ─────────────────────────────────────────

def parse_current_sprint(csv_text: str) -> tuple[str, str]:
    reader = csv.reader(io.StringIO(csv_text))
    all_rows = list(reader)

    if len(all_rows) < 2:
        return "", ""

    headers = [h.strip() for h in all_rows[0]]

    last_sprint_idx = 0
    sprint_name = "Текущий спринт"

    for i, row in enumerate(all_rows[1:], start=1):
        for cell in row:
            if "Запланированные задачи" in cell:
                last_sprint_idx = i
                sprint_name = cell.strip()
                break

    task_rows = all_rows[last_sprint_idx + 1:]

    tasks = []
    for row in task_rows:
        if not any(cell.strip() for cell in row):
            continue

        t = {}
        for j, val in enumerate(row):
            if j < len(headers) and headers[j]:
                t[headers[j]] = val.strip()

        name = t.get("Task", "")
        if not name or "Запланированные задачи" in name:
            continue

        num = t.get("№", t.get("#", ""))
        lines = []
        prefix = f"{num}. " if num else "- "
        lines.append(f"{prefix}{name}")

        if t.get("Lid"):
            lid_str = t["Lid"]
            if t.get("Lid #2"):
                lid_str += f", {t['Lid #2']}"
            lines.append(f"   Лид: {lid_str}")

        if t.get("Priority"):
            lines.append(f"   Приоритет: {t['Priority']}")

        if t.get("From"):
            lines.append(f"   От: {t['From']}")

        if t.get("DD"):
            lines.append(f"   Дедлайн: {t['DD']}")

        if t.get("Com"):
            lines.append(f"   Ком: {t['Com']}")

        tasks.append("\n".join(lines))

    return sprint_name, "\n\n".join(tasks)


def fetch_sheet() -> str | None:
    now = datetime.now()
    cached = sheet_cache["data"]
    updated = sheet_cache["updated_at"]

    if cached and updated and (now - updated) < timedelta(seconds=CACHE_TTL):
        return cached

    try:
        resp = requests.get(SHEET_URL, timeout=10)
        resp.raise_for_status()
        resp.encoding = "utf-8"

        sprint_name, tasks_text = parse_current_sprint(resp.text)

        if not tasks_text:
            logger.warning("No tasks found in current sprint")
            return cached

        result = f"Спринт: {sprint_name}\n\n{tasks_text}"
        sheet_cache["data"] = result
        sheet_cache["updated_at"] = now
        logger.info("Sprint refreshed: %s", sprint_name)
        return result

    except Exception as e:
        logger.error("Sheet fetch error: %s", e)
        return cached


def build_base_prompt() -> str:
    """SYSTEM_PROMPT with today's date and managers list (no sprint data)."""
    today = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")
    if MANAGERS:
        lines = [f"- {name}: {tg}" for name, tg in MANAGERS.items()]
        managers_str = "Telegram-ники менеджеров:\n" + "\n".join(lines) + "\n"
    else:
        managers_str = ""
    return SYSTEM_PROMPT.format(today=today, managers=managers_str)


def build_system_prompt() -> str:
    """Full system prompt with sprint data appended (for Q&A)."""
    prompt = build_base_prompt()
    data = fetch_sheet()
    if not data:
        return prompt
    return prompt + f"\n\n---\nДанные текущего спринта:\n\n{data}\n---"


# ── Write to sheet via Apps Script ────────────────────────

def post_to_apps_script(payload: dict) -> bool:
    if not APPS_SCRIPT_URL:
        return False
    try:
        resp = requests.post(APPS_SCRIPT_URL, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error("Apps Script error: %s", e)
        return False


def query_apps_script(payload: dict) -> str | None:
    """Post to Apps Script and return the raw response text (or None on error)."""
    if not APPS_SCRIPT_URL:
        return None
    try:
        resp = requests.post(APPS_SCRIPT_URL, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.text.strip()
    except Exception as e:
        logger.error("Apps Script error: %s", e)
        return None


# ── Incoming task flow ────────────────────────────────────

async def handle_inbox_start(update: Update, text: str) -> None:
    """Step 1: detect incoming task, extract basic info, ask for details."""
    user_id = update.effective_user.id
    sender_name = (
        update.effective_user.full_name
        or update.effective_user.username
        or "Неизвестный"
    )

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=EXTRACT_INBOX_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```json?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
    except Exception as e:
        logger.error("Inbox extract error: %s", e)
        data = {"task": text, "from": ""}

    task = data.get("task", text).strip()
    from_person = data.get("from", "").strip() or sender_name

    user_states[user_id] = {
        "state": "awaiting_inbox_details",
        "task": task,
        "from": from_person,
    }

    await update.message.reply_text(
        f"Записываю задачу: {task}\n"
        f"От кого: {from_person}\n\n"
        "Есть дополнительные детали, вводные или требования? "
        "(размеры, сроки, форматы, ссылки...)\n"
        "Если нет — просто напиши: нет"
    )


async def handle_inbox_details(update: Update, text: str) -> None:
    """Step 2: receive details, save to Входящие sheet."""
    user_id = update.effective_user.id
    state = user_states.pop(user_id, {})

    task = state.get("task", "")
    from_person = state.get("from", "")

    no_details = re.match(r"^(нет|no|ок|ok|всё|все|пропустить|без деталей)[.,!?]?$",
                          text.strip(), re.IGNORECASE)
    com = "" if no_details else text.strip()

    today = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")
    saved = post_to_apps_script({
        "sheet": "Входящие",
        "task": task,
        "com": com,
        "from": from_person,
        "date": today,
    })

    if saved:
        reply = f"Записал во Входящие:\nЗадача: {task}"
        if com:
            reply += f"\nДетали: {com}"
        reply += f"\nОт: {from_person}\n\nВсё зафиксировано в таблице ✅"
    else:
        reply = (
            f"Задача '{task}' принята, но сохранить в таблицу не получилось "
            "(APPS_SCRIPT_URL не настроен). Обратись к администратору."
        )

    await update.message.reply_text(reply)


# ── Sprint add flow ───────────────────────────────────────

async def handle_add_sprint_task(update: Update, text: str) -> None:
    if not APPS_SCRIPT_URL:
        await update.message.reply_text(
            "Добавление в спринт не настроено (нет APPS_SCRIPT_URL)."
        )
        return

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=EXTRACT_TASK_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```json?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        task_data = json.loads(raw)
    except Exception as e:
        logger.error("Task extraction error: %s", e)
        await update.message.reply_text(
            "Не понял задачу. Попробуй: Добавь Название проекта, П1, дедлайн 20 марта"
        )
        return

    task_name = task_data.get("task", "").strip()
    if not task_name:
        await update.message.reply_text("Не нашёл название задачи. Попробуй ещё раз.")
        return

    task_data["sheet"] = "sprint"
    ok = post_to_apps_script(task_data)

    if ok:
        priority = task_data.get("priority", "П2")
        dd = task_data.get("dd", "")
        confirm = f"Добавил в спринт: {task_name} ({priority})"
        if dd:
            confirm += f", дедлайн {dd}"
        confirm += " ✅"
        await update.message.reply_text(confirm)
        sheet_cache["updated_at"] = None  # invalidate cache
    else:
        await update.message.reply_text("Не удалось записать в таблицу.")


# ── Update comment flow ───────────────────────────────────

async def handle_update_comment(update: Update, text: str) -> None:
    """Extract project name + comment text, update Com field via Apps Script."""
    if not APPS_SCRIPT_URL:
        await update.message.reply_text("Обновление комментариев не настроено (нет APPS_SCRIPT_URL).")
        return

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=EXTRACT_COMMENT_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```json?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
    except Exception as e:
        logger.error("Comment extract error: %s", e)
        await update.message.reply_text("Не понял. Напиши: «Добавь комментарий к [проект]: [текст]»")
        return

    task_name = data.get("task", "").strip()
    com = data.get("com", "").strip()

    if not task_name:
        await update.message.reply_text("Не понял, к какому проекту. Напиши: «Добавь комментарий к [проект]: [текст]»")
        return
    if not com:
        await update.message.reply_text("Не нашёл текст комментария.")
        return

    result = query_apps_script({"action": "update_comment", "task": task_name, "com": com})
    if result is None:
        await update.message.reply_text("Не удалось связаться с таблицей.")
    elif result == "OK":
        await update.message.reply_text(f"Комментарий к «{task_name}» обновлён ✅")
        sheet_cache["updated_at"] = None
    elif "NOT FOUND" in result:
        await update.message.reply_text(
            f"Не нашёл задачу «{task_name}» в текущем спринте. Уточни название."
        )
    else:
        logger.warning("Apps Script unexpected response: %s", result)
        await update.message.reply_text(f"Комментарий к «{task_name}» обновлён ✅")
        sheet_cache["updated_at"] = None


# ── Handlers ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Я траффик-бот креативного отдела. Свой Агент в команде — знаю все проекты, "
        "спринты, дедлайны и статусы. Вижу, что горит, что скоро загорится, что уже закрыто.\n\n"
        "Не занимаюсь креативом сам — это не моё. Но слежу, чтобы всё сдавали вовремя "
        "и ничего не разваливалось.\n\n"
        "/sprint — статус недельного спринта\n"
        "/hot — самое горящее на сегодня\n"
        "/task — добавить задачу в спринт\n"
        "/help — что умею"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/sprint — статус недельного спринта по всем проектам\n"
        "/hot — самое горящее в работе на сегодня — проекты П1 и ближайшие дедлайны\n"
        "/task — добавить задачу в спринт\n"
        "/clear — сбросить контекст разговора\n\n"
        "Также можно просто написать:\n"
        "- Что в работе? Что горит на сегодня?\n"
        "- Добавь задачу такую-то, дедлайн через неделю\n\n"
        "В группе отвечаю на @упоминание или по имени Огент"
    )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    user_conversations.pop(uid, None)
    user_states.pop(uid, None)
    await update.message.reply_text("Готово, начнём заново ✨")


async def setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run this command in a group to register it for morning digest."""
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "Эту команду нужно запустить прямо в группе, куда слать утреннюю сводку."
        )
        return
    await update.message.reply_text(
        f"ID этой группы: {chat.id}\n\n"
        f"Добавь в Railway переменную:\n"
        f"TELEGRAM_GROUP_ID = {chat.id}"
    )


async def sprint_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Full sprint report."""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    data = fetch_sheet()
    if not data:
        await update.message.reply_text("Не удалось загрузить таблицу.")
        return

    try:
        today = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system=build_base_prompt(),
            messages=[{
                "role": "user",
                "content": (
                    f"Данные спринта:\n\n{data}\n\n"
                    f"Сегодня {today}. Полный статус спринта по приоритетам.\n"
                    "Сначала все П1 ГОРИМ, потом все П2, потом П3 — каждая группа отдельным абзацем с подписью приоритета. "
                    "Внутри каждой группы — список через дефис: название, дедлайн если есть, комментарий если есть. "
                    "Задачи Done и cancel не включать. "
                    "В конце одна строка: сколько всего активных задач."
                )
            }]
        )
        await _send(update, resp.content[0].text)
    except Exception as e:
        logger.error("Sprint error: %s", e)
        await update.message.reply_text("Ошибка при генерации сводки.")


async def hot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Only П1 tasks."""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    data = fetch_sheet()
    if not data:
        await update.message.reply_text("Не удалось загрузить таблицу.")
        return

    try:
        today = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=build_base_prompt(),
            messages=[{
                "role": "user",
                "content": (
                    f"Данные спринта:\n\n{data}\n\n"
                    f"Сегодня {today}. Что горит прямо сейчас:\n"
                    "1. Все П1 ГОРИМ — название, дедлайн, комментарий если есть.\n"
                    "2. Задачи с дедлайном сегодня или в ближайшие 2 дня (любой приоритет, кроме Done/cancel) — если П1 уже не вошли.\n"
                    "Если ничего срочного нет — так и скажи. Коротко и по делу."
                )
            }]
        )
        await _send(update, resp.content[0].text)
    except Exception as e:
        logger.error("Hot error: %s", e)
        await update.message.reply_text("Ошибка при генерации.")


async def task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a task to sprint. If args provided — parse directly. Else — ask."""
    if context.args:
        text = " ".join(context.args)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        await handle_add_sprint_task(update, text)
    else:
        user_id = update.effective_user.id
        user_states[user_id] = {"state": "awaiting_task_input"}
        await update.message.reply_text(
            "Опиши задачу — название, приоритет и дедлайн если есть.\n"
            "Пример: Баннер для Сбера, П1, дедлайн 20 марта"
        )


# keep /report as alias for backward compat
async def report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await sprint_cmd(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text
    user_id = update.effective_user.id
    chat_type = update.message.chat.type
    is_group = chat_type in ("group", "supergroup")

    # ── Group: respond only to @mention or «Огент»/«огент» ──
    if is_group:
        bot_username = context.bot_data.get("username", "")
        at_mention = bool(bot_username) and f"@{bot_username}" in text.lower()
        oget_mention = bool(OGET_RE.search(text))
        if not at_mention and not oget_mention:
            return
        # Strip both triggers before processing
        if bot_username:
            text = re.sub(rf"@{re.escape(bot_username)}", "", text, flags=re.IGNORECASE).strip()
        text = OGET_RE.sub("", text).strip()
        if not text:
            text = "Что в работе сегодня?"

    # ── Multi-step state ──
    state_info = user_states.get(user_id, {})

    if state_info.get("state") == "awaiting_task_input":
        user_states.pop(user_id, None)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        await handle_add_sprint_task(update, text)
        return

    if state_info.get("state") == "awaiting_inbox_details":
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        await handle_inbox_details(update, text)
        return

    # ── Update comment ──
    if UPDATE_COMMENT_RE.match(text.strip()):
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        await handle_update_comment(update, text)
        return

    # ── Add to sprint ──
    if ADD_KEYWORDS.match(text.strip()):
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        await handle_add_sprint_task(update, text)
        return

    # ── Incoming task ──
    if INBOX_KEYWORDS.search(text):
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        await handle_inbox_start(update, text)
        return

    # ── Normal Q&A ──
    if user_id not in user_conversations:
        user_conversations[user_id] = []

    user_conversations[user_id].append({"role": "user", "content": text})
    messages = user_conversations[user_id][-20:]

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=build_system_prompt(),
            messages=messages,
        )
        reply = resp.content[0].text
    except Exception as e:
        logger.error("API error: %s", e)
        await update.message.reply_text("Ошибка AI, попробуй ещё раз.")
        return

    user_conversations[user_id].append({"role": "assistant", "content": reply})
    await _send(update, reply)


async def _send(update: Update, text: str) -> None:
    if len(text) <= 4096:
        await update.message.reply_text(text)
    else:
        for i in range(0, len(text), 4096):
            await update.message.reply_text(text[i:i + 4096])


# ── Main ──────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    """Cache bot username at startup so group mention detection is fast & reliable."""
    me = await app.bot.get_me()
    app.bot_data["username"] = me.username.lower()
    logger.info("Bot username cached: @%s", me.username)


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("sprint", sprint_cmd))
    app.add_handler(CommandHandler("hot", hot_cmd))
    app.add_handler(CommandHandler("task", task_cmd))
    app.add_handler(CommandHandler("report", report))    # alias → sprint
    app.add_handler(CommandHandler("fire", hot_cmd))     # alias → hot
    app.add_handler(CommandHandler("setgroup", setgroup))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "edited_message", "callback_query"],
    )


if __name__ == "__main__":
    main()
