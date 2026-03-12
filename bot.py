import os
import logging
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

SYSTEM_PROMPT = "Ты полезный AI-ассистент. Отвечай чётко и по делу."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я AI-ассистент на базе Claude.\n\n"
        "Просто напиши мне что-нибудь и я отвечу.\n\n"
        "/clear — очистить историю диалога\n"
        "/help — помощь"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Как пользоваться:\n"
        "• Пиши любые вопросы — я помню контекст нашего разговора\n"
        "• /clear — начать разговор заново\n"
        "• /help — показать это сообщение"
    )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_conversations.pop(user_id, None)
    await update.message.reply_text("История диалога очищена. Начнём заново!")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_message = update.message.text

    if user_id not in user_conversations:
        user_conversations[user_id] = []

    user_conversations[user_id].append({"role": "user", "content": user_message})

    # Keep last 20 messages to stay within context limits
    messages = user_conversations[user_id][-20:]

    # Show typing indicator
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action="typing"
    )

    try:
        response = anthropic.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
