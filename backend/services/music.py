"""
Servicio de búsqueda musical, exploración de artistas, descubrimiento de temas similares
y Generador de Mix Inteligente usando ytmusicapi (sin autenticación).
Incluye búsqueda combinada en paralelo (Canciones + Videos musicales), top 60 de artistas
y filtros independientes (Karaoke/Pistas, En Vivo, Remix, Covers/Tributo y Anti-enganchados).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import re
from typing import Any

from ytmusicapi import YTMusic

from backend.services.drives import is_track_in_destination, normalize_key

# Instancia compartida de YTMusic sin autenticación
_ytmusic: YTMusic | None = None

# Patrones regex independientes para cada categoría de filtro
KARAOKE_PATTERN = re.compile(
    r"\b(karaoke|instrumental|pista\s+musical|backing\s+track|sin\s+voz)\b",
    re.IGNORECASE,
)
LIVE_PATTERN = re.compile(
    r"(\b(live|en\s+vivo|unplugged)\b|\(\s*vivo\s*\)|\[\s*vivo\s*\]|-\s*vivo\b)",
    re.IGNORECASE,
)
REMIX_PATTERN = re.compile(
    r"\b(remix|bootleg|mashup|edit|rework|vip\s+mix|dj\s+set)\b",
    re.IGNORECASE,
)
COVER_PATTERN = re.compile(
    r"\b(tributo|tribute|cover|homenaje|versi[oó]n\s+cover)\b",
    re.IGNORECASE,
)


def get_yt() -> YTMusic:
    """Inicializa perezosamente el cliente YTMusic sin autenticación."""
    global _ytmusic
    if _ytmusic is None:
        _ytmusic = YTMusic()
    return _ytmusic


def parse_duration_to_seconds(duration_str: str | None, duration_seconds: int | None = None) -> int:
    """Convierte cadenas como '3:45' o '1:02:15' a segundos enteros."""
    if isinstance(duration_seconds, (int, float)) and duration_seconds > 0:
        return int(duration_seconds)
    if not duration_str:
        return 210  # Estimación por defecto (3:30) si YouTube Music no reporta duración
    try:
        parts = [int(p) for p in str(duration_str).strip().split(":")]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 1:
            return parts[0]
    except Exception:
        pass
    return 210


def format_seconds(seconds: int) -> str:
    """Formatea segundos enteros en 'M:SS' o 'H:MM:SS'."""
    if seconds <= 0:
        return "0:00"
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def pick_best_thumbnail(thumbnails: list[dict[str, Any]] | None, video_id: str = "") -> str:
    """
    Elige la miniatura de mejor calidad entregada por ytmusicapi sin alterar
    parámetros firmados (=w...-h...) de lh3/yt3.googleusercontent.com, y usa
    https://i.ytimg.com/vi/{video_id}/hqdefault.jpg como fallback seguro.
    """
    if thumbnails and isinstance(thumbnails, list):
        for item in reversed(thumbnails):
            if isinstance(item, dict):
                url = (item.get("url") or "").strip()
                if url:
                    if url.startswith("//"):
                        return f"https:{url}"
                    return url
    if video_id:
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return ""


def classify_track_tags(title: str, album: str = "", artist: str = "") -> dict[str, bool]:
    """Clasifica una pista en flags independientes: is_karaoke, is_live, is_remix, is_cover."""
    combined = f"{title} {album}".strip()
    full_text = f"{title} {album} {artist}".strip()
    is_karaoke = bool(KARAOKE_PATTERN.search(full_text))
    is_live = bool(LIVE_PATTERN.search(combined))
    is_remix = bool(REMIX_PATTERN.search(combined))
    is_cover = bool(COVER_PATTERN.search(full_text))
    return {
        "is_karaoke": is_karaoke,
        "is_live": is_live,
        "is_remix": is_remix,
        "is_cover": is_cover,
        "is_live_or_remix": is_karaoke or is_live or is_remix or is_cover,
    }


def normalize_track_item(
    raw: dict[str, Any],
    fallback_artist: str = "",
    fallback_album: str = "",
    scan_data: dict[str, Any] | None = None,
    source_type: str | None = None,
) -> dict[str, Any] | None:
    """Normaliza un ítem de canción o video proveniente de search(), get_artist(), get_album() o get_watch_playlist()."""
    video_id = raw.get("videoId")
    if not video_id:
        return None

    title = (raw.get("title") or "").strip()
    if not title:
        return None

    # Determinar source_type ("song" | "video")
    result_type = (raw.get("resultType") or "").lower()
    video_type = (raw.get("videoType") or "").upper()
    if source_type in ("song", "video"):
        resolved_source = source_type
    elif result_type == "video" or (video_type and "ATV" not in video_type and "OMV" in video_type and not raw.get("album")):
        resolved_source = "video"
    else:
        resolved_source = "song"

    # Extraer artistas
    artists_list = raw.get("artists") or []
    artist_names = [a.get("name", "").strip() for a in artists_list if isinstance(a, dict) and a.get("name")]
    artist = ", ".join(artist_names) if artist_names else (fallback_artist or "Artista Desconocido")

    # Extraer álbum (sin descartar nunca pistas con album: None)
    album_obj = raw.get("album")
    if isinstance(album_obj, dict) and album_obj.get("name"):
        album = str(album_obj.get("name")).strip()
    elif isinstance(album_obj, str) and album_obj.strip():
        album = album_obj.strip()
    else:
        album = fallback_album or ("Video Musical" if resolved_source == "video" else "Single")

    # Duración
    raw_dur_str = raw.get("duration") or raw.get("length")
    raw_dur_sec = raw.get("duration_seconds")
    duration_seconds = parse_duration_to_seconds(raw_dur_str, raw_dur_sec)
    duration_formatted = format_seconds(duration_seconds)

    thumbnails = raw.get("thumbnails") or raw.get("thumbnail")
    if isinstance(thumbnails, dict):
        thumbnails = thumbnails.get("thumbnails", [])
    thumbnail_url = pick_best_thumbnail(thumbnails, video_id)

    flags = classify_track_tags(title, album, artist)

    track = {
        "videoId": video_id,
        "title": title,
        "artist": artist,
        "album": album,
        "duration_seconds": duration_seconds,
        "duration": duration_formatted,
        "thumbnail": thumbnail_url,
        "thumbnail_url": thumbnail_url,
        "source_type": resolved_source,
        "is_karaoke": flags["is_karaoke"],
        "is_live": flags["is_live"],
        "is_remix": flags["is_remix"],
        "is_cover": flags["is_cover"],
        "is_live_or_remix": flags["is_live_or_remix"],
        "already_in_dest": False,
    }

    if scan_data:
        track["already_in_dest"] = is_track_in_destination(track, scan_data)

    return track


def apply_track_filters(
    tracks: list[dict[str, Any]],
    max_duration_mins: float = 8.5,
    exclude_karaoke: bool = True,
    exclude_live: bool = False,
    exclude_remix: bool = False,
    exclude_cover: bool = False,
    exclude_live_remix: bool = False,
) -> list[dict[str, Any]]:
    """
    Aplica filtros automáticos configurables e independientes:
    - Anti-enganchados: oculta tracks mayores a max_duration_mins (ej. 8.5 min = 510s).
    - Sin Karaoke / Pistas (activo por defecto).
    - Sin En Vivo (configurable).
    - Sin Remix (configurable).
    - Sin Covers / Tributo (configurable).
    - Deduplicación por videoId.
    """
    if exclude_live_remix:
        exclude_karaoke = True
        exclude_live = True
        exclude_remix = True
        exclude_cover = True

    max_seconds = int(max_duration_mins * 60) if max_duration_mins and max_duration_mins > 0 else 0
    filtered: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for t in tracks:
        vid = t["videoId"]
        if vid in seen_ids:
            continue
        if max_seconds > 0 and t["duration_seconds"] > max_seconds:
            continue
        if exclude_karaoke and t.get("is_karaoke"):
            continue
        if exclude_live and t.get("is_live"):
            continue
        if exclude_remix and t.get("is_remix"):
            continue
        if exclude_cover and t.get("is_cover"):
            continue

        seen_ids.add(vid)
        filtered.append(t)

    return filtered


def _score_query_relevance(track: dict[str, Any], query_tokens: list[str]) -> int:
    """
    Calcula relevancia con los tokens de búsqueda para que colaboraciones o videos oficiales
    que coinciden exactamente con lo buscado (ej. 'Despójate Vol.2 - Hernán y La Champions Liga, BM, Callejero Fino')
    aparezcan arriba junto con las canciones oficiales.
    """
    haystack = normalize_key(f"{track.get('title', '')} {track.get('artist', '')}")
    title_norm = normalize_key(track.get("title", ""))
    score = 0
    matched_tokens = 0
    for tok in query_tokens:
        if tok in haystack:
            matched_tokens += 1
            score += 10
            if tok in title_norm:
                score += 6
    if query_tokens and matched_tokens == len(query_tokens):
        score += 25
    # Leve prioridad a 'song' oficial sobre 'video' a igualdad de coincidencia
    if track.get("source_type") == "song":
        score += 4
    return score


def search_songs(
    query: str,
    limit: int = 35,
    max_duration_mins: float = 8.5,
    exclude_karaoke: bool = True,
    exclude_live: bool = False,
    exclude_remix: bool = False,
    exclude_cover: bool = False,
    exclude_live_remix: bool = False,
    scan_data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Búsqueda combinada en paralelo:
    Consulta simultáneamente `filter="songs"` (límite ~25) y `filter="videos"` (límite ~15),
    priorizando canciones oficiales y sumando videos musicales (donde suelen indexarse
    remixes, colaboraciones, sesiones en vivo y lanzamientos como 'Despójate Vol.2')
    deduplicados por videoId.
    """
    if not query or not query.strip():
        return []

    clean_q = query.strip()
    yt = get_yt()
    song_limit = max(25, limit)
    video_limit = 18

    def _fetch_songs() -> list[dict[str, Any]]:
        try:
            return yt.search(clean_q, filter="songs", limit=song_limit) or []
        except Exception:
            return []

    def _fetch_videos() -> list[dict[str, Any]]:
        try:
            return yt.search(clean_q, filter="videos", limit=video_limit) or []
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_songs = pool.submit(_fetch_songs)
        fut_videos = pool.submit(_fetch_videos)
        raw_songs = fut_songs.result()
        raw_videos = fut_videos.result()

    song_tracks: list[dict[str, Any]] = []
    video_tracks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for item in raw_songs:
        norm = normalize_track_item(item, scan_data=scan_data, source_type="song")
        if norm and norm["videoId"] not in seen_ids:
            seen_ids.add(norm["videoId"])
            song_tracks.append(norm)

    for item in raw_videos:
        norm = normalize_track_item(item, scan_data=scan_data, source_type="video")
        if norm and norm["videoId"] not in seen_ids:
            seen_ids.add(norm["videoId"])
            video_tracks.append(norm)

    # Intercalar inteligentemente: poner en el top los videos que coincidan fuertemente con la búsqueda
    # (ej. cuando el tema buscado está indexado como video oficial/colaboración) junto con las top canciones.
    q_tokens = [t for t in normalize_key(clean_q).split() if len(t) >= 2]
    high_relevance_videos: list[dict[str, Any]] = []
    remaining_videos: list[dict[str, Any]] = []

    for vt in video_tracks:
        rel = _score_query_relevance(vt, q_tokens)
        if rel >= 18:
            high_relevance_videos.append(vt)
        else:
            remaining_videos.append(vt)

    # Combinar: top 3 canciones -> videos altamente relevantes -> resto de canciones -> resto de videos
    combined: list[dict[str, Any]] = (
        song_tracks[:3]
        + high_relevance_videos[:6]
        + song_tracks[3:]
        + high_relevance_videos[6:]
        + remaining_videos
    )

    return apply_track_filters(
        combined,
        max_duration_mins=max_duration_mins,
        exclude_karaoke=exclude_karaoke,
        exclude_live=exclude_live,
        exclude_remix=exclude_remix,
        exclude_cover=exclude_cover,
        exclude_live_remix=exclude_live_remix,
    )


def search_artists(query: str, limit: int = 16) -> list[dict[str, Any]]:
    """Busca artistas en YouTube Music."""
    if not query or not query.strip():
        return []

    yt = get_yt()
    raw_results = yt.search(query.strip(), filter="artists", limit=limit)
    artists: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for item in raw_results:
        browse_id = item.get("browseId")
        name = item.get("artist") or item.get("title") or ""
        if not browse_id or not name or browse_id in seen_ids:
            continue
        seen_ids.add(browse_id)

        subscribers = item.get("subscribers") or "Artista verificado"
        thumb = pick_best_thumbnail(item.get("thumbnails"))

        artists.append(
            {
                "browseId": browse_id,
                "name": name,
                "subscribers": subscribers,
                "thumbnail": thumb,
                "thumbnail_url": thumb,
            }
        )

    return artists


def get_artist_details(
    browse_id: str,
    max_duration_mins: float = 8.5,
    exclude_karaoke: bool = True,
    exclude_live: bool = False,
    exclude_remix: bool = False,
    exclude_cover: bool = False,
    exclude_live_remix: bool = False,
    scan_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Obtiene el perfil completo de un artista: nombre, foto, hasta 60 de sus canciones más
    populares reales (consultando `get_playlist(songs_browse_id, limit=60)` + videos populares del artista)
    y tanto sus Álbumes como sus Singles / EPs.
    """
    yt = get_yt()
    data = yt.get_artist(browse_id)

    artist_name = data.get("name") or "Artista"
    description = data.get("description") or ""
    subscribers = data.get("subscribers") or ""
    thumbnail = pick_best_thumbnail(data.get("thumbnails"))

    raw_songs: list[dict[str, Any]] = []
    songs_section = data.get("songs") or {}
    songs_browse_id = songs_section.get("browseId")

    if songs_browse_id:
        try:
            clean_pl_id = songs_browse_id[2:] if songs_browse_id.startswith("VL") else songs_browse_id
            pl = yt.get_playlist(clean_pl_id, limit=65)
            raw_songs = pl.get("tracks") or []
        except Exception:
            try:
                pl = yt.get_playlist(songs_browse_id, limit=65)
                raw_songs = pl.get("tracks") or []
            except Exception:
                raw_songs = songs_section.get("results") or []
    else:
        raw_songs = songs_section.get("results") or []

    tracks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for item in raw_songs:
        norm = normalize_track_item(item, fallback_artist=artist_name, scan_data=scan_data, source_type="song")
        if norm and norm["videoId"] not in seen_ids:
            seen_ids.add(norm["videoId"])
            tracks.append(norm)

    # Sumar videos oficiales del artista (por ej. colaboraciones o hits que estén en la sección videos)
    videos_section = data.get("videos") or {}
    for v_item in (videos_section.get("results") or [])[:15]:
        norm = normalize_track_item(v_item, fallback_artist=artist_name, scan_data=scan_data, source_type="video")
        if norm and norm["videoId"] not in seen_ids:
            seen_ids.add(norm["videoId"])
            tracks.append(norm)

    # Si aún hay menos de 35 temas, complementar con búsqueda directa de canciones y videos del artista
    if len(tracks) < 35:
        try:
            extra_songs = yt.search(artist_name, filter="songs", limit=40)
            for item in extra_songs:
                norm = normalize_track_item(item, fallback_artist=artist_name, scan_data=scan_data, source_type="song")
                if norm and norm["videoId"] not in seen_ids:
                    seen_ids.add(norm["videoId"])
                    tracks.append(norm)
        except Exception:
            pass

    filtered_tracks = apply_track_filters(
        tracks,
        max_duration_mins=max_duration_mins,
        exclude_karaoke=exclude_karaoke,
        exclude_live=exclude_live,
        exclude_remix=exclude_remix,
        exclude_cover=exclude_cover,
        exclude_live_remix=exclude_live_remix,
    )[:60]

    albums: list[dict[str, Any]] = []
    seen_albums: set[str] = set()
    for section_key, label in (("albums", "Álbum"), ("singles", "Single / EP")):
        section = data.get(section_key) or {}
        for alb in (section.get("results") or [])[:20]:
            alb_id = alb.get("browseId")
            alb_title = alb.get("title")
            if not alb_id or not alb_title or alb_id in seen_albums:
                continue
            seen_albums.add(alb_id)
            alb_thumb = pick_best_thumbnail(alb.get("thumbnails") or alb.get("thumbnail")) or thumbnail
            albums.append(
                {
                    "browseId": alb_id,
                    "title": alb_title,
                    "year": alb.get("year") or label,
                    "type": label,
                    "thumbnail": alb_thumb,
                    "thumbnail_url": alb_thumb,
                }
            )

    return {
        "browseId": browse_id,
        "name": artist_name,
        "description": description,
        "subscribers": subscribers,
        "thumbnail": thumbnail,
        "thumbnail_url": thumbnail,
        "top_songs": filtered_tracks,
        "albums": albums,
    }


def get_album_tracks(
    browse_id: str,
    max_duration_mins: float = 8.5,
    exclude_karaoke: bool = True,
    exclude_live: bool = False,
    exclude_remix: bool = False,
    exclude_cover: bool = False,
    exclude_live_remix: bool = False,
    scan_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Obtiene todas las canciones de un álbum o single/EP específico."""
    yt = get_yt()
    alb = yt.get_album(browse_id)
    album_title = alb.get("title") or "Álbum"
    artists = alb.get("artists") or []
    artist_name = ", ".join(a.get("name", "") for a in artists if a.get("name")) or "Artista"
    thumbnail = pick_best_thumbnail(alb.get("thumbnails"))

    tracks: list[dict[str, Any]] = []
    for item in alb.get("tracks") or []:
        norm = normalize_track_item(
            item,
            fallback_artist=artist_name,
            fallback_album=album_title,
            scan_data=scan_data,
            source_type="song",
        )
        if norm:
            if not norm["thumbnail"] and thumbnail:
                norm["thumbnail"] = thumbnail
                norm["thumbnail_url"] = thumbnail
            tracks.append(norm)

    filtered = apply_track_filters(
        tracks,
        max_duration_mins=max_duration_mins,
        exclude_karaoke=exclude_karaoke,
        exclude_live=exclude_live,
        exclude_remix=exclude_remix,
        exclude_cover=exclude_cover,
        exclude_live_remix=exclude_live_remix,
    )
    return {
        "browseId": browse_id,
        "title": album_title,
        "artist": artist_name,
        "year": alb.get("year") or "",
        "thumbnail": thumbnail,
        "thumbnail_url": thumbnail,
        "tracks": filtered,
    }


def get_similar_songs(
    video_id: str,
    limit: int = 28,
    max_duration_mins: float = 8.5,
    exclude_karaoke: bool = True,
    exclude_live: bool = False,
    exclude_remix: bool = False,
    exclude_cover: bool = False,
    exclude_live_remix: bool = False,
    scan_data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Obtiene temas similares / del mismo género o radio usando get_watch_playlist de ytmusicapi.
    """
    if not video_id:
        return []

    yt = get_yt()
    watch_data = yt.get_watch_playlist(videoId=video_id, radio=True, limit=limit + 12)
    raw_tracks = watch_data.get("tracks") or []

    tracks: list[dict[str, Any]] = []
    for item in raw_tracks:
        if item.get("videoId") == video_id:
            continue
        norm = normalize_track_item(item, scan_data=scan_data)
        if norm:
            tracks.append(norm)

    filtered = apply_track_filters(
        tracks,
        max_duration_mins=max_duration_mins,
        exclude_karaoke=exclude_karaoke,
        exclude_live=exclude_live,
        exclude_remix=exclude_remix,
        exclude_cover=exclude_cover,
        exclude_live_remix=exclude_live_remix,
    )
    return filtered[:limit]


def generate_smart_mix(
    seeds: list[str],
    total_count: int = 30,
    max_duration_mins: float = 8.5,
    exclude_karaoke: bool = True,
    exclude_live: bool = False,
    exclude_remix: bool = False,
    exclude_cover: bool = False,
    exclude_live_remix: bool = False,
    scan_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Generador de Mix Inteligente:
    Recibe de 1 a 5 artistas o canciones semilla + cantidad deseada.
    Resuelve cada semilla a sus temas principales (canciones + videos relevantes) y su radio (get_watch_playlist),
    y arma una lista balanceada intercalando (Round-Robin) pistas de cada semilla sin repetir canciones.
    """
    clean_seeds = [s.strip() for s in seeds if s and s.strip()][:5]
    if not clean_seeds:
        return {"seeds_resolved": [], "tracks": []}

    total_count = max(5, min(int(total_count), 100))
    yt = get_yt()

    seed_buckets: list[list[dict[str, Any]]] = []
    seeds_resolved: list[dict[str, Any]] = []
    per_seed_target = max(15, (total_count // len(clean_seeds)) + 12)

    for seed_query in clean_seeds:
        bucket: list[dict[str, Any]] = []
        try:
            direct_songs = yt.search(seed_query, filter="songs", limit=10) or []
        except Exception:
            direct_songs = []

        if len(direct_songs) < 3:
            try:
                direct_videos = yt.search(seed_query, filter="videos", limit=6) or []
                direct_songs.extend(direct_videos)
            except Exception:
                pass

        seed_video_id = None
        seed_label = seed_query

        for idx, item in enumerate(direct_songs):
            norm = normalize_track_item(item, scan_data=scan_data)
            if not norm:
                continue
            if idx == 0:
                seed_video_id = norm["videoId"]
                seed_label = f"{norm['artist']} — {norm['title']}"
            bucket.append(norm)

        if seed_video_id:
            try:
                watch = yt.get_watch_playlist(videoId=seed_video_id, radio=True, limit=per_seed_target)
                for w_item in watch.get("tracks") or []:
                    norm = normalize_track_item(w_item, scan_data=scan_data)
                    if norm:
                        bucket.append(norm)
            except Exception:
                pass

        filtered_bucket = apply_track_filters(
            bucket,
            max_duration_mins=max_duration_mins,
            exclude_karaoke=exclude_karaoke,
            exclude_live=exclude_live,
            exclude_remix=exclude_remix,
            exclude_cover=exclude_cover,
            exclude_live_remix=exclude_live_remix,
        )

        if filtered_bucket:
            seed_buckets.append(filtered_bucket)
            seeds_resolved.append(
                {
                    "query": seed_query,
                    "resolved_name": seed_label,
                    "found_count": len(filtered_bucket),
                }
            )

    balanced_mix: list[dict[str, Any]] = []
    seen_video_ids: set[str] = set()
    seen_norm_keys: set[str] = set()

    max_len = max((len(b) for b in seed_buckets), default=0)
    for i in range(max_len):
        for bucket in seed_buckets:
            if i >= len(bucket):
                continue
            track = bucket[i]
            vid = track["videoId"]
            t_key = normalize_key(f"{track['artist']} {track['title']}")
            if vid in seen_video_ids or (t_key and t_key in seen_norm_keys):
                continue

            seen_video_ids.add(vid)
            if t_key:
                seen_norm_keys.add(t_key)
            balanced_mix.append(track)

            if len(balanced_mix) >= total_count:
                break
        if len(balanced_mix) >= total_count:
            break

    return {
        "seeds_resolved": seeds_resolved,
        "tracks": balanced_mix,
    }
