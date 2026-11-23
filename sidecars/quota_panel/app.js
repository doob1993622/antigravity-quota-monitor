(() => {
  const REFRESH_MS = 30_000;
  const COUNTDOWN_TICK_MS = 30_000;
  const MODE_STORAGE_KEY = 'antigravity.quotaPanel.displayMode';

  const SOURCE_LABELS = {
    conversation: '当前对话',
    last_selected: '最近选用',
    default: '默认模型',
    fallback: '默认回退',
  };

  const $ = (id) => document.getElementById(id);

  function loadMode() {
    try {
      const saved = localStorage.getItem(MODE_STORAGE_KEY);
      if (saved === 'used' || saved === 'remaining') return saved;
    } catch {
      // Ignore storage failures inside sandboxed iframes.
    }
    return 'used';
  }

  function saveMode(mode) {
    try {
      localStorage.setItem(MODE_STORAGE_KEY, mode);
    } catch {
      // Ignore storage failures inside sandboxed iframes.
    }
  }

  const state = {
    data: null,
    expanded: false, // Always collapsed on initial open.
    mode: loadMode(),
    loading: true,
    refreshing: false,
    error: null,
  };

  const params = new URLSearchParams(location.search);
  const getConversationId = () =>
    window.sidecar?.conversationId || params.get('conversationId') || '';

  async function fetchJson(path) {
    const fn = window.sidecar?.fetch ? window.sidecar.fetch.bind(window.sidecar) : fetch;
    const res = await fn(path, { method: 'GET' });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    return res.json();
  }

  // ---------- Host theme synchronization ----------
  function syncScheme() {
    const probe = document.createElement('span');
    probe.style.cssText = 'position:absolute;visibility:hidden;color:var(--background, #f7f7f8)';
    document.documentElement.appendChild(probe);
    const color = getComputedStyle(probe).color;
    probe.remove();
    const nums = (color.match(/[\d.]+/g) || []).map(Number);
    if (nums.length < 3) return;
    const scale = color.startsWith('color(') ? 1 : 255;
    const [r, g, b] = nums.slice(0, 3).map((v) => v / scale);
    const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
    const theme = lum < 0.5 ? 'dark' : 'light';
    if (document.documentElement.dataset.theme !== theme) {
      document.documentElement.dataset.theme = theme;
    }
  }
  syncScheme();
  new MutationObserver(() => syncScheme()).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['style'],
  });

  // ---------- Formatting & escaping helpers ----------
  const esc = (s) =>
    String(s ?? '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[c]));

  function fmtPct(v) {
    if (typeof v !== 'number' || !Number.isFinite(v)) return '--';
    return Number.isInteger(v) ? `${v}%` : `${v.toFixed(1)}%`;
  }

  function fmtFullDateTime(ms) {
    if (!Number.isFinite(ms)) return '';
    try {
      return new Intl.DateTimeFormat('zh-CN', {
        dateStyle: 'medium',
        timeStyle: 'short',
      }).format(new Date(ms));
    } catch {
      return new Date(ms).toLocaleString();
    }
  }

  function fmtCountdown(resetMs) {
    if (!Number.isFinite(resetMs)) return '重置时间未知';
    const diffMs = resetMs - Date.now();
    if (diffMs <= 0) return '即将重置';
    const totalMin = Math.floor(diffMs / 60_000);
    if (totalMin < 1) return '不到 1 分钟后重置';
    const days = Math.floor(totalMin / 1440);
    const hours = Math.floor((totalMin % 1440) / 60);
    const mins = totalMin % 60;
    if (days > 0) return `${days}天${hours}小时后重置`;
    if (hours > 0) return `${hours}小时${mins}分后重置`;
    return `${mins}分钟后重置`;
  }

  function fmtRelativeUpdated(fetchedAt) {
    if (!Number.isFinite(fetchedAt)) return '刚刚更新';
    const sec = Math.max(0, Math.floor((Date.now() - fetchedAt) / 1000));
    if (sec < 10) return '刚刚更新';
    if (sec < 60) return `${sec} 秒前更新`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min} 分钟前更新`;
    return `${fmtFullDateTime(fetchedAt)} 更新`;
  }

  function toneClass(usedPercent) {
    if (usedPercent >= 85) return 'tone-crit';
    if (usedPercent >= 60) return 'tone-warn';
    return 'tone-ok';
  }

  // ---------- Rendering ----------
  function renderMeterHtml(label, bucket, missingText) {
    if (!bucket) {
      return `
        <div class="meter-note">
          <span>${esc(label)}</span>
          <span>${esc(missingText)}</span>
        </div>
      `;
    }

    const used = Number(bucket.usedPercent ?? 0);
    const rem = Number(bucket.remainingPercent ?? Math.max(0, 100 - used));
    const isUsedMode = state.mode === 'used';
    const primaryText = isUsedMode ? `已用 ${fmtPct(used)}` : `剩余 ${fmtPct(rem)}`;
    const secondaryText = isUsedMode ? `剩余 ${fmtPct(rem)}` : `已用 ${fmtPct(used)}`;
    const barWidth = Math.min(100, Math.max(0, isUsedMode ? used : rem));
    const tone = toneClass(used);
    const resetMs = Number.isFinite(bucket.resetMs)
      ? bucket.resetMs
      : (bucket.resetTime ? Date.parse(bucket.resetTime) : NaN);
    const resetTitle = Number.isFinite(resetMs) ? `预计重置时间：${fmtFullDateTime(resetMs)}` : '';
    const resetText = fmtCountdown(resetMs);

    return `
      <div class="meter">
        <div class="meter-top">
          <span class="meter-label">${esc(label)}</span>
          <span class="meter-values">
            <span class="meter-primary">${esc(primaryText)}</span>
            <span class="meter-secondary">· ${esc(secondaryText)}</span>
          </span>
        </div>
        <div class="bar-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${esc(String(barWidth))}">
          <div class="bar-fill ${tone}" style="width: ${barWidth}%"></div>
        </div>
        <div class="meter-sub" ${Number.isFinite(resetMs) ? `data-reset-ms="${resetMs}"` : ''} title="${esc(resetTitle)}">${esc(resetText)}</div>
      </div>
    `;
  }

  function renderFamiliesHtml(families) {
    if (!Array.isArray(families) || !families.length) {
      return '<div class="meter-sub">暂无归属此分组的可用模型</div>';
    }
    return `
      <div class="family-list">
        ${families.map((fam) => {
          const chips = fam.variants
            .filter((v) => v.tierLabel || v.isCurrent)
            .map((v) => {
              const chipText = v.tierLabel || '当前';
              return `<span class="chip ${v.isCurrent ? 'is-current' : ''}">${esc(chipText)}</span>`;
            })
            .join('');
          return `
            <div class="family-row ${fam.hasCurrent ? 'is-current' : ''}">
              <span class="family-name">${esc(fam.name)}</span>
              ${chips ? `<div class="family-chips">${chips}</div>` : ''}
            </div>
          `;
        }).join('')}
      </div>
    `;
  }

  function render() {
    const modeUsed = $('mode-used');
    const modeRem = $('mode-remaining');
    modeUsed.classList.toggle('active', state.mode === 'used');
    modeUsed.setAttribute('aria-pressed', String(state.mode === 'used'));
    modeRem.classList.toggle('active', state.mode === 'remaining');
    modeRem.setAttribute('aria-pressed', String(state.mode === 'remaining'));

    $('refresh-btn').classList.toggle('spinning', state.refreshing);

    $('loading').classList.toggle('hidden', !state.loading || Boolean(state.data));

    const errText = state.error || state.data?.error || null;
    const hasGroups = Boolean(state.data?.groups?.length);
    const showErrorBanner = Boolean(errText && !hasGroups);
    $('error-banner').classList.toggle('hidden', !showErrorBanner);
    if (showErrorBanner) {
      $('error-text').textContent = `获取用量失败：${errText}`;
    }

    $('stale-badge').classList.toggle('hidden', !state.data?.stale);

    if (state.data) {
      $('updated-at').textContent = fmtRelativeUpdated(state.data.fetchedAt);
    }

    if (!state.data || !hasGroups) {
      $('app-view').classList.add('hidden');
      return;
    }

    $('app-view').classList.remove('hidden');

    const planText = state.data.tier?.planName || state.data.tier?.name || '';
    $('plan-badge').textContent = planText;
    $('plan-badge').classList.toggle('hidden', !planText);

    const cur = state.data.current;
    $('current-model-name').textContent = cur?.label || '未识别到当前模型';
    const srcText = cur?.source ? (SOURCE_LABELS[cur.source] || cur.source) : '';
    $('current-source-badge').textContent = srcText;
    $('current-source-badge').classList.toggle('hidden', !srcText);
    $('current-group-badge').textContent = cur?.groupName || '';
    $('current-group-badge').classList.toggle('hidden', !cur?.groupName);

    $('current-meters').innerHTML =
      renderMeterHtml('5 小时用量', cur?.fiveHour, '无独立 5h 限额（按周计）') +
      renderMeterHtml('一周用量', cur?.weekly, '暂无周限额数据');

    const groups = state.data.groups || [];
    const totalModels = groups.reduce((sum, g) => sum + (g.modelCount || 0), 0);
    const cardBtn = $('current-card');
    cardBtn.setAttribute('aria-expanded', String(state.expanded));
    $('expand-hint').textContent = state.expanded
      ? '点击收起全部模型用量'
      : `点击展开全部模型用量 (${groups.length} 组 · ${totalModels} 个模型)`;

    const allModelsEl = $('all-models');
    allModelsEl.classList.toggle('hidden', !state.expanded);

    $('groups-list').innerHTML = groups.map((g) => {
      const fiveHourHtml = renderMeterHtml('5 小时用量', g.fiveHour, '仅周限额');
      const weeklyHtml = renderMeterHtml('一周用量', g.weekly, '暂无周限额数据');
      const extraHtml = (g.extraBuckets || [])
        .map((b) => renderMeterHtml(b.displayName || b.bucketId || '其他配额', b, ''))
        .join('');

      return `
        <article class="group-card ${g.isCurrentGroup ? 'is-current' : ''}">
          <div class="group-head">
            <div class="group-title-wrap">
              <h2 class="group-title">${esc(g.displayName)}</h2>
              ${g.isCurrentGroup ? '<span class="pill pill-accent">当前</span>' : ''}
            </div>
            <span class="group-count">${esc(String(g.modelCount || 0))} 个模型</span>
          </div>
          <div class="meters">
            ${fiveHourHtml}
            ${weeklyHtml}
            ${extraHtml}
          </div>
          ${renderFamiliesHtml(g.families)}
        </article>
      `;
    }).join('');

    const disabledCount = state.data.disabledModels?.length || 0;
    const footerLines = [
      '同组内模型共享该组的 5 小时与一周配额，按 Token 成本比例消耗。',
      disabledCount > 0 ? `另有 ${disabledCount} 个已停用模型未计入展示。` : '',
    ].filter(Boolean);
    const footerEl = $('quota-footer');
    footerEl.innerHTML = footerLines.map((line) => `<div>${esc(line)}</div>`).join('');
    footerEl.classList.toggle('hidden', footerLines.length === 0);
  }

  function tickRelativeTexts() {
    if (state.data?.fetchedAt) {
      $('updated-at').textContent = fmtRelativeUpdated(state.data.fetchedAt);
    }
    document.querySelectorAll('[data-reset-ms]').forEach((el) => {
      const ms = Number(el.getAttribute('data-reset-ms'));
      if (Number.isFinite(ms)) {
        el.textContent = fmtCountdown(ms);
      }
    });
  }

  // ---------- Data fetching ----------
  async function loadQuota({ force = false } = {}) {
    if (state.refreshing) return;
    state.refreshing = true;
    render();
    try {
      const convId = getConversationId();
      const qs = new URLSearchParams();
      if (convId) qs.set('conversationId', convId);
      if (force) qs.set('force', '1');
      const query = qs.toString();
      const payload = await fetchJson(`/api/quota${query ? `?${query}` : ''}`);
      state.data = payload;
      state.error = payload.error || null;
    } catch (err) {
      state.error = err?.message || String(err);
    } finally {
      state.loading = false;
      state.refreshing = false;
      render();
    }
  }

  // ---------- Event bindings ----------
  $('current-card').addEventListener('click', () => {
    state.expanded = !state.expanded;
    render();
  });

  $('mode-used').addEventListener('click', () => {
    if (state.mode === 'used') return;
    state.mode = 'used';
    saveMode('used');
    render();
  });

  $('mode-remaining').addEventListener('click', () => {
    if (state.mode === 'remaining') return;
    state.mode = 'remaining';
    saveMode('remaining');
    render();
  });

  $('refresh-btn').addEventListener('click', () => loadQuota({ force: true }));
  $('retry-btn').addEventListener('click', () => loadQuota({ force: true }));

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      loadQuota({ force: false });
    }
  });

  setInterval(() => {
    if (document.visibilityState === 'visible') {
      loadQuota({ force: false });
    }
  }, REFRESH_MS);

  setInterval(tickRelativeTexts, COUNTDOWN_TICK_MS);

  loadQuota({ force: false });
})();
