const form = document.querySelector("#extract-form");
const input = document.querySelector("#media-url");
const button = document.querySelector("#extract-button");
const message = document.querySelector("#message");
const result = document.querySelector("#result");

function showMessage(text, kind = "") {
  message.textContent = text;
  message.className = `message ${kind}`;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function formatSize(bytes) {
  if (bytes == null || bytes === "") return "";
  const n = Number(bytes);
  if (!Number.isFinite(n) || n <= 0) return "";
  return n < 1024 * 1024
    ? ` (${Math.max(1, Math.round(n / 1024))} KB)`
    : ` (${(n / 1024 / 1024).toFixed(1)} MB)`;
}

function buildPreview(format) {
  let media;
  if (format.format === "mp4") {
    media = document.createElement("video");
    media.src = format.url;
    media.muted = true;
    media.loop = true;
    media.autoplay = true;
    media.controls = false;
    media.setAttribute("playsinline", "");
    media.setAttribute("referrerpolicy", "no-referrer");
  } else {
    media = document.createElement("img");
    media.src = format.url;
    media.alt = "Animation preview";
    media.setAttribute("referrerpolicy", "no-referrer");
  }
  return media;
}

function renderResult(data) {
  result.replaceChildren();

  const card = el("div", "result-card");
  const stage = el("div", "preview");
  stage.append(el("p", "preview-loading", "Loading preview…"));

  const previewFormat =
    data.formats.find((f) => f.format === "mp4") || data.formats[0];
  const media = buildPreview(previewFormat);

  const showFail = () => {
    stage.replaceChildren(
      el(
        "p",
        "preview-fail",
        "Preview unavailable (blocked or missing), but you can still download."
      )
    );
  };

  media.addEventListener("error", showFail);
  media.addEventListener("loadeddata", () => {
    stage.replaceChildren(media);
  });
  // Images fire load, not loadeddata
  media.addEventListener("load", () => {
    stage.replaceChildren(media);
  });

  // Attach media; if it is already cached it may have loaded before listeners
  stage.append(media);
  if (media.tagName === "IMG" && media.complete && media.naturalWidth > 0) {
    stage.replaceChildren(media);
  }

  const meta = el("div", "result-meta");
  const badges = el("div", "result-badges");
  badges.append(el("span", "result-platform", data.platform.toUpperCase()));
  for (const f of data.formats) {
    badges.append(el("span", "result-format", f.format.toUpperCase()));
  }
  meta.append(badges);
  if (data.title) meta.append(el("p", "result-title", data.title));

  const actions = el("div", "actions");
  for (const f of data.formats) {
    const link = el(
      "a",
      "download",
      `Download ${f.format.toUpperCase()}${formatSize(f.size)}`
    );
    link.href = `/api/file?url=${encodeURIComponent(f.url)}&name=${encodeURIComponent(
      `${data.name}.${f.format}`
    )}`;
    link.setAttribute("download", `${data.name}.${f.format}`);
    link.rel = "noopener";
    actions.append(link);
  }

  card.append(stage, meta, actions);
  result.append(card);
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  const url = input.value.trim();
  if (!url) {
    showMessage("Paste a link first.", "error");
    return;
  }

  result.replaceChildren();
  button.disabled = true;
  button.textContent = "Extracting...";
  showMessage("Looking for the animation…");

  try {
    const response = await fetch("/api/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });

    const data = await response.json();

    if (!response.ok) {
      const detail = Array.isArray(data.detail)
        ? "Please enter a valid web link."
        : data.detail || "Something went wrong.";
      showMessage(detail, "error");
    } else if (!data.formats || !data.formats.length) {
      showMessage("No downloadable formats were returned.", "error");
    } else {
      showMessage("Animation found. Preview and download below.", "success");
      renderResult(data);
    }
  } catch {
    showMessage("Couldn't reach the server. Please try again.", "error");
  } finally {
    button.disabled = false;
    button.innerHTML = 'Extract <span aria-hidden="true">↗</span>';
  }
});
