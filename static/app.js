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
  let fullVideoPreviewUrl = null;
  let fullVideoPreviewRequest = 0;
  let brandBatchFiles = [];
  let brandBatchBusy = false;
  let cleanupRegions = [];
  let cleanupSuggestions = [];
  let cleanupVideoUrl = null;
  let cleanupDrag = null;
  let cleanupBeforeImageUrl = null;
  let cleanupAfterImageUrl = null;
  let cleanupRenderRevision = 0;
  let cleanupPreviewBusy = false;
  let currentPreview = null;
  let toastTimer = null;

  let renderedResultsKey = "";
  let renderedHistoryKey = "";
  let selectedClipIndex = 0;
  let previewVideoUrl = null;
  let previewRequestNumber = 0;
  let pendingCompletionId = null;
  let lastDisplayedClipFile = null;
  let lastReframeSeen = null;
  let reframeSubmitting = false;
  // New uploads are the default workspace; saved exports open only on request.
  let showingNewProject = true;
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
    $("featuredPoster").classList.remove("hidden");
    $("stageBackdrop").classList.remove("hidden");
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
    const batch = $("brandOnlyProfile"), old = batch.value;
    batch.replaceChildren();
    for (const brand of channelProfiles.filter(item => item.logo_url)) {
      const option = document.createElement("option");
      option.value = brand.id; option.textContent = brand.name;
      batch.appendChild(option);
    }
    batch.value = channelProfiles.some(p => p.id === old && p.logo_url) ? old :
      (channelProfiles.some(p => p.id === safe && p.logo_url) ? safe : "bajo-mundo");
    await updateBrandPreview();
    await updateFullVideoPreview(true);
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
      "TECHNICAL PASS — " + clip.technical.width + " × " + clip.technical.height +
      " H.264" + (clip.technical.audio ? " · Audio preserved" : " · No audio") +
      " · " + clock(clip.technical.duration) +
      (job.mode === "brand_only" ? " · Complete video preserved" : "") +
      ". This is not a monetization approval." :
      "Technical export has not been verified.";
    destination.appendChild(tech);
    if (job.mode === "brand_only") {
      const quality = document.createElement("p");
      quality.className = "cleanup-quality " +
        (clip.technical?.cleanup_region_count ? "needs-review" : "unknown");
      const regions = clip.technical?.cleanup_region_count || 0;
      quality.textContent = regions ?
        "CLEANUP ATTEMPTED — " + regions +
          " on-video area(s) reconstructed. This DOES NOT certify old logos are gone. Watch every second before publishing." :
        "NO VERIFIED GRAPHIC REMOVAL — Existing embedded branding may still be present. A new Bajo Mundo logo does not clean the footage.";
      destination.appendChild(quality);
    }
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

  function updatePreflight() {
    const checks = $("preflightChecks");
    const state = $("preflightState");
    if (!checks || !state) return;
    checks.replaceChildren();
    const file = $("videoFile").files[0];
    const flags = [];
    if (!file) {
      state.textContent = "Choose a video";
      checks.textContent = "Select a video to see the pre-export checks.";
      return;
    }
    const preview = $("sourcePreview");
    const seconds = Number.isFinite(preview.duration) ? preview.duration : 0;
    const platform = $("targetPlatform").value;
    const selectedLength = Number($("clipLength").value);
    const frame = preview.videoWidth && preview.videoHeight ?
      preview.videoWidth + " × " + preview.videoHeight : "Dimensions pending";
    flags.push(["info", "Source: " + frame + (seconds ? " · " + clock(seconds) : "")]);
    const title = file.name.toLowerCase();
    if (/official[\s_-]*(music[\s_-]*)?video|video[\s_-]*oficial|visualizer|lyric[\s_-]*video/.test(title)) {
      flags.push(["warning", "This appears to be an existing music video; independently confirm all footage, composition and recording rights."]);
    }
    if ($("rights").value === "unknown") {
      flags.push(["warning", "Footage rights have not been verified."]);
    } else if ($("rights").value === "commentary") {
      flags.push(["warning", "Commentary exceptions and platform originality require human review."]);
    }
    if ($("audioRights").value === "unknown") {
      flags.push(["warning", "Music and audio rights have not been verified."]);
    }
    if (["minimal", "unknown"].includes($("originality").value)) {
      flags.push(["warning", "A logo, crop or captions alone cannot establish originality for monetization."]);
    }
    if (seconds && seconds < selectedLength) {
      flags.push(["warning", "The source is shorter than your requested " + selectedLength + "s target; the result may be shorter."]);
    }
    if (platform === "tiktok_rewards" && seconds && seconds <= 60) {
      flags.push(["warning", "The source is too short to produce a video over 60 seconds for TikTok Creator Rewards."]);
    }
    if (preview.videoWidth && preview.videoHeight && preview.videoWidth > preview.videoHeight * 1.3 && layout === "fill") {
      flags.push(["info", "Wide source: fill will crop the sides. Use left/right focus if the source is split-screen."]);
    }
    if ($("coverOldBrand").value !== "none") {
      flags.push(["warning", "Covering an old corner logo is permitted only on footage you are authorized to rebrand."]);
    }
    for (const [level, message] of flags) {
      const item = document.createElement("div");
      item.className = "preflight-item " + level;
      item.textContent = message;
      checks.appendChild(item);
    }
    state.textContent = flags.some(flag => flag[0] === "warning") ?
      "Publishing review needed" : "Technical preview ready";
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
    updatePreflight();
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
    updatePreflight();
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
      updatePreflight();
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
        if ($("captions").value !== "existing") $("captions").value = recipe.captions;
        updateSummary();
      });
    });
    $("clipLength").addEventListener("change", updateSummary);
    $("captions").addEventListener("change", () => {
      $("brandName").disabled = $("captions").value === "existing";
      updateSummary();
    });
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
        updatePreflight();
      });
    });
    $("brandProfile").addEventListener("change", () => { updateBrandPreview(); updatePreflight(); });
    for (const id of ["rights", "audioRights", "originality", "targetPlatform", "coverOldBrand"]) {
      $(id).addEventListener("change", updatePreflight);
    }
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
    const keepExistingText = $("captions").value === "existing";
    form.append("captions", keepExistingText ? "none" : $("captions").value);
    form.append("keep_existing_text", String(keepExistingText));
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
      showingNewProject = false;
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
      '<p>' + esc(date) + (job.mode === "brand_only" ?
      ' · Full video · no cuts' :
      ' · ' + esc((job.clips || []).length || job.clip_count) +
      ' clips · ' + esc(job.clip_length) + 's target') + '</p></div>' +
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
      '<span class="clip-cover-top">' + (job.mode === "brand_only" ? "VIDEO " : "SHORT ") + count + '</span>' +
      '<span class="clip-cover-play">▶</span>' +
      '<span class="clip-cover-length">' + duration + '</span>' +
      '</button><div class="clip-info">' +
      '<div class="clip-number">' + (job.mode === "brand_only" ? "FULL VIDEO " : "HIGHLIGHT ") + count +
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
      '<span class="queue-text"><small>' + (job.mode === "brand_only" ? "VIDEO " : "SHORT ") + pad(index + 1) + ' · ' + clock(clip.length) + '</small>' +
      '<strong>' + esc(clip.title) + '</strong><em>From ' + clock(clip.start) + ' · ' +
      (clip.captions ? "Captions on" : "No captions") + '</em></span>' +
      '<span class="queue-arrow">↗</span></button>';
  }

  function renderResults(job) {
    if (!job || job.status !== "completed") {
      $("results").classList.add("hidden");
      return;
    }
    showingNewProject = false;
    const fingerprint = job.id + "/" + job.source_available + "/" +
      (job.clips || []).map(c => c.file).join("|");
    const previousFile = selectedJobId === job.id ? lastDisplayedClipFile : null;
    selectedJobId = job.id;
    if (renderedResultsKey === fingerprint && !$("results").classList.contains("hidden")) return;
    const changedJob = !renderedResultsKey.startsWith(job.id + "/");
    renderedResultsKey = fingerprint;
    $("results").classList.remove("hidden");
    document.body.classList.add("has-results");
    document.body.classList.remove("show-editor");
    $("editorHeadline").textContent = "Create highlight clips instead";
    const fullVideo = job.mode === "brand_only";
    $("resultsTitle").textContent = fullVideo ? "Your full branded video" : "Meet your new shorts";
    $("galleryTitle").textContent = fullVideo ? "Full-length export" : "All clips";
    $("reframeTool").classList.toggle("hidden", fullVideo);
    $("savedSmartClean").classList.remove("hidden");
    $("reframeSourceRow").classList.toggle("hidden", job.source_available === true);
    $("savedCleanSourceRow").classList.toggle("hidden", job.source_available === true);
    if (changedJob) {
      $("reframeSource").value = "";
      $("savedCleanSource").value = "";
      $("savedCleanState").textContent = "";
    }
    $("downloadAll").textContent = fullVideo ? "↓ Download full MP4" : "↓ Download all ZIP";
    $("resultsCount").textContent = (job.clips || []).length + (fullVideo ? " video" : " clips");
    $("stageQueueCount").textContent = (job.clips || []).length + (fullVideo ? " video" : " clips");
    $("resultsSource").textContent = job.filename;
    $("resultsTotal").textContent = fullVideo ?
      (job.cleanup_status === "needs_visual_review" ?
        "Full video · old graphics NEED REVIEW" : "Entire video preserved") :
      (job.clips || []).length + " finished edits";
    $("warnings").innerHTML = (job.warnings || []).map(w => '<div class="warning">' + esc(w) + '</div>').join("");
    renderPublishingReview(job);
    $("clipsGrid").innerHTML = (job.clips || []).map((clip,i) => buildClipCard(job,clip,i)).join("");
    $("clipQueue").innerHTML = (job.clips || []).map((clip,i) => buildQueueItem(job,clip,i)).join("");
    hydrateThumbnails($("clipsGrid"));
    hydrateThumbnails($("clipQueue"));
    let nextIndex = previousFile ? (job.clips || []).findIndex(c => c.file === previousFile) : 0;
    if (job.reframe?.status === "ready" && job.reframe.file &&
        lastReframeSeen !== job.id + "/" + job.reframe.file) {
      nextIndex = (job.clips || []).findIndex(c => c.file === job.reframe.file);
      lastReframeSeen = job.id + "/" + job.reframe.file;
      toast("Your cleaner, reframed version is ready. The old clip is still available.");
    }
    selectClip(Math.max(0, nextIndex), false);
    updateReframeStatus(job);
    $("activePanel").classList.add("hidden");
  }

  function selectClip(index, playNow = false) {
    const job = jobs.find(j => j.id === selectedJobId);
    if (!job || !job.clips || !job.clips[index]) return;
    clearVideoPreview();
    selectedClipIndex = index;
    renderSelectedReview(job);
    const clip = job.clips[index];
    lastDisplayedClipFile = clip.file;
    const total = job.clips.length;
    $("stageNumber").textContent = (job.mode === "brand_only" ? "VIDEO " : "CLIP ") + pad(index + 1);
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
      $("featuredPoster").classList.add("hidden");
      $("stageBackdrop").classList.add("hidden");
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
      if (!showingNewProject) {
        if (activeJobId) renderProgress(jobs.find(j => j.id === activeJobId));
        const selected = jobs.find(j => j.id === selectedJobId);
        if (selected?.status === "completed") {
          renderResults(selected);
          updateReframeStatus(selected);
        }
        else if (!selectedJobId || !selected) {
          const latest = jobs.find(j => j.status === "completed");
          if (latest) renderResults(latest);
        }
        if (pendingCompletionId && jobs.some(j => j.id === pendingCompletionId && j.status === "completed")) {
          pendingCompletionId = null;
          toast("Your video is ready! Open My exports to review.");
          setTimeout(() => $("results").scrollIntoView({ behavior: "smooth", block: "start" }), 80);
        }
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
      const job = jobs.find(j => j.id === jobId);
      const safe = text => String(text || "").replace(/[^A-Za-z0-9_-]+/g,"_").replace(/^_+|_+$/g,"");
      anchor.download = job?.mode === "brand_only" && file === "clip_01.mp4" ?
        (safe(job.filename.replace(/\.[^.]+$/, "")).slice(0,85) || "video") + "_" +
        (safe(job.channel_name).slice(0,35) || "Bajo_Mundo_TV") + ".mp4" : file;
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
    showingNewProject = false;
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


  function updateReframeStatus(job) {
    const status = $("reframeStatus");
    const button = $("reframeBtn");
    if (reframeSubmitting) return;
    const task = job?.reframe;
    const busy = task?.status === "queued" || task?.status === "rendering";
    button.disabled = busy;
    button.textContent = busy ? "Creating new clip…" : "Create cleaner version";
    status.textContent = busy ?
      "Generating " + task.focus + " focus from your original video. The new clip will appear here." :
      task?.status === "failed" ? (task.error || "Could not create the reframed clip.") :
      task?.status === "ready" ? "Latest version saved: " + task.file +
        ". Your old clip was not changed. Check the new composition before publishing." :
      "Choose left/right for a split-screen source. Embedded channel marks, captions or audio remain unless you have rights and edit them separately.";
  }

  async function createFocusedVersion() {
    if (reframeSubmitting) return;
    const job = jobs.find(j => j.id === selectedJobId);
    const clip = job?.clips?.[selectedClipIndex];
    if (!job || !clip) return toast("Select an existing clip first.");
    if (!$("reframePermission").checked) return toast("Please confirm you have permission to re-edit the source footage.");
    if (!["left", "right", "fill"].includes($("reframeFocus").value)) return;
    const source = $("reframeSource").files[0];
    if (!job.source_available && !source)
      return toast("Choose the original video from your device.");
    const old = clip.source_clip || clip.file;
    const data = new FormData();
    data.append("clip_file", old);
    data.append("focus", $("reframeFocus").value);
    data.append("rights_confirmed", "true");
    if (source) data.append("video", source, source.name);
    reframeSubmitting = true;
    $("reframeBtn").disabled = true;
    $("reframeStatus").textContent = "Submitting your focused edit…";
    try {
      const response = await api("/api/jobs/" + encodeURIComponent(job.id) + "/reframe",
                                 {method:"POST", body:data});
      const queued = await response.json();
      $("reframeSource").value = "";
      $("reframeStatus").textContent = "Creating " + queued.file + ".";
      toast("Reframing started. Watch for your new Short in the playlist.");
      await refreshJobs();
    } catch (error) {
      $("reframeStatus").textContent = error.message;
      toast("Reframe error: " + error.message);
      $("reframeBtn").disabled = false;
    } finally {
      reframeSubmitting = false;
    }
  }



  async function updateFullVideoPreview(fetchLogo=false) {
    const logo = $("watermarkPreviewImg");
    const profile = channelProfiles.find(p => p.id === $("brandOnlyProfile").value);
    const skipped = $("brandLogoAlreadyEmbedded").checked;
    const percent = Number($("brandLogoSize").value);
    const alpha = Number($("brandLogoOpacity").value);
    $("brandLogoSizeValue").textContent = percent + "%";
    $("brandLogoOpacityValue").textContent = alpha + "%";
    const keepExistingText = $("brandSubtitleMode").value === "existing" ||
      !!$("brandExistingTextOptions").querySelector('input[value="0"]:checked');
    $("showBannerDemo").hidden = keepExistingText || !$("brandFollowBanner").checked;
    $("showCaptionDemo").hidden = keepExistingText || $("brandSubtitleMode").value === "none";
    $("showCaptionDemo").classList.toggle("clean", $("brandSubtitleStyle").value === "clean");
    $("watermarkPreviewMessage").textContent = skipped ?
      "No extra watermark will be burned in. Your existing on-video logo stays." :
      "New exports will display " + (profile?.name || "your logo") +
      " at " + percent + "% of the video width.";
    logo.style.width = percent + "%";
    logo.style.opacity = alpha / 100;
    logo.hidden = skipped || !profile?.logo_url;
    const sample = $("watermarkDemo");
    sample.dataset.position = $("brandOnlyPosition").value;
    if (!fetchLogo) return;
    const serial = ++fullVideoPreviewRequest;
    if (fullVideoPreviewUrl) {
      URL.revokeObjectURL(fullVideoPreviewUrl);
      fullVideoPreviewUrl = null;
    }
    logo.removeAttribute("src");
    if (!profile?.logo_url) return;
    try {
      const response = await api(profile.logo_url);
      const url = URL.createObjectURL(await response.blob());
      if (serial !== fullVideoPreviewRequest ||
          $("brandOnlyProfile").value !== profile.id) {
        URL.revokeObjectURL(url);
        return;
      }
      fullVideoPreviewUrl = url;
      logo.src = url;
      logo.hidden = $("brandLogoAlreadyEmbedded").checked;
    } catch (_) {
      $("watermarkPreviewMessage").textContent = "Preview unavailable. The selected logo can still be checked before export.";
    }
  }


  function cleanupInfo(message) {
    $("cleanupInfo").textContent = message ||
      (cleanupRegions.length + " area(s) selected. New footage is generated with these rectangles repaired before the Bajo Mundo logo and subtitles are added.");
  }
  function allowedCleanupBox(box) {
    return [box.x,box.y,box.w,box.h].every(Number.isFinite) &&
      box.x >= 0 && box.y >= 0 && box.w >= 2 && box.h >= 2 &&
      box.w <= 65 && box.h <= 40 &&
      box.x + box.w <= 100.001 && box.y + box.h <= 100.001;
  }
  function renderCleanupBoxes() {
    const layer = $("cleanupDrawLayer");
    layer.replaceChildren();
    const add = (box, index, suggested=false) => {
      const item = document.createElement("div");
      item.className = suggested ? "cleanup-box suggested" : "cleanup-box";
      item.style.left=box.x+"%"; item.style.top=box.y+"%";
      item.style.width=box.w+"%"; item.style.height=box.h+"%";
      const caption=document.createElement("span");
      caption.textContent = (suggested?
        (box.kind === "possible_logo" ? "Possible logo " : "Text ") : "Remove ") + (index+1);
      item.appendChild(caption);
      if (!suggested) {
        const remove=document.createElement("button");
        remove.type="button"; remove.className="cleanup-delete";
        remove.textContent="×"; remove.setAttribute("aria-label","Remove cleanup box "+(index+1));
        remove.addEventListener("pointerdown",event=>event.stopPropagation());
        remove.addEventListener("click",event=>{
          event.stopPropagation();
          cleanupRegions.splice(index,1);
          invalidateCleanupPreview("Cleanup selection changed. Render a new processed frame.");
          renderCleanupBoxes();
          cleanupInfo();
        });
        item.appendChild(remove);
      }
      layer.appendChild(item);
    };
    cleanupRegions.forEach((box,index)=>add(box,index));
    cleanupSuggestions.forEach((box,index)=>add(box,index,true));
  }

  function invalidateCleanupPreview(message) {
    cleanupRenderRevision++;
    $("cleanupProcessedResult").classList.add("hidden");
    for (const url of [cleanupBeforeImageUrl, cleanupAfterImageUrl]) {
      if (url) URL.revokeObjectURL(url);
    }
    cleanupBeforeImageUrl = null;
    cleanupAfterImageUrl = null;
    $("cleanupBeforeFrame").removeAttribute("src");
    $("cleanupAfterFrame").removeAttribute("src");
    if (message) $("cleanupPreviewNotice").textContent = message;
    $("cleanupPreviewBtn").disabled = cleanupPreviewBusy || !brandBatchFiles.length;
  }

  async function renderActualCleanupFrame() {
    const video = $("cleanupVideo");
    if (!brandBatchFiles.length || !video.videoWidth || video.readyState < 2)
      return toast("Select a video and play or scrub until the picture loads.");
    if (cleanupRegions.length && !$("cleanupAuthorized").checked)
      return toast("Confirm your permission to modify the selected old graphics.");
    if (cleanupPreviewBusy) return;
    cleanupPreviewBusy = true;
    invalidateCleanupPreview("Processing your selected frame with the same cleanup filter used for exports…");
    const revision = cleanupRenderRevision;
    const button = $("cleanupPreviewBtn");
    button.disabled = true;
    button.textContent = "Rendering cleanup…";
    video.pause();
    try {
      const scale = Math.min(1, 1600 / Math.max(video.videoWidth, video.videoHeight));
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(96, Math.round(video.videoWidth * scale));
      canvas.height = Math.max(96, Math.round(video.videoHeight * scale));
      const context = canvas.getContext("2d", {alpha:false});
      if (!context) throw Error("Your browser cannot capture a comparison frame.");
      context.drawImage(video, 0, 0, canvas.width, canvas.height);
      const image = await new Promise(resolve=>canvas.toBlob(resolve,"image/jpeg",.89));
      if (!image) throw Error("Your browser could not capture the current video frame.");
      const form = new FormData();
      form.append("frame", image, "current-frame.jpg");
      form.append("cleanup_regions", JSON.stringify(cleanupRegions));
      form.append("cleanup_authorized", $("cleanupAuthorized").checked ? "true" : "false");
      form.append("brand_id", $("brandOnlyProfile").value);
      form.append("logo_position", $("brandOnlyPosition").value);
      form.append("logo_size", $("brandLogoSize").value);
      form.append("logo_opacity", $("brandLogoOpacity").value);
      form.append("logo_already_embedded", $("brandLogoAlreadyEmbedded").checked ? "true" : "false");
      form.append("crop_top", $("brandCropTop").value);
      form.append("crop_bottom", $("brandCropBottom").value);
      const response = await api("/api/cleanup/preview", {method:"POST",body:form});
      const output = await response.blob();
      if (revision !== cleanupRenderRevision) return;
      const source = URL.createObjectURL(image);
      const after = URL.createObjectURL(output);
      cleanupBeforeImageUrl = source;
      cleanupAfterImageUrl = after;
      $("cleanupBeforeFrame").src = source;
      $("cleanupAfterFrame").src = after;
      $("cleanupProcessedResult").classList.remove("hidden");
      const areas = Number(response.headers.get("X-Cleanup-Applied") || 0);
      $("cleanupPreviewNotice").textContent = areas ?
        "Processed comparison ready: " + areas + " selected area(s) interpolated and channel logo applied. Zoom in on the AFTER image for smearing or leftover graphics. Repeat at different timestamps." :
        "Processed comparison ready, but NO OLD GRAPHICS WERE REMOVED because no cleanup boxes were selected. The new logo alone cannot remove Buho TV.";
      $("cleanupProcessedResult").scrollIntoView({behavior:"smooth",block:"nearest"});
    } catch (error) {
      if (revision === cleanupRenderRevision)
        $("cleanupPreviewNotice").textContent =
          "Cleanup preview failed: " + error.message +
          ". Do not treat the original preview as an edited export.";
    } finally {
      cleanupPreviewBusy = false;
      button.disabled = !brandBatchFiles.length;
      button.textContent = "▶ Preview actual cleaned frame";
    }
  }

  function previewCleanupSource() {
    invalidateCleanupPreview("The video above is the original, not processed. Mark unwanted graphics and press Preview actual cleaned frame.");
    $("cleanupPreviewBtn").disabled = !brandBatchFiles.length;
    if (cleanupVideoUrl) {
      URL.revokeObjectURL(cleanupVideoUrl);
      cleanupVideoUrl=null;
    }
    const video=$("cleanupVideo");
    video.pause(); video.removeAttribute("src"); video.load();
    cleanupRegions=[]; cleanupSuggestions=[]; renderCleanupBoxes();
    $("cleanupApplySuggestions").classList.add("hidden");
    if (!brandBatchFiles.length) {
      $("cleanupVideoShell").classList.add("hidden");
      $("cleanupSeekPanel").classList.add("hidden");
      cleanupInfo("Select your videos to preview existing on-screen text, logos and stickers.");
      $("cleanupSuggest").disabled=true;
      return;
    }
    cleanupVideoUrl=URL.createObjectURL(brandBatchFiles[0]);
    video.src=cleanupVideoUrl;
    $("cleanupVideoShell").classList.remove("hidden");
    $("cleanupSeekPanel").classList.remove("hidden");
    $("cleanupSuggest").disabled=false;
    cleanupInfo("Previewing the first selected video. Scrub the timeline and drag rectangles over old graphics. Do not cover people or important parts of the scene.");
  }
  function setupCleanupEditor() {
    const video=$("cleanupVideo"),layer=$("cleanupDrawLayer"),seek=$("cleanupSeek");
    video.addEventListener("loadedmetadata",()=>{
      if (!video.videoWidth||!video.videoHeight) return;
      $("cleanupVideoShell").style.aspectRatio=video.videoWidth+"/"+video.videoHeight;
      const time=Math.min(0.35, Math.max(0, video.duration-.15));
      if (Number.isFinite(time)) video.currentTime=time;
    });
    video.addEventListener("timeupdate",()=>{
      if (Number.isFinite(video.duration)&&video.duration>0)
        seek.value=String(video.currentTime/video.duration*100);
    });
    seek.addEventListener("input",()=>{
      invalidateCleanupPreview("Video time changed. Preview the cleanup for this new frame.");
      if(Number.isFinite(video.duration)&&video.duration>0)
        video.currentTime=video.duration*Number(seek.value)/100;
    });
    $("cleanupPreviewBtn").addEventListener("click",renderActualCleanupFrame);
    $("cleanupPlay").addEventListener("click",()=>{
      if(video.paused) video.play().catch(()=>{});
      else video.pause();
    });
    function loc(event) {
      const rect=layer.getBoundingClientRect();
      return {x:Math.max(0,Math.min(100,(event.clientX-rect.left)/rect.width*100)),
              y:Math.max(0,Math.min(100,(event.clientY-rect.top)/rect.height*100))};
    }
    layer.addEventListener("pointerdown",event=>{
      if(event.target.closest("button") || !brandBatchFiles.length ||
         cleanupRegions.length>=6) return;
      event.preventDefault();video.pause();
      const start=loc(event);
      cleanupSuggestions=[];
      cleanupDrag={id:event.pointerId,start,now:start};
      layer.setPointerCapture(event.pointerId);
      renderCleanupBoxes();
    });
    layer.addEventListener("pointermove",event=>{
      if(!cleanupDrag || event.pointerId!==cleanupDrag.id) return;
      cleanupDrag.now=loc(event);
      const one=cleanupDrag.start,two=cleanupDrag.now;
      renderCleanupBoxes();
      const draft=document.createElement("div");
      draft.className="cleanup-box draft";
      draft.style.left=Math.min(one.x,two.x)+"%";
      draft.style.top=Math.min(one.y,two.y)+"%";
      draft.style.width=Math.abs(one.x-two.x)+"%";
      draft.style.height=Math.abs(one.y-two.y)+"%";
      layer.appendChild(draft);
    });
    function finish(event) {
      if(!cleanupDrag || event.pointerId!==cleanupDrag.id) return;
      const one=cleanupDrag.start,two=loc(event);
      cleanupDrag=null;
      const box={x:Math.round(Math.min(one.x,two.x)*100)/100,
                 y:Math.round(Math.min(one.y,two.y)*100)/100,
                 w:Math.round(Math.abs(one.x-two.x)*100)/100,
                 h:Math.round(Math.abs(one.y-two.y)*100)/100};
      if(allowedCleanupBox(box) &&
         cleanupRegions.reduce((n,x)=>n+x.w*x.h,0)+box.w*box.h<=3000) {
        cleanupRegions.push(box);
        invalidateCleanupPreview("Cleanup region changed. Press Preview actual cleaned frame to see the new result.");
        cleanupInfo("Area marked for cleanup. Review its placement throughout the video before generating.");
      } else {
        cleanupInfo("Draw a rectangle at least 2% wide and tall, max 65% width/40% height. Large masks can damage the scene.");
      }
      renderCleanupBoxes();
    }
    layer.addEventListener("pointerup",finish);
    layer.addEventListener("pointercancel",()=>{
      cleanupDrag=null;renderCleanupBoxes();
    });
    $("cleanupClear").addEventListener("click",()=>{
      invalidateCleanupPreview("No boxes selected. No source graphics will be removed.");
      cleanupRegions=[];cleanupSuggestions=[];
      $("cleanupApplySuggestions").classList.add("hidden");
      renderCleanupBoxes();cleanupInfo("All removal areas cleared. Your source footage is unchanged.");
    });
    $("cleanupSuggest").addEventListener("click",async ()=>{
      const file=brandBatchFiles[0];
      if (!file) return toast("Select a video first.");
      const button=$("cleanupSuggest");
      button.disabled=true;
      cleanupInfo("Analyzing a few frames for persistent on-screen text. This may miss logos; all suggested removals require your approval.");
      try{
        const data=new FormData();data.append("video",file,file.name);
        const result=await (await api("/api/cleanup/suggest",{method:"POST",body:data})).json();
        const textMasks=(result.regions||[]).filter(allowedCleanupBox)
          .map(box=>({...box,kind:"text"}));
        const imageMasks=(result.review_candidates||[]).filter(allowedCleanupBox)
          .map(box=>({...box,kind:"possible_logo"}));
        cleanupSuggestions=[...textMasks,...imageMasks]
          .filter((box,index,array)=>!array.slice(0,index).some(prev=>
            Math.abs(prev.x-box.x)<7 && Math.abs(prev.y-box.y)<7))
          .slice(0,6);
        renderCleanupBoxes();
        $("cleanupApplySuggestions").classList.toggle("hidden",!cleanupSuggestions.length);
        cleanupInfo(cleanupSuggestions.length?
          textMasks.length+" likely text area(s) plus "+imageMasks.length+
          " possible stationary-graphic area(s). Review each box before applying. Static real-world objects and faces can also trigger suggestions.":
          "No confident old-branding areas found. That does NOT certify the source as clean; scrub the video and draw boxes if needed.");
      }catch(error) {
        cleanupInfo("Automatic detection wasn't reliable: "+error.message+". Draw boxes manually.");
      }finally{button.disabled=false}
    });
    $("cleanupApplySuggestions").addEventListener("click",()=>{
      const accepted=cleanupSuggestions.slice(0,6-cleanupRegions.length);
      for(const box of accepted) {
        if(cleanupRegions.reduce((n,r)=>n+r.w*r.h,0)+box.w*box.h<=3000)
          cleanupRegions.push(box);
      }
      cleanupSuggestions=[];
      invalidateCleanupPreview("Suggested areas changed. Press Preview actual cleaned frame.");
      $("cleanupApplySuggestions").classList.add("hidden");
      renderCleanupBoxes();
      cleanupInfo("Suggested areas added. Check the full video before publishing; cleanup is not perfect.");
    });
  }


  async function cleanAndRebrandSaved() {
    const job = jobs.find(item => item.id === selectedJobId);
    if (!job || job.status !== "completed")
      return toast("Open a saved completed video first.");
    if (!$("savedCleanPermission").checked)
      return toast("Confirm you have permission to rebrand this source footage.");
    const source = $("savedCleanSource").files[0];
    if (!job.source_available && !source)
      return toast("Choose the original video from your device.");
    const button = $("savedCleanBtn");
    button.disabled = true;
    $("savedCleanState").textContent = "Creating a separate project; scanning old graphics from the original upload…";
    const form = new FormData();
    form.append("source_rights_confirmed", "true");
    form.append("auto_cleanup", "true");
    if (source) form.append("video", source, source.name);
    try {
      const result = await (await api(
        "/api/jobs/" + encodeURIComponent(job.id) + "/clean-rebrand",
        {method:"POST",body:form}
      )).json();
      $("savedCleanSource").value = "";
      $("savedCleanState").textContent = "New project " + result.id + " queued.";
      await refreshJobs();
      showPage("library", false);
      $("pageLibrary").scrollIntoView({behavior:"smooth",block:"start"});
      toast("New Bajo Mundo TV edit queued. Check My exports for cleanup review.");
    } catch(error) {
      $("savedCleanState").textContent = error.message;
      toast("Unable to rebrand saved source: " + error.message);
    } finally {
      button.disabled = false;
    }
  }

  async function uploadFullVideoLogo(file) {
    if (!file) return;
    const button = $("brandCustomLogoBtn");
    const status = $("brandCustomLogoStatus");
    if (brandBatchBusy) return toast("Finish the current batch upload before changing the logo.");
    if (!/\.(png|jpe?g|webp)$/i.test(file.name) || file.size <= 0 || file.size > 8 * 1048576) {
      status.textContent = "Choose a valid PNG, JPG or WEBP image no larger than 8 MB.";
      return toast(status.textContent);
    }
    button.disabled = true;
    button.textContent = "Uploading logo…";
    status.textContent = "Saving your logo so it can be reused on future videos…";
    try {
      const name = "Custom logo · " + file.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim().slice(0, 40);
      const form = new FormData();
      form.append("name", name);
      form.append("logo", file, file.name);
      const response = await api("/api/brands", {method: "POST", body: form});
      const saved = await response.json();
      await loadBrands(saved.id);
      $("brandOnlyProfile").value = saved.id;
      $("brandLogoAlreadyEmbedded").checked = false;
      invalidateCleanupPreview("Custom logo changed. Render a new processed preview frame.");
      await updateFullVideoPreview(true);
      status.textContent = "✓ " + saved.name + " saved and selected for all new uploads in this batch.";
      toast("Custom logo ready. It will appear on new video exports.");
    } catch (error) {
      status.textContent = "Couldn't save logo: " + error.message;
      toast(status.textContent);
    } finally {
      $("brandCustomLogoFile").value = "";
      button.disabled = false;
      button.textContent = "＋ Upload logo image";
    }
  }

  function pickBrandBatchFiles(files) {
    if (brandBatchBusy) return;
    const all = Array.from(files || []);
    brandBatchFiles = all.filter(file =>
      /\.(mp4|mov|webm|mkv|m4v)$/i.test(file.name) && file.size > 0 &&
      (!config || file.size <= config.max_upload_mb * 1048576));
    const count = brandBatchFiles.length;
    $("brandBatchFilesTitle").textContent = count ?
      count + (count === 1 ? " full video selected" : " full videos selected") :
      "Choose videos to brand";
    $("brandBatchFilesDetail").textContent = count ?
      brandBatchFiles.slice(0,3).map(file => file.name).join(" · ") +
        (count > 3 ? " · and " + (count - 3) + " more" : "") :
      "One video or a whole batch · MP4, MOV, MKV, WEBM, M4V";
    const textOptions = $("brandExistingTextOptions");
    textOptions.replaceChildren();
    brandBatchFiles.forEach((file, index) => {
      const label = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = String(index);
      label.append(checkbox, file.name);
      textOptions.append(label);
    });
    $("brandBatchStatus").textContent = count ?
      "Ready for full-length video. Selected speech subtitles may take longer and need proofreading." :
      "Choose valid video files under the per-video size limit.";
    if (all.length > count) toast((all.length-count) + " unsupported or oversized file(s) skipped.");
    updateBrandBatchButton();
    updateFullVideoPreview();
    previewCleanupSource();
  }
  function updateBrandBatchButton() {
    const b = $("brandBatchSubmit");
    const textMode = $("brandSubtitleMode").value;
    const textOptions = [...$("brandExistingTextOptions").querySelectorAll("input")];
    const keepsText = textMode === "existing";
    $("brandExistingTextFiles").classList.toggle("hidden", !textOptions.length || keepsText);
    $("brandExistingTextFiles").disabled = brandBatchBusy;
    $("brandSubtitleStyle").disabled = textMode !== "auto";
    $("brandFollowBanner").disabled = keepsText;
    const needsAutoCleanup = !keepsText && textOptions.some(option => !option.checked) &&
      $("autoCleanup").checked;
    b.disabled = brandBatchBusy || !brandBatchFiles.length ||
      !$("brandRightsAcknowledged").checked ||
      (needsAutoCleanup && !$("cleanupAuthorized").checked);
    b.textContent = brandBatchBusy ? "Uploading batch…" :
      "Brand " + (brandBatchFiles.length || "full") +
      (brandBatchFiles.length === 1 ? " video ↗" : " full videos ↗");
  }
  async function submitBrandBatch() {
    if (brandBatchBusy || !brandBatchFiles.length) return;
    if (!$("brandRightsAcknowledged").checked) return toast("Acknowledge the publishing-rights notice.");
    const top = Number($("brandCropTop").value), bottom = Number($("brandCropBottom").value);
    if (![top,bottom].every(n => Number.isInteger(n) && n >= 0 && n <= 25) || top + bottom > 40)
      return toast("Crop 0–25% per edge, maximum 40% combined.");
    const textMode = $("brandSubtitleMode").value;
    const autoCleanup = $("autoCleanup").checked;
    const existingTextFiles = new Set(Array.from(
      $("brandExistingTextOptions").querySelectorAll("input:checked"), option => Number(option.value)
    ));
    const queue = brandBatchFiles.map((file, index) => ({
      file, keepExistingText: textMode === "existing" || existingTextFiles.has(index)
    }));
    if ((cleanupRegions.length || (autoCleanup && queue.some(item => !item.keepExistingText))) &&
        !$("cleanupAuthorized").checked)
      return toast("Confirm permission to remove old on-screen graphics before continuing.");
    brandBatchBusy = true; updateBrandBatchButton();
    const total = queue.length, failures = [];
    let sent = 0, processed = 0;
    async function worker() {
      while (queue.length) {
        const {file, keepExistingText} = queue.shift();
        const form = new FormData();
        form.append("video", file, file.name);
        form.append("brand_id", $("brandOnlyProfile").value);
        form.append("logo_position", $("brandOnlyPosition").value);
        form.append("logo_size", $("brandLogoSize").value);
        form.append("logo_opacity", $("brandLogoOpacity").value);
        form.append("logo_already_embedded", $("brandLogoAlreadyEmbedded").checked ? "true" : "false");
        form.append("subtitle_mode", keepExistingText ? "none" : textMode);
        form.append("subtitle_style", $("brandSubtitleStyle").value);
        form.append("follow_banner", String(!keepExistingText && $("brandFollowBanner").checked));
        form.append("keep_existing_text", String(keepExistingText));
        form.append("performance_permission", $("brandPerformancePermission").value);
        form.append("release_reference", $("brandReleaseReference").value);
        form.append("cleanup_regions", JSON.stringify(cleanupRegions));
        form.append("cleanup_authorized", $("cleanupAuthorized").checked ? "true":"false");
        form.append("auto_cleanup", String(!keepExistingText && autoCleanup));
        form.append("crop_top", String(top));
        form.append("crop_bottom", String(bottom));
        form.append("rights", $("brandOnlyRights").value);
        form.append("audio_rights", $("brandOnlyAudio").value);
        form.append("originality", $("brandOnlyOriginality").value);
        form.append("rights_acknowledged", "true");
        try {
          await api("/api/brand-only", {method:"POST", body:form}); sent++;
        } catch (error) { failures.push(file.name + ": " + error.message); }
        processed++;
        $("brandBatchStatus").textContent =
          "Uploaded " + processed + " / " + total + " · " + sent +
          " accepted · " + failures.length + " failed. Rendering continues in My exports.";
      }
    }
    await Promise.all(Array.from({length:Math.min(2,total)}, () => worker()));
    brandBatchBusy = false; updateBrandBatchButton();
    $("brandBatchStatus").textContent = failures.length ?
      sent + " accepted; " + failures.length + " failed: " + failures.slice(0,2).join(" · ") :
      sent + " complete videos queued. Monitor and download each MP4 under My exports.";
    if (sent) {
      await refreshJobs().catch(() => {});
      showPage("library", false);
      $("pageLibrary").scrollIntoView({behavior:"smooth",block:"start"});
      toast(sent + " full videos accepted for branding. No clip cutting.");
    }
  }
  function setupBrandOnly() {
    setupCleanupEditor();
    $("autoCleanup").addEventListener("change",updateBrandBatchButton);
    $("cleanupAuthorized").addEventListener("change",updateBrandBatchButton);
    $("brandCustomLogoBtn").addEventListener("click", () => $("brandCustomLogoFile").click());
    $("brandCustomLogoFile").addEventListener("change", event => {
      uploadFullVideoLogo(event.target.files?.[0]);
    });
    $("brandOnlyProfile").addEventListener("change", () => {
      invalidateCleanupPreview("Channel logo changed. Render a new processed frame.");
      updateFullVideoPreview(true);
    });
    for (const id of ["brandOnlyPosition", "brandLogoSize", "brandLogoOpacity",
                      "brandLogoAlreadyEmbedded", "brandSubtitleMode",
                      "brandSubtitleStyle", "brandFollowBanner"]) {
      $(id).addEventListener("input", () => {
        if (["brandOnlyPosition","brandLogoSize","brandLogoOpacity","brandLogoAlreadyEmbedded"].includes(id))
          invalidateCleanupPreview("Logo settings changed. Reprocess the preview frame.");
        updateFullVideoPreview(false);
      });
      $(id).addEventListener("change", () => updateFullVideoPreview(false));
    }
    for (const id of ["brandCropTop", "brandCropBottom"]) {
      $(id).addEventListener("input",()=>invalidateCleanupPreview("Crop settings changed. Preview the revised frame."));
    }
    $("brandBatchBrowse").addEventListener("click", e => {
      e.stopPropagation(); $("brandBatchFiles").click();
    });
    $("brandBatchDrop").addEventListener("click", e => {
      if (!e.target.closest("button")) $("brandBatchFiles").click();
    });
    $("brandBatchDrop").addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault(); $("brandBatchFiles").click();
      }
    });
    $("brandBatchFiles").addEventListener("change", e => pickBrandBatchFiles(e.target.files));
    $("brandSubtitleMode").addEventListener("change", updateBrandBatchButton);
    $("brandExistingTextOptions").addEventListener("change", () => {
      updateBrandBatchButton();
      updateFullVideoPreview();
    });
    $("brandRightsAcknowledged").addEventListener("change", updateBrandBatchButton);
    for (const ev of ["dragenter","dragover"]) {
      $("brandBatchDrop").addEventListener(ev, e => {
        e.preventDefault(); $("brandBatchDrop").classList.add("dragging");
      });
    }
    $("brandBatchDrop").addEventListener("dragleave", e => {
      e.preventDefault(); $("brandBatchDrop").classList.remove("dragging");
    });
    $("brandBatchDrop").addEventListener("drop", e => {
      e.preventDefault(); $("brandBatchDrop").classList.remove("dragging");
      pickBrandBatchFiles(e.dataTransfer.files);
    });
    $("brandBatchSubmit").addEventListener("click", submitBrandBatch);
    updateBrandBatchButton();
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
      const job = jobs.find(j => j.id === selectedJobId);
      if (job?.mode === "brand_only" && job.clips?.[0]?.file)
        download(job.id, job.clips[0].file);
      else if (job?.zip_file) download(job.id, job.zip_file);
    });
    $("stagePlay").addEventListener("click", playSelectedClip);
    $("savedCleanBtn").addEventListener("click",cleanAndRebrandSaved);
    $("reframeBtn").addEventListener("click", createFocusedVersion);
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
      // Explicitly enter NEW PROJECT instead of leaving the last exported video
      // in front of the form. The 3.5-second poll must not reopen it.
      showingNewProject = true;
      selectedJobId = null;
      renderedResultsKey = "";
      $("results").classList.add("hidden");
      $("activePanel").classList.add("hidden");
      document.body.classList.remove("has-results");
      document.body.classList.add("show-editor");
      resetSource();
      showPage("create", false);
      $("brandQuick").scrollIntoView({behavior:"smooth",block:"start"});
    };
    $("newProjectBtn").addEventListener("click", openEditor);
    $("refreshBtn").addEventListener("click", () => refreshJobs().catch((e) => toast(e.message)));
    $("navCreate").addEventListener("click", openEditor);
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
    $("pageCrumb").textContent = createPage ?
      (showingNewProject ? "NEW PROJECT" : "PROJECT RESULTS") : "MY EXPORTS";
    if (shouldScroll) window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function init() {
    setupUploads();
    setupBrandOnly();
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
