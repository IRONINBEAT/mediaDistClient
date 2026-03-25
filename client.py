import json
import time
import os
import threading
import subprocess
from pathlib import Path
import requests
import socket

# ====================== НАСТРОЙКИ ======================
DOWNLOAD_DIR = Path("media")
CONFIG_FILE = Path("config.json")
MPV_SOCKET = "/tmp/mpv_socket"
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
        self._start_mpv()

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
        print("✅ Создан config.json. Заполни device_id и token!")
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
            "mpv", "--fullscreen", "--no-terminal", "--idle=yes",
            "--loop-playlist=no", f"--input-ipc-server={MPV_SOCKET}",
            "--force-window=yes", "--osc=no", "--no-border",
            "--keep-open=always", "--really-quiet",
            "--reset-on-next-file=all",
            "--vo=gpu", "--hwdec=auto"
        ]

        self.mpv_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.2)
        print("🎥 mpv запущен")

    def _send_mpv_command(self, command):
        if not Path(MPV_SOCKET).exists():
            self._start_mpv()
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(MPV_SOCKET)
            sock.send((json.dumps({"command": command}) + "\n").encode())
            sock.close()
        except:
            self._start_mpv()

    # ====================== ОСНОВНАЯ ЛОГИКА ======================
    def _try_sync_token(self):
        data = {"token": self.config["token"], "id": self.config["device_id"]}
        resp = self._api_post("/api/sync-token", data)
        if resp and resp.get("success") and resp.get("status") == "updated":
            self.config["token"] = resp.get("new_token")
            self._save_config()
            print("✅ Токен обновлён")
        return True

    def heartbeat(self):
        data = {"token": self.config["token"], "id": self.config["device_id"]}
        resp = self._api_post("/api/heartbeat", data)
        if resp:
            status = resp.get("status")
            self.device_status = {200: "active", 401: "unverified", 403: "blocked"}.get(status, "unknown")
            print(f"❤️ Heartbeat → {status} ({self.device_status})")

    def check_videos(self):
        with self.lock:
            current_ids = list(self.local_files.keys())

        data = {"token": self.config["token"], "id": self.config["device_id"], "videos": current_ids}
        resp = self._api_post("/api/check-videos", data)

        if not resp or not resp.get("answer"):
            return

        if resp.get("status") == 205:
            print("🔄 Обновляем контент...")
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
                    (DOWNLOAD_DIR / f"{fid}_pages").rmdir(exist_ok=True)  # упрощённо

        for item in videos_data:
            fid = item["id"]
            if fid in self.local_files and Path(self.local_files[fid]).exists():
                continue
            try:
                path = self._download_file(item["url"], fid, item["file_type"])
                with self.lock:
                    self.local_files[fid] = path
            except Exception as e:
                print(f"❌ Скачивание {fid}: {e}")

        with self.lock:
            self.current_playlist = videos_data[:]
        print(f"✅ Плейлист обновлён: {len(videos_data)} файлов")

    def _download_file(self, url, file_id, file_type):
        basename = os.path.basename(url)
        local_path = DOWNLOAD_DIR / f"{file_id}_{basename}"
        if local_path.exists():
            return str(local_path)

        r = requests.get(url, stream=True, timeout=60)
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(8192*4):
                f.write(chunk)

        if file_type == "pdf":
            self._render_pdf_pages(str(local_path), file_id)
        return str(local_path)

    def _render_pdf_pages(self, pdf_path, file_id):
        pages_dir = DOWNLOAD_DIR / f"{file_id}_pages"
        pages_dir.mkdir(exist_ok=True, parents=True)
        for f in pages_dir.glob("*.png"):
            f.unlink()

        prefix = str(pages_dir / "page")
        try:
            subprocess.run(["pdftoppm", "-png", pdf_path, prefix], check=True, capture_output=True)
            print(f"📄 PDF {file_id} → страницы извлечены")
        except Exception as e:
            print(f"⚠️ pdftoppm ошибка: {e}")

    def _get_pdf_page_path(self, file_id, page_num):
        pages_dir = DOWNLOAD_DIR / f"{file_id}_pages"
        if not pages_dir.exists():
            return None
        pngs = sorted(pages_dir.glob("*.png"),
                      key=lambda p: int(''.join(filter(str.isdigit, p.stem.split('-')[-1])) or 0))
        idx = page_num - 1
        return str(pngs[idx]) if 0 <= idx < len(pngs) else None

    # ====================== ИСПРАВЛЕННОЕ ВОСПРОИЗВЕДЕНИЕ ======================
    def _play_item(self, item):
        fid = item["id"]
        local_path = self.local_files.get(fid)
        if not local_path or not Path(local_path).exists():
            print(f"⚠️ Файл {fid} отсутствует")
            time.sleep(3)
            return

        ftype = item["file_type"]
        playback = item.get("playback", {})
        print(f"▶️  Воспроизводим: {fid} ({ftype})")

        try:
            # Полная остановка перед каждым новым файлом
            self._send_mpv_command(["stop"])
            time.sleep(0.25)

            if ftype == "video":
                self._send_mpv_command(["loadfile", local_path, "replace"])
                duration = playback.get("duration_seconds") or 30
                time.sleep(duration)
                self._send_mpv_command(["stop"])

            elif ftype == "image":
                dur = playback.get("duration_seconds") or 5
                self._send_mpv_command(["loadfile", local_path, "replace"])
                self._send_mpv_command(["set", "image-display-duration", str(dur)])
                time.sleep(dur)

            elif ftype == "pdf":
                pages = playback.get("pdf_page_durations", [])
                if not pages:
                    pages = [{"page": 1, "duration": 5}]

                for p in pages:
                    page_path = self._get_pdf_page_path(fid, p["page"])
                    if page_path:
                        self._send_mpv_command(["loadfile", page_path, "replace"])
                        time.sleep(p["duration"] + 0.15)   # + небольшая пауза
                    else:
                        time.sleep(p["duration"])

                self._send_mpv_command(["stop"])   # после всех страниц PDF

        except Exception as e:
            print(f"❌ Ошибка воспроизведения {fid}: {e}")
            self._start_mpv()

    # ====================== ЦИКЛЫ ======================
    def _heartbeat_loop(self):
        while not self.stop_event.is_set():
            self.heartbeat()
            time.sleep(self.config["heartbeat_interval"])

    def _check_videos_loop(self):
        while not self.stop_event.is_set():
            if self.device_status == "active":
                self.check_videos()
            time.sleep(self.config["check_videos_interval"])

    def _playback_loop(self):
        while not self.stop_event.is_set():
            with self.lock:
                if self.device_status != "active" or not self.current_playlist:
                    time.sleep(5)
                    continue
                playlist = self.current_playlist[:]

            for item in playlist:
                if self.stop_event.is_set():
                    break
                self._play_item(item)

            time.sleep(0.4)

    def run(self):
        print("🚀 Запуск клиента...")
        self.heartbeat()
        self.check_videos()

        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        threading.Thread(target=self._check_videos_loop, daemon=True).start()

        try:
            self._playback_loop()
        except KeyboardInterrupt:
            print("\n🛑 Завершение")
        finally:
            self.stop_event.set()
            if self.mpv_process:
                self.mpv_process.terminate()


if __name__ == "__main__":
    client = MediaClient()
    client.run()