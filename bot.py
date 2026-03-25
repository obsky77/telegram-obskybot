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
CALL_GROUP_ID = os.environ.get("CALL_GROUP_ID", "")

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

ЧТО Я УМЕЮ — отвечай на это полно, если спрашивают «что ты умеешь», «чем можешь помочь», «что умеет бот»:
- Показываю статус спринта: что в работе, что горит, что по конкретному проекту
- Добавляю задачи в спринт или входящие — в диалоге уточняю детали
- Меняю дедлайн, приоритет, закрываю задачу по запросу
- Нахожу файлы и папки проектов в Google Drive — по названию проекта
- Показываю закрытые задачи (Done) — за эту неделю, прошлую, или все
- Генерирую дайджест — аналитическое саммари спринта
- Даю творческие инсайты от известных креаторов и рекламщиков
- Пересылаю ссылки на звонки (Zoom, Jazz, Meet) боту-рекордеру — кинь ссылку или /call
- Со мной можно общаться цепочкой вопросов — помню контекст разговора

ЕСЛИ ВОПРОС НЕ ПО ТЕМЕ
Одна фраза, без объяснений что умеешь / не умеешь.
Примеры: «Это не по моей части.» / «Спроси что-нибудь по делу.» Можно с иронией.

РОЛИ В ПРОЕКТЕ — это критически важно, не путай:
Lid / Lid#2 — исполнители, те кто делают задачу руками. Дедлайн — их ответственность.
From — менеджер проекта, постановщик задачи. Отвечает за координацию и реализацию, но не за исполнение.

Пример: «Визитор центр» — делают Миша и Настя (Lid/Lid#2), менеджер проекта Алёна (From).
Если задача просрочена — это у Миши и Насти, не у Алёны.

Правила:
- «Кто делает / кто исполнитель / у кого дедлайн» → смотри Lid/Lid#2
- «Кто менеджер / кто ставил / с кем согласовывать» → смотри From
- Никогда не называй From исполнителем, и Lid — менеджером
- Если в Com написано уточнить у конкретного человека — упомяни его @ником
{managers}
ГРУППИРОВКА И АНАЛИЗ
Сам группируй задачи по проектам или статусам — не жди, пока спросят.
Замечай паттерны: что накапливается, что давно не двигается, что срочно.
Никогда не делай выводов о том, что конкретный человек перегружен — только общая картина по отделу.

УТОЧНЯЙ, ЕСЛИ НУЖНО
Если запрос неоднозначный — один короткий вопрос, не несколько. Не додумывай молча.

ДАННЫЕ И СТРУКТУРА ТАБЛИЦЫ
Колонки: Task — задача, Lid/Lid#2 — ответственные лиды, Priority — приоритет, From — постановщик/менеджер, DD — дедлайн, Com — комментарии.
Приоритеты в колонке Priority: П1 ГОРИМ — сдать первым / П2 — обычный темп / П3 — не горит / Done — закрыто / cancel — неактуально.
Дедлайны в данных уже аннотированы Python-кодом: рядом с датой написано (сегодня!), (завтра), (через N дн.), (просрочено N дн.), и метка недели — (эта неделя) или (прошлая неделя).
Сегодня {today}.

ЗАКРЫТЫЕ ЗАДАЧИ
«Что закрыто на этой неделе» = Priority=Done И в дедлайне есть метка «эта неделя».
«Что закрыто на прошлой неделе» = Priority=Done И в дедлайне есть метка «прошлая неделя».
«Все закрытые» = все задачи где Priority=Done.
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

MORNING_DIGEST_PROMPT = """\
Сегодня {today}, {weekday}. Напиши утреннюю сводку по спринту — три коротких аналитических абзаца.

Данные спринта:
{data}

Структура — строго три абзаца, без заголовков:

Абзац 1. Общая картина спринта: сколько задач в работе, какое общее ощущение — напряжённо, спокойно, есть хвост? Пиши как наблюдение, не перечисление.

Абзац 2. Главные риски: какие задачи горят или скоро загорятся. Объясни почему это важно и что будет если не успеть. Упомяни лидов с @ником если они есть в словаре менеджеров.

Абзац 3. Фокус на сегодня: что команде важно сделать именно сегодня. Конкретно и без воды — один-два приоритета максимум.

Правила:
- Только обычный текст, никаких *, **, #, ~, списков, тире-перечислений
- Каждый абзац — 2-3 предложения живым языком, аналитика и рассуждение
- @ники берёшь из словаря менеджеров (только если имя точно есть в словаре)
- Задачи Done и cancel не упоминать
- Максимум 12 строк суммарно
"""

EXTRACT_FEEDBACK_PROMPT = """\
Пользователь хочет передать фидбек, благодарность или сообщение для команды креативного отдела.
Извлеки само сообщение — то, что нужно передать команде.

Верни ТОЛЬКО валидный JSON без markdown:
{"message": "текст сообщения для команды"}

Если сообщение неясно — верни message как пустую строку.
"""

INSIGHT_PROMPT = """\
Ты — куратор творческих инсайтов для рекламно-креативной команды.
Каждый раз выбирай СЛУЧАЙНОГО автора из списка — не повторяй предыдущих.
Дай один инсайт, цитату или принцип, который вдохновляет и даёт новый взгляд на проблему.

Список авторов (50 имён — выбирай разных каждый раз):
Рик Рубин, Дэвид Оглви, Билл Бернбах, Пол Арден, Дэн Вайден, Ли Клоу,
Дэвид Дрога, Том Гэд, Алекс Осборн, Джефф Гудби, Боб Левенсон,
Вирджил Абло, Брайан Ино, Фаррелл Уильямс, Ханнес Нордэнсон,
Дэвид Финчер, Вес Андерсон, Дэвид Линч, Спайк Джонз, Мишель Гондри,
Стив Джобс, Джони Айв, Паула Шер, Стефан Загмайстер, Тьерри Саньо,
Дитер Рамс, Натаниэль Рассел, Казимир Малевич, Барбара Крюгер, Олайнка Эдисон,
Остин Клеон, Сет Годин, Малколм Гладуэлл, Адам Морган, Рори Сазерленд,
Марк Поллард, Руссель Дэвис, Том Роббинс, Джулия Кэмерон, Эд Кэтмулл,
Катерина Фейк, Анни Леонард, Стив Кофманн, Кэти Сиерра, Берни Краузе,
Тайни Фай, Линда Барри, Джек Дорси, Коби Брайант, Рин Исихара

Формат ответа — строго такой:
[один рандомный эмодзи] инсайт от [Имя автора]
[сам инсайт — 2-3 строки, без воды]

Пример:
🎯 инсайт от Рик Рубин
Лучшая идея — та, которую ты боишься показать первой.

Типы инсайтов (чередуй):
- Цитата с источником (книга, кампания, интервью)
- Творческий принцип: одно правило, которое реально работает
- Провокационный вопрос: что бы этот человек спросил про нашу задачу?
- Метод: конкретный приём из практики автора

Правила:
- Только обычный текст, без *, **, #
- Первая строка всегда: [эмодзи] инсайт от [Имя] — и больше ничего
- Эмодзи каждый раз разный, подходящий по настроению
- Максимум 3-4 строки суммарно, без воды
- Живо и неожиданно — избегай банальных цитат

Отвечай на русском.\
"""

EXTRACT_FILE_QUERY_PROMPT = """\
Пользователь ищет папку или файл в Google Drive. Извлеки название проекта из его сообщения.

Верни ТОЛЬКО валидный JSON без markdown:
{"query": "название проекта или ключевое слово"}

Примеры:
- "Где презентация по ПМФ?" → {"query": "ПМФ"}
- "найди файлы по Сберу" → {"query": "Сбер"}
- "дай ссылку на папку Альфа ролик" → {"query": "Альфа ролик"}
- "покажи материалы по брифу МТС" → {"query": "МТС"}

Только ключевое слово/название проекта, без лишних слов.
"""

EXTRACT_ASK_MANAGER_PROMPT = """\
Пользователь хочет задать вопрос менеджеру проекта. Извлеки из сообщения:
1. Название задачи или проекта
2. Вопрос, который нужно задать менеджеру (если указан явно)

Верни ТОЛЬКО валидный JSON без markdown:
{{"task": "название задачи или проекта", "question": "вопрос менеджеру или пустая строка"}}

Примеры:
- "спроси менеджера по ПМФ нужны ли правки" → {{"task": "ПМФ", "question": "нужны ли правки"}}
- "уточни у менеджера Сбер баннер" → {{"task": "Сбер баннер", "question": ""}}
- "тегни менеджера по Альфа — когда дедлайн?" → {{"task": "Альфа", "question": "когда дедлайн?"}}
"""

COMPOSE_MANAGER_QUESTION_PROMPT = """\
Ты — трекер-бот. Тебе нужно сформулировать короткий вопрос менеджеру проекта на основе комментариев к задаче.

Задача: {task}
Комментарии: {comments}

Сформулируй 1-2 коротких конкретных вопроса менеджеру, которые помогут уточнить детали по задаче.
Пиши живым языком, как коллега в чате. Без *, **, #. Максимум 3 строки.
"""

EXTRACT_UPDATE_FIELD_PROMPT = """\
Пользователь хочет изменить дедлайн, приоритет или статус задачи в спринте.
Сегодняшняя дата: {today}.

Данные текущего спринта (для поиска правильного названия задачи):
{sprint_data}

Верни ТОЛЬКО валидный JSON без markdown:
{{"task": "название задачи", "field": "DD" или "Priority", "value": "новое значение"}}

Правила:
- Найди НАИБОЛЕЕ ПОХОЖУЮ задачу из спринта выше по смыслу и написанию
- В поле "task" скопируй название так, как оно написано в спринте (или его ключевую часть)
- "field" — "DD" если меняют дедлайн/срок/дату, "Priority" если меняют приоритет/статус
- "value" для Priority: только одно из — "П1 ГОРИМ", "П2", "П3", "Done", "cancel"
- "value" для DD: дата в формате ДД.ММ.ГГГГ

Примеры:
- "поставь дедлайн Сбер баннер на 25 марта" → {{"task": "Сбер баннер", "field": "DD", "value": "25.03.2026"}}
- "измени приоритет ПМФ на П1" → {{"task": "ПМФ", "field": "Priority", "value": "П1 ГОРИМ"}}
- "закрой задачу Тося" → {{"task": "Тося Чайкина", "field": "Priority", "value": "Done"}}
- "поменяй статус альфа ролик на done" → {{"task": "Альфа ролик", "field": "Priority", "value": "Done"}}
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
# Feedback to creative team
FEEDBACK_RE = re.compile(
    r"^(передай\s+(команде|ребятам|креативу|дизайнерам|фидбек)"
    r"|фидбек\s+(для\s+команд|команде)"
    r"|хочу\s+(поблагодарить|похвалить|сказать\s+спасибо\s+(команде|ребятам))"
    r"|скажи\s+(команде|ребятам))",
    re.IGNORECASE
)
# Creative insight / quote
INSIGHT_RE = re.compile(
    r"(^дай\s+(инсайт|цитату|вдохновение|импульс)"
    r"|^инсайт$"
    r"|^вопрос\s+дня"
    r"|^разогрев)",
    re.IGNORECASE
)
# Ask manager / tag manager
ASK_MANAGER_RE = re.compile(
    r"(спроси\s+(у\s+)?(менеджер|постановщик|ответственн)"
    r"|уточни\s+(у\s+)?(менеджер|постановщик|ответственн)"
    r"|тегни\s+(менеджер|постановщик|ответственн)"
    r"|отметь\s+(менеджер|постановщик|ответственн)"
    r"|пинг(ани|ни)?\s+(менеджер|постановщик|ответственн)"
    r"|позови\s+(менеджер|постановщик|ответственн))",
    re.IGNORECASE
)
# Set deadline / priority intent
SET_FIELD_RE = re.compile(
    r"^(поставь|установи|измени|обнови|поменяй|задай|закрой|закрыть|отмени|отменить)\s+"
    r"(дедлайн|приоритет|срок|дату|статус|done|cancel|задачу|проект)",
    re.IGNORECASE
)
# Find file / Drive folder
FIND_FILE_RE = re.compile(
    # "где лежит / находится / хранится / взять / скачать" + что угодно
    r"(где\s+(лежит|находится|хранится|взять|скачать|найти)"
    # "где [тип файла]"
    r"|где\s+(файл|папк|презентац|преза|материал|бриф|ссылк|видео|видос|документ|доки)"
    # "найди / поищи [тип файла]"
    r"|(найди|поищи)\s+(файл|папк|презентац|преза|материал|бриф|видео|документ)"
    # "дай / скинь / кинь / пришли ссылку"
    r"|(дай|скинь|кинь|пришли|нужна)\s+ссылку?"
    # "покажи [тип файла]"
    r"|покажи\s+(материал|файл|папк|презентац|преза|бриф|видео)"
    # "есть ли / есть [тип файла]"
    r"|есть\s+(ли\s+)?(файл|папк|материал|презентац|видео|бриф)"
    # "скинь / кинь [тип файла]"
    r"|(скинь|кинь|пришли)\s+(файл|презентац|преза|материал|бриф|видео|документ))",
    re.IGNORECASE
)

# Call / meeting link detection
CALL_RE = re.compile(
    r"(подключись|зайди|присоединись|запиши)\s+(к|на|в)\s+(звонок|встреч|созвон|колл|конференц|зум|zoom)"
    r"|запиши\s+(встреч|звонок|созвон|колл)"
    r"|подключись\s+к\s+зуму"
    r"|зайди\s+(в|на)\s+(зум|zoom|meet|jazz|джаз)",
    re.IGNORECASE
)
MEETING_LINK_RE = re.compile(
    r"https?://(?:"
    r"[\w.-]*zoom\.us/[j/\w?=&-]+"            # Zoom
    r"|meet\.google\.com/[\w-]+"               # Google Meet
    r"|jazz\.sber\.ru/[\w?=&/-]+"              # SberJazz
    r"|salute\.sber\.ru/[\w?=&/-]+"            # Salute Jazz
    r"|teams\.microsoft\.com/[\w?=&/.-]+"      # MS Teams
    r"|telemost\.yandex\.ru/[\w?=&/-]+"        # Яндекс Телемост
    r")"
)


# ── Sheet parsing ─────────────────────────────────────────

def _annotate_deadline(dd_str: str, today) -> str:
    """Parse DD field and append a human-readable relative label.

    Supported input formats: DD.MM.YYYY, DD.MM.YY, DD.MM
    Returns original string unchanged if it cannot be parsed.
    """
    s = dd_str.strip()
    if not s:
        return s

    parsed = None
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d.%m"):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt == "%d.%m":
                # Assume current year; if that date already passed this year
                # and is more than 30 days ago, assume next year
                dt = dt.replace(year=today.year)
                if (dt.date() - today).days < -30:
                    dt = dt.replace(year=today.year + 1)
            elif fmt == "%d.%m.%y":
                # strptime maps 2-digit year: 25 → 2025
                pass
            parsed = dt.date()
            break
        except ValueError:
            continue

    if parsed is None:
        return s  # unknown format — leave as-is

    delta = (parsed - today).days

    if delta < -1:
        label = f"просрочено {abs(delta)} дн."
    elif delta == -1:
        label = "просрочено вчера"
    elif delta == 0:
        label = "сегодня!"
    elif delta == 1:
        label = "завтра"
    elif delta == 2:
        label = "послезавтра"
    elif delta <= 7:
        label = f"через {delta} дн."
    else:
        label = f"через {delta} дн."

    # Week classification — helps LLM answer "что закрыто на этой неделе"
    from datetime import timedelta
    week_monday = today - timedelta(days=today.weekday())
    week_sunday = week_monday + timedelta(days=6)
    prev_monday = week_monday - timedelta(days=7)
    prev_sunday = week_monday - timedelta(days=1)

    if week_monday <= parsed <= week_sunday:
        week_label = ", эта неделя"
    elif prev_monday <= parsed <= prev_sunday:
        week_label = ", прошлая неделя"
    else:
        week_label = ""

    return f"{s} ({label}{week_label})"


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
    today_date = datetime.now(MOSCOW_TZ).date()

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
            lines.append(f"   Дедлайн: {_annotate_deadline(t['DD'], today_date)}")

        if t.get("Com"):
            lines.append(f"   Ком: {t['Com']}")

        tasks.append("\n".join(lines))

    return sprint_name, "\n\n".join(tasks)


def _get_sprint_task_names(limit: int = 3) -> list[str]:
    """Return up to `limit` active task names from the current sprint (for examples)."""
    try:
        resp = requests.get(SHEET_URL, timeout=10)
        resp.raise_for_status()
        resp.encoding = "utf-8"
    except Exception:
        return []

    reader = csv.reader(io.StringIO(resp.text))
    all_rows = list(reader)
    if len(all_rows) < 2:
        return []

    headers = [h.strip() for h in all_rows[0]]
    last_sprint_idx = 0
    for i, row in enumerate(all_rows[1:], start=1):
        for cell in row:
            if "Запланированные задачи" in cell:
                last_sprint_idx = i
                break

    pri_col = headers.index("Priority") if "Priority" in headers else -1
    task_col = headers.index("Task") if "Task" in headers else 0

    names = []
    for row in all_rows[last_sprint_idx + 1:]:
        if not any(cell.strip() for cell in row):
            continue
        name = row[task_col].strip() if task_col < len(row) else ""
        if not name or "Запланированные задачи" in name:
            continue
        # Skip Done / cancel
        if pri_col >= 0 and pri_col < len(row):
            pri = row[pri_col].strip().lower()
            if pri in ("done", "cancel"):
                continue
        names.append(name)
        if len(names) >= limit:
            break
    return names


# ── Cached example project names ─────────────────────────
_example_cache: dict = {"names": None, "updated_at": None}


def _get_example_projects() -> tuple[str, str, str]:
    """Return 3 project names for use in examples. Cached for CACHE_TTL seconds."""
    now = datetime.now()
    cached = _example_cache["names"]
    updated = _example_cache["updated_at"]

    if cached and updated and (now - updated) < timedelta(seconds=CACHE_TTL):
        return cached

    names = _get_sprint_task_names(3)
    # Pad with generic fallbacks if sprint has fewer than 3 tasks
    fallbacks = ["проект А", "проект Б", "проект В"]
    while len(names) < 3:
        names.append(fallbacks[len(names)])

    result = (names[0], names[1], names[2])
    _example_cache["names"] = result
    _example_cache["updated_at"] = now
    return result


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


def query_apps_script(payload: dict) -> dict | None:
    """Post to Apps Script and return parsed JSON dict (or None on error).

    Google Apps Script redirects POST→GET (302), so the response may arrive
    with unexpected encoding or content-type.  We try several parsing
    strategies before falling back to a raw-text wrapper.
    """
    if not APPS_SCRIPT_URL:
        return None
    try:
        resp = requests.post(APPS_SCRIPT_URL, json=payload, timeout=15)
        resp.raise_for_status()

        # Strategy 1: requests built-in JSON parser
        try:
            data = resp.json()
            if isinstance(data, dict):
                return data
        except (ValueError, Exception):
            pass

        # Strategy 2: decode raw bytes (handles BOM, encoding quirks)
        body = resp.content.decode("utf-8-sig", errors="replace").strip()
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                return data
        except (ValueError, json.JSONDecodeError):
            pass

        # Strategy 3: raw text fallback
        logger.warning("Apps Script non-JSON response (action=%s): %s",
                        payload.get("action", "?"), body[:300])
        return {"status": "ok" if "ok" in body.lower() else "error",
                "message": body}
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

    # Определяем ник отправителя
    sender = update.effective_user
    sender_tag = "@" + sender.username if sender.username else sender.full_name or "Аноним"

    result = query_apps_script({
        "action": "update_comment",
        "task": task_name,
        "com": com,
        "sender": sender_tag,
    })
    if result is None:
        await update.message.reply_text("Не удалось связаться с таблицей.")
    elif result.get("status") == "error" and "not found" in result.get("message", "").lower():
        await update.message.reply_text(
            f"Не нашёл задачу «{task_name}» в текущем спринте. Уточни название."
        )
    else:
        # "ok" or any non-error response — treat as success
        await update.message.reply_text(f"Комментарий к «{task_name}» обновлён ✅")
        sheet_cache["updated_at"] = None


# ── Update field (deadline / priority) ───────────────────

async def handle_update_field(update: Update, text: str) -> None:
    """Parse task name + field + value, update via Apps Script."""
    if not APPS_SCRIPT_URL:
        await update.message.reply_text("Обновление задач не настроено (нет APPS_SCRIPT_URL).")
        return

    today = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")
    sprint_data = fetch_sheet() or "нет данных"
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=EXTRACT_UPDATE_FIELD_PROMPT.format(today=today, sprint_data=sprint_data),
            messages=[{"role": "user", "content": text}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```json?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
    except Exception as e:
        logger.error("Update field extract error: %s", e)
        await update.message.reply_text(
            "Не понял. Попробуй: «Поставь дедлайн [задача] на 25 марта» "
            "или «Измени приоритет [задача] на П1»"
        )
        return

    task_name = data.get("task", "").strip()
    field = data.get("field", "").strip()
    value = data.get("value", "").strip()

    if not task_name or not field or not value:
        await update.message.reply_text("Не хватает данных. Укажи задачу, поле и новое значение.")
        return

    if field not in ("DD", "Priority"):
        await update.message.reply_text("Могу менять только дедлайн (DD) или приоритет (Priority).")
        return

    result = query_apps_script({"action": "update_field", "task": task_name, "field": field, "value": value})

    field_ru = "дедлайн" if field == "DD" else "приоритет"
    if result is None:
        await update.message.reply_text("Не удалось связаться с таблицей.")
    elif result.get("status") == "error" and "not found" in result.get("message", "").lower():
        await update.message.reply_text(
            f"Не нашёл задачу «{task_name}» в текущем спринте. Уточни название."
        )
    else:
        # "ok" or any non-error response — treat as success
        await update.message.reply_text(f"Обновил {field_ru} для «{task_name}»: {value} ✅")
        sheet_cache["updated_at"] = None


# ── Ask manager (tag & question) ─────────────────────────

def _find_task_in_sprint(query: str) -> dict | None:
    """Find a task in current sprint by partial match. Returns dict with task fields or None."""
    raw = fetch_sheet()
    if not raw:
        return None

    # Re-fetch raw CSV to parse task fields
    try:
        resp = requests.get(SHEET_URL, timeout=10)
        resp.raise_for_status()
        resp.encoding = "utf-8"
    except Exception:
        return None

    reader = csv.reader(io.StringIO(resp.text))
    all_rows = list(reader)
    if len(all_rows) < 2:
        return None

    headers = [h.strip() for h in all_rows[0]]

    last_sprint_idx = 0
    for i, row in enumerate(all_rows[1:], start=1):
        for cell in row:
            if "Запланированные задачи" in cell:
                last_sprint_idx = i
                break

    query_lower = query.lower()
    for row in all_rows[last_sprint_idx + 1:]:
        if not any(cell.strip() for cell in row):
            continue
        t = {}
        for j, val in enumerate(row):
            if j < len(headers) and headers[j]:
                t[headers[j]] = val.strip()
        task_name = t.get("Task", "")
        if not task_name:
            continue
        # Bidirectional partial match
        if query_lower in task_name.lower() or task_name.lower() in query_lower:
            return t

    return None


def _resolve_manager_mention(name: str) -> str:
    """Resolve a manager name to @username. Returns '@username' or just the name."""
    if not name:
        return ""
    # Direct lookup
    if name in MANAGERS:
        return MANAGERS[name]
    # Case-insensitive partial match
    name_lower = name.lower()
    for key, username in MANAGERS.items():
        if key.lower() == name_lower or name_lower in key.lower() or key.lower() in name_lower:
            return username
    return name


async def handle_ask_manager(update: Update, text: str) -> None:
    """Find task, resolve manager, compose and send a tagged question."""
    # Step 1: Extract task name and optional question via Claude
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=EXTRACT_ASK_MANAGER_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```json?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
    except Exception as e:
        logger.error("Ask manager extract error: %s", e)
        await update.message.reply_text(
            "Не понял. Попробуй: «Спроси у менеджера по [проект]: [вопрос]»"
        )
        return

    task_query = data.get("task", "").strip()
    user_question = data.get("question", "").strip()

    if not task_query:
        await update.message.reply_text(
            "Не понял, по какому проекту. Попробуй: «Спроси у менеджера по [проект]»"
        )
        return

    # Step 2: Find task in sprint
    task = _find_task_in_sprint(task_query)
    if not task:
        await update.message.reply_text(
            f"Не нашёл задачу «{task_query}» в текущем спринте. Уточни название."
        )
        return

    task_name = task.get("Task", task_query)
    manager_name = task.get("From", "").strip()
    comments = task.get("Com", "").strip()

    if not manager_name:
        await update.message.reply_text(
            f"У задачи «{task_name}» не указан менеджер (колонка From пустая)."
        )
        return

    manager_mention = _resolve_manager_mention(manager_name)

    # Step 3: Compose the question
    if user_question:
        # User provided a specific question
        question_text = user_question
    elif comments:
        # Generate question from comments via Claude
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=COMPOSE_MANAGER_QUESTION_PROMPT.format(
                    task=task_name, comments=comments
                ),
                messages=[{"role": "user", "content": "Сформулируй вопрос"}],
            )
            question_text = resp.content[0].text.strip()
        except Exception as e:
            logger.error("Compose manager question error: %s", e)
            question_text = f"нужно уточнить детали по комментариям: {comments}"
    else:
        question_text = "нужно уточнить детали по задаче"

    # Step 4: Send the tagged message
    reply = f"{manager_mention}, по задаче «{task_name}»:\n{question_text}"
    await _send(update, reply)


# ── Feedback to creative team ─────────────────────────────

async def handle_feedback(update: Update, text: str) -> None:
    """Extract feedback message, save to feedback sheet via Apps Script."""
    if not APPS_SCRIPT_URL:
        await update.message.reply_text("Запись сообщений не настроена (нет APPS_SCRIPT_URL).")
        return

    sender = update.effective_user
    username = f"@{sender.username}" if sender.username else sender.full_name or "Неизвестный"

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=EXTRACT_FEEDBACK_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```json?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
    except Exception as e:
        logger.error("Feedback extract error: %s", e)
        data = {"message": text}

    message = data.get("message", "").strip()
    if not message:
        await update.message.reply_text(
            "Не понял, что передать команде. "
            "Попробуй: «Передай команде: вы сделали крутой баннер!»"
        )
        return

    today = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")
    ok = post_to_apps_script({
        "action": "add_feedback",
        "message": message,
        "from": username,
        "date": today,
    })

    if ok:
        await update.message.reply_text(
            f"Передал команде ✅\n\n«{message}»\n\nОт: {username}"
        )
    else:
        await update.message.reply_text("Не удалось записать — таблица недоступна.")


# ── Find file in Drive ────────────────────────────────────

async def handle_find_file(update: Update, text: str) -> None:
    """Extract project name via Claude, search Drive via Apps Script, format response."""
    if not APPS_SCRIPT_URL:
        await update.message.reply_text(
            "Поиск по Drive не настроен (нет APPS_SCRIPT_URL)."
        )
        return

    # Step 1: Extract the query keyword via Claude Haiku
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=EXTRACT_FILE_QUERY_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```json?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        query = json.loads(raw).get("query", "").strip()
    except Exception as e:
        logger.error("File query extract: %s", e)
        query = text.strip()

    if not query:
        p1, _, _ = _get_example_projects()
        await update.message.reply_text(f"Не понял, что ищем. Попробуй: /file {p1}")
        return

    # Step 2: Call Apps Script
    result = query_apps_script({"action": "search_drive", "query": query})
    if result is None:
        await update.message.reply_text("Не удалось связаться с Google Drive.")
        return

    # If response couldn't be parsed as structured JSON, retry parsing the message field
    if "matches" not in result and isinstance(result.get("message"), str):
        try:
            inner = json.loads(result["message"])
            if isinstance(inner, dict):
                result = inner
        except (ValueError, json.JSONDecodeError):
            pass

    status = result.get("status", "")

    if status == "not_found":
        await update.message.reply_text(
            f"Папки по запросу «{query}» не нашёл.\n\n"
            "Можешь посмотреть сам в общей папке проектов:\n"
            "https://drive.google.com/drive/u/2/folders/1oWKcFJpliR9GxnBZ56Els8nCdkfuE8s_"
        )
        return
    if status != "ok":
        await update.message.reply_text(
            f"Ошибка Drive: {result.get('message', '?')}"
        )
        return

    matches = result.get("matches", [])
    if not matches:
        await update.message.reply_text(f"Папок по запросу «{query}» не нашёл.")
        return

    # Step 4: Format response — plain text, no markdown
    lines = []
    for idx, folder in enumerate(matches):
        folder_name = folder.get("name", "")
        folder_url  = folder.get("url", "")
        files       = folder.get("files", [])

        if len(matches) > 1:
            lines.append(f"Папка {idx + 1}. {folder_name}:")
        else:
            lines.append(f'Папка "{folder_name}":')
        lines.append(folder_url)

        if files:
            lines.append(f"\nФайлы ({len(files)}):")
            for i, f in enumerate(files, 1):
                lines.append(f"{i}. {f['name']} — {f['url']}")
        else:
            lines.append("(папка пустая или файлы недоступны)")

        if idx < len(matches) - 1:
            lines.append("")

    await _send(update, "\n".join(lines))


async def file_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Search Drive for a project folder. Usage: /file ПМФ"""
    if context.args:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        await handle_find_file(update, " ".join(context.args))
    else:
        p1, _, _ = _get_example_projects()
        await update.message.reply_text(f"Укажи название проекта. Пример: /file {p1}")


# ── Creative insight ───────────────────────────────────────

async def _generate_insight() -> str:
    """Generate a single creative insight/quote via Claude."""
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=INSIGHT_PROMPT,
            messages=[{"role": "user", "content": "Дай инсайт"}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error("Insight generation error: %s", e)
        return ""


# ── Morning digest ────────────────────────────────────────

async def _generate_morning_digest() -> str:
    """Generate morning digest text using Claude."""
    data = fetch_sheet()
    if not data:
        return ""

    today = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")
    weekday_names = {
        "Monday": "понедельник", "Tuesday": "вторник", "Wednesday": "среда",
        "Thursday": "четверг", "Friday": "пятница", "Saturday": "суббота", "Sunday": "воскресенье"
    }
    weekday = weekday_names.get(datetime.now(MOSCOW_TZ).strftime("%A"), "")

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=build_base_prompt(),
            messages=[{
                "role": "user",
                "content": MORNING_DIGEST_PROMPT.format(
                    today=today,
                    weekday=weekday,
                    data=data,
                ),
            }],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error("Morning digest generation error: %s", e)
        return ""


# ── Handlers ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я трафик-бот креативного отдела.\n\n"
        "Знаю все проекты, спринты и дедлайны. Могу рассказать что горит, добавить задачу, "
        "найти файл или презентацию в Drive, передать фидбек команде.\n\n"
        "Общаться можно как командами, так и в свободном тексте — как с коллегой.\n\n"
        "/help — полный список того, что умею"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    p1, p2, p3 = _get_example_projects()
    await update.message.reply_text(
        "Вот что умею — работает командой или в свободном тексте.\n"
        "\n"
        "/sprint — статус спринта по всем проектам\n"
        "/hot — что горит прямо сейчас\n"
        f"Например: «Что закрыли на этой неделе?» или «Что по {p1}?»\n"
        "\n"
        "/task — добавить задачу в спринт\n"
        "Например: «Добавь во входящие: созвон с клиентом, от Маши»\n"
        "\n"
        "Изменить задачу:\n"
        f"«Поставь дедлайн {p1} на пятницу», «Закрой задачу {p2}», «Смени приоритет на П1»\n"
        "\n"
        "/file [проект] — найти папку и файлы в Google Drive\n"
        f"Например: «Где лежит презентация по {p3}?»\n"
        "\n"
        "/digest — аналитическое саммари спринта\n"
        "\n"
        "/call [ссылка] — переслать ссылку на встречу боту-рекордеру\n"
        "Или просто кинь ссылку на Zoom/Jazz/Meet — сам подхвачу.\n"
        "\n"
        "Можно спрашивать цепочкой — помню контекст разговора.\n"
        "/clear — начать заново\n"
        "\n"
        "В группе отвечаю на @упоминание или «Огент»."
    )


async def insight_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a creative insight or quote."""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    text = await _generate_insight()
    if not text:
        await update.message.reply_text("Не удалось получить инсайт. Попробуй ещё раз.")
        return
    await _send(update, text)


async def digest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual trigger for morning digest."""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    text = await _generate_morning_digest()
    if not text:
        await update.message.reply_text("Не удалось сформировать сводку — нет данных из таблицы.")
        return
    await _send(update, text)


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


async def setcallgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run this command in a group to register it as the call-forwarding target."""
    global CALL_GROUP_ID
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "Эту команду нужно запустить в группе, куда пересылать ссылки на звонки."
        )
        return
    CALL_GROUP_ID = str(chat.id)
    await update.message.reply_text(
        f"Группа для звонков установлена: {chat.id}\n\n"
        f"Чтобы сохранить между перезапусками, добавь в Railway:\n"
        f"CALL_GROUP_ID = {chat.id}"
    )


async def call_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward a meeting link to the recorder bot group via /call <link>."""
    args_text = " ".join(context.args) if context.args else ""
    link = _extract_meeting_link(args_text)
    if not link:
        await update.message.reply_text(
            "Скинь ссылку на встречу после команды.\n"
            "Например: /call https://zoom.us/j/123456"
        )
        return
    await _forward_call_link(update, context, link)


async def _forward_call_link(update: Update, context: ContextTypes.DEFAULT_TYPE, link: str) -> None:
    """Send meeting link to the recorder bot group, tagging @Ogentcallbot."""
    if not CALL_GROUP_ID:
        await update.message.reply_text(
            "Не настроена группа для записи звонков.\n"
            "Админ должен выполнить /setcallgroup в нужной группе."
        )
        return
    try:
        await context.bot.send_message(
            chat_id=int(CALL_GROUP_ID),
            text=f"@Ogentcallbot {link}",
        )
        await update.message.reply_text(f"Передал ссылку на запись: {link}")
    except Exception as e:
        logger.error("Failed to forward call link: %s", e)
        await update.message.reply_text("Не получилось переслать ссылку. Проверь настройки группы.")


def _extract_meeting_link(text: str) -> str | None:
    """Extract the first meeting URL from text."""
    m = MEETING_LINK_RE.search(text)
    return m.group(0) if m else None


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
        p1, _, _ = _get_example_projects()
        await update.message.reply_text(
            "Опиши задачу — название, приоритет и дедлайн если есть.\n"
            f"Пример: {p1}, П1, дедлайн 20 марта"
        )


async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tag project manager and ask a question. Usage: /ask [project] [question]"""
    if context.args:
        text = " ".join(context.args)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        await handle_ask_manager(update, text)
    else:
        p1, _, _ = _get_example_projects()
        await update.message.reply_text(
            "Укажи проект и (необязательно) вопрос.\n"
            f"Пример: /ask {p1} нужны ли правки?"
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

    # ── Call / meeting link ──
    meeting_link = _extract_meeting_link(text)
    if meeting_link:
        await _forward_call_link(update, context, meeting_link)
        return
    if CALL_RE.search(text.strip()):
        await update.message.reply_text(
            "Скинь ссылку на встречу — перешлю боту-рекордеру.\n"
            "Или: /call https://zoom.us/j/123456"
        )
        return

    # ── Find file / Drive folder ──
    if FIND_FILE_RE.search(text.strip()):
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        await handle_find_file(update, text)
        return

    # ── Feedback to creative team ──
    if FEEDBACK_RE.match(text.strip()):
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        await handle_feedback(update, text)
        return

    # ── Creative insight ──
    if INSIGHT_RE.match(text.strip()):
        await insight_cmd(update, context)
        return

    # ── Ask manager ──
    if ASK_MANAGER_RE.search(text.strip()):
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        await handle_ask_manager(update, text)
        return

    # ── Update comment ──
    if UPDATE_COMMENT_RE.match(text.strip()):
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        await handle_update_comment(update, text)
        return

    # ── Set deadline / priority ──
    if SET_FIELD_RE.match(text.strip()):
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        await handle_update_field(update, text)
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
    """Cache bot username on startup."""
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
    app.add_handler(CommandHandler("file", file_cmd))
    app.add_handler(CommandHandler("ask", ask_cmd))
    app.add_handler(CommandHandler("digest", digest_cmd))
    app.add_handler(CommandHandler("insight", insight_cmd))
    app.add_handler(CommandHandler("report", report))    # alias → sprint
    app.add_handler(CommandHandler("fire", hot_cmd))     # alias → hot
    app.add_handler(CommandHandler("setgroup", setgroup))
    app.add_handler(CommandHandler("setcallgroup", setcallgroup))
    app.add_handler(CommandHandler("call", call_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "edited_message", "callback_query"],
    )


if __name__ == "__main__":
    main()
