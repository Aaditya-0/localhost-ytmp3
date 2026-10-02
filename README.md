# YouTube → MP3 (local)

A small web app that runs only on your own computer. It uses **yt-dlp** to fetch the audio and **FFmpeg** to convert it to MP3. No third-party conversion websites or APIs are involved.

> Only download content you have permission to download, or that is otherwise legally downloadable. Downloading from YouTube may violate its Terms of Service for some content; you are responsible for how you use this tool.

## 1. Requirements

- **Python 3.9+** (https://www.python.org/downloads/ — tick "Add python.exe to PATH" during install)
- **FFmpeg** available in your system PATH (see section 5)

## 2. Installation

Open a terminal (PowerShell or Command Prompt) in the project folder:

```bash
pip install -r requirements.txt
```

Optional but recommended: use a virtual environment first.

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Running

```bash
python app.py
```

## 4. Opening the application

Open this address in your browser:

```text
http://127.0.0.1:5000
```

Stop the app with `Ctrl+C` in the terminal.

## How it works

1. Paste a YouTube URL and click **Get Video Info** (metadata only, nothing is downloaded yet).
2. Pick a quality (default 192 kbps) and click **Convert to MP3**.
3. A real progress bar shows the audio download. During the FFmpeg conversion no honest percentage exists, so a moving bar is shown instead.
4. Click **Download MP3**. The file is named after the video title.

Temporary files live in `downloads/` (one sub-folder per conversion). They are deleted about 2 minutes after you download them, or 30 minutes after conversion if you never download. Leftovers from earlier runs are removed on startup.

Note: the thumbnail is loaded by your browser directly from YouTube's image server. No audio or video data goes anywhere except from YouTube to your PC.

## 5. FFmpeg setup on Windows

**Option A – winget (easiest, Windows 10/11):**

```powershell
winget install Gyan.FFmpeg
```

Close and reopen your terminal afterwards.

**Option B – manual:**

1. Download a build from https://www.gyan.dev/ffmpeg/builds/ (the "release essentials" zip is enough).
2. Extract it to `C:\ffmpeg` so that `C:\ffmpeg\bin\ffmpeg.exe` exists.
3. Press **Win**, search for **"Edit the system environment variables"**, click **Environment Variables**.
4. Under *System variables* select **Path** → **Edit** → **New** → enter `C:\ffmpeg\bin` → **OK**.
5. Close and reopen your terminal.

Verify:

```bash
ffmpeg -version
```

If a version number is printed, FFmpeg is ready. Restart `python app.py` afterwards.

## 6. Troubleshooting

**"FFmpeg was not found"**
FFmpeg isn't in PATH. Re-check section 5, make sure you opened a *new* terminal after changing PATH, and that `ffmpeg -version` works in that same terminal.

**yt-dlp errors (HTTP 403, "Unable to extract", "Sign in to confirm you're not a bot")**
YouTube changes often and yt-dlp must keep up. Update it first:

```bash
pip install -U yt-dlp
```

Then restart the app. If YouTube still asks for verification, sign in to YouTube in a supported browser and start the app with browser cookies enabled. In PowerShell, for Chrome:

```powershell
$env:YTDLP_COOKIES_FROM_BROWSER = "chrome"
python app.py
```

Use `edge` or `firefox` if that is the browser where you are signed in. The setting is optional and applies to both video info and downloads. Browser cookies are sensitive; keep them on your own computer and unset the setting when you no longer need it (`Remove-Item Env:YTDLP_COOKIES_FROM_BROWSER`).

**Video unavailable / private / age-restricted / members-only / region-blocked**
These cannot be downloaded without being signed in or having access. Try a different video.

**Conversion fails**
- Run `ffmpeg -version` to confirm FFmpeg works.
- Update yt-dlp (above).
- Make sure the disk has free space.
- Look at the terminal where `python app.py` runs; the real error is logged there.

**Permission errors**
- Don't put the project in a protected folder such as `C:\Program Files`. Use a folder inside your user directory (e.g. Documents).
- Close any program that has an MP3 from `downloads/` open.
- Check that antivirus software isn't blocking the `downloads` folder.

**`python` or `pip` is not recognized**
Reinstall Python with "Add python.exe to PATH" ticked, or use `py app.py` and `py -m pip install -r requirements.txt`.

**Port 5000 already in use**
Another program uses it. Close it, or change the `port=5000` value at the bottom of `app.py` (and the address you open).

## Project structure

```text
youtube-mp3-converter/
├── app.py              Flask backend (routes, yt-dlp jobs, cleanup)
├── requirements.txt
├── README.md
├── .gitignore
├── downloads/          temporary MP3 files (created automatically)
├── templates/index.html
└── static/
    ├── style.css
    └── script.js
```

## API

| Method | Path                  | Purpose                                   |
|--------|-----------------------|-------------------------------------------|
| GET    | `/`                   | The web page                              |
| POST   | `/info`               | `{url}` → title, channel, duration, thumbnail |
| POST   | `/convert`            | `{url, quality}` → `{job_id}`             |
| GET    | `/progress/<job_id>`  | status, stage text, real percent if known |
| GET    | `/download/<job_id>`  | the MP3 file                              |
