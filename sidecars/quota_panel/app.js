(() => {
  const REFRESH_MS = 30_000;
  const COUNTDOWN_TICK_MS = 30_000;
  const MODE_STORAGE_KEY = 'antigravity.quotaPanel.displayMode';

  const SOURCE_LABELS = {
    conversation: '当前对话',
    recent_conversation: '最近对话',
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
    accountsData: null,
    expanded: false,
    accountsExpanded: false,
    importOpen: false,
    editingRemarkId: null,
    confirmDeleteId: null,
    pendingSwitchAccount: null,
    accountToast: null, // { text, kind: 'info'|'success'|'error' }
    busyAction: null,
    switchingBanner: null, // { text, kind: 'info'|'success'|'error' }
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

  async function postJson(path, body = {}) {
    const fn = window.sidecar?.fetch ? window.sidecar.fetch.bind(window.sidecar) : fetch;
    const res = await fn(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    let payload = null;
    try {
      payload = await res.json();
    } catch {
      payload = null;
    }
    if (!res.ok) {
      throw new Error(payload?.error || `HTTP ${res.status}`);
    }
    return payload || {};
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

  function shortGroupName(name) {
    const s = String(name || '').trim();
    if (/claude|gpt/i.test(s)) return 'Claude/GPT';
    if (/argon/i.test(s)) return 'Argon';
    if (/carbon/i.test(s)) return 'Carbon';
    if (/gemini/i.test(s)) return 'Gemini';
    return s.slice(0, 14) || 'Group';
  }

  // ---------- Account bar & drawer rendering ----------
  function renderCachedQuotaChips(lastQuota) {
    const groups = lastQuota?.groups;
    if (!Array.isArray(groups) || !groups.length) return '';
    const isUsed = state.mode === 'used';
    const chips = groups
      .slice(0, 3)
      .map((g) => {
        const fhVal = isUsed ? g.fiveHourUsed : g.fiveHourRemaining;
        const wkVal = isUsed ? g.weeklyUsed : g.weeklyRemaining;
        const parts = [];
        if (typeof fhVal === 'number') parts.push(`5h ${fmtPct(fhVal)}`);
        if (typeof wkVal === 'number') parts.push(`周 ${fmtPct(wkVal)}`);
        if (!parts.length) return '';
        return `<span class="quota-mini-chip">${esc(shortGroupName(g.displayName))} · ${esc(parts.join(' / '))}</span>`;
      })
      .filter(Boolean);
    if (!chips.length) return '';
    return `<div class="account-quota-chips">${chips.join('')}</div>`;
  }

  function renderAccountSection() {
    const accState = state.accountsData;
    const accounts = accState?.accounts || [];
    const live = accState?.liveAccount || state.data?.account || null;
    const currentSaved = accounts.find((a) => a.isCurrent);

    const dot = $('account-live-dot');
    const labelEl = $('active-account-label');
    const statusPill = $('active-account-status');
    const saveBtn = $('save-current-btn');

    if (currentSaved) {
      dot.className = 'account-dot';
      labelEl.textContent = currentSaved.remark
        ? `${currentSaved.remark} (${currentSaved.email})`
        : currentSaved.email;
      labelEl.title = currentSaved.email;
      statusPill.textContent = currentSaved.tier || '已保存';
      statusPill.className = 'pill pill-accent';
      saveBtn.textContent = '更新凭据';
    } else if (live?.email) {
      dot.className = 'account-dot is-unsaved';
      labelEl.textContent = live.email;
      labelEl.title = live.email;
      statusPill.textContent = '未保存';
      statusPill.className = 'pill pill-muted';
      saveBtn.textContent = '保存当前';
    } else {
      dot.className = 'account-dot is-offline';
      labelEl.textContent = accounts.length ? '未检测到在线账号' : '未保存账号快照';
      labelEl.title = '';
      statusPill.classList.add('hidden');
      saveBtn.textContent = '保存当前';
    }

    saveBtn.disabled = Boolean(state.busyAction);
    $('accounts-count-badge').textContent = String(accounts.length);
    const toggleBtn = $('accounts-toggle-btn');
    toggleBtn.setAttribute('aria-expanded', String(state.accountsExpanded));
    $('account-drawer').classList.toggle('hidden', !state.accountsExpanded);

    // Switch banner from active click OR persisted switch_status.json
    const bannerEl = $('switch-status-banner');
    let banner = state.switchingBanner;
    if (!banner && accState?.switchStatus?.state === 'failed' && accState.switchStatus.error) {
      banner = { text: `上次切换未完成：${accState.switchStatus.error}`, kind: 'error' };
    }
    if (banner) {
      bannerEl.className = `switch-status-banner ${banner.kind === 'error' ? 'is-error' : banner.kind === 'success' ? 'is-success' : ''}`;
      bannerEl.innerHTML = `<span>${esc(banner.text)}</span><button type="button" class="icon-mini-btn" data-action="dismiss-banner">关闭</button>`;
      bannerEl.classList.remove('hidden');
    } else {
      bannerEl.classList.add('hidden');
    }

    // Drawer toolbar badges & panels
    const agyCountEl = $('agy-switch-count');
    if (accState?.agySwitchDetected && accState.agySwitchCount > 0) {
      agyCountEl.textContent = String(accState.agySwitchCount);
      agyCountEl.classList.remove('hidden');
    } else {
      agyCountEl.classList.add('hidden');
    }

    $('import-panel').classList.toggle('hidden', !state.importOpen);

    const toastEl = $('account-toast');
    if (state.accountToast) {
      toastEl.textContent = state.accountToast.text;
      toastEl.className = `account-toast ${state.accountToast.kind === 'error' ? 'is-error' : state.accountToast.kind === 'success' ? 'is-success' : ''}`;
      toastEl.classList.remove('hidden');
    } else {
      toastEl.classList.add('hidden');
    }

    const listEl = $('accounts-list');
    if (!accounts.length) {
      listEl.innerHTML = '<div class="accounts-empty">暂无已保存账号。点击「保存当前凭据」或从 agy-switch / JSON 导入。</div>';
    } else {
      const switchLocked = Boolean(accState?.switchLocked || state.busyAction === 'switch');
      listEl.innerHTML = accounts.map((acc) => {
        const displayTitle = acc.remark ? acc.remark : acc.email;
        const subParts = [];
        if (acc.remark) subParts.push(acc.email);
        else if (acc.name) subParts.push(acc.name);
        if (acc.lastQuota?.fetchedAt) {
          subParts.push(`缓存于 ${fmtRelativeUpdated(acc.lastQuota.fetchedAt)}`);
        }

        const isEditing = state.editingRemarkId === acc.id;
        const isDeleting = state.confirmDeleteId === acc.id;

        const actionBtns = isDeleting
          ? `
            <button type="button" class="tool-btn tool-btn-danger" data-action="confirm-delete" data-id="${esc(acc.id)}">确认删除</button>
            <button type="button" class="icon-mini-btn" data-action="cancel-delete">取消</button>
          `
          : `
            <button type="button" class="icon-mini-btn" data-action="edit-remark" data-id="${esc(acc.id)}" title="编辑备注">备注</button>
            ${
              acc.isCurrent
                ? '<span class="pill pill-accent">当前</span>'
                : `<button type="button" class="tool-btn tool-btn-primary" data-action="request-switch" data-id="${esc(acc.id)}" ${(!acc.hasCredential || switchLocked) ? 'disabled' : ''}>切换</button>`
            }
            <button type="button" class="icon-mini-btn" data-action="ask-delete" data-id="${esc(acc.id)}" title="从本地库移除">删除</button>
          `;

        const remarkEditor = isEditing
          ? `
            <div class="remark-edit-row">
              <input type="text" class="remark-input" id="remark-input-${esc(acc.id)}" maxlength="40" value="${esc(acc.remark || '')}" placeholder="输入简短备注（最多40字）">
              <button type="button" class="tool-btn tool-btn-primary" data-action="save-remark" data-id="${esc(acc.id)}">保存</button>
              <button type="button" class="icon-mini-btn" data-action="cancel-remark">取消</button>
            </div>
          `
          : '';

        return `
          <div class="account-row ${acc.isCurrent ? 'is-current' : ''}">
            <div class="account-row-top">
              <div class="account-row-main">
                <div class="account-row-title">
                  <span class="account-row-name">${esc(displayTitle)}</span>
                  ${acc.tier ? `<span class="pill pill-muted">${esc(acc.tier)}</span>` : ''}
                  ${!acc.hasCredential ? '<span class="pill pill-muted">缺凭据</span>' : ''}
                </div>
                ${subParts.length ? `<div class="account-row-sub">${esc(subParts.join(' · '))}</div>` : ''}
              </div>
              <div class="account-row-actions">${actionBtns}</div>
            </div>
            ${remarkEditor}
            ${renderCachedQuotaChips(acc.lastQuota)}
          </div>
        `;
      }).join('');
    }

    // Confirmation modal
    const modal = $('switch-confirm-modal');
    if (state.pendingSwitchAccount) {
      const target = state.pendingSwitchAccount;
      const fromText = currentSaved?.email || live?.email || '当前登录账号';
      const toText = target.remark ? `${target.remark} (${target.email})` : target.email;
      $('switch-modal-desc').textContent = `即将从「${fromText}」切换到「${toText}」。`;
      $('switch-confirm-btn').disabled = state.busyAction === 'switch';
      modal.classList.remove('hidden');
    } else {
      modal.classList.add('hidden');
    }
  }

  // ---------- Quota rendering ----------
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

    renderAccountSection();

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

  function showToast(text, kind = 'info') {
    state.accountToast = { text, kind };
    render();
  }

  // ---------- Data fetching & account actions ----------
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

      const [quotaResult, accountsResult] = await Promise.allSettled([
        fetchJson(`/api/quota${query ? `?${query}` : ''}`),
        fetchJson(`/api/accounts${convId ? `?conversationId=${encodeURIComponent(convId)}` : ''}`),
      ]);

      if (quotaResult.status === 'fulfilled') {
        state.data = quotaResult.value;
        state.error = quotaResult.value?.error || null;
      } else {
        state.error = quotaResult.reason?.message || String(quotaResult.reason);
      }

      if (accountsResult.status === 'fulfilled') {
        state.accountsData = accountsResult.value;
      }
    } catch (err) {
      state.error = err?.message || String(err);
    } finally {
      state.loading = false;
      state.refreshing = false;
      render();
    }
  }

  async function captureCurrent() {
    if (state.busyAction) return;
    state.busyAction = 'capture';
    render();
    try {
      const res = await postJson('/api/accounts/capture', { conversationId: getConversationId() });
      if (res.accountsState) state.accountsData = res.accountsState;
      const email = res.account?.email || '';
      showToast(res.created ? `已保存账号快照：${email}` : `已更新账号凭据：${email}`, 'success');
    } catch (err) {
      state.accountsExpanded = true;
      showToast(err?.message || String(err), 'error');
    } finally {
      state.busyAction = null;
      render();
    }
  }

  async function importAgySwitch() {
    if (state.busyAction) return;
    state.busyAction = 'import-agy';
    render();
    try {
      const res = await postJson('/api/accounts/import-agy-switch', { conversationId: getConversationId() });
      if (res.accountsState) state.accountsData = res.accountsState;
      const summary = `导入完成：新增 ${res.imported || 0}，更新 ${res.updated || 0}` +
        (res.skipped ? `，跳过 ${res.skipped}` : '');
      showToast(summary, res.errors?.length && !res.imported && !res.updated ? 'error' : 'success');
    } catch (err) {
      showToast(err?.message || String(err), 'error');
    } finally {
      state.busyAction = null;
      render();
    }
  }

  async function submitImportJson() {
    if (state.busyAction) return;
    const content = $('import-json-input').value;
    state.busyAction = 'import-json';
    render();
    try {
      const res = await postJson('/api/accounts/import-json', {
        content,
        conversationId: getConversationId(),
      });
      if (res.accountsState) state.accountsData = res.accountsState;
      const summary = `JSON 导入：新增 ${res.imported || 0}，更新 ${res.updated || 0}` +
        (res.failed ? `，失败 ${res.failed}` : '');
      if (!res.failed) {
        $('import-json-input').value = '';
        state.importOpen = false;
      }
      showToast(
        res.errors?.length ? `${summary}（${res.errors[0]}）` : summary,
        res.failed && !res.imported && !res.updated ? 'error' : 'success',
      );
    } catch (err) {
      showToast(err?.message || String(err), 'error');
    } finally {
      state.busyAction = null;
      render();
    }
  }

  async function exportAccounts() {
    if (state.busyAction) return;
    state.busyAction = 'export';
    render();
    try {
      const res = await postJson('/api/accounts/export', {});
      const blob = new Blob([res.content], { type: 'application/json;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = res.filename || 'antigravity-accounts.json';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      showToast(`已导出 ${res.count || 0} 个账号备份（含登录凭据，请勿外泄）`, 'success');
    } catch (err) {
      showToast(err?.message || String(err), 'error');
    } finally {
      state.busyAction = null;
      render();
    }
  }

  async function confirmSwitchAccount() {
    const target = state.pendingSwitchAccount;
    if (!target || state.busyAction) return;
    state.busyAction = 'switch';
    render();
    try {
      const res = await postJson('/api/accounts/switch', { id: target.id });
      state.pendingSwitchAccount = null;
      state.switchingBanner = {
        text: res.message || `正在切换到 ${target.email}，Antigravity 即将重启...`,
        kind: 'info',
      };
    } catch (err) {
      state.pendingSwitchAccount = null;
      state.switchingBanner = {
        text: `切换失败：${err?.message || String(err)}`,
        kind: 'error',
      };
    } finally {
      state.busyAction = null;
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

  $('save-current-btn').addEventListener('click', captureCurrent);
  $('drawer-capture-btn').addEventListener('click', captureCurrent);

  $('accounts-toggle-btn').addEventListener('click', () => {
    state.accountsExpanded = !state.accountsExpanded;
    state.confirmDeleteId = null;
    state.editingRemarkId = null;
    render();
  });

  $('import-agy-btn').addEventListener('click', importAgySwitch);
  $('export-accounts-btn').addEventListener('click', exportAccounts);

  $('toggle-import-json-btn').addEventListener('click', () => {
    state.importOpen = !state.importOpen;
    render();
  });

  $('import-cancel-btn').addEventListener('click', () => {
    state.importOpen = false;
    render();
  });

  $('import-submit-btn').addEventListener('click', submitImportJson);

  $('import-file-input').addEventListener('change', (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      $('import-json-input').value = String(reader.result || '');
    };
    reader.readAsText(file, 'utf-8');
    e.target.value = '';
  });

  $('switch-status-banner').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-action="dismiss-banner"]');
    if (!btn) return;
    state.switchingBanner = null;
    if (state.accountsData?.switchStatus?.state === 'failed') {
      state.accountsData.switchStatus = null;
    }
    render();
  });

  $('accounts-list').addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    const action = btn.getAttribute('data-action');
    const id = btn.getAttribute('data-id');
    const accounts = state.accountsData?.accounts || [];

    if (action === 'edit-remark') {
      state.editingRemarkId = id;
      state.confirmDeleteId = null;
      render();
      const input = $(`remark-input-${id}`);
      if (input) input.focus();
    } else if (action === 'cancel-remark') {
      state.editingRemarkId = null;
      render();
    } else if (action === 'save-remark') {
      const input = $(`remark-input-${id}`);
      const remark = input ? input.value : '';
      try {
        const res = await postJson('/api/accounts/remark', {
          id,
          remark,
          conversationId: getConversationId(),
        });
        if (res.accountsState) state.accountsData = res.accountsState;
        state.editingRemarkId = null;
        showToast('备注已更新', 'success');
      } catch (err) {
        showToast(err?.message || String(err), 'error');
      }
    } else if (action === 'ask-delete') {
      state.confirmDeleteId = id;
      state.editingRemarkId = null;
      render();
    } else if (action === 'cancel-delete') {
      state.confirmDeleteId = null;
      render();
    } else if (action === 'confirm-delete') {
      try {
        const res = await postJson('/api/accounts/delete', {
          id,
          conversationId: getConversationId(),
        });
        if (res.accountsState) state.accountsData = res.accountsState;
        state.confirmDeleteId = null;
        showToast('已从本地账号库移除', 'success');
      } catch (err) {
        showToast(err?.message || String(err), 'error');
      }
    } else if (action === 'request-switch') {
      const target = accounts.find((a) => a.id === id);
      if (target) {
        state.pendingSwitchAccount = target;
        render();
      }
    }
  });

  $('accounts-list').addEventListener('keydown', (e) => {
    const input = e.target.closest('.remark-input');
    if (!input) return;
    if (e.key === 'Enter') {
      const id = input.id.replace(/^remark-input-/, '');
      const saveBtn = $('accounts-list').querySelector(`[data-action="save-remark"][data-id="${CSS.escape(id)}"]`);
      if (saveBtn) saveBtn.click();
    } else if (e.key === 'Escape') {
      state.editingRemarkId = null;
      render();
    }
  });

  $('switch-cancel-btn').addEventListener('click', () => {
    state.pendingSwitchAccount = null;
    render();
  });

  $('switch-confirm-btn').addEventListener('click', confirmSwitchAccount);

  $('switch-confirm-modal').addEventListener('click', (e) => {
    if (e.target === $('switch-confirm-modal')) {
      state.pendingSwitchAccount = null;
      render();
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && state.pendingSwitchAccount) {
      state.pendingSwitchAccount = null;
      render();
    }
  });

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
