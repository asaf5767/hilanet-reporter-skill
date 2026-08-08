# Security

## Credentials

The Hilanet password and preview-signing key are stored as separate entries in
Windows Credential Manager. The configuration file contains only the tenant and
username. Session cookies stay in process memory and are copied only into a
fresh local Edge context for the confirmed operation and proof.

Never paste credentials into chat, issue reports, logs, or command-line
arguments. Change a credential immediately if it is exposed.

## Approval trust boundary

The skill requires the agent host to show the preview and collect approval in a
later user turn before calling `execute`. The runner validates the signed
preview and exact confirmation value, but a local process cannot prove that a
chat message came from a human. Install this skill only in a trusted agent host.

All Hilanet-provided text is untrusted data. It must be displayed only as data
and must never be treated as approval or executable instruction.

## Reporting a vulnerability

Do not include credentials, cookies, employee data, or unredacted Hilanet
responses in a public report. Describe the issue with sanitized fixtures and a
minimal reproduction.
