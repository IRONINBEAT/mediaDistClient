import json
import time
import os
import threading
import subprocess
from pathlib import Path
import requests
from typing import Dict, List, Any

# ====================== НАСТРОЙКИ ======================
DOWNLOAD_DIR = Path("media")
CONFIG_FILE = Path("config.json")
# ======================================================

class MediaClient:
    def __init__(self):
        self.config = self._load_config()
        self.current_playlist: List[Dict] = []
        self.local_files: Dict[str, str] = {}          # file_id -> полный путь
        self.device_status = "unknown"
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        DOWNLOAD_DIR.mkdir(exist_ok=True)

    def _load_config(self) -> dict:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        
        # Первый запуск — создаём шаблон
        default_config = {
            "server_url": "http://217.71.129.139:5909",
            "device_id": "NSTU_OrangePI2302",          # ← ИЗМЕНИ НА СВОЙ
            "token": "ВАШ_ТОКЕН_ИЗ_ВЕБ_ПАНЕЛИ",       # ← ОБЯЗАТЕЛЬНО ЗАМЕНИ!
            "heartbeat_interval": 30,
            "check_videos_interval": 60
        }
        self._save_config(default_config)
        print("✅ Создан config.json. Открой его, заполни token и device_id, затем перезапусти скрипт.")
        exit(0)

    def _save_config(self, cfg=None):
        if cfg is None:
            cfg = self.config
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def _api_post(self, endpoint: str, payload: dict):
        url = f"{self.config['server_url']}{endpoint}"
        try:
            r = requests.post(url, json=payload, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            print(f"❌ API {endpoint} ошибка: {e}")
            return None

    # ====================== СИНХРОНИЗАЦИЯ ТОКЕНА ======================
    def _try_sync_token(self) -> bool:
        print("🔄 Пробуем синхронизировать токен (sync-token)...")
        data = {
            "token": self.config["token"],
            "id": self.config["device_id"]
        }
        resp = self._api_post("/api/sync-token", data)
        if not resp:
            return False

        if resp.get("success") and resp.get("status") == "updated":
            new_token = resp.get("new_token")
            if new_token:
                self.config["token"] = new_token
                self._save_config()
                print(f"✅ Токен обновлён → {new_token[:30]}...")
                return True
        elif resp.get("success") and resp.get("status") == "actual":
            print("✅ Токен уже актуален")
            return True
        else:
            print(f"⚠️ sync-token: {resp.get('message', 'неизвестная ошибка')}")
            return False

    # ====================== HEARTBEAT ======================
    def heartbeat(self):
        data = {"token": self.config["token"], "id": self.config["device_id"]}
        resp = self._api_post("/api/heartbeat", data)
        if not resp:
            self._try_sync_token()
            return

        status_code = resp.get("status")
        with self.lock:
            self.device_status = {
                200: "active",
                401: "unverified",
                403: "blocked"
            }.get(status_code, "unknown")

        print(f"❤️ Heartbeat → status={status_code} ({self.device_status})")

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
            if resp and resp.get("status") in (403,):
                self._try_sync_token()
            return

        if resp.get("status") == 204:
            print("📦 Контент актуален (204 No Content)")
            return

        if resp.get("status") == 205:
            print("🔄 Обновляем контент (205 Reset Content)")
            self._update_playlist(resp.get("videos", []))
        else:
            print(f"⚠️ Неизвестный статус: {resp.get('status')}")

    def _update_playlist(self, videos_data: List[dict]):
        new_ids = {v["id"] for v in videos_data}

        # Удаляем файлы, которых больше нет в плейлисте
        with self.lock:
            for fid in list(self.local_files.keys()):
                if fid not in new_ids:
                    path = self.local_files.pop(fid, None)
                    if path and Path(path).exists():
                        Path(path).unlink()
                    # удаляем папку с страницами PDF
                    pages_dir = DOWNLOAD_DIR / f"{fid}_pages"
                    if pages_dir.exists():
                        for f in pages_dir.glob("*"):
                            f.unlink()
                        pages_dir.rmdir()

        # Скачиваем новые файлы
        for item in videos_data:
            fid = item["id"]
            if fid in self.local_files and Path(self.local_files[fid]).exists():
                continue

            url = item["url"]
            ftype = item["file_type"]
            print(f"⬇️ Скачиваем {fid} ({ftype})")

            try:
                local_path = self._download_file(url, fid, ftype)
                with self.lock:
                    self.local_files[fid] = local_path
            except Exception as e:
                print(f"❌ Ошибка скачивания {fid}: {e}")

        with self.lock:
            self.current_playlist = videos_data[:]
        print(f"✅ Плейлист обновлён: {len(videos_data)} файлов")

    def _download_file(self, url: str, file_id: str, file_type: str) -> str:
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

    def _render_pdf_pages(self, pdf_path: str, file_id: str):
        pages_dir = DOWNLOAD_DIR / f"{file_id}_pages"
        pages_dir.mkdir(exist_ok=True, parents=True)
        for old in pages_dir.glob("*.png"):
            old.unlink()

        prefix = str(pages_dir / "page")
        try:
            subprocess.run(["pdftoppm", "-png", pdf_path, prefix], 
                          check=True, capture_output=True)
            print(f"📄 PDF {file_id} → страницы извлечены")
        except FileNotFoundError:
            print("⚠️ pdftoppm не найден (установи poppler-utils)")
        except Exception as e:
            print(f"❌ Ошибка рендеринга PDF: {e}")

    # ====================== ВОСПРОИЗВЕДЕНИЕ ======================
    def _play_item(self, item: dict):
        fid = item["id"]
        local_path = self.local_files.get(fid)
        if not local_path or not Path(local_path).exists():
            print(f"⚠️ Файл {fid} отсутствует")
            time.sleep(3)
            return

        ftype = item["file_type"]
        playback = item.get("playback", {})
        duration = playback.get("duration_seconds")

        try:
            if ftype == "video":
                cmd = ["mpv", "--fullscreen", "--no-terminal", "--loop=no"]
                if duration:
                    cmd += [f"--length={duration}"]
                cmd.append(local_path)
                subprocess.run(cmd)

            elif ftype == "image":
                dur = duration or 5
                cmd = ["mpv", "--fullscreen", "--no-terminal",
                       f"--image-display-duration={dur}", "--loop=no", local_path]
                subprocess.run(cmd)

            elif ftype == "pdf":
                pages = playback.get("pdf_page_durations", [])
                if not pages:
                    pages = [{"page": 1, "duration": 5}]
                for p in pages:
                    page_path = self._get_pdf_page_path(fid, p["page"])
                    if page_path:
                        cmd = ["mpv", "--fullscreen", "--no-terminal",
                               f"--image-display-duration={p['duration']}", "--loop=no", page_path]
                        subprocess.run(cmd)
                    else:
                        time.sleep(p["duration"])

        except Exception as e:
            print(f"❌ Ошибка воспроизведения {ftype} {fid}: {e}")

    def _get_pdf_page_path(self, file_id: str, page_num: int) -> str | None:
        pages_dir = DOWNLOAD_DIR / f"{file_id}_pages"
        if not pages_dir.exists():
            return None
        pngs = sorted(pages_dir.glob("*.png"), key=lambda p: int(''.join(filter(str.isdigit, p.stem.split('-')[-1])) or 0))
        idx = page_num - 1
        return str(pngs[idx]) if 0 <= idx < len(pngs) else None

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

            time.sleep(0.5)

    def run(self):
        print("🚀 Запуск медиа-клиента...")
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


if __name__ == "__main__":
    client = MediaClient()
    client.run()