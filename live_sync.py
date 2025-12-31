import time
import schedule
import logging
import json
import os
import threading
import datetime
from yandex_music import Client as YandexClient
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv
from difflib import SequenceMatcher
import telebot

load_dotenv()

# --- НАСТРОЙКИ ---
CONFIG = {
    "yandex_token": os.getenv("YANDEX_TOKEN"),
    "spotify_id": os.getenv("SPOTIPY_CLIENT_ID"),
    "spotify_secret": os.getenv("SPOTIPY_CLIENT_SECRET"),
    "spotify_redirect": os.getenv("SPOTIPY_REDIRECT_URI"),
    "tg_token": os.getenv("TELEGRAM_BOT_TOKEN"),
    "tg_chat_id": os.getenv("TELEGRAM_CHAT_ID"),
    "check_interval_minutes": 15,
    "scan_limit": 10,
    "match_threshold": 0.8,
    "max_retries": 5,
    "duration_threshold_sec": 10,
    "ignore_file": "ignore_list.json",
}

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger()


class MusicSync:
    def __init__(self):
        self.y_client = None
        self.sp_client = None
        self.ignore_db = {}

        # Статусные переменные для бота
        self.last_run = "Не запускался"
        self.is_running = False
        self.stats = {"added_y": 0, "added_s": 0, "errors": 0}

        # Инициализация
        self._init_clients()
        self._load_ignore_db()

        # Инициализация бота
        self.bot = None
        if CONFIG["tg_token"]:
            self.bot = telebot.TeleBot(CONFIG["tg_token"])

    def _init_clients(self):
        try:
            self.y_client = YandexClient(CONFIG["yandex_token"]).init()
            self.sp_client = spotipy.Spotify(
                auth_manager=SpotifyOAuth(
                    client_id=CONFIG["spotify_id"],
                    client_secret=CONFIG["spotify_secret"],
                    redirect_uri=CONFIG["spotify_redirect"],
                    scope="user-library-read user-library-modify",
                )
            )
            logger.info("✅ API подключены.")
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации API: {e}")
            self.send_tg(f"🚨 Ошибка старта: {e}")

    def send_tg(self, message):
        if self.bot and CONFIG["tg_chat_id"]:
            try:
                self.bot.send_message(CONFIG["tg_chat_id"], message)
            except Exception as e:
                logger.error(f"Ошибка отправки TG: {e}")

    def _load_ignore_db(self):
        if os.path.exists(CONFIG["ignore_file"]):
            try:
                with open(CONFIG["ignore_file"], "r", encoding="utf-8") as f:
                    self.ignore_db = json.load(f)
            except Exception:
                self.ignore_db = {}

    def _save_ignore_db(self):
        try:
            with open(CONFIG["ignore_file"], "w", encoding="utf-8") as f:
                json.dump(self.ignore_db, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def should_skip_track(self, unique_name):
        return self.ignore_db.get(unique_name, 0) >= CONFIG["max_retries"]

    def register_failure(self, unique_name):
        self.ignore_db[unique_name] = self.ignore_db.get(unique_name, 0) + 1
        self._save_ignore_db()

    def check_similarity(self, source_str, found_str):
        ratio = SequenceMatcher(None, source_str.lower(), found_str.lower()).ratio()
        is_match = ratio >= CONFIG["match_threshold"]
        icon = "✅" if is_match else "❌"
        logger.info(f"   [{ratio:.2f}] {icon} Текст: {source_str} <--> {found_str}")
        return is_match

    def check_duration(self, dur_ms_1, dur_ms_2):
        if not dur_ms_1 or not dur_ms_2:
            return True

        diff_ms = abs(dur_ms_1 - dur_ms_2)
        diff_sec = diff_ms / 1000
        is_ok = diff_sec <= CONFIG["duration_threshold_sec"]

        icon = "✅" if is_ok else "❌"
        logger.info(
            f"   [{int(diff_sec)}s] {icon} Время: {int(dur_ms_1 / 1000)}s <--> {int(dur_ms_2 / 1000)}s"
        )
        return is_ok

    def sync_yandex_to_spotify(self):
        logger.info("--- 🔄 Яндекс -> Spotify ---")
        try:
            likes = self.y_client.users_likes_tracks().tracks[: CONFIG["scan_limit"]]

            for short_track in likes:
                try:
                    ft = self.y_client.tracks([short_track.id])[0]
                    artist = ft.artists[0].name if ft.artists else "Unknown"
                    title = ft.title
                    y_dur = ft.duration_ms

                    unique_key = f"Y2S: {artist} - {title}"
                    if self.should_skip_track(unique_key):
                        continue

                    # Поиск
                    query = f"artist:{artist} track:{title}"
                    results = self.sp_client.search(q=query, limit=1, type="track")
                    items = results["tracks"]["items"]

                    if not items:
                        self.register_failure(unique_key)
                        continue

                    s_track = items[0]
                    s_dur = s_track["duration_ms"]
                    s_name_full = f"{s_track['artists'][0]['name']} - {s_track['name']}"
                    y_name_full = f"{artist} - {title}"

                    # 1. Проверка текста
                    if not self.check_similarity(y_name_full, s_name_full):
                        self.register_failure(unique_key)
                        continue

                    # 2. Проверка длительности
                    if not self.check_duration(y_dur, s_dur):
                        self.register_failure(unique_key)
                        continue

                    # Добавление
                    sp_id = s_track["id"]
                    if not self.sp_client.current_user_saved_tracks_contains([sp_id])[
                        0
                    ]:
                        self.sp_client.current_user_saved_tracks_add([sp_id])
                        msg = f"✅ Y -> S: {artist} - {title}"
                        logger.info(msg)
                        self.send_tg(msg)
                        self.stats["added_s"] += 1

                    time.sleep(0.5)
                except Exception as e:
                    logger.warning(f"Ошибка трека Y: {e}")

        except Exception as e:
            logger.error(f"Global Error Y->S: {e}")
            self.stats["errors"] += 1

    def sync_spotify_to_yandex(self):
        logger.info("--- 🔄 Spotify -> Яндекс ---")
        try:
            sp_likes = self.sp_client.current_user_saved_tracks(
                limit=CONFIG["scan_limit"]
            )
            y_likes_obj = self.y_client.users_likes_tracks()
            y_my_ids_set = {str(t.id) for t in y_likes_obj.tracks}

            for item in sp_likes["items"]:
                track = item["track"]
                s_artist = track["artists"][0]["name"]
                s_title = track["name"]
                s_dur = track["duration_ms"]

                unique_key = f"S2Y: {s_artist} - {s_title}"
                if self.should_skip_track(unique_key):
                    continue

                search_res = self.y_client.search(
                    f"{s_artist} - {s_title}", type_="track"
                )

                if not search_res.tracks or not search_res.tracks.results:
                    self.register_failure(unique_key)
                    continue

                best_match = search_res.tracks.results[0]
                y_dur = best_match.duration_ms
                y_name_full = f"{best_match.artists[0].name} - {best_match.title}"
                s_name_full = f"{s_artist} - {s_title}"

                # 1. Проверка текста
                if not self.check_similarity(s_name_full, y_name_full):
                    self.register_failure(unique_key)
                    continue

                # 2. Проверка длительности
                if not self.check_duration(s_dur, y_dur):
                    self.register_failure(unique_key)
                    continue

                y_found_id = str(best_match.id)
                if y_found_id not in y_my_ids_set:
                    self.y_client.users_likes_tracks_add(best_match.id)
                    msg = f"✅ S -> Y: {s_artist} - {s_title}"
                    logger.info(msg)
                    self.send_tg(msg)
                    self.stats["added_y"] += 1
                    y_my_ids_set.add(y_found_id)

                time.sleep(0.5)

        except Exception as e:
            logger.error(f"Global Error S->Y: {e}")
            self.stats["errors"] += 1

    def run_cycle(self):
        if self.is_running:
            logger.warning("Попытка запуска, но цикл уже идет.")
            return

        self.is_running = True
        self.last_run = datetime.datetime.now().strftime("%H:%M:%S")

        self.sync_yandex_to_spotify()
        self.sync_spotify_to_yandex()

        logger.info(f"💤 Сон {CONFIG['check_interval_minutes']} мин...")
        self.is_running = False


syncer = MusicSync()

if syncer.bot:

    @syncer.bot.message_handler(commands=["start", "help"])
    def send_welcome(message):
        syncer.bot.reply_to(
            message,
            "🎵 Бот синхронизации активен!\n\n/status - Статус\n/sync - Принудительный запуск",
        )

    @syncer.bot.message_handler(commands=["status"])
    def send_status(message):
        state = "🏃‍♂️ Работает" if syncer.is_running else "💤 Ждет"
        text = (
            f"📊 **Статус системы**\n"
            f"Состояние: {state}\n"
            f"Последний запуск: {syncer.last_run}\n"
            f"------------------\n"
            f"✅ Добавлено в Yandex: {syncer.stats['added_y']}\n"
            f"✅ Добавлено в Spotify: {syncer.stats['added_s']}\n"
            f"⚠️ Ошибок цикла: {syncer.stats['errors']}"
        )
        syncer.bot.reply_to(message, text, parse_mode="Markdown")

    @syncer.bot.message_handler(commands=["sync"])
    def force_sync(message):
        if syncer.is_running:
            syncer.bot.reply_to(message, "⏳ Синхронизация уже идет!")
        else:
            syncer.bot.reply_to(message, "🚀 Запускаю принудительную синхронизацию...")
            threading.Thread(target=syncer.run_cycle).start()


def run_bot_polling():
    if syncer.bot:
        try:
            logger.info("🤖 Бот Telegram запущен")
            syncer.bot.infinity_polling()
        except Exception as e:
            logger.error(f"Ошибка бота: {e}")


if __name__ == "__main__":
    # 1. Запускаем бота в фоне
    if CONFIG["tg_token"]:
        bot_thread = threading.Thread(target=run_bot_polling)
        bot_thread.daemon = True
        bot_thread.start()

        syncer.send_tg("🖥 Скрипт синхронизации успешно запущен!")

    # 2. Запускаем первый прогон
    syncer.run_cycle()

    # 3. Планировщик
    schedule.every(CONFIG["check_interval_minutes"]).minutes.do(syncer.run_cycle)

    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Остановка скрипта...")
            break
        except Exception as e:
            logger.error(f"Critical Loop Error: {e}")
            time.sleep(60)
