"use strict";

window.scheduleLiveRefresh = function scheduleLiveRefresh(callback) {
  let running = false;
  const refresh = async () => {
    if (running || document.hidden) return;
    running = true;
    try {
      await callback();
    } finally {
      running = false;
    }
  };
  const timer = window.setInterval(refresh, 5000);
  document.addEventListener("visibilitychange", refresh);
  return () => {
    window.clearInterval(timer);
    document.removeEventListener("visibilitychange", refresh);
  };
};
