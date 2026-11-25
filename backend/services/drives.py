"""
Detector de Pendrives, selector nativo de carpetas y escáner anti-duplicados
compatible con nombres estándar, ASCII y Shuffle Físico (001 - Artista - Título).
"""

from __future__ import annotations

import ctypes
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import psutil
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from unidecode import unidecode

# Regex para limpiar prefijos numéricos de Shuffle Físico (ej: "001 - ", "42 - ", "01. ")
SHUFFLE_PREFIX_RE = re.compile(r"^\d+\s*[-_.]+\s*")
# Regex para normalizar espacios y signos de puntuación al comparar duplicados
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_key(text: str) -> str:
    """Convierte cualquier texto a una clave alfanumérica ASCII en minúsculas para comparación."""
    if not text:
        return ""
    ascii_text = unidecode(str(text)).lower().strip()
    return NON_ALNUM_RE.sub(" ", ascii_text).strip()


def strip_shuffle_prefix(stem: str) -> str:
    """Elimina cualquier prefijo numérico inicial ('^\\d+\\s*-\\s*') de Shuffle Físico."""
    return SHUFFLE_PREFIX_RE.sub("", stem).strip()


def _get_windows_volume_info(mountpoint: str) -> tuple[str, str]:
    """
    Obtiene (volume_label, filesystem_type) en Windows usando GetVolumeInformationW.
    """
    if platform.system() != "Windows":
        return ("", "")
    try:
        kernel32 = ctypes.windll.kernel32
        vol_name_buf = ctypes.create_unicode_buffer(261)
        fs_name_buf = ctypes.create_unicode_buffer(261)
        serial_num = ctypes.c_ulong(0)
        max_comp_len = ctypes.c_ulong(0)
        fs_flags = ctypes.c_ulong(0)

        root_path = mountpoint if mountpoint.endswith("\\") else f"{mountpoint}\\"
        res = kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root_path),
            vol_name_buf,
            ctypes.sizeof(vol_name_buf),
            ctypes.byref(serial_num),
            ctypes.byref(max_comp_len),
            ctypes.byref(fs_flags),
            fs_name_buf,
            ctypes.sizeof(fs_name_buf),
        )
        if res:
            return (vol_name_buf.value.strip(), fs_name_buf.value.strip())
    except Exception:
        pass
    return ("", "")


def list_drives() -> list[dict[str, Any]]:
    """
    Lista unidades extraíbles y locales conectadas usando psutil.disk_partitions()
    y psutil.disk_usage(), indicando espacio libre en GB, etiqueta, formato
    (FAT32 / exFAT / NTFS) y nivel de compatibilidad con estéreos de auto.
    Prioriza las unidades extraíbles al principio de la lista.
    """
    drives: list[dict[str, Any]] = []
    seen_mounts: set[str] = set()

    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception:
        partitions = []

    for part in partitions:
        mount = part.mountpoint
        if not mount or mount in seen_mounts:
            continue
        if "cdrom" in part.opts.lower() or not part.fstype:
            continue

        seen_mounts.add(mount)
        try:
            usage = psutil.disk_usage(mount)
        except Exception:
            continue

        vol_label, win_fs = _get_windows_volume_info(mount)
        fstype = (win_fs or part.fstype or "Desconocido").upper()
        is_removable = "removable" in part.opts.lower()

        # En Linux/macOS considerar /media, /mnt, /run/media o /Volumes como extraíbles
        if platform.system() != "Windows":
            if any(mount.startswith(p) for p in ("/media", "/mnt", "/run/media", "/Volumes")):
                is_removable = True

        free_gb = round(usage.free / (1024**3), 2)
        total_gb = round(usage.total / (1024**3), 2)
        used_percent = round(usage.percent, 1)

        if not vol_label:
            vol_label = "PENDRIVE USB" if is_removable else "Disco Local"

        # Evaluar compatibilidad con estéreos de auto según sistema de archivos
        car_compat = "optimal"
        car_warning = None
        if fstype in ("NTFS", "EXFAT"):
            car_compat = "warning"
            car_warning = (
                f"Formato {fstype} detectado: Muchos estéreos de auto (especialmente Pioneer, "
                "Sony y equipos OEM) solo leen pendrives en FAT32. Si tu estéreo muestra "
                "'USB Error' o 'No Support', formatealo en FAT32."
            )
        elif "FAT" not in fstype:
            car_compat = "warning"
            car_warning = f"Formato {fstype}: Revisá si tu estéreo soporta este sistema de archivos (se recomienda FAT32)."

        letter = mount.rstrip("\\/")
        display_name = f"{letter} [{vol_label}] — {free_gb} GB libres ({fstype})"

        drives.append(
            {
                "mountpoint": mount,
                "letter": letter,
                "label": vol_label,
                "fstype": fstype,
                "is_removable": is_removable,
                "free_bytes": usage.free,
                "total_bytes": usage.total,
                "free_gb": free_gb,
                "total_gb": total_gb,
                "used_percent": used_percent,
                "car_compatibility": car_compat,
                "car_warning": car_warning,
                "display_name": display_name,
            }
        )

    # Ordenar: primero extraíbles (pendrives), luego por letra de unidad
    drives.sort(key=lambda d: (not d["is_removable"], d["mountpoint"]))
    return drives


def get_default_output_dir() -> str:
    """
    Devuelve la ruta del primer pendrive extraíble conectado o, si no hay ninguno,
    la carpeta local './BullakMusic_Descargas'.
    """
    drives = list_drives()
    for d in drives:
        if d["is_removable"]:
            return d["mountpoint"]

    default_dir = (Path.cwd() / "BullakMusic_Descargas").resolve()
    try:
        default_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return str(default_dir)


def get_folder_disk_info(folder_path: str) -> dict[str, Any]:
    """
    Obtiene información de espacio libre, total y formato de la unidad donde reside folder_path.
    """
    try:
        target = Path(folder_path).resolve()
        check_path = target if target.exists() else target.parent
        while not check_path.exists() and check_path.parent != check_path:
            check_path = check_path.parent

        usage = psutil.disk_usage(str(check_path))
        anchor = check_path.anchor or str(check_path)

        # Cruzar con list_drives() para obtener fstype y alertas
        drives = list_drives()
        matched_drive = None
        for d in drives:
            if os.path.normcase(anchor).startswith(os.path.normcase(d["mountpoint"].rstrip("\\/"))):
                matched_drive = d
                break

        fstype = matched_drive["fstype"] if matched_drive else "LOCAL"
        car_compat = matched_drive["car_compatibility"] if matched_drive else "optimal"
        car_warning = matched_drive["car_warning"] if matched_drive else None
        is_removable = matched_drive["is_removable"] if matched_drive else False
        label = matched_drive["label"] if matched_drive else check_path.name or "Carpeta Local"

        return {
            "path": str(target),
            "exists": target.exists(),
            "label": label,
            "fstype": fstype,
            "is_removable": is_removable,
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "free_gb": round(usage.free / (1024**3), 2),
            "total_gb": round(usage.total / (1024**3), 2),
            "used_percent": round(usage.percent, 1),
            "car_compatibility": car_compat,
            "car_warning": car_warning,
        }
    except Exception as exc:
        return {
            "path": folder_path,
            "exists": False,
            "label": "Ruta inválida",
            "fstype": "N/A",
            "is_removable": False,
            "free_bytes": 0,
            "total_bytes": 0,
            "free_gb": 0.0,
            "total_gb": 0.0,
            "used_percent": 0.0,
            "car_compatibility": "warning",
            "car_warning": f"No se pudo acceder a la ruta: {exc}",
        }


def scan_existing_mp3s(target_dir: str) -> dict[str, Any]:
    """
    Escanea recursivamente los archivos .mp3 en la carpeta destino.
    Limpia prefijos de Shuffle Físico ('^\\d+\\s*-\\s*') antes de normalizar nombres,
    y también extrae combinaciones 'artista - titulo' y títulos individuales de tags ID3
    para detectar duplicados con precisión.
    """
    result = {
        "count": 0,
        "total_size_mb": 0.0,
        "normalized_keys": set(),
        "normalized_titles": set(),
        "video_ids": set(),
        "files": [],
    }

    if not target_dir:
        return result

    root = Path(target_dir)
    if not root.exists() or not root.is_dir():
        return result

    total_bytes = 0
    max_scan = 3000  # Límite de seguridad para pendrives enormes

    try:
        for idx, mp3_path in enumerate(root.rglob("*.mp3")):
            if idx >= max_scan:
                break
            if not mp3_path.is_file():
                continue

            result["count"] += 1
            try:
                fsize = mp3_path.stat().st_size
                total_bytes += fsize
            except Exception:
                fsize = 0

            raw_stem = mp3_path.stem
            # Limpiar prefijo numérico de Shuffle Físico: "001 - Artista - Titulo" -> "Artista - Titulo"
            clean_stem = strip_shuffle_prefix(raw_stem)
            norm_stem = normalize_key(clean_stem)
            if norm_stem:
                result["normalized_keys"].add(norm_stem)

            # Si el nombre tiene formato "Artista - Titulo", guardar también el título limpio y la combinación
            if " - " in clean_stem:
                parts = clean_stem.split(" - ", 1)
                artist_part = normalize_key(parts[0])
                title_part = normalize_key(parts[1])
                if artist_part and title_part:
                    result["normalized_keys"].add(f"{artist_part} {title_part}")
                    result["normalized_titles"].add((artist_part, title_part))

            # Además considerar si está en subcarpeta por artista: "Artista/Titulo.mp3"
            parent_name = mp3_path.parent.name
            if parent_name and mp3_path.parent != root:
                norm_parent = normalize_key(parent_name)
                if norm_parent and norm_stem:
                    result["normalized_keys"].add(f"{norm_parent} {norm_stem}")
                    result["normalized_titles"].add((norm_parent, norm_stem))

            # Intentar leer tags ID3 rápidos si existen
            try:
                tags = ID3(str(mp3_path))
                tit2 = str(tags.get("TIT2", "")).strip()
                tpe1 = str(tags.get("TPE1", "")).strip()
                if tit2 and tpe1:
                    na = normalize_key(tpe1)
                    nt = normalize_key(tit2)
                    result["normalized_keys"].add(f"{na} {nt}")
                    result["normalized_titles"].add((na, nt))
                # Comentario opcional o TXXX si alguna vez se guardó videoId
                for frame in tags.getall("COMM"):
                    if frame.text:
                        for t in frame.text:
                            if len(str(t)) == 11:
                                result["video_ids"].add(str(t))
            except Exception:
                pass

            if len(result["files"]) < 200:
                result["files"].append(
                    {
                        "name": mp3_path.name,
                        "relative_path": str(mp3_path.relative_to(root)),
                        "size_mb": round(fsize / (1024 * 1024), 2),
                    }
                )
    except Exception:
        pass

    result["total_size_mb"] = round(total_bytes / (1024 * 1024), 2)
    return result


def is_track_in_destination(track: dict[str, Any], scan_data: dict[str, Any]) -> bool:
    """
    Compara una pista de búsqueda o cola contra el índice escaneado en la carpeta destino.
    Detecta coincidencias exactas o con Shuffle Físico / modo ASCII.
    """
    if not scan_data or scan_data.get("count", 0) == 0:
        return False

    vid = track.get("videoId") or track.get("video_id") or ""
    if vid and vid in scan_data.get("video_ids", set()):
        return True

    artist = normalize_key(track.get("artist", ""))
    title = normalize_key(track.get("title", ""))
    if not title:
        return False

    combined = f"{artist} {title}".strip()
    norm_keys: set[str] = scan_data.get("normalized_keys", set())
    norm_tuples: set[tuple[str, str]] = scan_data.get("normalized_titles", set())

    if combined in norm_keys or title in norm_keys:
        return True

    if (artist, title) in norm_tuples:
        return True

    # Comparación flexible: si el título y primer artista coinciden
    primary_artist = normalize_key(track.get("artist", "").split(",")[0].split("&")[0])
    if primary_artist:
        alt_combined = f"{primary_artist} {title}".strip()
        if alt_combined in norm_keys or (primary_artist, title) in norm_tuples:
            return True

    return False


def pick_folder_Dialog(initial_dir: str = "") -> str | None:
    """
    Abre un selector de carpetas nativo ejecutando tkinter en un subproceso aislado
    (subprocess.run con sys.executable -c) y forzando root.attributes('-topmost', True)
    para evitar bloqueos del loop de uvicorn o ventanas detrás del navegador en Windows.
    Si tkinter no está compilado en el entorno Python, usa un diálogo nativo de Windows
    mediante PowerShell System.Windows.Forms.FolderBrowserDialog como fallback seguro.
    """
    init_path = initial_dir if initial_dir and os.path.isdir(initial_dir) else str(Path.home())

    tk_script = (
        "import sys\n"
        "try:\n"
        "    import tkinter as tk\n"
        "    from tkinter import filedialog\n"
        "    root = tk.Tk()\n"
        "    root.withdraw()\n"
        "    root.attributes('-topmost', True)\n"
        "    root.lift()\n"
        "    root.focus_force()\n"
        "    folder = filedialog.askdirectory(parent=root, initialdir=sys.argv[1], title='BullakMusic-converter — Seleccionar Pendrive o Carpeta Destino')\n"
        "    root.destroy()\n"
        "    if folder:\n"
        "        sys.stdout.write(folder)\n"
        "except Exception as e:\n"
        "    sys.exit(2)\n"
    )

    try:
        proc = subprocess.run(
            [sys.executable, "-c", tk_script, init_path],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode == 0:
            chosen = proc.stdout.strip()
            return os.path.normpath(chosen) if chosen else None
    except Exception:
        pass

    # Fallback nativo en Windows vía PowerShell si el build de Python no trae Tcl/Tk
    if platform.system() == "Windows":
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$f.Description = 'BullakMusic-converter — Seleccionar Pendrive o Carpeta Destino'; "
            f"$f.SelectedPath = '{init_path.replace(chr(39), chr(39)*2)}'; "
            "$f.ShowNewFolderButton = $true; "
            "$top = New-Object System.Windows.Forms.Form; "
            "$top.TopMost = $true; "
            "if ($f.ShowDialog($top) -eq 'OK') { Write-Output $f.SelectedPath }"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return os.path.normpath(proc.stdout.strip())
        except Exception:
            pass

    return None


def open_folder_in_explorer(folder_path: str) -> bool:
    """Abre la carpeta destino en el explorador de archivos del sistema operativo."""
    target = Path(folder_path)
    target.mkdir(parents=True, exist_ok=True)
    resolved = str(target.resolve())

    try:
        if platform.system() == "Windows":
            os.startfile(resolved)  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", resolved])
        else:
            subprocess.Popen(["xdg-open", resolved])
        return True
    except Exception:
        return False
