import json
import os
import sys
import tempfile
import unittest
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(
    0,
    str(ROOT / "skills" / "attendance-reporter" / "scripts"),
)

from attendance import (  # noqa: E402
    AttendanceService,
    AttendanceType,
    AppConfig,
    DaySnapshot,
    FakeClock,
    HilanProtocol,
    PreviewStore,
    SafetyError,
    WindowsCredentialStore,
    WriteReceipt,
    build_plan,
    format_attendance_types,
    parse_day_snapshot,
    server_text,
)


NOW = datetime(2026, 8, 7, 9, 0, tzinfo=timezone.utc)
VACATION = AttendanceType(code="V77", name="Vacation", localized_name="חופשה")


class FakeBackend:
    def __init__(
        self,
        days,
        *,
        write_results=None,
        account_id="tenant:test-user:employee-27",
        verified_type_code=None,
    ):
        self.days = dict(days)
        self.write_results = list(write_results or [])
        self.writes = []
        self.account_id = account_id
        self.verified_type_code = verified_type_code

    def account_context(self):
        return self.account_id

    def verify_type(self, target_date, attendance_type):
        if isinstance(self.verified_type_code, Exception):
            raise self.verified_type_code
        return self.verified_type_code == attendance_type.code

    def resolve_type(self, requested):
        if requested.casefold() not in {"vacation", "חופשה", "v77"}:
            raise SafetyError("unknown attendance type")
        return VACATION

    def inspect(self, target_date):
        return self.days[target_date]

    def submit_absence(
        self,
        target_date,
        attendance_type,
        comment,
        *,
        today=None,
    ):
        self.writes.append((target_date, attendance_type.code, comment))
        result = (
            self.write_results.pop(0)
            if self.write_results
            else WriteReceipt.committed()
        )
        if result.status == "committed":
            self.days[target_date] = DaySnapshot(
                date=target_date,
                editable=True,
                existing_type_code=attendance_type.code,
                existing_type_name=attendance_type.name,
                source="user",
            )
        return result


def empty_day(day):
    return DaySnapshot(date=day, editable=True)


class AttendanceServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = PreviewStore(
            Path(self.temp.name),
            signing_key=b"test-preview-signing-key",
        )
        self.clock = FakeClock(NOW)

    def service(self, backend):
        return AttendanceService(backend, self.store, self.clock)

    def test_previews_exact_august_vacation_dates(self):
        dates = [date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 6)]
        backend = FakeBackend({day: empty_day(day) for day in dates})

        preview = self.service(backend).preview(
            build_plan(
                ["2026-08-04", "2026-08-05", "2026-08-06"],
                "vacation",
            )
        )

        self.assertEqual(
            [item["date"] for item in preview["days"]],
            ["2026-08-04", "2026-08-05", "2026-08-06"],
        )
        self.assertEqual(preview["attendance_type"]["code"], "V77")
        self.assertEqual(preview["status"], "ready")
        self.assertEqual(
            [item["proposed"]["action"] for item in preview["days"]],
            ["report", "report", "report"],
        )
        self.assertTrue(self.store.path_for(preview["preview_id"]).exists())
        self.assertEqual(backend.writes, [])

    def test_same_local_day_is_not_future_when_utc_is_previous_day(self):
        target = date(2026, 8, 5)
        backend = FakeBackend({target: empty_day(target)})
        clock = FakeClock(
            datetime(2026, 8, 4, 22, 30, tzinfo=timezone.utc),
            local_date=target,
        )
        service = AttendanceService(backend, self.store, clock)

        preview = service.preview(
            build_plan([target.isoformat()], "vacation")
        )

        self.assertEqual(preview["status"], "ready")

    def test_plan_rejects_ambiguous_duplicate_and_large_inputs(self):
        with self.assertRaisesRegex(SafetyError, "ISO"):
            build_plan(["04/08/2026"], "vacation")
        with self.assertRaisesRegex(SafetyError, "duplicate"):
            build_plan(["2026-08-04", "2026-08-04"], "vacation")
        with self.assertRaisesRegex(SafetyError, "at most 10"):
            build_plan(
                [f"2026-08-{day:02d}" for day in range(1, 12)],
                "vacation",
            )

    def test_preview_fails_closed_for_unsafe_days(self):
        cases = {
            "weekend": (
                date(2026, 8, 7),
                empty_day(date(2026, 8, 7)),
                "weekend",
            ),
            "future": (
                date(2026, 8, 9),
                empty_day(date(2026, 8, 9)),
                "future",
            ),
            "locked": (
                date(2026, 8, 4),
                DaySnapshot(date=date(2026, 8, 4), editable=False),
                "locked",
            ),
            "holiday": (
                date(2026, 8, 4),
                DaySnapshot(
                    date=date(2026, 8, 4), editable=True, holiday=True
                ),
                "holiday",
            ),
            "error": (
                date(2026, 8, 4),
                DaySnapshot(
                    date=date(2026, 8, 4),
                    editable=True,
                    error="missing report",
                ),
                "error",
            ),
            "existing": (
                date(2026, 8, 4),
                DaySnapshot(
                    date=date(2026, 8, 4),
                    editable=True,
                    existing_type_code="OTHER",
                    existing_type_name="Sickness",
                    source="user",
                ),
                "existing",
            ),
            "system": (
                date(2026, 8, 4),
                DaySnapshot(
                    date=date(2026, 8, 4),
                    editable=True,
                    existing_type_code="V77",
                    existing_type_name="Vacation",
                    source="system",
                ),
                "existing",
            ),
        }

        for name, (day, snapshot, message) in cases.items():
            with self.subTest(name=name):
                backend = FakeBackend({day: snapshot})
                with self.assertRaisesRegex(SafetyError, message):
                    self.service(backend).preview(
                        build_plan([day.isoformat()], "vacation")
                    )
                self.assertEqual(backend.writes, [])

    def test_execute_requires_exact_confirmation_and_fresh_state(self):
        day = date(2026, 8, 4)
        backend = FakeBackend({day: empty_day(day)})
        service = self.service(backend)
        preview = service.preview(build_plan([day.isoformat()], "vacation"))

        with self.assertRaisesRegex(SafetyError, "SUBMIT"):
            service.execute(preview["preview_id"], "yes")
        self.assertEqual(backend.writes, [])

        backend.days[day] = DaySnapshot(
            date=day,
            editable=True,
            existing_type_code="S1",
            existing_type_name="Sickness",
            source="user",
        )
        with self.assertRaisesRegex(SafetyError, "changed"):
            service.execute(
                preview["preview_id"],
                f"SUBMIT {preview['preview_id']}",
            )
        self.assertEqual(backend.writes, [])

    def test_preview_cannot_execute_against_another_account(self):
        day = date(2026, 8, 4)
        first = FakeBackend(
            {day: empty_day(day)},
            account_id="tenant-a:user-a:employee-1",
        )
        preview = self.service(first).preview(
            build_plan([day.isoformat()], "vacation")
        )
        second = FakeBackend(
            {day: empty_day(day)},
            account_id="tenant-b:user-b:employee-2",
        )

        with self.assertRaisesRegex(SafetyError, "account changed"):
            self.service(second).execute(
                preview["preview_id"],
                f"SUBMIT {preview['preview_id']}",
            )
        self.assertEqual(second.writes, [])

    def test_execute_writes_only_approved_dates_and_reads_back(self):
        dates = [date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 6)]
        backend = FakeBackend({day: empty_day(day) for day in dates})
        service = self.service(backend)
        preview = service.preview(
            build_plan([day.isoformat() for day in dates], "vacation")
        )

        result = service.execute(
            preview["preview_id"],
            f"SUBMIT {preview['preview_id']}",
        )

        self.assertEqual(
            [item[0] for item in backend.writes],
            dates,
        )
        self.assertEqual(result["status"], "committed")
        self.assertEqual(
            [item["status"] for item in result["days"]],
            ["committed", "committed", "committed"],
        )
        with self.assertRaisesRegex(SafetyError, "consumed"):
            service.execute(
                preview["preview_id"],
                f"SUBMIT {preview['preview_id']}",
            )

    def test_execute_stops_after_partial_or_unknown_failure(self):
        dates = [date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 6)]
        backend = FakeBackend(
            {day: empty_day(day) for day in dates},
            write_results=[
                WriteReceipt.committed(),
                WriteReceipt.outcome_unknown("connection lost"),
            ],
        )
        service = self.service(backend)
        preview = service.preview(
            build_plan([day.isoformat() for day in dates], "vacation")
        )

        result = service.execute(
            preview["preview_id"],
            f"SUBMIT {preview['preview_id']}",
        )

        self.assertEqual([item[0] for item in backend.writes], dates[:2])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            [item["status"] for item in result["days"]],
            ["committed", "outcome_unknown", "not_attempted"],
        )

    def test_expired_preview_never_writes(self):
        day = date(2026, 8, 4)
        backend = FakeBackend({day: empty_day(day)})
        service = self.service(backend)
        preview = service.preview(build_plan([day.isoformat()], "vacation"))
        self.clock.advance(minutes=16)

        with self.assertRaisesRegex(SafetyError, "expired"):
            service.execute(
                preview["preview_id"],
                f"SUBMIT {preview['preview_id']}",
            )
        self.assertEqual(backend.writes, [])

    def test_preview_expiring_during_revalidation_never_writes(self):
        day = date(2026, 8, 4)

        class SlowRevalidationBackend(FakeBackend):
            def inspect(self, target_date):
                if self.writes == ["revalidating"]:
                    self.writes.clear()
                    self.clock.advance(minutes=1)
                return super().inspect(target_date)

        backend = SlowRevalidationBackend({day: empty_day(day)})
        backend.clock = self.clock
        service = self.service(backend)
        preview = service.preview(build_plan([day.isoformat()], "vacation"))
        self.clock.advance(minutes=14)
        backend.writes.append("revalidating")

        with self.assertRaisesRegex(SafetyError, "expired"):
            service.execute(
                preview["preview_id"],
                f"SUBMIT {preview['preview_id']}",
            )
        self.assertEqual(backend.writes, [])

    def test_tampered_preview_never_writes(self):
        day = date(2026, 8, 4)
        extra = date(2026, 8, 3)
        backend = FakeBackend({day: empty_day(day), extra: empty_day(extra)})
        service = self.service(backend)
        preview = service.preview(build_plan([day.isoformat()], "vacation"))
        path = self.store.path_for(preview["preview_id"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["days"].append(
            {
                "date": extra.isoformat(),
                "weekday": "Monday",
                "before": empty_day(extra).fingerprint(),
                "proposed": {
                    "type_code": "V77",
                    "type_name": "Vacation",
                    "comment": "",
                },
            }
        )
        payload["expires_at"] = "2099-01-01T00:00:00+00:00"
        path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(SafetyError, "integrity"):
            service.execute(
                preview["preview_id"],
                f"SUBMIT {preview['preview_id']}",
            )
        self.assertEqual(backend.writes, [])

    def test_readback_failure_consumes_preview_and_reports_unknown(self):
        day = date(2026, 8, 4)

        class ReadbackFailureBackend(FakeBackend):
            def inspect(self, target_date):
                if self.writes:
                    raise SafetyError("read-back timed out")
                return super().inspect(target_date)

        backend = ReadbackFailureBackend({day: empty_day(day)})
        service = self.service(backend)
        preview = service.preview(build_plan([day.isoformat()], "vacation"))

        result = service.execute(
            preview["preview_id"],
            f"SUBMIT {preview['preview_id']}",
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["days"][0]["status"], "outcome_unknown")
        with self.assertRaisesRegex(SafetyError, "consumed"):
            service.execute(
                preview["preview_id"],
                f"SUBMIT {preview['preview_id']}",
            )

    def test_submit_exception_still_attempts_readback(self):
        day = date(2026, 8, 4)

        class SubmitAuthenticationFailureBackend(FakeBackend):
            def __init__(self, days):
                super().__init__(days)
                self.inspect_count = 0

            def inspect(self, target_date):
                self.inspect_count += 1
                return super().inspect(target_date)

            def submit_absence(
                self,
                target_date,
                attendance_type,
                comment,
                *,
                today=None,
            ):
                self.writes.append(
                    (target_date, attendance_type.code, comment)
                )
                raise SafetyError("cross-origin sign-in is unsupported")

        backend = SubmitAuthenticationFailureBackend({day: empty_day(day)})
        service = self.service(backend)
        preview = service.preview(build_plan([day.isoformat()], "vacation"))

        result = service.execute(
            preview["preview_id"],
            f"SUBMIT {preview['preview_id']}",
        )

        self.assertEqual(backend.inspect_count, 3)
        self.assertEqual(result["days"][0]["status"], "outcome_unknown")

    def test_explicit_rejection_is_never_overridden_by_proof(self):
        day = date(2026, 8, 4)

        class WarningAfterCommitBackend(FakeBackend):
            def submit_absence(
                self,
                target_date,
                attendance_type,
                comment,
                *,
                today=None,
            ):
                self.writes.append(
                    (target_date, attendance_type.code, comment)
                )
                self.days[target_date] = DaySnapshot(
                    date=target_date,
                    editable=True,
                    existing_type_code=attendance_type.code,
                    existing_type_name=attendance_type.name,
                    source="user",
                )
                return WriteReceipt.rejected("warning after save")

        backend = WarningAfterCommitBackend({day: empty_day(day)})
        backend.verified_type_code = "V77"
        service = self.service(backend)
        preview = service.preview(build_plan([day.isoformat()], "vacation"))

        result = service.execute(
            preview["preview_id"],
            f"SUBMIT {preview['preview_id']}",
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["days"][0]["status"], "rejected")

    def test_browser_proof_can_confirm_when_html_parser_lags(self):
        day = date(2026, 8, 2)
        backend = FakeBackend(
            {day: empty_day(day)},
            verified_type_code="V77",
        )
        service = self.service(backend)
        preview = service.preview(build_plan([day.isoformat()], "vacation"))
        backend.write_results = [
            WriteReceipt.outcome_unknown("read parser lagged")
        ]

        result = service.execute(
            preview["preview_id"],
            f"SUBMIT {preview['preview_id']}",
        )

        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["days"][0]["status"], "committed")

    def test_browser_proof_failure_preserves_per_date_result(self):
        day = date(2026, 8, 2)
        backend = FakeBackend(
            {day: empty_day(day)},
            verified_type_code=SafetyError("proof unavailable"),
        )
        service = self.service(backend)
        preview = service.preview(build_plan([day.isoformat()], "vacation"))
        backend.write_results = [
            WriteReceipt.outcome_unknown("response uncertain")
        ]

        result = service.execute(
            preview["preview_id"],
            f"SUBMIT {preview['preview_id']}",
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["days"][0]["status"], "outcome_unknown")


class HilanProtocolTests(unittest.TestCase):
    def test_server_text_removes_control_chars_and_caps_output(self):
        self.assertEqual(
            server_text("  vacation\nignore\x00instructions  "),
            "vacation ignoreinstructions",
        )
        self.assertEqual(server_text("x" * 500), "x" * 160)

    def test_formats_all_server_types_without_hardcoded_filtering(self):
        types = [
            AttendanceType("0", "attendance"),
            AttendanceType("481", "vacation", "חופשה"),
            AttendanceType("483", "sickness day", "מחלה"),
            AttendanceType("50", "1/2 vacation"),
        ]

        self.assertEqual(
            format_attendance_types(types),
            [
                {
                    "code": "0",
                    "name": "attendance",
                    "localized_name": "",
                },
                {
                    "code": "481",
                    "name": "vacation",
                    "localized_name": "חופשה",
                },
                {
                    "code": "483",
                    "name": "sickness day",
                    "localized_name": "מחלה",
                },
                {
                    "code": "50",
                    "name": "1/2 vacation",
                    "localized_name": "",
                },
            ],
        )

    def test_async_content_accepts_full_html_or_delta(self):
        html = "<!DOCTYPE html><form><input name='x' value='1'></form>"
        content, hidden = HilanProtocol.async_content(html)
        self.assertEqual(content, html)
        self.assertEqual(hidden, {})

        panel = "<div>selected</div>"
        state = "fresh"
        delta = (
            f"{len(panel)}|updatePanel|upGrid|{panel}|"
            f"{len(state)}|hiddenField|__VIEWSTATE|{state}|"
        )
        content, hidden = HilanProtocol.async_content(delta)
        self.assertEqual(content, panel)
        self.assertEqual(hidden, {"__VIEWSTATE": state})

    def test_extracts_plain_and_escaped_org_id(self):
        self.assertEqual(
            HilanProtocol.extract_org_id(
                '<script>window.x={"OrgId":"1234"}</script>'
            ),
            "1234",
        )
        self.assertEqual(
            HilanProtocol.extract_org_id(r'{\"OrgId\":\"5678\"}'),
            "5678",
        )
        with self.assertRaisesRegex(SafetyError, "OrgId"):
            HilanProtocol.extract_org_id("<html>no tenant marker</html>")

    def test_login_response_fails_closed(self):
        HilanProtocol.parse_login_response(
            '{"IsFail":false,"IsShowCaptcha":false}'
        )
        with self.assertRaisesRegex(SafetyError, "CAPTCHA"):
            HilanProtocol.parse_login_response(
                '{"IsFail":true,"IsShowCaptcha":true}'
            )
        with self.assertRaisesRegex(SafetyError, "temporarily locked"):
            HilanProtocol.parse_login_response('{"IsFail":true,"Code":18}')
        with self.assertRaisesRegex(SafetyError, "password"):
            HilanProtocol.parse_login_response('{"IsFail":true,"Code":6}')
        with self.assertRaisesRegex(SafetyError, "IsFail"):
            HilanProtocol.parse_login_response('{"Code":0}')

    def test_parses_character_counted_delta_with_hebrew(self):
        panel = '<div>חופשה</div>'
        hidden = "fresh-state"
        payload = (
            f"{len(panel)}|updatePanel|upGrid|{panel}|"
            f"{len(hidden)}|hiddenField|__VIEWSTATE|{hidden}|"
        )

        delta = HilanProtocol.parse_delta(payload)

        self.assertEqual(delta.update_panels["upGrid"], panel)
        self.assertEqual(delta.hidden_fields["__VIEWSTATE"], hidden)

    def test_resolves_vacation_from_tenant_data_not_fixed_code(self):
        calendar = """
        <select name="x_Symbol.SymbolId_y">
          <option value="">Choose</option>
          <option value="V77">V77 - Vacation</option>
        </select>
        """
        absences = json.dumps(
            {
                "Symbols": [
                    {
                        "Id": "V77",
                        "Name": "חופשה",
                        "DisplayName": "V77 - חופשה",
                    }
                ]
            }
        )

        types = HilanProtocol.parse_attendance_types(calendar, absences)
        resolved = HilanProtocol.resolve_type(types, "vacation")

        self.assertEqual(resolved.code, "V77")
        self.assertNotEqual(resolved.code, "481")

    def test_ignores_api_only_types_not_in_writable_dropdown(self):
        calendar = """
        <select name="x_Symbol.SymbolId_y">
          <option value="4">child sick</option>
        </select>
        """
        absences = json.dumps(
            {
                "Symbols": [
                    {"Id": "4", "Name": "child sick"},
                    {"Id": "484", "Name": "child sick"},
                ]
            }
        )

        types = HilanProtocol.parse_attendance_types(calendar, absences)

        self.assertEqual([item.code for item in types], ["4"])

    def test_parses_blank_existing_and_locked_selected_days(self):
        target = date(2026, 8, 4)
        base = """
        <td Days="9712">
          <tr class="dayImageNumberContainer"><td class="dTS">4</td></tr>
        </td>
        """
        blank = base + """
        <script>{"RowDate":"2026-8-4"}</script>
        <tr id="x_EmployeeReports_row_0">
          <select>
            <option value="" selected>Choose</option>
            <option value="V77">Vacation</option>
          </select>
        </tr>
        <input name="ctl00$mp$RG_Days_EMPID_2026_08$btnSave">
        """
        existing = blank.replace(
            '<option value="" selected>Choose</option>',
            '<option value="">Choose</option>',
        ).replace(
            '<option value="V77">Vacation</option>',
            '<option value="V77" selected>Vacation</option>',
        )
        locked = blank.replace(
            '<input name="ctl00$mp$RG_Days_EMPID_2026_08$btnSave">',
            (
                '<input name="ctl00$mp$RG_Days_EMPID_2026_08$btnSave" '
                'disabled="disabled">'
            ),
        )

        blank_state = parse_day_snapshot(target, blank)
        existing_state = parse_day_snapshot(target, existing)
        locked_state = parse_day_snapshot(target, locked)

        self.assertTrue(blank_state.editable)
        self.assertEqual(blank_state.source, "unreported")
        self.assertEqual(blank_state.existing_type_code, "")
        self.assertEqual(existing_state.existing_type_code, "V77")
        self.assertEqual(existing_state.source, "user")
        self.assertFalse(locked_state.editable)

    def test_visual_day_uses_absolute_index_not_duplicate_day_number(self):
        target = date(2026, 8, 30)
        html = """
        <td Days="9707">
          <tr class="dayImageNumberContainer"><td class="dTS">30</td></tr>
          <span class="calendarIcon fh-error"></span>
        </td>
        <td Days="9738">
          <tr class="dayImageNumberContainer"><td class="dTS">30</td></tr>
        </td>
        <script>{"RowDate":"2026-8-30"}</script>
        <tr id="x_EmployeeReports_row_0">
          <select><option value="" selected>Choose</option></select>
        </tr>
        <input name="ctl00$mp$RG_Days_EMPID_2026_08$btnSave">
        """

        snapshot = parse_day_snapshot(target, html)

        self.assertTrue(snapshot.known)
        self.assertEqual(snapshot.error, "")

    def test_selected_day_parser_fails_closed_on_wrong_or_ambiguous_row(self):
        target = date(2026, 8, 4)
        wrong = """
        <script>{"RowDate":"2026-8-5"}</script>
        <input name="ctl00$mp$RG_Days_EMPID_2026_08$btnSave">
        """
        ambiguous = """
        <script>{"RowDate":"2026-8-4"}</script>
        <tr id="x_EmployeeReports_row_0">
          <select>
            <option value="0" selected>attendance</option>
            <option value="V77">Vacation</option>
          </select>
          <input name="x_ManualEntry_EmployeeReports" value="">
          <input name="x_ManualExit_EmployeeReports" value="">
        </tr>
        <input name="ctl00$mp$RG_Days_EMPID_2026_08$btnSave">
        """

        with self.assertRaisesRegex(SafetyError, "different date"):
            parse_day_snapshot(target, wrong)
        self.assertFalse(parse_day_snapshot(target, ambiguous).known)

    def test_selected_day_parser_requires_visual_state(self):
        target = date(2026, 8, 4)
        panel_only = """
        <script>{"RowDate":"2026-8-4"}</script>
        <tr id="x_EmployeeReports_row_0">
          <select><option value="" selected>Choose</option></select>
        </tr>
        <input name="ctl00$mp$RG_Days_EMPID_2026_08$btnSave">
        """

        snapshot = parse_day_snapshot(target, panel_only)

        self.assertFalse(snapshot.known)
        self.assertIn(
            "unknown",
            snapshot.block_reason(date(2026, 8, 7)),
        )

    def test_accepts_report_date_cell_without_rowdata(self):
        target = date(2026, 8, 4)
        html = """
        <td Days="9712">
          <tr class="dayImageNumberContainer"><td class="dTS">4</td></tr>
        </td>
        <tr id="ctl00_mp_RG_Days_EMPID_2026_08_row_0">
          <td id="ctl00_mp_RG_Days_EMPID_2026_08_cellOf_ReportDate_row_0"
              ov="04/08 יום ג">04/08</td>
          <td>
            <tr id="x_EmployeeReports_row_0">
              <select><option value="" selected>Choose</option></select>
            </tr>
          </td>
        </tr>
        <input name="ctl00$mp$RG_Days_EMPID_2026_08$btnSave">
        """

        snapshot = parse_day_snapshot(target, html)

        self.assertTrue(snapshot.known)
        self.assertEqual(snapshot.date, target)

        with self.assertRaisesRegex(SafetyError, "different date"):
            parse_day_snapshot(
                target,
                html.replace('ov="04/08', 'ov="05/08').replace(
                    ">04/08<",
                    ">05/08<",
                ),
            )

    def test_unselected_dropdown_still_blocks_visual_existing_report(self):
        target = date(2026, 8, 4)
        html = """
        <td class="calendarAbcenseDay" Days="9712" title="Vacation">
          <tr class="dayImageNumberContainer"><td class="dTS">4</td></tr>
        </td>
        <span>RowData</span>
        <tr id="x_EmployeeReports_row_0_1">
          <select name="x_Symbol_EmployeeReports_row_0_1">
            <option value="0" selected>attendance</option>
          </select>
          <input name="x_ManualEntry_EmployeeReports_row_0_1" value="00:00">
          <input name="x_ManualExit_EmployeeReports_row_0_1" value="08:30">
        </tr>
        <tr id="ctl00_mp_RG_Days_EMPID_2026_08_row_0">
          <td id="ctl00_mp_RG_Days_EMPID_2026_08_cellOf_ReportDate_row_0"
              ov="04/08 יום ג">04/08</td>
          <td>
            <tr id="x_EmployeeReports_row_0">
              <select name="x_Symbol_EmployeeReports_row_0_0">
                <option value="0" selected>attendance</option>
                <option value="V77">Vacation</option>
              </select>
              <input name="x_ManualEntry_EmployeeReports_row_0_0" value="00:00">
              <input name="x_ManualExit_EmployeeReports_row_0_0" value="08:30">
            </tr>
          </td>
        </tr>
        <input name="ctl00$mp$RG_Days_EMPID_2026_08$btnSave">
        """

        snapshot = parse_day_snapshot(target, html)

        self.assertTrue(snapshot.known)
        self.assertEqual(snapshot.source, "user")
        self.assertIn(
            "existing",
            snapshot.block_reason(date(2026, 8, 7)),
        )

    def test_scheduled_hours_are_not_existing_attendance(self):
        target = date(2026, 8, 4)
        html = """
        <td class="calendarAbcenseDay" Days="9712">
          <tr class="dayImageNumberContainer"><td class="dTS">4</td></tr>
          <tr><td class="calendarMessageCell"><div>8:30</div></td></tr>
        </td>
        <tr id="x_EmployeeReports_row_0_1">
          <select name="x_Symbol_EmployeeReports_row_0_1">
            <option value="0" selected>attendance</option>
          </select>
          <input name="x_ManualEntry_EmployeeReports_row_0_1" value="00:00">
          <input name="x_ManualExit_EmployeeReports_row_0_1" value="08:30">
        </tr>
        <tr id="ctl00_mp_RG_Days_EMPID_2026_08_row_0">
          <td id="ctl00_mp_RG_Days_EMPID_2026_08_cellOf_ReportDate_row_0"
              ov="04/08 יום ג">04/08</td>
          <td>
            <tr id="x_EmployeeReports_row_0">
              <select name="x_Symbol_EmployeeReports_row_0_0">
                <option value="0" selected>attendance</option>
                <option value="V77">Vacation</option>
              </select>
              <input name="x_ManualEntry_EmployeeReports_row_0_0" value="00:00">
              <input name="x_ManualExit_EmployeeReports_row_0_0" value="08:30">
            </tr>
          </td>
        </tr>
        <input name="ctl00$mp$RG_Days_EMPID_2026_08$btnSave">
        """

        snapshot = parse_day_snapshot(target, html)

        self.assertTrue(snapshot.known)
        self.assertEqual(snapshot.source, "unreported")
        self.assertEqual(snapshot.existing_type_code, "")
        self.assertIsNone(snapshot.block_reason(date(2026, 8, 7)))

        reported = html.replace(
            'name="x_ManualEntry_EmployeeReports_row_0_0" value="00:00"',
            'name="x_ManualEntry_EmployeeReports_row_0_0" value="09:00"',
        ).replace(
            'name="x_ManualExit_EmployeeReports_row_0_0" value="08:30"',
            'name="x_ManualExit_EmployeeReports_row_0_0" value="18:00"',
        )
        reported_snapshot = parse_day_snapshot(target, reported)
        self.assertIn(
            "existing",
            reported_snapshot.block_reason(date(2026, 8, 7)),
        )

    def test_explicit_default_code_with_blank_times_is_unreported(self):
        target = date(2026, 7, 16)
        html = """
        <td class="calendarAbcenseDay" Days="9693">
          <tr class="dayImageNumberContainer"><td class="dTS">16</td></tr>
        </td>
        <tr id="ctl00_mp_RG_Days_EMPID_2026_07_row_0">
          <td id="ctl00_mp_RG_Days_EMPID_2026_07_cellOf_ReportDate_row_0"
              ov="16/07 יום ה">16/07</td>
          <td>
            <tr id="x_EmployeeReports_row_0">
              <select name="x_Symbol_EmployeeReports_row_0_0">
                <option value="0" selected>attendance</option>
                <option value="483">sickness day</option>
              </select>
              <input name="x_ManualEntry_EmployeeReports_row_0_0" value="">
              <input name="x_ManualExit_EmployeeReports_row_0_0" value="">
            </tr>
          </td>
        </tr>
        <input name="ctl00$mp$RG_Days_EMPID_2026_07$btnSave">
        """

        snapshot = parse_day_snapshot(target, html)

        self.assertTrue(snapshot.known)
        self.assertEqual(snapshot.source, "unreported")
        self.assertEqual(snapshot.existing_type_code, "")
        self.assertIsNone(snapshot.block_reason(date(2026, 8, 8)))

    def test_read_only_form_drops_all_write_triggers(self):
        fields = {
            "__VIEWSTATE": "state",
            "ctl00$mp$currentMonth": "01/08/2026",
            "ctl00$mp$RG_Days_EMPID_2026_08$btnSave": "שמירה",
            "x_reportsGrid_action": "DELETE_ROW",
        }

        safe = HilanProtocol.read_only_form(fields)

        self.assertEqual(
            safe,
            {
                "__VIEWSTATE": "state",
                "ctl00$mp$currentMonth": "01/08/2026",
            },
        )

@unittest.skipUnless(os.name == "nt", "Windows Credential Manager test")
class WindowsCredentialStoreTests(unittest.TestCase):
    def test_round_trips_password_without_project_storage(self):
        config = AppConfig(
            tenant=f"attendance-test-{uuid.uuid4().hex}",
            username="test-user",
        )
        store = WindowsCredentialStore()
        try:
            store.write(config, "temporary-test-password")
            self.assertEqual(
                store.read(config),
                "temporary-test-password",
            )
        finally:
            store.delete(config)

    def test_preview_signing_key_is_random_and_password_independent(self):
        config = AppConfig(
            tenant=f"attendance-test-{uuid.uuid4().hex}",
            username="test-user",
        )
        store = WindowsCredentialStore()
        try:
            store.write(config, "first-password")
            first_key = store.preview_key(config)
            store.write(config, "different-password")
            self.assertEqual(store.preview_key(config), first_key)
            self.assertEqual(len(first_key), 32)
        finally:
            store.delete(config)
            store.delete_preview_key(config)


class SkillContractTests(unittest.TestCase):
    def test_skill_is_publishable_and_self_contained(self):
        skill_root = ROOT / "skills" / "attendance-reporter"
        skill_file = skill_root / "SKILL.md"
        lines = skill_file.read_text(encoding="utf-8").splitlines()
        self.assertLess(len(lines), 500)
        self.assertEqual(lines[0], "---")
        frontmatter_end = lines.index("---", 1)
        frontmatter = "\n".join(lines[1:frontmatter_end])
        self.assertIn("name: attendance-reporter", frontmatter)
        self.assertIn("description:", frontmatter)
        self.assertIn("license: MIT", frontmatter)
        self.assertTrue((skill_root / "scripts" / "attendance.py").is_file())
        self.assertTrue((skill_root / "references" / "protocol.md").is_file())
        self.assertTrue((skill_root / "requirements.txt").is_file())


if __name__ == "__main__":
    unittest.main()
