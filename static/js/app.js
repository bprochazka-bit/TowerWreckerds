// Music World — minimal UI helpers (vendored, no external deps)

// Show a "working" overlay while long generation/render POSTs are in flight,
// so the user gets feedback during LLM and audio calls.
(function () {
  function ensureOverlay() {
    let o = document.getElementById("overlay");
    if (o) return o;
    o = document.createElement("div");
    o.id = "overlay";
    o.innerHTML =
      '<div class="box"><div class="ring"></div>' +
      '<div class="msg" id="overlay-msg">Working</div>' +
      '<div class="sub2">talking to the backends — this can take a moment</div></div>';
    document.body.appendChild(o);
    return o;
  }

  document.addEventListener("submit", function (e) {
    const form = e.target;
    if (!form.classList || !form.classList.contains("gen-form")) return;
    const o = ensureOverlay();
    const label = form.getAttribute("data-working") || "Working";
    document.getElementById("overlay-msg").textContent = label;
    o.classList.add("on");
  });
})();

// Highlight the active nav item based on the current path prefix.
(function () {
  const path = window.location.pathname;
  document.querySelectorAll(".rail nav a").forEach(function (a) {
    const href = a.getAttribute("href");
    if (href === "/" ? path === "/" : path.startsWith(href)) {
      a.classList.add("active");
    }
  });
})();
