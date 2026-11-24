"""Konwersja dźwięku ffmpeg: nagranie w formacie nadawcy (telefon, przeglądarka, tablet)
-> format tabletu z jego konfiguracji. Pliki tymczasowe (MP4/M4A wymaga zapisu z
przewijaniem, więc bez potoków). Wynik dla danego formatu liczony raz na wiadomość.
"""
import asyncio
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path

_LOG = logging.getLogger(__name__)

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
TMP_DIR = os.environ.get("AUDIO_TMP") or tempfile.gettempdir()
TIMEOUT = 60

# format tabletu -> (rozszerzenie, argumenty kodera); mono, wyrównana głośność
CODECS = {
    "m4a": ("m4a", ["-c:a", "aac", "-b:a", "64k", "-ar", "44100"]),
    "mp3": ("mp3", ["-c:a", "libmp3lame", "-b:a", "64k", "-ar", "44100"]),
    "ogg": ("ogg", ["-c:a", "libvorbis", "-q:a", "4", "-ar", "44100"]),
    "opus": ("opus", ["-c:a", "libopus", "-b:a", "32k"]),
    "wav": ("wav", ["-c:a", "pcm_s16le", "-ar", "22050"]),
}

# typ MIME nadawcy -> rozszerzenie pliku wejściowego (ffmpeg i tak rozpoznaje zawartość)
MIME_EXT = {
    "audio/webm": "webm", "video/webm": "webm", "audio/ogg": "ogg", "audio/opus": "opus",
    "audio/mp4": "m4a", "audio/m4a": "m4a", "audio/x-m4a": "m4a", "audio/aac": "aac",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
    "audio/3gpp": "3gp", "audio/amr": "amr",
}

_TIME_RE = re.compile(rb"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


class AudioError(Exception):
    pass


def ext_for(mime: str) -> str:
    mime = (mime or "").split(";")[0].strip().lower()
    return MIME_EXT.get(mime, "bin")


async def convert(data: bytes, src_ext: str, fmt: str) -> tuple[bytes, float]:
    """(dane w formacie fmt, długość w sekundach)."""
    if fmt not in CODECS:
        raise AudioError(f"nieznany format {fmt}")
    out_ext, codec = CODECS[fmt]
    tag = uuid.uuid4().hex[:10]
    src = Path(TMP_DIR) / f"kc_{tag}_in.{src_ext or 'bin'}"
    dst = Path(TMP_DIR) / f"kc_{tag}_out.{out_ext}"
    try:
        src.write_bytes(data)
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-hide_banner", "-nostdin", "-y", "-i", str(src), "-vn", "-ac", "1",
            "-af", "dynaudnorm=f=200:g=15", *codec, str(dst),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            raise AudioError("ffmpeg: przekroczony czas") from None
        if proc.returncode != 0 or not dst.exists():
            tail = err.decode(errors="replace").strip().splitlines()[-1:] or ["?"]
            raise AudioError(f"ffmpeg: {tail[0]}")
        times = _TIME_RE.findall(err)
        duration = 0.0
        if times:
            h, m, s = times[-1]
            duration = int(h) * 3600 + int(m) * 60 + float(s)
        return dst.read_bytes(), round(duration, 1)
    finally:
        for p in (src, dst):
            try:
                p.unlink()
            except OSError:
                pass


class Converted:
    """Jedna wiadomość w wielu formatach - konwersja raz na format."""
    def __init__(self, data: bytes, src_ext: str):
        self.data, self.src_ext = data, src_ext
        self._cache: dict[str, asyncio.Future] = {}

    async def get(self, fmt: str) -> tuple[bytes, float]:
        if fmt not in self._cache:
            self._cache[fmt] = asyncio.ensure_future(convert(self.data, self.src_ext, fmt))
        return await self._cache[fmt]
