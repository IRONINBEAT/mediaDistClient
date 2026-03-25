import os
import json
import time
import subprocess
import requests
import signal
from datetime import datetime
from urllib.parse import urlparse
from pathlib import Path

CONFIG_FILE = "config.json"
MEDIA_DIR = Path("content").resolve()
PLAYLIST_FILE = MEDIA_DIR / "playlist.m3u"

player_process = None

def log(msg):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{now}] {msg}")

def load_config():
    if not os.path.exists(CONFIG_FILE):
        log("config.json не найден! Создайте его.")
        exit(1)
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

def stop_player():
    global player_process
    if player_process:
        try:
            os.killpg(os.getpgid(player_process.pid), signal.SIGTERM)
        except:
            pass
        player_process = None
        log("Плеер остановлен")

def start_player():
    global player_process
    if not PLAYLIST_FILE.exists():
        log("playlist.m3u не найден")
        return False

    log("Запуск mpv (--vo=xv)")

    cmd = [
        "mpv",
        "--fs",
        "--loop-playlist=inf",
        "--no-osc",
        "--no-audio",
        "--no-border",
        "--keep-open=always",
        "--really-quiet",
        "--vo=xv",           # самый стабильный вариант для Orange Pi
        "--hwdec=auto-safe",
        f"--playlist={PLAYLIST_FILE}"
    ]

    try:
        player_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid
        )
        time.sleep(1.5)
        if player_process.poll() is None:
            log("mpv запущен успешно (--vo=xv)")
            return True
        else:
            log("mpv сразу упал после запуска")
            return False
    except Exception as e:
        log(f"Ошибка запуска mpv: {e}")
        return False


def build_m3u_playlist(videos_data):
    MEDIA_DIR.mkdir(exist_ok=True)
    with open(PLAYLIST_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for v in videos_data:
            fid = v["id"]
            ftype = v.get("file_type", "video")
            playback = v.get("playback", {})

            if ftype == "pdf":
                pages = playback.get("pdf_page_durations", [])
                if not pages:
                    pages = [{"page": 1, "duration": 5}]
                for p in pages:
                    page_file = MEDIA_DIR / f"{fid}_p-{p['page']:03d}.png"
                    if page_file.exists():
                        f.write(f"#EXTINF:{p['duration']},page{p['page']}\n")
                        f.write(f"{page_file}\n")
            else:
                for ext in [".mp4", ".png", ".jpg", ".jpeg"]:
                    candidate = MEDIA_DIR / f"{fid}{ext}"
                    if candidate.exists():
                        duration = playback.get("duration_seconds")
                        dur = duration if duration and duration > 0 else -1
                        f.write(f"#EXTINF:{dur},{fid}\n")
                        f.write(f"{candidate}\n")
                        break


def heartbeat(config):
    try:
        r = requests.post(
            f"{config['server_url']}/api/heartbeat",
            json={"token": config['token'], "id": config['device_id']},
            timeout=8
        )
        data = r.json()
        status = data.get("status")
        if status in (200, "actual") or data.get("success") is True:
            return "ok"
        if status in (403, "403"):
            return "blocked"
        return "invalid"
    except:
        return None


def check_videos(config):
    try:
        # Собираем текущие file_id
        current_ids = []
        for f in MEDIA_DIR.iterdir():
            if f.is_file():
                name = f.stem
                if "_p-" in name:
                    current_ids.append(name.split("_p-")[0])
                else:
                    current_ids.append(name)

        r = requests.post(
            f"{config['server_url']}/api/check-videos",
            json={
                "token": config['token'],
                "id": config['device_id'],
                "videos": list(set(current_ids))
            },
            timeout=15
        )
        data = r.json()

        if data.get("status") == 205:
            log("Получен новый контент (205)")
            stop_player()

            # Очистка старых файлов
            for f in MEDIA_DIR.iterdir():
                if f.is_file():
                    f.unlink()

            # Скачивание новых
            for v in data.get("videos", []):
                fid = v["id"]
                url = v["url"]
                ftype = v.get("file_type", "video")
                ext = os.path.splitext(urlparse(url).path)[1].lower() or ".mp4"
                path = MEDIA_DIR / f"{fid}{ext}"

                log(f"Скачиваю {fid}")
                subprocess.run(["wget", "-q", "-O", str(path), url], check=True)

                if ftype == "pdf":
                    log(f"Конвертирую PDF {fid}")
                    subprocess.run(["pdftoppm", "-r", "150", "-png", str(path),
                                    str(MEDIA_DIR / f"{fid}_p")],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    try:
                        path.unlink()
                    except:
                        pass

            build_m3u_playlist(data.get("videos", []))
            start_player()
            return True
        return False
    except Exception as e:
        log(f"check_videos ошибка: {e}")
        return False


def main():
    config = load_config()
    log("Клиент запущен — стабильная версия с vo=xv")

    signal.signal(signal.SIGINT, lambda *a: stop_player())
    signal.signal(signal.SIGTERM, lambda *a: stop_player())

    last_hb = 0
    last_check = 0

    # Первый запуск плеера
    start_player()

    while True:
        now = time.time()

        # Heartbeat
        if now - last_hb > config.get("heartbeat_interval", 30):
            status = heartbeat(config)
            last_hb = now
            if status == "blocked":
                log("Устройство заблокировано")
                stop_player()

        # Check-videos
        if now - last_check > config.get("check_videos_interval", 60):
            check_videos(config)
            last_check = now

        # Авторестарт mpv если упал
        global player_process
        if player_process and player_process.poll() is not None:
            log("mpv упал — перезапускаем через 2 секунды")
            time.sleep(2)
            start_player()

        time.sleep(1)


if __name__ == "__main__":
    MEDIA_DIR.mkdir(exist_ok=True)
    main()