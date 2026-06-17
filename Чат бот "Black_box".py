!pip install aiohttp

!pip install yandexgpt-python

!pip install python-telegram-bot==20.7

!pip install nest_asyncio

!pip install python-dotenv

# -*- coding: utf-8 -*-
import logging
import asyncio
import aiohttp
import re
import sqlite3
import nest_asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler
)
import os
from dotenv import load_dotenv

nest_asyncio.apply()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- конфигурация ----------
load_dotenv()  # ищет .env в текущей папке

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
FOLDER_ID = os.getenv("FOLDER_ID")
API_KEY = os.getenv("API_KEY")

USER_AGREEMENT_URL = "https://github.com/yourusername/yourrepo/blob/main/user_agreement.md"
PRIVACY_POLICY_URL = "https://github.com/yourusername/yourrepo/blob/main/privacy_policy.md"

# ---------- база данных ----------
DB_PATH = "running_line_bot.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            accepted_terms BOOLEAN DEFAULT 0,
            style TEXT DEFAULT 'info',
            speech_rate INTEGER DEFAULT 130,
            duration_seconds INTEGER DEFAULT 30,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def get_user(telegram_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT accepted_terms, style, speech_rate, duration_seconds FROM users WHERE telegram_id = ?', (telegram_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'accepted_terms': bool(row[0]),
            'style': row[1],
            'speech_rate': row[2],
            'duration_seconds': row[3]
        }
    return None

def create_user(telegram_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO users (telegram_id, accepted_terms, style, speech_rate, duration_seconds)
        VALUES (?, 0, 'info', 130, 30)
    ''', (telegram_id,))
    conn.commit()
    conn.close()

def update_user_accepted_terms(telegram_id: int, accepted: bool):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE users SET accepted_terms = ?, updated_at = CURRENT_TIMESTAMP WHERE telegram_id = ?', (accepted, telegram_id))
    conn.commit()
    conn.close()

def update_user_params(telegram_id: int, style: str = None, speech_rate: int = None, duration_seconds: int = None):
    updates = []
    values = []
    if style is not None:
        updates.append('style = ?')
        values.append(style)
    if speech_rate is not None:
        updates.append('speech_rate = ?')
        values.append(speech_rate)
    if duration_seconds is not None:
        updates.append('duration_seconds = ?')
        values.append(duration_seconds)
    if not updates:
        return
    updates.append('updated_at = CURRENT_TIMESTAMP')
    query = f'UPDATE users SET {", ".join(updates)} WHERE telegram_id = ?'
    values.append(telegram_id)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(query, values)
    conn.commit()
    conn.close()

def delete_user(telegram_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM users WHERE telegram_id = ?', (telegram_id,))
    conn.commit()
    conn.close()

# ---------- вспомогательные функции ----------
def count_words(text: str) -> int:
    return len(text.split())

def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек"
    minutes = seconds // 60
    remaining = seconds % 60
    if remaining == 0:
        return f"{minutes} мин"
    return f"{minutes} мин {remaining} сек"

def parse_duration_input(text: str) -> int | None:
    text = text.lower().strip().replace(',', '.')
    text = re.sub(r'\s+', '', text)
    match = re.match(r'^(\d+(?:\.\d+)?)([a-zа-я]*)$', text)
    if not match:
        return None
    value, unit = match.groups()
    value = float(value)
    if unit in ('сек', 'с', ''):
        seconds = int(value)
    elif unit in ('мин', 'м'):
        seconds = int(value * 60)
    else:
        return None
    if seconds < 10 or seconds > 600:
        return None
    return seconds

# ---------- промпты для YandexGPT ----------
BASE_PROMPT = """
Твоя роль: редактор новостей на радио. Ты пишешь текст для ведущего в эфире.
Соблюдай следующие жёсткие правила:

1. Запрещены: причастные/деепричастные обороты, сложные союзы (кроме «и», «но», «потому что»), пассивный залог.
2. Числительные и даты – словами, кроме телефонов, годов (пиши «2-го марта», «в 1995-м»), номеров машин/домов.
3. Избегай стыков трёх согласных, свистящих+шипящих, повторов звуков.
4. Ударения пиши ЗАГЛАВНОЙ внутри слова (маркЕтинг, звонИт, квАртал).
5. Запрещены клише: «на данный момент», «проведена работа», «было отмечено», «на сегодняшний день» и т.п.
6. Структура новости: первое предложение – главный факт (кто, что сделал, где, когда). Далее детали.

{style_instruction}

КРИТИЧЕСКИ ВАЖНО: твой ответ должен содержать ОТ {min_words} ДО {max_words} слов.
При превышении лимита ответ будет отклонён, при слишком малом объёме – тоже.
Сокращай или расширяй текст ровно настолько, чтобы попасть в диапазон. Сохраняй все ключевые детали.
"""

def build_system_prompt(params: dict) -> str:
    style = params.get('style', 'info')
    tempo = params.get('speech_rate', 130)
    duration = params.get('duration_seconds', 30)

    if style == 'info':
        style_text = """
### СТИЛЬ: ИНФОРМАЦИОННЫЙ
- Максимально сухо, фактологично. Без эпитетов, сравнений, оценок.
- Только глаголы и существительные. Нет «неожиданно», «сенсационно», «шокирующе».
- Строгий новостной тон, как в выпуске новостей.
"""
    else:  # entertainment
        style_text = """
### СТИЛЬ: РАЗВЛЕКАТЕЛЬНЫЙ
- Более живая речь, допустимы короткие образные выражения (например, «резкое торможение», «звонкая пощёчина»).
- Можно использовать лёгкую эмоциональную окраску, но без перехода в пафос.
- Сохраняй разговорную интонацию, избегай официальщины.
"""

    max_words = int((tempo * duration) / 60)
    if max_words < 15:
        max_words = 15
    if max_words > 600:
        max_words = 600
    min_words = int(max_words * 0.75)

    return BASE_PROMPT.format(
        style_instruction=style_text,
        min_words=min_words,
        max_words=max_words
    )

# ---------- работа с YandexGPT ----------
async def yandexgpt_request(messages, max_tokens=4000):
    url = "https://llm.api.cloud.yandex.net/v1/chat/completions"
    headers = {
        "Authorization": f"Api-Key {API_KEY}",
        "Content-Type": "application/json"
    }
    body = {
        "model": f"gpt://{FOLDER_ID}/deepseek-v32",
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": max_tokens
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=body) as resp:
            if resp.status == 200:
                data = await resp.json()
                if "choices" in data:
                    return data["choices"][0]["message"]["content"]
                elif "result" in data and "alternatives" in data["result"]:
                    return data["result"]["alternatives"][0]["message"]["text"]
            return None

async def get_yandexgpt_response(user_text: str, system_prompt: str, max_words: int = None) -> str:
    async def _make_request(messages, max_tokens=4000):
        return await yandexgpt_request(messages, max_tokens)

    answer = await _make_request([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text}
    ])
    if answer is None:
        return "Не удалось получить ответ от модели."

    if max_words is None:
        return answer

    min_words = int(max_words * 0.75)
    actual_words = count_words(answer)

    if min_words <= actual_words <= max_words:
        return answer

    if actual_words > max_words:
        instruction = f"Сократи до {max_words} слов (сейчас {actual_words}). Удали второстепенные детали, оставь суть."
    else:
        instruction = f"Увеличь объём до {max_words} слов (сейчас {actual_words}). Добавь недостающие детали из исходного текста, но сохрани стиль и правила."

    retry_prompt = (
        f"Твой предыдущий ответ содержит {actual_words} слов, а нужно в диапазоне от {min_words} до {max_words}. {instruction}\n"
        "Перепиши тот же текст, строго соблюдая все правила (ударения, запрет клише и т.д.). "
        "Ответь только исправленным текстом."
    )

    answer2 = await _make_request([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Исходный текст для адаптации:\n{user_text}\n\n{retry_prompt}"}
    ])
    if answer2 is None:
        return f"Не удалось исправить объём. Вот предыдущий ответ (на свой страх и риск):\n\n{answer}"

    actual2 = count_words(answer2)
    if min_words <= actual2 <= max_words:
        return answer2

    if actual2 > max_words:
        words = answer2.split()
        truncated = " ".join(words[:max_words])
        return truncated + "… (принудительное сокращение)"
    else:
        answer3 = await _make_request([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Текст слишком короткий ({actual2} слов, нужно {max_words}). Допиши недостающие детали из исходника, сохраняя стиль. Ответь только дополненным текстом:\n{answer2}"}
        ])
        if answer3 and count_words(answer3) <= max_words:
            return answer3
        else:
            return answer2 + " (текст слишком короткий, но это лучший вариант)"

# ---------- расстановка ударений ----------
async def put_stresses(text: str) -> str:
    prompt = f"""
Твоя задача — расставить ударения в русских словах. Выделяй ударную гласную ЗАГЛАВНОЙ буквой.
Правила:
- Для имён собственных используй литературную норму.
- Если слово многозначное и ударение зависит от контекста — выбери самый частотный вариант.
- Не меняй написание букв, кроме замены ударной гласной на заглавную.
- Знаки препинания и регистр первой буквы предложения сохраняй.
- Ответь только текстом с расставленными ударениями, без пояснений.

Текст: {text}
"""
    response = await yandexgpt_request([{"role": "user", "content": prompt}], max_tokens=1500)
    return response if response else "Не удалось расставить ударения."

# ---------- клавиатуры ----------
def accept_terms_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Принять условия", callback_data="accept_terms")]])

def legal_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Пользовательское соглашение", url=USER_AGREEMENT_URL)],
        [InlineKeyboardButton("Политика конфиденциальности", url=PRIVACY_POLICY_URL)]
    ])

def delete_confirmation_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Удалить", callback_data="confirm_delete"),
         InlineKeyboardButton("Отмена", callback_data="cancel_delete")]
    ])

def parameters_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Стиль речи", callback_data="param_style")],
        [InlineKeyboardButton("Темп речи", callback_data="param_tempo")],
        [InlineKeyboardButton("Хронометраж", callback_data="param_duration")]
    ])

def style_keyboard(current_style: str):
    info_text = "✅ Информационный" if current_style == 'info' else "Информационный"
    ent_text = "✅ Развлекательный" if current_style == 'entertainment' else "Развлекательный"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(info_text, callback_data="set_style_info")],
        [InlineKeyboardButton(ent_text, callback_data="set_style_entertainment")],
        [InlineKeyboardButton("Назад", callback_data="back_to_params")]
    ])

def tempo_main_keyboard(current_tempo: int):
    presets = [
        (110, "Медленно (110 слов/мин)"),
        (130, "Нормально (130 слов/мин)"),
        (170, "Быстро (170 слов/мин)")
    ]
    buttons = []
    for val, label in presets:
        display = f"✅ {label}" if current_tempo == val else label
        buttons.append([InlineKeyboardButton(display, callback_data=f"tempo_preset_{val}")])
    buttons.append([InlineKeyboardButton("Настроить темп", callback_data="tempo_finetune")])
    buttons.append([InlineKeyboardButton("Готово", callback_data="tempo_main_done")])
    return InlineKeyboardMarkup(buttons)

def tempo_finetune_keyboard(current_tempo: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("-10", callback_data="tempo_minus_10"),
         InlineKeyboardButton(str(current_tempo), callback_data="noop"),
         InlineKeyboardButton("+10", callback_data="tempo_plus_10")],
        [InlineKeyboardButton("Готово", callback_data="tempo_finetune_done")]
    ])

def tempo_for_timekeeping_main_keyboard(current_tempo: int):
    presets = [
        (110, "Медленно (110 слов/мин)"),
        (130, "Нормально (130 слов/мин)"),
        (170, "Быстро (170 слов/мин)")
    ]
    buttons = []
    for val, label in presets:
        display = f"✅ {label}" if current_tempo == val else label
        buttons.append([InlineKeyboardButton(display, callback_data=f"tk_tempo_preset_{val}")])
    buttons.append([InlineKeyboardButton("Настроить темп", callback_data="tk_tempo_finetune")])
    buttons.append([InlineKeyboardButton("Готово", callback_data="tk_tempo_main_done")])
    return InlineKeyboardMarkup(buttons)

def tempo_for_timekeeping_finetune_keyboard(current_tempo: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("-10", callback_data="tk_tempo_minus_10"),
         InlineKeyboardButton(str(current_tempo), callback_data="noop"),
         InlineKeyboardButton("+10", callback_data="tk_tempo_plus_10")],
        [InlineKeyboardButton("Готово", callback_data="tk_tempo_finetune_done")]
    ])

def back_only_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="back_to_params")]])

# ---------- обработчики команд ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user(user_id)
    if user_data is None:
        create_user(user_id)
        user_data = get_user(user_id)

    if not user_data['accepted_terms']:
        await update.message.reply_text(
            "*Добро пожаловать в бот «Running_Line»*\n\n"
            "Я адаптирую новостные тексты для радиоэфира. Перед началом работы необходимо принять "
            "Пользовательское соглашение и Политику конфиденциальности.\n\n"
            "*Важные условия:*\n"
            "• Бот выполняет *техническую адаптацию*, не изменяя смысл текста.\n"
            "• Ответственность за достоверность и юридическую чистоту итогового текста несёте *вы*.\n"
            "• Запрещено загружать экстремистский, порнографический или клеветнический контент.\n\n"
            "Нажимая «Принять условия», вы подтверждаете, что ознакомлены и согласны с документами.\n"
            "С документами можно ознакомиться по команде /legal.",
            parse_mode="Markdown",
            reply_markup=accept_terms_keyboard()
        )
    else:
        context.user_data['accepted_terms'] = True
        context.user_data['params'] = {
            'style': user_data['style'],
            'speech_rate': user_data['speech_rate'],
            'duration_seconds': user_data['duration_seconds']
        }
        await update.message.reply_text(
            "Условия приняты.\n\n"
            "Отправьте текст, и я адаптирую его для радио,\n"
            "либо выберите одну из команд:\n\n"
            "/parameters — настройки эфира\n"
            "/timekeeping — хронометраж текста\n"
            "/stress — расставить ударения\n"
            "/legal — юридические документы\n"
            "/deletemydata — удалить мои данные"
        )

async def parameters_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('accepted_terms'):
        await update.message.reply_text("Нажмите /start")
        return
    params = context.user_data['params']
    text = (
        f"⚙️ *Параметры эфира*\n\n"
        f"Стиль: {'Информационный' if params['style'] == 'info' else 'Развлекательный'}\n"
        f"Темп речи: {params['speech_rate']} слов/мин\n"
        f"Хронометраж: {format_duration(params['duration_seconds'])}"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=parameters_keyboard())

async def timekeeping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('accepted_terms'):
        await update.message.reply_text("Нажмите /start")
        return
    args = context.args
    if args and args[0].isdigit():
        tempo = int(args[0])
        if tempo < 50:
            tempo = 50
        if tempo > 300:
            tempo = 300
        context.user_data['timekeeping_tempo'] = tempo
        context.user_data['awaiting_timekeeping_text'] = True
        await update.message.reply_text(
            f"Темп установлен: {tempo} слов/мин.\n"
            "Отправьте текст для расчёта хронометража.\n"
            "Для отмены используйте /cancel"
        )
    else:
        current_tempo = context.user_data.get('temp_timekeeping_tempo', context.user_data['params']['speech_rate'])
        context.user_data['temp_timekeeping_tempo'] = current_tempo
        await update.message.reply_text(
            f"Выберите темп речи для расчёта хронометража.\nТекущий: {current_tempo} слов/мин",
            reply_markup=tempo_for_timekeeping_main_keyboard(current_tempo)
        )

async def stress_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('accepted_terms'):
        await update.message.reply_text("Нажмите /start")
        return
    context.user_data['awaiting_stress'] = True
    await update.message.reply_text(
        "🔊 *Расстановка ударений*\n\n"
        "Отправьте слово или короткую фразу (до 15 слов).\n"
        "Я выделю ударения ЗАГЛАВНОЙ буквой.\n\n"
        "Для отмены используйте /cancel",
        parse_mode="Markdown"
    )

async def legal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Юридическая информация*\nДокументы доступны по ссылкам ниже:",
        parse_mode="Markdown",
        reply_markup=legal_keyboard()
    )

async def deletemydata_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('accepted_terms'):
        await update.message.reply_text("Нажмите /start")
        return
    await update.message.reply_text(
        "Это удалит:\n\n"
        "• настройки\n"
        "• согласие с условиями\n"
        "• сохранённые данные\n\n"
        "Вы уверены?",
        reply_markup=delete_confirmation_keyboard()
    )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleared = False
    for flag in ['awaiting_duration', 'awaiting_timekeeping_text', 'awaiting_stress']:
        if context.user_data.get(flag):
            del context.user_data[flag]
            cleared = True
    if 'temp_timekeeping_tempo' in context.user_data:
        del context.user_data['temp_timekeeping_tempo']
        cleared = True
    if cleared:
        await update.message.reply_text("Действие отменено.")
    else:
        await update.message.reply_text("Нет активного действия для отмены.")

# ---------- обработчик callback'ов ----------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    params = context.user_data.get('params', {})

    # принятие условий
    if data == "accept_terms":
        update_user_accepted_terms(user_id, True)
        context.user_data['accepted_terms'] = True
        user_row = get_user(user_id)
        context.user_data['params'] = {
            'style': user_row['style'],
            'speech_rate': user_row['speech_rate'],
            'duration_seconds': user_row['duration_seconds']
        }
        await query.edit_message_text("Условия приняты. Спасибо!")
        await query.message.reply_text(
            "Отправьте текст, и я адаптирую его для радио,\n"
            "либо выберите одну из команд:\n\n"
            "/parameters — настройки эфира\n"
            "/timekeeping — хронометраж текста\n"
            "/stress — расставить ударения\n"
            "/legal — юридические документы\n"
            "/deletemydata — удалить мои данные"
        )
        return

    # удаление данных
    if data == "confirm_delete":
        delete_user(user_id)
        context.user_data.clear()
        await query.edit_message_text("Все ваши данные удалены. Для повторного использования начните с /start")
        return
    if data == "cancel_delete":
        await query.edit_message_text("Удаление отменено.")
        return

    # проверка принятия условий для остальных действий
    if not context.user_data.get('accepted_terms'):
        await query.edit_message_text("Нажмите /start")
        return

    # ---------- настройки: навигация ----------
    if data == "param_style":
        current = params.get('style', 'info')
        await query.edit_message_text("Выберите стиль речи:", reply_markup=style_keyboard(current))
        return
    elif data == "param_tempo":
        current = params.get('speech_rate', 130)
        await query.edit_message_text(
            f"Текущий темп: {current} слов/мин\n\nВыберите предустановку или настройте вручную:",
            reply_markup=tempo_main_keyboard(current)
        )
        return
    elif data == "param_duration":
        context.user_data['awaiting_duration'] = True
        context.user_data['duration_edit_msg_id'] = query.message.message_id
        await query.edit_message_text(
            f"Хронометраж\nТекущее значение: {format_duration(params['duration_seconds'])}\n\n"
            "Введите желаемый хронометраж (секунды или минуты).\n"
            "Примеры: 45, 90сек, 1.5мин, 2м\n"
            "Диапазон: 10–600 секунд.\n\n"
            "Или нажмите «Назад».",
            reply_markup=back_only_keyboard()
        )
        return
    elif data == "back_to_params":
        if 'awaiting_duration' in context.user_data:
            del context.user_data['awaiting_duration']
            if 'duration_edit_msg_id' in context.user_data:
                del context.user_data['duration_edit_msg_id']
        text = (
            f"⚙️ *Параметры эфира*\n\n"
            f"Стиль: {'Информационный' if params['style'] == 'info' else 'Развлекательный'}\n"
            f"Темп речи: {params['speech_rate']} слов/мин\n"
            f"Хронометраж: {format_duration(params['duration_seconds'])}"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=parameters_keyboard())
        return

    # ---------- стиль ----------
    if data == "set_style_info":
        params['style'] = 'info'
        context.user_data['params'] = params
        update_user_params(user_id, style='info')
        await query.edit_message_text("Выберите стиль речи:", reply_markup=style_keyboard('info'))
        return
    if data == "set_style_entertainment":
        params['style'] = 'entertainment'
        context.user_data['params'] = params
        update_user_params(user_id, style='entertainment')
        await query.edit_message_text("Выберите стиль речи:", reply_markup=style_keyboard('entertainment'))
        return

    # ---------- темп (в параметрах) ----------
    if data.startswith("tempo_preset_"):
        preset = int(data.split("_")[-1])
        params['speech_rate'] = preset
        context.user_data['params'] = params
        update_user_params(user_id, speech_rate=preset)
        await query.edit_message_text(
            f"Текущий темп: {preset} слов/мин\n\nВыберите предустановку или настройте вручную:",
            reply_markup=tempo_main_keyboard(preset)
        )
        return
    if data == "tempo_finetune":
        current = params['speech_rate']
        await query.edit_message_text(
            f"Тонкая настройка темпа\nТекущее значение: {current} слов/мин\n\nИспользуйте кнопки:",
            reply_markup=tempo_finetune_keyboard(current)
        )
        return
    if data in ("tempo_minus_10", "tempo_plus_10"):
        current = params['speech_rate']
        new_tempo = current - 10 if data == "tempo_minus_10" else current + 10
        new_tempo = max(50, min(300, new_tempo))
        params['speech_rate'] = new_tempo
        context.user_data['params'] = params
        update_user_params(user_id, speech_rate=new_tempo)
        await query.edit_message_text(
            f"Тонкая настройка темпа\nТекущее значение: {new_tempo} слов/мин\n\nИспользуйте кнопки:",
            reply_markup=tempo_finetune_keyboard(new_tempo)
        )
        return
    if data == "tempo_finetune_done":
        current = params['speech_rate']
        await query.edit_message_text(
            f"Текущий темп: {current} слов/мин\n\nВыберите предустановку или настройте вручную:",
            reply_markup=tempo_main_keyboard(current)
        )
        return
    if data == "tempo_main_done":
        text = (
            f"⚙️ *Параметры эфира*\n\n"
            f"Стиль: {'Информационный' if params['style'] == 'info' else 'Развлекательный'}\n"
            f"Темп речи: {params['speech_rate']} слов/мин\n"
            f"Хронометраж: {format_duration(params['duration_seconds'])}"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=parameters_keyboard())
        return

    # ---------- выбор темпа для timekeeping ----------
    if data.startswith("tk_tempo_preset_"):
        preset = int(data.split("_")[-1])
        context.user_data['temp_timekeeping_tempo'] = preset
        await query.edit_message_text(
            f"Выберите темп речи для расчёта хронометража.\nТекущий: {preset} слов/мин",
            reply_markup=tempo_for_timekeeping_main_keyboard(preset)
        )
        return
    if data == "tk_tempo_finetune":
        current = context.user_data.get('temp_timekeeping_tempo', 130)
        await query.edit_message_text(
            f"Тонкая настройка темпа\nТекущее значение: {current} слов/мин\n\nИспользуйте кнопки:",
            reply_markup=tempo_for_timekeeping_finetune_keyboard(current)
        )
        return
    if data in ("tk_tempo_minus_10", "tk_tempo_plus_10"):
        current = context.user_data.get('temp_timekeeping_tempo', 130)
        new_tempo = current - 10 if data == "tk_tempo_minus_10" else current + 10
        new_tempo = max(50, min(300, new_tempo))
        context.user_data['temp_timekeeping_tempo'] = new_tempo
        await query.edit_message_text(
            f"Тонкая настройка темпа\nТекущее значение: {new_tempo} слов/мин\n\nИспользуйте кнопки:",
            reply_markup=tempo_for_timekeeping_finetune_keyboard(new_tempo)
        )
        return
    if data == "tk_tempo_finetune_done":
        current = context.user_data.get('temp_timekeeping_tempo', 130)
        await query.edit_message_text(
            f"Выберите темп речи для расчёта хронометража.\nТекущий: {current} слов/мин",
            reply_markup=tempo_for_timekeeping_main_keyboard(current)
        )
        return
    if data == "tk_tempo_main_done":
        tempo = context.user_data.get('temp_timekeeping_tempo', context.user_data['params']['speech_rate'])
        context.user_data['timekeeping_tempo'] = tempo
        context.user_data['awaiting_timekeeping_text'] = True
        await query.edit_message_text(
            f"Темп установлен: {tempo} слов/мин.\n"
            "Отправьте текст для расчёта хронометража.\n"
            "Для отмены используйте /cancel"
        )
        return

    if data == "noop":
        return

# ---------- обработчик текстовых сообщений ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    if user_text.startswith('/'):
        return

    if not context.user_data.get('accepted_terms'):
        await update.message.reply_text("Нажмите /start")
        return

    user_id = update.effective_user.id
    params = context.user_data.get('params', {})

    # 1. ввод хронометража
    if context.user_data.get('awaiting_duration'):
        seconds = parse_duration_input(user_text)
        if seconds is not None:
            params['duration_seconds'] = seconds
            context.user_data['params'] = params
            update_user_params(user_id, duration_seconds=seconds)
            del context.user_data['awaiting_duration']
            msg_id = context.user_data.get('duration_edit_msg_id')
            if msg_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=msg_id,
                        text=f"Хронометраж установлен: {format_duration(seconds)}.",
                        reply_markup=None
                    )
                except Exception as e:
                    logger.warning(f"не удалось отредактировать сообщение: {e}")
                del context.user_data['duration_edit_msg_id']
            text = (
                f"⚙️ *Параметры эфира*\n\n"
                f"Стиль: {'Информационный' if params['style'] == 'info' else 'Развлекательный'}\n"
                f"Темп речи: {params['speech_rate']} слов/мин\n"
                f"Хронометраж: {format_duration(params['duration_seconds'])}"
            )
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=parameters_keyboard())
        else:
            await update.message.reply_text(
                "Ошибка: введите число от 10 до 600, можно с указанием единиц (сек, с, мин, м).\n"
                "Примеры: 45, 90сек, 1.5мин, 2м\nИспользуйте /cancel для отмены."
            )
        return

    # 2. расчёт хронометража (/timekeeping)
    if context.user_data.get('awaiting_timekeeping_text'):
        tempo = context.user_data.get('timekeeping_tempo', params.get('speech_rate', 130))
        word_count = count_words(user_text)
        seconds = (word_count / tempo) * 60
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        if minutes == 0:
            time_str = f"{secs} сек"
        elif secs == 0:
            time_str = f"{minutes} мин"
        else:
            time_str = f"{minutes} мин {secs} сек"

        reply = (
            f"*Хронометраж текста*\n"
            f"Слов: {word_count}\n"
            f"Темп речи: {tempo} слов/мин\n"
            f"Примерное время звучания: *{time_str}*"
        )
        if seconds < 5:
            reply += "\n\nТекст очень короткий – ведущий может не успеть его комфортно прочитать."
        elif seconds > 300:
            reply += "\n\nТекст длиннее 5 минут. Возможно, стоит разбить на части."
        await update.message.reply_text(reply, parse_mode="Markdown")
        del context.user_data['awaiting_timekeeping_text']
        context.user_data.pop('timekeeping_tempo', None)
        context.user_data.pop('temp_timekeeping_tempo', None)
        return

    # 3. расстановка ударений (/stress)
    if context.user_data.get('awaiting_stress'):
        del context.user_data['awaiting_stress']
        if count_words(user_text) > 15:
            await update.message.reply_text("Для расстановки ударений отправьте слово или короткую фразу (до 15 слов).")
            return
        await update.message.chat.send_action(action="typing")
        stressed = await put_stresses(user_text)
        await update.message.reply_text(f"*Ударения:*\n\n{stressed}", parse_mode="Markdown")
        return

    # 4. основная адаптация текста
    progress_msg = await update.message.reply_text("Подключаемся к редакторской... 0%")

    async def update_progress_periodically():
        stages = [
            (10, "Анализ текста... 10%"),
            (20, "Анализ текста... 20%"),
            (30, "Форматирование... 30%"),
            (40, "Форматирование... 40%"),
            (50, "Проверка стиля... 50%"),
            (60, "Проверка стиля... 60%"),
            (70, "Синхронизация с хронометражом... 70%"),
            (80, "Синхронизация с хронометражом... 80%"),
            (90, "Финальная обработка... 90%"),
        ]
        for delay, text in stages:
            await asyncio.sleep(10)
            if progress_task.cancelled():
                break
            try:
                await progress_msg.edit_text(text)
            except Exception:
                break

    progress_task = asyncio.create_task(update_progress_periodically())

    try:
        system_prompt = build_system_prompt(params)
        duration = params['duration_seconds']
        tempo = params['speech_rate']
        max_words = int((tempo * duration) / 60)
        if max_words < 15:
            max_words = 15
        if max_words > 600:
            max_words = 600

        answer = await get_yandexgpt_response(user_text, system_prompt, max_words)
    finally:
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

    style_display = "Информационный" if params['style'] == 'info' else "Развлекательный"
    actual_words = count_words(answer)
    min_words = int(max_words * 0.75)
    warning = ""
    if actual_words < min_words and count_words(user_text) < max_words * 0.8:
        warning = (
            "⚠️ *Исходного материала недостаточно*, чтобы достичь заданного хронометража "
            "без добавления новой информации.\n\n"
            "Текст адаптирован без искусственного увеличения объёма.\n\n"
        )

    result_text = (
        f"{warning}"
        f"*Результат адаптации*\n"
        f"(стиль: {style_display}, темп: {tempo} сл/мин, хронометраж: {format_duration(duration)})\n\n"
        f"{answer}"
    )
    await progress_msg.edit_text(result_text, parse_mode="Markdown")

# ---------- запуск бота ----------
def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("parameters", parameters_command))
    app.add_handler(CommandHandler("timekeeping", timekeeping_command))
    app.add_handler(CommandHandler("stress", stress_command))
    app.add_handler(CommandHandler("legal", legal_command))
    app.add_handler(CommandHandler("deletemydata", deletemydata_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
