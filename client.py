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
        self.local_files = {}          # file_id -> полный путь к файлу
        self.device_status = "unknown"
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.mpv_process = None

        DOWNLOAD_DIR.mkdir(exist_ok=True)
        self._start_mpv()              # запускаем mpv один раз

    def _load_config(self):
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        
        # Первый запуск — создаём шаблон
        default_config = {
            "server_url": "http://217.71.129.139:5909",
            "device_id": "NSTU_OrangePI2302",          # ← ИЗМЕНИ
            "token": "ВАШ_ТОКЕН_ИЗ_ПАНЕЛИ",            # ← ОБЯЗАТЕЛЬНО ЗАМЕНИ
            "heartbeat_interval": 30,
            "check_videos_interval": 60
        }
        self._save_config(default_config)
        print("✅ Создан config.json. Заполни device_id и token, затем перезапусти.")
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

    # ====================== MPV В ФОНОВОМ РЕЖИМЕ (IPC) ======================
    def _start_mpv(self):
        """Запускаем mpv один раз с IPC-сокетом"""
        if self.mpv_process and self.mpv_process.poll() is None:
            return

        cmd = [
            "mpv",
            "--fullscreen",
            "--no-terminal",
            "--idle=yes",
            "--loop-playlist=no",
            f"--input-ipc-server={MPV_SOCKET}",
            "--force-window=yes",
            "--osc=no",
            "--no-border",
            "--keep-open=always",
            "--really-quiet"
        ]

        self.mpv_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)  # даём mpv полностью запуститься
        print("🎥 mpv запущен в фоновом режиме (IPC)")

    def _send_mpv_command(self, command):
        """Отправка команды через сокет"""
        if not Path(MPV_SOCKET).exists():
            self._start_mpv()

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(MPV_SOCKET)
            sock.send((json.dumps({"command": command}) + "\n").encode())
            sock.close()
        except Exception:
            self._start_mpv()

    # ====================== СИНХРОНИЗАЦИЯ ТОКЕНА ======================
    def _try_sync_token(self):
        print("🔄 Пробуем синхронизировать токен...")
        data = {"token": self.config["token"], "id": self.config["device_id"]}
        resp = self._api_post("/api/sync-token", data)
        if not resp:
            return False

        if resp.get("success") and resp.get("status") == "updated":
            new_token = resp.get("new_token")
            if new_token:
                self.config["token"] = new_token
                self._save_config()
                print(f"✅ Токен обновлён")
                return True
        return True

    # ====================== HEARTBEAT ======================
    def heartbeat(self):
        data = {"token": self.config["token"], "id": self.config["device_id"]}
        resp = self._api_post("/api/heartbeat", data)
        if not resp:
            self._try_sync_token()
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
            if resp and resp.get("status") == 403:
                self._try_sync_token()
            return

        if resp.get("status") == 204:
            print("📦 Контент актуален")
            return

        if resp.get("status") == 205:
            print("🔄 Обновляем плейлист...")
            self._update_playlist(resp.get("videos", []))

    def _update_playlist(self, videos_data):
        new_ids = {v["id"] for v in videos_data}

        # Удаляем ненужные файлы
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
        print(f"✅ Плейлист обновлён ({len(videos_data)} файлов)")

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

    # ====================== ПЛАВНОЕ ВОСПРОИЗВЕДЕНИЕ ======================
    def _get_pdf_page_path(self, file_id, page_num):
        pages_dir = DOWNLOAD_DIR / f"{file_id}_pages"
        if not pages_dir.exists():
            return None
        
        pngs = sorted(
            pages_dir.glob("*.png"),
            key=lambda p: int(''.join(filter(str.isdigit, p.stem.split('-')[-1])) or 0)
        )
        idx = page_num - 1
        return str(pngs[idx]) if 0 <= idx < len(pngs) else None

    def _play_item(self, item):
        fid = item["id"]
        local_path = self.local_files.get(fid)
        if not local_path or not Path(local_path).exists():
            print(f"⚠️ Файл {fid} отсутствует")
            time.sleep(3)
            return

        ftype = item["file_type"]
        playback = item.get("playback", {})
        duration = playback.get("duration_seconds")

        print(f"▶️  Воспроизводим: {fid} ({ftype})")

        try:
            if ftype == "video":
                # Для видео загружаем один раз и ждём окончания
                self._send_mpv_command(["loadfile", local_path, "replace"])
                
                if duration:
                    # Если в панели задана конкретная длительность — ждём её
                    time.sleep(duration)
                else:
                    # Иначе ждём реальную длительность видео + небольшую паузу
                    time.sleep(0.5)  # даём mpv начать воспроизведение
                    # Здесь можно добавить более точное ожидание через get_property, но для простоты:
                    # просто ждём чуть больше, чем длительность видео (mpv сам остановится)
                    # Но надёжнее — просто дать mpv доиграть и перейти дальше
                    # Для зацикливания одного видео лучше использовать loop в mpv

                # Если плейлист состоит только из одного видео — зацикливаем его внутри mpv
                with self.lock:
                    if len(self.current_playlist) == 1 and ftype == "video":
                        self._send_mpv_command(["set", "loop", "inf"])
                    else:
                        self._send_mpv_command(["set", "loop", "no"])

            elif ftype == "image":
                dur = duration or 5
                self._send_mpv_command(["loadfile", local_path, "replace"])
                self._send_mpv_command(["set", "image-display-duration", str(dur)])
                time.sleep(dur + 0.2)   # небольшая пауза после изображения

            elif ftype == "pdf":
                pages = playback.get("pdf_page_durations", [])
                if not pages:
                    pages = [{"page": 1, "duration": 5}]
                
                for p in pages:
                    page_path = self._get_pdf_page_path(fid, p["page"])
                    if page_path:
                        self._send_mpv_command(["loadfile", page_path, "replace"])
                        time.sleep(p["duration"])
                    else:
                        time.sleep(p["duration"])

        except Exception as e:
            print(f"❌ Ошибка воспроизведения {fid}: {e}")
            self._start_mpv()

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

            time.sleep(0.8 if len(playlist) == 1 else 0.3)

    def run(self):
        print("🚀 Запуск медиа-клиента с плавным переключением...")
        self.heartbeat()
        self.check_videos()

        hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        cv_thread = threading.Thread(target=self._check_videos_loop, daemon=True)

        hb_thread.start()
        cv_thread.start()

        try:
            self._playback_loop()
        except KeyboardInterrupt:
            print("\n🛑 Завершение по Ctrl+C")
        finally:
            self.stop_event.set()
            if self.mpv_process:
                self.mpv_process.terminate()


if __name__ == "__main__":
    client = MediaClient()
    client.run()