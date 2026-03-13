import os
import csv
import io
import json
import logging
import re
import requests
from datetime import datetime, timedelta, time as dtime
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
TELEGRAM_GROUP_ID = os.environ.get("TELEGRAM_GROUP_ID", "")

CACHE_TTL = 300
sheet_cache: dict = {"data": None, "updated_at": None}
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# ── Prompts ───────────────────────────────────────────────
SYSTEM_PROMPT = """\
Ты Агент — траффик-бот креативного отдела. Знаешь все проекты, спринты, дедлайны и статусы.

Стиль общения:
- Деловой, уверенный, без лишних слов
- Никаких звёздочек, **жирного**, markdown-разметки, таблиц
- Коротко и по делу — как сообщение в рабочем чате
- Без лишних метафор и пафоса
- Эмодзи можно, но редко и по делу
- Одну короткую иронию или сухой юмор в конце — если уместно, не натужно

Как работать с данными:
- Сравнивай дедлайны (DD) с сегодняшней датой — называй что горит, что на подходе
- Читай комментарии (Com) — там детали по задачам, всегда учитывай
- Приоритеты важнее имён — говори П1, П2, П3, не перечисляй всех подряд
- Если спрашивают конкретно про человека — отвечай

Приоритеты:
- П1 ГОРИМ — сдать первым, срочно
- П2 — в работе, обычный темп
- П3 — не горит
- Done — закрыто
- cancel / loser — неактуально

Колонки:
- Task — задача/проект
- Lid, Lid #2 — ответственные
- Priority — приоритет
- From — постановщик задачи
- DD — дедлайн
- Com — комментарии

Сегодня: {today}

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

# ── Intent detection ──────────────────────────────────────
ADD_KEYWORDS = re.compile(
    r"^(добавь|добавить|создай|создать|запиши|новая задача|новый проект|внеси)",
    re.IGNORECASE
)
INBOX_KEYWORDS = re.compile(
    r"(входящ|задача от|есть задача|передай задачу|пришла задача)",
    re.IGNORECASE
)


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


def build_system_prompt() -> str:
    today = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")
    prompt = SYSTEM_PROMPT.format(today=today)
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


# ── Morning digest job ─────────────────────────────────────

async def morning_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily 10:00 Moscow digest to the group."""
    if not TELEGRAM_GROUP_ID:
        logger.info("Morning digest skipped: TELEGRAM_GROUP_ID not set")
        return

    data = fetch_sheet()
    if not data:
        logger.warning("Morning digest: no sheet data")
        return

    today = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")
    prompt = SYSTEM_PROMPT.format(today=today)

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=prompt,
            messages=[{
                "role": "user",
                "content": (
                    f"Данные спринта:\n\n{data}\n\n"
                    f"Сегодня {today}. Утренняя сводка для команды:\n"
                    "- Что горит сегодня (П1)\n"
                    "- Что планируем сдать сегодня (дедлайн сегодня или вчера)\n"
                    "- Что сделали (Done)\n"
                    "- Общая картина дня\n"
                    "Пиши бодро и кратко. Без звёздочек, без markdown."
                )
            }]
        )
        text = resp.content[0].text
        await context.bot.send_message(chat_id=int(TELEGRAM_GROUP_ID), text=text)
        logger.info("Morning digest sent to group %s", TELEGRAM_GROUP_ID)
    except Exception as e:
        logger.error("Morning digest error: %s", e)


# ── Handlers ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Я траффик-бот креативного отдела. Свой Агент в команде — знаю все проекты, "
        "спринты, дедлайны и статусы. Вижу, что горит, что скоро загорится, что уже закрыто.\n\n"
        "Не занимаюсь креативом сам — это не моё. Но слежу, чтобы всё сдавали вовремя "
        "и ничего не разваливалось.\n\n"
        "Можете написать мне задачу — добавлю в спринт.\n\n"
        "/report — сводка по спринту\n"
        "/help — что умею"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Что умею:\n"
        "- Ответить про статус спринта: что горит, что сдаём, кто чем занят\n"
        "- Добавить задачу в спринт: Добавь Название, П1, дедлайн 25 марта\n"
        "- Зафиксировать входящую задачу: Задача от Ани — нужен баннер\n"
        "- В группе отвечаю на @упоминание или слово Агент\n\n"
        "/report — полная сводка по спринту\n"
        "/clear — сбросить контекст разговора"
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


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    data = fetch_sheet()
    if not data:
        await update.message.reply_text("Не удалось загрузить таблицу.")
        return

    try:
        today = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")
        prompt = SYSTEM_PROMPT.format(today=today)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system=prompt,
            messages=[{
                "role": "user",
                "content": (
                    f"Данные спринта:\n\n{data}\n\n"
                    f"Сегодня {today}. Сводка как траффик-менеджер:\n"
                    "- Сколько задач в работе, сколько горит\n"
                    "- П1 с дедлайнами\n"
                    "- П2 на подходе\n"
                    "- Просроченные\n"
                    "- Что важного в комментариях\n"
                    "Без звёздочек, без markdown. Чистый текст. Шутку в конце."
                )
            }]
        )
        await _send(update, resp.content[0].text)
    except Exception as e:
        logger.error("Report error: %s", e)
        await update.message.reply_text("Ошибка при генерации отчёта.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text
    user_id = update.effective_user.id
    chat_type = update.message.chat.type
    is_group = chat_type in ("group", "supergroup")

    # ── Group: only respond when mentioned or called "Агент" ──
    if is_group:
        bot_username = (await context.bot.get_me()).username
        mentioned = (
            f"@{bot_username}".lower() in text.lower()
            or re.search(r"\bагент\b", text, re.IGNORECASE)
        )
        if not mentioned:
            return
        # Strip mention from text before processing
        text = re.sub(rf"@{re.escape(bot_username)}", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"\bагент[,!?.]?\s*", "", text, flags=re.IGNORECASE).strip()
        if not text:
            text = "Что в работе сегодня?"

    # ── Multi-step state: user is providing inbox task details ──
    state_info = user_states.get(user_id, {})
    if state_info.get("state") == "awaiting_inbox_details":
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        await handle_inbox_details(update, text)
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

def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("setgroup", setgroup))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Morning digest: every day at 10:00 Moscow time
    if app.job_queue:
        app.job_queue.run_daily(
            morning_digest,
            time=dtime(10, 0, tzinfo=MOSCOW_TZ),
            name="morning_digest",
        )
        logger.info("Morning digest scheduled at 10:00 Moscow time")
    else:
        logger.warning("Job queue not available — morning digest disabled")

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
