// Copyright (c) 2026 ZyvorAI Labs Private Limited. SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
(function () {
  var root = document.documentElement;
  var btn = document.getElementById("theme");
  function safeGet() { try { return localStorage.getItem("argus-theme"); } catch (e) { return null; } }
  function safeSet(v) { try { localStorage.setItem("argus-theme", v); } catch (e) {} }
  var saved = safeGet();
  if (saved) root.setAttribute("data-theme", saved);
  if (btn) btn.addEventListener("click", function () {
    var dark = root.getAttribute("data-theme") === "dark" ||
      (!root.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
    var next = dark ? "light" : "dark";
    root.setAttribute("data-theme", next); safeSet(next);
  });
  document.querySelectorAll("[data-copy]").forEach(function (b) {
    b.addEventListener("click", function () {
      var t = document.getElementById(b.getAttribute("data-copy")).innerText;
      navigator.clipboard.writeText(t).then(function () {
        b.textContent = "Copied ✓"; setTimeout(function () { b.textContent = "Copy"; }, 1800);
      }, function () {});
    });
  });
  var els = document.querySelectorAll(".rv");
  if (!("IntersectionObserver" in window)) { els.forEach(function (e) { e.classList.add("in"); }); return; }
  var io = new IntersectionObserver(function (es) {
    es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } });
  }, { threshold: 0.12 });
  els.forEach(function (e) { io.observe(e); });
})();
