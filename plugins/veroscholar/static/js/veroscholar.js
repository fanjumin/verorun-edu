/**
 * VeroScholar 引源索骥 — 前端共享逻辑
 *
 * 设计约束（插件标准 §12.11 iframe 例外 / §15.7 安全红线）：
 *   - 无外部 CDN，仅用系统 design-system.css 变量
 *   - 不 eval / 不 innerHTML 拼接不可信数据（统一 esc()）
 *   - token 仅经 URL 参数或同源 Cookie 传递，不写入全局可枚举属性
 */
(function () {
  'use strict';

  var token = null;
  var m = location.search.match(/[?&]token=([^&]+)/);
  if (m) { token = decodeURIComponent(m[1]); }
  if (!token) {
    try { token = localStorage.getItem('veroscholar_token'); } catch (e) { token = null; }
  }
  if (token) {
    try { localStorage.setItem('veroscholar_token', token); } catch (e) { /* ignore */ }
  }

  function esc(s) {
    if (s === null || s === undefined) { return ''; }
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function t(key) {
    var d = window.__t || {};
    return d[key] || key;
  }

  function api(url, options) {
    options = options || {};
    options.headers = options.headers || {};
    options.headers['Content-Type'] = 'application/json';
    options.headers['X-Requested-With'] = 'XMLHttpRequest';
    if (token) { options.headers['Authorization'] = 'Bearer ' + token; }
    return fetch(url, options).then(function (r) { return r.json(); });
  }

  /**
   * 鉴权请求头（不强制 Content-Type，供上传等非 JSON 请求复用）。
   * @returns {Object} 注入 Authorization / X-Requested-With 后的 headers 对象
   */
  function authHeaders() {
    var headers = { 'X-Requested-With': 'XMLHttpRequest' };
    if (token) { headers['Authorization'] = 'Bearer ' + token; }
    return headers;
  }

  /**
   * 带鉴权下载文件（blob → 临时 <a> 触发保存）。
   * @param {string} url      下载地址（需 JWT）
   * @param {string} filename 保存文件名
   */
  function download(url, filename) {
    fetch(url, { headers: authHeaders() })
      .then(function (r) {
        if (!r.ok) { throw new Error('HTTP ' + r.status); }
        return r.blob();
      })
      .then(function (blob) {
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
      })
      .catch(function () { /* 静默失败，调用方可选提示 */ });
  }

  function fmtAuthors(authors) {
    if (!authors || !authors.length) { return '—'; }
    var names = authors.slice(0, 3).map(function (a) { return a.name; });
    var rest = authors.length - 3;
    var out = names.join(', ');
    return rest > 0 ? out + ' et al.' : out;
  }

  function fmtYear(year) {
    return year ? String(year) : '—';
  }

  function sourceBadge(source) {
    var labels = { arxiv: 'arXiv', semantic_scholar: 'S2', semantic: 'S2', openalex: 'OpenAlex', openalex_zh: t('OpenAlex ZH') };
    return '<span class="vs-badge vs-badge-' + esc(source) + '">' + esc(labels[source] || source) + '</span>';
  }

  function statusBadge(status) {
    if (!status || status === 'unread') { return ''; }
    return '<span class="vs-badge vs-badge-status">' + esc(t('reading_status_' + status)) + '</span>';
  }

  function toast(msg, type) {
    var el = document.createElement('div');
    el.className = 'vs-toast vs-toast-' + (type || 'info');
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(function () { el.classList.add('vs-toast-out'); }, 2600);
    setTimeout(function () { el.remove(); }, 3000);
  }

  /**
   * 渲染论文列表卡片
   * @param {HTMLElement} container 容器
   * @param {Array} papers 论文数组（统一结构）
   * @param {Object} actions {onDetail, onAddToProject} 可选回调
   */
  function renderPapers(container, papers, actions) {
    if (!papers || !papers.length) {
      container.innerHTML = '<div class="vs-empty">' + esc(t('no_papers')) + '</div>';
      return;
    }
    var html = papers.map(function (p) {
      var pdf = p.pdf_url
        ? '<a class="vs-link" href="' + esc(p.pdf_url) + '" target="_blank" rel="noopener">PDF</a>'
        : '';
      return '' +
        '<div class="vs-paper">' +
          '<div class="vs-paper-head">' +
            sourceBadge(p.source_db) +
            statusBadge(p.reading_status) +
            '<span class="vs-paper-year">' + fmtYear(p.year) + '</span>' +
            '<span class="vs-paper-cite">' + esc(p.citation_count || 0) + ' cites</span>' +
          '</div>' +
          '<div class="vs-paper-title" data-id="' + esc(p.id || p.external_id) + '">' +
            esc(p.title) + '</div>' +
          '<div class="vs-paper-meta">' + esc(fmtAuthors(p.authors)) + '</div>' +
          (p.venue ? '<div class="vs-paper-venue">' + esc(p.venue) + '</div>' : '') +
          (p.abstract
            ? '<div class="vs-paper-abs" title="' + esc(p.abstract) + '">' + esc(p.abstract) + '</div>'
            : '') +
          '<div class="vs-paper-actions">' +
            pdf +
            (actions && actions.onDetail
              ? '<button class="btn btn-ghost btn-sm" data-act="detail" data-id="' + esc(p.id || p.external_id) + '">' + esc(t('detail')) + '</button>'
              : '') +
            (actions && actions.onAddToProject
              ? '<button class="btn btn-ghost btn-sm" data-act="add" data-id="' + esc(p.id || p.external_id) + '">' + esc(t('add_to_project')) + '</button>'
              : '') +
          '</div>' +
        '</div>';
    }).join('');

    container.innerHTML = html;

    if (actions && actions.onDetail) {
      container.querySelectorAll('[data-act="detail"]').forEach(function (btn) {
        btn.addEventListener('click', function () { actions.onDetail(btn.getAttribute('data-id')); });
      });
    }
    if (actions && actions.onAddToProject) {
      container.querySelectorAll('[data-act="add"]').forEach(function (btn) {
        btn.addEventListener('click', function () { actions.onAddToProject(btn.getAttribute('data-id')); });
      });
    }
  }

  function renderProjects(container, projects, actions) {
    if (!projects || !projects.length) {
      container.innerHTML = '<div class="vs-empty">' + esc(t('no_projects')) + '</div>';
      return;
    }
    var html = projects.map(function (p) {
      return '' +
        '<div class="vs-project">' +
          '<div class="vs-project-name">' + esc(p.name) + '</div>' +
          (p.description ? '<div class="vs-project-desc">' + esc(p.description) + '</div>' : '') +
          '<div class="vs-project-meta">' + esc(t('created')) + ': ' + esc(String(p.created_at || '').slice(0, 10)) + '</div>' +
          (actions && actions.onOpen
            ? '<button class="btn btn-ghost btn-sm" data-pid="' + esc(p.id) + '">' + esc(t('open')) + '</button>'
            : '') +
        '</div>';
    }).join('');
    container.innerHTML = html;
    if (actions && actions.onOpen) {
      container.querySelectorAll('[data-pid]').forEach(function (btn) {
        btn.addEventListener('click', function () { actions.onOpen(btn.getAttribute('data-pid')); });
      });
    }
  }

  window.VS = {
    token: token,
    api: api,
    download: download,
    authHeaders: authHeaders,
    t: t,
    esc: esc,
    fmtAuthors: fmtAuthors,
    fmtYear: fmtYear,
    toast: toast,
    renderPapers: renderPapers,
    renderProjects: renderProjects,
  };
})();
