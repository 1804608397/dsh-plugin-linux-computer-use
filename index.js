/**
 * Host half of the Linux Computer Use bundle.
 *
 * The bundle's real work is done by the stdio MCP server under ./server (see
 * cordis.patch.yml, entry "linux-computer-use-mcp"). This module adds the one
 * thing an MCP server cannot do for itself: a diagnostic tool that still works
 * while that server is disabled or failing to start.
 *
 * Deliberately dependency-free (node builtins only): a bundle that imports
 * DSH internals breaks on upgrade, and the doctor is exactly what you need
 * when something else is broken.
 */
import { execFile } from 'node:child_process'
import {
  accessSync,
  constants as fsConstants,
  copyFileSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
} from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PACKAGE_SERVER_SCRIPT = join(HERE, 'server', 'linux_desktop_mcp.py')

/**
 * Where this bundle keeps its own copy of the Python server.
 *
 * cordis.patch.yml cannot know where a published package lands, so the patch
 * runs a fixed launcher path instead; the launcher execs whatever is in this
 * directory. Keeping the copy under the user's home means a package update
 * refreshes the server without root.
 */
const INSTALL_DIR = join(homedir(), '.local', 'share', 'dsh-linux-computer-use')
const LAUNCHER = '/usr/local/bin/dsh-linux-desktop-mcp'
const SELFTEST_TIMEOUT_MS = 45000

/** Copy ./server/*.py into INSTALL_DIR, overwriting only files that changed. */
function syncServerFiles() {
  const source = join(HERE, 'server')
  if (!existsSync(source)) return { ok: false, dir: INSTALL_DIR, detail: `missing package directory ${source}` }
  const copied = []
  try {
    mkdirSync(INSTALL_DIR, { recursive: true, mode: 0o700 })
    for (const name of readdirSync(source)) {
      if (!name.endsWith('.py')) continue
      const from = join(source, name)
      const to = join(INSTALL_DIR, name)
      const wanted = readFileSync(from)
      if (existsSync(to)) {
        const current = readFileSync(to)
        if (current.length === wanted.length && current.equals(wanted)) continue
      }
      copyFileSync(from, to)
      copied.push(name)
    }
    return { ok: true, dir: INSTALL_DIR, copied }
  } catch (error) {
    return { ok: false, dir: INSTALL_DIR, detail: error?.message ?? String(error), copied }
  }
}

/** The server script the MCP bridge actually runs: the synced copy when present. */
function resolveServerScript() {
  const installed = join(INSTALL_DIR, 'linux_desktop_mcp.py')
  return existsSync(installed) ? installed : PACKAGE_SERVER_SCRIPT
}

function check(list, id, status, title, detail, fix) {
  const entry = { id, status, title }
  if (detail) entry.detail = detail
  if (fix) entry.fix = fix
  list.push(entry)
}

/** Run the server's own --selftest and parse its JSON report. */
function runSelftest(python, script) {
  return new Promise((resolve) => {
    execFile(
      python,
      ['-u', script, '--selftest'],
      { timeout: SELFTEST_TIMEOUT_MS, maxBuffer: 4 * 1024 * 1024, env: process.env },
      (error, stdout, stderr) => {
        if (error) {
          resolve({ error: `${error.message}${stderr ? `\n${String(stderr).trim().slice(-600)}` : ''}` })
          return
        }
        try {
          resolve({ report: JSON.parse(String(stdout)) })
        } catch (parseError) {
          resolve({ error: `could not parse the self-test output: ${parseError.message}` })
        }
      },
    )
  })
}

/** Names of the MCP tools this bundle publishes, if the bridge is live. */
function bridgedTools(tools) {
  try {
    const definitions = typeof tools?.definitions === 'function' ? tools.definitions() : []
    return [...definitions]
      .map((definition) => definition?.name)
      .filter((name) => typeof name === 'string' && name.startsWith('mcp__linux__'))
  } catch {
    return []
  }
}

function renderReport(_args, value) {
  const icon = { ok: 'ok  ', warn: 'warn', fail: 'FAIL' }
  const lines = []
  const failures = value.checks.filter((entry) => entry.status === 'fail').length
  const warnings = value.checks.filter((entry) => entry.status === 'warn').length
  lines.push(
    failures === 0
      ? `Linux Computer Use: ready${warnings > 0 ? ` (${warnings} warning${warnings === 1 ? '' : 's'})` : ''}`
      : `Linux Computer Use: ${failures} blocking problem${failures === 1 ? '' : 's'}, ${warnings} warning${warnings === 1 ? '' : 's'}`,
  )
  for (const entry of value.checks) {
    const head = `- [${icon[entry.status] ?? entry.status}] ${entry.title}`
    lines.push(entry.detail ? `${head}\n  ${entry.detail}` : head)
    if (entry.fix) lines.push(`  fix: ${entry.fix}`)
  }
  if (value.server?.log) lines.push(`\nserver log: ${value.server.log}`)
  if (value.summary) lines.push(`\n${value.summary}`)
  return [{ type: 'text', text: lines.join('\n') }]
}

const CHECK_SCHEMA = {
  type: 'object',
  properties: {
    id: { type: 'string' },
    status: { type: 'string', enum: ['ok', 'warn', 'fail'] },
    title: { type: 'string' },
    detail: { type: 'string' },
    fix: { type: 'string' },
  },
  required: ['id', 'status', 'title'],
}

/** Build the linux_mcp_doctor tool definition for one configured python. */
function createDoctor(tools, python, sync) {
  return {
    name: 'linux_mcp_doctor',
    description: [
      'Diagnose the Linux Computer Use bundle: whether the MCP bridge is live (mcp__linux__* tools registered),',
      'whether /dev/uinput is writable so pointer and keyboard injection can work, and whether screenshots,',
      'AT-SPI, the portal and the clipboard helpers are available.',
      'Run it first whenever a desktop action fails, or before asking the user to install something.',
    ].join(' '),
    parameters: {
      type: 'object',
      properties: {
        verbose: {
          type: 'boolean',
          default: false,
          description: 'Include raw paths and the full environment block in the report.',
        },
      },
      additionalProperties: false,
    },
    timeoutMs: SELFTEST_TIMEOUT_MS + 15000,
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          summary: { type: 'string' },
          bridge: {
            type: 'object',
            properties: {
              tools_registered: { type: 'number' },
              names: { type: 'array', items: { type: 'string' } },
            },
          },
          checks: { type: 'array', items: CHECK_SCHEMA },
          server: {
            type: 'object',
            properties: {
              name: { type: 'string' },
              version: { type: 'string' },
              log: { type: 'string' },
              script: { type: 'string' },
            },
          },
        },
        required: ['ok', 'checks'],
      },
      render: renderReport,
    },
    async execute(args) {
      const checks = []
      const script = resolveServerScript()
      const report = { ok: false, checks, bridge: { tools_registered: 0, names: [] }, server: { script } }

      if (sync && sync.ok === false) {
        check(
          checks,
          'install-sync',
          'warn',
          'could not refresh the installed server copy',
          sync.detail,
          'check that ~/.local/share is writable, then reload this plugin',
        )
      }

      const bridged = bridgedTools(tools)
      report.bridge = { tools_registered: bridged.length, names: bridged }
      if (bridged.length > 0) {
        check(checks, 'bridge', 'ok', `MCP bridge is live (${bridged.length} tools as mcp__linux__*)`)
      } else {
        check(
          checks,
          'bridge',
          'warn',
          'MCP bridge is not running (no mcp__linux__* tools registered)',
          'Computer Use ships disabled, so this is expected until you switch it on.',
          'Settings -> Plugins -> Linux Computer Use: enable it, then reload the page',
        )
      }

      if (!existsSync(script)) {
        check(checks, 'server-script', 'fail', 'the MCP server script is missing', script, 'reinstall the bundle from the plugins page')
        report.summary = 'The bundle looks incomplete; reinstall it before anything else.'
        return report
      }
      check(checks, 'server-script', 'ok', 'MCP server script present', script)

      if (existsSync(LAUNCHER)) {
        check(checks, 'launcher', 'ok', 'MCP launcher installed', LAUNCHER)
      } else {
        check(
          checks,
          'launcher',
          'fail',
          'the MCP launcher is missing, so the bridge cannot start',
          LAUNCHER,
          'sudo ./linux-computer-use-prep.sh installs it (it execs the copy under ~/.local/share)',
        )
      }

      try {
        accessSync('/dev/uinput', fsConstants.R_OK | fsConstants.W_OK)
        check(checks, 'uinput', 'ok', '/dev/uinput is writable by this account')
      } catch (error) {
        const detail =
          error?.code === 'ENOENT'
            ? '/dev/uinput does not exist on this machine (kernel built without CONFIG_INPUT_UINPUT?)'
            : '/dev/uinput exists but this account cannot open it for writing'
        check(
          checks,
          'uinput',
          'fail',
          'pointer and keyboard injection cannot work',
          detail,
          'sudo ./linux-computer-use-prep.sh, then log out and back in so the "input" group applies',
        )
      }

      const selftest = await runSelftest(python, script)
      if (selftest.error) {
        check(
          checks,
          'selftest',
          'fail',
          'the desktop server self-test crashed',
          selftest.error,
          'check python3-gi and gir1.2-atspi-2.0 are installed, then run: python3 server/linux_desktop_mcp.py --selftest',
        )
      } else {
        report.server = { ...(selftest.report.server ?? {}), script }
        for (const entry of selftest.report.checks ?? []) checks.push(entry)
      }

      report.ok = !checks.some((entry) => entry.status === 'fail')
      const failures = checks.filter((entry) => entry.status === 'fail').length
      const warnings = checks.filter((entry) => entry.status === 'warn').length
      report.summary = report.ok
        ? `All blocking checks passed${warnings > 0 ? ` (${warnings} warning${warnings === 1 ? '' : 's'})` : ''}.`
        : `${failures} blocking problem${failures === 1 ? '' : 's'} must be fixed first.`
      if (args?.verbose === true) {
        report.environment = { session: process.env.XDG_SESSION_TYPE, desktop: process.env.XDG_CURRENT_DESKTOP, python }
      }
      return report
    },
  }
}

export const inject = ['tools']

export const name = 'linux-computer-use-doctor'

export function apply(context, config = {}) {
  const configured = typeof config?.python === 'string' && config.python ? config.python : undefined
  const python = configured ?? process.env.DSH_LINUX_CU_PYTHON ?? 'python3'
  // Keep the launcher's copy of the server in step with the installed package,
  // so updating the bundle updates the MCP server with it.
  const sync = syncServerFiles()
  const tool = createDoctor(context.tools, python, sync)
  context.effect(() => context.tools.register(tool))
}
