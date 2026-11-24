/**
 * Cross-platform credential store and native `agy` CLI session helper.
 *
 * Clean-room MIT implementation for `quota-monitor`:
 * - Windows Credential Manager (`gemini:antigravity` via advapi32 P/Invoke over stdin Base64)
 * - macOS Keychain (`gemini` / `antigravity` with `go-keyring-base64:` prefix)
 * - Linux Secret Service (`secret-tool`)
 * - Native `agy` CLI session (`~/.gemini/antigravity-cli/antigravity-oauth-token`)
 *
 * Never logs, prints, or passes secret tokens on process command lines.
 */
import { execFileSync } from 'node:child_process';
import {
  chmodSync,
  existsSync,
  lstatSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';

export const CREDENTIAL_SERVICE = 'gemini';
export const CREDENTIAL_ACCOUNT = 'antigravity';
export const WINDOWS_CRED_TARGET = 'gemini:antigravity';
export const MACOS_KEYRING_PREFIX = 'go-keyring-base64:';
export const WIN_MAX_CRED_BLOB_BYTES = 2560;
export const DEFAULT_EXPIRY_ISO = '1970-01-01T00:00:00.000000Z';

const CRED_ERR_NOT_FOUND_MARK = 'QUOTA_MONITOR_CRED_NOT_FOUND';
const CRED_B64_PREFIX = 'QUOTA_MONITOR_B64:';

/**
 * Normalizes various Antigravity / OAuth token JSON shapes into the canonical
 * Go `oauth2.Token`-compatible credential wrapper used by Antigravity & `agy`.
 */
export function normalizeCredentialPayload(rawInput) {
  let obj = rawInput;
  if (typeof rawInput === 'string') {
    const trimmed = rawInput.trim();
    if (!trimmed) {
      throw new Error('Credential payload is empty.');
    }
    try {
      obj = JSON.parse(trimmed);
    } catch {
      throw new Error('Credential payload is not valid JSON.');
    }
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
    throw new Error('Credential payload must be a JSON object.');
  }

  const rawToken =
    obj.token && typeof obj.token === 'object' && !Array.isArray(obj.token)
      ? obj.token
      : obj;

  const refreshToken =
    typeof rawToken.refresh_token === 'string' ? rawToken.refresh_token.trim() : '';
  if (!refreshToken) {
    throw new Error('Credential payload is missing a valid refresh_token.');
  }

  const accessToken =
    typeof rawToken.access_token === 'string' ? rawToken.access_token : '';
  const tokenType =
    typeof rawToken.token_type === 'string' && rawToken.token_type.trim()
      ? rawToken.token_type.trim()
      : 'Bearer';
  const expiry = normalizeExpiryString(rawToken.expiry, rawToken.expiry_timestamp);
  const authMethod =
    typeof obj.auth_method === 'string' && obj.auth_method.trim()
      ? obj.auth_method.trim()
      : 'consumer';
  const idToken =
    typeof obj.id_token === 'string' && obj.id_token.trim()
      ? obj.id_token.trim()
      : typeof rawToken.id_token === 'string' && rawToken.id_token.trim()
        ? rawToken.id_token.trim()
        : null;

  const normalizedToken = { ...rawToken };
  delete normalizedToken.id_token;
  delete normalizedToken.expiry_timestamp;
  normalizedToken.access_token = accessToken;
  normalizedToken.token_type = tokenType;
  normalizedToken.refresh_token = refreshToken;
  normalizedToken.expiry = expiry;

  const normalized = {};
  for (const [k, v] of Object.entries(obj)) {
    if (
      k === 'token' ||
      k === 'access_token' ||
      k === 'refresh_token' ||
      k === 'token_type' ||
      k === 'expiry' ||
      k === 'expiry_timestamp' ||
      k === 'id_token'
    ) {
      continue;
    }
    normalized[k] = v;
  }

  normalized.token = normalizedToken;
  normalized.auth_method = authMethod;
  if (idToken) {
    normalized.id_token = idToken;
  }

  return normalized;
}

function normalizeExpiryString(expiryRaw, expiryTsRaw) {
  if (typeof expiryRaw === 'string' && expiryRaw.trim()) {
    const parsed = Date.parse(expiryRaw.trim());
    if (Number.isFinite(parsed)) {
      return toMicroIso(parsed);
    }
  }
  if (typeof expiryTsRaw === 'number' && Number.isFinite(expiryTsRaw) && expiryTsRaw > 0) {
    const ms = expiryTsRaw > 1e12 ? expiryTsRaw : expiryTsRaw * 1000;
    return toMicroIso(ms);
  }
  return DEFAULT_EXPIRY_ISO;
}

function toMicroIso(ms) {
  const iso = new Date(ms).toISOString();
  // Convert .123Z -> .123000Z to match Go RFC3339Micro formatting cleanly.
  return iso.replace(/\.(\d{3})Z$/, '.$1000Z');
}

/**
 * Serializes a normalized credential payload compactly, optionally enforcing a
 * maximum UTF-8 byte length (e.g., Windows CRED_MAX_CREDENTIAL_BLOB_SIZE = 2560).
 */
export function serializeCredentialPayload(payloadObj, { maxBytes = Infinity } = {}) {
  const normalized = normalizeCredentialPayload(payloadObj);
  let json = JSON.stringify(normalized);
  if (Buffer.byteLength(json, 'utf-8') <= maxBytes) {
    return json;
  }

  if (normalized.id_token) {
    const withoutIdToken = { ...normalized };
    delete withoutIdToken.id_token;
    json = JSON.stringify(withoutIdToken);
    if (Buffer.byteLength(json, 'utf-8') <= maxBytes) {
      return json;
    }
  }

  if (normalized.token?.access_token) {
    // Clearing cached access_token lets Go oauth2 re-mint one from refresh_token.
    const compact = {
      ...normalized,
      token: {
        ...normalized.token,
        access_token: '',
        expiry: DEFAULT_EXPIRY_ISO,
      },
    };
    delete compact.id_token;
    json = JSON.stringify(compact);
    if (Buffer.byteLength(json, 'utf-8') <= maxBytes) {
      return json;
    }
  }

  throw new Error(
    `Credential payload exceeds platform limit (${Buffer.byteLength(json, 'utf-8')} > ${maxBytes} bytes).`,
  );
}

/**
 * Extracts `{ email, name }` from a JWT `id_token` without verifying signatures
 * (used strictly as an offline display/identity fallback when UserStatus is absent).
 */
export function extractProfileFromIdToken(idToken) {
  if (typeof idToken !== 'string' || !idToken.includes('.')) {
    return { email: null, name: null };
  }
  const parts = idToken.split('.');
  if (parts.length < 2 || !parts[1]) {
    return { email: null, name: null };
  }
  try {
    const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const padLen = (4 - (b64.length % 4)) % 4;
    const jsonText = Buffer.from(b64 + '='.repeat(padLen), 'base64').toString('utf-8');
    const claims = JSON.parse(jsonText);
    const email =
      typeof claims?.email === 'string' && claims.email.includes('@')
        ? claims.email.trim()
        : null;
    const name =
      typeof claims?.name === 'string' && claims.name.trim()
        ? claims.name.trim()
        : null;
    return { email, name };
  } catch {
    return { email: null, name: null };
  }
}

export function extractEmailFromIdToken(idToken) {
  return extractProfileFromIdToken(idToken).email;
}

/**
 * Resolves the native `agy` CLI session file path if and only if `.gemini/antigravity-cli`
 * already exists as a real directory. Never initializes or creates a CLI directory.
 */
export function resolveCliSessionPath(homeDir = homedir()) {
  const cliDir = join(homeDir, '.gemini', 'antigravity-cli');
  let st;
  try {
    st = lstatSync(cliDir);
  } catch (err) {
    if (err && err.code === 'ENOENT') {
      return null;
    }
    throw new Error(`Cannot inspect agy CLI directory: ${err?.message || err}`);
  }
  if (st.isSymbolicLink() || !st.isDirectory()) {
    throw new Error('The agy session location must be a real directory.');
  }
  return join(cliDir, 'antigravity-oauth-token');
}

export function readCliSession({ homeDir = homedir() } = {}) {
  const sessionPath = resolveCliSessionPath(homeDir);
  if (!sessionPath) return null;
  let st;
  try {
    st = lstatSync(sessionPath);
  } catch (err) {
    if (err && err.code === 'ENOENT') return null;
    throw new Error(`Cannot inspect agy session file: ${err?.message || err}`);
  }
  if (st.isSymbolicLink() || !st.isFile()) {
    throw new Error('The agy session target must be a regular file.');
  }
  return readFileSync(sessionPath, 'utf-8');
}

export function writeFileAtomicVerified(filePath, content, mode = 0o600) {
  const dir = dirname(filePath);
  const tmpPath = join(dir, `.tmp-${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}`);
  const buf = typeof content === 'string' ? Buffer.from(content, 'utf-8') : content;
  try {
    writeFileSync(tmpPath, buf, { mode });
    renameSync(tmpPath, filePath);
    if (process.platform !== 'win32') {
      try {
        chmodSync(filePath, mode);
      } catch {
        // Best-effort permission tightening.
      }
    }
    const verify = readFileSync(filePath);
    if (!verify.equals(buf)) {
      throw new Error('Written file verification mismatch.');
    }
  } catch (err) {
    try {
      if (existsSync(tmpPath)) unlinkSync(tmpPath);
    } catch {
      // Ignore cleanup error.
    }
    throw err;
  }
}

/**
 * Synchronizes an initialized native `agy` CLI session file without touching
 * generic Gemini CLI files (`oauth_creds.json`, `google_accounts.json`).
 */
export function syncCliSession(payloadJson, { homeDir = homedir(), required = false } = {}) {
  const sessionPath = resolveCliSessionPath(homeDir);
  if (!sessionPath) {
    if (required) {
      throw new Error('No initialized agy CLI session directory found.');
    }
    return { synced: false, path: null };
  }

  let st = null;
  try {
    st = lstatSync(sessionPath);
  } catch (err) {
    if (!err || err.code !== 'ENOENT') {
      throw new Error(`Cannot inspect agy session file: ${err?.message || err}`);
    }
  }
  if (st && (st.isSymbolicLink() || !st.isFile())) {
    throw new Error('The agy session target must be a regular file, not a symlink or directory.');
  }

  writeFileAtomicVerified(sessionPath, payloadJson, 0o600);
  return { synced: true, path: sessionPath };
}

function resolveMockFile(mockFile) {
  const candidate =
    typeof mockFile === 'string' && mockFile.trim()
      ? mockFile.trim()
      : (process.env.ANTIGRAVITY_QUOTA_MOCK_KEYRING_FILE || '').trim();
  return candidate || null;
}

const WIN_CRED_CSHARP = `
using System;
using System.Runtime.InteropServices;

public static class QuotaMonitorCredHelper {
    private const uint CRED_TYPE_GENERIC = 1;
    private const uint CRED_PERSIST_LOCAL_MACHINE = 2;
    private const int ERROR_NOT_FOUND = 1168;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct FILETIME {
        public uint dwLowDateTime;
        public uint dwHighDateTime;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct CREDENTIALW_WRITE {
        public uint Flags;
        public uint Type;
        public string TargetName;
        public string Comment;
        public FILETIME LastWritten;
        public uint CredentialBlobSize;
        public IntPtr CredentialBlob;
        public uint Persist;
        public uint AttributeCount;
        public IntPtr Attributes;
        public string TargetAlias;
        public string UserName;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct CREDENTIALW_READ {
        public uint Flags;
        public uint Type;
        public IntPtr TargetName;
        public IntPtr Comment;
        public FILETIME LastWritten;
        public uint CredentialBlobSize;
        public IntPtr CredentialBlob;
        public uint Persist;
        public uint AttributeCount;
        public IntPtr Attributes;
        public IntPtr TargetAlias;
        public IntPtr UserName;
    }

    [DllImport("advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredReadW(string target, uint type, uint flags, out IntPtr credential);

    [DllImport("advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredWriteW(ref CREDENTIALW_WRITE credential, uint flags);

    [DllImport("advapi32.dll", EntryPoint = "CredDeleteW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredDeleteW(string target, uint type, uint flags);

    [DllImport("advapi32.dll", EntryPoint = "CredFree", SetLastError = true)]
    private static extern void CredFree(IntPtr buffer);

    public static string ReadBase64(string target) {
        IntPtr ptr = IntPtr.Zero;
        if (!CredReadW(target, CRED_TYPE_GENERIC, 0, out ptr) || ptr == IntPtr.Zero) {
            int err = Marshal.GetLastWin32Error();
            if (err == ERROR_NOT_FOUND) return null;
            throw new InvalidOperationException("CredReadW failed with Win32 error " + err);
        }
        try {
            CREDENTIALW_READ cred = (CREDENTIALW_READ)Marshal.PtrToStructure(ptr, typeof(CREDENTIALW_READ));
            int size = (int)cred.CredentialBlobSize;
            if (size <= 0 || cred.CredentialBlob == IntPtr.Zero) {
                return "";
            }
            byte[] bytes = new byte[size];
            Marshal.Copy(cred.CredentialBlob, bytes, 0, size);
            return Convert.ToBase64String(bytes);
        } finally {
            CredFree(ptr);
        }
    }

    public static void WriteBase64(string target, string user, string b64) {
        byte[] bytes = Convert.FromBase64String(b64);
        if (bytes.Length > 2560) {
            throw new InvalidOperationException("Credential exceeds Windows 2560-byte limit.");
        }
        IntPtr blob = bytes.Length > 0 ? Marshal.AllocHGlobal(bytes.Length) : IntPtr.Zero;
        try {
            if (bytes.Length > 0) {
                Marshal.Copy(bytes, 0, blob, bytes.Length);
            }
            CREDENTIALW_WRITE cred = new CREDENTIALW_WRITE();
            cred.Flags = 0;
            cred.Type = CRED_TYPE_GENERIC;
            cred.TargetName = target;
            cred.Comment = null;
            cred.CredentialBlobSize = (uint)bytes.Length;
            cred.CredentialBlob = blob;
            cred.Persist = CRED_PERSIST_LOCAL_MACHINE;
            cred.AttributeCount = 0;
            cred.Attributes = IntPtr.Zero;
            cred.TargetAlias = null;
            cred.UserName = user;

            CredDeleteW(target, CRED_TYPE_GENERIC, 0);
            if (!CredWriteW(ref cred, 0)) {
                int err = Marshal.GetLastWin32Error();
                throw new InvalidOperationException("CredWriteW failed with Win32 error " + err);
            }
        } finally {
            if (blob != IntPtr.Zero) {
                Marshal.FreeHGlobal(blob);
            }
        }
    }

    public static void Delete(string target) {
        if (!CredDeleteW(target, CRED_TYPE_GENERIC, 0)) {
            int err = Marshal.GetLastWin32Error();
            if (err != ERROR_NOT_FOUND) {
                throw new InvalidOperationException("CredDeleteW failed with Win32 error " + err);
            }
        }
    }
}
`;

function runWinCredPowerShell(psBody, stdinText = undefined) {
  const script =
    `$ErrorActionPreference = 'Stop'\n` +
    `$ProgressPreference = 'SilentlyContinue'\n` +
    `Add-Type -TypeDefinition @'\n${WIN_CRED_CSHARP}\n'@\n` +
    `${psBody}\n`;
  const encoded = Buffer.from(script, 'utf16le').toString('base64');
  try {
    const opts = {
      encoding: 'utf-8',
      windowsHide: true,
      timeout: 12_000,
    };
    if (stdinText !== undefined) {
      opts.input = stdinText;
    } else {
      opts.stdio = ['ignore', 'pipe', 'pipe'];
    }
    return execFileSync(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
      opts,
    );
  } catch (err) {
    const stderr = String(err?.stderr || err?.message || '').trim();
    throw new Error(`Windows Credential Manager operation failed: ${stderr.slice(0, 240)}`);
  }
}

export function readSystemCredential({ mockFile } = {}) {
  const mockPath = resolveMockFile(mockFile);
  if (mockPath) {
    if (!existsSync(mockPath)) return null;
    const raw = readFileSync(mockPath, 'utf-8').trim();
    return raw || null;
  }

  if (process.platform === 'win32') {
    const out = runWinCredPowerShell(
      `$b64 = [QuotaMonitorCredHelper]::ReadBase64('${WINDOWS_CRED_TARGET}')\n` +
        `if ($null -eq $b64) { Write-Output '${CRED_ERR_NOT_FOUND_MARK}' } else { Write-Output ('${CRED_B64_PREFIX}' + $b64) }`,
    );
    if (out.includes(CRED_ERR_NOT_FOUND_MARK)) return null;
    const m = /QUOTA_MONITOR_B64:([A-Za-z0-9+/=]*)/.exec(out);
    if (!m) {
      throw new Error('Unexpected response while reading Windows Credential Manager.');
    }
    const b64 = m[1];
    return b64 ? Buffer.from(b64, 'base64').toString('utf-8') : null;
  }

  if (process.platform === 'darwin') {
    try {
      const raw = execFileSync(
        'security',
        ['find-generic-password', '-s', CREDENTIAL_SERVICE, '-a', CREDENTIAL_ACCOUNT, '-w'],
        { encoding: 'utf-8', timeout: 8_000 },
      ).trim();
      if (!raw) return null;
      if (raw.startsWith(MACOS_KEYRING_PREFIX)) {
        return Buffer.from(raw.slice(MACOS_KEYRING_PREFIX.length), 'base64').toString('utf-8');
      }
      return raw;
    } catch {
      return null;
    }
  }

  if (process.platform === 'linux') {
    try {
      const raw = execFileSync(
        'secret-tool',
        ['lookup', 'service', CREDENTIAL_SERVICE, 'username', CREDENTIAL_ACCOUNT],
        { encoding: 'utf-8', timeout: 8_000 },
      ).trim();
      return raw || null;
    } catch {
      return null;
    }
  }

  throw new Error(`Unsupported platform for credential store: ${process.platform}`);
}

function writeRawSystemCredential(rawPayload, { mockFile } = {}) {
  const text = String(rawPayload ?? '');
  const mockPath = resolveMockFile(mockFile);
  if (mockPath) {
    writeFileAtomicVerified(mockPath, text, 0o600);
    return text;
  }

  if (process.platform === 'win32') {
    const b64 = Buffer.from(text, 'utf-8').toString('base64');
    runWinCredPowerShell(
      `$b64 = [Console]::In.ReadToEnd().Trim()\n` +
        `[QuotaMonitorCredHelper]::WriteBase64('${WINDOWS_CRED_TARGET}', '${CREDENTIAL_ACCOUNT}', $b64)`,
      b64,
    );
    return text;
  }

  if (process.platform === 'darwin') {
    const encoded = `${MACOS_KEYRING_PREFIX}${Buffer.from(text, 'utf-8').toString('base64')}`;
    try {
      execFileSync(
        'security',
        [
          'add-generic-password',
          '-U',
          '-s',
          CREDENTIAL_SERVICE,
          '-a',
          CREDENTIAL_ACCOUNT,
          '-w',
          encoded,
          '-A',
        ],
        { stdio: 'pipe', timeout: 8_000 },
      );
    } catch {
      try {
        execFileSync(
          'security',
          ['delete-generic-password', '-s', CREDENTIAL_SERVICE, '-a', CREDENTIAL_ACCOUNT],
          { stdio: 'ignore', timeout: 5_000 },
        );
      } catch {
        // Ignore if absent.
      }
      execFileSync(
        'security',
        [
          'add-generic-password',
          '-s',
          CREDENTIAL_SERVICE,
          '-a',
          CREDENTIAL_ACCOUNT,
          '-w',
          encoded,
          '-A',
        ],
        { stdio: 'pipe', timeout: 8_000 },
      );
    }
    return text;
  }

  if (process.platform === 'linux') {
    execFileSync(
      'secret-tool',
      [
        'store',
        '--label=Antigravity',
        'service',
        CREDENTIAL_SERVICE,
        'username',
        CREDENTIAL_ACCOUNT,
      ],
      { input: text, encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'], timeout: 8_000 },
    );
    return text;
  }

  throw new Error(`Unsupported platform for credential store: ${process.platform}`);
}

export function writeSystemCredential(payloadJson, { mockFile } = {}) {
  const maxBytes = process.platform === 'win32' ? WIN_MAX_CRED_BLOB_BYTES : Infinity;
  const serialized = serializeCredentialPayload(payloadJson, { maxBytes });
  return writeRawSystemCredential(serialized, { mockFile });
}

export function deleteSystemCredential({ mockFile } = {}) {
  const mockPath = resolveMockFile(mockFile);
  if (mockPath) {
    if (existsSync(mockPath)) unlinkSync(mockPath);
    return;
  }
  if (process.platform === 'win32') {
    runWinCredPowerShell(`[QuotaMonitorCredHelper]::Delete('${WINDOWS_CRED_TARGET}')`);
    return;
  }
  if (process.platform === 'darwin') {
    try {
      execFileSync(
        'security',
        ['delete-generic-password', '-s', CREDENTIAL_SERVICE, '-a', CREDENTIAL_ACCOUNT],
        { stdio: 'ignore', timeout: 5_000 },
      );
    } catch {
      // Ignore if absent.
    }
    return;
  }
  if (process.platform === 'linux') {
    try {
      execFileSync(
        'secret-tool',
        ['clear', 'service', CREDENTIAL_SERVICE, 'username', CREDENTIAL_ACCOUNT],
        { stdio: 'ignore', timeout: 5_000 },
      );
    } catch {
      // Ignore if absent.
    }
  }
}

/**
 * Atomically updates both the OS credential store (`gemini:antigravity`) and
 * an initialized native `agy` CLI session (`~/.gemini/antigravity-cli/antigravity-oauth-token`),
 * rolling back the OS credential if CLI synchronization fails.
 */
export function writeAllCredentials(payloadInput, { homeDir = homedir(), mockFile } = {}) {
  const maxBytes = process.platform === 'win32' ? WIN_MAX_CRED_BLOB_BYTES : Infinity;
  const serialized = serializeCredentialPayload(payloadInput, { maxBytes });

  let prevOsCred = null;
  let hadPrevLookup = false;
  try {
    prevOsCred = readSystemCredential({ mockFile });
    hadPrevLookup = true;
  } catch {
    hadPrevLookup = false;
  }

  writeSystemCredential(serialized, { mockFile });

  try {
    const cliResult = syncCliSession(serialized, { homeDir, required: false });
    return {
      serialized,
      cliSynced: cliResult.synced,
      cliPath: cliResult.path,
    };
  } catch (cliErr) {
    let rollbackNote = '';
    if (hadPrevLookup) {
      try {
        if (prevOsCred) {
          writeRawSystemCredential(prevOsCred, { mockFile });
          rollbackNote = ' Previous OS credential was restored.';
        } else {
          deleteSystemCredential({ mockFile });
          rollbackNote = ' Previous empty OS credential state was restored.';
        }
      } catch (rbErr) {
        rollbackNote = ` Rollback failed (${rbErr?.message || rbErr}).`;
      }
    }
    throw new Error(
      `System credential store updated, but agy CLI session sync failed: ${cliErr?.message || cliErr}.${rollbackNote}`,
    );
  }
}
