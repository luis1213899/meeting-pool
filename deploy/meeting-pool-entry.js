/* 在工作台里注入「会议号池」入口。
 *
 * 为什么不改 React 源码：站点主 bundle 的线上版本与本地源码不同源，
 * 重新构建会把未发布的改动带上线。这里沿用站点已有的 bridge 注入方式
 * （参考 console-admin-bridge.js），只做 DOM 层面的增补，可随时撤掉。
 *
 * 注入点：
 *   1. 左侧栏「聊天」分组，紧跟「魔法创意屋」后面
 *   2. 工作台概览页的快捷入口（.na-quick-links）
 */
(function () {
  'use strict';

  var HREF = '/tenxunhuiyiyuding/';
  var LABEL = '会议号池';
  var MARK = 'data-meeting-pool-entry';

  function makeLink(template, withExternalIcon) {
    var node = template.cloneNode(true);
    node.setAttribute('href', HREF);
    node.setAttribute(MARK, '1');
    node.setAttribute('title', '腾讯会议号池：借用团队会员号开会');
    node.removeAttribute('aria-current');
    var span = node.querySelector('span');
    if (span) span.textContent = LABEL;
    if (!withExternalIcon) {
      var ext = node.querySelector('.sidebar-external');
      if (ext) ext.remove();
    }
    return node;
  }

  function patchSidebar() {
    var sidebar = document.getElementById('console-sidebar');
    if (!sidebar) return;
    var groups = sidebar.querySelectorAll('section.sidebar-group');
    for (var i = 0; i < groups.length; i++) {
      var links = groups[i].querySelectorAll('nav a');
      for (var j = 0; j < links.length; j++) {
        var span = links[j].querySelector('span');
        if (!span || span.textContent.trim() !== '魔法创意屋') continue;
        var nav = links[j].parentNode;
        if (nav.querySelector('[' + MARK + ']')) return;
        nav.insertBefore(makeLink(links[j], true), links[j].nextSibling);
        return;
      }
    }
  }

  function patchQuickLinks() {
    var quick = document.querySelector('.na-quick-links');
    if (!quick || quick.querySelector('[' + MARK + ']')) return;
    var links = quick.querySelectorAll('a');
    for (var i = 0; i < links.length; i++) {
      if (links[i].getAttribute('href') !== '/media/') continue;
      quick.appendChild(makeLink(links[i], false));
      return;
    }
  }

  function patch() {
    try { patchSidebar(); patchQuickLinks(); } catch (e) { /* 静默：注入失败不影响站点 */ }
  }

  var timer = null;
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(patch, 120);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', schedule);
  } else {
    schedule();
  }
  new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
})();
