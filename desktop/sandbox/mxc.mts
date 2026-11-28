// WittyCode Windows sandbox helper — bridge between the Python backend and the
// official Microsoft MXC Node SDK (@microsoft/mxc-sdk/v1).
//
// Runs directly on Node 24+ (built-in type stripping). Invoked as:
//
//     node.exe mxc.mts            # request JSON arrives on stdin
//
// stdin JSON:
//   {
//     "action":  "probe" | "run",
//     "sdkDir":  "<dir that contains node_modules/@microsoft/mxc-sdk>",
//     "request": { <ContainerRequest fields> }          // run only
//   }
//
// probe: prints one JSON line {ok, detail} on stdout, exits 0 (or 2 on error).
// run:   the native SDK always re-probes the actual request first; when it
//        cannot be served, reports {refused: true, probeError} and launches
//        nothing. Otherwise runs it and prints one bounded JSON line
//        {refused, exitCode, timedOut, warnings, stdoutB64, stderrB64} — the
//        workload's captured streams are truncated to STREAM_CAP bytes each
//        and base64-encoded so the frame is always complete and well under
//        the host's 4 MiB read cap; exit code/timeout are fields, never the
//        helper's own exit code. Helper failures print MXC_HELPER_ERROR on
//        stderr and exit 2.

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { pathToFileURL } from 'node:url';

const HELPER_ERROR_EXIT = 2;
const KNOWN_TIERS = new Set(['base-container', 'appcontainer-bfs', 'appcontainer-dacl']);
const STREAM_CAP = 512 * 1024;    // bytes kept per captured stream
const MAX_WARNINGS = 8;
const WARNING_CAP = 200;

// Bytes-safe cap: truncate the UTF-8 buffer, never a UTF-16 string mid-codepoint.
function cap64(text: unknown): string {
  const buf = Buffer.from(typeof text === 'string' ? text : '', 'utf8');
  return buf.subarray(0, STREAM_CAP).toString('base64');
}

function capWarnings(warnings: unknown): string[] {
  return Array.isArray(warnings)
    ? warnings.slice(0, MAX_WARNINGS).map((w) => String(w).slice(0, WARNING_CAP))
    : [];
}

interface HelperRequest {
  action: string;
  sdkDir: string;
  request?: Record<string, unknown>;
}

function readStdin(): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    process.stdin.on('data', (chunk: Buffer) => chunks.push(chunk));
    process.stdin.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    process.stdin.on('error', reject);
    process.stdin.resume();
  });
}

function sdkEntryUrl(sdkDir: string): string {
  const pkgJsonPath = join(sdkDir, 'node_modules', '@microsoft', 'mxc-sdk', 'package.json');
  const pkg = JSON.parse(readFileSync(pkgJsonPath, 'utf8'));
  let sub: unknown = pkg?.exports?.['./v1'];
  if (sub && typeof sub === 'object') {
    const cond = sub as Record<string, unknown>;
    sub = cond.import ?? cond.default ?? cond.require ?? cond.types;
  }
  if (typeof sub !== 'string' || !sub) sub = './dist/v1/index.js';
  return pathToFileURL(join(dirname(pkgJsonPath), sub as string)).href;
}

async function loadSdk(sdkDir: string): Promise<Record<string, any>> {
  const url = sdkEntryUrl(sdkDir);
  return (await import(url)) as Record<string, any>;
}

async function doProbe(sdk: Record<string, any>, request: Record<string, unknown> | undefined): Promise<number> {
  const support = sdk.getPlatformSupport?.();
  const out = sdk.probe ? sdk.probe(request) : { error: 'probe() not exported by SDK' };
  const detail = {
    isSupported: support?.isSupported ?? null,
    reason: support?.reason ?? null,
    availableMethods: support?.availableMethods ?? [],
    isolationTier: out?.tier ?? support?.isolationTier ?? null,
    probeError: out?.error ?? null,
    warnings: [...(out?.warnings ?? []), ...(support?.isolationWarnings ?? [])],
  };
  const ok = Boolean(support?.isSupported) && !out?.error && KNOWN_TIERS.has(out?.tier ?? support?.isolationTier);
  process.stdout.write(JSON.stringify({ ok, detail }) + '\n');
  return 0;
}

async function doRun(sdk: Record<string, any>, request: Record<string, unknown>): Promise<number> {
  // Native probe against the actual policy: refuse before any launch.
  const check = sdk.probe(request);
  if (check && check.error) {
    process.stdout.write(JSON.stringify({
      refused: true,
      probeError: String(check.error).slice(0, 500),
      warnings: capWarnings(check.warnings),
    }) + '\n');
    return 0;
  }
  const result = await sdk.run(request);
  process.stdout.write(JSON.stringify({
    refused: false,
    exitCode: result.exitCode,
    timedOut: Boolean(result.timedOut),
    warnings: capWarnings(result.warnings),
    stdoutB64: cap64(result.stdout),
    stderrB64: cap64(result.stderr),
  }) + '\n');
  return 0;
}

async function main(): Promise<number> {
  const raw = await readStdin();
  const parsed = JSON.parse(raw) as HelperRequest;
  if (!parsed || typeof parsed.sdkDir !== 'string' || !parsed.sdkDir) {
    process.stderr.write('MXC_HELPER_ERROR missing sdkDir\n');
    return HELPER_ERROR_EXIT;
  }
  const sdk = await loadSdk(parsed.sdkDir);
  if (parsed.action === 'probe') {
    return await doProbe(sdk, parsed.request);
  }
  if (parsed.action === 'run') {
    if (!parsed.request || typeof parsed.request.command !== 'string') {
      process.stderr.write('MXC_HELPER_ERROR missing request.command\n');
      return HELPER_ERROR_EXIT;
    }
    return await doRun(sdk, parsed.request);
  }
  process.stderr.write(`MXC_HELPER_ERROR unknown action ${String(parsed.action)}\n`);
  return HELPER_ERROR_EXIT;
}

main().then(
  (code) => { process.exitCode = code; },
  (error) => {
    const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    process.stderr.write(`MXC_HELPER_ERROR ${message}\n`);
    process.exitCode = HELPER_ERROR_EXIT;
  },
);
