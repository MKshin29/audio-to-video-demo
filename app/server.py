"""Локальный веб-сервер инструмента «Аудио → видео-чат».

Запуск: start.bat (или `python app/server.py`). Интерфейс открывается в браузере.
Все данные обрабатываются локально, в папке workspace.
"""
import io
import json
import logging
import os
import shutil
import socket
import sys
import threading
import time
import traceback
import uuid
import webbrowser

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audio_utils  # noqa: E402
import transcriber  # noqa: E402
from renderer import ChatRenderer, render_video  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.path.join(ROOT, "workspace")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
KEEP_DAYS = 14

RESOLUTIONS = {"720": (720, 1280), "1080": (1080, 1920)}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024

jobs = {}  # id -> dict (в памяти + meta.json на диске)
jobs_lock = threading.Lock()
cpu_lock = threading.Lock()  # тяжёлые задачи выполняем по одной


# ---------------------------------------------------------------- хранение задач
def job_dir(job_id):
    if not job_id or not all(c in "0123456789abcdef" for c in job_id):
        abort(404)
    return os.path.join(WORKSPACE, job_id)


def save_meta(job):
    data = {k: v for k, v in job.items() if not k.startswith("_")}
    path = os.path.join(job_dir(job["id"]), "meta.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def get_job(job_id):
    with jobs_lock:
        if job_id in jobs:
            return jobs[job_id]
        path = os.path.join(job_dir(job_id), "meta.json")
        if not os.path.isfile(path):
            abort(404)
        with open(path, encoding="utf-8") as f:
            job = json.load(f)
        # задача, прерванная закрытием программы, не может продолжаться
        if job.get("status") in ("transcribing", "queued"):
            job.update(status="error", error="Распознавание было прервано. Загрузите файл заново.")
        if job.get("status") == "rendering":
            job.update(status="ready", render_status=None)
        jobs[job_id] = job
        return job


def update(job, **kw):
    with jobs_lock:
        job.update(kw)
        save_meta(job)


def cleanup_workspace():
    os.makedirs(WORKSPACE, exist_ok=True)
    limit = time.time() - KEEP_DAYS * 86400
    for name in os.listdir(WORKSPACE):
        p = os.path.join(WORKSPACE, name)
        if os.path.isdir(p) and os.path.getmtime(p) < limit:
            shutil.rmtree(p, ignore_errors=True)


# ---------------------------------------------------------------- фоновые задачи
def transcribe_worker(job):
    d = job_dir(job["id"])
    try:
        with cpu_lock:
            update(job, status="transcribing", progress=0.0, message="Подготовка…")
            segments = transcriber.transcribe(
                os.path.join(d, "asr.wav"), job["model"], job["language"],
                status_cb=lambda m: update(job, message=m),
                progress_cb=lambda p: job.update(progress=round(p, 3)),
            )
        update(job, status="ready", segments=segments, progress=1.0, message="Готово")
    except Exception as e:
        traceback.print_exc()
        update(job, status="error", error=str(e))


def render_worker(job, segments, out_name):
    d = job_dir(job["id"])
    try:
        with cpu_lock:
            update(job, render_status="rendering", render_progress=0.0, render_error=None)
            w, h = RESOLUTIONS.get(str(job["settings"].get("resolution")), RESOLUTIONS["1080"])
            settings = dict(job["settings"], width=w, height=h)
            tmp = os.path.join(d, "render.tmp.mp4")
            t0 = time.time()
            render_video(segments, job["duration"], os.path.join(d, "play.wav"), tmp, settings,
                         progress_cb=lambda p: job.update(render_progress=round(p, 3)))
            out = os.path.join(d, out_name)
            os.replace(tmp, out)
            print("Видео готово за %.1f с: %s" % (time.time() - t0, out))
        update(job, render_status="done", render_progress=1.0, video=out_name)
    except Exception as e:
        traceback.print_exc()
        update(job, render_status="error", render_error=str(e))


# ---------------------------------------------------------------- маршруты
@app.route("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.route("/static/<path:name>")
def static_files(name):
    return send_from_directory(STATIC, name)


@app.route("/api/config")
def config():
    return jsonify({
        "models": [dict(m, downloaded=transcriber.model_downloaded(m["id"])) for m in transcriber.MODELS],
    })


@app.route("/api/jobs")
def list_jobs():
    out = []
    if os.path.isdir(WORKSPACE):
        for name in os.listdir(WORKSPACE):
            p = os.path.join(WORKSPACE, name, "meta.json")
            if os.path.isfile(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        m = json.load(f)
                    out.append({k: m.get(k) for k in ("id", "filename", "created", "duration", "status", "video")})
                except Exception:
                    pass
    out.sort(key=lambda m: m.get("created") or 0, reverse=True)
    return jsonify(out[:20])


@app.route("/api/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="Файл не выбран"), 400
    job_id = uuid.uuid4().hex[:16]
    d = os.path.join(WORKSPACE, job_id)
    os.makedirs(d)
    ext = os.path.splitext(f.filename)[1].lower()[:10] or ".bin"
    src = os.path.join(d, "source" + ext)
    f.save(src)
    try:
        ch = min(2, audio_utils.input_channels(src))
        audio_utils.convert_to_wav(src, os.path.join(d, "play.wav"), 44100, ch)
        audio_utils.convert_to_wav(src, os.path.join(d, "asr.wav"), 16000, ch)
        duration = audio_utils.wav_duration(os.path.join(d, "play.wav"))
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        return jsonify(error=str(e)), 400
    if duration < 0.5:
        shutil.rmtree(d, ignore_errors=True)
        return jsonify(error="Аудиофайл пустой или слишком короткий"), 400
    job = {
        "id": job_id, "filename": f.filename, "created": time.time(), "duration": round(duration, 3),
        "channels": ch, "model": request.form.get("model", "small"),
        "language": request.form.get("language", "ru"),
        "status": "queued", "progress": 0.0, "message": "В очереди…", "segments": [],
        "settings": {}, "render_status": None,
    }
    with jobs_lock:
        jobs[job_id] = job
        save_meta(job)
    if request.form.get("skip_asr") == "1":
        update(job, status="ready", message="Без распознавания",
               segments=[{"speaker": "robot", "start": 0.0, "end": min(3.0, duration), "text": ""}])
    else:
        threading.Thread(target=transcribe_worker, args=(job,), daemon=True).start()
    return jsonify(id=job_id)


@app.route("/api/job/<job_id>")
def job_status(job_id):
    job = get_job(job_id)
    return jsonify({k: v for k, v in job.items() if not k.startswith("_")})


@app.route("/api/job/<job_id>/segments", methods=["PUT"])
def save_segments(job_id):
    job = get_job(job_id)
    data = request.get_json(force=True)
    update(job, segments=clean_segments(data.get("segments", []), job["duration"]),
           settings=data.get("settings", job.get("settings", {})))
    return jsonify(ok=True)


def clean_segments(segments, duration):
    out = []
    for s in segments:
        try:
            start = max(0.0, min(float(s.get("start", 0)), duration))
            end = max(start, min(float(s.get("end", start)), duration))
        except (TypeError, ValueError):
            continue
        out.append({"speaker": "client" if s.get("speaker") == "client" else "robot",
                    "start": round(start, 2), "end": round(end, 2), "text": str(s.get("text", ""))})
    out.sort(key=lambda s: s["start"])
    return out


@app.route("/api/job/<job_id>/audio")
def job_audio(job_id):
    return send_file(os.path.join(job_dir(job_id), "play.wav"), mimetype="audio/wav", conditional=True)


@app.route("/api/job/<job_id>/preview", methods=["POST"])
def preview(job_id):
    job = get_job(job_id)
    data = request.get_json(force=True)
    segs = clean_segments(data.get("segments", []), job["duration"])
    settings = dict(data.get("settings", {}), width=540, height=960)
    r = ChatRenderer(segs, job["duration"], os.path.join(job_dir(job_id), "play.wav"), settings)
    img = r.frame_at(float(data.get("t", 0)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/api/job/<job_id>/render", methods=["POST"])
def start_render(job_id):
    job = get_job(job_id)
    if job.get("render_status") == "rendering":
        return jsonify(error="Видео уже создаётся"), 409
    data = request.get_json(force=True)
    segs = [s for s in clean_segments(data.get("segments", []), job["duration"]) if s["text"].strip()]
    if not segs:
        return jsonify(error="Нет ни одной реплики с текстом"), 400
    update(job, segments=clean_segments(data.get("segments", []), job["duration"]),
           settings=data.get("settings", {}), render_status="rendering", render_progress=0.0)
    threading.Thread(target=render_worker, args=(job, segs, "video.mp4"), daemon=True).start()
    return jsonify(ok=True)


@app.route("/api/job/<job_id>/video")
def job_video(job_id):
    job = get_job(job_id)
    if not job.get("video"):
        abort(404)
    path = os.path.join(job_dir(job_id), job["video"])
    if not os.path.isfile(path):
        abort(404)
    base = os.path.splitext(os.path.basename(job["filename"]))[0] or "dialog"
    return send_file(path, mimetype="video/mp4", conditional=True,
                     as_attachment=request.args.get("download") == "1", download_name=base + "_chat.mp4")


@app.route("/api/job/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    d = job_dir(job_id)
    with jobs_lock:
        jobs.pop(job_id, None)
    shutil.rmtree(d, ignore_errors=True)
    return jsonify(ok=True)


# ---------------------------------------------------------------- запуск
def free_port(preferred=8765):
    for port in [preferred] + list(range(preferred + 1, preferred + 50)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def main():
    # убираем служебный шум Flask из консоли: оставляем только наши сообщения и ошибки
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    import flask.cli
    flask.cli.show_server_banner = lambda *a, **k: None
    cleanup_workspace()
    port = free_port()
    url = "http://127.0.0.1:%d/" % port
    print("=" * 60)
    print(" Аудио -> видео-чат: интерфейс открыт по адресу %s" % url)
    print(" Не закрывайте это окно, пока работаете с инструментом.")
    print("=" * 60)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
