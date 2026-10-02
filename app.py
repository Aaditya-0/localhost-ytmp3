"""Local YouTube -> MP3 converter.

Flask serves a small web page; yt-dlp downloads the audio and FFmpeg
(called by yt-dlp) converts it to MP3. Everything runs on this computer.
"""

import os
import re
import shutil
import threading
import time
import uuid
from urllib.parse import parse_qs, urlparse

import yt_dlp
from flask import Flask, jsonify, render_template, request, send_file
from yt_dlp.utils import DownloadError

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")

ALLOWED_BITRATES = {"128", "192", "256", "320"}
DEFAULT_BITRATE = "192"

# Optional browser authentication for YouTube verification challenges. This is
# deliberately opt-in because browser cookies grant access to the signed-in
# account. Set YTDLP_COOKIES_FROM_BROWSER to a browser name (for example,
# "chrome", "edge", or "firefox") before starting the app.
COOKIES_FROM_BROWSER = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip()

FINISHED_FILE_TTL = 30 * 60      # delete a finished file nobody downloaded after 30 min
DOWNLOADED_FILE_GRACE = 2 * 60   # delete 2 min after a download completes
CLEANUP_INTERVAL = 60            # how often the cleanup thread runs (seconds)
STUCK_DOWNLOAD_TIMEOUT = 60 * 60  # ignore a "download in progress" flag after 1 hour

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

app = Flask(__name__)

# job_id -> dict with the state of one conversion. Protected by JOBS_LOCK.
JOBS = {}
JOBS_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def check_ffmpeg():
    """True if the ffmpeg executable can be found on PATH."""
    return shutil.which("ffmpeg") is not None


def extract_video_id(raw_url):
    """Return the 11-character video ID of a YouTube URL, or None if invalid."""
    raw_url = (raw_url or "").strip()
    if not raw_url:
        return None
    if "://" not in raw_url:
        raw_url = "https://" + raw_url

    try:
        parsed = urlparse(raw_url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None

    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    candidate = None

    if host in SHORT_HOSTS and parts:
        candidate = parts[0]
    elif host in YOUTUBE_HOSTS and parts:
        if parts[0] == "watch":
            candidate = (parse_qs(parsed.query).get("v") or [None])[0]
        elif parts[0] in ("shorts", "live", "embed") and len(parts) >= 2:
            candidate = parts[1]

    if candidate and VIDEO_ID_RE.match(candidate):
        return candidate
    return None


def canonical_url(video_id):
    """Rebuild a clean URL from the ID so only a known-safe URL reaches yt-dlp."""
    return f"https://www.youtube.com/watch?v={video_id}"


def format_duration(seconds):
    if not seconds:
        return "Unknown"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def sanitize_filename(title, max_length=150):
    """Make a title safe to use as a Windows filename."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title or "")
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
    if not name or name.split(".")[0].upper() in reserved:
        name = "audio"
    return name[:max_length].rstrip(". ")


def clean_error_message(exc):
    """Turn a yt-dlp/network exception into a short, friendly message."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
    lowered = text.lower()

    rules = [
        (("private video",), "This video is private and cannot be downloaded."),
        (("video unavailable", "this video is not available", "has been removed",
          "account associated with this video has been terminated"),
         "This video is unavailable."),
        (("sign in to confirm your age", "age-restricted", "age restricted"),
         "This video is age-restricted and cannot be downloaded without signing in."),
        (("members-only", "members only", "join this channel"),
         "This video is for channel members only."),
        (("not available in your country", "blocked it in your country", "geo"),
         "This video is not available in your country."),
        (("live event", "is live", "premieres in"),
         "Live streams and upcoming premieres cannot be converted."),
        (("ffmpeg", "ffprobe"),
         "FFmpeg was not found or failed. Make sure FFmpeg is installed and in your PATH."),
        (("getaddrinfo", "urlopen error", "certificate verify failed", "ssl:", "timed out", "connection reset",
          "unable to download webpage", "network is unreachable", "temporary failure"),
         "Network problem. Check your internet connection and try again."),
        (("http error 403",),
         "YouTube refused the download (HTTP 403). Try updating yt-dlp: pip install -U yt-dlp"),
        (("sign in to confirm you", "not a bot"),
         "YouTube is asking for verification. Update yt-dlp, or sign in to YouTube in your browser and restart this app with YTDLP_COOKIES_FROM_BROWSER set to chrome, edge, or firefox."),
    ]
    for needles, message in rules:
        if any(n in lowered for n in needles):
            return message
    return "Could not process this video. Try updating yt-dlp (pip install -U yt-dlp)."


def json_error(message, status=400):
    return jsonify({"success": False, "error": message}), status


# --------------------------------------------------------------------------
# yt-dlp work
# --------------------------------------------------------------------------

def fetch_video_info(video_url):
    """Read video metadata without downloading the media."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
        "format": "bestaudio/best",
        "ignore_no_formats_error": True,
    }
    if COOKIES_FROM_BROWSER:
        options["cookiesfrombrowser"] = (COOKIES_FROM_BROWSER, None, None, None)
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(video_url, download=False)

    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise DownloadError("This is a live event")

    return {
        "success": True,
        "title": info.get("title") or "Untitled",
        "channel": info.get("uploader") or info.get("channel") or "Unknown",
        "duration": format_duration(info.get("duration")),
        "duration_seconds": info.get("duration") or 0,
        "thumbnail": info.get("thumbnail") or "",
        # Size of the original audio stream, when YouTube reports it.
        "source_size": info.get("filesize") or info.get("filesize_approx"),
    }


def update_job(job_id, **fields):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)


def run_conversion(job_id, video_url, bitrate):
    """Background thread: download audio and convert it to MP3."""
    job_dir = os.path.join(DOWNLOADS_DIR, job_id)

    def on_download_progress(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            percent = round(done / total * 100, 1) if total else None
            update_job(job_id, stage="Downloading audio...", percent=percent)
        elif d["status"] == "finished":
            update_job(job_id, stage="Converting to MP3...", percent=None)

    def on_postprocess(d):
        if d["status"] == "started":
            update_job(job_id, stage="Converting to MP3...", percent=None)
        elif d["status"] == "finished":
            update_job(job_id, stage="Almost done...", percent=None)

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": os.path.join(job_dir, "audio.%(ext)s"),
        "socket_timeout": 20,
        "retries": 5,
        "progress_hooks": [on_download_progress],
        "postprocessor_hooks": [on_postprocess],
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": bitrate,
        }],
    }
    if COOKIES_FROM_BROWSER:
        options["cookiesfrombrowser"] = (COOKIES_FROM_BROWSER, None, None, None)

    try:
        os.makedirs(job_dir, exist_ok=True)
        update_job(job_id, status="running", stage="Fetching video...", percent=None)

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(video_url, download=True)

        mp3_path = os.path.join(job_dir, "audio.mp3")
        if not os.path.isfile(mp3_path):
            raise RuntimeError("MP3 file was not created")

        update_job(
            job_id,
            status="done",
            stage="Conversion complete",
            percent=100,
            file_path=mp3_path,
            filename=sanitize_filename(info.get("title")) + ".mp3",
            finished_at=time.time(),
        )
    except Exception as exc:  # noqa: BLE001 - we show a friendly message instead
        app.logger.error("Conversion failed for job %s: %s", job_id, exc)
        shutil.rmtree(job_dir, ignore_errors=True)
        message = clean_error_message(exc) if isinstance(exc, DownloadError) else (
            "Conversion failed. Please try again."
            if not isinstance(exc, OSError) else
            "Could not write the file. Check folder permissions and free disk space."
        )
        update_job(job_id, status="error", stage="Failed", error=message, finished_at=time.time())


# --------------------------------------------------------------------------
# Cleanup of temporary files
# --------------------------------------------------------------------------

def cleanup_old_jobs():
    """Delete files of finished jobs that were downloaded or abandoned."""
    now = time.time()
    expired = []
    with JOBS_LOCK:
        for job_id, job in JOBS.items():
            if job["status"] not in ("done", "error"):
                continue  # still working
            # A download in progress blocks cleanup (with a safety timeout in
            # case a browser vanished without closing the connection cleanly).
            if job["active_downloads"] > 0 and now - job["last_download_started"] < STUCK_DOWNLOAD_TIMEOUT:
                continue
            downloaded_at = job["downloaded_at"]
            if downloaded_at and now - downloaded_at > DOWNLOADED_FILE_GRACE:
                expired.append(job_id)
            elif now - job["finished_at"] > FINISHED_FILE_TTL:
                expired.append(job_id)
        for job_id in expired:
            del JOBS[job_id]

    for job_id in expired:
        shutil.rmtree(os.path.join(DOWNLOADS_DIR, job_id), ignore_errors=True)


def cleanup_loop():
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            cleanup_old_jobs()
        except Exception as exc:  # noqa: BLE001
            app.logger.error("Cleanup error: %s", exc)


def clear_leftovers():
    """On startup remove job folders left over from a previous run."""
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    for entry in os.listdir(DOWNLOADS_DIR):
        path = os.path.join(DOWNLOADS_DIR, entry)
        if JOB_ID_RE.match(entry) and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", ffmpeg_ok=check_ffmpeg())


@app.route("/info", methods=["POST"])
def info():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    raw_url = data.get("url")
    if not isinstance(raw_url, str):
        raw_url = ""
    raw_url = raw_url.strip()
    if not raw_url:
        return json_error("Please paste a YouTube URL.")

    video_id = extract_video_id(raw_url)
    if not video_id:
        return json_error("That doesn't look like a valid YouTube video URL.")

    try:
        return jsonify(fetch_video_info(canonical_url(video_id)))
    except DownloadError as exc:
        return json_error(clean_error_message(exc), 422)
    except Exception as exc:  # noqa: BLE001
        app.logger.error("Info failed: %s", exc)
        return json_error("Could not read video information. Please try again.", 500)


@app.route("/convert", methods=["POST"])
def convert():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    raw_url = data.get("url")
    if not isinstance(raw_url, str):
        raw_url = ""
    raw_url = raw_url.strip()
    bitrate = str(data.get("quality") or DEFAULT_BITRATE)

    if not raw_url:
        return json_error("Please paste a YouTube URL.")
    video_id = extract_video_id(raw_url)
    if not video_id:
        return json_error("That doesn't look like a valid YouTube video URL.")
    if bitrate not in ALLOWED_BITRATES:
        return json_error("Invalid audio quality.")
    if not check_ffmpeg():
        return json_error(
            "FFmpeg was not found. Install FFmpeg and make sure it is in your "
            "system PATH (check with: ffmpeg -version).", 503)

    with JOBS_LOCK:
        # Prevent duplicate conversions of the same video + quality.
        for existing_id, job in JOBS.items():
            if (job["video_id"] == video_id and job["bitrate"] == bitrate
                    and job["status"] in ("queued", "running")):
                return jsonify({"success": True, "job_id": existing_id})

        job_id = uuid.uuid4().hex
        JOBS[job_id] = {
            "video_id": video_id,
            "bitrate": bitrate,
            "status": "queued",
            "stage": "Fetching video...",
            "percent": None,
            "error": None,
            "file_path": None,
            "filename": None,
            "finished_at": 0,
            "downloaded_at": None,
            "active_downloads": 0,
            "last_download_started": 0,
        }

    thread = threading.Thread(
        target=run_conversion,
        args=(job_id, canonical_url(video_id), bitrate),
        daemon=True,
    )
    thread.start()
    return jsonify({"success": True, "job_id": job_id})


@app.route("/progress/<job_id>")
def progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id) if JOB_ID_RE.match(job_id) else None
        if job is None:
            return json_error("Unknown or expired job.", 404)
        return jsonify({
            "success": True,
            "status": job["status"],
            "stage": job["stage"],
            "percent": job["percent"],
            "filename": job["filename"],
            "error": job["error"],
        })


@app.route("/download/<job_id>")
def download(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id) if JOB_ID_RE.match(job_id) else None
        if job is None:
            return json_error("Unknown or expired job.", 404)
        if job["status"] != "done" or not job["file_path"] or not os.path.isfile(job["file_path"]):
            return json_error("The file is not ready.", 409)
        job["active_downloads"] += 1  # blocks cleanup while sending
        job["last_download_started"] = time.time()
        file_path, filename = job["file_path"], job["filename"]

    def finished_sending():
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["active_downloads"] -= 1
                JOBS[job_id]["downloaded_at"] = time.time()

    try:
        response = send_file(file_path, mimetype="audio/mpeg",
                             as_attachment=True, download_name=filename)
    except Exception:
        finished_sending()
        raise
    # send_file() uses "direct passthrough", which makes Werkzeug skip close
    # callbacks. Turn it off so finished_sending() really runs after the transfer.
    response.direct_passthrough = False
    response.call_on_close(finished_sending)
    return response


# --------------------------------------------------------------------------
# Start-up
# --------------------------------------------------------------------------

if __name__ == "__main__":
    clear_leftovers()
    threading.Thread(target=cleanup_loop, daemon=True).start()

    if not check_ffmpeg():
        print("\n" + "=" * 60)
        print("WARNING: FFmpeg was not found.")
        print("Please install FFmpeg and make sure it is available in your")
        print("system PATH. Verify with:  ffmpeg -version")
        print("=" * 60 + "\n")

    print("Open http://127.0.0.1:5000 in your browser")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
