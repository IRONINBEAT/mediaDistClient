#!/usr/bin/env python3
"""
Digital signage client for the Media-Content Distribution System.
Handles token sync, heartbeat, playlist updates and media playback.
"""

import json
import os
import shutil
import sys
import threading
import time
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

import requests
import vlc
from pdf2image import convert_from_path

# ------------------ Configuration ------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("client")

DEFAULT_CONFIG = {
    "server_url": "http://217.71.129.139:4085",
    "device_id": "NSTU_OrangePI2302",
    "token": "",
    "media_dir": "./content",
    "heartbeat_interval": 30,
    "check_videos_interval": 60,
    "image_display_duration": 5
}

@dataclass
class VideoItem:
    """Representation of a media file from the server."""
    id: str
    url: str
    duration_config: Optional[Dict] = None


class Client:
    def __init__(self, config_path: str = "config.json"):
        self.config_path = config_path
        self.config = self._load_config()
        self.media_dir = self.config["media_dir"]
        os.makedirs(self.media_dir, exist_ok=True)

        # Current playlist (ordered list of VideoItem)
        self.playlist: List[VideoItem] = []
        self.playlist_lock = threading.Lock()

        # Flag to signal player that playlist changed
        self.playlist_updated = threading.Event()

        # Control flag for graceful shutdown
        self.running = True

        # VLC instance
        self.vlc_instance = vlc.Instance("--no-xlib")  # for headless use
        self.player = self.vlc_instance.media_player_new()

        # Background worker thread
        self.worker_thread = None

    def _load_config(self) -> dict:
        """Load configuration from JSON file, create default if missing."""
        if not os.path.exists(self.config_path):
            logger.warning(f"Config file {self.config_path} not found, creating default.")
            with open(self.config_path, "w") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            return DEFAULT_CONFIG.copy()
        with open(self.config_path, "r") as f:
            config = json.load(f)
        # Ensure all keys exist
        for k, v in DEFAULT_CONFIG.items():
            if k not in config:
                config[k] = v
        return config

    def _save_config(self):
        """Write current configuration to file."""
        with open(self.config_path, "w") as f:
            json.dump(self.config, f, indent=2)
        logger.info("Configuration saved.")

    def _request(self, endpoint: str, data: dict) -> Optional[dict]:
        """Send a POST request with JSON data, return parsed response or None on error."""
        url = f"{self.config['server_url'].rstrip('/')}{endpoint}"
        try:
            resp = requests.post(url, json=data, timeout=10)
            resp.raise_for_status()
            # Some responses may not be JSON (e.g., 204 No Content)
            if resp.status_code == 204:
                return None
            return resp.json()
        except requests.RequestException as e:
            logger.error(f"Request to {endpoint} failed: {e}")
            return None

    # ---------- API methods ----------

    def sync_token(self) -> bool:
        """Call /api/sync-token and handle possible token update."""
        data = {
            "token": self.config["token"],
            "id": self.config["device_id"]
        }
        response = self._request("/api/sync-token", data)
        if response is None:
            logger.error("No response from sync-token")
            return False

        if response.get("success"):
            status = response.get("status")
            if status == "actual":
                logger.info("Token is valid and active.")
                return True
            elif status == "updated":
                new_token = response.get("new_token")
                if new_token:
                    logger.info(f"Token updated to {new_token[:8]}...")
                    self.config["token"] = new_token
                    self._save_config()
                    return True
                else:
                    logger.error("Server returned 'updated' but no new_token")
                    return False
            else:
                logger.error(f"Unexpected sync-token status: {status}")
                return False
        else:
            logger.error(f"Token sync failed: {response.get('message')}")
            return False

    def heartbeat(self) -> Tuple[bool, int, str]:
        """
        Send heartbeat to server.
        Returns (should_continue, status_code, message).
        """
        data = {
            "token": self.config["token"],
            "id": self.config["device_id"]
        }
        response = self._request("/api/heartbeat", data)
        if response is None:
            # No response means network error
            return (False, -1, "Network error")
        answer = response.get("answer")
        status = response.get("status")
        message = response.get("message", "")
        if answer is True:
            # status 200 (active), 401 (unverified), 403 (blocked)
            if status == 200:
                logger.info("Heartbeat OK: device active")
                return (True, status, message)
            elif status == 401:
                logger.warning("Heartbeat: device unverified")
                return (True, status, message)
            elif status == 403:
                logger.error("Heartbeat: device blocked")
                return (True, status, message)
            else:
                logger.warning(f"Heartbeat: unexpected status {status}")
                return (True, status, message)
        else:
            logger.error(f"Heartbeat failed: {message}")
            return (False, status, message)

    def check_videos(self) -> bool:
        """
        Send current video IDs to server, download new ones, update playlist.
        Returns True if playlist changed.
        """
        with self.playlist_lock:
            current_ids = [item.id for item in self.playlist]

        data = {
            "token": self.config["token"],
            "id": self.config["device_id"],
            "videos": current_ids
        }
        response = self._request("/api/check-videos", data)
        if response is None:
            logger.warning("No response from check-videos")
            return False

        answer = response.get("answer")
        status = response.get("status")
        message = response.get("message", "")
        videos_data = response.get("videos", [])

        if not answer:
            logger.error(f"Check-videos failed: {message}")
            return False

        if status == 204:
            logger.info("No content changes.")
            return False
        elif status == 205:
            logger.info(f"Server returned {len(videos_data)} videos.")
            new_playlist = []
            for v in videos_data:
                # Ensure duration_config is present (may be None)
                dur_cfg = v.get("duration_config")
                item = VideoItem(id=v["id"], url=v["url"], duration_config=dur_cfg)
                new_playlist.append(item)

            # Download missing files
            for item in new_playlist:
                self._ensure_file(item)

            # Update playlist
            with self.playlist_lock:
                self.playlist = new_playlist
            self.playlist_updated.set()
            logger.info("Playlist updated.")
            return True
        else:
            logger.warning(f"Unexpected check-videos status {status}: {message}")
            return False

    def _ensure_file(self, item: VideoItem) -> bool:
        """
        Download the file if not already present in media_dir.
        Returns True if file exists after this call.
        """
        filename = os.path.basename(item.url)
        local_path = os.path.join(self.media_dir, filename)

        if os.path.exists(local_path):
            return True

        # Download
        logger.info(f"Downloading {item.id} -> {filename}")
        try:
            resp = requests.get(item.url, stream=True, timeout=30)
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            logger.info(f"Downloaded {filename}")
            return True
        except requests.RequestException as e:
            logger.error(f"Failed to download {item.url}: {e}")
            return False

    # ---------- Media playback ----------

    def _play_video(self, path: str, duration: float):
        """Play a video for the given duration using VLC."""
        media = self.vlc_instance.media_new(path)
        self.player.set_media(media)
        self.player.play()
        time.sleep(0.5)  # allow playback to start
        # Wait for either duration or end-of-media
        start = time.time()
        while self.player.is_playing() and (time.time() - start) < duration:
            time.sleep(0.1)
        self.player.stop()

    def _play_image(self, path: str, duration: float):
        """Display an image for a given duration using VLC."""
        media = self.vlc_instance.media_new(path)
        self.player.set_media(media)
        self.player.play()
        time.sleep(duration)
        self.player.stop()

    def _play_pdf(self, path: str, page_durations: List[float]):
        """
        Convert PDF pages to images and display each for its duration.
        Temporary images are deleted after display.
        """
        # Convert to list of PIL images
        try:
            images = convert_from_path(path)
        except Exception as e:
            logger.error(f"Failed to convert PDF {path}: {e}")
            return

        # Ensure we have enough durations (pad or truncate)
        durations = page_durations[:len(images)]
        if len(durations) < len(images):
            # Use default 5 seconds for remaining pages
            durations += [5.0] * (len(images) - len(durations))

        temp_dir = os.path.join(self.media_dir, "pdf_temp")
        os.makedirs(temp_dir, exist_ok=True)

        for i, (img, dur) in enumerate(zip(images, durations)):
            # Save image to temporary file
            temp_path = os.path.join(temp_dir, f"page_{i}.png")
            img.save(temp_path, "PNG")
            # Display
            self._play_image(temp_path, dur)
            # Remove after use
            try:
                os.remove(temp_path)
            except OSError:
                pass

        # Cleanup temp dir if empty
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass

    def _play_item(self, item: VideoItem):
        """Play a single media file according to its type and duration config."""
        # Determine file type from extension (fallback to video)
        ext = os.path.splitext(item.url)[1].lower()
        file_type = "video"  # default
        if ext in [".jpg", ".jpeg", ".png"]:
            file_type = "image"
        elif ext == ".pdf":
            file_type = "pdf"

        local_path = os.path.join(self.media_dir, os.path.basename(item.url))
        if not os.path.exists(local_path):
            logger.error(f"File {local_path} missing, skipping.")
            return

        logger.info(f"Playing {item.id} ({file_type})")

        dur_cfg = item.duration_config or {}
        if file_type == "video":
            # For video, duration_config may contain {"duration": seconds}
            duration = dur_cfg.get("duration", 30.0)
            self._play_video(local_path, duration)
        elif file_type == "image":
            duration = dur_cfg.get("duration", self.config["image_display_duration"])
            self._play_image(local_path, duration)
        elif file_type == "pdf":
            # duration_config may contain {"pages": [10, 15, ...]}
            pages = dur_cfg.get("pages", [])
            if not pages:
                # default 5 seconds per page, assume 1 page
                pages = [5.0]
            self._play_pdf(local_path, pages)
        else:
            logger.warning(f"Unsupported file type {file_type}")

    def player_loop(self):
        """Main loop for the player. Reloads playlist when updated."""
        logger.info("Player thread started.")
        while self.running:
            # Wait for playlist update or periodic check
            self.playlist_updated.wait(timeout=1.0)
            self.playlist_updated.clear()

            with self.playlist_lock:
                playlist_copy = self.playlist.copy()

            if not playlist_copy:
                logger.info("Playlist empty, waiting for content...")
                time.sleep(5)
                continue

            # Play through the playlist once
            for item in playlist_copy:
                if not self.running:
                    break
                self._play_item(item)
                # Check for playlist update while playing
                if self.playlist_updated.is_set():
                    logger.info("Playlist updated, restarting player loop.")
                    break

    # ---------- Background worker ----------

    def _worker(self):
        """Periodically send heartbeat and check for video updates."""
        last_heartbeat = 0
        last_check = 0

        while self.running:
            now = time.time()
            # Heartbeat
            if now - last_heartbeat >= self.config["heartbeat_interval"]:
                ok, status, msg = self.heartbeat()
                last_heartbeat = now
                if not ok:
                    logger.error(f"Heartbeat error: {msg}")
                elif status == 403:
                    logger.critical("Device blocked, shutting down.")
                    self.running = False
                    break
                # status 401 (unverified) is ok, continue

            # Check videos
            if now - last_check >= self.config["check_videos_interval"]:
                changed = self.check_videos()
                last_check = now
                # if changed, the player will reload automatically

            time.sleep(1)

    # ---------- Main ----------

    def run(self):
        """Start client threads and wait for termination."""
        # Perform initial token sync
        if not self.sync_token():
            logger.error("Initial token sync failed, aborting.")
            return

        # Start background worker
        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()

        # Start player (blocking)
        self.player_loop()

        logger.info("Client stopped.")

def main():
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
    else:
        config_path = "config.json"
    client = Client(config_path)
    try:
        client.run()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        client.running = False
        # Wait for threads to finish (player already stopped)
        if client.worker_thread:
            client.worker_thread.join(timeout=5)

if __name__ == "__main__":
    main()