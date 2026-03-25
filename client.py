import json
import time
import os
import threading
import subprocess
from pathlib import Path
import requests

# ====================== НАСТРОЙКИ ======================
DOWNLOAD_DIR = Path("media").resolve()          # абсолютный путь!
CONFIG_FILE = Path("config.json")
PLAYLIST_FILE = DOWNLOAD_DIR / "playlist.m3u"
# ======================================================

class MediaClient:
    def __init__(self):
        self.config = self._load_config()
        self.current_playlist = []
        self.local_files = {}                   # file_id -> абсолютный путь
        self.device_status = "unknown"
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.mpv_process = None

        DOWNLOAD_DIR.mkdir(exist_ok=True)
        self._start_mpv()

    def _load_config(self):
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        
        default = {
            "server_url": "http://217.71.129.139:5909",
            "device_id": "ТВОЙ_DEVICE_ID",
            "token": "ТВОЙ_ТОКЕН",
            "heartbeat_interval": 30,
            "check_videos_interval": 60
        }
        self._save_config(default)
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
            print(f"❌ API {endpoint}: {e}")
            return None

    # ====================== MPV ======================
    def _start_mpv(self):
        if self.mpv_process and self.mpv_process.poll() is None:
            return

        cmd = [
            "mpv",
            "--fullscreen",
            "--no-terminal",
            "--loop-playlist=inf",
            f"--playlist={PLAYLIST_FILE}",
            "--osc=no",
            "--no-border",
            "--keep-open=always",
            "--vo=gpu",           # попробуй потом --vo=xv или --vo=drm если не запустится
            "--hwdec=auto",
            "--really-quiet",
            "--log-file=mpv.log"
        ]

        print("🚀 Запускаем mpv...")
        try:
            self.mpv_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            time.sleep(1.5)
            if self.mpv_process.poll() is None:
                print("✅ mpv запущен")
            else:
                print("❌ mpv упал. Смотри mpv.log")
        except Exception as e:
            print(f"❌ Ошибка запуска mpv: {e}")

    def _rebuild_playlist(self):
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

                f.write(f"{path}\n")          # теперь абсолютный путь!

    def _rebuild_and_restart_mpv(self):
        self._rebuild_playlist()
        self._start_mpv()

    # ====================== HEARTBEAT & CHECK ======================
    def heartbeat(self):
        data = {"token": self.config["token"], "id": self.config["device_id"]}
        resp = self._api_post("/api/heartbeat", data)
        if resp and "status" in resp:
            status = resp["status"]
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

        with self.lock:
            for fid in list(self.local_files.keys()):
                if fid not in new_ids:
                    p = self.local_files.pop(fid, None)
                    if p and Path(p).exists():
                        Path(p).unlink()

        for item in videos_data:
            fid = item["id"]
            if fid in self.local_files and Path(self.local_files[fid]).exists():
                continue
            try:
                local_path = self._download_file(item["url"], fid, item["file_type"])
                with self.lock:
                    self.local_files[fid] = local_path
            except Exception as e:
                print(f"❌ Скачивание {fid} ошибка: {e}")

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

        return str(local_path)                     # возвращаем абсолютный путь

    def _render_pdf_pages(self, pdf_path, file_id):
        # оставляем как было раньше (или можешь отключить пока)
        pass

    def run(self):
        print("🚀 Запуск клиента (исправленные пути)...")
        self.heartbeat()

        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        threading.Thread(target=self._check_videos_loop, daemon=True).start()

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