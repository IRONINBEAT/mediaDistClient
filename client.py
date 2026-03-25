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
        self.local_files = {}          # file_id -> путь
        self.device_status = "unknown"
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.mpv_process = None

        DOWNLOAD_DIR.mkdir(exist_ok=True)
        self._start_mpv()              # сразу запускаем плеер

    def _load_config(self):
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        
        default_config = {
            "server_url": "http://217.71.129.139:4085",
            "device_id": "NSTU_OrangePI2302",  
            "token": "ВАШ_ТОКЕН_ИЗ_ПАНЕЛИ",           
            "heartbeat_interval": 30,
            "check_videos_interval": 60
        }
        self._save_config(default_config)
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
        except requests.exceptions.RequestException as e:
            print(f"❌ API {endpoint} ошибка: {e}")
            return None

    # ====================== MPV + M3U ПЛЕЙЛИСТ ======================
    def _start_mpv(self):
        """Запускаем mpv один раз с плейлистом"""
        if self.mpv_process and self.mpv_process.poll() is None:
            return

        cmd = [
            "mpv",
            "--fullscreen",
            "--no-terminal",
            "--loop-playlist=inf",      # бесконечный цикл плейлиста
            f"--playlist={PLAYLIST_FILE}",
            "--osc=no",
            "--no-border",
            "--keep-open=always",
            "--really-quiet"
        ]

        self.mpv_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
        print("🎥 mpv запущен с M3U-плейлистом")

    def _rebuild_and_restart_mpv(self):
        """Перестраиваем плейлист и перезапускаем mpv (самый стабильный способ)"""
        print("🔄 Перестраиваем плейлист и перезапускаем mpv...")

        # 1. Убиваем текущий mpv
        if self.mpv_process:
            self.mpv_process.terminate()
            try:
                self.mpv_process.wait(timeout=2)
            except:
                self.mpv_process.kill()
            time.sleep(0.6)

        # 2. Создаём новый M3U
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
                    f.write(f"#EXTINF:-1,{fid}\n")   # -1 = полная длительность видео

                f.write(f"{path}\n")

        # 3. Запускаем mpv заново
        self._start_mpv()

    # ====================== HEARTBEAT ======================
    def heartbeat(self):
        data = {"token": self.config["token"], "id": self.config["device_id"]}
        resp = self._api_post("/api/heartbeat", data)
        if not resp:
            return

        status_code = resp.get("status")
        with self.lock:
            self.device_status = {200: "active", 401: "unverified", 403: "blocked"}.get(status_code, "unknown")
        print(f"❤️ Heartbeat → {status_code} ({self.device_status})")

    # ====================== CHECK-VIDEOS ======================
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

        if resp.get("status") == 204:
            print("📦 Контент актуален")
            return

        if resp.get("status") == 205:
            print("🔄 Новый контент от сервера — обновляем плейлист")
            self._update_playlist(resp.get("videos", []))

    def _update_playlist(self, videos_data):
        new_ids = {v["id"] for v in videos_data}

        # Удаляем старые файлы
        with self.lock:
            for fid in list(self.local_files.keys()):
                if fid not in new_ids:
                    path = self.local_files.pop(fid, None)
                    if path and Path(path).exists():
                        Path(path).unlink()
                    pages_dir = DOWNLOAD_DIR / f"{fid}_pages"
                    if pages_dir.exists():
                        for f in pages_dir.glob("*"):
                            f.unlink()
                        pages_dir.rmdir()

        # Скачиваем новые
        for item in videos_data:
            fid = item["id"]
            if fid in self.local_files and Path(self.local_files[fid]).exists():
                continue

            print(f"⬇️ Скачиваем {fid} ({item['file_type']})")
            try:
                local_path = self._download_file(item["url"], fid, item["file_type"])
                with self.lock:
                    self.local_files[fid] = local_path
            except Exception as e:
                print(f"❌ Ошибка скачивания {fid}: {e}")

        with self.lock:
            self.current_playlist = videos_data[:]

        # Перезапускаем mpv с новым плейлистом
        self._rebuild_and_restart_mpv()

    def _download_file(self, url, file_id, file_type):
        basename = os.path.basename(url)
        local_path = DOWNLOAD_DIR / f"{file_id}_{basename}"

        if local_path.exists():
            return str(local_path)

        r = requests.get(url, stream=True, timeout=60)
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192 * 4):
                f.write(chunk)

        if file_type == "pdf":
            self._render_pdf_pages(str(local_path), file_id)

        return str(local_path)

    def _render_pdf_pages(self, pdf_path, file_id):
        pages_dir = DOWNLOAD_DIR / f"{file_id}_pages"
        pages_dir.mkdir(exist_ok=True, parents=True)
        for old in pages_dir.glob("*.png"):
            old.unlink()

        prefix = str(pages_dir / "page")
        try:
            subprocess.run(["pdftoppm", "-png", pdf_path, prefix], check=True, capture_output=True)
            print(f"📄 PDF {file_id} → страницы извлечены")
        except Exception as e:
            print(f"⚠️ Ошибка рендеринга PDF: {e}")

    # ====================== ФОНОВЫЕ ЦИКЛЫ ======================
    def _heartbeat_loop(self):
        while not self.stop_event.is_set():
            self.heartbeat()
            time.sleep(self.config["heartbeat_interval"])

    def _check_videos_loop(self):
        while not self.stop_event.is_set():
            if self.device_status == "active":
                self.check_videos()
            time.sleep(self.config["check_videos_interval"])

    def run(self):
        print("🚀 Запуск медиа-клиента (M3U + mpv)")
        self.heartbeat()
        self.check_videos()

        hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        cv_thread = threading.Thread(target=self._check_videos_loop, daemon=True)

        hb_thread.start()
        cv_thread.start()

        try:
            while not self.stop_event.is_set():
                time.sleep(10)   # главный поток просто живёт
        except KeyboardInterrupt:
            print("\n🛑 Завершение")
        finally:
            self.stop_event.set()
            if self.mpv_process:
                self.mpv_process.terminate()


if __name__ == "__main__":
    client = MediaClient()
    client.run()