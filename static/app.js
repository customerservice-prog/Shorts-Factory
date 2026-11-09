(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let accessKey = "";
  let config = null;
  let jobs = [];
  let activeJobId = null;
  let selectedJobId = null;
  let layout = "blur";
  let currentPreview = null;
  let toastTimer = null;
  let sourcePreviewUrl = null;

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[char]);

  function toast(message) {
    const bar = $("toast");
    bar.textContent = message;
    bar.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => bar.classList.add("hidden"), 6500);
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (accessKey) headers.set("X-Access-Key", accessKey);
    const response = await fetch(path, { ...options, headers, cache: "no-store" });
    if (!response.ok) {
      let message = "Request failed (" + response.status + ")";
      try {
        const body = await response.json();
        message = typeof body.detail === "string" ? body.detail : message;
      } catch (_) {}
      throw new Error(message);
    }
    return response;
  }

  const presets = {
    quick: { count: 6, length: "20", captions: "pop" },
    balanced: { count: 5, length: "45", captions: "pop" },
    story: { count: 3, length: "60", captions: "classic" }
  };

  function updateSummary() {
    const count = Number($("clipCount").value);
    const length = Number($("clipLength").value);
    $("clipCountText").textContent = count + (count === 1 ? " clip" : " clips");
    $("outputSummary").textContent = count + " vertical " + (count === 1 ? "clip" : "clips") + " · ~" + length + "s each";
    const matched = Object.entries(presets).find(([, p]) => p.count === count &&
      Number(p.length) === length && p.captions === $("captions").value);
    document.querySelectorAll("[data-preset]").forEach((button) => {
      const selected = !!matched && button.dataset.preset === matched[0];
      button.classList.toggle("selected", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
  }

  function setFile(file) {
    if (!file) return;
    const allowed = /\.(mp4|mov|mkv|webm|m4v)$/i.test(file.name);
    if (!allowed) {
      $("videoFile").value = "";
      return toast("Choose MP4, MOV, MKV, WEBM or M4V video.");
    }
    if (!file.size) {
      $("videoFile").value = "";
      return toast("That file is empty. Choose a different video.");
    }
    if (config && file.size > config.max_upload_mb * 1024 * 1024) {
      $("videoFile").value = "";
      return toast("That video is over the " + config.max_upload_mb + " MB limit.");
    }
    const dt = new DataTransfer();
    dt.items.add(file);
    $("videoFile").files = dt.files;
    if (sourcePreviewUrl) URL.revokeObjectURL(sourcePreviewUrl);
    sourcePreviewUrl = URL.createObjectURL(file);
    const preview = $("sourcePreview");
    preview.pause();
    preview.src = sourcePreviewUrl;
    preview.onloadedmetadata = () => {
      if (!Number.isFinite(preview.duration)) return;
      const seconds = Math.round(preview.duration);
      const duration = Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0");
      $("sourceMeta").textContent = duration + " min:sec · " + (file.size / 1048576).toFixed(1) + " MB";
    };
    preview.onerror = () => {
      $("sourceMeta").textContent = (file.size / 1048576).toFixed(1) + " MB · Preview unavailable (file may still process)";
    };
    $("sourceName").textContent = file.name;
    $("sourceMeta").textContent = (file.size / 1048576).toFixed(1) + " MB · Checking preview…";
    $("sourceCard").classList.remove("hidden");
    $("dropzone").classList.add("hidden");
    $("generateBtn").disabled = false;
    $("generateText").textContent = "Generate my shorts";
    updateSummary();
  }

  function setupUploads() {
    $("browseBtn").addEventListener("click", (e) => { e.stopPropagation(); $("videoFile").click(); });
    $("dropzone").addEventListener("click", (e) => { if (!e.target.closest("button")) $("videoFile").click(); });
    $("dropzone").addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("videoFile").click(); }
    });
    $("videoFile").addEventListener("change", (e) => setFile(e.target.files[0]));
    $("replaceBtn").addEventListener("click", () => $("videoFile").click());
    document.querySelectorAll("[data-preset]").forEach((button) => {
      button.addEventListener("click", () => {
        const recipe = presets[button.dataset.preset];
        if (!recipe) return;
        $("clipCount").value = String(recipe.count);
        $("clipLength").value = recipe.length;
        $("captions").value = recipe.captions;
        updateSummary();
      });
    });
    $("clipLength").addEventListener("change", updateSummary);
    $("captions").addEventListener("change", updateSummary);
    for (const eventName of ["dragenter", "dragover"]) {
      $("dropzone").addEventListener(eventName, (e) => {
        e.preventDefault(); e.stopPropagation(); $("dropzone").classList.add("dragging");
      });
    }
    $("dropzone").addEventListener("dragleave", (e) => {
      e.preventDefault(); $("dropzone").classList.remove("dragging");
    });
    $("dropzone").addEventListener("drop", (e) => {
      e.preventDefault(); e.stopPropagation();
      setFile(e.dataTransfer.files[0]);
    });
    $("logoPick").addEventListener("click", () => $("logoFile").click());
    $("logoFile").addEventListener("change", (e) => {
      const file = e.target.files[0];
      if (!file) return;
      if (!/\.(png|jpe?g|webp)$/i.test(file.name)) {
        $("logoFile").value = "";
        return toast("Use a PNG, JPG or WEBP image for your logo.");
      }
      $("logoLabel").textContent = file.name;
      const image = document.createElement("img");
      image.alt = "Logo selected";
      image.src = URL.createObjectURL(file);
      image.onload = () => URL.revokeObjectURL(image.src);
      $("logoSymbol").replaceChildren(image);
    });
    $("clipCount").addEventListener("input", updateSummary);
    updateSummary();
    document.querySelectorAll("[data-layout]").forEach((button) => {
      button.addEventListener("click", () => {
        layout = button.dataset.layout;
        document.querySelectorAll("[data-layout]").forEach((b) => b.classList.toggle("active", b === button));
      });
    });
    $("createForm").addEventListener("submit", create);
  }

  async function create(event) {
    event.preventDefault();
    const video = $("videoFile").files[0];
    if (!video) return toast("Please choose a video first.");
    if (config?.needs_setup) return toast("Set APP_PASSWORD in your server environment first.");
    const form = new FormData();
    form.append("video", video);
    if ($("logoFile").files[0]) form.append("logo", $("logoFile").files[0]);
    form.append("clip_count", $("clipCount").value);
    form.append("clip_length", $("clipLength").value);
    form.append("layout", layout);
    form.append("captions", $("captions").value);
    form.append("brand_name", $("brandName").value);
    form.append("rights", $("rights").value);
    const button = $("generateBtn");
    const old = button.innerHTML;
    button.disabled = true;
    button.textContent = "Uploading video...";
    $("activePanel").classList.remove("hidden");
    $("results").classList.add("hidden");
    $("progressHeadline").textContent = "Uploading source video...";
    $("progressNote").textContent = "Do not close this page until upload completes.";
    $("progressPercent").textContent = "0%";
    $("progressFill").style.width = "2%";
    try {
      const response = await api("/api/jobs", { method: "POST", body: form });
      const job = await response.json();
      activeJobId = job.id;
      selectedJobId = job.id;
      toast("Video uploaded. Your processing job has started.");
      await refreshJobs();
      $("activePanel").scrollIntoView({ behavior: "smooth", block: "center" });
    } catch (error) {
      $("activePanel").classList.add("hidden");
      toast(error.message);
    } finally {
      button.disabled = !$("videoFile").files.length;
      button.innerHTML = old;
    }
  }

  function statusLabel(status) {
    return ({
      queued: "Queued", analyzing: "Analyzing", rendering: "Rendering",
      completed: "Complete", failed: "Failed", interrupted: "Interrupted"
    })[status] || "Unknown";
  }

  function renderProgress(job) {
    if (!job) {
      $("activePanel").classList.add("hidden");
      return;
    }
    $("activePanel").classList.remove("hidden");
    const done = job.status === "completed";
    $("progressHeadline").textContent = done ? "All shorts are ready" : job.status === "failed" ? "Processing failed" : "Making your shorts";
    $("progressNote").textContent = job.note || "Processing...";
    $("progressPercent").textContent = (job.progress || 0) + "%";
    $("progressFill").style.width = (job.progress || 0) + "%";
    ["step1", "step2", "step3", "step4"].forEach((id, index) => {
      $(id).classList.toggle("reached", (job.progress || 0) >= [2, 12, 40, 96][index]);
    });
  }

  function historyMarkup(job) {
    const date = new Date((job.created_at || 0) * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    const canDelete = !["queued", "analyzing", "rendering"].includes(job.status);
    return '<article class="history-item"><span class="history-file-icon">▶</span>' +
      '<div class="history-details"><strong title="' + esc(job.filename) + '">' + esc(job.filename) + '</strong>' +
      '<p>' + esc(date) + ' · ' + esc(job.clip_count) + ' requested · ' + esc(job.clip_length) + ' sec</p></div>' +
      '<span class="status-tag ' + esc(job.status) + '">' + esc(statusLabel(job.status)) + '</span>' +
      '<button type="button" data-view="' + esc(job.id) + '">' + (job.status === "completed" ? "View clips" : "Details") + '</button>' +
      (canDelete ? '<button type="button" class="delete-btn" data-delete="' + esc(job.id) + '" aria-label="Delete job">×</button>' : "") +
      '</article>';
  }

  function renderHistory() {
    const html = jobs.length ? jobs.map(historyMarkup).join("") : '<div class="empty-history">Your first video will appear here.</div>';
    $("historyList").innerHTML = html;
    $("libraryList").innerHTML = html;
    const complete = jobs.filter((j) => j.status === "completed");
    if ($("statsVideos")) $("statsVideos").textContent = complete.length;
    if ($("statsClips")) $("statsClips").textContent = complete.reduce((sum, j) => sum + (j.clips || []).length, 0);
    $("navCount").textContent = jobs.length;
  }

  function renderResults(job) {
    if (!job || job.status !== "completed") {
      $("results").classList.add("hidden");
      return;
    }
    selectedJobId = job.id;
    $("results").classList.remove("hidden");
    $("warnings").innerHTML = (job.warnings || []).map((w) => '<div class="warning">' + esc(w) + '</div>').join("");
    const clips = job.clips || [];
    $("clipsGrid").innerHTML = clips.map((clip, index) => (
      '<article class="clip-card"><div class="clip-thumb"><span>▶</span><small>9 : 16 VERTICAL</small></div>' +
      '<div class="clip-info"><div class="clip-number">SHORT ' + String(index + 1).padStart(2, "0") + '</div>' +
      '<h3 title="' + esc(clip.title) + '">' + esc(clip.title) + '</h3>' +
      '<div class="clip-meta"><span>' + esc(clip.length) + ' sec · ' + (clip.captions ? "Captioned" : "No captions") +
      '</span><span class="score">' + esc(clip.highlight_score) + '/100 score</span></div>' +
      '<div class="clip-buttons"><button type="button" data-preview="' + esc(clip.file) + '">▶ Preview</button>' +
      '<button class="download" type="button" data-download="' + esc(clip.file) + '">↓ Export</button></div></div></article>'
    )).join("");
  }

  async function refreshJobs() {
    try {
      const response = await api("/api/jobs");
      jobs = (await response.json()).jobs || [];
      $("systemState").textContent = "Engine ready";
      renderHistory();
      const working = jobs.find((j) => ["queued", "analyzing", "rendering"].includes(j.status));
      if (working) activeJobId = working.id;
      if (activeJobId) renderProgress(jobs.find((j) => j.id === activeJobId));
      const selected = jobs.find((j) => j.id === selectedJobId);
      if (selected && selected.status === "completed") renderResults(selected);
      else if (!selectedJobId) {
        const latest = jobs.find((j) => j.status === "completed");
        if (latest) renderResults(latest);
      }
    } catch (error) {
      $("systemState").textContent = "Connection issue";
      if (error.message.includes("401")) {
        $("authModal").classList.remove("hidden");
      }
      throw error;
    }
  }

  async function fileBlob(jobId, filename) {
    const response = await api("/api/jobs/" + encodeURIComponent(jobId) + "/files/" + encodeURIComponent(filename));
    return response.blob();
  }

  async function download(jobId, file) {
    try {
      toast("Preparing your download...");
      const blob = await fileBlob(jobId, file);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = file;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 120000);
    } catch (error) { toast("Download error: " + error.message); }
  }

  async function preview(jobId, file) {
    try {
      toast("Loading video preview...");
      const blob = await fileBlob(jobId, file);
      const url = URL.createObjectURL(blob);
      if (currentPreview) currentPreview();
      const backdrop = document.createElement("div");
      backdrop.className = "modal-backdrop";
      backdrop.style.zIndex = "60";
      const frame = document.createElement("div");
      frame.style.cssText = "position:relative;display:flex;flex-direction:column;align-items:center;gap:10px;width:min(400px,96vw);background:#111620;padding:17px;border:1px solid #344037;border-radius:15px";
      const close = document.createElement("button");
      close.textContent = "✕ Close preview";
      close.style.cssText = "align-self:flex-end;border:1px solid #47525f;border-radius:8px;background:#252e3b;color:white;padding:8px 12px";
      const video = document.createElement("video");
      video.controls = true;
      video.autoplay = true;
      video.playsInline = true;
      video.src = url;
      video.style.cssText = "width:auto;max-width:100%;height:min(70vh,650px);aspect-ratio:9/16;background:black;border-radius:10px";
      function finish() {
        video.pause(); video.removeAttribute("src"); video.load();
        backdrop.remove(); URL.revokeObjectURL(url); currentPreview = null;
      }
      currentPreview = finish;
      close.addEventListener("click", finish);
      backdrop.addEventListener("click", (e) => { if (e.target === backdrop) finish(); });
      frame.append(close, video);
      backdrop.appendChild(frame);
      document.body.appendChild(backdrop);
    } catch (error) { toast("Preview error: " + error.message); }
  }

  async function remove(jobId) {
    if (!confirm("Permanently delete this job and its exported videos?")) return;
    try {
      await api("/api/jobs/" + encodeURIComponent(jobId), { method: "DELETE" });
      if (selectedJobId === jobId) { selectedJobId = null; $("results").classList.add("hidden"); }
      if (activeJobId === jobId) { activeJobId = null; $("activePanel").classList.add("hidden"); }
      await refreshJobs();
      toast("Job deleted.");
    } catch (error) { toast(error.message); }
  }

  function setupActions() {
    document.addEventListener("click", async (event) => {
      const view = event.target.closest("[data-view]");
      const del = event.target.closest("[data-delete]");
      const down = event.target.closest("[data-download]");
      const play = event.target.closest("[data-preview]");
      if (view) {
        const job = jobs.find((j) => j.id === view.dataset.view);
        if (!job) return;
        activeJobId = job.id;
        renderProgress(job);
        if (job.status === "completed") {
          renderResults(job);
          showPage("create");
          $("results").scrollIntoView({ behavior: "smooth" });
        } else {
          showPage("create");
          $("activePanel").scrollIntoView({ behavior: "smooth" });
        }
      } else if (del) remove(del.dataset.delete);
      else if (down && selectedJobId) download(selectedJobId, down.dataset.download);
      else if (play && selectedJobId) preview(selectedJobId, play.dataset.preview);
    });
    $("downloadAll").addEventListener("click", () => {
      const job = jobs.find((j) => j.id === selectedJobId);
      if (job && job.zip_file) download(job.id, job.zip_file);
    });
    $("refreshBtn").addEventListener("click", () => refreshJobs().catch((e) => toast(e.message)));
    $("navCreate").addEventListener("click", () => showPage("create"));
    $("navLibrary").addEventListener("click", () => showPage("library"));
    $("authForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      accessKey = $("accessPassword").value;
      $("authError").textContent = "";
      try {
        await refreshJobs();
        $("authModal").classList.add("hidden");
        $("accessPassword").value = "";
      } catch (error) {
        $("authError").textContent = error.message;
        accessKey = "";
      }
    });
  }

  function showPage(page) {
    const createPage = page === "create";
    $("pageCreate").classList.toggle("hidden", !createPage);
    $("pageLibrary").classList.toggle("hidden", createPage);
    $("navCreate").classList.toggle("active", createPage);
    $("navLibrary").classList.toggle("active", !createPage);
    $("pageCrumb").textContent = createPage ? "NEW PROJECT" : "MY EXPORTS";
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function init() {
    setupUploads();
    setupActions();
    try {
      const response = await fetch("/api/config", { cache: "no-store" });
      if (!response.ok) throw new Error("Cannot connect to the processing server.");
      config = await response.json();
      $("uploadLimit").textContent = "MP4, MOV, WEBM, MKV, M4V · Up to " + config.max_upload_mb + " MB";
      if (config.needs_setup) {
        $("authModal").classList.remove("hidden");
        $("authError").textContent = "Server setup required: set APP_PASSWORD in the host's environment variables.";
        $("authForm").querySelector("button").disabled = true;
        $("systemState").textContent = "Setup required";
      } else if (config.protected) {
        $("authModal").classList.remove("hidden");
        $("systemState").textContent = "Private workspace";
      } else {
        await refreshJobs();
      }
      setInterval(() => { if (!config?.needs_setup && (!config?.protected || accessKey)) refreshJobs().catch(() => {}); }, 3500);
    } catch (error) {
      $("systemState").textContent = "Server offline";
      toast(error.message);
    }
  }
  init();
})();
