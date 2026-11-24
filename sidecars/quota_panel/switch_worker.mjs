/**
 * Detached Account Switch Worker & Process Lifecycle Helper.
 *
 * Designed so that `quota_panel` (which runs inside Antigravity's process tree /
 * Windows Job Object) can spawn this worker completely detached into the user's
 * interactive desktop session (Task Scheduler `Schedule.Service` on Windows)
 * before gracefully closing Antigravity, updating credentials
 * (`gemini:antigravity` + `agy` CLI), guarding against all-zero
 * `antigravity_state.pbtxt`, and relaunching Antigravity.
 *
 * Clean-room MIT implementation. Never logs credentials or command-line tokens.
 */
import { execFileSync, spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import {
  closeSync,
  existsSync,
  lstatSync,
  mkdirSync,
  openSync,
  readdirSync,
  readFileSync,
  readSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createAccountStore } from './accounts_store.mjs';
import { writeAllCredentials } from './keyring.mjs';

const __filename = fileURLToPath(import.meta.url);
const MAX_STATE_RECOVERY_BYTES = 16 * 1024 * 1024;
const GRACEFUL_CLOSE_TIMEOUT_MS = 12_000;
const FORCE_CLOSE_TIMEOUT_MS = 5_000;
const LS_SETTLE_TIMEOUT_MS = 3_500;
const LS_KILL_SETTLE_TIMEOUT_MS = 3_000;
const STALE_JOB_MS = 300_000;
const MAX_WORKER_LOG_LINES = 120;
const POLL_INTERVAL_MS = 250;

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function appendWorkerLog(vaultDir, message) {
  if (!vaultDir) return;
  try {
    mkdirSync(vaultDir, { recursive: true, mode: 0o700 });
    const logPath = join(vaultDir, 'switch_worker.log');
    let lines = [];
    if (existsSync(logPath)) {
      lines = readFileSync(logPath, 'utf-8').split(/\r?\n/).filter(Boolean);
    }
    lines.push(`${new Date().toISOString()} ${message}`);
    if (lines.length > MAX_WORKER_LOG_LINES) {
      lines = lines.slice(-MAX_WORKER_LOG_LINES);
    }
    writeFileSync(logPath, lines.join('\n') + '\n', { encoding: 'utf-8', mode: 0o600 });
  } catch {
    // Diagnostic logging must never break account switching.
  }
}

function isUsableStandaloneNode(candidate) {
  try {
    if (typeof candidate !== 'string' || !candidate.trim()) return false;
    const p = candidate.trim();
    if (/\\Microsoft\\WindowsApps\\/i.test(p)) return false;
    if (p.toLowerCase().includes('antigravity')) return false;
    if (!existsSync(p)) return false;
    return statSync(p).isFile();
  } catch {
    return false;
  }
}

export function resolveWorkerRuntime(options = {}) {
  const {
    execPath = process.execPath,
    isElectronEnv = Boolean(process.versions?.electron),
    platform = process.platform,
    preferStandaloneNode = true,
  } = options || {};

  if (!isElectronEnv && isUsableStandaloneNode(execPath)) {
    return { exe: execPath, isElectron: false, source: 'process_execPath' };
  }

  if (preferStandaloneNode) {
    const candidates = [];
    if (platform === 'win32') {
      try {
        const out = execFileSync('where.exe', ['node'], {
          encoding: 'utf-8',
          windowsHide: true,
          timeout: 3_000,
        });
        for (const line of out.split(/\r?\n/)) {
          const clean = line.trim();
          if (clean) candidates.push(clean);
        }
      } catch {
        // Ignore PATH lookup failure.
      }
      const localAppData = process.env.LOCALAPPDATA || join(homedir(), 'AppData', 'Local');
      candidates.push(
        join(process.env.ProgramFiles || 'C:\\Program Files', 'nodejs', 'node.exe'),
        join(process.env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)', 'nodejs', 'node.exe'),
        join(localAppData, 'Programs', 'nodejs', 'node.exe'),
      );
      if (process.env.NVM_SYMLINK) {
        candidates.push(join(process.env.NVM_SYMLINK, 'node.exe'));
      }
    } else {
      try {
        const out = execFileSync('which', ['node'], {
          encoding: 'utf-8',
          timeout: 3_000,
        });
        for (const line of out.split(/\r?\n/)) {
          const clean = line.trim();
          if (clean) candidates.push(clean);
        }
      } catch {
        // Ignore PATH lookup failure.
      }
      candidates.push('/opt/homebrew/bin/node', '/usr/local/bin/node', '/usr/bin/node');
    }

    const seen = new Set();
    for (const cand of candidates) {
      if (!cand) continue;
      const key = cand.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      if (isUsableStandaloneNode(cand)) {
        return { exe: cand, isElectron: false, source: 'system_node' };
      }
    }
  }

  return { exe: execPath, isElectron: true, source: 'electron_run_as_node' };
}

export function isSwitchWorkerProcess(exePath, name, commandLine = '') {
  return String(commandLine || '').toLowerCase().includes('switch_worker.mjs');
}

export function isSidecarNodeProcess(exePath, name, commandLine = '') {
  if (isSwitchWorkerProcess(exePath, name, commandLine)) return false;
  const cmd = String(commandLine || '');
  const cmdLower = cmd.toLowerCase();
  return (
    cmdLower.includes('sidecar_sdk') ||
    cmdLower.includes('register_loader.js') ||
    cmdLower.includes('--ms-enable-electron-run-as-node') ||
    /\b(main|launch)\.mjs\b/i.test(cmd)
  );
}

export function isIdeName(value) {
  const normalized = String(value || '').toLowerCase().replace(/[-_]+/g, ' ');
  return normalized.includes('antigravity ide');
}

export function isSwitchProcess(exePath, name) {
  const exeLower = String(exePath || '').toLowerCase();
  const nameLower = String(name || '').toLowerCase();
  const base = exeLower.split(/[\\/]/).pop() || '';
  return (
    base.startsWith('agy-switch') ||
    nameLower.startsWith('agy-switch') ||
    exeLower.includes('antigravity switch.app')
  );
}

export function isHelperProcess(exePath, name, commandLine = '') {
  if (isSwitchWorkerProcess(exePath, name, commandLine)) return true;
  if (isSidecarNodeProcess(exePath, name, commandLine)) return true;
  const exeLower = String(exePath || '').toLowerCase();
  const nameLower = String(name || '').toLowerCase();
  const cmdLower = String(commandLine || '').toLowerCase();
  return (
    cmdLower.includes('--type=') ||
    nameLower.includes('helper') ||
    nameLower.includes('plugin') ||
    nameLower.includes('renderer') ||
    nameLower.includes('gpu') ||
    nameLower.includes('crashpad') ||
    nameLower.includes('utility') ||
    nameLower.includes('audio') ||
    nameLower.includes('sandbox') ||
    nameLower.includes('language_server') ||
    exeLower.includes('crashpad') ||
    exeLower.includes('language_server')
  );
}

export function parseCommandLineArgv(commandLine) {
  const text = String(commandLine || '');
  const tokens = [];
  let current = '';
  let inQuotes = false;
  let inToken = false;

  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (!inQuotes && /\s/.test(ch)) {
      if (inToken) {
        tokens.push(current);
        current = '';
        inToken = false;
      }
      continue;
    }

    inToken = true;
    if (ch === '\\') {
      let slashCount = 1;
      while (i + 1 < text.length && text[i + 1] === '\\') {
        slashCount += 1;
        i += 1;
      }
      if (i + 1 < text.length && text[i + 1] === '"') {
        current += '\\'.repeat(Math.floor(slashCount / 2));
        if (slashCount % 2 === 1) {
          current += '"';
          i += 1;
        }
      } else {
        current += '\\'.repeat(slashCount);
      }
      continue;
    }

    if (ch === '"') {
      inQuotes = !inQuotes;
      continue;
    }

    current += ch;
  }

  if (inToken) {
    tokens.push(current);
  }
  return tokens;
}

const SAFE_RELAUNCH_BOOL_FLAGS = new Set([
  '--no-proxy-server',
  '--proxy-auto-detect',
  '--ignore-certificate-errors',
  '--disable-gpu',
  '--disable-http2',
  '--disable-quic',
]);

const SAFE_RELAUNCH_VALUE_PREFIXES = [
  '--proxy-server=',
  '--proxy-bypass-list=',
  '--proxy-pac-url=',
  '--lang=',
  '--host-resolver-rules=',
  '--enable-features=',
  '--disable-features=',
];

const SAFE_RELAUNCH_SPLIT_FLAGS = new Set([
  '--proxy-server',
  '--proxy-bypass-list',
  '--proxy-pac-url',
  '--lang',
  '--host-resolver-rules',
]);

export function extractSafeRelaunchArgs(commandLine) {
  const tokens = parseCommandLineArgv(commandLine);
  if (tokens.length <= 1) return [];

  const result = [];
  const seen = new Set();
  const pushUnique = (flag) => {
    if (!flag || seen.has(flag)) return;
    seen.add(flag);
    result.push(flag);
  };

  for (let i = 1; i < tokens.length; i++) {
    const tok = tokens[i];
    if (!tok) continue;
    const lower = tok.toLowerCase();

    if (SAFE_RELAUNCH_BOOL_FLAGS.has(lower)) {
      pushUnique(lower);
      continue;
    }

    let matchedPrefix = false;
    for (const prefix of SAFE_RELAUNCH_VALUE_PREFIXES) {
      if (lower.startsWith(prefix)) {
        pushUnique(`${prefix}${tok.slice(prefix.length)}`);
        matchedPrefix = true;
        break;
      }
    }
    if (matchedPrefix) continue;

    if (SAFE_RELAUNCH_SPLIT_FLAGS.has(lower) && i + 1 < tokens.length && !tokens[i + 1].startsWith('-')) {
      pushUnique(`${lower}=${tokens[i + 1]}`);
      i += 1;
    }
  }

  return result;
}

export function isAppProcessCandidate(exePath, name, commandLine = '') {
  const exeLower = String(exePath || '').toLowerCase();
  const nameLower = String(name || '').toLowerCase();
  if (isHelperProcess(exeLower, nameLower, commandLine)) return false;
  if (isIdeName(exeLower) || isIdeName(nameLower)) return false;
  if (isSwitchProcess(exeLower, nameLower)) return false;
  return exeLower.includes('antigravity') || nameLower.includes('antigravity');
}

function compareSemver(a, b) {
  const pa = String(a || '')
    .trim()
    .replace(/^v/i, '')
    .split(/[^0-9]+/)
    .slice(0, 3)
    .map((n) => parseInt(n, 10) || 0);
  const pb = String(b || '')
    .trim()
    .replace(/^v/i, '')
    .split(/[^0-9]+/)
    .slice(0, 3)
    .map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < 3; i++) {
    const da = pa[i] || 0;
    const db = pb[i] || 0;
    if (da !== db) return da - db;
  }
  return 0;
}

/**
 * Antigravity IDE 2.6+ shares the App/agy system credential store (`gemini:antigravity`).
 * Checks `resources/app/product.json` next to the discovered IDE executable.
 */
export function isIdeVersionShared(ideExePath) {
  if (!ideExePath) return false;
  let baseDir = dirname(resolve(ideExePath));
  if (process.platform === 'darwin') {
    const idx = ideExePath.toLowerCase().indexOf('.app');
    if (idx >= 0) {
      baseDir = join(ideExePath.slice(0, idx + 4), 'Contents', 'Resources');
    }
  } else {
    baseDir = join(baseDir, 'resources');
  }
  const productPath = join(baseDir, 'app', 'product.json');
  try {
    if (!existsSync(productPath)) return false;
    const data = JSON.parse(readFileSync(productPath, 'utf-8'));
    const ver = data?.ideVersion || data?.version || '';
    if (!ver) return false;
    return compareSemver(ver, '2.6.0') >= 0;
  } catch {
    return false;
  }
}

function runPowerShellEncoded(script, timeout = 12_000) {
  const full = `$ErrorActionPreference = 'Stop'\n$ProgressPreference = 'SilentlyContinue'\n${script}\n`;
  const encoded = Buffer.from(full, 'utf16le').toString('base64');
  return execFileSync(
    'powershell.exe',
    ['-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
    {
      encoding: 'utf-8',
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      timeout,
    },
  );
}

function queryWindowsProcesses() {
  const script = `
$procs = Get-CimInstance Win32_Process -Filter "Name LIKE '%antigravity%' OR Name LIKE '%language_server%'" |
  Select-Object ProcessId, ParentProcessId, Name, ExecutablePath, CommandLine
if ($null -eq $procs) {
  Write-Output '[]'
} else {
  $arr = @($procs)
  Write-Output (ConvertTo-Json -InputObject $arr -Compress)
}
`;
  const out = runPowerShellEncoded(script, 10_000);
  const start = out.indexOf('[');
  if (start < 0) return [];
  try {
    const parsed = JSON.parse(out.slice(start));
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function queryPosixProcesses() {
  try {
    const out = execFileSync('ps', ['-ax', '-o', 'pid=,ppid=,comm=,args='], {
      encoding: 'utf-8',
      timeout: 8_000,
    });
    const rows = [];
    for (const line of out.split(/\r?\n/)) {
      const m = /^\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$/.exec(line);
      if (!m) continue;
      rows.push({
        ProcessId: parseInt(m[1], 10),
        ParentProcessId: parseInt(m[2], 10),
        Name: m[3],
        ExecutablePath: m[3],
        CommandLine: m[4],
      });
    }
    return rows;
  } catch {
    return [];
  }
}

function findInstalledAppExecutable(platform = process.platform, homeDir = homedir()) {
  if (platform === 'win32') {
    const candidates = [
      join(process.env.LOCALAPPDATA || join(homeDir, 'AppData', 'Local'), 'Programs', 'Antigravity', 'Antigravity.exe'),
      join(process.env.ProgramFiles || 'C:\\Program Files', 'Antigravity', 'Antigravity.exe'),
      join(process.env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)', 'Antigravity', 'Antigravity.exe'),
    ];
    return candidates.find((p) => existsSync(p)) || null;
  }
  if (platform === 'darwin') {
    const candidates = [
      '/Applications/Antigravity.app',
      join(homeDir, 'Applications', 'Antigravity.app'),
    ];
    return candidates.find((p) => existsSync(p)) || null;
  }
  const candidates = [
    '/usr/bin/antigravity',
    '/usr/local/bin/antigravity',
    '/opt/antigravity/antigravity',
    join(homeDir, '.local', 'bin', 'antigravity'),
  ];
  return candidates.find((p) => existsSync(p)) || null;
}

export function discoverRunningAndInstalledTargets({
  platform = process.platform,
  homeDir = homedir(),
} = {}) {
  const procs = platform === 'win32' ? queryWindowsProcesses() : queryPosixProcesses();
  const appPids = [];
  const idePids = [];
  const sidecarPids = [];
  const languageServerPids = [];
  const relaunchMap = new Map(); // lowerPath -> { exe, args }

  const addRelaunch = (exePath, cmdLine = '') => {
    if (!exePath || !existsSync(exePath)) return;
    let normalized = exePath;
    if (platform === 'darwin') {
      const idx = exePath.toLowerCase().indexOf('.app');
      if (idx >= 0) normalized = exePath.slice(0, idx + 4);
    }
    const key = normalized.toLowerCase();
    const extracted = cmdLine ? extractSafeRelaunchArgs(cmdLine) : [];
    if (!relaunchMap.has(key)) {
      relaunchMap.set(key, { exe: normalized, args: extracted });
    } else if (extracted.length) {
      const entry = relaunchMap.get(key);
      for (const arg of extracted) {
        if (!entry.args.includes(arg)) {
          entry.args.push(arg);
        }
      }
    }
  };

  for (const proc of procs) {
    const pid = Number(proc.ProcessId);
    if (!Number.isInteger(pid) || pid <= 0 || pid === process.pid || pid === process.ppid) continue;
    const name = String(proc.Name || '');
    const exe = String(proc.ExecutablePath || '');
    const cmd = String(proc.CommandLine || '');

    if (isSwitchWorkerProcess(exe, name, cmd)) {
      continue;
    }

    if (exe.toLowerCase().includes('language_server') || name.toLowerCase().includes('language_server')) {
      languageServerPids.push({ pid, exe, cmd });
      continue;
    }

    if (isSidecarNodeProcess(exe, name, cmd)) {
      if (exe.toLowerCase().includes('antigravity') || name.toLowerCase().includes('antigravity')) {
        sidecarPids.push(pid);
      }
      continue;
    }

    if (isHelperProcess(exe, name, cmd) || isSwitchProcess(exe, name)) {
      continue;
    }

    if (isIdeName(exe) || isIdeName(name)) {
      if (isIdeVersionShared(exe)) {
        idePids.push(pid);
        addRelaunch(exe, cmd);
      }
      continue;
    }

    if (isAppProcessCandidate(exe, name, cmd)) {
      appPids.push(pid);
      addRelaunch(exe, cmd);
    }
  }

  const installedApp = findInstalledAppExecutable(platform, homeDir);
  if (relaunchMap.size === 0 && installedApp) {
    addRelaunch(installedApp, '');
  }

  const relaunchEntries = [...relaunchMap.values()];
  return {
    appPids,
    idePids,
    sidecarPids,
    closePids: [...new Set([...appPids, ...idePids])],
    languageServerPids,
    relaunchTargets: relaunchEntries.map((e) => e.exe),
    relaunchEntries,
    installedApp,
  };
}

function isPidRunning(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err?.code === 'EPERM';
  }
}

function requestWindowsGracefulClose(pids) {
  if (!pids.length) return;
  const pidList = pids.filter((p) => Number.isInteger(p) && p > 0).join(',');
  if (!pidList) return;
  const script = `
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class QuotaMonitorWinClose {
    private const uint WM_CLOSE = 0x0010;
    private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool PostMessageW(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);

    public static void ClosePids(int[] targetPids) {
        HashSet<uint> set = new HashSet<uint>();
        foreach (int p in targetPids) {
            if (p > 0) set.Add((uint)p);
        }
        EnumWindows((hWnd, lParam) => {
            uint pid = 0;
            GetWindowThreadProcessId(hWnd, out pid);
            if (pid != 0 && set.Contains(pid) && IsWindowVisible(hWnd)) {
                PostMessageW(hWnd, WM_CLOSE, IntPtr.Zero, IntPtr.Zero);
            }
            return true;
        }, IntPtr.Zero);
    }
}
'@
[QuotaMonitorWinClose]::ClosePids(@(${pidList}))
`;
  try {
    runPowerShellEncoded(script, 8_000);
  } catch {
    // Graceful close is best-effort; caller waits and falls back to taskkill.
  }
}

function forceKillPid(pid) {
  if (!Number.isInteger(pid) || pid <= 0 || pid === process.pid || pid === process.ppid) return;
  if (process.platform === 'win32') {
    try {
      execFileSync('taskkill', ['/F', '/T', '/PID', String(pid)], {
        stdio: 'ignore',
        windowsHide: true,
        timeout: 5_000,
      });
    } catch {
      // Process may already have exited.
    }
  } else {
    try {
      process.kill(pid, 'SIGKILL');
    } catch {
      // Process may already have exited.
    }
  }
}

function findRelatedLsPids(lsEntries, installRoots) {
  const pids = new Set();
  for (const item of lsEntries || []) {
    const pid = Number(item?.pid);
    if (!Number.isInteger(pid) || pid <= 0) continue;
    const exeLower = String(item.exe || '').toLowerCase();
    const cmdLower = String(item.cmd || '').toLowerCase();
    let matched = false;
    for (const root of installRoots) {
      if (exeLower && exeLower.startsWith(root)) {
        matched = true;
        break;
      }
    }
    if (!matched && (cmdLower.includes('--override_ide_name antigravity') || cmdLower.includes('--subclient_type hub'))) {
      matched = true;
    }
    if (matched) pids.add(pid);
  }
  return [...pids];
}

async function closeTargetsGracefully(targets, vaultDir = null) {
  const closePids = targets?.closePids || [];
  const languageServerPids = targets?.languageServerPids || [];
  const relaunchPaths = targets?.relaunchTargets || [];
  const rootCandidates = [...relaunchPaths, ...(targets?.installedApp ? [targets.installedApp] : [])];
  const installRoots = new Set(rootCandidates.map((p) => dirname(resolve(p)).toLowerCase()));

  if (closePids.length) {
    if (process.platform === 'win32') {
      requestWindowsGracefulClose(closePids);
      const deadline = Date.now() + GRACEFUL_CLOSE_TIMEOUT_MS;
      while (Date.now() < deadline) {
        if (!closePids.some((pid) => isPidRunning(pid))) break;
        await sleep(POLL_INTERVAL_MS);
      }

      for (const pid of closePids.filter((p) => isPidRunning(p))) {
        forceKillPid(pid);
      }

      const hardDeadline = Date.now() + FORCE_CLOSE_TIMEOUT_MS;
      while (Date.now() < hardDeadline) {
        if (!closePids.some((pid) => isPidRunning(pid))) break;
        await sleep(POLL_INTERVAL_MS);
      }
    } else {
      for (const pid of closePids) {
        try {
          process.kill(pid, 'SIGTERM');
        } catch {
          // Ignore already exited.
        }
      }
      const deadline = Date.now() + GRACEFUL_CLOSE_TIMEOUT_MS;
      while (Date.now() < deadline) {
        if (!closePids.some((pid) => isPidRunning(pid))) break;
        await sleep(POLL_INTERVAL_MS);
      }
      for (const pid of closePids.filter((p) => isPidRunning(p))) {
        forceKillPid(pid);
      }
    }
  }

  // Wait briefly for child language_server processes to flush and exit naturally.
  const initialLsPids = findRelatedLsPids(languageServerPids, installRoots);
  if (initialLsPids.length) {
    const lsDeadline = Date.now() + LS_SETTLE_TIMEOUT_MS;
    while (Date.now() < lsDeadline) {
      if (!initialLsPids.some((pid) => isPidRunning(pid))) break;
      await sleep(200);
    }
  }

  // Re-query to catch any orphaned sidecars or surviving standalone language_server / Hub processes.
  const freshTargets = discoverRunningAndInstalledTargets();
  for (const p of [...(freshTargets.relaunchTargets || []), ...(freshTargets.installedApp ? [freshTargets.installedApp] : [])]) {
    installRoots.add(dirname(resolve(p)).toLowerCase());
  }

  const sidecarToKill = [...new Set([...(targets?.sidecarPids || []), ...(freshTargets.sidecarPids || [])])]
    .filter((pid) => pid !== process.pid && pid !== process.ppid && isPidRunning(pid));
  if (sidecarToKill.length) {
    appendWorkerLog(vaultDir, `cleanup killing ${sidecarToKill.length} lingering sidecar process(es)`);
    for (const pid of sidecarToKill) {
      forceKillPid(pid);
    }
  }

  const survivingLsPids = [...new Set([
    ...initialLsPids,
    ...findRelatedLsPids(freshTargets.languageServerPids, installRoots),
  ])].filter((pid) => isPidRunning(pid));

  if (survivingLsPids.length) {
    appendWorkerLog(vaultDir, `cleanup terminating ${survivingLsPids.length} surviving language_server/hub process(es)`);
    if (process.platform !== 'win32') {
      for (const pid of survivingLsPids) {
        try {
          process.kill(pid, 'SIGTERM');
        } catch {
          // Ignore already exited.
        }
      }
      const termDeadline = Date.now() + 800;
      while (Date.now() < termDeadline) {
        if (!survivingLsPids.some((pid) => isPidRunning(pid))) break;
        await sleep(150);
      }
    }
    for (const pid of survivingLsPids.filter((p) => isPidRunning(p))) {
      forceKillPid(pid);
    }
    const killDeadline = Date.now() + LS_KILL_SETTLE_TIMEOUT_MS;
    while (Date.now() < killDeadline) {
      if (!survivingLsPids.some((pid) => isPidRunning(pid))) break;
      await sleep(200);
    }
    const stillAlive = survivingLsPids.filter((p) => isPidRunning(p));
    if (stillAlive.length) {
      throw new Error('无法停止旧的 Antigravity Language Server 进程，已中止凭据切换以防会话冲突。');
    }
  }
}

/**
 * Checks whether `<appDataDir>/antigravity_state.pbtxt` is an all-NUL corrupt file
 * (an observed Windows crash artifact when Antigravity exits abruptly) and moves it
 * to a timestamped `.bak` file while preserving its bytes. Non-zero files are untouched.
 */
export function recoverZeroedStateFile(appDataDir) {
  if (!appDataDir) return null;
  const statePath = join(appDataDir, 'antigravity_state.pbtxt');
  let st;
  try {
    st = lstatSync(statePath);
  } catch (err) {
    if (err && err.code === 'ENOENT') return null;
    throw new Error(`Cannot inspect Antigravity state file: ${err?.message || err}`);
  }

  if (st.isSymbolicLink() || !st.isFile() || st.size === 0 || st.size > MAX_STATE_RECOVERY_BYTES) {
    return null;
  }

  const fd = openSync(statePath, 'r');
  try {
    const buf = Buffer.allocUnsafe(8192);
    let offset = 0;
    while (offset < st.size) {
      const n = readSync(fd, buf, 0, Math.min(buf.length, st.size - offset), offset);
      if (n <= 0) break;
      for (let i = 0; i < n; i++) {
        if (buf[i] !== 0) {
          return null; // Valid non-zero content; leave untouched.
        }
      }
      offset += n;
    }
  } finally {
    closeSync(fd);
  }

  const backupPath = `${statePath}.corrupt.${Date.now()}.bak`;
  renameSync(statePath, backupPath);
  return backupPath;
}

const STRIPPED_ENV_EXACT = new Set([
  'ELECTRON_RUN_AS_NODE',
  'ELECTRON_NO_ATTACH_CONSOLE',
  'NODE_OPTIONS',
]);

const STRIPPED_ENV_PREFIX_RE = /^ANTIGRAVITY_(LS_|CSRF_|SIDECAR_|EXECUTABLE_|AGENTAPI_|CONVERSATION_|PROJECT_)/i;
const PRESERVED_LAUNCH_ENV_RE =
  /^(HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY|GRPC_PROXY|SSL_CERT_FILE|SSL_CERT_DIR|NODE_EXTRA_CA_CERTS|ANTIGRAVITY_QUOTA_PYTHON)$/i;

/**
 * Captures transient proxy/TLS environment variables from the active Sidecar process
 * (`quota_panel`) before crossing the Windows Task Scheduler boundary (which resets
 * environment variables back to `HKCU\Environment` and would otherwise drop launcher-injected
 * `HTTP_PROXY` / `HTTPS_PROXY` needed by Go `language_server`).
 */
export function captureInheritedLaunchEnv(baseEnv = process.env) {
  const captured = {};
  for (const [key, val] of Object.entries(baseEnv || {})) {
    if (typeof val !== 'string') continue;
    const trimmed = val.trim();
    if (!trimmed || trimmed.length > 1024 || /[\r\n\0]/.test(trimmed)) continue;
    if (PRESERVED_LAUNCH_ENV_RE.test(key)) {
      captured[key] = trimmed;
    }
  }
  return captured;
}

/**
 * Derives Go-compatible `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` fallback variables
 * from Chromium `--proxy-server=` / `--proxy-bypass-list=` CLI flags when explicit
 * proxy environment variables were not set.
 */
export function deriveProxyEnvFromArgs(args = []) {
  if (!Array.isArray(args) || !args.length) return {};
  if (args.some((a) => String(a || '').toLowerCase() === '--no-proxy-server')) {
    return {};
  }

  let proxyRaw = '';
  let bypassRaw = '';
  for (const arg of args) {
    const s = String(arg || '');
    const lower = s.toLowerCase();
    if (lower.startsWith('--proxy-server=')) {
      proxyRaw = s.slice('--proxy-server='.length).trim();
    } else if (lower.startsWith('--proxy-bypass-list=')) {
      bypassRaw = s.slice('--proxy-bypass-list='.length).trim();
    }
  }

  if (!proxyRaw || proxyRaw.includes('=') || /[\r\n\0]/.test(proxyRaw)) {
    return {};
  }

  const proxyUrl = /^[a-z][a-z0-9+.-]*:\/\//i.test(proxyRaw) ? proxyRaw : `http://${proxyRaw}`;
  const noProxy = bypassRaw
    ? bypassRaw
        .split(/[;,]+/)
        .map((part) => part.trim().replace(/^\[|\]$/g, ''))
        .filter(Boolean)
        .join(',')
    : 'localhost,127.0.0.1,::1';

  return {
    HTTP_PROXY: proxyUrl,
    HTTPS_PROXY: proxyUrl,
    NO_PROXY: noProxy || 'localhost,127.0.0.1,::1',
  };
}

export function mergeLaunchEnv(capturedEnv = {}, args = []) {
  const cleanCaptured = captureInheritedLaunchEnv(capturedEnv);
  const hasExplicitProxy = Object.keys(cleanCaptured).some((k) =>
    /^(HTTP_PROXY|HTTPS_PROXY|ALL_PROXY)$/i.test(k),
  );
  if (hasExplicitProxy) {
    return cleanCaptured;
  }
  return {
    ...deriveProxyEnvFromArgs(args),
    ...cleanCaptured,
  };
}

/**
 * Produces a clean environment dictionary for relaunching desktop GUI apps so
 * Electron never inherits `ELECTRON_RUN_AS_NODE=1` or stale Sidecar/LS tokens.
 */
export function buildCleanGuiEnv(baseEnv = process.env) {
  const clean = {};
  for (const [key, val] of Object.entries(baseEnv || {})) {
    if (typeof val !== 'string') continue;
    const upper = key.toUpperCase();
    if (STRIPPED_ENV_EXACT.has(upper)) continue;
    if (STRIPPED_ENV_PREFIX_RE.test(key)) continue;
    clean[key] = val;
  }
  return clean;
}

/**
 * Formats an argv array into a single Windows `lpParameters` command string
 * compatible with `ShellExecuteExW` / `CommandLineToArgvW`.
 */
export function formatWindowsCommandArgs(args = []) {
  if (!Array.isArray(args) || !args.length) return '';
  return args
    .map((raw) => {
      const s = String(raw ?? '');
      if (!s.length) return '""';
      if (!/[\s"&|()^;]/.test(s)) return s;
      const escaped = s
        .replace(/(\\*)"/g, '$1$1\\"')
        .replace(/(\\+)$/, '$1$1');
      return `"${escaped}"`;
    })
    .join(' ');
}

/**
 * Launches a GUI executable completely outside the current Windows Task Scheduler
 * Job Object (`Shell.Application.ShellExecute` -> `wscript.exe` -> `WScript.Shell.Run`)
 * while explicitly injecting captured `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` vars
 * (which `explorer.exe` does not have when set via a custom launcher script) and
 * stripping `ELECTRON_RUN_AS_NODE`.
 */
export function relaunchExecutableDetached(
  exe,
  args = [],
  { vaultDir = null, windowShow = 1, extraEnv = null } = {},
) {
  const effectiveExtraEnv = mergeLaunchEnv(extraEnv || {}, args);
  const cleanEnv = {
    ...buildCleanGuiEnv(process.env),
    ...effectiveExtraEnv,
  };
  const resolvedExe = resolve(exe);
  const workDir = dirname(resolvedExe);

  if (process.platform === 'win32') {
    const wscriptExe = join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'wscript.exe');
    if (existsSync(wscriptExe)) {
      const vbsStr = (s) => '"' + String(s).replace(/"/g, '""') + '"';
      const targetDir = vaultDir && existsSync(vaultDir) ? vaultDir : tmpdir();
      const id = randomUUID();
      const innerVbsPath = join(targetDir, `switch_relaunch_inner_${id}.vbs`);
      const outerVbsPath = join(targetDir, `switch_relaunch_outer_${id}.vbs`);
      const showMode = Number.isInteger(windowShow) ? windowShow : 1;
      const argLine = formatWindowsCommandArgs(args);

      const innerLines = [
        'Option Explicit',
        'On Error Resume Next',
        'Dim sh, fso, exePath, argLine, cmd',
        'Set sh = CreateObject("WScript.Shell")',
        'Set fso = CreateObject("Scripting.FileSystemObject")',
        'sh.Environment("PROCESS").Remove("ELECTRON_RUN_AS_NODE")',
        'sh.Environment("PROCESS").Remove("ELECTRON_NO_ATTACH_CONSOLE")',
        'sh.Environment("PROCESS").Remove("NODE_OPTIONS")',
      ];
      for (const [k, v] of Object.entries(effectiveExtraEnv)) {
        innerLines.push(`sh.Environment("PROCESS")(${vbsStr(k)}) = ${vbsStr(v)}`);
      }
      innerLines.push(
        `sh.CurrentDirectory = ${vbsStr(workDir)}`,
        `exePath = ${vbsStr(resolvedExe)}`,
        `argLine = ${vbsStr(argLine)}`,
        'If Len(argLine) > 0 Then',
        '  cmd = """" & exePath & """ " & argLine',
        'Else',
        '  cmd = """" & exePath & """"',
        'End If',
        `sh.Run cmd, ${showMode}, False`,
        'fso.DeleteFile WScript.ScriptFullName, True',
        '',
      );

      const outerLines = [
        'Option Explicit',
        'Dim sa',
        'Set sa = CreateObject("Shell.Application")',
        `sa.ShellExecute ${vbsStr(wscriptExe)}, ${vbsStr(`//B //Nologo "${innerVbsPath}"`)}, ${vbsStr(workDir)}, "open", 0`,
        'WScript.Sleep 200',
        '',
      ];

      const toUtf16Bom = (lines) =>
        Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(lines.join('\r\n'), 'utf16le')]);

      writeFileSync(innerVbsPath, toUtf16Bom(innerLines), { mode: 0o600 });
      writeFileSync(outerVbsPath, toUtf16Bom(outerLines), { mode: 0o600 });
      try {
        execFileSync(wscriptExe, ['//B', '//Nologo', outerVbsPath], {
          env: cleanEnv,
          stdio: 'ignore',
          windowsHide: true,
          timeout: 8_000,
        });
        return { method: 'shell_execute_twohop', envKeys: Object.keys(effectiveExtraEnv).sort() };
      } catch {
        try {
          if (existsSync(innerVbsPath)) unlinkSync(innerVbsPath);
        } catch {
          // Best-effort cleanup on failure.
        }
        // Fall through to detached spawn below if WScript fails.
      } finally {
        try {
          if (existsSync(outerVbsPath)) unlinkSync(outerVbsPath);
        } catch {
          // Best-effort cleanup.
        }
      }
    }

    const child = spawn(resolvedExe, args, {
      detached: true,
      stdio: 'ignore',
      windowsHide: false,
      cwd: workDir,
      env: cleanEnv,
    });
    child.unref();
    return { method: 'spawn', envKeys: Object.keys(effectiveExtraEnv).sort() };
  }

  if (process.platform === 'darwin') {
    const envArgs = [];
    for (const [k, v] of Object.entries(effectiveExtraEnv)) {
      envArgs.push('--env', `${k}=${v}`);
    }
    const openArgs = ['-a', exe, ...envArgs, ...(args.length ? ['--args', ...args] : [])];
    const child = spawn('open', openArgs, { detached: true, stdio: 'ignore', env: cleanEnv });
    child.unref();
    return { method: 'open', envKeys: Object.keys(effectiveExtraEnv).sort() };
  }

  const child = spawn(resolvedExe, args, {
    detached: true,
    stdio: 'ignore',
    cwd: workDir,
    env: cleanEnv,
  });
  child.unref();
  return { method: 'spawn', envKeys: Object.keys(effectiveExtraEnv).sort() };
}

async function relaunchTargets(targets, vaultDir = null, launchEnv = null) {
  const entries =
    Array.isArray(targets?.relaunchEntries) && targets.relaunchEntries.length
      ? targets.relaunchEntries
      : (targets?.relaunchTargets || []).map((exe) => ({ exe, args: [] }));
  if (!entries.length) return { relaunched: [] };

  const relaunched = [];
  for (const entry of entries) {
    const exe = entry?.exe;
    const args = Array.isArray(entry?.args) ? entry.args : [];
    if (!exe || !existsSync(exe)) continue;
    const res = relaunchExecutableDetached(exe, args, {
      vaultDir,
      windowShow: 1,
      extraEnv: launchEnv,
    });
    appendWorkerLog(
      vaultDir,
      `relaunch invoked method=${res.method} exe=${basename(exe)} args=${args.length} envKeys=${(res.envKeys || []).join(',') || 'none'}`,
    );
    relaunched.push(exe);
  }

  if (relaunched.length && process.platform === 'win32') {
    let verifiedCount = 0;
    const verifyDeadline = Date.now() + 2_500;
    while (Date.now() < verifyDeadline) {
      await sleep(250);
      const check = discoverRunningAndInstalledTargets();
      if (check.closePids.length > 0) {
        verifiedCount = check.closePids.length;
        break;
      }
    }
    appendWorkerLog(vaultDir, `relaunch verify activeGuiPids=${verifiedCount}`);
  }

  return { relaunched };
}

/**
 * Executes a full switch job. When `dryRun` is true, exercises locking,
 * credential serialization, mock keyring + CLI sync, state file check, and
 * status reporting WITHOUT closing/relaunching Antigravity or touching real OS keyrings.
 */
export async function runSwitchJob(jobConfig, { dryRun = false } = {}) {
  const {
    accountId,
    vaultDir,
    appDataDir = null,
    homeDir = homedir(),
    mockKeyringFile = null,
    targetEmail = '',
    launchEnv = null,
  } = jobConfig || {};

  const store = createAccountStore({
    vaultDir,
    homeDir,
    mockKeyringFile,
  });

  store.acquireSwitchLock(accountId, process.pid);
  try {
    appendWorkerLog(vaultDir, `job stage=preparing pid=${process.pid} dryRun=${Boolean(dryRun)}`);
    store.writeSwitchStatus({
      state: 'running',
      stage: 'preparing',
      targetAccountId: accountId,
      targetEmail,
      workerPid: process.pid,
      dryRun: Boolean(dryRun),
      startedAt: Date.now(),
    });

    const { meta, payloadJson } = store.getAccountCredential(accountId);

    let targets = {
      closePids: [],
      sidecarPids: [],
      relaunchTargets: [],
      relaunchEntries: [],
      languageServerPids: [],
    };
    if (!dryRun) {
      targets = discoverRunningAndInstalledTargets({ homeDir });
      appendWorkerLog(
        vaultDir,
        `job stage=closing_app closePids=${targets.closePids.length} sidecarPids=${targets.sidecarPids.length} lsPids=${targets.languageServerPids.length}`,
      );
      store.writeSwitchStatus({
        state: 'running',
        stage: 'closing_app',
        targetAccountId: accountId,
        targetEmail: meta.email,
        workerPid: process.pid,
        dryRun: false,
      });
      await closeTargetsGracefully(targets, vaultDir);
    }

    if (appDataDir && (process.platform === 'win32' || dryRun)) {
      recoverZeroedStateFile(appDataDir);
    }

    appendWorkerLog(vaultDir, 'job stage=writing_credentials');
    store.writeSwitchStatus({
      state: 'running',
      stage: 'writing_credentials',
      targetAccountId: accountId,
      targetEmail: meta.email,
      workerPid: process.pid,
      dryRun: Boolean(dryRun),
    });

    const credResult = writeAllCredentials(payloadJson, {
      homeDir,
      mockFile: mockKeyringFile,
    });

    store.markSwitchedActive(accountId);

    let relaunched = [];
    if (!dryRun) {
      const relaunchCount = targets.relaunchEntries?.length || targets.relaunchTargets?.length || 0;
      const argsCount = (targets.relaunchEntries || []).reduce((sum, e) => sum + (e.args?.length || 0), 0);
      appendWorkerLog(vaultDir, `job stage=relaunching_app targets=${relaunchCount} preservedArgs=${argsCount}`);
      store.writeSwitchStatus({
        state: 'running',
        stage: 'relaunching_app',
        targetAccountId: accountId,
        targetEmail: meta.email,
        workerPid: process.pid,
        dryRun: false,
      });
      relaunched = (await relaunchTargets(targets, vaultDir, launchEnv)).relaunched;
    }

    appendWorkerLog(vaultDir, `job stage=done cliSynced=${Boolean(credResult.cliSynced)} relaunched=${relaunched.length}`);
    const doneStatus = store.writeSwitchStatus({
      state: 'completed',
      stage: 'done',
      targetAccountId: accountId,
      targetEmail: meta.email,
      cliSynced: credResult.cliSynced,
      relaunchedCount: relaunched.length,
      workerPid: process.pid,
      dryRun: Boolean(dryRun),
      completedAt: Date.now(),
    });
    return doneStatus;
  } catch (err) {
    appendWorkerLog(vaultDir, `job stage=error message=${err?.message || String(err)}`);
    store.writeSwitchStatus({
      state: 'failed',
      stage: 'error',
      targetAccountId: accountId,
      targetEmail,
      workerPid: process.pid,
      dryRun: Boolean(dryRun),
      error: err?.message || String(err),
    });
    throw err;
  } finally {
    store.releaseSwitchLock(process.pid);
  }
}

/**
 * Spawns `switch_worker.mjs` completely detached from the parent Antigravity
 * process tree / Windows Job Object so closing Antigravity will not terminate it.
 * On Windows, resolves standalone Node (falling back to ELECTRON_RUN_AS_NODE=1)
 * and registers/runs a self-cleaning interactive-token Task Scheduler job
 * (`Schedule.Service`, `LogonType = 3`) via a hidden `wscript.exe //B //Nologo`
 * runner so the worker runs in desktop Session 1 outside Antigravity's Job Object.
 */
export function launchSwitchWorker(jobConfig) {
  const { vaultDir } = jobConfig || {};
  if (!vaultDir) {
    throw new Error('vaultDir is required to launch switch worker.');
  }
  mkdirSync(vaultDir, { recursive: true, mode: 0o700 });

  // Sweep stale unconsumed switch job manifests and runner scripts older than 5 minutes.
  try {
    const now = Date.now();
    for (const file of readdirSync(vaultDir)) {
      const isStaleTarget =
        (file.startsWith('switch_job_') && file.endsWith('.json')) ||
        (file.startsWith('switch_runner_') && file.endsWith('.vbs')) ||
        (file.startsWith('switch_relaunch_') && file.endsWith('.vbs'));
      if (!isStaleTarget) continue;
      const full = join(vaultDir, file);
      try {
        const st = statSync(full);
        if (st.isFile() && now - st.mtimeMs > STALE_JOB_MS) {
          unlinkSync(full);
        }
      } catch {
        // Ignore individual file error.
      }
    }
  } catch {
    // Ignore sweep failure.
  }

  const store = createAccountStore({
    vaultDir,
    homeDir: jobConfig.homeDir || homedir(),
    mockKeyringFile: jobConfig.mockKeyringFile || null,
  });
  store.writeSwitchStatus({
    state: 'running',
    stage: 'launching_worker',
    targetAccountId: jobConfig.accountId,
    targetEmail: jobConfig.targetEmail || '',
    workerPid: null,
    dryRun: Boolean(jobConfig.dryRun),
    startedAt: Date.now(),
  });

  const launchEnv =
    jobConfig.launchEnv && typeof jobConfig.launchEnv === 'object'
      ? captureInheritedLaunchEnv(jobConfig.launchEnv)
      : captureInheritedLaunchEnv(process.env);

  const jobId = randomUUID();
  const jobPath = join(vaultDir, `switch_job_${jobId}.json`);
  writeFileSync(jobPath, JSON.stringify({ ...jobConfig, launchEnv, jobId }, null, 2), {
    encoding: 'utf-8',
    mode: 0o600,
  });

  const runtime = resolveWorkerRuntime({
    execPath: jobConfig.simulateExecPath || process.execPath,
    isElectronEnv: jobConfig.simulateElectronEnv ?? Boolean(process.versions?.electron),
    preferStandaloneNode: jobConfig.preferStandaloneNode !== false,
  });
  const workerPath = __filename;
  const workerDir = dirname(workerPath);

  appendWorkerLog(
    vaultDir,
    `launch attempt runtime=${runtime.source} exe=${basename(runtime.exe)} isElectron=${runtime.isElectron} dryRun=${Boolean(jobConfig.dryRun)} envKeys=${Object.keys(launchEnv).sort().join(',') || 'none'}`,
  );

  if (process.platform === 'win32' && (!jobConfig.dryRun || jobConfig.useTaskSchedulerInDryRun)) {
    const q = (s) => String(s).replace(/'/g, "''");
    const vbsStr = (s) => '"' + String(s).replace(/"/g, '""') + '"';
    const taskName = `QuotaMonitorSwitch_${jobId.replace(/-/g, '').slice(0, 12)}`;
    const wscriptCandidate = join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'wscript.exe');

    let runnerVbsPath = null;
    let actPath = null;
    let actArgs = null;

    if (existsSync(wscriptCandidate)) {
      runnerVbsPath = join(vaultDir, `switch_runner_${jobId}.vbs`);
      const dryArgSuffix = jobConfig.dryRun ? ' & " --dry-run"' : '';
      const vbsLines = [
        'Option Explicit',
        'On Error Resume Next',
        'Dim sh, fso, exePath, workerPath, jobPath, cmd',
        'Set sh = CreateObject("WScript.Shell")',
        'Set fso = CreateObject("Scripting.FileSystemObject")',
      ];
      if (runtime.isElectron) {
        vbsLines.push('sh.Environment("PROCESS")("ELECTRON_RUN_AS_NODE") = "1"');
      }
      vbsLines.push(
        `sh.CurrentDirectory = ${vbsStr(workerDir)}`,
        `exePath = ${vbsStr(runtime.exe)}`,
        `workerPath = ${vbsStr(workerPath)}`,
        `jobPath = ${vbsStr(jobPath)}`,
        `cmd = """" & exePath & """ """ & workerPath & """ --job """ & jobPath & """"${dryArgSuffix}`,
        'sh.Run cmd, 0, False',
        'fso.DeleteFile WScript.ScriptFullName, True',
        '',
      );
      const vbsSource = vbsLines.join('\r\n');
      const vbsBuf = Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(vbsSource, 'utf16le')]);
      writeFileSync(runnerVbsPath, vbsBuf, { mode: 0o600 });
      actPath = wscriptCandidate;
      actArgs = `//B //Nologo "${runnerVbsPath}"`;
    } else if (!runtime.isElectron) {
      actPath = runtime.exe;
      actArgs = `"${workerPath}" --job "${jobPath}"` + (jobConfig.dryRun ? ' --dry-run' : '');
    }

    if (actPath) {
      const psScript = `
$svc = New-Object -ComObject Schedule.Service
$svc.Connect()
$root = $svc.GetFolder('\\')
$td = $svc.NewTask(0)
$td.Principal.LogonType = 3
$td.Settings.Hidden = $true
$td.Settings.AllowDemandStart = $true
$td.Settings.DisallowStartIfOnBatteries = $false
$td.Settings.StopIfGoingOnBatteries = $false
$td.Settings.ExecutionTimeLimit = 'PT5M'
$act = $td.Actions.Create(0)
$act.Path = '${q(actPath)}'
$act.WorkingDirectory = '${q(workerDir)}'
$act.Arguments = '${q(actArgs)}'
$taskName = '${q(taskName)}'
$consumed = $false
try {
  $reg = $root.RegisterTaskDefinition($taskName, $td, 6, $null, $null, 3)
  $null = $reg.Run($null)
  for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 100
    if (-not (Test-Path -LiteralPath '${q(jobPath)}')) {
      $consumed = $true
      break
    }
  }
} finally {
  try { $root.DeleteTask($taskName, 0) } catch {}
}
if ($consumed) {
  Write-Output 'WORKER_CONSUMED_JOB'
} else {
  Write-Output 'TASK_SCHEDULER_NOT_CONSUMED'
}
`;
      try {
        const out = runPowerShellEncoded(psScript, 12_000);
        if (out.includes('WORKER_CONSUMED_JOB')) {
          try {
            if (runnerVbsPath && existsSync(runnerVbsPath)) unlinkSync(runnerVbsPath);
          } catch {
            // Best-effort cleanup.
          }
          appendWorkerLog(vaultDir, `launch method=task_scheduler consumed=true runtime=${runtime.source}`);
          return {
            launched: true,
            pid: null,
            method: 'task_scheduler',
            jobConsumed: true,
            runtimeSource: runtime.source,
            jobPath,
          };
        }
        appendWorkerLog(vaultDir, 'launch task_scheduler did not consume job within 3s; falling back to detached spawn');
      } catch (err) {
        appendWorkerLog(vaultDir, `launch task_scheduler error; falling back to detached spawn: ${err?.message || err}`);
      }
      try {
        if (runnerVbsPath && existsSync(runnerVbsPath)) unlinkSync(runnerVbsPath);
      } catch {
        // Best-effort cleanup.
      }
    }
  }

  const args = [workerPath, '--job', jobPath];
  if (jobConfig.dryRun) args.push('--dry-run');
  const spawnEnv = buildCleanGuiEnv(process.env);
  if (runtime.isElectron) {
    spawnEnv.ELECTRON_RUN_AS_NODE = '1';
  }
  const child = spawn(runtime.exe, args, {
    detached: true,
    stdio: 'ignore',
    windowsHide: true,
    env: spawnEnv,
  });
  child.unref();
  appendWorkerLog(vaultDir, `launch method=spawn pid=${child.pid || 'unknown'} runtime=${runtime.source}`);
  return {
    launched: true,
    pid: child.pid || null,
    method: 'spawn',
    runtimeSource: runtime.source,
    jobPath,
  };
}

// ---------------------------------------------------------------------------
// Direct CLI entry point (`node switch_worker.mjs --job <path> [--dry-run]`)
// ---------------------------------------------------------------------------
const isDirectRun = Boolean(
  process.argv[1] &&
    (process.platform === 'win32'
      ? resolve(__filename).toLowerCase() === resolve(process.argv[1]).toLowerCase()
      : import.meta.url === pathToFileURL(resolve(process.argv[1])).href),
);

if (isDirectRun) {
  const args = process.argv.slice(2);
  const jobIdx = args.indexOf('--job');
  const jobFile = jobIdx >= 0 ? args[jobIdx + 1] : null;
  const dryRunFlag = args.includes('--dry-run');

  if (!jobFile || !existsSync(jobFile)) {
    console.error('[switch_worker] Missing or invalid --job manifest.');
    process.exit(1);
  }

  let jobConfig = null;
  try {
    jobConfig = JSON.parse(readFileSync(jobFile, 'utf-8'));
  } catch (err) {
    console.error(`[switch_worker] Cannot read job manifest: ${err?.message || err}`);
    process.exit(1);
  } finally {
    try {
      unlinkSync(jobFile);
    } catch {
      // Ignore cleanup error.
    }
  }

  const effectiveDryRun = Boolean(dryRunFlag || jobConfig?.dryRun);
  runSwitchJob(jobConfig, { dryRun: effectiveDryRun })
    .then((status) => {
      if (effectiveDryRun) {
        console.log(JSON.stringify(status));
      }
      process.exit(0);
    })
    .catch((err) => {
      console.error(`[switch_worker] Switch failed: ${err?.message || err}`);
      process.exit(1);
    });
}
