// UI contract checks: keeps designer changes from silently breaking controls.
const fs = require("fs");
const assert = require("assert");
const html = fs.readFileSync("static/index.html", "utf8");
const script = fs.readFileSync("static/app.js", "utf8");
const css = fs.readFileSync("static/style.css", "utf8");
const backend = fs.readFileSync("app.py", "utf8");

const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
assert.strictEqual(ids.length, new Set(ids).size, "HTML has duplicate element ids");
const idsUsed = [...script.matchAll(/\$\("([^"]+)"\)/g)].map(m => m[1]);
for (const id of idsUsed) assert.ok(ids.includes(id), "JS expects missing element: " + id);

for (const id of ["results", "clipsGrid", "clipQueue", "featuredVideo", "featuredPoster",
  "stageBackdrop", "stagePlay", "stageDownload", "stageCoverDownload", "stageCopy",
  "newProjectBtn", "activePanel", "createForm", "editorHeadline", "preflightPanel", "preflightChecks", "preflightState",
  "reviewPanel", "reviewStatus", "reframeBtn", "reframeFocus", "reframePermission", "reframeStatus",
  "brandQuick", "brandBatchFiles", "brandBatchDrop", "brandBatchBrowse", "brandOnlyProfile",
  "brandBatchSubmit", "brandRightsAcknowledged", "brandBatchStatus", "brandOnlyPosition",
  "brandCropTop", "brandCropBottom", "brandOnlyRights", "brandOnlyAudio", "brandOnlyOriginality",
  "resultsTitle", "galleryTitle", "reframeTool", "watermarkPreviewImg",
  "watermarkDemo", "brandLogoSize", "brandLogoOpacity",
  "brandLogoSizeValue", "brandLogoOpacityValue", "brandLogoAlreadyEmbedded",
  "watermarkPreviewMessage", "brandSubtitleMode", "brandSubtitleStyle",
  "brandFollowBanner", "showBannerDemo", "showCaptionDemo",
  "brandPerformancePermission", "brandReleaseReference", "cleanupVideo",
  "cleanupDrawLayer", "cleanupVideoShell", "cleanupSeek", "cleanupSeekPanel",
  "cleanupPlay", "cleanupSuggest", "cleanupClear", "cleanupApplySuggestions",
  "cleanupAuthorized", "cleanupInfo", "cleanupGraphics"]) {
  assert.ok(ids.includes(id), "Review studio lacks: " + id);
}

assert.ok(script.includes("/thumbnails/"), "UI does not load thumbnail endpoint");
assert.ok(script.includes("thumbnailUrl("), "Thumbnail loader missing");
assert.ok(script.includes("clip.file"), "Clip identity not preserved");
assert.ok(script.includes("data-select"), "Playlist/gallery selection missing");
assert.ok(script.includes("playSelectedClip"), "Video preview interaction missing");
assert.ok(script.includes("updatePreflight"), "Pre-render checks must run before generation");
assert.ok(script.includes("submitBrandBatch"), "Full-length branding flow must be wired");
assert.ok(backend.includes('@app.post("/api/brand-only"'), "Full-length branding API is missing");
assert.ok(script.includes("brandBatchFiles"), "Brand batch input missing");
assert.ok(script.includes("createFocusedVersion"), "Existing clips must expose the reframe action");
assert.ok(backend.includes('@app.post("/api/jobs/{job_id}/reframe"'), "Reframe API is missing");
assert.ok(html.indexOf('class="review-stage"') < html.indexOf('id="reviewPanel"'), "Video preview should precede long publishing warnings");
assert.ok(script.includes("URL.revokeObjectURL"), "Blob URL cleanup missing");
assert.ok(backend.includes('@app.get("/api/jobs/{job_id}/thumbnails/{clip_filename}")'), "Thumbnail API missing");
assert.ok(backend.includes("protect(x_access_key)"), "Authentication guard missing");
assert.ok(css.includes(".clip-cover img.thumb-loaded"), "Real thumbnail card styles missing");
assert.ok(css.includes(".review-stage"), "Review studio styles missing");

console.log("UI contract valid: unique DOM IDs, wired controls, secure thumbnail endpoint and player.");

assert.ok(script.includes("let showingNewProject = true"), "Brand uploader should be the default when loading the site");
assert.ok(script.includes('showingNewProject = true;\n      selectedJobId = null;'), "New project must clear old results selection");
assert.ok(script.includes('document.body.classList.remove("has-results")'), "New project must reveal brand uploader, not keep results-only CSS");
assert.ok(script.includes('if (!showingNewProject) {'), "Polling must not replace the new uploader with an old export");
assert.ok(html.includes('Brand full videos. Automatically.'), "Batch branding section must be visible in new-project flow");

assert.ok(script.includes("updateFullVideoPreview"), "Watermark preview not wired");
assert.ok(script.includes('form.append("logo_size"'), "Size control not passed to backend");
assert.ok(script.includes('form.append("logo_opacity"'), "Opacity control not passed to backend");
assert.ok(script.includes('form.append("logo_already_embedded"'), "Double logo prevention missing");
assert.ok(backend.includes('"default": "bajo-mundo"'), "Original Bajo Mundo logo must be default");

assert.ok(script.includes('form.append("subtitle_mode"'), "Subtitle choice must reach renderer");
assert.ok(script.includes('form.append("follow_banner"'), "Follow banner choice missing");
assert.ok(script.includes('form.append("performance_permission"'), "Artist permission review missing");
assert.ok(backend.includes('performance_permission: str = Form("unknown")'), "Performance permission not checked");
assert.ok(html.includes('Bajo Mundo TV · Original logo'), "Original channel logo must remain default");

assert.ok(script.includes("renderCleanupBoxes"), "Visual cleanup box drawing must be wired");
assert.ok(script.includes('form.append("cleanup_regions"'), "Cleanup geometry must reach the backend");
assert.ok(script.includes('form.append("cleanup_authorized"'), "Cleanup rights confirmation must reach the backend");
assert.ok(script.includes("/api/cleanup/suggest"), "Smart OCR suggestion endpoint not wired");
assert.ok(backend.includes('@app.post("/api/cleanup/suggest")'), "Smart overlay suggestions route missing");
