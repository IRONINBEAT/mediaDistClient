import json
import time
import os
import threading
import subprocess
from pathlib import Path
import requests

# ====================== НАСТРОЙКИ ======================
DOWNLOAD_DIR = Path("media")
CONFIG_FILE = Path("config.json")
PLAYLIST_FILE = DOWNLOAD_DIR / "playlist.m3u"
# ======================================================

class MediaClient:
    def __init__(self):
        self.config = self._load_config()
        self.current_playlist = []
        self.local_files = {}
        self.device_status = "unknown"
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.mpv_process = None

        DOWNLOAD_DIR.mkdir(exist_ok=True)
        self._start_mpv()                      # сразу пытаемся запустить плеер

    def _load_config(self):
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        
        default_config = {
            "server_url": "http://217.71.129.139:5909",
            "device_id": "ВАШ_DEVICE_ID",
            "token": "ВАШ_ТОКЕН",
            "heartbeat_interval": 30,
            "check_videos_interval": 60
        }
        self._save_config(default_config)
        print("✅ Создан config.json — заполни device_id и token")
        exit(0)

    def _save_config(self, cfg=None):
        if cfg is None:
            cfg = self.config
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def _api_post(self, endpoint, payload):
        url = f"{self.config['server_url']}{endpoint}"
        try:
            r = requests.post(url, json=payload, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"❌ API ошибка {endpoint}: {e}")
            return None

    # ====================== ЗАПУСК MPV ======================
    def _start_mpv(self):
        """Запускаем mpv с выводом ошибок в консоль"""
        if self.mpv_process and self.mpv_process.poll() is None:
            return

        # Флаги, хорошо работающие на Orange Pi / ARM
        cmd = [
            "mpv",
            "--fullscreen",
            "--no-terminal",           # убираем терминал mpv
            "--loop-playlist=inf",
            f"--playlist={PLAYLIST_FILE}",
            "--osc=no",
            "--no-border",
            "--keep-open=always",
            "--vo=gpu",                # или "xv" если gpu не работает
            "--hwdec=auto",            # аппаратное декодирование
            "--really-quiet",
            "--log-file=mpv.log"       # логируем ошибки mpv
        ]

        print("🚀 Запускаем mpv...")
        try:
            self.mpv_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True
            )
            time.sleep(1.5)

            # Проверяем, жив ли процесс
            if self.mpv_process.poll() is None:
                print("✅ mpv успешно запущен (окно должно появиться)")
            else:
                print("❌ mpv упал сразу после запуска. Проверь mpv.log")
                # Выводим последние строки лога
                if Path("mpv.log").exists():
                    print("Последние строки mpv.log:")
                    with open("mpv.log", "r") as f:
                        print("".join(f.readlines()[-10:]))

        except FileNotFoundError:
            print("❌ Ошибка: mpv не найден. Установи: sudo apt install mpv")
        except Exception as e:
            print(f"❌ Ошибка запуска mpv: {e}")

    def _rebuild_playlist(self):
        """Создаём M3U плейлист"""
        print(f"🔄 Создаём плейлист ({len(self.current_playlist)} файлов)")

        with open(PLAYLIST_FILE, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for item in self.current_playlist:
                fid = item["id"]
                path = self.local_files.get(fid)
                if not path or not Path(path).exists():
                    continue

                playback = item.get("playback", {})
                duration = playback.get("duration_seconds")

                if duration and duration > 0:
                    f.write(f"#EXTINF:{duration},{fid}\n")
                else:
                    f.write(f"#EXTINF:-1,{fid}\n")

                f.write(str(path) + "\n")

    def _rebuild_and_restart_mpv(self):
        self._rebuild_playlist()
        self._start_mpv()               # перезапускаем mpv с новым плейлистом

    # ====================== HEARTBEAT & CHECK ======================
    def heartbeat(self):
        data = {"token": self.config["token"], "id": self.config["device_id"]}
        resp = self._api_post("/api/heartbeat", data)
        if resp and resp.get("status") is not None:
            status = resp.get("status")
            with self.lock:
                self.device_status = {200: "active", 401: "unverified", 403: "blocked"}.get(status, "unknown")
            print(f"❤️ Heartbeat → {status} ({self.device_status})")

    def check_videos(self):
        with self.lock:
            current_ids = list(self.local_files.keys())

        data = {
            "token": self.config["token"],
            "id": self.config["device_id"],
            "videos": current_ids
        }
        resp = self._api_post("/api/check-videos", data)
        if not resp or not resp.get("answer"):
            return

        if resp.get("status") == 205:
            print("🔄 Новый контент — обновляем")
            self._update_playlist(resp.get("videos", []))
        elif resp.get("status") == 204:
            print("📦 Контент актуален")

    def _update_playlist(self, videos_data):
        new_ids = {v["id"] for v in videos_data}

        # Удаляем лишние файлы
        with self.lock:
            for fid in list(self.local_files.keys()):
                if fid not in new_ids:
                    p = self.local_files.pop(fid, None)
                    if p and Path(p).exists():
                        Path(p).unlink()

        # Скачиваем новые
        for item in videos_data:
            fid = item["id"]
            if fid in self.local_files and Path(self.local_files[fid]).exists():
                continue
            try:
                local_path = self._download_file(item["url"], fid, item["file_type"])
                with self.lock:
                    self.local_files[fid] = local_path
            except Exception as e:
                print(f"❌ Скачивание {fid} не удалось: {e}")

        with self.lock:
            self.current_playlist = videos_data[:]

        self._rebuild_and_restart_mpv()

    def _download_file(self, url, file_id, file_type):
        local_path = DOWNLOAD_DIR / f"{file_id}_{os.path.basename(url)}"
        if local_path.exists():
            return str(local_path)

        print(f"⬇️ Скачиваем {file_id}")
        r = requests.get(url, stream=True, timeout=60)
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192*4):
                f.write(chunk)

        if file_type == "pdf":
            self._render_pdf_pages(str(local_path), file_id)
        return str(local_path)

    def _render_pdf_pages(self, pdf_path, file_id):
        # ... (оставляем как было)
        pass   # можешь оставить старую реализацию

    def run(self):
        print("🚀 Запуск клиента...")
        self.heartbeat()

        hb = threading.Thread(target=self._heartbeat_loop, daemon=True)
        cv = threading.Thread(target=self._check_videos_loop, daemon=True)
        hb.start()
        cv.start()

        try:
            while not self.stop_event.is_set():
                time.sleep(5)
        except KeyboardInterrupt:
            print("\n🛑 Остановка")
        finally:
            self.stop_event.set()
            if self.mpv_process:
                self.mpv_process.terminate()

    def _heartbeat_loop(self):
        while not self.stop_event.is_set():
            self.heartbeat()
            time.sleep(self.config["heartbeat_interval"])

    def _check_videos_loop(self):
        while not self.stop_event.is_set():
            if self.device_status == "active":
                self.check_videos()
            time.sleep(self.config["check_videos_interval"])


if __name__ == "__main__":
    client = MediaClient()
    client.run()