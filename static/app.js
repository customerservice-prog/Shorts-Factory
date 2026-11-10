(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let accessKey = "";
  let config = null;
  let jobs = [];
  let activeJobId = null;
  let selectedJobId = null;
  let layout = "fill";
  let channelProfiles = [];
  let brandPreviewUrl = null;
  let currentPreview = null;
  let toastTimer = null;

  let renderedResultsKey = "";
  let renderedHistoryKey = "";
  let selectedClipIndex = 0;
  let previewVideoUrl = null;
  let previewRequestNumber = 0;
  let pendingCompletionId = null;
  const thumbs = new Map();
  const pendingThumbs = new Map();
  const pad = (number) => String(number).padStart(2, "0");
  function clock(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const minutes = Math.floor(total / 60);
    return (minutes >= 60 ? Math.floor(minutes / 60) + ":" + pad(minutes % 60) : minutes) + ":" + pad(total % 60);
  }
  function thumbnailKey(jobId, file) { return jobId + "/" + file; }

  async function thumbnailUrl(jobId, filename) {
    const key = thumbnailKey(jobId, filename);
    if (thumbs.has(key)) return thumbs.get(key);
    if (pendingThumbs.has(key)) return pendingThumbs.get(key);
    const work = (async () => {
      const response = await api("/api/jobs/" + encodeURIComponent(jobId) +
        "/thumbnails/" + encodeURIComponent(filename));
      const blob = await response.blob();
      if (!blob.type.includes("image/") || !blob.size) throw new Error("Thumbnail is unavailable");
      const url = URL.createObjectURL(blob);
      thumbs.set(key, url);
      return url;
    })();
    pendingThumbs.set(key, work);
    try { return await work; }
    finally { pendingThumbs.delete(key); }
  }

  function applyThumbnail(image) {
    const jobId = image.dataset.thumbJob;
    const file = image.dataset.clipThumb;
    if (!jobId || !file) return;
    thumbnailUrl(jobId, file).then((url) => {
      if (image.isConnected && image.dataset.thumbJob === jobId && image.dataset.clipThumb === file) {
        image.src = url;
        image.classList.add("thumb-loaded");
      }
    }).catch(() => { if (image.isConnected) image.classList.add("thumb-unavailable"); });
  }
  function hydrateThumbnails(root) {
    root.querySelectorAll("img[data-clip-thumb]").forEach(applyThumbnail);
  }
  function clearVideoPreview() {
    previewRequestNumber++;
    const video = $("featuredVideo");
    video.pause();
    video.removeAttribute("src");
    video.load();
    video.classList.add("hidden");
    if (previewVideoUrl) URL.revokeObjectURL(previewVideoUrl);
    previewVideoUrl = null;
    $("stageLoading").classList.add("hidden");
    $("stagePlayWrap").classList.remove("hidden");
  }

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


  async function loadBrands(selectedId = "bajo-mundo") {
    const response = await api("/api/brands");
    const data = await response.json();
    channelProfiles = data.brands || [];
    const select = $("brandProfile");
    const safe = channelProfiles.some(p => p.id === selectedId) ? selectedId : "bajo-mundo";
    select.replaceChildren();
    for (const brand of channelProfiles) {
      const option = document.createElement("option");
      option.value = brand.id; option.textContent = brand.name;
      select.appendChild(option);
    }
    const custom = document.createElement("option");
    custom.value = "new"; custom.textContent = "+ Add another channel";
    select.appendChild(custom); select.value = safe;
    await updateBrandPreview();
  }

  async function updateBrandPreview() {
    const id = $("brandProfile").value;
    const isNew = id === "new";
    $("newBrandControls").classList.toggle("hidden", !isNew);
    const brand = channelProfiles.find(p => p.id === id);
    $("brandPreviewName").textContent = isNew ? "New channel" : brand?.name || "No logo";
    $("brandPreviewDescription").textContent = isNew ? "Save this profile before generating" :
      id === "none" ? "Exports will not display a channel logo" : "Added automatically to new exports";
    if (brandPreviewUrl) { URL.revokeObjectURL(brandPreviewUrl); brandPreviewUrl = null; }
    $("brandPreviewImg").hidden = true;
    $("brandPreviewImg").removeAttribute("src");
    $("brandPreviewFallback").hidden = false;
    $("brandPreviewFallback").textContent = id === "none" ? "—" : "TV";
    if (brand?.logo_url) {
      try {
        const response = await api(brand.logo_url);
        const url = URL.createObjectURL(await response.blob());
        if ($("brandProfile").value !== id) { URL.revokeObjectURL(url); return; }
        brandPreviewUrl = url;
        $("brandPreviewImg").src = url; $("brandPreviewImg").hidden = false;
        $("brandPreviewFallback").hidden = true;
      } catch (_) {
        $("brandPreviewDescription").textContent = "Logo preview unavailable. Choose another channel.";
      }
    }
  }

  async function saveChannelProfile() {
    if (!$("logoFile").files[0]) return toast("Choose a new channel logo first.");
    if (!$("newChannelName").value.trim()) return toast("Enter the channel name.");
    $("saveBrandBtn").disabled = true;
    try {
      const form = new FormData();
      form.append("name", $("newChannelName").value.trim());
      form.append("logo", $("logoFile").files[0]);
      const response = await api("/api/brands", { method: "POST", body: form });
      const saved = await response.json();
      $("logoFile").value = "";
      $("newChannelName").value = "";
      $("logoLabel").textContent = "Transparent PNG recommended";
      $("logoSymbol").textContent = "＋";
      await loadBrands(saved.id);
      toast("Channel profile saved. The next export will use its logo.");
    } catch (error) {
      toast("Unable to save channel: " + error.message);
    } finally {
      $("saveBrandBtn").disabled = false;
    }
  }

  function renderPublishingReview(job) {
    const review = job.review;
    $("reviewFlags").replaceChildren();
    if (!review) {
      $("reviewStatus").textContent = "Older export · not audited";
      $("clipReview").textContent = "This export predates the automated checks. Copyright and monetization were not verified.";
      return;
    }
    $("reviewStatus").textContent = review.status === "high_risk" ?
      "High risk · review first" : "Manual review required";
    for (const finding of review.source_findings || []) {
      const wrap = document.createElement("div");
      wrap.className = "review-flag " + (finding.level || "review");
      const heading = document.createElement("strong"); heading.textContent = finding.title;
      const detail = document.createElement("p"); detail.textContent = finding.detail;
      wrap.append(heading, detail);
      $("reviewFlags").appendChild(wrap);
    }
    renderSelectedReview(job);
  }

  function renderSelectedReview(job) {
    const destination = $("clipReview");
    destination.replaceChildren();
    const report = job?.review?.clip_reviews?.[selectedClipIndex];
    const clip = job?.clips?.[selectedClipIndex];
    if (!report || !clip) {
      destination.textContent = "No per-clip audit available for this export.";
      return;
    }
    const headline = document.createElement("h4");
    headline.textContent = "Clip " + pad(selectedClipIndex + 1) + " · technical + content review";
    destination.appendChild(headline);
    const tech = document.createElement("p");
    tech.textContent = clip.technical?.verified ?
      "TECHNICAL PASS — 1080 × 1920 H.264" + (clip.technical.audio ? " · AAC audio" : "") +
      " · " + clock(clip.technical.duration) + ". This is not a monetization approval." :
      "Technical export has not been verified.";
    destination.appendChild(tech);
    const list = document.createElement("ul");
    const findings = report.checks || [];
    for (const flag of findings) {
      const line = document.createElement("li");
      line.textContent = flag.title + " — " + flag.detail;
      list.appendChild(line);
    }
    if (!findings.length) {
      const li = document.createElement("li");
      li.textContent = "No additional automatic warnings detected. Manual rights and originality review still required.";
      list.appendChild(li);
    }
    destination.appendChild(list);
  }

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

  function resetSource() {
    $("videoFile").value = "";
    if (sourcePreviewUrl) {
      $("sourcePreview").pause();
      $("sourcePreview").removeAttribute("src");
      $("sourcePreview").load();
      URL.revokeObjectURL(sourcePreviewUrl);
      sourcePreviewUrl = null;
    }
    $("sourceCard").classList.add("hidden");
    $("dropzone").classList.remove("hidden", "dragging");
    $("generateBtn").disabled = true;
    $("generateText").textContent = "Choose a video to start";
  }

  function setFile(file) {
    if (!file) return;
    const allowed = /\.(mp4|mov|mkv|webm|m4v)$/i.test(file.name);
    if (!allowed) {
      resetSource();
      return toast("Choose MP4, MOV, MKV, WEBM or M4V video.");
    }
    if (!file.size) {
      resetSource();
      return toast("That file is empty. Choose a different video.");
    }
    if (config && file.size > config.max_upload_mb * 1024 * 1024) {
      resetSource();
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
    $("brandProfile").addEventListener("change", () => updateBrandPreview());
    $("saveBrandBtn").addEventListener("click", saveChannelProfile);
    $("targetPlatform").addEventListener("change", () => {
      if ($("targetPlatform").value === "tiktok_rewards" && Number($("clipLength").value) < 90) {
        $("clipLength").value = "90";
        updateSummary();
        toast("Target switched to 90 seconds for TikTok Creator Rewards. Source must support clips longer than 60 seconds.");
      }
    });
    $("createForm").addEventListener("submit", create);
  }

  async function create(event) {
    event.preventDefault();
    const video = $("videoFile").files[0];
    if (!video) return toast("Please choose a video first.");
    if (config?.needs_setup) return toast("Set APP_PASSWORD in your server environment first.");
    if ($("brandProfile").value === "new") return toast("Save your new channel logo before creating clips.");
    if ($("coverOldBrand").value !== "none" && !["owned", "licensed"].includes($("rights").value)) {
      return toast("Replacing an old logo requires owned or licensed footage."); }
    const form = new FormData();
    form.append("video", video);
    form.append("brand_id", $("brandProfile").value);
    form.append("cover_old_brand", $("coverOldBrand").value);
    form.append("audio_rights", $("audioRights").value);
    form.append("originality", $("originality").value);
    form.append("target_platform", $("targetPlatform").value);
    form.append("rights_evidence", $("rightsEvidence").value);
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
    document.body.classList.remove("show-editor");
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
      pendingCompletionId = job.id;
      renderedResultsKey = "";
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
      queued: "Queued", analyzing: "Finding highlights", rendering: "Editing shorts",
      completed: "Ready", failed: "Failed", interrupted: "Interrupted"
    })[status] || "Unknown";
  }

  function renderProgress(job) {
    const panel = $("activePanel");
    if (!job || job.status === "completed") {
      panel.classList.add("hidden");
      return;
    }
    panel.classList.remove("hidden");
    $("progressHeadline").textContent = job.status === "failed" ? "Processing needs attention" :
      job.status === "interrupted" ? "Processing was interrupted" : "Creating your shorts";
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
    const first = (job.clips || [])[0];
    const poster = first ? '<img class="history-poster" data-thumb-job="' + esc(job.id) +
      '" data-clip-thumb="' + esc(first.file) + '" alt="Video thumbnail" loading="lazy">' : '<span>▶</span>';
    return '<article class="history-item"><span class="history-file-icon">' + poster + '</span>' +
      '<div class="history-details"><strong title="' + esc(job.filename) + '">' + esc(job.filename) + '</strong>' +
      '<p>' + esc(date) + ' · ' + esc((job.clips || []).length || job.clip_count) +
      ' clips · ' + esc(job.clip_length) + 's target</p></div>' +
      '<span class="status-tag ' + esc(job.status) + '">' + esc(statusLabel(job.status)) + '</span>' +
      '<button type="button" data-view="' + esc(job.id) + '">' + (job.status === "completed" ? "Open studio ↗" : "View status") + '</button>' +
      (canDelete ? '<button type="button" class="delete-btn" data-delete="' + esc(job.id) + '" aria-label="Delete project">×</button>' : "") +
      '</article>';
  }

  function renderHistory() {
    const fingerprint = jobs.map((j) => j.id + "/" + j.status + "/" + j.updated_at).join("|");
    if (fingerprint === renderedHistoryKey) return;
    renderedHistoryKey = fingerprint;
    const html = jobs.length ? jobs.map(historyMarkup).join("") :
      '<div class="empty-history"><span>▦</span><strong>No projects yet</strong><p>Upload a video above and your edited shorts will appear here.</p></div>';
    $("historyList").innerHTML = html;
    $("libraryList").innerHTML = html;
    $("navCount").textContent = jobs.length;
    hydrateThumbnails($("historyList"));
    hydrateThumbnails($("libraryList"));
  }

  function buildClipCard(job, clip, index) {
    const duration = clock(clip.length);
    const startTime = clock(clip.start);
    const count = pad(index + 1);
    const captions = clip.captions ? "Captioned" : "No captions";
    return '<article class="clip-card" data-card="' + index + '">' +
      '<button type="button" class="clip-cover" data-select="' + index + '" aria-label="Preview short ' + count + '">' +
      '<span class="cover-placeholder">▣</span><img data-thumb-job="' + esc(job.id) + '" data-clip-thumb="' + esc(clip.file) +
      '" alt="Actual video frame of short ' + count + '" loading="lazy">' +
      '<span class="clip-cover-top">SHORT ' + count + '</span>' +
      '<span class="clip-cover-play">▶</span>' +
      '<span class="clip-cover-length">' + duration + '</span>' +
      '</button><div class="clip-info">' +
      '<div class="clip-number">HIGHLIGHT ' + count +
      (index === 0 ? '<span class="top-pick">✦ TOP PICK</span>' : "") + '</div>' +
      '<h3 title="' + esc(clip.title) + '">' + esc(clip.title) + '</h3>' +
      '<div class="clip-stats"><span>◷ ' + startTime + ' in source</span><span>▣ ' + captions + '</span></div>' +
      '<div class="clip-buttons"><button type="button" data-preview="' + esc(clip.file) + '">▶ Watch clip</button>' +
      '<button class="download" type="button" data-download="' + esc(clip.file) + '">↓ MP4</button></div></div></article>';
  }

  function buildQueueItem(job, clip, index) {
    return '<button type="button" class="queue-item" data-select="' + index +
      '" aria-label="Select short ' + pad(index + 1) + '">' +
      '<span class="queue-poster"><img data-thumb-job="' + esc(job.id) +
      '" data-clip-thumb="' + esc(clip.file) + '" alt="" loading="lazy"><span>▶</span></span>' +
      '<span class="queue-text"><small>SHORT ' + pad(index + 1) + ' · ' + clock(clip.length) + '</small>' +
      '<strong>' + esc(clip.title) + '</strong><em>From ' + clock(clip.start) + ' · ' +
      (clip.captions ? "Captions on" : "No captions") + '</em></span>' +
      '<span class="queue-arrow">↗</span></button>';
  }

  function renderResults(job) {
    if (!job || job.status !== "completed") {
      $("results").classList.add("hidden");
      return;
    }
    const fingerprint = job.id + "/" + (job.clips || []).map(c => c.file).join("|");
    selectedJobId = job.id;
    if (renderedResultsKey === fingerprint && !$("results").classList.contains("hidden")) return;
    renderedResultsKey = fingerprint;
    $("results").classList.remove("hidden");
    document.body.classList.add("has-results");
    document.body.classList.remove("show-editor");
    $("editorHeadline").textContent = "Create your next batch";
    $("resultsCount").textContent = (job.clips || []).length + " clips";
    $("stageQueueCount").textContent = (job.clips || []).length + " clips";
    $("resultsSource").textContent = job.filename;
    $("resultsTotal").textContent = (job.clips || []).length + " finished edits";
    $("warnings").innerHTML = (job.warnings || []).map(w => '<div class="warning">' + esc(w) + '</div>').join("");
    renderPublishingReview(job);
    $("clipsGrid").innerHTML = (job.clips || []).map((clip,i) => buildClipCard(job,clip,i)).join("");
    $("clipQueue").innerHTML = (job.clips || []).map((clip,i) => buildQueueItem(job,clip,i)).join("");
    hydrateThumbnails($("clipsGrid"));
    hydrateThumbnails($("clipQueue"));
    selectClip(0, false);
    $("activePanel").classList.add("hidden");
  }

  function selectClip(index, playNow = false) {
    const job = jobs.find(j => j.id === selectedJobId);
    if (!job || !job.clips || !job.clips[index]) return;
    clearVideoPreview();
    selectedClipIndex = index;
    renderSelectedReview(job);
    const clip = job.clips[index];
    const total = job.clips.length;
    $("stageNumber").textContent = "CLIP " + pad(index + 1);
    $("stageDuration").textContent = clock(clip.length);
    $("stagePosition").textContent = pad(index + 1) + " / " + pad(total);
    $("stageClipTitle").textContent = clip.title;
    $("stageClipDetails").textContent = clock(clip.length) + " · from " + clock(clip.start) +
      " in your original video · " + (clip.captions ? "Captions included" : "No captions");
    $("featuredPoster").removeAttribute("src");
    $("stageBackdrop").removeAttribute("src");
    const currentJob = selectedJobId;
    thumbnailUrl(job.id, clip.file).then(url => {
      if (currentJob !== selectedJobId || selectedClipIndex !== index) return;
      $("featuredPoster").src = url;
      $("stageBackdrop").src = url;
    }).catch(() => {});
    document.querySelectorAll("[data-select]").forEach(button => {
      const selected = Number(button.dataset.select) === index;
      button.classList.toggle("selected", selected);
      button.setAttribute("aria-pressed", selected ? "true" : "false");
    });
    document.querySelectorAll("[data-card]").forEach(card => {
      card.classList.toggle("selected", Number(card.dataset.card) === index);
    });
    if (playNow) playSelectedClip();
  }

  async function playSelectedClip() {
    const job = jobs.find(j => j.id === selectedJobId);
    const clip = job?.clips?.[selectedClipIndex];
    if (!job || !clip) return;
    clearVideoPreview();
    const requestId = ++previewRequestNumber;
    $("stageLoading").classList.remove("hidden");
    $("stagePlayWrap").classList.add("hidden");
    try {
      const blob = await fileBlob(job.id, clip.file);
      if (requestId !== previewRequestNumber) return;
      previewVideoUrl = URL.createObjectURL(blob);
      const player = $("featuredVideo");
      player.src = previewVideoUrl;
      player.classList.remove("hidden");
      $("stageLoading").classList.add("hidden");
      player.play().catch(() => { /* Browser may require tapping play. */ });
    } catch (error) {
      if (requestId === previewRequestNumber) {
        $("stageLoading").classList.add("hidden");
        $("stagePlayWrap").classList.remove("hidden");
        toast("Unable to preview this clip: " + error.message);
      }
    }
  }

  async function refreshJobs() {
    try {
      const response = await api("/api/jobs");
      jobs = (await response.json()).jobs || [];
      if (!channelProfiles.length) {
        try { await loadBrands(); } catch (error) { toast("Branding unavailable: " + error.message); }
      }
      $("systemState").textContent = "Engine ready";
      renderHistory();
      const working = jobs.find(j => ["queued", "analyzing", "rendering"].includes(j.status));
      if (working) activeJobId = working.id;
      if (activeJobId) renderProgress(jobs.find(j => j.id === activeJobId));
      const selected = jobs.find(j => j.id === selectedJobId);
      if (selected?.status === "completed") renderResults(selected);
      else if (!selectedJobId || !selected) {
        const latest = jobs.find(j => j.status === "completed");
        if (latest) renderResults(latest);
      }
      if (pendingCompletionId && jobs.some(j => j.id === pendingCompletionId && j.status === "completed")) {
        pendingCompletionId = null;
        toast("Your shorts are ready! Pick a thumbnail to review.");
        setTimeout(() => $("results").scrollIntoView({ behavior: "smooth", block: "start" }), 80);
      }
    } catch (error) {
      $("systemState").textContent = "Connection issue";
      if (error.message.includes("Incorrect access password") || error.message.includes("401")) {
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
    const job = jobs.find(j => j.id === jobId);
    if (!job) return;
    const index = (job.clips || []).findIndex(c => c.file === file);
    if (index < 0) return;
    if (selectedJobId !== jobId) renderResults(job);
    selectClip(index, true);
    showPage("create", false);
    $("results").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function remove(jobId) {
    if (!confirm("Permanently delete this job and its exported videos?")) return;
    try {
      await api("/api/jobs/" + encodeURIComponent(jobId), { method: "DELETE" });
      if (selectedJobId === jobId) {
        clearVideoPreview();
        selectedJobId = null;
        renderedResultsKey = "";
        $("results").classList.add("hidden");
        document.body.classList.remove("has-results", "show-editor");
        $("editorHeadline").textContent = "Create your first batch";
      }
      if (activeJobId === jobId) { activeJobId = null; $("activePanel").classList.add("hidden"); }
      for (const [key, url] of thumbs) {
        if (key.startsWith(jobId + "/")) { URL.revokeObjectURL(url); thumbs.delete(key); }
      }
      renderedHistoryKey = "";
      await refreshJobs();
      toast("Job deleted.");
    } catch (error) { toast(error.message); }
  }

  function setupActions() {
    document.addEventListener("click", async (event) => {
      const selectedButton = event.target.closest("[data-select]");
      if (selectedButton) {
        selectClip(Number(selectedButton.dataset.select), true);
        $("results").scrollIntoView({behavior:"smooth",block:"start"});
        return;
      }
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
    $("stagePlay").addEventListener("click", playSelectedClip);
    $("stageDownload").addEventListener("click", () => {
      const job = jobs.find(j => j.id === selectedJobId);
      const clip = job?.clips?.[selectedClipIndex];
      if (clip) download(job.id, clip.file);
    });
    $("stageCoverDownload").addEventListener("click", async () => {
      const job = jobs.find(j => j.id === selectedJobId);
      const clip = job?.clips?.[selectedClipIndex];
      if (!job || !clip) return;
      try {
        const url = await thumbnailUrl(job.id, clip.file);
        const link = document.createElement("a");
        link.href = url;
        link.download = clip.file.replace(".mp4", "-cover.jpg");
        document.body.append(link);
        link.click();
        link.remove();
        toast("Video cover downloaded as JPG.");
      } catch (error) {
        toast("Cover unavailable: " + error.message);
      }
    });
    $("stageCopy").addEventListener("click", async () => {
      const job = jobs.find(j => j.id === selectedJobId);
      const clip = job?.clips?.[selectedClipIndex];
      if (!clip) return;
      const caption = clip.title + "\n\n#Shorts";
      try {
        await navigator.clipboard.writeText(caption);
        toast("Caption copied. Review and personalize it before posting.");
      } catch (_) {
        const promptText = window.prompt("Copy this caption:", caption);
        if (promptText !== null) toast("Caption available to copy.");
      }
    });
    const openEditor = () => {
      showPage("create", false);
      document.body.classList.add("show-editor");
      resetSource();
      $("createForm").scrollIntoView({behavior:"smooth",block:"start"});
    };
    $("newProjectBtn").addEventListener("click", openEditor);
    $("refreshBtn").addEventListener("click", () => refreshJobs().catch((e) => toast(e.message)));
    $("navCreate").addEventListener("click", () => {
      if (document.body.classList.contains("has-results")) openEditor();
      else showPage("create");
    });
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

  function showPage(page, shouldScroll = true) {
    const createPage = page === "create";
    $("pageCreate").classList.toggle("hidden", !createPage);
    $("pageLibrary").classList.toggle("hidden", createPage);
    $("navCreate").classList.toggle("active", createPage);
    $("navLibrary").classList.toggle("active", !createPage);
    $("pageCrumb").textContent = createPage ? "NEW PROJECT" : "MY EXPORTS";
    if (shouldScroll) window.scrollTo({ top: 0, behavior: "smooth" });
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
