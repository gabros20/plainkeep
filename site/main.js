(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------- Sticky header background ---------- */
  var header = document.getElementById("site-header");
  function updateHeader() {
    if (window.scrollY > 8) header.classList.add("is-scrolled");
    else header.classList.remove("is-scrolled");
  }
  updateHeader();
  window.addEventListener("scroll", updateHeader, { passive: true });

  /* ---------- Scroll reveal ---------- */
  var revealEls = Array.prototype.slice.call(document.querySelectorAll(".reveal"));
  if (reduceMotion || !("IntersectionObserver" in window)) {
    revealEls.forEach(function (el) { el.classList.add("is-visible"); });
  } else {
    var revealObserver = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add("is-visible");
            revealObserver.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.12, rootMargin: "0px 0px -60px 0px" }
    );
    revealEls.forEach(function (el) { revealObserver.observe(el); });
  }

  /* ---------- Prompt gutter: markers + progress ---------- */
  var markers = Array.prototype.slice.call(document.querySelectorAll(".prompt-marker"));
  var sections = markers
    .map(function (m) { return document.getElementById(m.getAttribute("data-target")); })
    .filter(Boolean);

  markers.forEach(function (m) {
    m.addEventListener("click", function () {
      var target = document.getElementById(m.getAttribute("data-target"));
      if (target) {
        target.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
      }
    });
  });

  var gutterFill = document.getElementById("gutter-fill");
  var gutterRail = document.getElementById("gutter-rail");

  function updateGutter() {
    var doc = document.documentElement;
    var scrollTop = window.scrollY || doc.scrollTop;
    var scrollHeight = (doc.scrollHeight - window.innerHeight) || 1;
    var progress = Math.min(1, Math.max(0, scrollTop / scrollHeight));

    if (gutterFill) gutterFill.style.height = (progress * 100).toFixed(2) + "%";
    if (gutterRail) gutterRail.style.width = (progress * 100).toFixed(2) + "%";

    var viewportMid = scrollTop + window.innerHeight * 0.4;
    var passedIndex = -1;
    sections.forEach(function (sec, i) {
      if (sec.offsetTop <= viewportMid) passedIndex = i;
    });
    markers.forEach(function (m, i) {
      m.classList.toggle("is-passed", i <= passedIndex);
    });
  }

  updateGutter();
  window.addEventListener("scroll", updateGutter, { passive: true });
  window.addEventListener("resize", updateGutter);

  /* ---------- Copy to clipboard ---------- */
  var copyButtons = Array.prototype.slice.call(document.querySelectorAll("[data-copy]"));
  copyButtons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy") || "";
      var done = function () {
        var original = btn.textContent;
        btn.textContent = "Copied";
        window.setTimeout(function () { btn.textContent = original; }, 1600);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, done);
      } else {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); } catch (e) { /* clipboard unsupported — silently no-op */ }
        document.body.removeChild(ta);
        done();
      }
    });
  });

  /* ---------- Terminal panel overflow affordance ----------
     Real command output must never silently clip: panels that need horizontal
     scroll get a visible right-edge fade + a styled scrollbar. */
  var panelBodies = Array.prototype.slice.call(document.querySelectorAll(".terminal-panel__body"));
  function updatePanel(el) {
    var scrollable = el.scrollWidth > el.clientWidth + 1;
    var atEnd = el.scrollLeft + el.clientWidth >= el.scrollWidth - 2;
    var panel = el.closest(".terminal-panel");
    if (panel) panel.classList.toggle("has-fade", scrollable && !atEnd);
  }
  function updateScrollAffordance() { panelBodies.forEach(updatePanel); }
  panelBodies.forEach(function (el) {
    el.addEventListener("scroll", function () { updatePanel(el); }, { passive: true });
  });
  updateScrollAffordance();
  window.addEventListener("resize", updateScrollAffordance);
  window.addEventListener("load", updateScrollAffordance);

  /* ---------- Played terminal sessions ----------
     The markup contains the COMPLETE session (static-complete without JS, and
     under prefers-reduced-motion). With JS + motion, the session is hidden and
     replayed: command lines type themselves, output lines print in order. */
  var players = Array.prototype.slice.call(document.querySelectorAll(".term-play"));
  players.forEach(function (panel) {
    var pre = panel.querySelector("pre");
    if (!pre) return;
    var lines = Array.prototype.slice.call(pre.querySelectorAll(".tp-line"));
    if (!lines.length) return;
    var replayBtn = panel.querySelector(".term-play__replay");

    if (reduceMotion || !("IntersectionObserver" in window)) {
      if (replayBtn) replayBtn.hidden = true;
      return;
    }

    var body = panel.querySelector(".terminal-panel__body");
    if (body) body.style.minHeight = body.offsetHeight + "px";

    var caret = document.createElement("span");
    caret.className = "tp-caret";
    var runToken = 0;

    lines.forEach(function (l) {
      if (l.hasAttribute("data-cmd")) l.dataset.full = l.textContent;
    });

    function hideAll() {
      lines.forEach(function (l) {
        l.hidden = true;
        if (l.hasAttribute("data-cmd")) l.textContent = "";
      });
    }

    function play() {
      var token = ++runToken;
      hideAll();
      if (replayBtn) replayBtn.hidden = true;
      var i = 0;

      function next() {
        if (token !== runToken) return;
        if (i >= lines.length) {
          if (caret.parentNode) caret.parentNode.removeChild(caret);
          if (replayBtn) replayBtn.hidden = false;
          return;
        }
        var line = lines[i++];
        line.hidden = false;
        if (line.hasAttribute("data-cmd")) {
          var full = line.dataset.full || "";
          var pos = 0;
          line.appendChild(caret);
          (function typeChar() {
            if (token !== runToken) return;
            if (pos < full.length) {
              pos += 1;
              line.textContent = full.slice(0, pos);
              line.appendChild(caret);
              window.setTimeout(typeChar, 24 + Math.floor(18 * ((pos * 7) % 5) / 5));
            } else {
              window.setTimeout(next, 320);
            }
          })();
        } else {
          window.setTimeout(next, line.textContent.trim() === "" ? 30 : 90);
        }
      }
      next();
    }

    var seen = false;
    var obs = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting && !seen) {
          seen = true;
          obs.unobserve(panel);
          window.setTimeout(play, 350);
        }
      });
    }, { threshold: 0.35 });
    obs.observe(panel);

    if (replayBtn) replayBtn.addEventListener("click", play);
  });

  /* ---------- Command anatomy: hover/focus a part, its legend card lifts ---------- */
  var anatomies = Array.prototype.slice.call(document.querySelectorAll(".anatomy"));
  anatomies.forEach(function (root) {
    var all = Array.prototype.slice.call(root.querySelectorAll("[data-part]"));
    function activate(part) {
      all.forEach(function (el) {
        el.classList.toggle("is-active", part !== null && el.getAttribute("data-part") === part);
      });
    }
    all.forEach(function (el) {
      ["mouseenter", "focus", "click"].forEach(function (ev) {
        el.addEventListener(ev, function () { activate(el.getAttribute("data-part")); });
      });
    });
    root.addEventListener("mouseleave", function () { activate(null); });
  });
})();
