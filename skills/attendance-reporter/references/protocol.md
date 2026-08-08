# Protocol provenance

The Hilanet surface is private and undocumented. This implementation was
written independently from protocol facts researched against Shaon commit
`9752f5306191ef1f02c7d4ebecad046b114ee774`; no Shaon code or runtime is used.

- Login and session signals:
  https://github.com/avivsinai/shaon/blob/9752f5306191ef1f02c7d4ebecad046b114ee774/PROTOCOL.md#L13-L35
- Employee identity fields:
  https://github.com/avivsinai/shaon/blob/9752f5306191ef1f02c7d4ebecad046b114ee774/PROTOCOL.md#L52-L69
- Calendar navigation and form replay:
  https://github.com/avivsinai/shaon/blob/9752f5306191ef1f02c7d4ebecad046b114ee774/PROTOCOL.md#L71-L112
- Tenant attendance-type discovery:
  https://github.com/avivsinai/shaon/blob/9752f5306191ef1f02c7d4ebecad046b114ee774/crates/provider-hilan/src/ontology.rs#L142-L210
- State-changing requests are not retried:
  https://github.com/avivsinai/shaon/blob/9752f5306191ef1f02c7d4ebecad046b114ee774/crates/provider-hilan/src/client.rs#L955-L1039

Federated SSO is not implemented because the researched protocol covers only
Hilanet password login. Cross-origin sign-in fails explicitly instead of
collecting or forwarding credentials.

The implementation discovers the tenant's reporting types and selects them
through Hilan's native controls. It never deletes or replaces an existing user
report. Types that require extra hours, attachments, or other fields may still
require manual completion.

## Observed tenant variations

Read-only verification against an enterprise tenant showed two compatibility
requirements:

- Calendar AJAX requests reject a custom command-line user-agent; the client
  sends browser-compatible Windows Edge headers.
- Selected dates may be exposed as `DD/MM` in the `cellOf_ReportDate_row_`
  calendar cell instead of Shaon's observed unpadded `RowDate` JSON. The client
  validates either representation against the requested year and date.

These observations affect parsing and browser fidelity only. They do not relax
the preview, existing-report, confirmation, or no-retry safety gates.

## Browser-native scheduled-row writes

At least one tenant rejects hand-built calendar POSTs for its blank scheduled
row as `Overlapping Report`. The write adapter therefore reuses the
authenticated in-memory cookies in headless Edge and lets Hilan's own
JavaScript perform the change:

1. select the exact absolute calendar day;
2. load Selected Days;
3. bind the unique matching `DD/MM` row;
4. require the current editable type to be blank/default code `0`;
5. select the tenant-derived vacation/sickness code;
6. click the native Save once and handle one recognized server dialog;
7. reload independently and prove the rendered type code.

Independent browser automation examples supporting this pattern:

- https://github.com/EyalMeschman/hilan-automation/blob/0f9341a5f97bd5e56e188d52c73a04439b15f145/src/hilan.py#L147-L195
- https://github.com/ronharel02/hilan-attendance/blob/d4fc0d72c191359c58d7634dd6afa50653602bde/hilan_attendance/browser/pages/calendar.py#L963-L1040
- https://github.com/ZivAlpertNV/hilan-automation/blob/027a9085e559e7a5576bb747dfd3bf721c7352f5/hilan_filler.py#L1100-L1780

The browser writer remains behind the same signed preview, exact confirmation,
account identity, single-use, and fresh proof gates.
