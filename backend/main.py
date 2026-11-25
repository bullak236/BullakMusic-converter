"""
BullakMusic-converter (CONVERTER // CAR AUDIO STUDIO) — FastAPI Server by bullak.
Sirve tanto la API REST / SSE / Audio Stream Proxy como el frontend estático.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from backend.services.downloader import (
    download_manager,
    get_system_status,
    resolve_preview_stream_url,
    update_ytdlp_engine,
)
from backend.services.drives import (
    get_default_output_dir,
    get_folder_disk_info,
    list_drives,
    open_folder_in_explorer,
    pick_folder_Dialog,
    scan_existing_mp3s,
)
from backend.services.music import (
    generate_smart_mix,
    get_album_tracks,
    get_artist_details,
    get_similar_songs,
    search_artists,
    search_songs,
)

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    banner = (
        "\n"
        "  +----------------------------------------------------------------------+\n"
        "  |  BULLAKMUSIC-CONVERTER v1.0 // CAR AUDIO STUDIO                      |\n"
        "  |  by bullak  *  MP3 CBR 44.1 kHz  *  ID3v2.3 (TSSE)                   |\n"
        "  +----------------------------------------------------------------------+\n"
        "  |  * Web UI Local : http://localhost:8000                              |\n"
        "  |  * GitHub Repo  : https://github.com/bullak236/BullakMusic-converter |\n"
        "  |  * Autor        : https://github.com/bullak236                       |\n"
        "  +----------------------------------------------------------------------+\n"
    )
    print(banner, flush=True)
    yield


app = FastAPI(
    title="BullakMusic-converter",
    description=(
        "BullakMusic-converter by bullak — Buscador, generador de mix y descargador "
        "MP3 CBR 44.1 kHz 100% compatible con estéreos de auto (https://github.com/bullak236/BullakMusic-converter)."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Modelos Pydantic
# ---------------------------------------------------------------------------
class MixRequest(BaseModel):
    seeds: list[str] = Field(..., min_length=1, max_length=5)
    total_count: int = Field(default=30, ge=5, le=100)
    max_duration_mins: float = Field(default=8.5, ge=0.0, le=60.0)
    exclude_karaoke: bool = Field(default=True)
    exclude_live: bool = Field(default=False)
    exclude_remix: bool = Field(default=False)
    exclude_cover: bool = Field(default=False)
    exclude_live_remix: bool = Field(default=False)
    target_dir: str = Field(default="")


class FolderPathRequest(BaseModel):
    path: str = Field(default="")


class TrackItemModel(BaseModel):
    videoId: str
    title: str
    artist: str
    album: str = "Single"
    duration: str = "3:30"
    duration_seconds: int = 210
    thumbnail: str = ""


class DownloadBatchRequest(BaseModel):
    tracks: list[TrackItemModel]
    output_dir: str
    bitrate: int = 192
    normalize: bool = False
    ascii_mode: bool = True
    organization: str = "flat"  # flat | artist_folders | physical_shuffle
    concurrency: int = Field(default=3, ge=1, le=5)


# ---------------------------------------------------------------------------
# Endpoints de Estado del Sistema y Mantenimiento
# ---------------------------------------------------------------------------
@app.get("/api/system/status")
async def api_system_status() -> dict[str, Any]:
    return await asyncio.to_thread(get_system_status)


@app.post("/api/system/update-ytdlp")
async def api_update_ytdlp() -> dict[str, Any]:
    return await update_ytdlp_engine()


# ---------------------------------------------------------------------------
# Endpoints de Unidades USB, Carpetas y Escáner Anti-Duplicados
# ---------------------------------------------------------------------------
@app.get("/api/drives")
async def api_list_drives(target_dir: str = Query(default="")) -> dict[str, Any]:
    drives = await asyncio.to_thread(list_drives)
    selected_dir = (
        target_dir.strip()
        if target_dir and target_dir.strip()
        else await asyncio.to_thread(get_default_output_dir)
    )
    folder_info = await asyncio.to_thread(get_folder_disk_info, selected_dir)
    scan_data = await asyncio.to_thread(scan_existing_mp3s, selected_dir)

    return {
        "drives": drives,
        "selected_dir": selected_dir,
        "folder_info": folder_info,
        "existing_mp3_count": scan_data["count"],
        "existing_total_mb": scan_data["total_size_mb"],
        "existing_files": scan_data["files"][:50],
    }


@app.post("/api/drives/pick-folder")
async def api_pick_folder(req: FolderPathRequest) -> dict[str, Any]:
    chosen = await asyncio.to_thread(pick_folder_Dialog, req.path)
    if not chosen:
        return {"selected": False, "path": req.path}

    folder_info = await asyncio.to_thread(get_folder_disk_info, chosen)
    scan_data = await asyncio.to_thread(scan_existing_mp3s, chosen)
    return {
        "selected": True,
        "path": chosen,
        "folder_info": folder_info,
        "existing_mp3_count": scan_data["count"],
        "existing_total_mb": scan_data["total_size_mb"],
    }


@app.post("/api/drives/open-folder")
async def api_open_folder(req: FolderPathRequest) -> dict[str, Any]:
    if not req.path:
        raise HTTPException(status_code=400, detail="Ruta vacía.")
    ok = await asyncio.to_thread(open_folder_in_explorer, req.path)
    return {"opened": ok, "path": req.path}


# ---------------------------------------------------------------------------
# Endpoints de Búsqueda Musical, Artistas, Similares y Generador de Mix
# ---------------------------------------------------------------------------
@app.get("/api/search")
async def api_search(
    q: str = Query(..., min_length=1),
    mode: str = Query(default="songs"),
    max_duration_mins: float = Query(default=8.5),
    exclude_karaoke: bool = Query(default=True),
    exclude_live: bool = Query(default=False),
    exclude_remix: bool = Query(default=False),
    exclude_cover: bool = Query(default=False),
    exclude_live_remix: bool = Query(default=False),
    target_dir: str = Query(default=""),
) -> dict[str, Any]:
    scan_data = await asyncio.to_thread(scan_existing_mp3s, target_dir) if target_dir else None

    if mode == "artists":
        artists = await asyncio.to_thread(search_artists, q, 18)
        return {"mode": "artists", "artists": artists, "tracks": []}

    tracks = await asyncio.to_thread(
        search_songs,
        q,
        36,
        max_duration_mins,
        exclude_karaoke,
        exclude_live,
        exclude_remix,
        exclude_cover,
        exclude_live_remix,
        scan_data,
    )
    return {"mode": "songs", "tracks": tracks, "artists": []}


@app.get("/api/artist/{browse_id}")
async def api_artist_details(
    browse_id: str,
    max_duration_mins: float = Query(default=8.5),
    exclude_karaoke: bool = Query(default=True),
    exclude_live: bool = Query(default=False),
    exclude_remix: bool = Query(default=False),
    exclude_cover: bool = Query(default=False),
    exclude_live_remix: bool = Query(default=False),
    target_dir: str = Query(default=""),
) -> dict[str, Any]:
    scan_data = await asyncio.to_thread(scan_existing_mp3s, target_dir) if target_dir else None
    try:
        details = await asyncio.to_thread(
            get_artist_details,
            browse_id,
            max_duration_mins,
            exclude_karaoke,
            exclude_live,
            exclude_remix,
            exclude_cover,
            exclude_live_remix,
            scan_data,
        )
        return details
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"No se pudo cargar el artista: {exc}") from exc


@app.get("/api/album/{browse_id}")
async def api_album_details(
    browse_id: str,
    max_duration_mins: float = Query(default=8.5),
    exclude_karaoke: bool = Query(default=True),
    exclude_live: bool = Query(default=False),
    exclude_remix: bool = Query(default=False),
    exclude_cover: bool = Query(default=False),
    exclude_live_remix: bool = Query(default=False),
    target_dir: str = Query(default=""),
) -> dict[str, Any]:
    scan_data = await asyncio.to_thread(scan_existing_mp3s, target_dir) if target_dir else None
    try:
        album = await asyncio.to_thread(
            get_album_tracks,
            browse_id,
            max_duration_mins,
            exclude_karaoke,
            exclude_live,
            exclude_remix,
            exclude_cover,
            exclude_live_remix,
            scan_data,
        )
        return album
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"No se pudo cargar el álbum: {exc}") from exc


@app.get("/api/similar/{video_id}")
async def api_similar_songs(
    video_id: str,
    max_duration_mins: float = Query(default=8.5),
    exclude_karaoke: bool = Query(default=True),
    exclude_live: bool = Query(default=False),
    exclude_remix: bool = Query(default=False),
    exclude_cover: bool = Query(default=False),
    exclude_live_remix: bool = Query(default=False),
    target_dir: str = Query(default=""),
) -> dict[str, Any]:
    scan_data = await asyncio.to_thread(scan_existing_mp3s, target_dir) if target_dir else None
    try:
        tracks = await asyncio.to_thread(
            get_similar_songs,
            video_id,
            28,
            max_duration_mins,
            exclude_karaoke,
            exclude_live,
            exclude_remix,
            exclude_cover,
            exclude_live_remix,
            scan_data,
        )
        return {"videoId": video_id, "tracks": tracks}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"No se pudieron obtener temas similares: {exc}") from exc


@app.post("/api/mix/generate")
async def api_generate_mix(req: MixRequest) -> dict[str, Any]:
    scan_data = await asyncio.to_thread(scan_existing_mp3s, req.target_dir) if req.target_dir else None
    try:
        mix_data = await asyncio.to_thread(
            generate_smart_mix,
            req.seeds,
            req.total_count,
            req.max_duration_mins,
            req.exclude_karaoke,
            req.exclude_live,
            req.exclude_remix,
            req.exclude_cover,
            req.exclude_live_remix,
            scan_data,
        )
        return mix_data
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error al generar el Mix: {exc}") from exc


# ---------------------------------------------------------------------------
# Proxy Local de Imágenes con Caché LRU en Memoria + Anti-SSRF
# ---------------------------------------------------------------------------
ALLOWED_IMAGE_HOSTS = {
    "i.ytimg.com",
    "i9.ytimg.com",
    "img.youtube.com",
    "lh3.googleusercontent.com",
    "yt3.ggpht.com",
    "yt3.googleusercontent.com",
    "music.youtube.com",
}
ALLOWED_IMAGE_SUFFIXES = (
    ".googleusercontent.com",
    ".ytimg.com",
    ".ggpht.com",
    ".youtube.com",
)

# Caché LRU en memoria (~300 imágenes o ~30 MB máximo) para evitar 403/429 y carga instantánea
_IMAGE_CACHE: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
_IMAGE_CACHE_BYTES: int = 0
MAX_IMAGE_CACHE_ITEMS = 300
MAX_IMAGE_CACHE_BYTES = 30 * 1024 * 1024  # 30 MB


def _is_allowed_image_host(hostname: str) -> bool:
    host = (hostname or "").lower().strip(".")
    if not host:
        return False
    if host in ALLOWED_IMAGE_HOSTS:
        return True
    return any(host.endswith(suffix) for suffix in ALLOWED_IMAGE_SUFFIXES)


@app.get("/api/image-proxy")
async def api_image_proxy(url: str = Query(..., min_length=8, max_length=1024)) -> Response:
    """
    Proxy seguro de miniaturas con protección estricta Anti-SSRF y caché LRU en memoria:
    - Valida protocolo HTTPS y dominios legítimos de YouTube/Google.
    - Usa follow_redirects=True y omite Referer para evitar bloqueos ORB/403.
    - Cachea hasta 300 portadas (~30 MB) con Cache-Control: public, max-age=86400.
    """
    global _IMAGE_CACHE_BYTES
    clean_url = url.strip()
    parsed = urlparse(clean_url)
    hostname = (parsed.hostname or "").lower()

    if parsed.scheme != "https" or not _is_allowed_image_host(hostname):
        raise HTTPException(
            status_code=403,
            detail="Dominio de imagen bloqueado por política Anti-SSRF (solo dominios oficiales de YouTube/Google).",
        )

    # 1. Servir desde caché LRU si ya fue descargada
    cached = _IMAGE_CACHE.get(clean_url)
    if cached is not None:
        _IMAGE_CACHE.move_to_end(clean_url)
        img_bytes, media_type = cached
        return Response(
            content=img_bytes,
            media_type=media_type,
            headers={
                "Cache-Control": "public, max-age=86400",
                "X-Cache": "HIT",
            },
        )

    # 2. Descargar sin enviar Referer y siguiendo redirecciones
    req_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }

    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        try:
            resp = await client.get(clean_url, headers=req_headers)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"No se pudo obtener la imagen: {exc}") from exc

    if resp.status_code != 200 or not resp.content:
        raise HTTPException(status_code=resp.status_code if resp.status_code != 200 else 404, detail="Miniatura no encontrada.")

    content_type = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip().lower()
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="El recurso remoto no es una imagen válida.")

    img_data = resp.content
    data_len = len(img_data)

    # 3. Guardar en caché LRU respetando límites de 300 ítems y 30 MB
    if data_len <= 4 * 1024 * 1024:
        while (
            len(_IMAGE_CACHE) >= MAX_IMAGE_CACHE_ITEMS
            or (_IMAGE_CACHE_BYTES + data_len) > MAX_IMAGE_CACHE_BYTES
        ) and _IMAGE_CACHE:
            _, (evicted_bytes, _) = _IMAGE_CACHE.popitem(last=False)
            _IMAGE_CACHE_BYTES = max(0, _IMAGE_CACHE_BYTES - len(evicted_bytes))

        _IMAGE_CACHE[clean_url] = (img_data, content_type)
        _IMAGE_CACHE_BYTES += data_len

    return Response(
        content=img_data,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Cache": "MISS",
        },
    )


# ---------------------------------------------------------------------------
# Endpoint de Previsualización de Audio de 30s (Proxy Stream con cierre limpio)
# ---------------------------------------------------------------------------
@app.get("/api/preview/{video_id}")
async def api_preview_audio(video_id: str, request: Request) -> StreamingResponse:
    """
    Obtiene el stream de audio con yt-dlp priorizando formatos nativos
    (bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio) y lo proxea al <audio>
    del frontend evitando bloqueos de CORS y cerrando limpiamente el cliente httpx
    cuando el usuario pausa o cambia de canción.
    """
    try:
        stream_url, default_mime = await resolve_preview_stream_url(video_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"No se pudo obtener preview: {exc}") from exc

    forward_headers: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
    }
    range_header = request.headers.get("range")
    if range_header:
        forward_headers["Range"] = range_header

    client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, read=60.0), follow_redirects=True)
    try:
        upstream_req = client.build_request("GET", stream_url, headers=forward_headers)
        upstream_resp = await client.send(upstream_req, stream=True)
    except Exception as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Error al conectar con stream: {exc}") from exc

    resp_headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }
    for h_name in ("Content-Length", "Content-Range"):
        if h_name in upstream_resp.headers:
            resp_headers[h_name] = upstream_resp.headers[h_name]

    content_type = upstream_resp.headers.get("Content-Type", default_mime)

    async def _stream_generator():
        try:
            async for chunk in upstream_resp.aiter_bytes(chunk_size=32768):
                yield chunk
        except (asyncio.CancelledError, httpx.StreamClosed, httpx.ReadError):
            pass
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        _stream_generator(),
        status_code=upstream_resp.status_code,
        media_type=content_type,
        headers=resp_headers,
    )


# ---------------------------------------------------------------------------
# Endpoints de Cola de Descarga Concurrente + SSE en Tiempo Real
# ---------------------------------------------------------------------------
@app.get("/api/downloads/state")
async def api_downloads_state() -> dict[str, Any]:
    return download_manager.get_snapshot()


@app.post("/api/downloads/start")
async def api_downloads_start(req: DownloadBatchRequest) -> dict[str, Any]:
    if not req.tracks:
        raise HTTPException(status_code=400, detail="La selección de canciones está vacía.")
    if not req.output_dir or not req.output_dir.strip():
        raise HTTPException(status_code=400, detail="Seleccioná un pendrive o carpeta destino.")

    tracks_dicts = [t.model_dump() for t in req.tracks]
    options = {
        "output_dir": req.output_dir.strip(),
        "bitrate": req.bitrate,
        "normalize": req.normalize,
        "ascii_mode": req.ascii_mode,
        "organization": req.organization,
        "concurrency": req.concurrency,
    }
    return await download_manager.start_batch(tracks_dicts, options)


@app.post("/api/downloads/retry/{video_id}")
async def api_downloads_retry(video_id: str) -> dict[str, Any]:
    return await download_manager.retry_track(video_id)


@app.post("/api/downloads/cancel")
async def api_downloads_cancel() -> dict[str, Any]:
    return await download_manager.cancel_all()


@app.post("/api/downloads/clear")
async def api_downloads_clear() -> dict[str, Any]:
    return await download_manager.clear_completed()


@app.get("/api/downloads/stream")
async def api_downloads_stream(request: Request) -> EventSourceResponse:
    """
    Canal Server-Sent Events (SSE) que envía el estado inicial y todas las
    actualizaciones de progreso por pista en tiempo real.
    """
    queue = download_manager.subscribe()

    async def _event_publisher():
        try:
            yield {
                "event": "snapshot",
                "data": json.dumps(download_manager.get_snapshot()),
            }
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield {
                        "event": msg["event"],
                        "data": json.dumps(msg["data"]),
                    }
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        except asyncio.CancelledError:
            pass
        finally:
            download_manager.unsubscribe(queue)

    return EventSourceResponse(_event_publisher())


# ---------------------------------------------------------------------------
# Servir Frontend Estático (Single Command Experience)
# ---------------------------------------------------------------------------
FRONTEND_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/")
async def serve_index() -> FileResponse:
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Frontend index.html no encontrado.")
    return FileResponse(index_file)


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
