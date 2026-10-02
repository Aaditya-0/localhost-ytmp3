"use strict";

const $ = (id) => document.getElementById(id);

const urlInput = $("urlInput");
const pasteBtn = $("pasteBtn");
const infoBtn = $("infoBtn");
const errorBox = $("errorBox");
const videoCard = $("videoCard");
const thumb = $("thumb");
const videoTitle = $("videoTitle");
const videoChannel = $("videoChannel");
const videoDuration = $("videoDuration");
const sizeInfo = $("sizeInfo");
const quality = $("quality");
const convertBtn = $("convertBtn");
const progressSection = $("progressSection");
const progressLabel = $("progressLabel");
const progressPercent = $("progressPercent");
const progressFill = $("progressFill");
const doneSection = $("doneSection");
const doneFilename = $("doneFilename");
const downloadBtn = $("downloadBtn");

let durationSeconds = 0;
let sourceSize = null;
let isConverting = false;
let pollTimer = null;

// ---------- helpers ----------

async function postJSON(url, payload) {
    const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    let data;
    try {
        data = await response.json();
    } catch {
        throw new Error("The server sent an unexpected response.");
    }
    if (!response.ok || !data.success) {
        throw new Error(data.error || "Something went wrong.");
    }
    return data;
}

function showError(message) {
    errorBox.textContent = message;
    errorBox.classList.remove("hidden");
}

function clearError() {
    errorBox.classList.add("hidden");
    errorBox.textContent = "";
}

function formatBytes(bytes) {
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function updateSizeInfo() {
    const parts = [];
    if (durationSeconds) {
        const estimate = (durationSeconds * Number(quality.value) * 1000) / 8;
        parts.push(`Estimated MP3 size: ~${formatBytes(estimate)}`);
    }
    if (sourceSize) {
        parts.push(`Source audio: ~${formatBytes(sourceSize)}`);
    }
    sizeInfo.textContent = parts.join(" · ");
}

function resetConversionUI() {
    progressSection.classList.add("hidden");
    doneSection.classList.add("hidden");
    convertBtn.classList.remove("hidden");
    convertBtn.disabled = false;
    stopPolling();
    isConverting = false;
}

function stopPolling() {
    if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
    }
}

function setProgress(label, percent) {
    progressLabel.textContent = label;
    if (typeof percent === "number") {
        progressFill.classList.remove("indeterminate");
        progressFill.style.width = percent + "%";
        progressPercent.textContent = Math.round(percent) + "%";
    } else {
        // No real percentage available: show a moving bar, not a fake number.
        progressFill.classList.add("indeterminate");
        progressFill.style.width = "";
        progressPercent.textContent = "";
    }
}

// ---------- actions ----------

async function pasteFromClipboard() {
    try {
        urlInput.value = (await navigator.clipboard.readText()).trim();
        urlInput.dispatchEvent(new Event("input"));
    } catch {
        showError("Could not read the clipboard. Please paste with Ctrl+V instead.");
    }
}

async function getVideoInfo() {
    clearError();
    resetConversionUI();
    videoCard.classList.add("hidden");

    const url = urlInput.value.trim();
    if (!url) {
        showError("Please paste a YouTube URL.");
        return;
    }

    infoBtn.disabled = true;
    infoBtn.textContent = "Fetching...";
    try {
        const data = await postJSON("/info", { url });
        videoTitle.textContent = data.title;
        videoChannel.textContent = data.channel;
        videoDuration.textContent = data.duration;
        thumb.src = data.thumbnail && data.thumbnail.startsWith("https://") ? data.thumbnail : "";
        durationSeconds = data.duration_seconds || 0;
        sourceSize = data.source_size || null;
        updateSizeInfo();
        videoCard.classList.remove("hidden");
    } catch (err) {
        showError(err.message || "Could not reach the local server.");
    } finally {
        infoBtn.disabled = false;
        infoBtn.textContent = "Get Video Info";
    }
}

async function startConversion() {
    if (isConverting) return; // prevent duplicate requests
    clearError();
    isConverting = true;
    convertBtn.disabled = true;
    doneSection.classList.add("hidden");
    progressSection.classList.remove("hidden");
    setProgress("Fetching video...", null);

    try {
        const data = await postJSON("/convert", {
            url: urlInput.value.trim(),
            quality: quality.value,
        });
        pollProgress(data.job_id, 0);
    } catch (err) {
        showError(err.message || "Could not reach the local server.");
        resetConversionUI();
    }
}

async function pollProgress(jobId, failures) {
    try {
        const response = await fetch(`/progress/${jobId}`);
        const data = await response.json();
        if (!response.ok || !data.success) {
            throw new Error(data.error || "Lost track of the conversion.");
        }

        if (data.status === "error") {
            showError(data.error || "Conversion failed.");
            resetConversionUI();
            return;
        }
        if (data.status === "done") {
            progressSection.classList.add("hidden");
            convertBtn.classList.add("hidden");
            doneFilename.textContent = data.filename;
            downloadBtn.href = `/download/${jobId}`;
            doneSection.classList.remove("hidden");
            isConverting = false;
            return;
        }

        setProgress(data.stage, data.percent);
        pollTimer = setTimeout(() => pollProgress(jobId, 0), 700);
    } catch (err) {
        if (failures >= 4) {
            showError(err.message || "Lost connection to the local server.");
            resetConversionUI();
            return;
        }
        pollTimer = setTimeout(() => pollProgress(jobId, failures + 1), 1000);
    }
}

// ---------- events ----------

pasteBtn.addEventListener("click", pasteFromClipboard);
infoBtn.addEventListener("click", getVideoInfo);
convertBtn.addEventListener("click", startConversion);
quality.addEventListener("change", updateSizeInfo);
urlInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") getVideoInfo();
});
urlInput.addEventListener("input", () => {
    // A different URL means the previous video card no longer applies.
    if (!isConverting) {
        videoCard.classList.add("hidden");
        resetConversionUI();
    }
});
