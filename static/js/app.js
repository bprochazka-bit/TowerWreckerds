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

// Collapsible panels: click a panel's header to fold it away. State persists
// per page in localStorage so collapses stick. Progressive enhancement — the
// markup works fine with JS disabled (panels are just always open).
(function () {
  function storeKey(label) {
    return "mw:collapse:" + location.pathname + ":" + label;
  }
  document.querySelectorAll(".panel").forEach(function (panel) {
    const head = panel.firstElementChild;
    if (!head || !head.classList.contains("strip-label")) return;
    // Don't make a panel collapsible if its header is the only thing in it.
    if (!head.nextSibling) return;

    const body = document.createElement("div");
    body.className = "panel-body";
    while (head.nextSibling) body.appendChild(head.nextSibling);
    panel.appendChild(body);
    head.classList.add("panel-toggle");

    const key = storeKey((head.textContent || "").trim());
    try {
      if (localStorage.getItem(key) === "1") panel.classList.add("collapsed");
    } catch (e) { /* localStorage may be unavailable */ }

    head.addEventListener("click", function () {
      const collapsed = panel.classList.toggle("collapsed");
      try { localStorage.setItem(key, collapsed ? "1" : "0"); } catch (e) {}
    });
  });
})();
