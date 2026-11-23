/**
 * Portable launcher for the Antigravity Windows desktop floating quota widget.
 *
 * Automatically locates a Python installation with PyQt5 available without
 * relying on hardcoded machine-specific paths. Exits cleanly (code 0) on
 * non-Windows platforms or when PyQt5 is not installed so the right-sidebar
 * panel remains unaffected.
 */
import { execFileSync, spawn } from 'node:child_process';
import { existsSync, readdirSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const widgetScript = resolve(__dirname, '..', '..', 'widget', 'quota_floating_widget.pyw');
const args = new Set(process.argv.slice(2));
const isDryRun = args.has('--dry-run');
const isDetached = args.has('--detached');

function isRealExecutable(filePath) {
  try {
    if (!filePath || !existsSync(filePath)) return false;
    // Skip Microsoft Store zero-byte app execution aliases if they aren't usable.
    if (/\\Microsoft\\WindowsApps\\/i.test(filePath)) return false;
    return statSync(filePath).isFile();
  } catch {
    return false;
  }
}

function whereExe(name) {
  try {
    const out = execFileSync('where.exe', [name], {
      encoding: 'utf-8',
      windowsHide: true,
      timeout: 3000,
    });
    return out
      .split(/\r?\n/)
      .map((l) => l.trim())
      .filter(Boolean);
  } catch {
    return [];
  }
}

function collectPythonCandidates() {
  const candidates = [];
  const seen = new Set();
  const push = (p) => {
    if (!p) return;
    const norm = resolve(p);
    const key = norm.toLowerCase();
    if (!seen.has(key) && isRealExecutable(norm)) {
      seen.add(key);
      candidates.push(norm);
    }
  };

  // 1. Explicit user override
  if (process.env.ANTIGRAVITY_QUOTA_PYTHON) {
    push(process.env.ANTIGRAVITY_QUOTA_PYTHON);
  }

  // 2. Active PATH lookups
  for (const exe of whereExe('pythonw')) push(exe);
  for (const exe of whereExe('python')) push(exe);

  // 3. Common user/system Python & Conda installation directories (dynamic)
  const home = homedir();
  const localAppData = process.env.LOCALAPPDATA || join(home, 'AppData', 'Local');
  const userProfile = process.env.USERPROFILE || home;
  const programData = process.env.ProgramData || 'C:\\ProgramData';
  const programFiles = process.env.ProgramFiles || 'C:\\Program Files';

  const baseRoots = [
    join(userProfile, 'anaconda3'),
    join(userProfile, 'miniconda3'),
    join(userProfile, 'miniforge3'),
    join(userProfile, 'mambaforge'),
    join(localAppData, 'anaconda3'),
    join(localAppData, 'miniconda3'),
    join(programData, 'anaconda3'),
    join(programData, 'miniconda3'),
  ];

  for (const root of baseRoots) {
    push(join(root, 'pythonw.exe'));
    push(join(root, 'python.exe'));
    const envsDir = join(root, 'envs');
    try {
      if (existsSync(envsDir)) {
        for (const ent of readdirSync(envsDir, { withFileTypes: true })) {
          if (ent.isDirectory()) {
            push(join(envsDir, ent.name, 'pythonw.exe'));
            push(join(envsDir, ent.name, 'python.exe'));
          }
        }
      }
    } catch {
      // Ignore unreadable directories.
    }
  }

  const pythonRoots = [
    join(localAppData, 'Programs', 'Python'),
    programFiles,
    'C:\\',
  ];
  for (const parent of pythonRoots) {
    try {
      if (!existsSync(parent)) continue;
      for (const ent of readdirSync(parent, { withFileTypes: true })) {
        if (ent.isDirectory() && /^Python3\d+$/i.test(ent.name)) {
          push(join(parent, ent.name, 'pythonw.exe'));
          push(join(parent, ent.name, 'python.exe'));
        }
      }
    } catch {
      // Ignore unreadable directories.
    }
  }

  return candidates;
}

function toProbeAndGuiPair(candidate) {
  const dir = dirname(candidate);
  const siblingPython = join(dir, 'python.exe');
  const siblingPythonw = join(dir, 'pythonw.exe');
  const probeExe = isRealExecutable(siblingPython) ? siblingPython : candidate;
  const guiExe = isRealExecutable(siblingPythonw) ? siblingPythonw : candidate;
  return { probeExe, guiExe };
}

function hasPyQt5(probeExe) {
  try {
    execFileSync(probeExe, ['-c', 'import PyQt5.QtWidgets'], {
      stdio: 'ignore',
      windowsHide: true,
      timeout: 5000,
    });
    return true;
  } catch {
    return false;
  }
}

function findPyQt5Python() {
  const testedProbes = new Set();
  for (const candidate of collectPythonCandidates()) {
    const { probeExe, guiExe } = toProbeAndGuiPair(candidate);
    const key = probeExe.toLowerCase();
    if (testedProbes.has(key)) continue;
    testedProbes.add(key);
    if (hasPyQt5(probeExe)) {
      return guiExe;
    }
  }
  return null;
}

if (process.platform !== 'win32') {
  const msg = `[quota_floating] Floating desktop widget is Windows-only (current platform: ${process.platform}); right-sidebar panel remains active.`;
  if (isDryRun) {
    console.log(JSON.stringify({
      ok: true,
      platform: process.platform,
      pythonExe: null,
      widgetScript,
      widgetExists: existsSync(widgetScript),
      note: msg,
    }, null, 2));
  } else {
    console.log(msg);
  }
  process.exit(0);
}

const widgetExists = existsSync(widgetScript);
const pythonExe = findPyQt5Python();

if (isDryRun) {
  const ok = Boolean(pythonExe && widgetExists);
  console.log(JSON.stringify({
    ok,
    platform: process.platform,
    pythonExe,
    widgetScript,
    widgetExists,
  }, null, 2));
  process.exit(ok ? 0 : 1);
}

if (!widgetExists) {
  console.error(`[quota_floating] Widget script not found: ${widgetScript}`);
  process.exit(0);
}

if (!pythonExe) {
  console.log(
    '[quota_floating] Python with PyQt5 not found. Install PyQt5 (e.g. `pip install PyQt5`) ' +
    'or set ANTIGRAVITY_QUOTA_PYTHON to enable the floating desktop widget.',
  );
  process.exit(0);
}

if (isDetached) {
  const child = spawn(pythonExe, [widgetScript], {
    detached: true,
    stdio: 'ignore',
    windowsHide: true,
  });
  child.unref();
  process.exit(0);
}

const child = spawn(pythonExe, [widgetScript], {
  stdio: 'inherit',
  windowsHide: true,
});

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => {
    try {
      child.kill(sig);
    } catch {
      // Ignore if already exited.
    }
  });
}

child.on('error', (err) => {
  console.error(`[quota_floating] Failed to launch widget: ${err?.message || err}`);
  process.exit(1);
});

child.on('exit', (code) => {
  process.exit(typeof code === 'number' ? code : 0);
});
