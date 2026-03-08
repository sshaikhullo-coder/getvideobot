
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
import yt_dlp
import os
import re
import logging
import threading
import queue
import time
import requests
from urllib.parse import urlparse
from datetime import datetime, date
import json
import sqlite3
from functools import wraps
import subprocess
import sys
import traceback
import asyncio
from concurrent.futures import ThreadPoolExecutor
import uuid

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
import yt_dlp
import os
import re
import logging
import threading
import queue
import time
import requests
from urllib.parse import urlparse
from datetime import datetime, date
import json
import sqlite3
from functools import wraps
import subprocess
import sys
import traceback
import asyncio
from concurrent.futures import ThreadPoolExecutor
import uuid
from dotenv import load_dotenv
# ================== LOAD ENV ==================
# .env файлын окуйт жана TOKEN менен ADMIN_ID алат
load_dotenv(".env")
# ================== INIT BOT ==================


# ================== НАСТРОЙКИ ==================
TOKEN = os.getenv ( "BOT_TOKEN" )
ADMIN_ID = int ( os.getenv ( "ADMIN_ID" ) )
MAX_FILE_SIZE = 50 * 1024 * 1024                          # 50 MB
DB_FILE = "bot_database.db"
DOWNLOAD_DIR = "downloads"                                # папка для временных файлов

# ================== ЛОГИРОВАНИЕ ==================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot_log.txt", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ================== БАЗА ДАННЫХ ==================
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cursor = conn.cursor()
# Таблица пользователей
cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    join_date TEXT,
    downloads INTEGER DEFAULT 0,
    premium BOOLEAN DEFAULT 0,
    banned BOOLEAN DEFAULT 0,
    last_daily_reset TEXT
)
''')
# Таблица настроек пользователя
cursor.execute('''
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    video_quality TEXT DEFAULT 'best',
    audio_format TEXT DEFAULT 'mp3',
    FOREIGN KEY(user_id) REFERENCES users(id)
)
''')
# Таблица статистики
cursor.execute('''
CREATE TABLE IF NOT EXISTS stats (
    key TEXT PRIMARY KEY,
    value INTEGER
)
''')
conn.commit()

# ================== ПОТОКОБЕЗОПАСНОСТЬ ==================
db_lock = threading.Lock()
bot = telebot.TeleBot(TOKEN)
executor = ThreadPoolExecutor(max_workers=10)
download_queue = queue.Queue()

# Создаём папку для загрузок, если её нет
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ================== ДЕКОРАТОРЫ ==================
def check_ban(func):
    @wraps(func)
    def wrapper(message, *args, **kwargs):
        user_id = message.from_user.id
        with db_lock:
            # Регистрируем пользователя, если его нет
            cursor.execute("INSERT OR IGNORE INTO users (id, username, first_name, last_name, join_date) VALUES (?, ?, ?, ?, ?)",
                           (user_id, message.from_user.username, message.from_user.first_name,
                            message.from_user.last_name, datetime.now().isoformat()))
            # Проверяем бан
            cursor.execute("SELECT banned FROM users WHERE id=?", (user_id,))
            row = cursor.fetchone()
            if row and row[0] == 1:
                bot.reply_to(message, "🚫 Сен бандалгансың! Ботту колдоно албайсың.")
                return
            conn.commit()
        return func(message, *args, **kwargs)
    return wrapper

def check_ban_callback(func):
    @wraps(func)
    def wrapper(call, *args, **kwargs):
        user_id = call.from_user.id
        with db_lock:
            cursor.execute("SELECT banned FROM users WHERE id=?", (user_id,))
            row = cursor.fetchone()
            if row and row[0] == 1:
                bot.answer_callback_query(call.id, "🚫 Сиз бандалгансыз!", show_alert=True)
                return
        return func(call, *args, **kwargs)
    return wrapper

def admin_only(func):
    @wraps(func)
    def wrapper(message, *args, **kwargs):
        if message.from_user.id != ADMIN_ID:
            bot.reply_to(message, "🚫 Бул команда админ үчүн гана!")
            return
        return func(message, *args, **kwargs)
    return wrapper

def admin_only_callback(func):
    @wraps(func)
    def wrapper(call, *args, **kwargs):
        if call.from_user.id != ADMIN_ID:
            bot.answer_callback_query(call.id, "🚫 Бул админ үчүн гана!", show_alert=True)
            return
        return func(call, *args, **kwargs)
    return wrapper

def rate_limit(func):
    @wraps(func)
    def wrapper(message, *args, **kwargs):
        user_id = message.from_user.id
        today = date.today().isoformat()
        with db_lock:
            # Проверяем премиум
            cursor.execute("SELECT premium FROM users WHERE id=?", (user_id,))
            premium = cursor.fetchone()
            if premium and premium[0] == 1:
                # Премиум – без лимита
                return func(message, *args, **kwargs)

            # Проверяем ежедневный лимит
            cursor.execute("SELECT downloads, last_daily_reset FROM users WHERE id=?", (user_id,))
            row = cursor.fetchone()
            if row:
                dls, last_reset = row
                if last_reset != today:
                    # Сбрасываем счётчик
                    cursor.execute("UPDATE users SET downloads=0, last_daily_reset=? WHERE id=?", (today, user_id))
                    conn.commit()
                    dls = 0
                if dls >= 100:
                    bot.reply_to(message, "⛔ Күнүнө лимит ашты (100)! Эртең кайра аракет кыл же премиум ал.")
                    return
        return func(message, *args, **kwargs)
    return wrapper

def log_action(func):
    @wraps(func)
    def wrapper(message, *args, **kwargs):
        logger.info(f"User {message.from_user.id} executed {func.__name__} with message: {message.text}")
        return func(message, *args, **kwargs)
    return wrapper

# ================== ФУНКЦИИ ПОМОЩНИКИ ==================
def register_user(user):
    with db_lock:
        cursor.execute("""
        INSERT OR IGNORE INTO users (id, username, first_name, last_name, join_date, last_daily_reset) 
        VALUES (?, ?, ?, ?, ?, ?)
        """, (user.id, user.username, user.first_name, user.last_name,
              datetime.now().isoformat(), date.today().isoformat()))
        cursor.execute("""
        UPDATE users SET username=?, first_name=?, last_name=? WHERE id=?
        """, (user.username, user.first_name, user.last_name, user.id))
        conn.commit()

def update_stats(key, increment=1):
    with db_lock:
        cursor.execute("INSERT OR REPLACE INTO stats (key, value) VALUES (?, COALESCE((SELECT value FROM stats WHERE key=?), 0) + ?)",
                       (key, key, increment))
        conn.commit()

def increment_downloads(user_id):
    with db_lock:
        cursor.execute("UPDATE users SET downloads = downloads + 1 WHERE id=?", (user_id,))
        conn.commit()

def is_valid_url(url):
    try:
        parsed = urlparse(url)
        return all([parsed.scheme, parsed.netloc]) and re.match(r'^(https?|ftp)://', url)
    except:
        return False

def detect_platform(url):
    url = url.lower()
    if 'youtube.com' in url or 'youtu.be' in url:
        return 'youtube'
    elif 'tiktok.com' in url:
        return 'tiktok'
    elif 'instagram.com' in url:
        return 'instagram'
    elif 'facebook.com' in url or 'fb.watch' in url:
        return 'facebook'
    elif 'twitter.com' in url or 'x.com' in url:
        return 'twitter'
    else:
        return 'other'

# ================== МЕНЮ (КЛАВИАТУРЫ) ==================
def main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(KeyboardButton("📥 Видео жүктөө"), KeyboardButton("🎬 Кино жүктөө"))
    markup.add(KeyboardButton("📊 Статистика"), KeyboardButton("🆘 Жардам"))
    markup.add(KeyboardButton("⚙️ Орнотуулар"), KeyboardButton("💎 Премиум"))
    markup.add(KeyboardButton("📺 Плейлист жүктөө"), KeyboardButton("🖼️ Сүрөт жүктөө"))
    markup.add(KeyboardButton("ℹ️ Бот жөнүндө"), KeyboardButton("🔍 Издөө"))
    markup.add(KeyboardButton("📄 Текстке айландыруу"))
    return markup

def settings_menu():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("📹 Жогорку (best)", callback_data="set_quality_best"),
        InlineKeyboardButton("📹 1080p", callback_data="set_quality_1080p")
    )
    markup.add(
        InlineKeyboardButton("📹 720p", callback_data="set_quality_720p"),
        InlineKeyboardButton("📹 480p же төмөн", callback_data="set_quality_low")
    )
    markup.add(
        InlineKeyboardButton("🎵 Аудио формат: MP3", callback_data="set_audio_mp3"),
        InlineKeyboardButton("🎵 Аудио формат: WAV", callback_data="set_audio_wav")
    )
    markup.add(InlineKeyboardButton("🔙 Артка", callback_data="back_main"))
    return markup

def premium_menu():
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("💳 Премиум сатып алуу: 100 сом/ай", callback_data="buy_premium"))
    markup.add(InlineKeyboardButton("🔙 Артка", callback_data="back_main"))
    return markup

# ================== ЗАГРУЗЧИК В ПОТОКЕ ==================
def download_worker():
    while True:
        task = download_queue.get()
        if task is None:
            break
        message, url, mode, options = task
        user_id = message.from_user.id
        unique_id = f"{user_id}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        try:
            platform = detect_platform(url)
            logger.info(f"Downloading from {platform}: {url} in mode {mode} for user {user_id}")

            base_opts = {
                'noplaylist': True if mode != 'playlist' else False,
                'quiet': True,
                'no_warnings': True,
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'socket_timeout': 30,
                'retries': 5,
                'fragment_retries': 10,
                'skip_unavailable_fragments': True,
                'outtmpl': os.path.join(DOWNLOAD_DIR, f'{unique_id}_%(title)s.%(ext)s')
            }

            if mode == 'video':
                # Получаем качество из настроек пользователя (по умолчанию best)
                with db_lock:
                    cursor.execute("SELECT video_quality FROM user_settings WHERE user_id=?", (user_id,))
                    q = cursor.fetchone()
                    quality = q[0] if q else 'best'
                # Преобразуем качество в формат yt-dlp
                if quality == '1080p':
                    fmt = 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]'
                elif quality == '720p':
                    fmt = 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]'
                elif quality == 'low':
                    fmt = 'worst[ext=mp4]'
                else:
                    fmt = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]'

                opts = {**base_opts, 'format': fmt}
                file_ext = '.mp4'
            elif mode == 'audio':
                with db_lock:
                    cursor.execute("SELECT audio_format FROM user_settings WHERE user_id=?", (user_id,))
                    af = cursor.fetchone()
                    audio_fmt = af[0] if af else 'mp3'
                opts = {**base_opts,
                        'format': 'bestaudio/best',
                        'postprocessors': [{
                            'key': 'FFmpegExtractAudio',
                            'preferredcodec': audio_fmt,
                            'preferredquality': '192',
                        }]}
                file_ext = f'.{audio_fmt}'
            elif mode == 'thumbnail':
                opts = {**base_opts, 'skip_download': True, 'writethumbnail': True}
                file_ext = '.jpg'
            elif mode == 'subtitle':
                opts = {**base_opts, 'skip_download': True, 'writesubtitles': True,
                        'subtitleslangs': ['en', 'ru', 'ky'], 'subtitlesformat': 'srt'}
                file_ext = '.srt'
            elif mode == 'playlist':
                opts = {**base_opts, 'format': 'best', 'yes_playlist': True}
                file_ext = '.mp4'
            else:
                raise ValueError("Unknown mode")

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info.get('_type') == 'playlist' and mode != 'playlist':
                    bot.send_message(message.chat.id, "📺 Бул плейлист! /playlist командасын колдон.")
                    return
                ydl.download([url])

            # Ищем скачанные файлы (с уникальным префиксом)
            files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(unique_id) and f.endswith(file_ext)]
            if not files:
                # Возможно, расширение другое (например, .webm -> .mp4 после конвертации)
                files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(unique_id)]
            if not files:
                raise FileNotFoundError("No file downloaded")

            for file in files:
                filepath = os.path.join(DOWNLOAD_DIR, file)
                size = os.path.getsize(filepath)
                if size > MAX_FILE_SIZE:
                    bot.send_message(message.chat.id,
                                     f"⚠️ Файл өтө чоң ({size/1024/1024:.2f} MB). Лимит: 50 MB.")
                    os.remove(filepath)
                    continue

                caption = info.get('title', '')[:1024] if info else ''
                with open(filepath, 'rb') as f:
                    if mode == 'video':
                        bot.send_video(message.chat.id, f, caption=caption, timeout=120)
                    elif mode == 'audio':
                        bot.send_audio(message.chat.id, f, caption=caption, timeout=120)
                    elif mode == 'thumbnail':
                        bot.send_photo(message.chat.id, f, caption="🖼️ Thumbnail")
                    elif mode == 'subtitle':
                        bot.send_document(message.chat.id, f, caption="📄 Субтитрлер")
                    elif mode == 'playlist':
                        bot.send_video(message.chat.id, f, caption=file, timeout=120)
                os.remove(filepath)

            # Увеличиваем счётчик загрузок
            increment_downloads(user_id)
            update_stats('total_downloads')

        except Exception as e:
            logger.error(f"Error downloading {url}: {str(e)}\n{traceback.format_exc()}")
            bot.send_message(message.chat.id, f"❌ Ката: {str(e)}. Туура ссылка экенин текшер.")
        finally:
            download_queue.task_done()

# Запускаем несколько рабочих потоков
for _ in range(5):
    threading.Thread(target=download_worker, daemon=True).start()

# ================== ОБРАБОТЧИКИ КОМАНД ==================
@bot.message_handler(commands=['start'])
@log_action
@check_ban
def start(message):
    register_user(message.from_user)
    update_stats('total_users')
    # Уведомление админу о новом пользователе (если новый)
    with db_lock:
        cursor.execute("SELECT join_date FROM users WHERE id=?", (message.from_user.id,))
        join_date = cursor.fetchone()[0]
    if join_date == datetime.now().isoformat()[:10]:  # если сегодня зарегистрирован
        try:
            bot.send_message(ADMIN_ID,
                             f"🆕 Жаңы колдонуучу!\n"
                             f"👤 Аты: {message.from_user.first_name}\n"
                             f"💬 Никнейм: @{message.from_user.username}\n"
                             f"🆔 ID: {message.from_user.id}\n"
                             f"📅 Убакыт: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        except:
            pass

    total_users = cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    text = (f"👋 Салам, [{message.from_user.first_name}](tg://user?id={message.from_user.id})!\n\n"
            f"🤖 *Super Downloader Bot*'ко кош келдиңиз!\n\n"
            f"📥 Видео, кино, музыка жүктөө\n"
            f"📊 Ботту колдонуп жаткан адамдар: {total_users}+")
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=main_menu())

@bot.message_handler(commands=['help'])
@log_action
def help_cmd(message):
    text = """
🆘 Жардам:

Салам!
Эгерде сизге кандайдыр бир суроо же көйгөй жаралса, биз сизге жардам берүүгө даярбыз.
📩 Суроолоруңузду бул жерге жөнөтсөңүз болот: @S_SH_M_00
Биздин команда мүмкүн болушунча тез арада жооп берет.
 

- Ссылканы түз жибер: автоматтык видео жүктөө.
- /video <ссылка> : Видео жүктөө (сапат тандоо менен).
- /mp3 <ссылка> : Аудио жүктөө (MP3/WAV).
- /playlist <ссылка> : Плейлист жүктөө.
- /thumb <ссылка> : Видеонун сүрөтүн жүктөө.
- /sub <ссылка> : Субтитр жүктөө.
- /search <сөз> : YouTube'тан издөө жана жүктөө.
- /stats : Статистика (админ үчүн толук).
- /admin : Админ панели.

Премиум: лимитсиз жүктөө, 4K ж.б.
    """
    bot.send_message(message.chat.id, text, reply_markup=main_menu())

@bot.message_handler(commands=['id'])
def my_id(message):
    bot.reply_to(message, f"Сенин Telegram ID: {message.from_user.id}")

@bot.message_handler(commands=['admin'])
@admin_only
def admin_panel(message):
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("📊 Толук статистика", callback_data="admin_stats"),
        InlineKeyboardButton("📤 Бродкаст", callback_data="admin_broadcast")
    )
    markup.add(
        InlineKeyboardButton("🚫 Бан", callback_data="admin_ban"),
        InlineKeyboardButton("🔓 Унбан", callback_data="admin_unban")
    )
    markup.add(
        InlineKeyboardButton("🔄 Ботту кайра жүктөө", callback_data="admin_restart")
    )
    bot.send_message(message.chat.id, "🛡️ Админ панели:", reply_markup=markup)





@bot.message_handler(commands=['stats'])
@admin_only
def stats(message):
    with db_lock:
        total_users = cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_dls = cursor.execute("SELECT value FROM stats WHERE key='total_downloads'").fetchone()
        total_dls = total_dls[0] if total_dls else 0
        users = cursor.execute("SELECT id, username, first_name, downloads FROM users ORDER BY downloads DESC LIMIT 50").fetchall()

    text = f"📊 Статистика (админ):\n👥 Колдонуучулар: {total_users}\n📥 Жүктөөлөр: {total_dls}\n\n"

    if users:
        text += "👥 Активдүү колдонуучулар (топ 50):\n"
        for i, (uid, uname, fname, dls) in enumerate(users, 1):
            uname = f"@{uname}" if uname else "жок"
            text += f"{i}. {fname} ({uname}) | 🆔 {uid} – 📥 {dls} жүктөө\n"
    else:
        text += "Колдонуучулар жок."

    bot.send_message(message.chat.id, text)





@bot.message_handler(commands=['stats'])
@admin_only
def stats(message):
    with db_lock:
        total_users = cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_dls = cursor.execute("SELECT value FROM stats WHERE key='total_downloads'").fetchone()
        total_dls = total_dls[0] if total_dls else 0
        users = cursor.execute("SELECT id, username, first_name, downloads FROM users ORDER BY downloads DESC LIMIT 50").fetchall()

    text = f"📊 Статистика (админ):\n👥 Колдонуучулар: {total_users}\n📥 Жүктөөлөр: {total_dls}\n\n"
    if users:
        text += "👥 Активдүү колдонуучулар (топ 50):\n"
        for i, (uid, uname, fname, dls) in enumerate(users, 1):
            uname = f"@{uname}" if uname else "жок"
            text += f"{i}. {fname} ({uname}) – {dls} жүктөө\n"
    else:
        text += "Колдонуучулар жок."

    # Разбиваем, если слишком длинно
    if len(text) > 4000:
        for x in range(0, len(text), 4000):
            bot.send_message(message.chat.id, text[x:x+4000])
    else:
        bot.send_message(message.chat.id, text)

@bot.message_handler(commands=['ban'])
@admin_only
def ban_user(message):
    try:
        parts = message.text.split()
        user_id = None
        for p in parts:
            if p.isdigit():
                user_id = int(p)
                break
        if not user_id:
            raise ValueError
        with db_lock:
            cursor.execute("UPDATE users SET banned=1 WHERE id=?", (user_id,))
            conn.commit()
        bot.reply_to(message, f"🚫 Колдонуулучу {user_id} чектетилди.")
    except:
        bot.reply_to(message, "❌ Формат: /ban <user_id>")

@bot.message_handler(commands=['unban'])
@admin_only
def unban_user(message):
    try:
        parts = message.text.split()
        user_id = None
        for p in parts:
            if p.isdigit():
                user_id = int(p)
                break
        if not user_id:
            raise ValueError
        with db_lock:
            cursor.execute("UPDATE users SET banned=0 WHERE id=?", (user_id,))
            conn.commit()
        bot.reply_to(message, f"✅ Колдонуулучу {user_id} чектетилди.")
    except:
        bot.reply_to(message, "❌ Формат: /unban <user_id>")

@bot.message_handler(commands=['myban'])
def my_ban_status(message):
    uid = message.from_user.id
    with db_lock:
        cursor.execute("SELECT banned FROM users WHERE id=?", (uid,))
        row = cursor.fetchone()
    if not row:
        bot.reply_to(message, "Сиз ботто катталган эмессиз.")
    elif row[0] == 1:
        bot.reply_to(message, "🚫 Сиз бандалгансыз!")
    else:
        bot.reply_to(message, "✅ Аккаунтуңуз таза.")

@bot.message_handler(commands=['video', 'mp3', 'playlist', 'thumb', 'sub'])
@rate_limit
@log_action
def download_command(message):
    command = message.text.split()[0][1:]  # без слеша
    try:
        url = message.text.split(maxsplit=1)[1]
        if not is_valid_url(url):
            raise ValueError("Invalid URL")
        mode_map = {
            'video': 'video',
            'mp3': 'audio',
            'playlist': 'playlist',
            'thumb': 'thumbnail',
            'sub': 'subtitle'
        }
        mode = mode_map.get(command, 'video')
        bot.reply_to(message, f"⏳ Жүктөө башталды ({command})...")
        download_queue.put((message, url, mode, {}))
    except IndexError:
        bot.reply_to(message, f"❌ /{command} <ссылка> форматында жибер.")
    except ValueError:
        bot.reply_to(message, "❌ Туура эмес ссылка.")

@bot.message_handler(commands=['search'])
@log_action
def search_youtube(message):
    try:
        query = message.text.split(maxsplit=1)[1]
        bot.reply_to(message, f"🔍 '{query}' боюнча издөө...")

        def search_task():
            ydl_opts = {'quiet': True, 'extract_flat': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                results = ydl.extract_info(f"ytsearch10:{query}", download=False)['entries']
            if not results:
                bot.send_message(message.chat.id, "❌ Эч нерсе табылган жок.")
                return
            markup = InlineKeyboardMarkup(row_width=1)
            for res in results:
                title = res['title'][:50]
                url = res['url']
                markup.add(InlineKeyboardButton(f"📹 {title}", callback_data=f"dl_video:{url}"))
            bot.send_message(message.chat.id, "Издөө натыйжалары:", reply_markup=markup)
        executor.submit(search_task)
    except IndexError:
        bot.reply_to(message, "❌ /search <сөз> форматында жибер.")

# ================== АВТОМАТИЧЕСКАЯ ЗАГРУЗКА ПО ССЫЛКЕ ==================
@bot.message_handler(func=lambda m: is_valid_url(m.text))
@rate_limit
@log_action
@check_ban
def auto_download(message):
    url = message.text.strip()
    bot.reply_to(message, "⏳ Автоматтык жүктөө башталды (видео)...")
    download_queue.put((message, url, 'video', {}))

# ================== ОБРАБОТКА КНОПОК ГЛАВНОГО МЕНЮ ==================
@bot.message_handler(func=lambda m: m.text == "📥 Видео жүктөө")
def menu_video(message):
    bot.reply_to(message, "Видео жүктөө үчүн YouTube же башка сайттан ссылка жибериңиз.")

@bot.message_handler(func=lambda m: m.text == "🎬 Кино жүктөө")
def menu_movie(message):
    bot.reply_to(message, "Кино жүктөө үчүн ссылка жибериңиз (MP3 катары сакталат).")

@bot.message_handler(func=lambda m: m.text == "📊 Статистика")
def menu_stats(message):
    with db_lock:
        total_dls = cursor.execute("SELECT value FROM stats WHERE key='total_downloads'").fetchone()
        total_dls = total_dls[0] if total_dls else 0
        user_dls = cursor.execute("SELECT downloads FROM users WHERE id=?", (message.from_user.id,)).fetchone()
        user_dls = user_dls[0] if user_dls else 0
    text = f"📊 Жалпы жүктөөлөр: {total_dls}\nСиз жүктөгөн: {user_dls}"
    bot.reply_to(message, text)

@bot.message_handler(func=lambda m: m.text == "🆘 Жардам")
def menu_help(message):
    help_cmd(message)

@bot.message_handler(func=lambda m: m.text == "⚙️ Орнотуулар")
def show_settings(message):
    bot.send_message(message.chat.id, "⚙️ Орнотуулар:", reply_markup=settings_menu())

@bot.message_handler(func=lambda m: m.text == "💎 Премиум")
def show_premium(message):
    text = """
💎 Премиум мүмкүнчүлүктөр:
• Лимитсиз жүктөө (күнүнө 100+)
• 4K жана жогорку сапаттагы видео
• Приоритеттүү жүктөө (кезек жок)
• Жарнамасыз иштөө (келечекте)

Баасы: 100 сом / 1 ай

Төлөм ыкмалары:
✅ MBank, Elsom, O!Money, Visa/Mastercard

Төлөө үчүн админге жазыңыз: @S_SH_M_00
"""
    bot.send_message(message.chat.id, text, reply_markup=premium_menu())

@bot.message_handler(func=lambda m: m.text == "📺 Плейлист жүктөө")
def menu_playlist(message):
    bot.reply_to(message, "Плейлист жүктөө үчүн плейлисттин ссылкасын /playlist менен жибериңиз.")

@bot.message_handler(func=lambda m: m.text == "🖼️ Сүрөт жүктөө")
def menu_thumbnail(message):
    bot.reply_to(message, "Видеонун сүрөтүн (thumbnail) алуу үчүн /thumb <ссылка> колдонуңуз.")

@bot.message_handler(func=lambda m: m.text == "ℹ️ Бот жөнүндө")
@check_ban
def about(message):
    text = """
🤖 **SUPER DOWNLOADER BOT**

📥 YouTube, TikTok, Instagram, Facebook ж.б. платформалардан видео жүктөө
───────────────────────
👨‍💻 Developer: Shaikhullo
💻 Telegram + yt-dlp + telebot
📅 2026
───────────────────────
Жеке долбоор • © Shaikhullo Dev
    """
    bot.reply_to(message, text, parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text == "🔍 Издөө")
def menu_search(message):
    bot.reply_to(message, "Издөө үчүн /search <сөз> командасын колдонуңуз.")

@bot.message_handler(func=lambda m: m.text == "📄 Текстке айландыруу")
def transcribe(message):
    bot.reply_to(message, "🎤 Аудио/видео файл жибер, текстке айландырам (келечектеги функция).")

# ================== ОБРАБОТКА INLINE КНОПОК ==================
@bot.callback_query_handler(func=lambda call: True)
@check_ban_callback
def callback_handler(call):
    data = call.data
    user_id = call.from_user.id

    # Настройки качества видео
    if data.startswith("set_quality_"):
        quality_map = {
            "set_quality_best": "best",
            "set_quality_1080p": "1080p",
            "set_quality_720p": "720p",
            "set_quality_low": "low"
        }
        selected = quality_map.get(data)
        if selected:
            with db_lock:
                cursor.execute("INSERT OR REPLACE INTO user_settings (user_id, video_quality) VALUES (?, ?)",
                               (user_id, selected))
                conn.commit()
            bot.answer_callback_query(call.id, f"Видео сапаты: {selected} тандалды ✓")
            # Обновим меню (не обязательно)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                          reply_markup=settings_menu())

    # Настройки аудио формата
    elif data.startswith("set_audio_"):
        fmt = "mp3" if data == "set_audio_mp3" else "wav"
        with db_lock:
            cursor.execute("INSERT OR REPLACE INTO user_settings (user_id, audio_format) VALUES (?, ?)",
                           (user_id, fmt))
            conn.commit()
        bot.answer_callback_query(call.id, f"Аудио формат: {fmt.upper()} тандалды ✓")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=settings_menu())

    # Загрузка по результатам поиска
    elif data.startswith("dl_video:"):
        url = data.split(":", 1)[1]
        bot.answer_callback_query(call.id, "Жүктөө башталды...")
        # Создаём фиктивное сообщение для очереди
        class FakeMessage:
            def __init__(self, chat, user):
                self.chat = chat
                self.from_user = user
        fake_msg = FakeMessage(call.message.chat, call.from_user)
        download_queue.put((fake_msg, url, 'video', {}))

    # Возврат в главное меню (если нужно скрыть инлайн клавиатуру)
    elif data == "back_main":
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, "Башкы меню:", reply_markup=main_menu())

    # Премиум покупка
    elif data == "buy_premium":
        bot.answer_callback_query(call.id, "Төлөм системасы иштелип жатат. Админге жазыңыз.")
        bot.send_message(call.message.chat.id,
                         "Премиум сатып алуу үчүн админге жазыңыз: @S_SH_M_00")

    # ========== АДМИН КНОПКИ ==========
    elif data == "admin_stats":
        if user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "🚫 Жеткиликтүү эмес!", show_alert=True)
            return
        # Просто вызываем команду /stats
        stats(call.message)

    elif data == "admin_broadcast":
        if user_id != ADMIN_ID:
            return
        bot.send_message(call.message.chat.id,
                         "📤 Бродкаст режими. Жөнөтүлүүчү билдирүүнү жазыңыз (текст, сүрөт, видео).")
        bot.register_next_step_handler(call.message, process_broadcast)

    elif data == "admin_ban":
        if user_id != ADMIN_ID:
            return
        bot.send_message(call.message.chat.id, "Бан кылынуучу колдонуучунун ID'син жазыңыз:")
        bot.register_next_step_handler(call.message, process_ban)

    elif data == "admin_unban":
        if user_id != ADMIN_ID:
            return
        bot.send_message(call.message.chat.id, "Унбан кылынуучу колдонуучунун ID'син жазыңыз:")
        bot.register_next_step_handler(call.message, process_unban)

    elif data == "admin_restart":
        if user_id != ADMIN_ID:
            return
        bot.send_message(call.message.chat.id, "🔄 Бот кайра жүктөлүүдө...")
        # Останавливаем потоки и перезапускаем
        for _ in range(5):
            download_queue.put(None)
        time.sleep(1)
        python = sys.executable
        os.execl(python, python, *sys.argv)

# ================== ШАГИ ДЛЯ АДМИНА ==================
def process_broadcast(message):
    if message.from_user.id != ADMIN_ID:
        return
    # Получаем всех пользователей
    with db_lock:
        users = cursor.execute("SELECT id FROM users").fetchall()
    sent = 0
    for (uid,) in users:
        try:
            # Копируем сообщение (может быть текст, фото, видео и т.д.)
            if message.content_type == 'text':
                bot.send_message(uid, message.text)
            elif message.content_type == 'photo':
                bot.send_photo(uid, message.photo[-1].file_id, caption=message.caption)
            elif message.content_type == 'video':
                bot.send_video(uid, message.video.file_id, caption=message.caption)
            elif message.content_type == 'document':
                bot.send_document(uid, message.document.file_id, caption=message.caption)
            else:
                continue
            sent += 1
        except Exception as e:
            logger.warning(f"Не удалось отправить {uid}: {e}")
    bot.send_message(message.chat.id, f"📤 Бродкаст жөнөтүлдү: {sent} адамга")

def process_ban(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(message.text.strip())
        with db_lock:
            cursor.execute("UPDATE users SET banned=1 WHERE id=?", (uid,))
            conn.commit()
        bot.reply_to(message, f"🚫 Пользователь {uid} забанен.")
    except:
        bot.reply_to(message, "❌ Туура ID жазыңыз.")

def process_unban(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(message.text.strip())
        with db_lock:
            cursor.execute("UPDATE users SET banned=0 WHERE id=?", (uid,))
            conn.commit()
        bot.reply_to(message, f"✅ Пользователь {uid} разбанен.")
    except:
        bot.reply_to(message, "❌ Туура ID жазыңыз.")

# ================== ЗАПУСК ==================
if __name__ == "__main__":
    logger.info("🤖 Super Bot иштеп жатат...")
    try:
        bot.infinity_polling(timeout=30, long_polling_timeout=30)
    except Exception as e:
        logger.error(f"Polling error: {e}")
    finally:
        # Останавливаем потоки загрузки
        for _ in range(5):
            download_queue.put(None)
        executor.shutdown(wait=True)
        conn.close()