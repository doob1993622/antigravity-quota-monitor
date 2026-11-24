/**
 * Antigravity Sidebar Model Quota Monitor & Local Account Switcher Sidecar Backend.
 *
 * Queries the Antigravity Language Server's Connect-RPC endpoints
 * (`RetrieveUserQuotaSummary` & `GetUserStatus`) and inspects the active
 * conversation transcript / client state to identify the current model, while
 * managing a pure-local credential snapshot vault for one-click account switching
 * (syncing Antigravity App, IDE 2.6+, and native `agy` CLI sessions).
 */
import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, unlinkSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { SidecarApp, Response } from 'sidecar_sdk';
import { createAccountStore, resolveVaultDir } from './accounts_store.mjs';
import { launchSwitchWorker } from './switch_worker.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ID_RE = /^[0-9a-f-]{8,}$/i;
const CACHE_TTL_MS = 10_000;
const RPC_TIMEOUT_MS = 8_000;
const RESET_MATCH_WINDOW_MS = 120_000;

const THINKING_SUFFIX_RE = /\s*\((Low|Medium|High|XHigh|Extra High|Minimal)\)\s*$/i;
const THINKING_ORDER = new Map([
  ['low', 1],
  ['medium', 2],
  ['high', 3],
  ['xhigh', 4],
  ['extra high', 4],
  ['minimal', 0],
]);

// ---------------------------------------------------------------------------
// File mtime/size cache (for transcript.jsonl and antigravity_state.pbtxt)
// ---------------------------------------------------------------------------
const fileCache = new Map();

function readCachedText(path) {
  let stat;
  try {
    stat = statSync(path);
  } catch {
    return null;
  }
  const key = `${stat.mtimeMs}:${stat.size}`;
  const hit = fileCache.get(path);
  if (hit && hit.key === key) {
    return hit.text;
  }
  try {
    const text = readFileSync(path, 'utf-8');
    fileCache.set(path, { key, text });
    return text;
  } catch {
    return null;
  }
}

function findAppDataDir() {
  const dataDir = process.env.ANTIGRAVITY_EXECUTABLE_DATA_DIR;
  if (dataDir) {
    for (let d = dataDir; d !== dirname(d); d = dirname(d)) {
      if (basename(d) === 'sidecar_data') {
        const candidate = dirname(d);
        if (existsSync(candidate)) return candidate;
      }
    }
  }
  const fallback = join(homedir(), '.gemini', 'antigravity');
  return existsSync(fallback) ? fallback : null;
}

// ---------------------------------------------------------------------------
// Language Server RPC client + 10s TTL cache
// ---------------------------------------------------------------------------
function getLsBaseUrl() {
  const raw = (process.env.ANTIGRAVITY_LS_ADDRESS || 'localhost:5387').trim();
  return /^https?:\/\//i.test(raw) ? raw.replace(/\/+$/, '') : `http://${raw.replace(/\/+$/, '')}`;
}

async function callLsRpc(method, body = {}) {
  const url = `${getLsBaseUrl()}/exa.language_server_pb.LanguageServerService/${method}`;
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-codeium-csrf-token': process.env.ANTIGRAVITY_CSRF_TOKEN || '',
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(RPC_TIMEOUT_MS),
  });
  const text = await res.text();
  if (!res.ok) {
    throw new Error(`${method} HTTP ${res.status}: ${text.slice(0, 200)}`);
  }
  return text ? JSON.parse(text) : {};
}

const rpcCache = {
  fetchedAt: 0,
  quotaSummary: null,
  userStatus: null,
};
let inflightRpc = null;

async function fetchQuotaSources(force = false) {
  const now = Date.now();
  const cacheFresh =
    !force &&
    rpcCache.fetchedAt > 0 &&
    now - rpcCache.fetchedAt < CACHE_TTL_MS &&
    (rpcCache.quotaSummary || rpcCache.userStatus);

  if (cacheFresh) {
    return {
      fetchedAt: rpcCache.fetchedAt,
      quotaSummary: rpcCache.quotaSummary,
      userStatus: rpcCache.userStatus,
      stale: false,
      error: null,
    };
  }

  if (inflightRpc && !force) {
    return inflightRpc;
  }

  const work = (async () => {
    const [summaryRes, statusRes] = await Promise.allSettled([
      callLsRpc('RetrieveUserQuotaSummary', {}),
      callLsRpc('GetUserStatus', { metadata: { ideName: 'antigravity' } }),
    ]);

    const errors = [];
    let quotaSummary = null;
    let userStatus = null;
    let usedFallbackCache = false;

    if (summaryRes.status === 'fulfilled') {
      quotaSummary = summaryRes.value?.response || summaryRes.value || null;
      if (quotaSummary) rpcCache.quotaSummary = quotaSummary;
    } else {
      errors.push(summaryRes.reason?.message || String(summaryRes.reason));
      if (rpcCache.quotaSummary) {
        quotaSummary = rpcCache.quotaSummary;
        usedFallbackCache = true;
      }
    }

    if (statusRes.status === 'fulfilled') {
      userStatus = statusRes.value?.userStatus || statusRes.value || null;
      if (userStatus) rpcCache.userStatus = userStatus;
    } else {
      errors.push(statusRes.reason?.message || String(statusRes.reason));
      if (rpcCache.userStatus) {
        userStatus = rpcCache.userStatus;
        usedFallbackCache = true;
      }
    }

    const bothFailed = summaryRes.status === 'rejected' && statusRes.status === 'rejected';
    if (!bothFailed && (quotaSummary || userStatus)) {
      rpcCache.fetchedAt = Date.now();
    }

    return {
      fetchedAt: bothFailed && rpcCache.fetchedAt ? rpcCache.fetchedAt : Date.now(),
      quotaSummary,
      userStatus,
      stale: bothFailed || usedFallbackCache,
      error: errors.length ? errors.join('; ') : null,
    };
  })();

  inflightRpc = work;
  try {
    return await work;
  } finally {
    if (inflightRpc === work) inflightRpc = null;
  }
}

// ---------------------------------------------------------------------------
// Current model detection & matching
// ---------------------------------------------------------------------------
function normalizeKey(s) {
  return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
}

function humanizeModelId(rawId) {
  const clean = String(rawId || '').trim();
  if (!clean) return 'Unknown Model';
  return clean
    .split(/[-_]+/)
    .filter(Boolean)
    .map((part) => {
      if (/^[0-9.]+$/.test(part)) return part;
      if (/^gpt$/i.test(part)) return 'GPT';
      if (/^oss$/i.test(part)) return 'OSS';
      if (/^xhigh$/i.test(part)) return '(XHigh)';
      return part.charAt(0).toUpperCase() + part.slice(1);
    })
    .join(' ')
    .replace(/\s+\((XHigh)\)$/, ' ($1)');
}

function matchModelConfig(rawId, configs) {
  if (!rawId || !Array.isArray(configs) || !configs.length) return null;
  const needle = String(rawId).trim().toLowerCase();
  const ordered = [
    ...configs.filter((c) => !c?.disabled),
    ...configs.filter((c) => c?.disabled),
  ];

  // 1. Exact case-insensitive modelId
  for (const cfg of ordered) {
    if (typeof cfg?.modelId === 'string' && cfg.modelId.toLowerCase() === needle) return cfg;
  }
  // 2. Exact case-insensitive modelOrAlias.model or alias
  for (const cfg of ordered) {
    const m = cfg?.modelOrAlias?.model;
    const a = cfg?.modelOrAlias?.alias;
    if (typeof m === 'string' && m.toLowerCase() === needle) return cfg;
    if (typeof a === 'string' && a.toLowerCase() === needle) return cfg;
  }
  // 3. Exact case-insensitive label
  for (const cfg of ordered) {
    if (typeof cfg?.label === 'string' && cfg.label.toLowerCase() === needle) return cfg;
  }
  // 4. Normalized alphanumeric match
  const norm = normalizeKey(rawId);
  if (norm) {
    for (const cfg of ordered) {
      if (normalizeKey(cfg?.modelId) === norm) return cfg;
      if (normalizeKey(cfg?.label) === norm) return cfg;
      if (normalizeKey(cfg?.modelOrAlias?.model) === norm) return cfg;
    }
  }
  return null;
}

function readTranscriptLastModel(appData, convId) {
  if (!appData || !convId || !ID_RE.test(convId)) return null;
  const trPath = join(appData, 'brain', convId, '.system_generated', 'logs', 'transcript.jsonl');
  const text = readCachedText(trPath);
  if (!text) return null;
  const lines = text.split('\n');
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim();
    if (!line || !line.includes('"model"')) continue;
    try {
      const obj = JSON.parse(line);
      if (typeof obj?.model === 'string' && obj.model.trim()) {
        return obj.model.trim();
      }
    } catch {
      // Skip malformed or torn lines.
    }
  }
  return null;
}

const RECENT_CONV_CACHE_TTL_MS = 5_000;
let recentConvCache = { checkedAt: 0, model: null };

function readMostRecentConversationModel(appData) {
  if (!appData) return null;
  const now = Date.now();
  if (now - recentConvCache.checkedAt < RECENT_CONV_CACHE_TTL_MS) {
    return recentConvCache.model;
  }
  const brainDir = join(appData, 'brain');
  let entries;
  try {
    entries = readdirSync(brainDir, { withFileTypes: true });
  } catch {
    recentConvCache = { checkedAt: now, model: null };
    return null;
  }
  let bestConv = null;
  let bestMtime = -1;
  for (const ent of entries) {
    if (!ent.isDirectory() || !ID_RE.test(ent.name)) continue;
    const trPath = join(brainDir, ent.name, '.system_generated', 'logs', 'transcript.jsonl');
    try {
      const st = statSync(trPath);
      if (st.mtimeMs > bestMtime) {
        bestMtime = st.mtimeMs;
        bestConv = ent.name;
      }
    } catch {
      // Skip conversations without a transcript.
    }
  }
  const model = bestConv ? readTranscriptLastModel(appData, bestConv) : null;
  recentConvCache = { checkedAt: now, model };
  return model;
}

function readLastSelectedStateModel(appData) {
  if (!appData) return null;
  const stPath = join(appData, 'antigravity_state.pbtxt');
  const text = readCachedText(stPath);
  if (!text) return null;
  const m = /^\s*last_selected_agent_model:\s*([A-Z0-9_]+)/m.exec(text);
  return m ? m[1].trim() : null;
}

function detectCurrentModel(appData, convId, configs, defaultModelEnum) {
  const candidates = [];
  const convModel = readTranscriptLastModel(appData, convId);
  if (convModel) candidates.push({ rawId: convModel, source: 'conversation' });

  if (!convModel) {
    const recentModel = readMostRecentConversationModel(appData);
    if (recentModel) candidates.push({ rawId: recentModel, source: 'recent_conversation' });
  }

  const stateModel = readLastSelectedStateModel(appData);
  if (stateModel) candidates.push({ rawId: stateModel, source: 'last_selected' });

  if (typeof defaultModelEnum === 'string' && defaultModelEnum.trim()) {
    candidates.push({ rawId: defaultModelEnum.trim(), source: 'default' });
  }

  for (const cand of candidates) {
    const matched = matchModelConfig(cand.rawId, configs);
    if (matched) {
      return {
        modelId: matched.modelId || cand.rawId,
        modelEnum: matched.modelOrAlias?.model || null,
        label: matched.label || humanizeModelId(cand.rawId),
        source: cand.source,
        config: matched,
      };
    }
  }

  if (candidates.length) {
    const first = candidates[0];
    return {
      modelId: first.rawId,
      modelEnum: /^MODEL_/.test(first.rawId) ? first.rawId : null,
      label: humanizeModelId(first.rawId),
      source: first.source,
      config: null,
    };
  }

  const fallback = (configs || []).find((c) => !c?.disabled) || (configs || [])[0] || null;
  if (fallback) {
    return {
      modelId: fallback.modelId || fallback.modelOrAlias?.model || fallback.label,
      modelEnum: fallback.modelOrAlias?.model || null,
      label: fallback.label || humanizeModelId(fallback.modelId),
      source: 'fallback',
      config: fallback,
    };
  }

  return null;
}

// ---------------------------------------------------------------------------
// Bucket normalization & model -> group mapping
// ---------------------------------------------------------------------------
function normalizeBucket(bucket) {
  if (!bucket || typeof bucket !== 'object') return null;
  const hasNum = typeof bucket.remainingFraction === 'number' && Number.isFinite(bucket.remainingFraction);
  // In proto3 JSON, a float field equal to 0 can be omitted when resetTime is present.
  const rawRem = hasNum ? bucket.remainingFraction : (bucket.resetTime ? 0 : 1);
  const remainingFraction = Math.min(1, Math.max(0, rawRem));
  const usedFraction = 1 - remainingFraction;
  const usedPercent = Math.round(usedFraction * 1000) / 10;
  const remainingPercent = Number((100 - usedPercent).toFixed(1));
  const resetTime = typeof bucket.resetTime === 'string' && bucket.resetTime ? bucket.resetTime : null;
  const parsedMs = resetTime ? Date.parse(resetTime) : NaN;
  const resetMs = Number.isFinite(parsedMs) ? parsedMs : null;

  return {
    bucketId: bucket.bucketId || '',
    displayName: bucket.displayName || '',
    description: bucket.description || '',
    window: bucket.window || inferWindow(bucket),
    remainingFraction,
    usedFraction,
    usedPercent,
    remainingPercent,
    resetTime,
    resetMs,
  };
}

function inferWindow(bucket) {
  const id = String(bucket?.bucketId || '').toLowerCase();
  const win = String(bucket?.window || '').toLowerCase();
  if (win === '5h' || id.endsWith('-5h') || id.includes('5h')) return '5h';
  if (win === 'weekly' || id.endsWith('-weekly') || id.includes('weekly')) return 'weekly';
  return win || 'other';
}

function slugifyGroup(name, idx) {
  const slug = String(name || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return slug || `group-${idx + 1}`;
}

function parseGroupModelPrefixes(description) {
  const text = String(description || '');
  const m = /Models within this group:\s*(.+?)(?:\.|$)/i.exec(text);
  if (!m) return [];
  return m[1]
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

function synthesizeGroupsFromConfigs(activeConfigs) {
  const map = new Map();
  for (const cfg of activeConfigs) {
    const q = cfg?.quotaInfo || {};
    const rem = typeof q.remainingFraction === 'number' ? q.remainingFraction : 1;
    const reset = q.resetTime || '';
    const key = `${reset}|${rem}`;
    if (!map.has(key)) {
      const parsedMs = reset ? Date.parse(reset) : NaN;
      const isWeekly = Number.isFinite(parsedMs) && parsedMs - Date.now() > 12 * 3600 * 1000;
      map.set(key, {
        displayName: isWeekly ? '模型周配额分组' : '模型 5h 配额分组',
        description: '',
        buckets: [
          {
            bucketId: isWeekly ? 'synth-weekly' : 'synth-5h',
            displayName: isWeekly ? 'Weekly Limit Remaining' : 'Five Hour Limit Remaining',
            window: isWeekly ? 'weekly' : '5h',
            remainingFraction: rem,
            resetTime: reset || null,
          },
        ],
      });
    }
  }
  return [...map.values()];
}

function findGroupIndexForModel(modelLabel, modelId, modelQuota, groups) {
  if (!groups.length) return -1;
  const labelLower = String(modelLabel || '').toLowerCase();
  const idLower = String(modelId || '').toLowerCase();
  const combined = `${labelLower} ${idLower}`;

  // 1. Longest prefix / substring match from "Models within this group: ..."
  let bestIdx = -1;
  let bestLen = 0;
  for (let i = 0; i < groups.length; i++) {
    for (const prefix of groups[i]._prefixes) {
      const pLower = prefix.toLowerCase();
      if (labelLower === pLower || labelLower.startsWith(`${pLower} `) || labelLower.startsWith(`${pLower}(`) || labelLower.includes(pLower)) {
        if (pLower.length > bestLen) {
          bestLen = pLower.length;
          bestIdx = i;
        }
      }
    }
  }
  if (bestIdx >= 0) return bestIdx;

  // 2. Keyword matching: check Argon first before general Gemini.
  if (combined.includes('argon')) {
    const idx = groups.findIndex(
      (g) =>
        g.displayName.toLowerCase().includes('argon') ||
        g.buckets.some((b) => b.bucketId.toLowerCase().includes('argon')),
    );
    if (idx >= 0) return idx;
  }

  if (/\b(claude|gpt|openai)\b/i.test(combined)) {
    const idx = groups.findIndex(
      (g) =>
        /\b(claude|gpt|3p)\b/i.test(g.displayName) ||
        g.buckets.some((b) => b.bucketId.toLowerCase().startsWith('3p-')),
    );
    if (idx >= 0) return idx;
  }

  if (combined.includes('gemini') || combined.includes('orchestrator')) {
    const idx = groups.findIndex(
      (g) =>
        !g.displayName.toLowerCase().includes('argon') &&
        (g.displayName.toLowerCase().includes('gemini') ||
          g.buckets.some((b) => /^gemini-(5h|weekly)$/i.test(b.bucketId))),
    );
    if (idx >= 0) return idx;
  }

  // 3. Tiebreaker: match resetTime within +-120s
  const modelResetMs = modelQuota?.resetTime ? Date.parse(modelQuota.resetTime) : NaN;
  if (Number.isFinite(modelResetMs)) {
    let minDiff = Infinity;
    let resetIdx = -1;
    for (let i = 0; i < groups.length; i++) {
      for (const b of groups[i].buckets) {
        if (b.resetMs == null) continue;
        const diff = Math.abs(b.resetMs - modelResetMs);
        if (diff <= RESET_MATCH_WINDOW_MS && diff < minDiff) {
          minDiff = diff;
          resetIdx = i;
        }
      }
    }
    if (resetIdx >= 0) return resetIdx;
  }

  return 0;
}

function splitModelFamily(label) {
  const text = String(label || '').trim();
  const m = THINKING_SUFFIX_RE.exec(text);
  if (!m) {
    return { familyName: text, tierLabel: null };
  }
  return {
    familyName: text.slice(0, m.index).trim(),
    tierLabel: m[1],
  };
}

function buildFamiliesForModels(models) {
  const familyMap = new Map();
  for (const m of models) {
    const { familyName, tierLabel } = splitModelFamily(m.label);
    if (!familyMap.has(familyName)) {
      familyMap.set(familyName, {
        name: familyName,
        variants: [],
      });
    }
    familyMap.get(familyName).variants.push({
      label: m.label,
      modelId: m.modelId,
      modelEnum: m.modelEnum,
      tierLabel,
      isCurrent: m.isCurrent,
      quotaInfo: m.quotaInfo,
    });
  }

  const families = [];
  for (const fam of familyMap.values()) {
    fam.variants.sort((a, b) => {
      const oa = a.tierLabel ? (THINKING_ORDER.get(a.tierLabel.toLowerCase()) ?? 99) : -1;
      const ob = b.tierLabel ? (THINKING_ORDER.get(b.tierLabel.toLowerCase()) ?? 99) : -1;
      if (oa !== ob) return oa - ob;
      return a.label.localeCompare(b.label);
    });
    const hasCurrent = fam.variants.some((v) => v.isCurrent);
    const refVariant = fam.variants.find((v) => v.isCurrent) || fam.variants[0] || null;
    families.push({
      name: fam.name,
      variants: fam.variants,
      hasCurrent,
      remainingPercent: refVariant?.quotaInfo?.remainingPercent ?? null,
      usedPercent: refVariant?.quotaInfo?.usedPercent ?? null,
      resetTime: refVariant?.quotaInfo?.resetTime ?? null,
      resetMs: refVariant?.quotaInfo?.resetMs ?? null,
    });
  }
  return families;
}

function buildQuotaPayload({ fetchedAt, quotaSummary, userStatus, stale, error }, appData, convId) {
  const modelData = userStatus?.cascadeModelConfigData || {};
  const allConfigs = Array.isArray(modelData.clientModelConfigs) ? modelData.clientModelConfigs : [];
  const activeConfigs = allConfigs.filter((c) => !c?.disabled);
  const disabledModels = allConfigs
    .filter((c) => c?.disabled)
    .map((c) => ({
      label: c.label || c.modelId || 'Disabled Model',
      modelId: c.modelId || c.modelOrAlias?.model || '',
    }));

  const currentDetected = detectCurrentModel(
    appData,
    convId,
    allConfigs,
    modelData.defaultOverrideModelConfig?.modelOrAlias?.model,
  );

  const rawGroups =
    Array.isArray(quotaSummary?.groups) && quotaSummary.groups.length
      ? quotaSummary.groups
      : synthesizeGroupsFromConfigs(activeConfigs);

  const groups = rawGroups.map((g, idx) => {
    const normalizedBuckets = (Array.isArray(g.buckets) ? g.buckets : [])
      .map(normalizeBucket)
      .filter(Boolean);
    const fiveHour = normalizedBuckets.find((b) => b.window === '5h') || null;
    const weekly = normalizedBuckets.find((b) => b.window === 'weekly') || null;
    const extraBuckets = normalizedBuckets.filter((b) => b !== fiveHour && b !== weekly);
    return {
      id: slugifyGroup(g.displayName, idx),
      displayName: g.displayName || `Model Group ${idx + 1}`,
      description: g.description || '',
      isCurrentGroup: false,
      fiveHour,
      weekly,
      extraBuckets,
      buckets: normalizedBuckets,
      models: [],
      families: [],
      modelCount: 0,
      familyCount: 0,
      _prefixes: parseGroupModelPrefixes(g.description),
    };
  });

  // Map every active model to a group.
  for (const cfg of activeConfigs) {
    const modelId = cfg.modelId || cfg.modelOrAlias?.model || cfg.label || '';
    const modelEnum = cfg.modelOrAlias?.model || null;
    const isCurrent = Boolean(
      currentDetected &&
        ((currentDetected.modelId && modelId && currentDetected.modelId.toLowerCase() === modelId.toLowerCase()) ||
          (currentDetected.modelEnum && modelEnum && currentDetected.modelEnum === modelEnum) ||
          (currentDetected.label && cfg.label && currentDetected.label.toLowerCase() === cfg.label.toLowerCase())),
    );
    const groupIdx = findGroupIndexForModel(cfg.label, modelId, cfg.quotaInfo, groups);
    const qNorm = cfg.quotaInfo && (typeof cfg.quotaInfo.remainingFraction === 'number' || cfg.quotaInfo.resetTime)
      ? normalizeBucket({
          bucketId: modelId,
          displayName: cfg.label || modelId,
          window: 'model',
          remainingFraction: cfg.quotaInfo.remainingFraction,
          resetTime: cfg.quotaInfo.resetTime,
        })
      : null;

    if (groupIdx >= 0) {
      groups[groupIdx].models.push({
        label: cfg.label || humanizeModelId(modelId),
        modelId,
        modelEnum,
        tagTitle: cfg.tagTitle || '',
        isCurrent,
        quotaInfo: qNorm
          ? {
              remainingPercent: qNorm.remainingPercent,
              usedPercent: qNorm.usedPercent,
              resetTime: qNorm.resetTime,
              resetMs: qNorm.resetMs,
            }
          : null,
      });
    }
  }

  for (const g of groups) {
    g.families = buildFamiliesForModels(g.models);
    g.modelCount = g.models.length;
    g.familyCount = g.families.length;
    delete g._prefixes;
  }

  let currentGroupIdx = -1;
  if (currentDetected) {
    currentGroupIdx = groups.findIndex((g) => g.models.some((m) => m.isCurrent));
    if (currentGroupIdx < 0) {
      currentGroupIdx = findGroupIndexForModel(
        currentDetected.label,
        currentDetected.modelId,
        currentDetected.config?.quotaInfo,
        groups,
      );
    }
  }
  const currentGroup = currentGroupIdx >= 0 ? groups[currentGroupIdx] : null;
  if (currentGroup) {
    currentGroup.isCurrentGroup = true;
  }

  const planName = userStatus?.planStatus?.planInfo?.planName || userStatus?.userTier?.name || null;
  const tierName = userStatus?.userTier?.name || planName || null;
  const userEmail = typeof userStatus?.email === 'string' ? userStatus.email.trim() : '';
  const userName = typeof userStatus?.name === 'string' ? userStatus.name.trim() : '';

  let currentPayload = null;
  if (currentDetected) {
    let fallbackBucket = null;
    if (!currentGroup && currentDetected.config?.quotaInfo) {
      fallbackBucket = normalizeBucket({
        bucketId: currentDetected.modelId,
        displayName: currentDetected.label,
        window: 'unknown',
        remainingFraction: currentDetected.config.quotaInfo.remainingFraction,
        resetTime: currentDetected.config.quotaInfo.resetTime,
      });
    }
    currentPayload = {
      modelId: currentDetected.modelId,
      modelEnum: currentDetected.modelEnum,
      label: currentDetected.label,
      source: currentDetected.source,
      groupId: currentGroup?.id || null,
      groupName: currentGroup?.displayName || null,
      fiveHour: currentGroup?.fiveHour || null,
      weekly: currentGroup?.weekly || fallbackBucket || null,
    };
  }

  return {
    fetchedAt,
    stale: Boolean(stale),
    error: error || null,
    conversationId: convId || null,
    account: userEmail || userName ? { email: userEmail || null, name: userName || null } : null,
    tier: tierName || planName ? { id: userStatus?.userTier?.id || null, name: tierName, planName } : null,
    summaryDescription: quotaSummary?.description || '',
    current: currentPayload,
    groups,
    disabledModels,
  };
}

// ---------------------------------------------------------------------------
// Runtime paths, account vault & Sidecar HTTP Server
// ---------------------------------------------------------------------------
const pluginDirName = basename(resolve(__dirname, '..', '..')) || 'quota-monitor';
const runtimeDir =
  process.env.ANTIGRAVITY_EXECUTABLE_DATA_DIR ||
  join(homedir(), '.gemini', 'antigravity', 'sidecar_data', pluginDirName, 'quota_panel', 'data');
const runtimeFile = join(runtimeDir, 'runtime.json');
const sidecarTokenFile = join(runtimeDir, 'sidecar_token.json');
const accountStore = createAccountStore({ vaultDir: resolveVaultDir(runtimeDir) });

function jsonError(status, message) {
  return new Response(JSON.stringify({ error: message }), {
    status,
    contentType: 'application/json; charset=utf-8',
  });
}

const app = new SidecarApp();

async function getLiveContext(convIdOverride = '', force = false) {
  const convId =
    (typeof convIdOverride === 'string' && convIdOverride.trim()) ||
    app.config.conversationId ||
    '';
  const appData = findAppDataDir();
  const sources = await fetchQuotaSources(force);
  const quotaPayload = buildQuotaPayload(sources, appData, convId);
  return { appData, convId, sources, quotaPayload };
}

app.page('/', () => readFileSync(join(__dirname, 'index.html'), 'utf-8'));

app.api(
  '/styles.css',
  () => new Response(readFileSync(join(__dirname, 'styles.css'), 'utf-8'), { contentType: 'text/css; charset=utf-8' }),
  'GET',
);

app.api(
  '/app.js',
  () => new Response(readFileSync(join(__dirname, 'app.js'), 'utf-8'), { contentType: 'application/javascript; charset=utf-8' }),
  'GET',
);

app.api(
  '/api/quota',
  async (data) => {
    const force = data?.force === '1' || data?.force === 'true';
    const { sources, quotaPayload } = await getLiveContext(data?.conversationId, force);
    try {
      accountStore.syncActiveAccountQuota(sources.userStatus, quotaPayload);
    } catch {
      // Keep quota endpoint resilient to local vault IO errors.
    }
    return quotaPayload;
  },
  'GET',
);

app.api(
  '/api/accounts',
  async (data) => {
    const { sources, quotaPayload } = await getLiveContext(data?.conversationId, false);
    try {
      accountStore.syncActiveAccountQuota(sources.userStatus, quotaPayload);
    } catch {
      // Ignore passive sync errors.
    }
    return {
      ...accountStore.listAccounts({
        liveUserStatus: sources.userStatus,
        quotaPayload,
      }),
      platform: process.platform,
    };
  },
  'GET',
);

app.api(
  '/api/accounts/switch-status',
  () => ({
    status: accountStore.readSwitchStatus(),
    switchLocked: accountStore.isSwitchLocked(),
  }),
  'GET',
);

app.api(
  '/api/accounts/capture',
  async (data) => {
    const { sources, quotaPayload } = await getLiveContext(data?.conversationId, false);
    try {
      const result = accountStore.captureCurrentAccount({
        liveUserStatus: sources.userStatus,
        quotaPayload,
        remark: data?.remark,
      });
      return {
        ok: true,
        ...result,
        accountsState: accountStore.listAccounts({
          liveUserStatus: sources.userStatus,
          quotaPayload,
        }),
      };
    } catch (err) {
      return jsonError(400, err?.message || String(err));
    }
  },
  'POST',
);

app.api(
  '/api/accounts/remark',
  async (data) => {
    try {
      const account = accountStore.updateRemark(data?.id, data?.remark);
      const { sources, quotaPayload } = await getLiveContext(data?.conversationId, false);
      return {
        ok: true,
        account,
        accountsState: accountStore.listAccounts({
          liveUserStatus: sources.userStatus,
          quotaPayload,
        }),
      };
    } catch (err) {
      return jsonError(400, err?.message || String(err));
    }
  },
  'POST',
);

app.api(
  '/api/accounts/delete',
  async (data) => {
    try {
      const result = accountStore.deleteAccount(data?.id);
      const { sources, quotaPayload } = await getLiveContext(data?.conversationId, false);
      return {
        ok: true,
        ...result,
        accountsState: accountStore.listAccounts({
          liveUserStatus: sources.userStatus,
          quotaPayload,
        }),
      };
    } catch (err) {
      return jsonError(400, err?.message || String(err));
    }
  },
  'POST',
);

app.api(
  '/api/accounts/import-agy-switch',
  async (data) => {
    try {
      const result = accountStore.importFromAgySwitch();
      const { sources, quotaPayload } = await getLiveContext(data?.conversationId, false);
      return {
        ok: true,
        ...result,
        accountsState: accountStore.listAccounts({
          liveUserStatus: sources.userStatus,
          quotaPayload,
        }),
      };
    } catch (err) {
      return jsonError(400, err?.message || String(err));
    }
  },
  'POST',
);

app.api(
  '/api/accounts/import-json',
  async (data) => {
    try {
      const result = accountStore.importFromJson(data?.content);
      const { sources, quotaPayload } = await getLiveContext(data?.conversationId, false);
      return {
        ok: true,
        ...result,
        accountsState: accountStore.listAccounts({
          liveUserStatus: sources.userStatus,
          quotaPayload,
        }),
      };
    } catch (err) {
      return jsonError(400, err?.message || String(err));
    }
  },
  'POST',
);

app.api(
  '/api/accounts/export',
  (data) => {
    try {
      const result = accountStore.exportAccountsJson({ ids: data?.ids });
      return { ok: true, ...result };
    } catch (err) {
      return jsonError(400, err?.message || String(err));
    }
  },
  'POST',
);

app.api(
  '/api/accounts/switch',
  (data) => {
    try {
      const { meta } = accountStore.getAccountCredential(data?.id);
      if (accountStore.isSwitchLocked()) {
        return jsonError(409, '已有账号切换任务正在执行中，请等待 Antigravity 完成重启。');
      }
      const worker = launchSwitchWorker({
        accountId: meta.id,
        vaultDir: accountStore.vaultDir,
        appDataDir: findAppDataDir(),
        homeDir: homedir(),
        targetEmail: meta.email,
      });
      return {
        ok: true,
        switching: true,
        targetId: meta.id,
        targetEmail: meta.email,
        workerPid: worker.pid,
        message: `正在切换到 ${meta.email}，Antigravity 即将重启...`,
      };
    } catch (err) {
      return jsonError(400, err?.message || String(err));
    }
  },
  'POST',
);

function removeOwnedFile(filePath) {
  try {
    const existing = JSON.parse(readFileSync(filePath, 'utf-8'));
    if (existing && existing.pid === process.pid) {
      unlinkSync(filePath);
    }
  } catch {
    // Ignore missing or already-removed file.
  }
}

function cleanupRuntimeFile() {
  removeOwnedFile(runtimeFile);
  removeOwnedFile(sidecarTokenFile);
}

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => {
    cleanupRuntimeFile();
    process.exit(0);
  });
}

app.run().then((server) => {
  try {
    const addr = server.address();
    if (addr && typeof addr === 'object' && addr.port) {
      mkdirSync(runtimeDir, { recursive: true });
      writeFileSync(
        runtimeFile,
        JSON.stringify(
          {
            host: '127.0.0.1',
            port: addr.port,
            pid: process.pid,
            startedAt: Date.now(),
          },
          null,
          2,
        ),
        'utf-8',
      );
      writeFileSync(
        sidecarTokenFile,
        JSON.stringify(
          {
            token: process.env.ANTIGRAVITY_SIDECAR_UI_TOKEN || '',
            pid: process.pid,
            startedAt: Date.now(),
          },
          null,
          2,
        ),
        { encoding: 'utf-8', mode: 0o600 },
      );
    }
  } catch (err) {
    console.error(`[quota_panel] failed to write runtime metadata: ${err?.message || err}`);
  }
});
