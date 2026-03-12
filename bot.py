import os
import csv
import io
import logging
import requests
from datetime import datetime, timedelta

from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Per-user conversation history (in-memory)
user_conversations: dict[int, list] = {}

SHEET_URL = os.environ.get(
    "SHEET_URL",
    "https://docs.google.com/spreadsheets/d/12PGjDfUKdpo0oCPJXWIJXigEC78cIchhd2ySyfzJkc4/export?format=csv&gid=469759902"
)
CACHE_TTL = 300  # 5 minutes

sheet_cache: dict = {"data": None, "updated_at": None}

SYSTEM_PROMPT = """\
Ты крутой бизнес-ассистент креативного отдела. Стиль — умный коллега в рабочем чате.

ВАЖНО — как отвечать:
- Пиши просто и понятно, как человек в чате, а НЕ как робот
- НЕ используй формат таблиц, pipe-разделители или CSV-вид
- Пиши короткими живыми предложениями
- Можешь использовать эмодзи, но без перебора
- Пример хорошего ответа: "На этой неделе 12 задач в работе, 2 из них горят! 🔥 ВФМ/Екатеринбург и Визитор Центр — оба на Мише, дедлайн 13 марта."

Приоритеты задач:
- П1 ГОРИМ — горящий, сдать первым
- П2 — средний
- П3 — низкий, можно подождать
- Done — сделано
- cancel — отменено
- loser — неактуально

Колонки данных:
- Task — проект/задача
- Lid — ответственный лид
- Lid #2 — второй ответственный
- Priority — приоритет
- From — от кого задача
- DD — дедлайн
- Com — комментарии (важные детали!)

Как отвечать на вопросы:
- "Горим?" / "Что срочное?" → задачи П1 ГОРИМ с лидами и дедлайнами
- "Что сдаём завтра/скоро?" → ближайшие дедлайны
- "Что в работе?" → активные задачи (не Done/cancel/loser), по приоритету
- "Кто чем занят?" → группируй по лиду
- Учитывай комментарии — там детали

Сегодня: {today}

Отвечай на языке пользователя.\
"""


def parse_current_sprint(csv_text: str) -> tuple[str, str]:
    """Parse CSV, find the LAST sprint section, return only those tasks.

    Returns (sprint_name, formatted_text).
    """
    reader = csv.reader(io.StringIO(csv_text))
    all_rows = list(reader)

    if len(all_rows) < 2:
        return "", ""

    # First row = headers
    headers = [h.strip() for h in all_rows[0]]

    # Find ALL "Запланированные задачи" rows — take the LAST one
    last_sprint_idx = 0
    sprint_name = "Текущий спринт"

    for i, row in enumerate(all_rows[1:], start=1):
        for cell in row:
            if "Запланированные задачи" in cell:
                last_sprint_idx = i
                sprint_name = cell.strip()
                break

    # Take only rows AFTER the last sprint header
    task_rows = all_rows[last_sprint_idx + 1:]

    # Build readable task list
    tasks = []
    for row in task_rows:
        if not any(cell.strip() for cell in row):
            continue

        # Map header -> value
        t = {}
        for j, val in enumerate(row):
            if j < len(headers) and headers[j]:
                t[headers[j]] = val.strip()

        name = t.get("Task", "")
        if not name or "Запланированные задачи" in name:
            continue

        # Build a clean readable block for each task
        num = t.get("№", t.get("#", ""))
        lines = []
        prefix = f"{num}. " if num else "• "
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
    """Fetch sprint sheet CSV, parse only current sprint, cache 5 min."""
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
    """System prompt + today's date + current sprint data."""
    today = datetime.now().strftime("%d.%m.%Y")
    prompt = SYSTEM_PROMPT.format(today=today)
    data = fetch_sheet()
    if not data:
        return prompt
    return prompt + f"\n\n---\nДанные текущего спринта:\n\n{data}\n---"


# ── Handlers ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! 👋 Я ассистент креативного отдела.\n\n"
        "Спрашивай про текущий спринт:\n"
        "• По каким проектам горим?\n"
        "• Какие задачи сдаём на этой неделе?\n"
        "• Кто чем занят?\n\n"
        "/report — сводка по спринту\n"
        "/clear — начать заново"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Просто пиши вопросы про спринт — я вижу актуальные задачи.\n\n"
        "Примеры:\n"
        "• Что горит?\n"
        "• Что сдаём завтра?\n"
        "• Что делает Миша?\n\n"
        "/report — полная сводка\n"
        "/clear — очистить историю"
    )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_conversations.pop(update.effective_user.id, None)
    await update.message.reply_text("Готово, начнём заново ✨")


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate a sprint summary."""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    data = fetch_sheet()
    if not data:
        await update.message.reply_text("Не удалось загрузить таблицу 😕")
        return

    try:
        today = datetime.now().strftime("%d.%m.%Y")
        prompt = SYSTEM_PROMPT.format(today=today)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system=prompt,
            messages=[{
                "role": "user",
                "content": (
                    f"Данные спринта:\n\n{data}\n\n"
                    f"Сегодня {today}. Дай короткую живую сводку по спринту:\n"
                    "- Сколько задач всего, сколько горит\n"
                    "- Что именно горит (П1) — назови задачи, лидов, дедлайны\n"
                    "- Ближайшие дедлайны\n"
                    "- Есть ли просроченные\n"
                    "Пиши как в рабочем чате, коротко и по делу."
                )
            }]
        )
        await _send_long_message(update, response.content[0].text)
    except Exception as e:
        logger.error("Report error: %s", e)
        await update.message.reply_text("Ошибка при генерации отчёта 🔄")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle free-text messages."""
    user_id = update.effective_user.id
    text = update.message.text

    if user_id not in user_conversations:
        user_conversations[user_id] = []

    user_conversations[user_id].append({"role": "user", "content": text})
    messages = user_conversations[user_id][-20:]

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=build_system_prompt(),
            messages=messages,
        )
        reply = response.content[0].text
    except Exception as e:
        logger.error("API error: %s", e)
        await update.message.reply_text("Ошибка AI, попробуй ещё раз 😕")
        return

    user_conversations[user_id].append({"role": "assistant", "content": reply})
    await _send_long_message(update, reply)


async def _send_long_message(update: Update, text: str) -> None:
    """Split message if it exceeds Telegram's 4096-char limit."""
    if len(text) <= 4096:
        await update.message.reply_text(text)
    else:
        for i in range(0, len(text), 4096):
            await update.message.reply_text(text[i:i + 4096])


# ── Main ──────────────────────────────────────────────────

def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started — sprint tracker")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
