#!/usr/bin/env python3
"""
VidDrop Backend — Python Flask + yt-dlp
Kurulum: pip install flask yt-dlp flask-cors
Çalıştır: python server.py
"""

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os
import uuid
import threading
import time

app = Flask(__name__)
CORS(app)  # HTML dosyasından erişim için

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# İndirme durumlarını takip etmek için
jobs = {}  # job_id -> { status, progress, title, filename, error }


# ─── Yardımcı fonksiyonlar ───────────────────────────────────────────────────

def quality_to_format(quality):
    formats = {
        "4K":    "bestvideo[height<=2160]+bestaudio/best[height<=2160]",
        "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "720p":  "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "480p":  "bestvideo[height<=480]+bestaudio/best[height<=480]",
        "360p":  "bestvideo[height<=360]+bestaudio/best[height<=360]",
        "MP3":   "bestaudio/best",
    }
    return formats.get(quality, "best")


def progress_hook(job_id):
    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                jobs[job_id]["progress"] = round(downloaded / total * 100, 1)
            jobs[job_id]["status"] = "downloading"
            jobs[job_id]["speed"] = d.get("_speed_str", "")
            jobs[job_id]["eta"] = d.get("_eta_str", "")
        elif d["status"] == "finished":
            jobs[job_id]["progress"] = 99
            jobs[job_id]["status"] = "processing"
    return hook


def do_download(job_id, url, quality):
    filename_base = os.path.join(DOWNLOAD_DIR, job_id)

    ydl_opts = {
        "format": quality_to_format(quality),
        "outtmpl": filename_base + ".%(ext)s",
        "progress_hooks": [progress_hook(job_id)],
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }

    # MP3 ise ses çıkart
    if quality == "MP3":
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
        ydl_opts["outtmpl"] = filename_base + ".%(ext)s"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "video")
            ext = "mp3" if quality == "MP3" else "mp4"
            final_file = filename_base + f".{ext}"

            jobs[job_id].update({
                "status": "done",
                "progress": 100,
                "title": title,
                "filename": final_file,
                "ext": ext,
            })
    except Exception as e:
        jobs[job_id].update({
            "status": "error",
            "error": str(e),
        })


# ─── API Endpoint'leri ───────────────────────────────────────────────────────

@app.route("/info", methods=["POST"])
def get_info():
    """Video bilgisini döner (indirmeden)"""
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL gerekli"}), 400

    try:
        ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            duration_sec = info.get("duration", 0)
            mins = duration_sec // 60
            secs = duration_sec % 60

            return jsonify({
                "title":     info.get("title", "Başlıksız"),
                "duration":  f"{mins}:{secs:02d}",
                "thumbnail": info.get("thumbnail", ""),
                "uploader":  info.get("uploader", ""),
                "platform":  info.get("extractor_key", ""),
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/download", methods=["POST"])
def start_download():
    """İndirmeyi başlatır, job_id döner"""
    data = request.get_json()
    url     = data.get("url", "").strip()
    quality = data.get("quality", "1080p")

    if not url:
        return jsonify({"error": "URL gerekli"}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "starting",
        "progress": 0,
        "title": "",
        "filename": "",
        "error": "",
    }

    thread = threading.Thread(target=do_download, args=(job_id, url, quality), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>", methods=["GET"])
def get_status(job_id):
    """İndirme durumunu döner"""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "İş bulunamadı"}), 404
    return jsonify(job)


@app.route("/file/<job_id>", methods=["GET"])
def get_file(job_id):
    """Tamamlanan dosyayı indirir"""
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Dosya hazır değil"}), 404

    filepath = job["filename"]
    if not os.path.exists(filepath):
        return jsonify({"error": "Dosya bulunamadı"}), 404

    title = job.get("title", "video")
    ext   = job.get("ext", "mp4")
    safe_title = "".join(c for c in title if c.isalnum() or c in " -_")[:60]

    return send_file(
        filepath,
        as_attachment=True,
        download_name=f"{safe_title}.{ext}",
    )


@app.route("/cleanup/<job_id>", methods=["DELETE"])
def cleanup(job_id):
    """İndirilen dosyayı ve job'u temizler"""
    job = jobs.get(job_id)
    if job:
        filepath = job.get("filename", "")
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
        del jobs[job_id]
    return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "version": "1.0"})


# ─── Otomatik temizlik (1 saat sonra dosyaları sil) ──────────────────────────

def auto_cleanup():
    while True:
        time.sleep(3600)
        for jid in list(jobs.keys()):
            filepath = jobs[jid].get("filename", "")
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
        jobs.clear()

threading.Thread(target=auto_cleanup, daemon=True).start()


if __name__ == "__main__":
    print("─" * 40)
    print("  VidDrop Backend başlatılıyor...")
    print("  http://localhost:5000")
    print("─" * 40)
    app.run(host="0.0.0.0", port=5000, debug=False)
