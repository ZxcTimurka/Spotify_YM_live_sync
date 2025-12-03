import time
import schedule
import logging
from yandex_music import Client as YandexClient
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import os
from dotenv import load_dotenv

load_dotenv()

# --- НАСТРОЙКИ ---
CONFIG = {
    'yandex_token': os.getenv('YANDEX_TOKEN'),
    'spotify_id': os.getenv('SPOTIPY_CLIENT_ID'),
    'spotify_secret': os.getenv('SPOTIPY_CLIENT_SECRET'),
    'spotify_redirect': os.getenv('SPOTIPY_REDIRECT_URI'),
    
    'check_interval_minutes': 0.1,
    'scan_limit': 15,
}

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger()


class MusicSync:
    def __init__(self):
        self.y_client = None
        self.sp_client = None
        self._init_clients()

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
            logger.info("Клиенты API успешно инициализированы.")
        except Exception as e:
            logger.error(f"Ошибка инициализации: {e}")

    # --- ЯНДЕКС -> SPOTIFY ---
    def sync_yandex_to_spotify(self):
        logger.info("--- [1/2] Проверка: Яндекс -> Spotify ---")
        try:
            # 1. Берем лайки (TrackShort - это безопасно, там только ID)
            likes_list = self.y_client.users_likes_tracks()
            # Берем последние N треков
            recent_tracks_short = likes_list.tracks[: CONFIG["scan_limit"]]

            for short_track in recent_tracks_short:
                try:
                    # 2. Загружаем полную инфу по ОДНОМУ треку
                    # Это защитит от падения всего списка, если один трек "битый"
                    full_track_info = self.y_client.tracks([short_track.id])[0]

                    artist = (
                        full_track_info.artists[0].name
                        if full_track_info.artists
                        else "Unknown"
                    )
                    title = full_track_info.title

                    query = f"artist:{artist} track:{title}"

                    # Поиск в Spotify
                    results = self.sp_client.search(q=query, limit=1, type="track")
                    items = results["tracks"]["items"]

                    if not items:
                        continue

                    sp_track_id = items[0]["id"]

                    # Проверка на наличие (вернет [True] или [False])
                    is_saved = self.sp_client.current_user_saved_tracks_contains(
                        [sp_track_id]
                    )[0]

                    if not is_saved:
                        self.sp_client.current_user_saved_tracks_add([sp_track_id])
                        logger.info(f"[+] Spotify Add: {artist} - {title}")

                    time.sleep(0.5)  # Вежливость к API

                except Exception as inner_e:
                    logger.warning(
                        f"Пропуск битого трека в Яндекс (ID {short_track.id}): {inner_e}"
                    )
                    continue

        except Exception as e:
            logger.error(f"Ошибка в цикле Яндекс->Spotify: {e}")

    # --- SPOTIFY -> ЯНДЕКС ---
    def sync_spotify_to_yandex(self):
        logger.info("--- [2/2] Проверка: Spotify -> Яндекс ---")
        try:
            # 1. Получаем последние лайки Спотифая
            sp_likes = self.sp_client.current_user_saved_tracks(
                limit=CONFIG["scan_limit"]
            )

            # 2. Получаем ВСЕ ID лайков Яндекса для проверки дублей
            # Важно: приводим все к строке (str), чтобы избежать ошибок "123" != 123
            y_likes_obj = self.y_client.users_likes_tracks()
            y_my_ids_set = {str(t.id) for t in y_likes_obj.tracks}

            for item in sp_likes["items"]:
                track = item["track"]
                artist = track["artists"][0]["name"]
                title = track["name"]

                # Поиск в Яндекс
                search_query = f"{artist} - {title}"
                search_result = self.y_client.search(search_query, type_="track")

                if not search_result.tracks or not search_result.tracks.results:
                    continue

                best_match = search_result.tracks.results[0]
                y_found_id = str(best_match.id)  # Приводим к строке

                # ГЛАВНАЯ ПРОВЕРКА
                if y_found_id not in y_my_ids_set:
                    self.y_client.users_likes_tracks_add(best_match.id)
                    logger.info(
                        f"[+] Яндекс Add: {artist} - {title} (ID: {y_found_id})"
                    )
                    # Добавляем в локальный набор, чтобы не добавить еще раз в этом же цикле
                    y_my_ids_set.add(y_found_id)
                else:
                    # Трек уже есть, тихо пропускаем
                    pass

                time.sleep(0.5)

        except Exception as e:
            logger.error(f"Ошибка в цикле Spotify->Яндекс: {e}")

    def run_cycle(self):
        logger.info(">>> Запуск синхронизации")
        self.sync_yandex_to_spotify()
        self.sync_spotify_to_yandex()
        logger.info("<<< Синхронизация завершена")


if __name__ == "__main__":
    syncer = MusicSync()

    # Запуск сразу
    syncer.run_cycle()

    # Планировщик
    schedule.every(CONFIG["check_interval_minutes"]).minutes.do(syncer.run_cycle)

    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Critical Loop Error: {e}")
            time.sleep(60)
