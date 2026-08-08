<div align="center">

# Hilanet Attendance Skill

### Report attendance by telling your AI agent what happened

[![Agent Skill](https://img.shields.io/badge/Agent%20Skill-attendance--reporter-black)](skills/attendance-reporter/SKILL.md)
[![Tests](https://img.shields.io/badge/tests-36%20passing-brightgreen)](#development)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D4)](#requirements)

</div>

```text
You: Report vacation on August 2.

Agent: I am going to report:
       • Sunday, 2026-08-02
       • Type: vacation
       Approve?

You: Yes.

Agent: Done. Hilanet now shows vacation for 2026-08-02.
```

The skill discovers your employer's current Hilanet reporting options, reads
the exact date, creates a signed preview, waits for approval, performs the
operation once, and reloads Hilanet to prove the result.

## Quick start

You need Windows, Python 3.12 or newer, Microsoft Edge, and a Hilanet account
that supports password login.

### Ask your AI to set it up

Paste this into Claude Code, GitHub Copilot, Codex, Cursor, or another agent
that can use a terminal:

```text
Install the attendance-reporter skill from
https://github.com/asaf5767/hilanet-reporter-skill.

Then:
1. Install the pinned Python dependency from the skill's requirements.txt.
2. Find the installed attendance-reporter skill directory.
3. Ask me for my Hilanet tenant and username, but never ask for my password in chat.
4. Run scripts/attendance.py setup in an interactive private terminal so I can
   enter the password myself.
5. Run scripts/attendance.py doctor.
6. Confirm that doctor returns status "ready" and show me the reporting options.
7. Tell me to reload skills or start a new agent session.

Do not submit attendance during setup.
```

The agent can handle installation, dependency setup, tenant discovery, and
verification. You must type the password yourself into the private setup prompt.

### 1. Install the skill

The easiest option is the Skills CLI:

```powershell
npx skills add asaf5767/hilanet-reporter-skill --global
```

Install Playwright:

```powershell
python -m pip install playwright==1.61.0
```

### 2. Open the installed skill directory

The usual locations are:

```text
~/.claude/skills/attendance-reporter   # Claude Code
~/.agents/skills/attendance-reporter   # Copilot, Codex, Cursor, VS Code
```

Run the remaining commands from that directory.

### 3. Find your tenant

Your tenant is the part before `.hilan.co.il`.

```text
https://acme.net.hilan.co.il/login
        ^^^^^^^^
tenant: acme.net
```

### 4. Store your credentials

```powershell
python scripts\attendance.py setup `
  --tenant YOUR_TENANT `
  --username YOUR_USERNAME
```

The command asks for the password privately and stores it in Windows Credential
Manager. Never paste the password into chat.

### 5. Verify the connection

```powershell
python scripts\attendance.py doctor
```

Setup is complete when the result contains:

```json
{
  "status": "ready"
}
```

### 6. Reload your agent and try it

Start a new agent session or reload skills, then ask:

```text
What attendance-reporting options are available?
```

Then report a date:

```text
Report vacation on 2026-08-02.
```

The agent will show the exact action and ask for approval before writing.

## Why this exists

Hilanet has no public attendance API. Existing tools reconstruct its private
ASP.NET form requests, but those requests can differ between tenants.

This project uses two approaches:

- Read-only HTTPS requests for login, calendar state, and reporting-type
  discovery.
- Hilan's own browser controls in a fresh headless Edge session for confirmed
  writes and proof.

That browser-backed write path was required by the live-tested enterprise
tenant, which rejected reconstructed form submissions as overlapping reports.

## Verified compatibility

| Environment | Status |
|---|---|
| One enterprise Hilanet tenant | Live tested |
| Windows 10/11 | Supported |
| Microsoft Edge | Required |
| Python 3.12+ | Supported |
| Hilanet password login | Supported |
| Federated SSO | Not supported |
| Other Hilanet tenants | Run `doctor`; not yet live verified |

The implementation is intended to be tenant-aware, but only one enterprise
tenant is proven end to end.

## Features

- Fetches reporting types from the configured Hilanet tenant.
- Supports exact single or multiple dates, up to 10 per preview.
- Resolves exact names and codes without an extra question.
- Asks before using a translation, abbreviation, or semantic match.
- Asks the user to choose when several server options are plausible.
- Handles tenant scheduled/default rows through Hilan's native controls.
- Requires preview and approval before every write.
- Verifies every attempted date with a fresh Hilanet browser read.
- Stores credentials in Windows Credential Manager.
- Reports committed, rejected, unknown, and not-attempted results per date.

Types that require hours, attachments, certificates, or other fields may still
require manual completion. The skill never guesses those values.

## Other installation options

Install for the current project:

```powershell
npx skills add asaf5767/hilanet-reporter-skill
```

### GitHub CLI

```powershell
gh skill install asaf5767/hilanet-reporter-skill attendance-reporter
```

### Manual installation

Copy the skill directory into the location used by your agent:

```text
skills/attendance-reporter
```

Common destinations:

```text
~/.claude/skills/attendance-reporter   # Claude Code
~/.agents/skills/attendance-reporter   # Copilot, Codex, Cursor, VS Code
```

Install the Python dependency:

```powershell
python -m pip install -r requirements.txt
```

Run this command from the installed `attendance-reporter` skill directory.

Start a new agent session or reload skills after installation.

## Usage

Ask naturally:

```text
Report vacation on 2026-08-02.
```

```text
I was sick on 2026-08-10 and 2026-08-11.
```

```text
Report miluim on 2026-09-03.
```

The workflow is always:

```text
request → server type lookup → date validation → preview
        → human approval → one write → fresh server proof
```

### Type matching

The tenant's dropdown is the source of truth.

| User phrase | Server options | Behavior |
|---|---|---|
| `vacation` | `vacation` | Exact match; preview directly |
| `miluim` | `reserve duty` | Semantic match; ask for confirmation |
| `sick` | sickness, child sick, partner sick, half-day variants | Ask the user to choose |
| unknown phrase | no matching option | Stop; never invent a code |

The skill refreshes the reporting vocabulary before every preview.

## Safety model

- Preview files are HMAC-signed with an independent random key stored in
  Windows Credential Manager.
- Each preview is bound to one Hilanet account, expires after 15 minutes, and
  is atomically single-use.
- Existing user attendance, locked dates, holidays, errors, future dates, and
  Friday/Saturday fail closed.
- Execution re-reads every date before the first write.
- Writes use one fresh headless Edge context and Hilan's native controls.
- Writes are never automatically retried after an uncertain result.
- Success requires a separate fresh browser read of the exact date and type.
- Tenant-provided names and messages are treated as untrusted data.

The human approval UI in the agent host is the trust boundary for submission.
Install the skill only in an agent host you trust.

See [SECURITY.md](SECURITY.md) for credential handling and vulnerability
reporting.

## Supported reporting options

The skill does not ship a hardcoded list. It discovers the options available to
the current employee:

```powershell
python scripts\attendance.py types
```

A typical tenant may expose vacation, sickness, family sickness, reserve duty,
work from home, company events, training, volunteering, mourning, and half-day
variants. The actual list always comes from the configured tenant.

To check that every current dropdown option can be selected without saving:

```powershell
python scripts\attendance.py probe-types --date YYYY-MM-DD
```

Use a confirmed blank/default date. The command selects and resets every option
but never clicks Save. This validates UI compatibility, not final server
acceptance.

## Troubleshooting

### `attendance skill is not configured`

Run `setup` from the installed skill directory.

### `account is temporarily locked`

Stop retrying. Wait several minutes, verify the password manually in Hilanet,
then run `doctor` once.

### CAPTCHA is required

Complete the CAPTCHA manually in Hilanet, then rerun the command. The skill does
not bypass CAPTCHA or MFA.

### The date is reported as existing attendance

Open the date in Hilanet. The skill refuses to overwrite a real user report.
Employer schedule rows with default type `0` are handled separately.

### A reporting type is missing

Run `types`. If the type is absent from the actual writable dropdown, the skill
will not offer it even if another Hilanet endpoint mentions it.

### The operation result is unknown

Do not immediately retry. Check Hilanet manually or ask the agent to perform a
read-only proof for the exact date.

### Claude or another agent does not see the skill

Reload skills or start a new session. Confirm that `SKILL.md` exists under the
agent's configured skills directory.

## Architecture

```text
skills/
└── attendance-reporter/
    ├── SKILL.md                 agent workflow
    ├── requirements.txt        pinned runtime dependency
    ├── references/
    │   └── protocol.md          protocol evidence and limitations
    └── scripts/
        └── attendance.py        deterministic runner
tests/
└── test_attendance.py
```

`attendance.py` contains three deep paths behind one skill interface:

1. Authenticated read adapter for tenant types and calendar state.
2. Signed preview and approval state machine.
3. Account-checked Edge write adapter plus independent proof adapter.

The project does not depend on Shaon at runtime and does not copy or fork its
source. Public Shaon protocol notes and other browser-automation projects were
used as research evidence; links are recorded in
[`protocol.md`](skills/attendance-reporter/references/protocol.md).

## Development

Install dependencies:

```powershell
python -m pip install -r skills\attendance-reporter\requirements.txt
```

Run tests and static checks:

```powershell
python -m unittest discover -s tests -v
python -m py_compile skills\attendance-reporter\scripts\attendance.py
npx --yes skills@1.5.20 add . --list
```

Tests do not submit attendance. The Windows credential test creates and deletes
a temporary dummy credential.

## Scope and responsibility

This project is for automating your own single-user Hilanet account. It is not
payroll, tax, HR, legal, or employment-compliance advice.

You are responsible for following your employer's policies and reviewing every
submission. Hilan and Hilanet are names used only to identify compatibility.
This project is independent and is not affiliated with or endorsed by Hilan.

## License

[MIT](LICENSE)
