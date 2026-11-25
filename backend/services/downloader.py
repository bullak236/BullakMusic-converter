"""
Gestor de Descarga Concurrente y Cola Dinámica Continua (enqueue / append),
Conversión FFmpeg CBR 44.1 kHz (con normalización EBU R128 loudnorm + aresample=44100),
Streaming de Preview de 30s y Eventos SSE en tiempo real.
"""

from __future__ import annotations

import asyncio
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yt_dlp

from backend.services.tagger import (
    apply_car_safe_id3v23_tags,
    build_car_safe_filepath,
    fetch_and_prepare_cover,
)

# Caché en memoria de URLs de stream para el preview de 30s (TTL = 20 minutos)
_PREVIEW_CACHE: dict[str, tuple[str, str, float]] = {}
PREVIEW_TTL_SECONDS = 1200.0
LEADING_NUM_RE = re.compile(r"^(\d+)\s*[-_.]+\s*")


def find_ffmpeg_binary() -> str | None:
    """
    Busca el ejecutable de ffmpeg en el PATH del sistema y en las ubicaciones
    habituales de instalación en Windows (WinGet Links, WinGet Packages, Chocolatey, Scoop, C:\\ffmpeg).
    """
    path_bin = shutil.which("ffmpeg")
    if path_bin:
        return path_bin

    if platform.system() == "Windows":
        local_app = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        program_data = os.environ.get("ProgramData", "C:\\ProgramData")
        candidates = [
            Path(local_app) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe",
            Path(program_data) / "chocolatey" / "bin" / "ffmpeg.exe",
            Path.home() / "scoop" / "shims" / "ffmpeg.exe",
            Path("C:/ffmpeg/bin/ffmpeg.exe"),
        ]
        for c in candidates:
            if c.exists():
                return str(c)

        winget_pkgs = Path(local_app) / "Microsoft" / "WinGet" / "Packages"
        if winget_pkgs.exists():
            try:
                for exe in winget_pkgs.rglob("ffmpeg.exe"):
                    return str(exe)
            except Exception:
                pass

    return None


def get_system_status() -> dict[str, Any]:
    """
    Reporta el estado de FFmpeg, versión de yt-dlp y versión de Python para el Panel de Estado del Sistema.
    """
    ffmpeg_path = find_ffmpeg_binary()
    ffmpeg_version = None
    if ffmpeg_path:
        try:
            proc = subprocess.run(
                [ffmpeg_path, "-version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0 and proc.stdout:
                first_line = proc.stdout.splitlines()[0]
                ffmpeg_version = first_line.strip()
        except Exception:
            ffmpeg_version = "Instalado"

    return {
        "ffmpeg_installed": bool(ffmpeg_path),
        "ffmpeg_path": ffmpeg_path,
        "ffmpeg_version": ffmpeg_version,
        "ffmpeg_install_hint": (
            "winget install Gyan.FFmpeg"
            if platform.system() == "Windows"
            else "sudo apt install ffmpeg  # o brew install ffmpeg"
        ),
        "ytdlp_version": getattr(yt_dlp.version, "__version__", "Desconocida"),
        "python_version": platform.python_version(),
        "os_platform": platform.system(),
    }


async def update_ytdlp_engine() -> dict[str, Any]:
    """
    Actualiza yt-dlp ejecutando `sys.executable -m pip install -U yt-dlp[default]`
    y soporta fallback automático a `uv pip install -U yt-dlp[default]` si el entorno
    fue creado con uv sin pip.
    """
    commands_to_try = [
        [sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"],
    ]

    uv_bin = shutil.which("uv") or str(
        Path.home() / ".local" / "bin" / ("uv.exe" if platform.system() == "Windows" else "uv")
    )
    if os.path.exists(uv_bin) or shutil.which("uv"):
        commands_to_try.append([uv_bin, "pip", "install", "--python", sys.executable, "-U", "yt-dlp[default]"])

    last_output = ""
    success = False

    for cmd in commands_to_try:
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            last_output = (proc.stdout or "") + "\n" + (proc.stderr or "")
            if proc.returncode == 0:
                success = True
                break
        except Exception as exc:
            last_output = str(exc)

    new_version = getattr(yt_dlp.version, "__version__", "Desconocida")
    try:
        ver_proc = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", "import yt_dlp; print(yt_dlp.version.__version__)"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if ver_proc.returncode == 0 and ver_proc.stdout.strip():
            new_version = ver_proc.stdout.strip()
    except Exception:
        pass

    return {
        "success": success,
        "version": new_version,
        "output": last_output.strip()[-800:],
    }


async def resolve_preview_stream_url(video_id: str) -> tuple[str, str]:
    """
    Obtiene la URL directa de audio con yt-dlp priorizando formatos reproducibles
    nativamente en el navegador: `bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio`.
    Devuelve (stream_url, content_type).
    """
    now = time.time()
    cached = _PREVIEW_CACHE.get(video_id)
    if cached and (now - cached[2]) < PREVIEW_TTL_SECONDS:
        return (cached[0], cached[1])

    def _extract() -> tuple[str, str]:
        ydl_opts = {
            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "extract_flat": False,
        }
        url = f"https://music.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            stream_url = info.get("url")
            ext = (info.get("ext") or "m4a").lower()
            if not stream_url and info.get("requested_formats"):
                stream_url = info["requested_formats"][0].get("url")
                ext = (info["requested_formats"][0].get("ext") or "m4a").lower()
            if not stream_url:
                raise RuntimeError("No se pudo extraer URL de previsualización")

            mime_map = {
                "m4a": "audio/mp4",
                "mp4": "audio/mp4",
                "webm": "audio/webm",
                "opus": "audio/webm",
                "mp3": "audio/mpeg",
            }
            content_type = mime_map.get(ext, "audio/mp4")
            return (stream_url, content_type)

    stream_url, content_type = await asyncio.to_thread(_extract)
    _PREVIEW_CACHE[video_id] = (stream_url, content_type, now)
    return (stream_url, content_type)


def _scan_used_shuffle_prefixes(output_dir: str) -> set[int]:
    """
    Escanea los archivos .mp3 ya existentes en la carpeta destino para detectar
    qué prefijos numéricos (001, 002...) están ocupados y evitar colisiones en Shuffle Físico.
    """
    used: set[int] = set()
    if not output_dir:
        return used
    root = Path(output_dir)
    if not root.exists() or not root.is_dir():
        return used
    try:
        for mp3_file in root.glob("*.mp3"):
            m = LEADING_NUM_RE.match(mp3_file.stem)
            if m:
                try:
                    used.add(int(m.group(1)))
                except ValueError:
                    pass
    except Exception:
        pass
    return used


class DownloadManager:
    """
    Motor de descargas concurrentes con Cola Dinámica Continua (enqueue / append),
    transmisión SSE en tiempo real y perfil Car-Safe.
    Permite agregar nuevos lotes de distintas bandas en cualquier momento sin
    interrumpir las descargas que ya están en progreso.
    """

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.items: dict[str, dict[str, Any]] = {}
        self.options: dict[str, Any] = {
            "output_dir": "",
            "bitrate": 192,
            "normalize": False,
            "ascii_mode": True,
            "organization": "flat",
            "concurrency": 3,
        }
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(3)
        self._active_tasks: dict[str, asyncio.Task[None]] = {}
        self._used_shuffle_numbers: set[int] = set()
        self._cancelled: bool = False

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers.discard(q)

    async def broadcast(self, event_type: str, payload: dict[str, Any]) -> None:
        msg = {"event": event_type, "data": payload}
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except Exception:
                pass

    def get_snapshot(self) -> dict[str, Any]:
        items_list = list(self.items.values())
        completed = sum(1 for i in items_list if i["status"] == "completed")
        errors = sum(1 for i in items_list if i["status"] == "error")
        queued = sum(1 for i in items_list if i["status"] == "queued")
        in_progress = sum(1 for i in items_list if i["status"] in ("downloading", "processing"))
        active = queued + in_progress
        return {
            "items": items_list,
            "options": self.options,
            "summary": {
                "total": len(items_list),
                "completed": completed,
                "errors": errors,
                "queued": queued,
                "in_progress": in_progress,
                "active": active,
                "is_running": active > 0,
            },
        }

    def _allocate_shuffle_indices(self, count: int, output_dir: str) -> list[int]:
        """
        Genera `count` prefijos numéricos aleatorios sin colisionar ni con archivos
        ya existentes en `output_dir` ni con tareas previas/activas en la sesión.
        """
        if count <= 0:
            return []

        disk_used = _scan_used_shuffle_prefixes(output_dir)
        session_used = {
            int(item["shuffle_index"])
            for item in self.items.values()
            if item.get("shuffle_index") is not None
        }
        all_used = disk_used | session_used | self._used_shuffle_numbers

        # Definir un rango amplio y natural (1..max_pool) para elegir números libres y mezclarlos
        max_existing = max(all_used, default=0)
        pool_upper = max(max_existing + count + 30, len(all_used) + count + 30, 99)
        available = [n for n in range(1, pool_upper + 1) if n not in all_used]
        random.shuffle(available)
        chosen = available[:count]
        self._used_shuffle_numbers.update(chosen)
        return chosen

    async def start_batch(
        self,
        tracks: list[dict[str, Any]],
        options: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Encola dinámicamente (append) un lote de pistas en la cola activa sin frenar
        ni reiniciar las descargas en curso.
        Ignora automáticamente cualquier videoId que ya esté en cola, descargándose
        o ya completado en la sesión.
        """
        self._cancelled = False
        concurrency = max(1, min(int(options.get("concurrency", 3)), 5))
        new_options = {
            "output_dir": options.get("output_dir", "") or self.options.get("output_dir", ""),
            "bitrate": 320 if int(options.get("bitrate", 192)) == 320 else 192,
            "normalize": bool(options.get("normalize", False)),
            "ascii_mode": bool(options.get("ascii_mode", True)),
            "organization": options.get("organization", "flat"),
            "concurrency": concurrency,
        }

        # Solo recrear el semáforo si no hay descargas corriendo activamente,
        # para no romper los locks de las tareas en vuelo
        any_running = any(not t.done() for t in self._active_tasks.values())
        if not any_running or concurrency != self.options.get("concurrency"):
            if not any_running:
                self._semaphore = asyncio.Semaphore(concurrency)
        self.options = new_options

        # Filtrar pistas realmente nuevas (ignorar las que ya están en cola, procesándose o completadas)
        tracks_to_enqueue: list[dict[str, Any]] = []
        seen_in_batch: set[str] = set()
        skipped_count = 0

        for track in tracks:
            vid = track.get("videoId")
            if not vid or vid in seen_in_batch:
                continue
            seen_in_batch.add(vid)

            existing_item = self.items.get(vid)
            existing_task = self._active_tasks.get(vid)
            if existing_item and existing_item["status"] in ("queued", "downloading", "processing", "completed"):
                if existing_item["status"] == "completed" or (existing_task and not existing_task.done()):
                    skipped_count += 1
                    continue

            tracks_to_enqueue.append(track)

        new_count = len(tracks_to_enqueue)
        if new_count == 0:
            snap = self.get_snapshot()
            snap["enqueued_count"] = 0
            snap["skipped_count"] = skipped_count
            return snap

        if new_options["organization"] == "physical_shuffle":
            shuffle_indices = self._allocate_shuffle_indices(new_count, new_options["output_dir"])
        else:
            base_idx = len(self.items)
            shuffle_indices = [base_idx + i + 1 for i in range(new_count)]

        total_expected = max(99, len(self.items) + new_count)
        newly_queued_vids: list[str] = []

        for idx, track in enumerate(tracks_to_enqueue):
            vid = track["videoId"]
            shuffle_num = shuffle_indices[idx]

            item_state = {
                "videoId": vid,
                "title": track.get("title", "Canción"),
                "artist": track.get("artist", "Artista"),
                "album": track.get("album", "BullakMusic"),
                "duration": track.get("duration", "3:30"),
                "duration_seconds": track.get("duration_seconds", 210),
                "thumbnail": track.get("thumbnail", ""),
                "shuffle_index": shuffle_num,
                "total_batch": total_expected,
                "item_options": dict(new_options),
                "status": "queued",
                "status_label": "En cola",
                "progress": 0,
                "error_message": None,
                "output_file": None,
            }
            self.items[vid] = item_state
            newly_queued_vids.append(vid)

        await self.broadcast("snapshot", self.get_snapshot())

        # Lanzar tareas únicamente para las canciones recién agregadas sin tocar las que están bajando
        for vid in newly_queued_vids:
            t = asyncio.create_task(self._process_single_track(vid))
            self._active_tasks[vid] = t

        snap = self.get_snapshot()
        snap["enqueued_count"] = len(newly_queued_vids)
        snap["skipped_count"] = skipped_count
        return snap

    async def retry_track(self, video_id: str) -> dict[str, Any]:
        """Reintenta individualmente una pista que haya dado error."""
        item = self.items.get(video_id)
        if not item:
            return self.get_snapshot()

        existing_task = self._active_tasks.get(video_id)
        if existing_task and not existing_task.done():
            return self.get_snapshot()

        self._cancelled = False
        item["status"] = "queued"
        item["status_label"] = "En cola (Reintento)"
        item["progress"] = 0
        item["error_message"] = None

        await self._emit_item_update(item)
        t = asyncio.create_task(self._process_single_track(video_id))
        self._active_tasks[video_id] = t
        return self.get_snapshot()

    async def cancel_all(self) -> dict[str, Any]:
        """Cancela las pistas en cola y en curso."""
        self._cancelled = True
        for vid, task in list(self._active_tasks.items()):
            if not task.done():
                task.cancel()
        for item in self.items.values():
            if item["status"] in ("queued", "downloading", "processing"):
                item["status"] = "error"
                item["status_label"] = "Cancelado"
                item["error_message"] = "Descarga cancelada por el usuario."
        await self.broadcast("snapshot", self.get_snapshot())
        return self.get_snapshot()

    async def clear_completed(self) -> dict[str, Any]:
        """
        Limpia de la lista de progreso las pistas ya completadas (o con error),
        sin interrumpir en absoluto las descargas que siguen corriendo en segundo plano.
        """
        for vid in list(self.items.keys()):
            if self.items[vid]["status"] in ("completed", "error"):
                del self.items[vid]
        await self.broadcast("snapshot", self.get_snapshot())
        return self.get_snapshot()

    async def _emit_item_update(self, item: dict[str, Any]) -> None:
        await self.broadcast(
            "track_update",
            {
                "item": item,
                "summary": self.get_snapshot()["summary"],
            },
        )

    async def _process_single_track(self, video_id: str) -> None:
        item = self.items.get(video_id)
        if not item:
            return

        # Usar las opciones con las que se encoló este ítem (permite encolar lotes sucesivos con seguridad)
        opts = item.get("item_options") or self.options

        async with self._semaphore:
            if self._cancelled:
                return

            ffmpeg_bin = find_ffmpeg_binary()
            if not ffmpeg_bin:
                item["status"] = "error"
                item["status_label"] = "Falta FFmpeg"
                item["error_message"] = (
                    "FFmpeg no está instalado o no se encuentra en el PATH. "
                    "Abrí 'Estado del Sistema' arriba a la derecha para ver el comando de instalación."
                )
                await self._emit_item_update(item)
                return

            try:
                item["status"] = "downloading"
                item["status_label"] = "Descargando audio..."
                item["progress"] = 5
                await self._emit_item_update(item)

                loop = asyncio.get_running_loop()

                with tempfile.TemporaryDirectory(prefix="bullakmusic_") as tmp_dir:
                    tmp_path = Path(tmp_dir)
                    raw_template = str(tmp_path / f"{video_id}.%(ext)s")

                    last_emit_time = 0.0

                    def _progress_hook(d: dict[str, Any]) -> None:
                        nonlocal last_emit_time
                        if d.get("status") == "downloading":
                            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                            downloaded = d.get("downloaded_bytes", 0)
                            if total > 0:
                                pct = int((downloaded / total) * 55) + 5  # 5% -> 60%
                                item["progress"] = min(60, max(item["progress"], pct))
                            now = time.time()
                            if (now - last_emit_time) > 0.25:
                                last_emit_time = now
                                loop.call_soon_threadsafe(
                                    lambda: asyncio.create_task(self._emit_item_update(item))
                                )

                    def _run_ytdlp() -> Path:
                        ydl_opts = {
                            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
                            "outtmpl": raw_template,
                            "quiet": True,
                            "no_warnings": True,
                            "noplaylist": True,
                            "progress_hooks": [_progress_hook],
                        }
                        url = f"https://music.youtube.com/watch?v={video_id}"
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([url])

                        files = list(tmp_path.glob(f"{video_id}.*"))
                        if not files:
                            raise RuntimeError("No se generó el archivo temporal de audio.")
                        return files[0]

                    downloaded_file = await asyncio.to_thread(_run_ytdlp)

                    # Paso 2: Conversión FFmpeg a MP3 CBR 44100 Hz (+ loudnorm si está activado)
                    item["status"] = "processing"
                    if opts["normalize"]:
                        item["status_label"] = "Normalizando (-14 LUFS) a 44.1 kHz..."
                    else:
                        item["status_label"] = f"Convirtiendo MP3 CBR {opts['bitrate']}k (44.1 kHz)..."
                    item["progress"] = 68
                    await self._emit_item_update(item)

                    converted_mp3 = tmp_path / f"{video_id}_car.mp3"
                    bitrate_str = f"{opts['bitrate']}k"

                    ffmpeg_cmd = [
                        ffmpeg_bin,
                        "-y",
                        "-i",
                        str(downloaded_file),
                        "-vn",
                    ]
                    if opts["normalize"]:
                        ffmpeg_cmd.extend(
                            [
                                "-af",
                                "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=44100",
                            ]
                        )
                    ffmpeg_cmd.extend(
                        [
                            "-ar",
                            "44100",
                            "-ac",
                            "2",
                            "-codec:a",
                            "libmp3lame",
                            "-b:a",
                            bitrate_str,
                            "-id3v2_version",
                            "3",
                            str(converted_mp3),
                        ]
                    )

                    proc = await asyncio.to_thread(
                        subprocess.run,
                        ffmpeg_cmd,
                        capture_output=True,
                        text=True,
                        timeout=300,
                    )
                    if proc.returncode != 0 or not converted_mp3.exists():
                        err_tail = (proc.stderr or "Error en FFmpeg")[-300:]
                        raise RuntimeError(f"Fallo al convertir audio con FFmpeg: {err_tail}")

                    # Paso 3: Descargar y recortar carátula 500x500 JPEG Baseline (<100 KB) y escribir tags ID3v2.3
                    item["status_label"] = "Etiquetando ID3v2.3 + Portada 500x500..."
                    item["progress"] = 88
                    await self._emit_item_update(item)

                    cover_bytes = await fetch_and_prepare_cover(
                        item.get("thumbnail", ""),
                        video_id=video_id,
                    )

                    final_path = build_car_safe_filepath(
                        output_dir=opts["output_dir"],
                        artist=item["artist"],
                        title=item["title"],
                        ascii_mode=opts["ascii_mode"],
                        organization=opts["organization"],
                        shuffle_index=item.get("shuffle_index"),
                        total_batch=item.get("total_batch", 99),
                    )

                    await asyncio.to_thread(
                        apply_car_safe_id3v23_tags,
                        converted_mp3,
                        item["title"],
                        item["artist"],
                        item["album"],
                        cover_bytes,
                        opts["ascii_mode"],
                        item.get("shuffle_index"),
                        video_id,
                    )

                    await asyncio.to_thread(shutil.move, str(converted_mp3), str(final_path))

                    item["status"] = "completed"
                    item["status_label"] = "Listo en destino"
                    item["progress"] = 100
                    item["output_file"] = str(final_path)
                    await self._emit_item_update(item)

            except asyncio.CancelledError:
                item["status"] = "error"
                item["status_label"] = "Cancelado"
                item["error_message"] = "Tarea cancelada."
                await self._emit_item_update(item)
            except Exception as exc:
                item["status"] = "error"
                item["status_label"] = "Error"
                item["error_message"] = str(exc)
                await self._emit_item_update(item)


download_manager = DownloadManager()
