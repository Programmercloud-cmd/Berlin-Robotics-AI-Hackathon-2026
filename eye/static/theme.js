"use strict";

(() => {
  const root = document.documentElement;
  const system = matchMedia("(prefers-color-scheme: dark)");
  // Loopback views share a host across ports. A cookie keeps their theme in sync.
  const saved = () => document.cookie.match(/(?:^|; *)eye-theme=(bright|dark)(?:;|$)/)?.[1];
  let selected = saved();
  let toggle;

  function applyTheme() {
    const theme = selected || (system.matches ? "dark" : "bright");
    if (root.dataset.theme !== theme) root.dataset.theme = theme;
    if (toggle) toggle.textContent = theme === "dark" ? "Use bright theme" : "Use dark theme";
  }

  function syncTheme() {
    selected = saved() || selected;
    applyTheme();
  }

  applyTheme();
  system.addEventListener("change", applyTheme);
  addEventListener("focus", syncTheme);
  addEventListener("pageshow", syncTheme);
  document.addEventListener("DOMContentLoaded", () => {
    toggle = document.querySelector(".theme-toggle");
    applyTheme();
    toggle.hidden = false;
    toggle.addEventListener("click", () => {
      selected = root.dataset.theme === "dark" ? "bright" : "dark";
      document.cookie = "eye-theme=" + selected + "; Path=/; Max-Age=31536000; SameSite=Lax";
      applyTheme();
    });
  });
})();
