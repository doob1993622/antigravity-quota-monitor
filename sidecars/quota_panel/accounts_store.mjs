/**
 * Local Account Vault, Cached Quota Snapshot & Import/Export Manager.
 *
 * Pure local snapshot model (zero hardcoded OAuth client secrets):
 * - Stores account metadata in `<vaultDir>/accounts.json`
 * - Stores individual native OAuth credential JSONs in `<vaultDir>/credentials/<id>.json`
 * - Never exposes tokens in `listAccounts()` or HTTP GET responses
 * - Supports passive quota cache syncing, `~/.antigravity_tools` import, and JSON backup/restore
 */
import { randomUUID } from 'node:crypto';
import {
  existsSync,
  lstatSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  unlinkSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import {
  extractProfileFromIdToken,
  normalizeCredentialPayload,
  readCliSession,
  readSystemCredential,
  serializeCredentialPayload,
  writeFileAtomicVerified,
} from './keyring.mjs';

const ACCOUNT_ID_RE = /^[a-zA-Z0-9_-]{4,64}$/;
const LOCK_STALE_MS = 120_000;
const STATUS_STALE_MS = 300_000;
const LAUNCH_STALE_MS = 25_000;
const PASSIVE_SYNC_MIN_INTERVAL_MS = 60_000;
const MAX_REMARK_LEN = 40;

export function resolveVaultDir(runtimeDir) {
  return join(runtimeDir, 'vault');
}

function sanitizeAccountId(id) {
  const clean = String(id || '').trim();
  if (!ACCOUNT_ID_RE.test(clean)) {
    throw new Error('无效的账号 ID 格式。');
  }
  return clean;
}

function sanitizeRemark(remark) {
  if (remark == null) return null;
  const trimmed = String(remark).trim().replace(/\s+/g, ' ');
  if (!trimmed) return null;
  return trimmed.slice(0, MAX_REMARK_LEN);
}

function normalizeEmailKey(email) {
  return String(email || '').trim().toLowerCase();
}

function isValidEmail(email) {
  const s = String(email || '').trim();
  if (!s || s.length > 254 || /\s/.test(s)) return false;
  const parts = s.split('@');
  return parts.length === 2 && parts[0].length > 0 && parts[1].length > 0 && parts[1].includes('.');
}

function isPidAlive(pid) {
  if (typeof pid !== 'number' || !Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err?.code === 'EPERM';
  }
}

function readJsonSafe(filePath) {
  try {
    if (!existsSync(filePath)) return null;
    const raw = readFileSync(filePath, 'utf-8').replace(/^\uFEFF/, '');
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function extractLiveIdentity(liveUserStatus) {
  const src = liveUserStatus?.userStatus || liveUserStatus || null;
  if (!src || typeof src !== 'object') {
    return { email: null, name: null, tier: null, planName: null };
  }
  const email = typeof src.email === 'string' && src.email.trim() ? src.email.trim() : null;
  const name = typeof src.name === 'string' && src.name.trim() ? src.name.trim() : null;
  const planName = src.planStatus?.planInfo?.planName || src.userTier?.name || null;
  const tier = src.userTier?.name || planName || null;
  return { email, name, tier, planName };
}

/**
 * Extracts a compact, token-free quota snapshot from `buildQuotaPayload()` output
 * for offline display in account lists while other accounts are active.
 */
export function summarizeQuotaForCache(quotaPayload) {
  if (!quotaPayload || !Array.isArray(quotaPayload.groups) || !quotaPayload.groups.length) {
    return null;
  }
  return {
    fetchedAt: Number.isFinite(quotaPayload.fetchedAt) ? quotaPayload.fetchedAt : Date.now(),
    tier: quotaPayload.tier?.planName || quotaPayload.tier?.name || null,
    groups: quotaPayload.groups.map((g) => ({
      id: g.id || '',
      displayName: g.displayName || '',
      isCurrentGroup: Boolean(g.isCurrentGroup),
      fiveHourUsed: g.fiveHour ? Number(g.fiveHour.usedPercent ?? 0) : null,
      fiveHourRemaining: g.fiveHour ? Number(g.fiveHour.remainingPercent ?? 0) : null,
      fiveHourResetMs: g.fiveHour?.resetMs ?? null,
      weeklyUsed: g.weekly ? Number(g.weekly.usedPercent ?? 0) : null,
      weeklyRemaining: g.weekly ? Number(g.weekly.remainingPercent ?? 0) : null,
      weeklyResetMs: g.weekly?.resetMs ?? null,
    })),
  };
}

function summarizeAgySwitchQuota(quotaObj) {
  if (!quotaObj || typeof quotaObj !== 'object') return null;
  const fetchedAt =
    typeof quotaObj.last_updated === 'number' && quotaObj.last_updated > 0
      ? quotaObj.last_updated * 1000
      : Date.now();
  const tier =
    typeof quotaObj.subscription_tier === 'string' && quotaObj.subscription_tier.trim()
      ? quotaObj.subscription_tier.trim()
      : null;

  const groups = [];
  if (Array.isArray(quotaObj.quota_groups) && quotaObj.quota_groups.length) {
    for (const grp of quotaObj.quota_groups) {
      const buckets = Array.isArray(grp?.buckets) ? grp.buckets : [];
      const b5h = buckets.find((b) => String(b?.window || '').toLowerCase() === '5h');
      const bWk = buckets.find((b) => String(b?.window || '').toLowerCase() === 'weekly');
      const parseFrac = (b) => {
        if (!b || b.remaining_fraction_known === false) return null;
        if (typeof b.remaining_fraction !== 'number' || !Number.isFinite(b.remaining_fraction)) return null;
        const rem = Math.round(Math.min(1, Math.max(0, b.remaining_fraction)) * 1000) / 10;
        const used = Number((100 - rem).toFixed(1));
        const resetMs = b.reset_time ? Date.parse(b.reset_time) : NaN;
        return { rem, used, resetMs: Number.isFinite(resetMs) ? resetMs : null };
      };
      const f = parseFrac(b5h);
      const w = parseFrac(bWk);
      groups.push({
        id: String(grp?.display_name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-'),
        displayName: grp?.display_name || 'Model Group',
        isCurrentGroup: false,
        fiveHourUsed: f ? f.used : null,
        fiveHourRemaining: f ? f.rem : null,
        fiveHourResetMs: f ? f.resetMs : null,
        weeklyUsed: w ? w.used : null,
        weeklyRemaining: w ? w.rem : null,
        weeklyResetMs: w ? w.resetMs : null,
      });
    }
  } else if (Array.isArray(quotaObj.models) && quotaObj.models.length) {
    const valid = quotaObj.models.filter((m) => typeof m?.percentage === 'number' && m.percentage_known !== false);
    if (valid.length) {
      const minPct = Math.min(...valid.map((m) => Math.min(100, Math.max(0, m.percentage))));
      groups.push({
        id: 'imported-summary',
        displayName: 'All Models',
        isCurrentGroup: false,
        fiveHourUsed: null,
        fiveHourRemaining: null,
        fiveHourResetMs: null,
        weeklyUsed: Number((100 - minPct).toFixed(1)),
        weeklyRemaining: Number(minPct.toFixed(1)),
        weeklyResetMs: null,
      });
    }
  }

  if (!groups.length && !tier) return null;
  return { fetchedAt, tier, groups };
}

function quotaSummariesEqual(a, b) {
  if (!a && !b) return true;
  if (!a || !b) return false;
  if ((a.tier || null) !== (b.tier || null)) return false;
  const ag = Array.isArray(a.groups) ? a.groups : [];
  const bg = Array.isArray(b.groups) ? b.groups : [];
  if (ag.length !== bg.length) return false;
  for (let i = 0; i < ag.length; i++) {
    if (
      ag[i].id !== bg[i].id ||
      ag[i].fiveHourUsed !== bg[i].fiveHourUsed ||
      ag[i].weeklyUsed !== bg[i].weeklyUsed
    ) {
      return false;
    }
  }
  return true;
}

function formatExportTimestamp(date = new Date()) {
  const pad = (n) => String(n).padStart(2, '0');
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}_` +
    `${pad(date.getHours())}-${pad(date.getMinutes())}-${pad(date.getSeconds())}`
  );
}

export function createAccountStore({
  vaultDir,
  homeDir = homedir(),
  mockKeyringFile = null,
} = {}) {
  if (!vaultDir) {
    throw new Error('vaultDir is required for createAccountStore.');
  }

  const credsDir = join(vaultDir, 'credentials');
  const indexPath = join(vaultDir, 'accounts.json');
  const switchStatusPath = join(vaultDir, 'switch_status.json');
  const switchLockPath = join(vaultDir, 'switch.lock');

  let lastPassiveSyncAt = 0;

  function ensureDirs() {
    mkdirSync(credsDir, { recursive: true, mode: 0o700 });
  }

  function credFilePath(id) {
    const clean = sanitizeAccountId(id);
    return join(credsDir, `${clean}.json`);
  }

  function loadIndex() {
    const raw = readJsonSafe(indexPath);
    if (!raw || typeof raw !== 'object' || !Array.isArray(raw.accounts)) {
      return {
        schemaVersion: 1,
        activeAccountId: null,
        accounts: [],
      };
    }
    return {
      schemaVersion: 1,
      activeAccountId: typeof raw.activeAccountId === 'string' ? raw.activeAccountId : null,
      accounts: raw.accounts.filter((a) => a && typeof a === 'object' && ACCOUNT_ID_RE.test(String(a.id || ''))),
    };
  }

  function saveIndex(index) {
    ensureDirs();
    const payload = {
      schemaVersion: 1,
      activeAccountId: index.activeAccountId || null,
      accounts: index.accounts,
    };
    writeFileAtomicVerified(indexPath, JSON.stringify(payload, null, 2), 0o600);
  }

  function hasValidCredentialFile(id) {
    try {
      const p = credFilePath(id);
      if (!existsSync(p)) return false;
      const st = lstatSync(p);
      return st.isFile() && st.size > 0;
    } catch {
      return false;
    }
  }

  function resolveAgySwitchSourceDir(overrideDir = null) {
    const candidate =
      (typeof overrideDir === 'string' && overrideDir.trim()) ||
      (process.env.ABV_DATA_DIR || '').trim() ||
      join(homeDir, '.antigravity_tools');
    return candidate;
  }

  function probeAgySwitch() {
    const dir = resolveAgySwitchSourceDir();
    const accDir = join(dir, 'accounts');
    if (!existsSync(accDir)) {
      return { detected: false, count: 0 };
    }
    try {
      const files = readdirSync(accDir).filter((f) => f.endsWith('.json'));
      return { detected: files.length > 0, count: files.length };
    } catch {
      return { detected: false, count: 0 };
    }
  }

  function readSwitchStatus() {
    const raw = readJsonSafe(switchStatusPath);
    if (!raw || typeof raw !== 'object') return null;
    if (raw.state === 'running') {
      const updatedAt = Number(raw.updatedAt || raw.startedAt || 0);
      if (raw.stage === 'launching_worker' && (!Number.isFinite(updatedAt) || Date.now() - updatedAt > LAUNCH_STALE_MS)) {
        return {
          ...raw,
          state: 'failed',
          error: raw.error || '后台切换辅助进程未能启动，请重载插件后重试。',
          stale: true,
        };
      }
      const staleByTime = !Number.isFinite(updatedAt) || Date.now() - updatedAt > STATUS_STALE_MS;
      const pidGone = raw.workerPid && !isPidAlive(raw.workerPid);
      if (staleByTime || pidGone) {
        return {
          ...raw,
          state: 'failed',
          error: raw.error || '后台切换进程已结束或超时，请检查账号状态后重试。',
          stale: true,
        };
      }
    }
    return raw;
  }

  function writeSwitchStatus(status) {
    ensureDirs();
    const payload = {
      ...status,
      updatedAt: Date.now(),
    };
    writeFileAtomicVerified(switchStatusPath, JSON.stringify(payload, null, 2), 0o600);
    return payload;
  }

  function acquireSwitchLock(targetAccountId, pid = process.pid) {
    ensureDirs();
    const existing = readJsonSafe(switchLockPath);
    if (existing && typeof existing === 'object') {
      const age = Date.now() - Number(existing.startedAt || 0);
      if (age >= 0 && age < LOCK_STALE_MS && isPidAlive(existing.pid) && existing.pid !== pid) {
        throw new Error('已有账号切换任务正在执行中，请等待 Antigravity 完成重启。');
      }
    }
    const lockData = {
      pid,
      targetAccountId: sanitizeAccountId(targetAccountId),
      startedAt: Date.now(),
    };
    writeFileAtomicVerified(switchLockPath, JSON.stringify(lockData, null, 2), 0o600);
    return lockData;
  }

  function releaseSwitchLock(expectedPid = null) {
    try {
      if (!existsSync(switchLockPath)) return;
      if (expectedPid !== null) {
        const cur = readJsonSafe(switchLockPath);
        if (cur && cur.pid && cur.pid !== expectedPid) return;
      }
      unlinkSync(switchLockPath);
    } catch {
      // Best-effort cleanup.
    }
  }

  function isSwitchLocked() {
    const existing = readJsonSafe(switchLockPath);
    if (!existing || typeof existing !== 'object') return false;
    const age = Date.now() - Number(existing.startedAt || 0);
    return age >= 0 && age < LOCK_STALE_MS && isPidAlive(existing.pid);
  }

  function sanitizeAccountDto(meta, { isCurrent, hasCredential }) {
    return {
      id: meta.id,
      email: meta.email,
      name: meta.name || null,
      remark: meta.remark || null,
      tier: meta.tier || meta.planName || null,
      planName: meta.planName || meta.tier || null,
      source: meta.source || 'capture',
      isCurrent: Boolean(isCurrent),
      hasCredential: Boolean(hasCredential),
      lastQuota: meta.lastQuota || null,
      createdAt: meta.createdAt || null,
      updatedAt: meta.updatedAt || null,
      lastUsedAt: meta.lastUsedAt || null,
    };
  }

  function upsertAccountRecord({
    email,
    name = null,
    remark = undefined,
    tier = null,
    planName = null,
    lastQuota = null,
    source = 'capture',
    payloadObj,
    setActive = false,
  }) {
    const cleanEmail = String(email || '').trim();
    if (!isValidEmail(cleanEmail)) {
      throw new Error(`账号邮箱格式无效: ${cleanEmail || '(空)'}`);
    }

    const serializedCred = serializeCredentialPayload(payloadObj);
    const index = loadIndex();
    const emailKey = normalizeEmailKey(cleanEmail);
    const now = Date.now();

    let existingIdx = index.accounts.findIndex((a) => normalizeEmailKey(a.email) === emailKey);
    const created = existingIdx < 0;
    let record;

    if (created) {
      const id = randomUUID();
      record = {
        id,
        email: cleanEmail,
        name: name || null,
        remark: remark !== undefined ? sanitizeRemark(remark) : null,
        tier: tier || planName || null,
        planName: planName || tier || null,
        lastQuota: lastQuota || null,
        source,
        createdAt: now,
        updatedAt: now,
        lastUsedAt: setActive ? now : null,
      };
      index.accounts.push(record);
    } else {
      const prev = index.accounts[existingIdx];
      record = {
        ...prev,
        email: cleanEmail,
        name: name || prev.name || null,
        remark: remark !== undefined ? sanitizeRemark(remark) : prev.remark || null,
        tier: tier || prev.tier || null,
        planName: planName || tier || prev.planName || null,
        lastQuota: lastQuota || prev.lastQuota || null,
        source: prev.source || source,
        updatedAt: now,
        lastUsedAt: setActive ? now : prev.lastUsedAt || null,
      };
      index.accounts[existingIdx] = record;
    }

    if (setActive) {
      index.activeAccountId = record.id;
    }

    ensureDirs();
    writeFileAtomicVerified(credFilePath(record.id), serializedCred, 0o600);
    saveIndex(index);

    return { record, created };
  }

  function syncActiveAccountQuota(liveUserStatus, quotaPayload) {
    const identity = extractLiveIdentity(liveUserStatus);
    if (!identity.email) return false;

    const index = loadIndex();
    const emailKey = normalizeEmailKey(identity.email);
    const idx = index.accounts.findIndex((a) => normalizeEmailKey(a.email) === emailKey);
    if (idx < 0) return false;

    const existing = index.accounts[idx];
    const nextQuota = summarizeQuotaForCache(quotaPayload);
    const nextTier = identity.tier || nextQuota?.tier || existing.tier || null;
    const nextPlan = identity.planName || nextTier || existing.planName || null;
    const nextName = identity.name || existing.name || null;

    const unchanged =
      index.activeAccountId === existing.id &&
      existing.tier === nextTier &&
      existing.name === nextName &&
      quotaSummariesEqual(existing.lastQuota, nextQuota);

    if (unchanged && Date.now() - lastPassiveSyncAt < PASSIVE_SYNC_MIN_INTERVAL_MS) {
      return false;
    }
    if (unchanged) {
      lastPassiveSyncAt = Date.now();
      return false;
    }

    index.accounts[idx] = {
      ...existing,
      name: nextName,
      tier: nextTier,
      planName: nextPlan,
      lastQuota: nextQuota || existing.lastQuota || null,
      updatedAt: Date.now(),
      lastUsedAt: Date.now(),
    };
    index.activeAccountId = existing.id;
    saveIndex(index);
    lastPassiveSyncAt = Date.now();
    return true;
  }

  function listAccounts({ liveUserStatus = null, quotaPayload = null } = {}) {
    const index = loadIndex();
    const live = extractLiveIdentity(liveUserStatus);
    const liveEmailKey = live.email ? normalizeEmailKey(live.email) : null;
    const liveQuota = summarizeQuotaForCache(quotaPayload);
    const liveTier = live.tier || liveQuota?.tier || null;
    const agySwitch = probeAgySwitch();
    const switchStatus = readSwitchStatus();

    const accounts = index.accounts.map((meta) => {
      const matchesLive = liveEmailKey
        ? normalizeEmailKey(meta.email) === liveEmailKey
        : meta.id === index.activeAccountId;
      const effectiveMeta = matchesLive && liveQuota
        ? {
            ...meta,
            tier: liveTier || meta.tier,
            planName: live.planName || liveTier || meta.planName,
            lastQuota: liveQuota,
          }
        : meta;
      return sanitizeAccountDto(effectiveMeta, {
        isCurrent: matchesLive,
        hasCredential: hasValidCredentialFile(meta.id),
      });
    });

    // Sort: current first, then most recently used, then creation time.
    accounts.sort((a, b) => {
      if (a.isCurrent !== b.isCurrent) return a.isCurrent ? -1 : 1;
      const luDiff = (b.lastUsedAt || 0) - (a.lastUsedAt || 0);
      if (luDiff !== 0) return luDiff;
      return (a.createdAt || 0) - (b.createdAt || 0);
    });

    const liveSaved = liveEmailKey
      ? accounts.some((a) => normalizeEmailKey(a.email) === liveEmailKey && a.hasCredential)
      : false;

    return {
      schemaVersion: 1,
      activeAccountId: index.activeAccountId,
      liveAccount: live.email || live.name
        ? {
            email: live.email,
            name: live.name,
            tier: liveTier,
            isSaved: liveSaved,
          }
        : null,
      agySwitchDetected: agySwitch.detected,
      agySwitchCount: agySwitch.count,
      switchStatus,
      switchLocked: isSwitchLocked(),
      accounts,
    };
  }

  function captureCurrentAccount({ liveUserStatus = null, quotaPayload = null, remark = undefined } = {}) {
    let rawCred = null;
    try {
      rawCred = readSystemCredential({ mockFile: mockKeyringFile });
    } catch {
      rawCred = null;
    }
    if (!rawCred) {
      try {
        rawCred = readCliSession({ homeDir });
      } catch {
        rawCred = null;
      }
    }
    if (!rawCred) {
      throw new Error('未在系统凭据管理器或 agy CLI 会话中找到当前登录凭据，请先在 Antigravity 中完成登录。');
    }

    const payloadObj = normalizeCredentialPayload(rawCred);
    const live = extractLiveIdentity(liveUserStatus);
    const idClaims = extractProfileFromIdToken(payloadObj.id_token);

    const email = live.email || idClaims.email;
    if (!isValidEmail(email)) {
      throw new Error('无法从当前登录态识别邮箱，请等待 Antigravity 连接就绪后重试。');
    }

    const name = live.name || idClaims.name || null;
    const quotaSummary = summarizeQuotaForCache(quotaPayload);
    const tier = live.tier || quotaSummary?.tier || null;

    const { record, created } = upsertAccountRecord({
      email,
      name,
      remark,
      tier,
      planName: live.planName || tier,
      lastQuota: quotaSummary,
      source: 'capture',
      payloadObj,
      setActive: true,
    });

    return {
      created,
      account: sanitizeAccountDto(record, { isCurrent: true, hasCredential: true }),
    };
  }

  function updateRemark(id, remark) {
    const cleanId = sanitizeAccountId(id);
    const index = loadIndex();
    const idx = index.accounts.findIndex((a) => a.id === cleanId);
    if (idx < 0) {
      throw new Error('未找到指定账号。');
    }
    index.accounts[idx] = {
      ...index.accounts[idx],
      remark: sanitizeRemark(remark),
      updatedAt: Date.now(),
    };
    saveIndex(index);
    return sanitizeAccountDto(index.accounts[idx], {
      isCurrent: index.accounts[idx].id === index.activeAccountId,
      hasCredential: hasValidCredentialFile(cleanId),
    });
  }

  function deleteAccount(id) {
    const cleanId = sanitizeAccountId(id);
    const index = loadIndex();
    const before = index.accounts.length;
    index.accounts = index.accounts.filter((a) => a.id !== cleanId);
    if (index.accounts.length === before) {
      throw new Error('未找到要删除的账号。');
    }
    if (index.activeAccountId === cleanId) {
      index.activeAccountId = null;
    }
    saveIndex(index);

    try {
      const p = credFilePath(cleanId);
      if (existsSync(p)) unlinkSync(p);
    } catch {
      // Ignore credential unlink failure after index removal.
    }
    return { deletedId: cleanId };
  }

  function getAccountCredential(id) {
    const cleanId = sanitizeAccountId(id);
    const index = loadIndex();
    const meta = index.accounts.find((a) => a.id === cleanId);
    if (!meta) {
      throw new Error('未找到目标账号。');
    }
    const p = credFilePath(cleanId);
    if (!existsSync(p)) {
      throw new Error(`账号 ${meta.email} 缺少本地凭据文件，请重新保存或导入。`);
    }
    const raw = readFileSync(p, 'utf-8');
    const payloadObj = normalizeCredentialPayload(raw);
    const payloadJson = serializeCredentialPayload(payloadObj);
    return { meta, payloadObj, payloadJson };
  }

  function markSwitchedActive(id) {
    const cleanId = sanitizeAccountId(id);
    const index = loadIndex();
    const idx = index.accounts.findIndex((a) => a.id === cleanId);
    if (idx < 0) return false;
    index.activeAccountId = cleanId;
    index.accounts[idx] = {
      ...index.accounts[idx],
      lastUsedAt: Date.now(),
      updatedAt: Date.now(),
    };
    saveIndex(index);
    return true;
  }

  function importFromAgySwitch({ sourceDir = null } = {}) {
    const rootDir = resolveAgySwitchSourceDir(sourceDir);
    const accDir = join(rootDir, 'accounts');
    if (!existsSync(accDir)) {
      throw new Error('未检测到 agy-switch 本地账号目录 (~/.antigravity_tools/accounts)。');
    }

    const index = loadIndex();
    const existingByEmail = new Map(
      index.accounts.map((a) => [normalizeEmailKey(a.email), a]),
    );

    const files = readdirSync(accDir).filter((f) => f.endsWith('.json'));
    let imported = 0;
    let updated = 0;
    let skipped = 0;
    const errors = [];

    for (const file of files) {
      const fullPath = join(accDir, file);
      let raw;
      try {
        raw = JSON.parse(readFileSync(fullPath, 'utf-8').replace(/^\uFEFF/, ''));
      } catch {
        skipped += 1;
        errors.push(`${file}: JSON 解析失败`);
        continue;
      }

      const email = typeof raw?.email === 'string' ? raw.email.trim() : '';
      const refreshToken = typeof raw?.token?.refresh_token === 'string'
        ? raw.token.refresh_token.trim()
        : '';

      if (!isValidEmail(email) || !refreshToken) {
        skipped += 1;
        errors.push(`${file}: 缺少有效邮箱或 refresh_token`);
        continue;
      }

      try {
        const payloadObj = normalizeCredentialPayload({
          token: {
            access_token: raw.token.access_token || '',
            token_type: raw.token.token_type || 'Bearer',
            refresh_token: refreshToken,
            expiry_timestamp: raw.token.expiry_timestamp,
            expiry: raw.token.expiry,
          },
          auth_method: 'consumer',
          id_token: raw.token.id_token || undefined,
        });

        const existing = existingByEmail.get(normalizeEmailKey(email));
        const remark = existing?.remark !== undefined && existing?.remark !== null
          ? undefined
          : (typeof raw.custom_label === 'string' && raw.custom_label.trim() ? raw.custom_label.trim() : undefined);

        const lastQuota = summarizeAgySwitchQuota(raw.quota);
        const tier = lastQuota?.tier || raw?.quota?.subscription_tier || null;

        const res = upsertAccountRecord({
          email,
          name: typeof raw.name === 'string' && raw.name.trim() ? raw.name.trim() : null,
          remark,
          tier,
          planName: tier,
          lastQuota,
          source: 'agy-switch',
          payloadObj,
          setActive: false,
        });
        existingByEmail.set(normalizeEmailKey(email), res.record);
        if (res.created) imported += 1;
        else updated += 1;
      } catch (err) {
        skipped += 1;
        errors.push(`${email || file}: ${err?.message || err}`);
      }
    }

    return { imported, updated, skipped, errors };
  }

  function importFromJson(rawText) {
    const text = String(rawText || '').trim();
    if (!text) {
      throw new Error('导入内容不能为空。');
    }

    let parsed;
    try {
      parsed = JSON.parse(text.replace(/^\uFEFF/, ''));
    } catch {
      throw new Error('导入内容不是合法的 JSON 格式。');
    }

    let rows;
    if (Array.isArray(parsed)) {
      rows = parsed;
    } else if (parsed && typeof parsed === 'object' && Array.isArray(parsed.accounts)) {
      rows = parsed.accounts;
    } else if (parsed && typeof parsed === 'object') {
      rows = [parsed];
    } else {
      throw new Error('不支持的 JSON 导入结构。');
    }

    if (!rows.length) {
      throw new Error('JSON 中未包含任何账号记录。');
    }

    let imported = 0;
    let updated = 0;
    let failed = 0;
    const errors = [];

    for (let i = 0; i < rows.length; i++) {
      const row = rows[i];
      if (!row || typeof row !== 'object' || Array.isArray(row)) {
        failed += 1;
        errors.push(`第 ${i + 1} 项: 必须为对象`);
        continue;
      }

      try {
        let payloadObj;
        if (row.credentialPayload && typeof row.credentialPayload === 'object') {
          payloadObj = normalizeCredentialPayload(row.credentialPayload);
        } else if (row.token && typeof row.token === 'object') {
          payloadObj = normalizeCredentialPayload(row);
        } else if (typeof row.refresh_token === 'string' && row.refresh_token.trim()) {
          payloadObj = normalizeCredentialPayload({
            token: {
              access_token: typeof row.access_token === 'string' ? row.access_token : '',
              token_type: 'Bearer',
              refresh_token: row.refresh_token.trim(),
              expiry: row.expiry,
              expiry_timestamp: row.expiry_timestamp,
            },
            auth_method: row.auth_method || 'consumer',
            id_token: typeof row.id_token === 'string' ? row.id_token : undefined,
          });
        } else {
          throw new Error('缺少 credentialPayload / token / refresh_token');
        }

        const idClaims = extractProfileFromIdToken(payloadObj.id_token);
        const email =
          (typeof row.email === 'string' && row.email.trim()) ||
          idClaims.email ||
          '';
        if (!isValidEmail(email)) {
          throw new Error('缺少有效 email（且 id_token 中未包含邮箱）');
        }

        const name =
          (typeof row.name === 'string' && row.name.trim()) ||
          idClaims.name ||
          null;
        const remarkRaw =
          row.remark !== undefined
            ? row.remark
            : row.custom_label !== undefined
              ? row.custom_label
              : undefined;
        const tier =
          (typeof row.tier === 'string' && row.tier.trim()) ||
          (typeof row.planName === 'string' && row.planName.trim()) ||
          null;

        const res = upsertAccountRecord({
          email,
          name,
          remark: remarkRaw,
          tier,
          planName: tier,
          lastQuota: row.lastQuota && typeof row.lastQuota === 'object' ? row.lastQuota : null,
          source: 'json',
          payloadObj,
          setActive: false,
        });
        if (res.created) imported += 1;
        else updated += 1;
      } catch (err) {
        failed += 1;
        const label = typeof row.email === 'string' && row.email.trim() ? row.email.trim() : `第 ${i + 1} 项`;
        errors.push(`${label}: ${err?.message || err}`);
      }
    }

    return { imported, updated, failed, errors };
  }

  function exportAccountsJson({ ids = null } = {}) {
    const index = loadIndex();
    const idFilter = Array.isArray(ids) && ids.length ? new Set(ids.map((v) => String(v))) : null;
    const exported = [];

    for (const meta of index.accounts) {
      if (idFilter && !idFilter.has(meta.id)) continue;
      if (!hasValidCredentialFile(meta.id)) continue;
      const { payloadObj } = getAccountCredential(meta.id);
      exported.push({
        email: meta.email,
        name: meta.name || null,
        remark: meta.remark || null,
        tier: meta.tier || meta.planName || null,
        credentialPayload: payloadObj,
      });
    }

    if (!exported.length) {
      throw new Error('当前没有可导出的已保存账号凭据。');
    }

    const doc = {
      schemaVersion: 1,
      exportedAt: new Date().toISOString(),
      warning: 'Contains OAuth login credentials. Keep this file private.',
      accounts: exported,
    };

    return {
      filename: `antigravity-accounts-${formatExportTimestamp()}.json`,
      count: exported.length,
      content: JSON.stringify(doc, null, 2),
    };
  }

  return {
    vaultDir,
    listAccounts,
    syncActiveAccountQuota,
    captureCurrentAccount,
    updateRemark,
    deleteAccount,
    getAccountCredential,
    markSwitchedActive,
    importFromAgySwitch,
    importFromJson,
    exportAccountsJson,
    acquireSwitchLock,
    releaseSwitchLock,
    readSwitchStatus,
    writeSwitchStatus,
    isSwitchLocked,
  };
}
