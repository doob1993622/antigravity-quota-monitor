/**
 * Local OpenAI Codex quota reader (zero token cost).
 *
 * Scans the most recent session rollout logs under `~/.codex/sessions`
 * (or `$CODEX_HOME/sessions`) to extract the latest `rate_limits` snapshot
 * (`primary` 5-hour window and `secondary` weekly window) and normalizes it
 * into the same group schema used by Antigravity `quota_panel`.
 */
import { closeSync, existsSync, openSync, readdirSync, readFileSync, readSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const SCAN_TTL_MS = 10_000;
const TAIL_BYTES = 256 * 1024;
const MAX_DAY_DIRS = 14;
const MAX_FILES_TO_CHECK = 12;

let scanCache = {
  checkedAt: 0,
  group: null,
};

function resolveCodexHome() {
  if (process.env.CODEX_HOME && existsSync(process.env.CODEX_HOME)) {
    return process.env.CODEX_HOME;
  }
  const fallback = join(homedir(), '.codex');
  return existsSync(fallback) ? fallback : null;
}

function readConfiguredCodexModel(codexHome) {
  try {
    const cfgPath = join(codexHome, 'config.toml');
    if (!existsSync(cfgPath)) return null;
    const text = readFileSync(cfgPath, 'utf-8');
    const m = /^\s*model\s*=\s*["']([^"']+)["']/m.exec(text);
    return m ? m[1].trim() : null;
  } catch {
    return null;
  }
}

function listSubdirsDesc(dirPath) {
  try {
    return readdirSync(dirPath, { withFileTypes: true })
      .filter((d) => d.isDirectory())
      .map((d) => d.name)
      .sort((a, b) => b.localeCompare(a));
  } catch {
    return [];
  }
}

function collectRecentSessionFiles(sessionsRoot) {
  const dayDirs = [];
  for (const year of listSubdirsDesc(sessionsRoot)) {
    const yearDir = join(sessionsRoot, year);
    for (const month of listSubdirsDesc(yearDir)) {
      const monthDir = join(yearDir, month);
      for (const day of listSubdirsDesc(monthDir)) {
        dayDirs.push(join(monthDir, day));
        if (dayDirs.length >= MAX_DAY_DIRS) break;
      }
      if (dayDirs.length >= MAX_DAY_DIRS) break;
    }
    if (dayDirs.length >= MAX_DAY_DIRS) break;
  }

  const files = [];
  for (const dir of dayDirs) {
    try {
      for (const ent of readdirSync(dir, { withFileTypes: true })) {
        if (!ent.isFile() || !ent.name.endsWith('.jsonl')) continue;
        const fullPath = join(dir, ent.name);
        try {
          const st = statSync(fullPath);
          files.push({ path: fullPath, mtimeMs: st.mtimeMs, size: st.size });
        } catch {
          // Ignore disappearing temp files.
        }
      }
    } catch {
      // Ignore unreadable day directory.
    }
    if (files.length >= MAX_FILES_TO_CHECK * 2) break;
  }

  files.sort((a, b) => b.mtimeMs - a.mtimeMs);
  return files.slice(0, MAX_FILES_TO_CHECK);
}

function readTailText(filePath, fileSize, maxBytes) {
  if (fileSize <= maxBytes) {
    return readFileSync(filePath, 'utf-8');
  }
  const fd = openSync(filePath, 'r');
  try {
    const start = Math.max(0, fileSize - maxBytes);
    const len = fileSize - start;
    const buf = Buffer.allocUnsafe(len);
    const bytesRead = readSync(fd, buf, 0, len, start);
    return buf.subarray(0, bytesRead).toString('utf-8');
  } finally {
    closeSync(fd);
  }
}

function extractRateLimitsFromText(text) {
  const lines = text.split('\n');
  let rateLimits = null;
  let sessionModel = null;

  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim();
    if (!line) continue;

    if (!rateLimits && line.includes('"rate_limits"')) {
      try {
        const obj = JSON.parse(line);
        const rl = obj?.payload?.rate_limits || obj?.rate_limits || null;
        if (rl && (rl.primary || rl.secondary)) {
          rateLimits = rl;
        }
      } catch {
        // Ignore partial or malformed lines.
      }
    }

    if (!sessionModel && line.includes('"model"')) {
      try {
        const obj = JSON.parse(line);
        const m = obj?.payload?.model || obj?.model || null;
        if (typeof m === 'string' && m.trim()) {
          sessionModel = m.trim();
        }
      } catch {
        // Ignore partial lines.
      }
    }

    if (rateLimits && sessionModel) break;
  }

  return { rateLimits, sessionModel };
}

function findLatestCodexSnapshot(sessionsRoot) {
  const files = collectRecentSessionFiles(sessionsRoot);
  for (const f of files) {
    try {
      let text = readTailText(f.path, f.size, TAIL_BYTES);
      let res = extractRateLimitsFromText(text);
      if (!res.rateLimits && f.size > TAIL_BYTES) {
        text = readFileSync(f.path, 'utf-8');
        res = extractRateLimitsFromText(text);
      }
      if (res.rateLimits) {
        return {
          rateLimits: res.rateLimits,
          sessionModel: res.sessionModel,
          recordedAt: f.mtimeMs,
        };
      }
    } catch {
      // Skip unreadable file.
    }
  }
  return null;
}

function normalizeCodexWindow(winObj, bucketId, displayName, windowKey, nowMs) {
  if (!winObj || typeof winObj !== 'object') return null;
  const rawResetSec = Number(winObj.resets_at);
  const rawResetMs = Number.isFinite(rawResetSec) && rawResetSec > 0 ? rawResetSec * 1000 : null;
  const expired = rawResetMs != null && rawResetMs <= nowMs;

  const rawUsed = Number(winObj.used_percent);
  const clampedUsed = Number.isFinite(rawUsed) ? Math.min(100, Math.max(0, rawUsed)) : 0;
  const usedPercent = expired ? 0 : Math.round(clampedUsed * 10) / 10;
  const remainingPercent = Number((100 - usedPercent).toFixed(1));
  const usedFraction = usedPercent / 100;
  const remainingFraction = remainingPercent / 100;
  const resetMs = expired ? null : rawResetMs;
  const resetTime = resetMs != null ? new Date(resetMs).toISOString() : null;

  return {
    bucketId,
    displayName,
    description: '',
    window: windowKey,
    remainingFraction,
    usedFraction,
    usedPercent,
    remainingPercent,
    resetTime,
    resetMs,
  };
}

function formatCodexModelLabel(raw) {
  const s = String(raw || '').trim();
  if (!s) return 'Codex';
  return s
    .replace(/^gpt-/i, 'GPT-')
    .replace(/-codex$/i, ' Codex');
}

export function readCodexQuotaGroup(force = false) {
  const now = Date.now();
  if (!force && scanCache.checkedAt > 0 && now - scanCache.checkedAt < SCAN_TTL_MS) {
    return scanCache.group;
  }

  const codexHome = resolveCodexHome();
  if (!codexHome) {
    scanCache = { checkedAt: now, group: null };
    return null;
  }

  const sessionsRoot = join(codexHome, 'sessions');
  if (!existsSync(sessionsRoot)) {
    scanCache = { checkedAt: now, group: null };
    return null;
  }

  const snap = findLatestCodexSnapshot(sessionsRoot);
  if (!snap) {
    scanCache = { checkedAt: now, group: null };
    return null;
  }

  const fiveHour = normalizeCodexWindow(
    snap.rateLimits.primary,
    'codex-5h',
    'Five Hour Limit Remaining',
    '5h',
    now,
  );
  const weekly = normalizeCodexWindow(
    snap.rateLimits.secondary,
    'codex-weekly',
    'Weekly Limit Remaining',
    'weekly',
    now,
  );

  const buckets = [fiveHour, weekly].filter(Boolean);
  if (!buckets.length) {
    scanCache = { checkedAt: now, group: null };
    return null;
  }

  const modelNames = [];
  const cfgModel = readConfiguredCodexModel(codexHome);
  if (cfgModel) modelNames.push(cfgModel);
  if (snap.sessionModel && !modelNames.some((m) => m.toLowerCase() === snap.sessionModel.toLowerCase())) {
    modelNames.push(snap.sessionModel);
  }
  if (!modelNames.length) {
    modelNames.push('Codex');
  }

  const refBucket = fiveHour || weekly;
  const models = modelNames.map((raw) => {
    const label = formatCodexModelLabel(raw);
    return {
      label,
      modelId: raw,
      modelEnum: null,
      tagTitle: 'Codex',
      isCurrent: false,
      quotaInfo: refBucket
        ? {
            remainingPercent: refBucket.remainingPercent,
            usedPercent: refBucket.usedPercent,
            resetTime: refBucket.resetTime,
            resetMs: refBucket.resetMs,
          }
        : null,
    };
  });

  const families = models.map((m) => ({
    name: m.label,
    variants: [
      {
        label: m.label,
        modelId: m.modelId,
        modelEnum: null,
        tierLabel: null,
        isCurrent: false,
        quotaInfo: m.quotaInfo,
      },
    ],
    hasCurrent: false,
    remainingPercent: m.quotaInfo?.remainingPercent ?? null,
    usedPercent: m.quotaInfo?.usedPercent ?? null,
    resetTime: m.quotaInfo?.resetTime ?? null,
    resetMs: m.quotaInfo?.resetMs ?? null,
  }));

  const group = {
    id: 'codex-local',
    displayName: 'OpenAI Codex',
    description: '本地 Codex 会话记录配额（零 Token 消耗）',
    externalSource: 'codex',
    recordedAt: snap.recordedAt || null,
    isCurrentGroup: false,
    fiveHour,
    weekly,
    extraBuckets: [],
    buckets,
    models,
    families,
    modelCount: models.length,
    familyCount: families.length,
  };

  scanCache = { checkedAt: now, group };
  return group;
}
