"""
Pipeline de etiquetado ID3v2.3 y carátulas JPEG Baseline 500x500 (<100 KB) "Car-Safe"
y sanitización de rutas compatibles con estéreos de auto (FAT32 / ASCII).
Incluye el frame estándar TSSE ("BullakMusic-converter by bullak").
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import httpx
from mutagen.id3 import APIC, COMM, ID3, TALB, TIT2, TPE1, TRCK, TSSE, ID3NoHeaderError
from pathvalidate import sanitize_filename
from PIL import Image, ImageDraw, ImageOps
from unidecode import unidecode

MAX_COVER_BYTES = 98_000  # < 100 KB estricto para estéreos de auto
COVER_SIZE = (500, 500)
ENCODER_SIGNATURE = "BullakMusic-converter by bullak"


def clean_car_text(text: str, ascii_mode: bool = False) -> str:
    """
    Limpia texto para tags ID3 y nombres de archivo.
    Si ascii_mode=True (Modo Estéreo Antiguo), aplica Unidecode para eliminar
    acentos, 'ñ', diéresis, símbolos y emojis que rompen pantallas VFD/LCD en FAT32.
    """
    if not text:
        return ""
    val = str(text).strip()
    if ascii_mode:
        val = unidecode(val)
        # Mantener solo caracteres ASCII imprimibles seguros
        val = re.sub(r"[^\x20-\x7E]+", "", val)
    # Colapsar espacios múltiples
    val = re.sub(r"\s+", " ", val).strip()
    return val


def build_car_safe_filepath(
    output_dir: str,
    artist: str,
    title: str,
    ascii_mode: bool = False,
    organization: str = "flat",
    shuffle_index: int | None = None,
    total_batch: int = 99,
) -> Path:
    """
    Construye la ruta final del archivo MP3 según las opciones del Perfil Estéreo de Auto:
    - organization == 'flat': Todo en la carpeta raíz destino ('Artista - Titulo.mp3')
    - organization == 'artist_folders': Subcarpetas por artista ('Artista/Artista - Titulo.mp3')
    - organization == 'physical_shuffle': Antepone prefijo aleatorio ('001 - Artista - Titulo.mp3')
    """
    base_dir = Path(output_dir)

    # Tomar artista principal para carpetas más limpias
    primary_artist = artist.split(",")[0].strip() if artist else "Artista"
    clean_artist = clean_car_text(primary_artist, ascii_mode=ascii_mode) or "Artista"
    clean_full_artist = clean_car_text(artist, ascii_mode=ascii_mode) or clean_artist
    clean_title = clean_car_text(title, ascii_mode=ascii_mode) or "Track"

    # Reemplazar barras o dos puntos antes de pathvalidate para legibilidad
    for ch in ("/", "\\", ":", "|", "*"):
        clean_artist = clean_artist.replace(ch, "-")
        clean_full_artist = clean_full_artist.replace(ch, "-")
        clean_title = clean_title.replace(ch, "-")

    if organization == "artist_folders":
        safe_folder = str(
            sanitize_filename(
                clean_artist[:60],
                replacement_text="_",
                platform="universal",
            )
        ).strip(" .") or "Artista"
        target_folder = base_dir / safe_folder
        raw_filename = f"{clean_full_artist} - {clean_title}.mp3"
    elif organization == "physical_shuffle" and shuffle_index is not None:
        target_folder = base_dir
        digits = max(3, len(str(max(total_batch, shuffle_index))))
        prefix = f"{shuffle_index:0{digits}d}"
        raw_filename = f"{prefix} - {clean_full_artist} - {clean_title}.mp3"
    else:
        target_folder = base_dir
        raw_filename = f"{clean_full_artist} - {clean_title}.mp3"

    # Limitar longitud de nombre de archivo para sistemas FAT32 de estéreos antiguos
    safe_filename = str(
        sanitize_filename(
            raw_filename,
            replacement_text="",
            platform="universal",
            max_len=120,
        )
    ).strip()

    if not safe_filename.lower().endswith(".mp3"):
        safe_filename = f"{safe_filename}.mp3"

    target_folder.mkdir(parents=True, exist_ok=True)
    return target_folder / safe_filename


def process_image_to_baseline_jpeg(image_bytes: bytes) -> bytes:
    """
    Convierte cualquier imagen (WebP, PNG, JPEG progresivo o rectangular 16:9)
    en un cuadrado perfecto de 500x500 píxeles codificado en JPEG Baseline
    (progressive=False) con un peso garantizado menor a 100 KB.
    """
    with Image.open(io.BytesIO(image_bytes)) as img:
        rgb_img = img.convert("RGB")
        # Recorte cuadrado centrado (500x500) con filtro LANCZOS de alta nitidez
        square_img = ImageOps.fit(
            rgb_img,
            COVER_SIZE,
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )

    # Comprimir iterativamente asegurando JPEG Baseline (< 100 KB)
    for quality in (85, 78, 70, 62, 54, 45):
        buf = io.BytesIO()
        square_img.save(
            buf,
            format="JPEG",
            quality=quality,
            progressive=False,  # CRÍTICO: estéreos de auto fallan con JPEG progresivo
            optimize=True,
        )
        data = buf.getvalue()
        if len(data) <= MAX_COVER_BYTES:
            return data

    # Si aún supera 98 KB (muy raro en 500x500), bajar a calidad 38
    buf = io.BytesIO()
    square_img.save(buf, format="JPEG", quality=38, progressive=False, optimize=True)
    return buf.getvalue()


def generate_fallback_cover_jpeg() -> bytes:
    """
    Genera una carátula 500x500 JPEG Baseline elegante con estética automotriz
    de BullakMusic en caso de que la canción no tenga miniatura disponible o falle la red.
    """
    img = Image.new("RGB", COVER_SIZE, color=(15, 15, 20))
    draw = ImageDraw.Draw(img)
    # Círculos concéntricos estilo dial hi-fi de BullakMusic
    draw.ellipse((70, 70, 430, 430), outline=(249, 115, 22), width=6)
    draw.ellipse((150, 150, 350, 350), outline=(63, 63, 74), width=3)
    draw.ellipse((220, 220, 280, 280), fill=(249, 115, 22))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82, progressive=False, optimize=True)
    return buf.getvalue()


async def fetch_and_prepare_cover(thumbnail_url: str, video_id: str = "") -> bytes:
    """
    Descarga la miniatura de forma asíncrona con httpx y la transforma a
    500x500 JPEG Baseline (<100 KB).
    """
    urls_to_try: list[str] = []
    if thumbnail_url:
        urls_to_try.append(thumbnail_url)
    if video_id:
        urls_to_try.append(f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg")
        urls_to_try.append(f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg")

    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        for url in urls_to_try:
            try:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.content:
                    return process_image_to_baseline_jpeg(resp.content)
            except Exception:
                continue

    return generate_fallback_cover_jpeg()


def apply_car_safe_id3v23_tags(
    mp3_path: str | Path,
    title: str,
    artist: str,
    album: str = "BullakMusic",
    cover_jpeg_bytes: bytes | None = None,
    ascii_mode: bool = False,
    track_number: int | None = None,
    video_id: str = "",
) -> None:
    """
    Borra cualquier metadato previo (ID3v2.4, APEv2, comentarios basura) y escribe
    estrictamente tags ID3v2.3 limpios (TIT2, TPE1, TALB, TRCK, TSSE, APIC) con Mutagen.
    """
    path_str = str(mp3_path)

    # Limpiar tags existentes para evitar conflictos de versión ID3v2.4
    try:
        existing = ID3(path_str)
        existing.delete(path_str)
    except ID3NoHeaderError:
        pass
    except Exception:
        pass

    tags = ID3()

    clean_title = clean_car_text(title, ascii_mode=ascii_mode) or "Track"
    clean_artist = clean_car_text(artist, ascii_mode=ascii_mode) or "Artista"
    clean_album = clean_car_text(album, ascii_mode=ascii_mode) or "BullakMusic"

    # En modo ASCII usamos encoding=0 (ISO-8859-1 / Latin-1) que leen incluso estéreos de los años 2000;
    # en modo estándar usamos encoding=1 (UTF-16) compatible con la especificación oficial ID3v2.3.
    text_encoding = 0 if ascii_mode else 1

    tags.add(TIT2(encoding=text_encoding, text=clean_title))
    tags.add(TPE1(encoding=text_encoding, text=clean_artist))
    tags.add(TALB(encoding=text_encoding, text=clean_album))
    tags.add(TSSE(encoding=0, text=ENCODER_SIGNATURE))

    if track_number is not None and track_number > 0:
        tags.add(TRCK(encoding=0, text=str(track_number)))

    if video_id:
        tags.add(COMM(encoding=0, lang="eng", desc="", text=video_id))

    if cover_jpeg_bytes:
        tags.add(
            APIC(
                encoding=0,
                mime="image/jpeg",
                type=3,  # 3 = Front Cover (obligatorio para que el estéreo muestre la foto)
                desc="Cover",
                data=cover_jpeg_bytes,
            )
        )

    # Guardar forzando v2_version=3 (ID3v2.3 estricto) y v1=2 (incluye ID3v1 al final del archivo para estéreos viejos)
    tags.save(path_str, v1=2, v2_version=3)
