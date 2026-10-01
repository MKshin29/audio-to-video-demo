"""Работа с аудио: конвертация через ffmpeg (из пакета imageio-ffmpeg) и чтение WAV."""
import subprocess
import wave

import numpy as np
import imageio_ffmpeg

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def ffmpeg_exe():
    return imageio_ffmpeg.get_ffmpeg_exe()


def input_channels(src):
    """Число каналов в первой аудиодорожке входного файла (по выводу `ffmpeg -i`)."""
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", src],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
    info = proc.stderr.decode("utf-8", errors="replace")
    for line in info.splitlines():
        if "Audio:" in line:
            low = line.lower()
            return 1 if (" mono" in low or " 1 channels" in low) else 2
    raise RuntimeError("Во входном файле не найдена аудиодорожка (формат не поддерживается?)")


def convert_to_wav(src, dst, sample_rate, channels):
    """Перекодирует любой входной аудиофайл в 16-битный PCM WAV.

    Такой файл гарантированно воспроизводится браузером, точно перематывается
    и одинаково читается при распознавании и при сборке видео.
    """
    proc = subprocess.run([
        ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", src, "-vn", "-map", "0:a:0",
        "-ac", str(channels), "-ar", str(sample_rate), "-c:a", "pcm_s16le", dst,
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise RuntimeError("Не удалось прочитать аудиофайл: " + (" ".join(err[-3:]) if err else "ошибка ffmpeg"))


def read_wav(path):
    """Читает 16-битный PCM WAV, возвращает (float32 массив [каналы, отсчёты], частота)."""
    with wave.open(path, "rb") as wf:
        channels = wf.getnchannels()
        rate = wf.getframerate()
        if wf.getsampwidth() != 2:
            raise RuntimeError("Ожидался 16-битный WAV")
        raw = wf.readframes(wf.getnframes())
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return data.reshape(-1, channels).T.copy(), rate


def wav_duration(path):
    with wave.open(path, "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())
