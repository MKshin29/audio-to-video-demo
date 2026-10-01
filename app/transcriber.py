"""Распознавание речи (faster-whisper, полностью локально) и разметка реплик по говорящим."""
import os
import re
import threading

import numpy as np

from audio_utils import read_wav
from diarization import assign_speakers, label_robot_by_talk_time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(ROOT, "models")

MODELS = [
    {"id": "small", "title": "small — быстро, качество среднее (~0.5 ГБ)"},
    {"id": "medium", "title": "medium — медленнее, качество хорошее (~1.5 ГБ)"},
    {"id": "large-v3-turbo", "title": "large-v3-turbo — хорошее качество, умеренная скорость (~1.6 ГБ)"},
    {"id": "large-v3", "title": "large-v3 — лучшее качество, медленно (~3 ГБ)"},
]
HF_REPOS = {
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}

# Фразы, которые Whisper «галлюцинирует» на тишине и шуме
HALLUCINATIONS = re.compile(
    r"(субтитр|продолжение следует|спасибо за просмотр|редактор субтитров|подписывайтесь|"
    r"amara\.org|dimatorzok)", re.IGNORECASE)

_models = {}
_model_lock = threading.Lock()


def local_model_path(name):
    """Папка с моделью, положенной вручную: models/<name>/model.bin (для сетей без доступа к HuggingFace)."""
    p = os.path.join(MODELS_DIR, name)
    return p if os.path.isfile(os.path.join(p, "model.bin")) else None


def model_downloaded(name):
    if local_model_path(name):
        return True
    repo_dir = os.path.join(MODELS_DIR, "models--" + HF_REPOS.get(name, "").replace("/", "--"), "snapshots")
    if os.path.isdir(repo_dir):
        for snap in os.listdir(repo_dir):
            if os.path.isfile(os.path.join(repo_dir, snap, "model.bin")):
                return True
    return False


def get_model(name, status_cb):
    from faster_whisper import WhisperModel

    with _model_lock:
        if name in _models:
            return _models[name]
        threads = max(1, (os.cpu_count() or 4))
        kwargs = dict(device="cpu", compute_type="int8", cpu_threads=threads)
        path = local_model_path(name)
        if path:
            status_cb("Загрузка модели из папки models\\%s…" % name)
            model = WhisperModel(path, **kwargs)
        else:
            try:
                status_cb("Загрузка модели «%s»…" % name)
                model = WhisperModel(name, download_root=MODELS_DIR, local_files_only=True, **kwargs)
            except Exception:
                status_cb("Скачивание модели «%s» (только при первом использовании, может занять несколько минут)…" % name)
                try:
                    model = WhisperModel(name, download_root=MODELS_DIR, **kwargs)
                except Exception as e:
                    raise RuntimeError(
                        "Не удалось скачать модель «%s». Нужен доступ в интернет при первом запуске "
                        "или ручная установка модели в папку models\\%s (см. README). Детали: %s" % (name, name, e))
        _models[name] = model
        return model


def _run_whisper(model, audio, language, progress_cb):
    """Распознаёт один канал, возвращает список фраз с границами слов."""
    duration = max(len(audio) / 16000.0, 0.1)
    segments, _info = model.transcribe(
        audio,
        language=None if language == "auto" else language,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
        word_timestamps=True,
        condition_on_previous_text=False,
    )
    out = []
    for seg in segments:
        progress_cb(min(1.0, seg.end / duration))
        text = seg.text.strip()
        if not text or HALLUCINATIONS.search(text):
            continue
        if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
            continue
        words = [{"start": w.start, "end": w.end, "word": w.word} for w in (seg.words or [])]
        out.append({"start": seg.start, "end": seg.end, "text": text, "words": words})
    progress_cb(1.0)
    return out


def _split_by_pauses(segments, gap=0.7):
    """Разбивает фразы Whisper по длинным паузам между словами.

    В моно-записи одна фраза Whisper иногда захватывает и робота, и клиента;
    смена говорящего почти всегда сопровождается паузой.
    """
    out = []
    for seg in segments:
        words = seg["words"]
        if len(words) < 2:
            out.append({"start": seg["start"], "end": seg["end"], "text": seg["text"]})
            continue
        chunk = [words[0]]
        for w in words[1:]:
            if w["start"] - chunk[-1]["end"] >= gap:
                out.append(_chunk_to_seg(chunk))
                chunk = []
            chunk.append(w)
        out.append(_chunk_to_seg(chunk))
    return [s for s in out if s["text"]]


def _chunk_to_seg(words):
    return {"start": words[0]["start"], "end": words[-1]["end"],
            "text": "".join(w["word"] for w in words).strip()}


def _merge_same_speaker(segments, max_gap=1.2):
    out = []
    for s in segments:
        if out and out[-1]["speaker"] == s["speaker"] and s["start"] - out[-1]["end"] <= max_gap:
            out[-1]["end"] = max(out[-1]["end"], s["end"])
            out[-1]["text"] = (out[-1]["text"] + " " + s["text"]).strip()
        else:
            out.append(dict(s))
    return out


def _rms(x):
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


def _channels_are_separate(stereo):
    """True, если в левом и правом канале разные собеседники (типичная запись телефонии)."""
    left, right = stereo[0], stereo[1]
    rl, rr = _rms(left), _rms(right)
    if min(rl, rr) < 0.03 * max(rl, rr, 1e-9):
        return False  # один из каналов фактически пустой
    step = max(1, len(left) // 2_000_000)
    corr = np.corrcoef(left[::step], right[::step])[0, 1]
    return not np.isfinite(corr) or abs(corr) < 0.8


def transcribe(asr_wav, model_name, language, status_cb, progress_cb):
    """Возвращает список реплик [{speaker, start, end, text}], отсортированных по времени."""
    audio, rate = read_wav(asr_wav)
    assert rate == 16000
    model = get_model(model_name, status_cb)

    if audio.shape[0] == 2 and _channels_are_separate(audio):
        segments = []
        for ch in range(2):
            status_cb("Распознавание: стереозапись, канал %d из 2…" % (ch + 1))
            res = _run_whisper(model, audio[ch], language,
                               lambda p, ch=ch: progress_cb((ch + p) / 2.0))
            for s in _split_by_pauses(res, gap=1.0):
                a, b = int(s["start"] * rate), int(s["end"] * rate)
                own, other = _rms(audio[ch][a:b]), _rms(audio[1 - ch][a:b])
                if own < 0.35 * other:
                    continue  # «эхо» собеседника, просочившееся в чужой канал
                s["speaker"] = ch
                segments.append(s)
        segments.sort(key=lambda s: s["start"])
        segments = label_robot_by_talk_time(segments)
    else:
        mono = audio.mean(axis=0) if audio.shape[0] > 1 else audio[0]
        status_cb("Распознавание речи…")
        res = _run_whisper(model, mono, language, progress_cb)
        status_cb("Определение говорящих…")
        segments = assign_speakers(mono, _split_by_pauses(res))

    segments = _merge_same_speaker(segments)
    return [{"speaker": s["speaker"], "start": round(float(s["start"]), 2), "end": round(float(s["end"]), 2),
             "text": s["text"]} for s in segments]
