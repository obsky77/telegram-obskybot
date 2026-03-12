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

anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Conversation history per user (in-memory)
user_conversations: dict[int, list] = {}

SHEET_URL = os.environ.get(
    "SHEET_URL",
    "https://docs.google.com/spreadsheets/d/12PGjDfUKdpo0oCPJXWIJXigEC78cIchhd2ySyfzJkc4/export?format=csv&gid=469759902"
)
CACHE_TTL = 300  # 5 минут

sheet_cache: dict = {"data": None, "updated_at": None}

SYSTEM_PROMPT = """Ты крутой бизнес-ассистент креативного отдела. Твой стиль — умный, прямой, без воды.

Как ты работаешь:
- Даёшь конкретные ответы, а не общие фразы
- Говоришь как умный коллега, а не как корпоративный робот
- Структурируешь ответы — списки, шаги, приоритеты
- Если вопрос размытый — уточняешь, что именно нужно
- Иногда можешь сказать неудобную правду, если это поможет

Ты работаешь с еженедельным спринтом креативного отдела. Структура таблицы:
- Task — название проекта/задачи
- Lid — ответственный лид
- Lid #2 — второй ответственный
- Priority — приоритет задачи:
  * П1 ГОРИМ — САМЫЙ СРОЧНЫЙ, горит, нужно сдать в первую очередь
  * П2 — средний приоритет
  * П3 — низкий приоритет, можно подождать
  * Done — задача выполнена
  * cancel — задача отменена
  * loser — задача потеряла актуальность
- From — кто поставил задачу / источник
- DD — дедлайн (дата сдачи)
- Com — комментарии к задаче, важные детали

Как отвечать на типичные вопросы:
- "По каким проектам горим?" → показать все задачи с Priority = "П1 ГОРИМ"
- "Какие задачи сдаём завтра / на этой неделе?" → проверить колонку DD и вывести задачи с ближайшими дедлайнами
- "Что в работе?" → задачи без статуса Done/cancel, отсортированные по приоритету
- "Кто чем занимается?" → сгруппировать задачи по Lid
- Всегда учитывай комментарии (Com) — там важные детали по задачам

Сегодняшняя дата: {today}

Отвечай на том языке, на котором пишет пользователь."""


def fetch_sheet() -> str | None:
    """Читает Google Sheet и возвращает данные как текст. Кэш 5 минут."""
    now = datetime.now()
    cached = sheet_cache["data"]
    updated = sheet_cache["updated_at"]

    if cached and updated and (now - updated) < timedelta(seconds=CACHE_TTL):
        return cached

    try:
        resp = requests.get(SHEET_URL, timeout=10)
        resp.raise_for_status()
        reader = csv.reader(io.StringIO(resp.text))
        rows = [row for row in reader if any(cell.strip() for cell in row)]
        text = "\n".join([" | ".join(row) for row in rows])
        sheet_cache["data"] = text
        sheet_cache["updated_at"] = now
        logger.info("Sheet refreshed: %d rows", len(rows))
        return text
    except Exception as e:
        logger.error("Sheet fetch error: %s", e)
        return cached  # вернём старые данные если есть


def build_system_prompt_with_sheet() -> str:
    """Добавляет данные таблицы в системный промпт."""
    today = datetime.now().strftime("%d.%m.%Y")
    prompt = SYSTEM_PROMPT.format(today=today)
    data = fetch_sheet()
    if not data:
        return prompt
    return (
        prompt
        + f"\n\n---\nАктуальные данные спринта (обновляется каждые 5 минут):\n\n{data}\n---"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я бизнес-ассистент с доступом к вашей рабочей таблице.\n\n"
        "Что умею:\n"
        "• Отвечать на вопросы по задачам и данным из таблицы\n"
        "• /report — аналитическая сводка по таблице\n"
        "• /clear — очистить историю диалога\n"
        "• /help — помощь"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Команды:\n"
        "• /report — сводка по данным из таблицы\n"
        "• /clear — начать диалог заново\n"
        "• /help — это сообщение\n\n"
        "Просто пиши вопросы — я вижу данные из таблицы и отвечаю на их основе."
    )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_conversations.pop(user_id, None)
    await update.message.reply_text("История очищена. Начнём заново!")


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Генерирует аналитическую сводку по таблице."""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    data = fetch_sheet()
    if not data:
        await update.message.reply_text("Не удалось загрузить данные из таблицы.")
        return

    try:
        today = datetime.now().strftime("%d.%m.%Y")
        prompt = SYSTEM_PROMPT.format(today=today)
        response = anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system=prompt,
            messages=[{
                "role": "user",
                "content": (
                    f"Вот данные текущего спринта:\n\n{data}\n\n"
                    f"Сегодня {today}. Дай краткую сводку по спринту:\n"
                    "1. 🔥 П1 ГОРИМ — что горит прямо сейчас\n"
                    "2. 📅 Ближайшие дедлайны — что сдаём в ближайшие дни\n"
                    "3. 🔄 В работе — задачи П2 и П3 в процессе\n"
                    "4. ⚠️ Риски — просроченные или без дедлайна с высоким приоритетом"
                )
            }]
        )
        await update.message.reply_text(response.content[0].text)
    except Exception as e:
        logger.error("Report error: %s", e)
        await update.message.reply_text("Ошибка при генерации отчёта. Попробуй ещё раз.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_message = update.message.text

    if user_id not in user_conversations:
        user_conversations[user_id] = []

    user_conversations[user_id].append({"role": "user", "content": user_message})

    # Keep last 20 messages to stay within context limits
    messages = user_conversations[user_id][-20:]

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        response = anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=build_system_prompt_with_sheet(),
            messages=messages,
        )
        assistant_text = response.content[0].text
    except Exception as e:
        logger.error("Anthropic API error: %s", e)
        await update.message.reply_text("Произошла ошибка при обращении к AI. Попробуй ещё раз.")
        return

    user_conversations[user_id].append({"role": "assistant", "content": assistant_text})

    # Telegram message limit is 4096 chars
    if len(assistant_text) > 4096:
        for i in range(0, len(assistant_text), 4096):
            await update.message.reply_text(assistant_text[i:i + 4096])
    else:
        await update.message.reply_text(assistant_text)


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started with Google Sheets integration")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
