#!/usr/bin/env python3
"""扫描 <dir>/output/*.md，生成单文件汇总页 <dir>/视频总结.html。

- 所有 Markdown 以 JSON 形式内嵌，双击 HTML 即可离线查看（快照模式）。
- 若通过 http 服务打开（如 `python3 -m http.server`，工作目录为 <dir>），
  页面每 30 秒轮询 output/ 目录，新增/修改的 Markdown 自动出现（实时模式）。
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>视频总结</title>
<style>
:root{
  --bg:#f7f7f8; --panel:#ffffff; --text:#1d1d1f; --muted:#6e6e73;
  --accent:#2563eb; --border:#e3e3e6; --hover:#eef2ff; --active:#e0e9ff;
  --quote-bg:#f1f5f9; --code-bg:#eef0f3;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#141416; --panel:#1d1d20; --text:#e8e8ea; --muted:#98989f;
    --accent:#7aa2ff; --border:#2c2c30; --hover:#26262c; --active:#2c3350;
    --quote-bg:#232327; --code-bg:#26262b;
  }
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--text);
  font:15px/1.75 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",Roboto,sans-serif;
  display:flex;flex-direction:column}
#topbar{display:flex;align-items:center;gap:12px;padding:10px 18px;
  border-bottom:1px solid var(--border);background:var(--panel);flex:0 0 auto}
#topbar h1{font-size:16px;margin:0;flex:0 0 auto}
#topbar .spacer{flex:1}
#toggle-player{border:1px solid var(--border);background:var(--panel);color:var(--text);
  padding:6px 14px;border-radius:8px;cursor:pointer;font-size:13px}
#toggle-player:hover{background:var(--hover)}
#content-tabs{display:flex;gap:4px;max-width:860px;margin:0 auto;padding:0 28px}
#content-tabs[hidden]{display:none}
#content-tabs button{border:0;background:none;color:var(--muted);
  padding:9px 20px;cursor:pointer;font-size:14px;
  border-bottom:2px solid transparent;margin-bottom:-1px}
#content-tabs button:hover:not(:disabled){color:var(--text)}
#content-tabs button.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}
#content-tabs button:disabled{opacity:.45;cursor:not-allowed}
.tabs-right{margin-left:auto;display:flex;gap:10px;align-items:center}
#follow-mode{font-size:12px;color:var(--muted)}
#toggle-zh{border:1px solid var(--border);
  background:var(--panel);color:var(--muted);padding:4px 12px;border-radius:8px;
  cursor:pointer;font-size:12.5px}
#toggle-zh:hover{color:var(--text);background:var(--hover)}
#toggle-zh.on{color:var(--accent);border-color:var(--accent)}
.srt-line{display:flex;gap:12px;padding:3px 0;font-size:14px}
.srt-ts{flex:0 0 auto;color:var(--accent);font-variant-numeric:tabular-nums;text-decoration:none}
a.srt-ts:hover{text-decoration:underline}
span.srt-ts{color:var(--muted)}
.srt-body{flex:1;min-width:0}
.srt-zh{display:none;color:var(--muted)}
#content.show-zh .srt-zh{display:block}
.srt-line,#content p{transition:background .25s}
.srt-line.playing{background:var(--active);box-shadow:0 0 0 6px var(--active);border-radius:4px}
#content p.playing{background:var(--hover);box-shadow:0 0 0 10px var(--hover);border-radius:4px}
.srt-note{color:var(--muted);font-size:12.5px;margin:14px 0 10px}
#layout{display:flex;flex:1;min-height:0}
#sidebar{width:280px;flex:0 0 280px;border-right:1px solid var(--border);
  background:var(--panel);overflow-y:auto;padding:10px}
.nav-item{padding:10px 12px;border-radius:8px;cursor:pointer;margin-bottom:4px;position:relative}
.nav-item:hover{background:var(--hover)}
.nav-item.active{background:var(--active)}
.nav-del{position:absolute;top:6px;right:6px;display:none;border:1px solid var(--border);
  background:var(--panel);color:var(--muted);border-radius:6px;cursor:pointer;
  font-size:12px;line-height:1;padding:3px 7px}
.nav-item:hover .nav-del{display:block}
.nav-del:hover{color:#dc2626;border-color:#dc2626}
.nav-item .t{font-size:13.5px;font-weight:600;line-height:1.45;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.nav-item .m{font-size:12px;color:var(--muted);margin-top:2px}
#main{flex:1;min-width:0;overflow-y:auto}
#main-header{position:sticky;top:0;z-index:5;background:var(--bg);
  border-bottom:1px solid var(--border)}
#player-wrap{padding:14px 24px 10px}
#player-frame,#player-video{width:100%;max-width:656px;aspect-ratio:16/9;display:block;
  margin:0 auto;border:0;border-radius:10px;background:#000}
#file-notice{max-width:820px;margin:0 auto 10px;padding:10px 14px;border-radius:8px;
  background:#fef3c7;color:#92400e;font-size:13px;line-height:1.6}
@media (prefers-color-scheme: dark){#file-notice{background:#3a2f14;color:#fbbf24}}
body.file-mode #player-frame{display:none}
body.player-hidden #player-wrap{display:none}
#content{max-width:860px;margin:0 auto;padding:8px 28px 60px}
#content h2{font-size:22px;border-bottom:1px solid var(--border);padding-bottom:6px;margin-top:28px}
#content h3{font-size:18px;margin-top:26px}
#content a{color:var(--accent);text-decoration:none}
#content a:hover{text-decoration:underline}
#content blockquote{margin:14px 0;padding:10px 16px;background:var(--quote-bg);
  border-left:3px solid var(--accent);border-radius:0 8px 8px 0;color:var(--muted)}
#content blockquote p{margin:4px 0}
#content code{background:var(--code-bg);padding:1px 5px;border-radius:4px;font-size:.9em}
#content hr{border:0;border-top:1px solid var(--border);margin:28px 0}
#empty{color:var(--muted);text-align:center;padding:80px 20px}
.gen-note{font-size:11.5px;color:var(--muted);padding:12px;text-align:center}
@media (max-width: 900px){
  #layout{flex-direction:column}
  #sidebar{width:100%;flex:0 0 auto;max-height:32vh;border-right:0;border-bottom:1px solid var(--border)}
}
</style>
</head>
<body>
<div id="topbar">
  <h1>🎬 视频总结</h1>
  <span class="spacer"></span>
  <button id="toggle-player">隐藏播放器</button>
</div>
<div id="layout">
  <nav id="sidebar"></nav>
  <div id="main">
    <div id="main-header">
      <div id="player-wrap"><div id="file-notice" hidden>⚠️ 本地播放服务未在运行（YouTube 不允许 file:// 页面嵌入播放，Error 153）。重新运行一次视频总结，或在本目录执行 <code>python3 -m http.server __PORT__ --bind 127.0.0.1</code> 后刷新本页即可自动跳转。当前时间戳将改为在新标签页打开原视频对应时间点。</div><div id="platform-notice" hidden style="max-width:820px;margin:0 auto 10px;padding:10px 14px;border-radius:8px;background:var(--quote-bg);color:var(--muted);font-size:13px;line-height:1.6"></div><iframe id="player-frame" referrerpolicy="strict-origin-when-cross-origin" allow="autoplay; fullscreen; encrypted-media; picture-in-picture" allowfullscreen></iframe><video id="player-video" controls playsinline preload="metadata" style="display:none"></video></div>
      <div id="content-tabs" hidden>
        <button id="tab-summary" class="active">总结</button>
        <button id="tab-srt">原文</button>
        <span class="tabs-right"><span id="follow-mode" hidden>估算跟随 · 点时间戳校准</span><button id="toggle-zh" hidden>显示翻译</button></span>
      </div>
    </div>
    <article id="content"><div id="empty">output/ 目录暂无总结</div></article>
  </div>
</div>
<script id="embedded-data" type="application/json">__DATA__</script>
<script>
(function(){
  'use strict';
  var SERVER = 'http://127.0.0.1:__PORT__/';
  if(location.protocol === 'file:'){
    try{
      fetch(SERVER, {mode:'no-cors'}).then(function(){
        location.replace(SERVER + encodeURIComponent('视频总结.html'));
      }).catch(function(){});
    }catch(e){}
  }
  var state = {docs: [], cur: -1, view: 'summary', zhOn: false, playTime: null, playing: false};
  try{ state.zhOn = localStorage.getItem('vsSrtZh') === '1'; }catch(e){}

  function parseTime(v){
    if(!v) return null;
    if(/^\d+$/.test(v)) return +v;
    var m = v.match(/^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$/);
    if(!m) return null;
    return (+(m[1]||0))*3600 + (+(m[2]||0))*60 + (+(m[3]||0));
  }

  function parseVideoLink(href){
    var u; try{ u = new URL(href); }catch(e){ return null; }
    var host = u.hostname.replace(/^(www\.|m\.)/,'');
    if(host === 'youtube.com' || host === 'youtu.be'){
      var id = null;
      if(host === 'youtu.be') id = u.pathname.slice(1).split('/')[0];
      else id = u.searchParams.get('v') || (u.pathname.indexOf('/embed/') === 0 ? u.pathname.split('/')[2] : null);
      if(!id) return null;
      return {platform:'youtube', id:id, t:parseTime(u.searchParams.get('t') || u.searchParams.get('start'))};
    }
    if(host === 'bilibili.com' || host === 'b23.tv'){
      var m = u.pathname.match(/BV[A-Za-z0-9]+/);
      if(!m) return null;
      return {platform:'bilibili', id:m[0], t:parseTime(u.searchParams.get('t'))};
    }
    if(host === 'podcasts.apple.com'){
      var col = (u.pathname.match(/\/id(\d+)/) || [])[1];
      var ep = u.searchParams.get('i');
      if(!col || !ep) return null;
      var country = (u.pathname.split('/')[1] || 'us');
      if(country.length !== 2) country = 'us';
      var th = u.hash && u.hash.indexOf('t=') >= 0 ? u.hash.replace(/^#/,'').split('t=')[1] : null;
      return {platform:'apple_podcasts', id:ep, collectionId:col, country:country, t:parseTime(th || u.searchParams.get('t'))};
    }
    if(host === 'xiaoyuzhoufm.com'){
      var xm = u.pathname.match(/\/episode\/([0-9a-fA-F]+)/);
      if(!xm) return null;
      return {platform:'xiaoyuzhou', id:xm[1], t:parseTime(u.searchParams.get('t'))};
    }
    if(host === 'xiaohongshu.com' || host === 'xhslink.com'){
      var nm = u.pathname.match(/\/explore\/([0-9a-fA-F]+)/);
      if(!nm) return null;
      return {platform:'xiaohongshu', id:nm[1], t:parseTime(u.searchParams.get('t')), href:href};
    }
    if(host === 'longbridge.com' || host === 'longbridge.cn'){
      var lm = u.pathname.match(/\/lives\/(\d+)/);
      if(!lm) return null;
      return {platform:'longbridge', id:lm[1], t:parseTime(u.searchParams.get('t'))};
    }
    return null;
  }

  function extractVideo(md){
    var m = md.match(/(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/)([A-Za-z0-9_-]{11})/);
    if(m) return {platform:'youtube', id:m[1]};
    m = md.match(/bilibili\.com\/video\/(BV[A-Za-z0-9]+)/);
    if(m) return {platform:'bilibili', id:m[1]};
    m = md.match(/podcasts\.apple\.com\/([a-z]{2})\/podcast\/[^?\s]*\/id(\d+)\?[^)\s]*i=(\d+)/);
    if(m) return {platform:'apple_podcasts', id:m[3], collectionId:m[2], country:m[1]};
    m = md.match(/xiaoyuzhoufm\.com\/episode\/([0-9a-fA-F]+)/);
    if(m) return {platform:'xiaoyuzhou', id:m[1]};
    m = md.match(/xiaohongshu\.com\/explore\/([0-9a-fA-F]+)/);
    if(m) return {platform:'xiaohongshu', id:m[1]};
    m = md.match(/longbridge\.(?:com|cn)\/[^)\s]*?\/lives\/(\d+)/);
    if(m) return {platform:'longbridge', id:m[1]};
    return null;
  }

  function splitSrt(md){
    /* 拆出原文/中文字幕与运行统计；统计常在字幕之后，不得并入 SRT 或丢弃。 */
    var re = /(^|\n)(#{2,3}) (原文字幕|中文字幕|运行统计)\s*\n/g;
    var m, marks = [];
    while((m = re.exec(md))){
      var headStart = m.index + (m[1] ? m[1].length : 0);
      marks.push({name: m[3], start: headStart, body: m.index + m[0].length});
    }
    var cut = marks.length ? marks[0].start : md.length;
    var res = {
      body: md.slice(0, cut).replace(/\s+$/, ''),
      srt: null,
      zh: null,
      stats: null
    };
    marks.forEach(function(mk, i){
      var end = i + 1 < marks.length ? marks[i+1].start : md.length;
      var t = md.slice(mk.body, end).trim();
      t = t.replace(/^`{3,}[A-Za-z]*\s*\n/, '').replace(/\n`{3,}$/, '').trim();
      if(mk.name === '原文字幕') res.srt = t || null;
      else if(mk.name === '中文字幕') res.zh = t || null;
      else if(mk.name === '运行统计') res.stats = t || null;
    });
    return res;
  }

  function enrich(d){
    d.video = extractVideo(d.md);
    var parts = splitSrt(d.md);
    d.body = parts.body;
    d.srt = parts.srt;
    d.zh = parts.zh;
    d.stats = parts.stats;
    var t = d.file.replace(/_总结\.md$/,'').replace(/\.md$/,'');
    if(d.video && t.slice(-(d.video.id.length+1)) === '_'+d.video.id)
      t = t.slice(0, -(d.video.id.length+1));
    d.title = t;
    d.uploader = (d.md.match(/📺\s*([^|\n]+?)\s*\|/)||[])[1] || '';
    d.date = (d.md.match(/上传于\s*([\d-]+)/)||[])[1] || '';
    return d;
  }

  /* ---------- markdown 渲染（覆盖总结文档用到的子集） ---------- */
  function esc(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function inline(s){
    s = esc(s);
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/\[([^\]]*)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    return s;
  }
  function mdRender(md){
    var lines = md.split(/\r?\n/), out = [], i = 0, m, buf;
    while(i < lines.length){
      var l = lines[i];
      if(/^\s*$/.test(l)){ i++; continue; }
      if(/^---+\s*$/.test(l)){ out.push('<hr>'); i++; continue; }
      if((m = l.match(/^(#{1,4})\s+(.*)/))){
        var lv = Math.min(m[1].length+1, 5);
        out.push('<h'+lv+'>'+inline(m[2])+'</h'+lv+'>'); i++; continue;
      }
      if(/^>/.test(l)){
        buf = [];
        while(i < lines.length && /^>/.test(lines[i])){ buf.push(lines[i].replace(/^>\s?/,'')); i++; }
        out.push('<blockquote>'+buf.filter(function(b){return b.trim()!=='';})
          .map(function(b){return '<p>'+inline(b)+'</p>';}).join('')+'</blockquote>');
        continue;
      }
      if(/^[-*]\s+/.test(l)){
        buf = [];
        while(i < lines.length && /^[-*]\s+/.test(lines[i])){ buf.push(lines[i].replace(/^[-*]\s+/,'')); i++; }
        out.push('<ul>'+buf.map(function(b){return '<li>'+inline(b)+'</li>';}).join('')+'</ul>');
        continue;
      }
      if(/^\d+\.\s+/.test(l)){
        buf = [];
        while(i < lines.length && /^\d+\.\s+/.test(lines[i])){ buf.push(lines[i].replace(/^\d+\.\s+/,'')); i++; }
        out.push('<ol>'+buf.map(function(b){return '<li>'+inline(b)+'</li>';}).join('')+'</ol>');
        continue;
      }
      buf = [];
      while(i < lines.length && !/^\s*$/.test(lines[i]) && !/^(#|>|[-*]\s|\d+\.\s|---)/.test(lines[i])){
        buf.push(lines[i]); i++;
      }
      out.push('<p>'+buf.map(inline).join('<br>')+'</p>');
    }
    return out.join('\n');
  }

  /* ---------- 原文字幕渲染 ---------- */
  function pad2(n){ return (n < 10 ? '0' : '') + n; }
  function parseCues(srt){
    var cues = [], blocks = srt.replace(/\r/g,'').split(/\n\s*\n/);
    blocks.forEach(function(b){
      var lines = b.split('\n').filter(function(x){ return x.trim() !== ''; });
      var ti = -1;
      for(var j = 0; j < lines.length; j++){ if(lines[j].indexOf('-->') >= 0){ ti = j; break; } }
      if(ti < 0) return;
      /* 保留毫秒：丢弃小数会让字幕高亮最多提前近 1 秒 */
      var m = lines[ti].match(/(?:(\d+):)?(\d+):(\d+)(?:[.,](\d+))?/);
      if(!m) return;
      var frac = m[4] ? parseFloat('0.' + m[4]) : 0;
      var sec = (+(m[1]||0))*3600 + (+m[2])*60 + (+m[3]) + frac;
      var text = lines.slice(ti+1).join(' ').replace(/<[^>]+>/g,'').replace(/\s+/g,' ').trim();
      if(!text) return;
      cues.push({sec: sec, text: text});
    });
    return cues;
  }
  function srtRender(d){
    var cues = parseCues(d.srt), out = [];
    var zh = d.zh ? parseCues(d.zh) : [];
    var byIndex = zh.length === cues.length;
    function zhNear(sec){
      if(!zh.length) return null;
      var lo = 0, hi = zh.length - 1;
      while(lo < hi){ var mid = (lo + hi) >> 1; if(zh[mid].sec < sec) lo = mid + 1; else hi = mid; }
      var best = zh[lo];
      if(lo > 0 && Math.abs(zh[lo-1].sec - sec) < Math.abs(best.sec - sec)) best = zh[lo-1];
      return Math.abs(best.sec - sec) <= 2.5 ? best.text : null;
    }
    cues.forEach(function(c, idx){
      var jump = Math.floor(c.sec);
      var h = Math.floor(c.sec/3600), mn = Math.floor(c.sec%3600/60), s = Math.floor(c.sec%60);
      var label = '[' + (h ? h + ':' + pad2(mn) : pad2(mn)) + ':' + pad2(s) + ']';
      var href = '';
      if(d.video){
        if(d.video.platform === 'youtube'){
          href = 'https://www.youtube.com/watch?v=' + d.video.id + '&t=' + jump + 's';
        }else if(d.video.platform === 'bilibili'){
          href = 'https://www.bilibili.com/video/' + d.video.id + '?t=' + jump;
        }else if(d.video.platform === 'apple_podcasts'){
          var ctry = d.video.country || 'us';
          href = 'https://podcasts.apple.com/' + ctry + '/podcast/id' + d.video.collectionId + '?i=' + d.video.id + '#t=' + jump;
        }else if(d.video.platform === 'xiaoyuzhou'){
          href = 'https://www.xiaoyuzhoufm.com/episode/' + d.video.id + '?t=' + jump;
        }else if(d.video.platform === 'longbridge'){
          href = 'https://longbridge.com/zh-CN/lives/' + d.video.id + '?t=' + jump;
        }
      }
      var zhText = byIndex ? zh[idx].text : zhNear(c.sec);
      out.push('<div class="srt-line" data-sec="' + c.sec + '">' +
        (href ? '<a class="srt-ts" href="' + href + '" target="_blank" rel="noopener">' + label + '</a>'
              : '<span class="srt-ts">' + label + '</span>') +
        '<div class="srt-body"><div class="srt-text">' + esc(c.text) + '</div>' +
        (zhText ? '<div class="srt-zh">' + esc(zhText) + '</div>' : '') + '</div></div>');
    });
    return '<div class="srt-note">原文字幕（点击时间戳可跳转播放）· 共 ' + out.length + ' 条' +
      (zh.length ? ' · 含中文翻译' : '') + '</div>' + out.join('\n');
  }

  /* ---------- 播放器 ---------- */
  var fileMode = location.protocol === 'file:';
  function loadPlayer(video, t, autoplay){
    var f = document.getElementById('player-frame');
    var notice = document.getElementById('platform-notice');
    if(notice) notice.hidden = true;
    if(fileMode) return;
    state.playTime = t || 0;
    state.playing = false;
    stopBiliClock();
    var vid = document.getElementById('player-video');
    /* 切到非长桥/空文档时，暂停并隐藏原生播放器 */
    if(!video || video.platform !== 'longbridge'){
      try{ vid.pause(); }catch(e){}
      vid.style.display = 'none';
    }
    if(!video){ state.playTime = null; f.removeAttribute('src'); f.style.display=''; return; }
    if(video.platform === 'longbridge'){
      f.removeAttribute('src'); f.style.display = 'none';
      vid.style.display = '';
      var startAt = t || 0;
      function lbSeekPlay(){
        function seek(){ try{ if(startAt > 0) vid.currentTime = startAt; }catch(e){} }
        if(startAt > 0){ if(vid.readyState >= 1) seek(); else vid.addEventListener('loadedmetadata', seek, {once:true}); }
        if(autoplay){ var pr = vid.play(); if(pr && pr.catch) pr.catch(function(){}); }
      }
      if(vid.getAttribute('data-id') !== video.id){
        /* 本地 serve.py 现取回放并做 faststart 改写：首个 Range 即可秒取元数据 */
        vid.setAttribute('data-id', video.id);
        vid.setAttribute('data-lb-fallback', '0');
        vid.src = 'api/lb_stream?id=' + encodeURIComponent(video.id);
      }
      lbSeekPlay();
      return;
    }
    if(video.platform === 'youtube'){
      f.style.display='';
      f.src = 'https://www.youtube.com/embed/'+video.id+'?rel=0&enablejsapi=1&origin='+
        encodeURIComponent(location.origin)+'&start='+(t||0)+(autoplay?'&autoplay=1':'');
    }else if(video.platform === 'bilibili'){
      f.style.display='';
      f.src = 'https://player.bilibili.com/player.html?bvid='+video.id+'&t='+(t||0)+'&autoplay='+(autoplay?1:0)+'&high_quality=1';
      /* B 站无进度接口：点时间戳跳转播放时用本地时钟估算跟随；
         装了 bili_bridge.user.js 油猴脚本则由桥接消息驱动精确跟随 */
      if(autoplay && biliMode !== 'bridge') startBiliClock(t || 0);
    }else if(video.platform === 'apple_podcasts'){
      f.style.display='';
      var c = video.country || 'us';
      f.src = 'https://embed.podcasts.apple.com/'+c+'/podcast/id'+video.collectionId+'?i='+video.id+(t?('&t='+t):'');
    }else{
      f.removeAttribute('src');
      f.style.display='none';
      if(notice){
        notice.hidden = false;
        notice.textContent = '该平台（'+(video.platform||'')+'）无稳定内嵌播放器；点击正文时间戳将在新标签页打开原链接。';
      }
    }
  }

  /* ---------- 播放跟随：高亮当前段落/字幕行并滚动到顶部 ---------- */
  var followEl = null, userScrollUntil = 0, ytPoll = null;
  var biliTimer = null, biliMode = 'none'; /* none=时钟估算可用 bridge=油猴桥接 */
  var BILI_START_LAG = 1.0; /* B站播放器从载入到实际起播约耗时 1 秒，估算时钟起点回退补偿 */
  function setFollowChip(show){ document.getElementById('follow-mode').hidden = !show; }
  function stopBiliClock(){
    if(biliTimer){ clearInterval(biliTimer); biliTimer = null; }
    setFollowChip(false);
  }
  function startBiliClock(t){
    stopBiliClock();
    state.playTime = t - BILI_START_LAG;
    state.playing = true;
    setFollowChip(true);
    var last = Date.now();
    biliTimer = setInterval(function(){
      var now = Date.now();
      state.playTime += (now - last) / 1000;
      last = now;
      updateFollow();
    }, 500);
    updateFollow();
  }
  function closestP(el){ while(el && el.tagName !== 'P') el = el.parentElement; return el; }
  function scrollToEl(el){
    var main = document.getElementById('main');
    var hdr = document.getElementById('main-header');
    /* 字幕行滚到第二行：上一行保留在顶部作上下文 */
    var anchor = el;
    if(el.classList.contains('srt-line')){
      var prev = el.previousElementSibling;
      if(prev && prev.classList && prev.classList.contains('srt-line')) anchor = prev;
    }
    var top = Math.max(0, main.scrollTop + anchor.getBoundingClientRect().top - hdr.getBoundingClientRect().bottom - 10);
    var from = main.scrollTop;
    main.scrollTo({top: top, behavior: 'smooth'});
    setTimeout(function(){
      /* 后台窗口平滑滚动被节流时直接跳转 */
      if(Math.abs(main.scrollTop - from) < 2 && Math.abs(top - from) > 2 &&
         Date.now() > userScrollUntil) main.scrollTop = top;
    }, 500);
  }
  function currentMarkers(){
    var d = state.cur >= 0 ? state.docs[state.cur] : null;
    var c = document.getElementById('content');
    if(!d || !d.video) return [];
    var els, res = [], i;
    if(state.view === 'srt'){
      els = c.querySelectorAll('.srt-line[data-sec]');
      for(i = 0; i < els.length; i++) res.push({t: +els[i].getAttribute('data-sec'), el: els[i]});
    }else{
      els = c.querySelectorAll('p strong a');
      for(i = 0; i < els.length; i++){
        var p = parseVideoLink(els[i].href);
        var blk = closestP(els[i]);
        if(p && p.t != null && p.id === d.video.id && blk) res.push({t: p.t, el: blk});
      }
      res.sort(function(a, b){ return a.t - b.t; });
    }
    return res;
  }
  function updateFollow(){
    if(state.playTime == null) return;
    var marks = currentMarkers(), cur = null;
    /* 严格按字幕起始时间匹配；勿再加提前量（旧 +0.3 会叠加上截断毫秒后的误差） */
    for(var i = 0; i < marks.length; i++){
      if(marks[i].t <= state.playTime) cur = marks[i].el; else break;
    }
    if(cur === followEl) return;
    if(followEl && followEl.classList) followEl.classList.remove('playing');
    followEl = cur;
    if(!cur) return;
    cur.classList.add('playing');
    if(Date.now() > userScrollUntil) scrollToEl(cur);
  }
  function ytSend(msg){
    var f = document.getElementById('player-frame');
    if(f.contentWindow){
      try{ f.contentWindow.postMessage(JSON.stringify(msg), 'https://www.youtube.com'); }catch(e){}
    }
  }
  document.getElementById('player-frame').addEventListener('load', function(){
    if(!/youtube\.com\/embed/.test(this.src || '')) return;
    var n = 0;
    clearInterval(ytPoll);
    ytPoll = setInterval(function(){
      ytSend({event: 'listening', id: 'vs', channel: 'widget'});
      if(++n > 20) clearInterval(ytPoll);
    }, 500);
  });
  window.addEventListener('message', function(e){
    if(e.origin !== 'https://www.youtube.com' || typeof e.data !== 'string') return;
    var data; try{ data = JSON.parse(e.data); }catch(err){ return; }
    if(!data || !data.info) return;
    clearInterval(ytPoll);
    if(data.info.currentTime != null) state.playTime = data.info.currentTime;
    if(data.info.playerState != null) state.playing = data.info.playerState === 1;
    if(state.playing) updateFollow();
  });
  /* bili_bridge.user.js（油猴脚本）从 player.bilibili.com 内部广播精确进度 */
  window.addEventListener('message', function(e){
    if(e.origin !== 'https://player.bilibili.com') return;
    var d = e.data;
    if(!d || d.source !== 'vs-bili-bridge') return;
    if(biliMode !== 'bridge'){ biliMode = 'bridge'; stopBiliClock(); }
    if(typeof d.currentTime === 'number') state.playTime = d.currentTime;
    state.playing = !d.paused;
    if(state.playing) updateFollow();
  });
  /* 长桥原生 <video>：currentTime 精确驱动分段/字幕跟随 */
  (function(){
    var vid = document.getElementById('player-video');
    if(!vid) return;
    vid.addEventListener('timeupdate', function(){
      state.playTime = vid.currentTime;
      if(state.playing) updateFollow();
    });
    vid.addEventListener('play', function(){ state.playing = true; updateFollow(); });
    vid.addEventListener('pause', function(){ state.playing = false; });
    vid.addEventListener('seeked', function(){ state.playTime = vid.currentTime; updateFollow(); });
    /* faststart 流失败时回退到长桥回放直链（首帧较慢但仍可播） */
    vid.addEventListener('error', function(){
      var id = vid.getAttribute('data-id');
      if(!id || vid.getAttribute('data-lb-fallback') === '1') return;
      vid.setAttribute('data-lb-fallback', '1');
      var at = vid.currentTime || 0;
      fetch('api/longbridge?id=' + encodeURIComponent(id)).then(function(r){ return r.json(); }).then(function(j){
        if(j && (j.replay_url || j.m3u8_live_url)){
          vid.src = j.replay_url || j.m3u8_live_url;
          if(at > 0) vid.addEventListener('loadedmetadata', function(){ try{ vid.currentTime = at; }catch(e){} }, {once:true});
          var pr = vid.play(); if(pr && pr.catch) pr.catch(function(){});
        }
      }).catch(function(){});
    });
  })();
  function setPlayerHidden(hidden){
    document.body.classList.toggle('player-hidden', hidden);
    document.getElementById('toggle-player').textContent = hidden ? '显示播放器' : '隐藏播放器';
    try{ localStorage.setItem('vsPlayerHidden', hidden ? '1' : '0'); }catch(e){}
  }

  /* ---------- 渲染 ---------- */
  function renderNav(){
    var nav = document.getElementById('sidebar');
    nav.innerHTML = '';
    state.docs.forEach(function(d, idx){
      var el = document.createElement('div');
      el.className = 'nav-item' + (idx === state.cur ? ' active' : '');
      var meta = [d.uploader, d.date].filter(Boolean).join(' · ');
      el.innerHTML = '<div class="t"></div>' + (meta ? '<div class="m"></div>' : '');
      el.querySelector('.t').textContent = d.title;
      if(meta) el.querySelector('.m').textContent = meta;
      el.onclick = function(){ selectDoc(idx); };
      if(state.api){
        var del = document.createElement('button');
        del.className = 'nav-del';
        del.textContent = '✕';
        del.title = '删除该总结（含 Markdown 文件，移入 output/.trash 可找回）';
        del.onclick = function(ev){ ev.stopPropagation(); handleDelete(del, d); };
        el.appendChild(del);
      }
      nav.appendChild(el);
    });
    var note = document.createElement('div');
    note.className = 'gen-note';
    note.textContent = '生成于 __GENERATED_AT__ · 共 ' + state.docs.length + ' 篇';
    nav.appendChild(note);
  }
  function handleDelete(btn, d){
    btn.disabled = true;
    btn.textContent = '…';
    fetch('api/delete', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({file: d.file})})
      .then(function(r){ return r.json(); })
      .then(function(res){
        if(res.status === 'ok'){ removeLocal(d.file); }
        else { btn.disabled = false; btn.textContent = '删除失败'; setTimeout(function(){ btn.textContent = '✕'; }, 2000); }
      })
      .catch(function(){
        btn.disabled = false; btn.textContent = '删除失败';
        setTimeout(function(){ btn.textContent = '✕'; }, 2000);
      });
  }
  function sigOf(docs){
    return docs.map(function(d){ return d.file + ':' + d.md.length + ':' + d.mtime; }).join('|');
  }
  function removeLocal(file){
    var curFile = state.cur >= 0 && state.docs[state.cur] ? state.docs[state.cur].file : null;
    state.docs = state.docs.filter(function(x){ return x.file !== file; });
    state.sig = sigOf(state.docs);
    if(curFile === file || state.docs.length === 0){
      selectDoc(state.docs.length ? Math.min(state.cur, state.docs.length - 1) : -1);
    }else{
      state.cur = state.docs.findIndex(function(x){ return x.file === curFile; });
      renderNav();
    }
  }
  function updateTabs(){
    var d = state.cur >= 0 ? state.docs[state.cur] : null;
    var sw = document.getElementById('content-tabs');
    sw.hidden = !d;
    if(!d) return;
    var bs = document.getElementById('tab-summary'), bo = document.getElementById('tab-srt');
    bs.classList.toggle('active', state.view === 'summary');
    bo.classList.toggle('active', state.view === 'srt');
    bo.disabled = !d.srt;
    bo.title = d.srt ? '查看原始字幕全文' : '该总结未附原文字幕';
    var tz = document.getElementById('toggle-zh');
    tz.hidden = !(state.view === 'srt' && d.srt && d.zh);
    tz.classList.toggle('on', state.zhOn);
    tz.textContent = state.zhOn ? '隐藏翻译' : '显示翻译';
  }
  function renderContent(){
    var d = state.cur >= 0 ? state.docs[state.cur] : null;
    var c = document.getElementById('content');
    if(!d) c.innerHTML = '<div id="empty">output/ 目录暂无总结</div>';
    else if(state.view === 'srt' && d.srt) c.innerHTML = srtRender(d);
    else {
      var html = mdRender(d.body);
      if(d.stats) html += (html ? '\n' : '') + mdRender('## 运行统计\n\n' + d.stats);
      c.innerHTML = html;
    }
    c.classList.toggle('show-zh', state.zhOn);
    followEl = null;
    if(state.playing) updateFollow();
    updateTabs();
  }
  function setView(v){
    if(state.view === v) return;
    state.view = v;
    renderContent();
    if(state.playing && followEl) scrollToEl(followEl);
    else document.getElementById('main').scrollTop = 0;
  }
  function selectDoc(idx, keepPlayer){
    state.cur = idx;
    var d = state.docs[idx];
    if(!d || !d.srt) state.view = 'summary';
    renderContent();
    if(!keepPlayer) loadPlayer(d && d.video, 0, false);
    renderNav();
    document.getElementById('main').scrollTop = 0;
  }

  document.getElementById('content').addEventListener('click', function(e){
    var a = e.target.closest ? e.target.closest('a') : null;
    if(!a || fileMode) return;
    var p = parseVideoLink(a.href);
    var cur = state.docs[state.cur];
    if(!p || !cur || !cur.video || p.id !== cur.video.id) return;
    if(p.platform === 'xiaohongshu' || p.platform === 'xiaoyuzhou') return;
    if(p.t != null){
      e.preventDefault();
      setPlayerHidden(false);
      loadPlayer(cur.video, p.t, true);
    }
  });
  document.getElementById('toggle-player').onclick = function(){
    setPlayerHidden(!document.body.classList.contains('player-hidden'));
  };
  document.getElementById('tab-summary').onclick = function(){ setView('summary'); };
  document.getElementById('tab-srt').onclick = function(){
    var d = state.cur >= 0 ? state.docs[state.cur] : null;
    if(d && d.srt) setView('srt');
  };
  document.getElementById('toggle-zh').onclick = function(){
    state.zhOn = !state.zhOn;
    try{ localStorage.setItem('vsSrtZh', state.zhOn ? '1' : '0'); }catch(e){}
    document.getElementById('content').classList.toggle('show-zh', state.zhOn);
    updateTabs();
  };
  ['wheel', 'touchmove'].forEach(function(ev){
    document.getElementById('main').addEventListener(ev, function(){
      userScrollUntil = Date.now() + 5000;
    }, {passive: true});
  });

  function applyDocs(docs){
    docs.forEach(enrich);
    docs.sort(function(a,b){ return (b.mtime||0) - (a.mtime||0); });
    var sig = sigOf(docs);
    if(sig === state.sig) return;
    state.sig = sig;
    var curFile = state.cur >= 0 && state.docs[state.cur] ? state.docs[state.cur].file : null;
    state.docs = docs;
    var idx = curFile ? docs.findIndex(function(d){ return d.file === curFile; }) : -1;
    var changed = !(idx >= 0 && idx === state.cur && curFile);
    selectDoc(idx >= 0 ? idx : (docs.length ? 0 : -1), !changed);
  }

  /* ---------- 实时模式：通过 http 打开时轮询 output/ ---------- */
  var live = /^https?:$/.test(location.protocol);
  function refresh(){
    if(!live) return;
    fetch('output/?_=' + Math.random()).then(function(r){
      if(!r.ok) throw 0;
      return r.text();
    }).then(function(txt){
      var files = [], m, re = /href="([^"]+\.md)"/gi;
      while((m = re.exec(txt))){
        var f = decodeURIComponent(m[1]);
        if(f.indexOf('/') === -1) files.push(f);
      }
      return Promise.all(files.map(function(f){
        return fetch('output/' + encodeURIComponent(f) + '?_=' + Math.random()).then(function(r){
          if(!r.ok) return null;
          var lm = Date.parse(r.headers.get('Last-Modified') || '') || 0;
          return r.text().then(function(md){ return {file:f, mtime:lm/1000, md:md}; });
        });
      }));
    }).then(function(docs){
      docs = docs.filter(Boolean);
      if(docs.length) applyDocs(docs);
    }).catch(function(){});
  }

  /* ---------- 启动 ---------- */
  var embedded = [];
  try{ embedded = JSON.parse(document.getElementById('embedded-data').textContent); }catch(e){}
  var hidden = false;
  try{ hidden = localStorage.getItem('vsPlayerHidden') === '1'; }catch(e){}
  setPlayerHidden(hidden);
  if(fileMode){
    document.body.classList.add('file-mode');
    document.getElementById('file-notice').hidden = false;
  }
  applyDocs(embedded);
  if(live){
    refresh();
    setInterval(refresh, 30000);
    fetch('api/ping').then(function(r){ return r.json(); }).then(function(j){
      if(j.service === 'video_summary'){ state.api = true; renderNav(); }
    }).catch(function(){});
  }
})();
</script>
</body>
</html>
'''


def dir_port(base):
    """每个工作目录一个确定性端口，避免多目录冲突。"""
    return 18900 + int(hashlib.md5(base.encode('utf-8')).hexdigest(), 16) % 100


def _listening(port):
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(('127.0.0.1', port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _ping(port):
    """端口上是否为本 skill 的 serve.py（带删除 API）。"""
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/api/ping' % port, timeout=1) as r:
            return json.loads(r.read().decode('utf-8')).get('service') == 'video_summary'
    except Exception:
        return False


def _replace_legacy_server(port):
    """端口被旧版纯静态 http.server 占用时结束它；只杀命令行含本 skill 服务特征的进程。"""
    if os.name == 'nt':
        return
    try:
        pids = subprocess.run(['lsof', '-ti', ':%d' % port],
                              capture_output=True, text=True).stdout.split()
        for pid in pids:
            cmdline = subprocess.run(['ps', '-p', pid, '-o', 'command='],
                                     capture_output=True, text=True).stdout
            if 'http.server' in cmdline or 'serve.py' in cmdline:
                subprocess.run(['kill', pid], capture_output=True)
        time.sleep(0.5)
    except Exception:
        pass


def ensure_server(base, port):
    """确保后台运行本 skill 的 serve.py（仅监听 127.0.0.1，重启后失效，不装任何常驻项）。"""
    if _listening(port):
        if _ping(port):
            return True
        _replace_legacy_server(port)
        if _listening(port):
            return False  # 端口被未知进程占用，如实报告
    python = shutil.which('python3') or sys.executable
    serve = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'serve.py')
    cmd = [python, serve, '--dir', base, '--port', str(port)]
    kwargs = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL,
              'stdin': subprocess.DEVNULL}
    if os.name == 'nt':
        kwargs['creationflags'] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True
    subprocess.Popen(cmd, **kwargs)
    for _ in range(15):
        if _listening(port):
            return True
        time.sleep(0.2)
    return False


def main():
    ap = argparse.ArgumentParser(description='生成 视频总结.html')
    ap.add_argument('--dir', default='.', help='工作目录（包含 output/ 子目录）')
    args = ap.parse_args()
    base = os.path.abspath(args.dir)
    outdir = os.path.join(base, 'output')
    docs = []
    for p in sorted(glob.glob(os.path.join(outdir, '*.md'))):
        try:
            with open(p, encoding='utf-8') as f:
                md = f.read()
        except OSError as e:
            print(json.dumps({'status': 'warn', 'file': p, 'error': str(e)}, ensure_ascii=False))
            continue
        docs.append({'file': os.path.basename(p), 'mtime': os.path.getmtime(p), 'md': md})
    port = dir_port(base)
    data = json.dumps(docs, ensure_ascii=False).replace('</', '<\\/')
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    html = (TEMPLATE.replace('__DATA__', data)
            .replace('__GENERATED_AT__', now)
            .replace('__PORT__', str(port)))
    out_path = os.path.join(base, '视频总结.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    bridge_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bili_bridge.user.js')
    bridge_url = None
    try:
        shutil.copyfile(bridge_src, os.path.join(base, 'bili_bridge.user.js'))
        bridge_url = 'http://127.0.0.1:%d/bili_bridge.user.js' % port
    except OSError:
        pass
    serving = ensure_server(base, port)
    url = 'http://127.0.0.1:%d/%s' % (port, '视频总结.html')
    print(json.dumps({'status': 'ok', 'html': out_path, 'docs': len(docs),
                      'server_running': serving, 'url': url,
                      'bili_bridge': bridge_url}, ensure_ascii=False))


if __name__ == '__main__':
    main()
