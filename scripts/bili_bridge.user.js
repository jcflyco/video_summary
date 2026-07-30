// ==UserScript==
// @name         video_summary Bilibili 进度桥
// @namespace    video_summary
// @version      1.0
// @description  向父页面广播 B 站外链播放器的播放进度，供「视频总结」汇总页自动跟随高亮
// @match        https://player.bilibili.com/player.html*
// @grant        none
// @run-at       document-idle
// ==/UserScript==
(function () {
  'use strict';
  if (window.top === window) return; // 仅在被嵌入为 iframe 时工作
  setInterval(function () {
    try {
      var p = window.player;
      if (!p || typeof p.getCurrentTime !== 'function') return;
      if (typeof p.isInitialized === 'function' && !p.isInitialized()) return;
      window.parent.postMessage({
        source: 'vs-bili-bridge',
        currentTime: p.getCurrentTime(),
        paused: typeof p.isPaused === 'function' ? p.isPaused() : false
      }, '*');
    } catch (e) { /* 播放器未就绪时静默 */ }
  }, 500);
})();
