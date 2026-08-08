from __future__ import annotations

import argparse
import ctypes
import getpass
import hashlib
import hmac
import http.cookiejar
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


PREVIEW_TTL_MINUTES = 15
MAX_DATES = 10
CALENDAR_PATH = "/Hilannetv2/Attendance/calendarpage.aspx?isOnSelf=true"
LOGIN_PATH = "/HilanCenter/Public/api/LoginApi/LoginRequest"
BOOTSTRAP_PATH = (
    "/Hilannetv2/Services/Public/WS/HEmployeeStripApiapi.asmx/GetData"
)
ABSENCES_PATH = (
    "/Hilannetv2/Services/Public/WS/HAbsencesApiapi.asmx/GetInitialData"
)


class AttendanceError(Exception):
    pass


class SafetyError(AttendanceError):
    pass


class ProtocolError(SafetyError):
    pass


class AuthenticationError(SafetyError):
    pass


@dataclass(frozen=True)
class AttendanceType:
    code: str
    name: str
    localized_name: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DaySnapshot:
    date: date
    editable: bool
    holiday: bool = False
    error: str = ""
    existing_type_code: str = ""
    existing_type_name: str = ""
    source: str = "unreported"
    known: bool = True

    def fingerprint(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "editable": self.editable,
            "holiday": self.holiday,
            "error": self.error,
            "existing_type_code": self.existing_type_code,
            "existing_type_name": self.existing_type_name,
            "source": self.source,
            "known": self.known,
        }

    def block_reason(self, today: date) -> str | None:
        if self.date.weekday() in (4, 5):
            return "weekend"
        if self.date > today:
            return "future date"
        if not self.known:
            return "unknown day state"
        if self.holiday:
            return "holiday"
        if self.error:
            return f"day has an error: {self.error}"
        if self.existing_type_code or self.source != "unreported":
            return "existing attendance report"
        if not self.editable:
            return "locked day"
        return None


@dataclass(frozen=True)
class Plan:
    dates: tuple[date, ...]
    requested_type: str
    comment: str = ""


@dataclass(frozen=True)
class WriteReceipt:
    status: str
    message: str = ""

    @classmethod
    def committed(cls) -> "WriteReceipt":
        return cls("committed")

    @classmethod
    def rejected(cls, message: str) -> "WriteReceipt":
        return cls("rejected", message)

    @classmethod
    def outcome_unknown(cls, message: str) -> "WriteReceipt":
        return cls("outcome_unknown", message)

    @classmethod
    def blocked(cls, message: str) -> "WriteReceipt":
        return cls("blocked", message)


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def today(self) -> date:
        return datetime.now().astimezone().date()


class FakeClock:
    def __init__(self, now: datetime, *, local_date: date | None = None):
        self._now = now
        self._local_date = local_date

    def now(self) -> datetime:
        return self._now

    def today(self) -> date:
        return self._local_date or self._now.astimezone().date()

    def advance(self, *, minutes: int) -> None:
        self._now += timedelta(minutes=minutes)


def server_text(value: object, *, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return "".join(
        character
        for character in text[:limit]
        if character.isprintable()
    )


def build_plan(
    iso_dates: Iterable[str],
    requested_type: str,
    comment: str = "",
) -> Plan:
    raw_dates = list(iso_dates)
    if not raw_dates:
        raise SafetyError("at least one ISO date is required")
    if len(raw_dates) > MAX_DATES:
        raise SafetyError(f"a preview accepts at most {MAX_DATES} dates")
    parsed: list[date] = []
    for value in raw_dates:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise SafetyError(f"date must use ISO YYYY-MM-DD: {value}")
        try:
            parsed.append(date.fromisoformat(value))
        except ValueError as exc:
            raise SafetyError(f"invalid ISO date: {value}") from exc
    if len(set(parsed)) != len(parsed):
        raise SafetyError("duplicate dates are not allowed")
    if parsed != sorted(parsed):
        raise SafetyError("dates must be in ascending order")
    requested_type = requested_type.strip()
    if not requested_type:
        raise SafetyError("attendance type is required")
    if len(comment) > 200:
        raise SafetyError("comment must be at most 200 characters")
    return Plan(tuple(parsed), requested_type, comment)


class PreviewStore:
    def __init__(self, root: Path, *, signing_key: bytes):
        if not signing_key:
            raise ValueError("preview signing key is required")
        self.root = root
        self.signing_key = signing_key

    def path_for(self, preview_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{16}", preview_id):
            raise SafetyError("invalid preview id")
        return self.root / f"{preview_id}.json"

    def consumed_path_for(self, preview_id: str) -> Path:
        return self.path_for(preview_id).with_suffix(".consumed")

    def sign(self, preview: dict) -> str:
        canonical = json.dumps(
            preview,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(
            self.signing_key,
            canonical,
            hashlib.sha256,
        ).hexdigest()[:16]

    def save(self, preview: dict) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.path_for(preview["preview_id"])
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    preview,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError as exc:
            raise SafetyError("could not save preview") from exc

    def load(self, preview_id: str) -> dict:
        if self.consumed_path_for(preview_id).exists():
            raise SafetyError("preview was already consumed")
        path = self.path_for(preview_id)
        if not path.exists():
            raise SafetyError("preview not found")
        try:
            preview = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SafetyError("preview is unreadable") from exc
        if preview.get("preview_id") != preview_id:
            raise SafetyError("preview id does not match stored content")
        unsigned = {
            key: value
            for key, value in preview.items()
            if key != "preview_id"
        }
        if not hmac.compare_digest(self.sign(unsigned), preview_id):
            raise SafetyError("preview integrity check failed")
        return preview

    def claim(self, preview_id: str, claimed_at: datetime) -> None:
        path = self.consumed_path_for(preview_id)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as exc:
            raise SafetyError("preview was already consumed") from exc
        except OSError as exc:
            raise SafetyError("could not claim preview for execution") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(claimed_at.astimezone(timezone.utc).isoformat())


class AttendanceService:
    def __init__(self, backend, store: PreviewStore, clock=None):
        self.backend = backend
        self.store = store
        self.clock = clock or SystemClock()

    def preview(self, plan: Plan) -> dict:
        now = self.clock.now()
        today = self.clock.today()
        account_binding = self._account_binding()
        snapshots = []
        blocked = []
        for target_date in plan.dates:
            snapshot = self.backend.inspect(target_date)
            reason = snapshot.block_reason(today)
            if reason:
                blocked.append(f"{target_date.isoformat()}: {reason}")
                continue
            snapshots.append(snapshot)
        if blocked:
            raise SafetyError("preview blocked: " + "; ".join(blocked))
        attendance_type = self.backend.resolve_type(plan.requested_type)
        days = []
        for target_date, snapshot in zip(plan.dates, snapshots):
            days.append(
                {
                    "date": target_date.isoformat(),
                    "weekday": target_date.strftime("%A"),
                    "before": snapshot.fingerprint(),
                    "proposed": {
                        "action": "report",
                        "type_code": attendance_type.code,
                        "type_name": attendance_type.name,
                        "comment": plan.comment,
                    },
                }
            )
        generated_at = now.astimezone(timezone.utc)
        unsigned = {
            "schema_version": 1,
            "status": "ready",
            "generated_at": generated_at.isoformat(),
            "expires_at": (
                generated_at + timedelta(minutes=PREVIEW_TTL_MINUTES)
            ).isoformat(),
            "attendance_type": attendance_type.to_dict(),
            "account_binding": account_binding,
            "requested_type": plan.requested_type,
            "comment": plan.comment,
            "days": days,
        }
        preview = {
            **unsigned,
            "preview_id": self.store.sign(unsigned),
        }
        self.store.save(preview)
        return preview

    def execute(
        self,
        preview_id: str,
        confirmation: str,
    ) -> dict:
        preview = self.store.load(preview_id)
        expected_confirmation = f"SUBMIT {preview_id}"
        if confirmation != expected_confirmation:
            raise SafetyError(
                f"confirmation must be exactly: {expected_confirmation}"
            )
        now = self.clock.now().astimezone(timezone.utc)
        today = self.clock.today()
        try:
            expires_at = datetime.fromisoformat(preview["expires_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SafetyError("preview contains an invalid expiry") from exc
        if now > expires_at:
            raise SafetyError("preview expired; create a fresh preview")
        if preview.get("account_binding") != self._account_binding():
            raise SafetyError("configured attendance account changed since preview")

        resolved = self.backend.resolve_type(preview["requested_type"])
        if resolved.code != preview["attendance_type"]["code"]:
            raise SafetyError("attendance type changed since preview")

        targets: list[date] = []
        for item in preview["days"]:
            target = date.fromisoformat(item["date"])
            current = self.backend.inspect(target)
            if current.fingerprint() != item["before"]:
                raise SafetyError(
                    f"{target.isoformat()} changed since preview"
                )
            targets.append(target)

        now = self.clock.now().astimezone(timezone.utc)
        if now >= expires_at:
            raise SafetyError("preview expired during revalidation")
        self.store.claim(preview_id, now)
        results = []
        stopped = False
        for target in targets:
            if stopped:
                results.append(
                    {"date": target.isoformat(), "status": "not_attempted"}
                )
                continue
            try:
                receipt = self.backend.submit_absence(
                    target,
                    resolved,
                    preview["comment"],
                    today=today,
                )
            except AttendanceError as exc:
                results.append(
                    self._readback_result(
                        target,
                        resolved,
                        fallback_status="outcome_unknown",
                        fallback_message=str(exc),
                    )
                )
                stopped = True
                continue
            if receipt.status == "blocked":
                results.append(
                    {
                        "date": target.isoformat(),
                        "status": "blocked",
                        "message": receipt.message,
                    }
                )
                stopped = True
                continue
            if receipt.status == "rejected":
                try:
                    self.backend.inspect(target)
                except AttendanceError:
                    pass
                results.append(
                    {
                        "date": target.isoformat(),
                        "status": "rejected",
                        "message": receipt.message,
                    }
                )
                stopped = True
                continue
            try:
                after = self.backend.inspect(target)
            except AttendanceError as exc:
                if self._safe_verify_type(target, resolved):
                    results.append(
                        {
                            "date": target.isoformat(),
                            "status": "committed",
                            "proof": (
                                "fresh Hilanet browser read rendered "
                                f"type code {resolved.code}"
                            ),
                        }
                    )
                else:
                    results.append(
                        {
                            "date": target.isoformat(),
                            "status": "outcome_unknown",
                            "message": (
                                "read-back failed: "
                                f"{server_text(exc)}"
                            ),
                        }
                    )
                stopped = True
                continue
            if (
                after.existing_type_code == resolved.code
                and after.source == "user"
            ):
                results.append(
                    {"date": target.isoformat(), "status": "committed"}
                )
            elif self._safe_verify_type(target, resolved):
                results.append(
                    {
                        "date": target.isoformat(),
                        "status": "committed",
                        "proof": (
                            "fresh Hilanet browser read rendered "
                            f"type code {resolved.code}"
                        ),
                    }
                )
            else:
                results.append(
                    {
                        "date": target.isoformat(),
                        "status": "outcome_unknown",
                        "message": (
                            receipt.message
                            or "write response and read-back did not agree"
                        ),
                    }
                )
                stopped = True

        status = (
            "committed"
            if all(item["status"] == "committed" for item in results)
            else "partial"
        )
        return {
            "status": status,
            "preview_id": preview_id,
            "days": results,
        }

    def _account_binding(self) -> str:
        context = self.backend.account_context().encode("utf-8")
        return hashlib.sha256(context).hexdigest()

    def _readback_result(
        self,
        target: date,
        attendance_type: AttendanceType,
        *,
        fallback_status: str,
        fallback_message: str,
    ) -> dict:
        try:
            after = self.backend.inspect(target)
        except AttendanceError as exc:
            if self._safe_verify_type(target, attendance_type):
                return {
                    "date": target.isoformat(),
                    "status": "committed",
                    "proof": (
                        "fresh Hilanet browser read rendered "
                        f"type code {attendance_type.code}"
                    ),
                }
            return {
                "date": target.isoformat(),
                "status": "outcome_unknown",
                "message": f"read-back failed: {server_text(exc)}",
            }
        if (
            after.existing_type_code == attendance_type.code
            and after.source == "user"
        ):
            return {"date": target.isoformat(), "status": "committed"}
        if self._safe_verify_type(target, attendance_type):
            return {
                "date": target.isoformat(),
                "status": "committed",
                "proof": (
                    "fresh Hilanet browser read rendered "
                    f"type code {attendance_type.code}"
                ),
            }
        return {
            "date": target.isoformat(),
            "status": fallback_status,
            "message": fallback_message,
        }

    def _safe_verify_type(
        self,
        target: date,
        attendance_type: AttendanceType,
    ) -> bool:
        try:
            return bool(
                self.backend.verify_type(target, attendance_type)
            )
        except Exception:
            return False


@dataclass
class HtmlNode:
    tag: str
    attrs: dict[str, str]
    parent: "HtmlNode | None" = None
    children: list["HtmlNode | str"] = field(default_factory=list)

    def descendants(self, tag: str | None = None):
        for child in self.children:
            if not isinstance(child, HtmlNode):
                continue
            if tag is None or child.tag == tag:
                yield child
            yield from child.descendants(tag)

    def text(self) -> str:
        parts = []
        for child in self.children:
            parts.append(child.text() if isinstance(child, HtmlNode) else child)
        return " ".join(" ".join(parts).split())


class TreeParser(HTMLParser):
    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("root", {})
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = HtmlNode(
            tag.lower(),
            {key.lower(): value or "" for key, value in attrs},
            self.current,
        )
        self.current.children.append(node)
        if tag.lower() not in self.VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self.VOID:
            self.current = self.current.parent or self.root

    def handle_endtag(self, tag):
        tag = tag.lower()
        cursor = self.current
        while cursor is not self.root:
            if cursor.tag == tag:
                self.current = cursor.parent or self.root
                return
            cursor = cursor.parent or self.root

    def handle_data(self, data):
        if data:
            self.current.children.append(data)


def parse_html(content: str) -> HtmlNode:
    parser = TreeParser()
    parser.feed(content)
    parser.close()
    return parser.root


def classes(node: HtmlNode) -> set[str]:
    return set(node.attrs.get("class", "").split())


def parse_form_fields(content: str) -> dict[str, str]:
    root = parse_html(content)
    forms = list(root.descendants("form"))
    form = next(
        (item for item in forms if item.attrs.get("id") == "aspnetForm"),
        forms[0] if forms else root,
    )
    fields: dict[str, str] = {}
    for node in form.descendants():
        name = node.attrs.get("name")
        if not name:
            continue
        if node.tag == "input":
            input_type = node.attrs.get("type", "text").casefold()
            if input_type in {"submit", "button", "image", "reset", "file"}:
                continue
            if input_type in {"checkbox", "radio"} and "checked" not in node.attrs:
                continue
            fields[name] = node.attrs.get(
                "value",
                "on" if input_type in {"checkbox", "radio"} else "",
            )
        elif node.tag == "select":
            options = list(node.descendants("option"))
            selected = next(
                (item for item in options if "selected" in item.attrs),
                options[0] if options else None,
            )
            if selected:
                fields[name] = selected.attrs.get("value", selected.text())
        elif node.tag == "textarea":
            fields[name] = node.text()
    return fields


@dataclass(frozen=True)
class DeltaResponse:
    update_panels: dict[str, str]
    hidden_fields: dict[str, str]
    scripts: tuple[str, ...]
    redirects: tuple[str, ...]


class HilanProtocol:
    ORG_ID = re.compile(r'\\?"OrgId\\?"\s*:\s*\\?"([0-9]+)\\?"')
    ROW_DATE = re.compile(r'"RowDate"\s*:\s*"(\d{4})-(\d{1,2})-(\d{1,2})"')

    @staticmethod
    def validate_tenant(tenant: str) -> str:
        tenant = tenant.strip()
        if not tenant or not re.fullmatch(r"[A-Za-z0-9.-]+", tenant):
            raise SafetyError("tenant must contain only letters, digits, dots, or hyphens")
        if tenant.startswith(".") or tenant.endswith(".") or ".." in tenant:
            raise SafetyError("tenant has an invalid DNS-label shape")
        return tenant

    @classmethod
    def extract_org_id(cls, content: str) -> str:
        match = cls.ORG_ID.search(content)
        if not match:
            raise ProtocolError("tenant page did not contain a numeric OrgId")
        return match.group(1)

    @staticmethod
    def parse_login_response(content: str) -> None:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise AuthenticationError(
                "login did not return JSON; SSO may be required"
            ) from exc
        if "IsFail" not in payload or not isinstance(payload["IsFail"], bool):
            raise ProtocolError("login response is missing Boolean IsFail")
        if payload.get("IsShowCaptcha") is True:
            raise AuthenticationError("CAPTCHA must be completed in Hilanet")
        if payload["IsFail"] is False:
            return
        if payload.get("Code") == 18:
            raise AuthenticationError("account is temporarily locked")
        if payload.get("Code") == 6:
            raise AuthenticationError("password must be changed in Hilanet")
        raise AuthenticationError(
            server_text(
                payload.get("ErrorMessage") or "unknown login error"
            )
        )

    @staticmethod
    def parse_bootstrap(content: str) -> dict:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ProtocolError("employee bootstrap returned invalid JSON") from exc
        principal = payload.get("PrincipalUser")
        if not isinstance(principal, dict):
            raise ProtocolError("employee bootstrap is missing PrincipalUser")
        organization_id = payload.get("OrganizationId") or principal.get(
            "OrganizationId"
        )
        if not organization_id:
            raise ProtocolError("employee bootstrap is missing OrganizationId")
        try:
            short_id = int(principal["EmployeeId"])
            long_id = str(principal["UserId"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtocolError("employee bootstrap contains invalid IDs") from exc
        if not long_id:
            raise ProtocolError("employee bootstrap contains an empty UserId")
        return {
            "long_id": long_id,
            "short_id": short_id,
            "organization_id": str(organization_id),
        }

    @staticmethod
    def parse_delta(content: str) -> DeltaResponse:
        if not content:
            raise ProtocolError("empty ASP.NET delta response")
        position = 0
        panels: dict[str, str] = {}
        hidden: dict[str, str] = {}
        scripts: list[str] = []
        redirects: list[str] = []
        while position < len(content):
            if content[position] == "|":
                position += 1
                continue
            length_end = content.find("|", position)
            if length_end < 0 or not content[position:length_end].isdigit():
                raise ProtocolError("malformed ASP.NET delta length")
            length = int(content[position:length_end])
            type_start = length_end + 1
            type_end = content.find("|", type_start)
            id_start = type_end + 1
            id_end = content.find("|", id_start)
            if type_end < 0 or id_end < 0:
                raise ProtocolError("malformed ASP.NET delta header")
            kind = content[type_start:type_end]
            item_id = content[id_start:id_end]
            value_start = id_end + 1
            value_end = value_start + length
            if value_end > len(content):
                raise ProtocolError("truncated ASP.NET delta content")
            value = content[value_start:value_end]
            if value_end < len(content) and content[value_end] != "|":
                raise ProtocolError("invalid ASP.NET delta delimiter")
            position = value_end + 1
            if kind == "updatePanel":
                panels[item_id] = value
            elif kind == "hiddenField":
                hidden[item_id] = value
            elif kind in {"scriptBlock", "scriptStartupBlock"}:
                scripts.append(value)
            elif kind == "pageRedirect":
                redirects.append(value)
        if not panels and not hidden and not scripts and not redirects:
            raise ProtocolError("ASP.NET delta contained no recognized entries")
        return DeltaResponse(panels, hidden, tuple(scripts), tuple(redirects))

    @classmethod
    def async_content(
        cls,
        content: str,
    ) -> tuple[str, dict[str, str]]:
        if content.lstrip().casefold().startswith(
            ("<html", "<!doctype")
        ):
            return content, {}
        delta = cls.parse_delta(content)
        if delta.redirects:
            decoded = [
                urllib.parse.unquote(item)
                for item in delta.redirects
            ]
            raise ProtocolError(
                f"Hilan async request redirected to {decoded[0]}"
            )
        return "\n".join(delta.update_panels.values()), delta.hidden_fields

    @staticmethod
    def parse_attendance_types(
        calendar_html: str,
        absences_json: str,
    ) -> list[AttendanceType]:
        by_code: dict[str, AttendanceType] = {}
        root = parse_html(calendar_html)
        for select in root.descendants("select"):
            name = select.attrs.get("name", "")
            if "Symbol.SymbolId" not in name and "SymbolId" not in name:
                continue
            for option in select.descendants("option"):
                code = server_text(option.attrs.get("value", ""), limit=32)
                label = server_text(option.text())
                if code and not re.fullmatch(r"[A-Za-z0-9._/-]+", code):
                    raise ProtocolError(
                        "attendance type contains an invalid code"
                    )
                if code or label:
                    by_code[code] = AttendanceType(code, label)
            break
        if absences_json:
            try:
                payload = json.loads(absences_json)
            except json.JSONDecodeError as exc:
                raise ProtocolError("absence types returned invalid JSON") from exc
            if isinstance(payload.get("d"), str):
                payload = json.loads(payload["d"])
            elif isinstance(payload.get("d"), dict):
                payload = payload["d"]
            symbols = payload.get("Symbols", [])
            if not isinstance(symbols, list):
                raise ProtocolError("absence types Symbols must be a list")
            for symbol in symbols:
                code = server_text(symbol.get("Id", ""), limit=32)
                localized = server_text(symbol.get("Name", ""))
                display = server_text(symbol.get("DisplayName", ""))
                if code and not re.fullmatch(r"[A-Za-z0-9._/-]+", code):
                    raise ProtocolError(
                        "absence type contains an invalid code"
                    )
                existing = by_code.get(code)
                if existing is None:
                    continue
                by_code[code] = AttendanceType(
                    code,
                    existing.name or display,
                    localized,
                )
        return [item for code, item in by_code.items() if code]

    @staticmethod
    def resolve_type(
        attendance_types: Iterable[AttendanceType],
        requested: str,
    ) -> AttendanceType:
        needle = requested.strip().casefold()
        matches = []
        for item in attendance_types:
            candidates = {
                item.code.casefold(),
                item.name.casefold(),
                item.localized_name.casefold(),
            }
            if item.localized_name == "חופשה":
                candidates.add("vacation")
            if item.localized_name == "מחלה":
                candidates.add("sickness")
            if needle in candidates:
                matches.append(item)
        unique = {item.code: item for item in matches}
        if len(unique) != 1:
            available = ", ".join(
                sorted(
                    {
                        item.name or item.localized_name or item.code
                        for item in attendance_types
                    }
                )
            )
            raise SafetyError(
                f"attendance type must match exactly one tenant type; available: {available}"
            )
        return next(iter(unique.values()))

    @staticmethod
    def selected_day_index(target_date: date) -> int:
        return (target_date - date(2000, 1, 1)).days

    @classmethod
    def selected_response_dates(
        cls,
        content: str,
        assumed_year: int,
    ) -> set[date]:
        dates = {
            date(int(year), int(month), int(day))
            for year, month, day in cls.ROW_DATE.findall(content)
        }
        root = parse_html(content)
        for cell in root.descendants("td"):
            if "cellOf_ReportDate_row_" not in cell.attrs.get("id", ""):
                continue
            raw = (
                cell.attrs.get("ov")
                or cell.attrs.get("title")
                or cell.text()
            )
            match = re.search(
                r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b",
                raw,
            )
            if not match:
                continue
            day, month, year = match.groups()
            try:
                dates.add(
                    date(
                        int(year or assumed_year),
                        int(month),
                        int(day),
                    )
                )
            except ValueError:
                continue
        return dates

    @staticmethod
    def read_only_form(fields: dict[str, str]) -> dict[str, str]:
        safe = {}
        for key, value in fields.items():
            lowered = key.casefold()
            if lowered.endswith(("$btnsave", "_btnsave")):
                continue
            if lowered.endswith("reportsgrid_action"):
                continue
            if value == "DELETE_ROW":
                continue
            safe[key] = value
        return safe

def _visual_day_node(root: HtmlNode, target_date: date) -> HtmlNode | None:
    target_index = str(HilanProtocol.selected_day_index(target_date))
    for cell in root.descendants("td"):
        if cell.attrs.get("days") == target_index:
            return cell
    return None


def parse_day_snapshot(
    target_date: date,
    content: str,
) -> DaySnapshot:
    row_dates = HilanProtocol.selected_response_dates(
        content,
        target_date.year,
    )
    if row_dates and target_date not in row_dates:
        raise ProtocolError("selected-day response returned a different date")
    root = parse_html(content)
    suffixes = (
        f"{target_date.year}_{target_date.month:02d}$btnSave",
        f"{target_date.year}_{target_date.month:02d}_btnSave",
    )
    save = None
    for item in root.descendants("input"):
        identity = item.attrs.get("name") or item.attrs.get("id", "")
        if (
            ("$RG_Days_" in identity or "_RG_Days_" in identity)
            and identity.endswith(suffixes)
        ):
            save = item
            break
    existing_code = ""
    existing_name = ""
    ambiguous_existing_row = False
    default_code = ""
    default_name = ""
    selected_report_row = None
    selected_row_suffix = ""
    report_rows = []
    for row in root.descendants("tr"):
        if "_EmployeeReports_row_" not in row.attrs.get("id", ""):
            continue
        selects = list(row.descendants("select"))
        if not selects:
            continue
        select_identity = (
            selects[0].attrs.get("name")
            or selects[0].attrs.get("id", "")
        )
        suffix_match = re.search(
            r"EmployeeReports_row_(\d+_\d+)",
            select_identity,
        )
        suffix = suffix_match.group(1) if suffix_match else ""
        report_rows.append((suffix != "0_0", row, selects[0], suffix))

    if report_rows:
        _, selected_report_row, selected_select, selected_row_suffix = sorted(
            report_rows,
            key=lambda item: item[0],
        )[0]
        options = list(selected_select.descendants("option"))
        explicit = next(
            (item for item in options if "selected" in item.attrs),
            None,
        )
        selected = explicit or (options[0] if options else None)
        if selected and selected.attrs.get("value", "").strip():
            if explicit:
                existing_code = selected.attrs["value"].strip()
                existing_name = selected.text().strip()
            else:
                default_code = selected.attrs["value"].strip()
                default_name = selected.text().strip()
                ambiguous_existing_row = True
        if selected is None:
            ambiguous_existing_row = True

    visual = _visual_day_node(root, target_date)
    visual_classes = classes(visual) if visual else set()
    visual_text = visual.text().casefold() if visual else ""
    visual_title = visual.attrs.get("title", "").strip() if visual else ""
    visual_messages = []
    if visual:
        for node in visual.descendants():
            if "cDM" in classes(node):
                visual_messages.append(node.text())

    def is_time(value: str) -> bool:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
        return bool(
            match
            and int(match.group(1)) < 24
            and int(match.group(2)) < 60
        )

    def normalize_time(value: str) -> str:
        hour, minute = value.strip().split(":")
        return f"{int(hour):02d}:{int(minute):02d}"

    visual_times = {
        normalize_time(value)
        for value in (
            [visual_title]
            + visual_messages
            + re.findall(r"\b\d{1,2}:\d{2}\b", visual_text)
        )
        if is_time(value)
    }
    visual_has_time = bool(visual_times)
    manual_times = []
    manual_entries = []
    manual_exits = []
    manual_inputs = (
        selected_report_row.descendants("input")
        if selected_report_row
        else ()
    )
    for item in manual_inputs:
        identity = item.attrs.get("name") or item.attrs.get("id", "")
        if (
            "ManualEntry_EmployeeReports" in identity
            or "ManualExit_EmployeeReports" in identity
        ):
            if selected_row_suffix and (
                f"EmployeeReports_row_{selected_row_suffix}" not in identity
            ):
                continue
            value = item.attrs.get("value", "").strip()
            manual_times.append(value)
            if "ManualEntry_EmployeeReports" in identity:
                manual_entries.append(value)
            else:
                manual_exits.append(value)
    nonempty_exits = [value for value in manual_exits if value]
    scheduled_manual_times = (
        bool(nonempty_exits)
        and bool(visual_times)
        and all(value in {"", "00:00"} for value in manual_entries)
        and all(
            is_time(value)
            and normalize_time(value) in visual_times
            for value in nonempty_exits
        )
    )
    default_manual_times = not any(manual_times) or scheduled_manual_times
    candidate_code = existing_code or default_code
    candidate_name = existing_name or default_name
    default_is_blank_attendance = (
        candidate_code == "0"
        and candidate_name.casefold()
        in {"attendance", "work day", "יום עבודה"}
        and default_manual_times
    )
    if default_is_blank_attendance:
        existing_code = ""
        existing_name = ""
        default_code = ""
        default_name = ""
        ambiguous_existing_row = False
    elif default_code:
        existing_code = default_code
        existing_name = default_name
    icon_classes = set()
    if visual:
        for span in visual.descendants("span"):
            icon_classes.update(classes(span))
    holiday = bool(
        {"cHD", "holiday"} & visual_classes
        or "חג" in visual_text
        or any(
            token in visual_text.split()
            for token in ("d.off", "day", "ho", "h.o")
        )
    )
    error = ""
    if {"fh-error", "fh-warning"} & icon_classes:
        error = "Hilan marked the day with an error"
    elif any(marker in visual_text for marker in ("שגיאה", "error", "missing")):
        error = "Hilan reported an attendance error"
    source = "unreported"
    uncertain_visual_absence = False
    if "cED" in visual_classes:
        source = "system"
    elif existing_code:
        source = "user"
    elif "calendarAbcenseDay" in visual_classes:
        if visual_title and not is_time(visual_title):
            source = "user"
        elif default_is_blank_attendance:
            pass
        elif not visual_has_time:
            uncertain_visual_absence = True
    if source == "system" and not existing_code:
        existing_code = "system-filled"
        existing_name = "System-filled attendance"
    if source != "unreported":
        ambiguous_existing_row = False
    return DaySnapshot(
        date=target_date,
        editable=save is not None and "disabled" not in save.attrs,
        holiday=holiday,
        error=error,
        existing_type_code=existing_code,
        existing_type_name=existing_name,
        source=source,
        known=(
            bool(row_dates)
            and save is not None
            and visual is not None
            and not ambiguous_existing_row
            and not uncertain_visual_absence
        ),
    )


@dataclass(frozen=True)
class AppConfig:
    tenant: str
    username: str

    @property
    def base_url(self) -> str:
        return f"https://{self.tenant}.hilan.co.il"


def app_data_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "attendance-reporter"
    return Path.home() / ".attendance-reporter"


def config_path() -> Path:
    return app_data_root() / "config.json"


def preview_root() -> Path:
    return app_data_root() / "previews"


def save_config(config: AppConfig) -> None:
    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(config), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as exc:
        raise SafetyError("could not save attendance configuration") from exc


def load_config() -> AppConfig:
    path = config_path()
    if not path.exists():
        raise SafetyError("attendance skill is not configured; run setup")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SafetyError("attendance configuration is unreadable") from exc
    tenant = HilanProtocol.validate_tenant(str(payload.get("tenant", "")))
    username = str(payload.get("username", "")).strip()
    if not username:
        raise SafetyError("configured username is empty")
    return AppConfig(tenant, username)


class CredentialFileTime(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


class Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", CredentialFileTime),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


class WindowsCredentialStore:
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self):
        if os.name != "nt":
            raise SafetyError("Windows Credential Manager is required")
        self.advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self.advapi.CredWriteW.argtypes = [
            ctypes.POINTER(Credential),
            ctypes.c_uint32,
        ]
        self.advapi.CredWriteW.restype = ctypes.c_int
        self.advapi.CredReadW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.POINTER(Credential)),
        ]
        self.advapi.CredReadW.restype = ctypes.c_int
        self.advapi.CredFree.argtypes = [ctypes.c_void_p]
        self.advapi.CredDeleteW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self.advapi.CredDeleteW.restype = ctypes.c_int

    @staticmethod
    def target(config: AppConfig) -> str:
        return f"attendance-reporter:{config.base_url}"

    @staticmethod
    def preview_key_target(config: AppConfig) -> str:
        return (
            "attendance-reporter-preview-key:"
            f"{config.base_url}:{config.username}"
        )

    def _write(self, target: str, username: str, secret: str) -> None:
        blob = secret.encode("utf-16-le")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = Credential(
            Type=self.CRED_TYPE_GENERIC,
            TargetName=target,
            CredentialBlobSize=len(blob),
            CredentialBlob=ctypes.cast(
                buffer,
                ctypes.POINTER(ctypes.c_ubyte),
            ),
            Persist=self.CRED_PERSIST_LOCAL_MACHINE,
            UserName=username,
        )
        if not self.advapi.CredWriteW(ctypes.byref(credential), 0):
            raise SafetyError(
                f"Windows Credential Manager write failed ({ctypes.get_last_error()})"
            )

    def _read(self, target: str, missing_message: str) -> str:
        pointer = ctypes.POINTER(Credential)()
        ok = self.advapi.CredReadW(
            target,
            self.CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        )
        if not ok:
            raise AuthenticationError(missing_message)
        try:
            credential = pointer.contents
            blob = ctypes.string_at(
                credential.CredentialBlob,
                credential.CredentialBlobSize,
            )
            return blob.decode("utf-16-le")
        finally:
            self.advapi.CredFree(pointer)

    def write(self, config: AppConfig, password: str) -> None:
        self._write(self.target(config), config.username, password)

    def read(self, config: AppConfig) -> str:
        return self._read(
            self.target(config),
            "stored Hilanet credential was not found; run setup",
        )

    def preview_key(self, config: AppConfig) -> bytes:
        target = self.preview_key_target(config)
        try:
            encoded = self._read(
                target,
                "preview signing key was not found",
            )
        except AuthenticationError:
            encoded = secrets.token_urlsafe(32)
            self._write(target, config.username, encoded)
        return hashlib.sha256(encoded.encode("utf-8")).digest()

    def delete(self, config: AppConfig) -> None:
        self._delete(self.target(config))

    def delete_preview_key(self, config: AppConfig) -> None:
        self._delete(self.preview_key_target(config))

    def _delete(self, target: str) -> None:
        if not self.advapi.CredDeleteW(
            target,
            self.CRED_TYPE_GENERIC,
            0,
        ):
            raise SafetyError(
                f"Windows Credential Manager delete failed ({ctypes.get_last_error()})"
            )


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_origin: str):
        self.allowed_origin = allowed_origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(self.allowed_origin)
        new = urllib.parse.urlsplit(newurl)
        if (new.scheme, new.netloc) != (old.scheme, old.netloc):
            raise AuthenticationError(
                "cross-origin sign-in is unsupported; owned Edge automation may be required"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class HttpResponse:
    status: int
    url: str
    body: str


class HilanSession:
    def __init__(self, config: AppConfig, password: str):
        self.config = config
        self.password = password
        jar = http.cookiejar.CookieJar()
        self.cookie_jar = jar
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar),
            SameOriginRedirectHandler(config.base_url),
        )
        self.identity: dict | None = None
        self.last_selected_html = ""

    def _request(
        self,
        method: str,
        path: str,
        *,
        form: dict[str, str] | None = None,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> HttpResponse:
        url = urllib.parse.urljoin(self.config.base_url, path)
        split = urllib.parse.urlsplit(url)
        base = urllib.parse.urlsplit(self.config.base_url)
        if (split.scheme, split.netloc) != (base.scheme, base.netloc):
            raise SafetyError("request escaped the configured Hilanet origin")
        body = None
        request_headers = {
            "Accept-Language": "en-IL,en;q=0.9,he-IL;q=0.8,he;q=0.7,en-US;q=0.6",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0"
            ),
        }
        if form is not None:
            body = urllib.parse.urlencode(form).encode("utf-8")
            request_headers["Content-Type"] = (
                "application/x-www-form-urlencoded"
            )
        elif json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        request = urllib.request.Request(
            url,
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return HttpResponse(
                    response.status,
                    response.geturl(),
                    raw.decode(charset, errors="replace"),
                )
        except urllib.error.HTTPError as exc:
            raise ProtocolError(f"Hilan returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ProtocolError(f"Hilan request failed: {exc.reason}") from exc
        except OSError as exc:
            raise ProtocolError(f"Hilan transport failed: {exc}") from exc

    def authenticate(self) -> None:
        root = self._request("GET", "/")
        org_id = HilanProtocol.extract_org_id(root.body)
        login = self._request(
            "POST",
            LOGIN_PATH,
            form={
                "username": self.config.username,
                "password": self.password,
                "orgId": org_id,
            },
        )
        HilanProtocol.parse_login_response(login.body)
        bootstrap = self._request("POST", BOOTSTRAP_PATH, json_body={})
        self.identity = HilanProtocol.parse_bootstrap(bootstrap.body)

    def ensure_authenticated(self) -> None:
        if self.identity is None:
            self.authenticate()

    def _calendar_headers(self, *, async_post: bool = False) -> dict[str, str]:
        headers = {
            "Origin": self.config.base_url,
            "Referer": urllib.parse.urljoin(
                self.config.base_url,
                CALENDAR_PATH,
            ),
        }
        if async_post:
            headers.update(
                {
                    "Accept": "*/*",
                    "X-MicrosoftAjax": "Delta=true",
                    "X-Requested-With": "XMLHttpRequest",
                    "Cache-Control": "no-cache",
                }
            )
        return headers

    @staticmethod
    def _displayed_month(fields: dict[str, str]) -> date:
        raw = fields.get("ctl00$mp$currentMonth", "")
        try:
            return datetime.strptime(raw, "%d/%m/%Y").date().replace(day=1)
        except ValueError as exc:
            raise ProtocolError("calendar did not expose its current month") from exc

    def _load_month(self, target_date: date) -> tuple[str, dict[str, str]]:
        response = self._request("GET", CALENDAR_PATH)
        content = response.body
        fields = parse_form_fields(content)
        requested = target_date.replace(day=1)
        current = self._displayed_month(fields)
        if current == requested:
            return self._ensure_visual_grid(content, fields, target_date)

        first_index = HilanProtocol.selected_day_index(requested)
        regular = HilanProtocol.read_only_form(fields)
        regular["__calendarSelectedDays"] = str(first_index)
        regular["ctl00$mp$currentMonth"] = requested.strftime("01/%m/%Y")
        regular["ctl00$mp$RefreshPeriod"] = "דיווח תקופתי"
        response = self._request(
            "POST",
            CALENDAR_PATH,
            form=regular,
            headers=self._calendar_headers(),
        )
        content = response.body
        fields = parse_form_fields(content)
        if self._displayed_month(fields) == requested:
            return self._ensure_visual_grid(content, fields, target_date)

        event = HilanProtocol.read_only_form(fields)
        event["__calendarSelectedDays"] = str(first_index)
        event["ctl00$mp$currentMonth"] = requested.strftime("01/%m/%Y")
        event["__EVENTTARGET"] = "ctl00$mp$calendar_monthChanged"
        event["__EVENTARGUMENT"] = requested.strftime("01/%m/%Y")
        response = self._request(
            "POST",
            CALENDAR_PATH,
            form=event,
            headers=self._calendar_headers(),
        )
        content = response.body
        fields = parse_form_fields(content)
        if self._displayed_month(fields) != requested:
            raise ProtocolError("Hilan did not navigate to the requested month")
        return self._ensure_visual_grid(content, fields, target_date)

    def _ensure_visual_grid(
        self,
        content: str,
        fields: dict[str, str],
        target_date: date,
    ) -> tuple[str, dict[str, str]]:
        if _visual_day_node(parse_html(content), target_date) is not None:
            return content, fields
        refresh = HilanProtocol.read_only_form(fields)
        refresh["ctl00$mp$currentMonth"] = target_date.strftime("01/%m/%Y")
        refresh["ctl00$mp$RefreshPeriod"] = "דיווח תקופתי"
        refresh["__EVENTTARGET"] = "ctl00$mp$RefreshPeriod"
        refresh["__EVENTARGUMENT"] = ""
        refresh["__ASYNCPOST"] = "true"
        refresh["ctl00$ms"] = "ctl00$ms|ctl00$mp$RefreshPeriod"
        headers = self._calendar_headers(async_post=True)
        headers["H-XSRF-Token"] = fields.get("H-XSRF-Token", "")
        response = self._request(
            "POST",
            CALENDAR_PATH,
            form=refresh,
            headers=headers,
        )
        panel_html, hidden_fields = HilanProtocol.async_content(
            response.body
        )
        merged = dict(fields)
        merged.update(hidden_fields)
        merged.update(parse_form_fields(panel_html))
        return f"{content}\n{panel_html}", merged

    def _select_day(self, target_date: date):
        self.ensure_authenticated()
        base_html, fields = self._load_month(target_date)
        selected_index = HilanProtocol.selected_day_index(target_date)
        selected = HilanProtocol.read_only_form(fields)
        selected["__calendarSelectedDays"] = f",{selected_index}"
        selected["ctl00$mp$currentMonth"] = target_date.strftime("01/%m/%Y")
        selected["ctl00$mp$RefreshSelectedDays"] = "ימים נבחרים"
        selected["ctl00$ms"] = (
            "ctl00$mp$upBtns|ctl00$mp$RefreshSelectedDays"
        )
        selected["__EVENTTARGET"] = ""
        selected["__EVENTARGUMENT"] = ""
        selected["__ASYNCPOST"] = "true"
        headers = self._calendar_headers(async_post=True)
        headers["H-XSRF-Token"] = fields.get("H-XSRF-Token", "")
        response = self._request(
            "POST",
            CALENDAR_PATH,
            form=selected,
            headers=headers,
        )
        panel_html, hidden_fields = HilanProtocol.async_content(
            response.body
        )
        row_dates = HilanProtocol.selected_response_dates(
            panel_html,
            target_date.year,
        )
        if target_date not in row_dates:
            raise ProtocolError(
                "Hilan selected a different day; refusing to continue"
            )
        merged = dict(fields)
        merged.update(hidden_fields)
        merged.update(parse_form_fields(panel_html))
        merged["__calendarSelectedDays"] = str(selected_index)
        merged["ctl00$mp$currentMonth"] = target_date.strftime("01/%m/%Y")
        combined = f"{base_html}\n{panel_html}"
        self.last_selected_html = combined
        snapshot = parse_day_snapshot(target_date, combined)
        return snapshot, merged, combined, selected_index

    def attendance_types(self) -> list[AttendanceType]:
        self.ensure_authenticated()
        calendar = self._request("GET", CALENDAR_PATH)
        calendar_html = f"{calendar.body}\n{self.last_selected_html}"
        try:
            absences = self._request("POST", ABSENCES_PATH, json_body={})
        except ProtocolError:
            types = HilanProtocol.parse_attendance_types(calendar_html, "")
            if not types:
                raise
            return types
        return HilanProtocol.parse_attendance_types(calendar_html, absences.body)

    def inspect(self, target_date: date) -> DaySnapshot:
        snapshot, _, _, _ = self._select_day(target_date)
        return snapshot

    def submit_absence(
        self,
        target_date: date,
        attendance_type: AttendanceType,
        comment: str,
        *,
        today: date,
    ) -> WriteReceipt:
        snapshot, _, _, _ = self._select_day(target_date)
        reason = snapshot.block_reason(today)
        if reason:
            return WriteReceipt.blocked(
                f"{target_date.isoformat()} became blocked: {reason}"
            )
        return self.submit_absence_browser(
            target_date,
            attendance_type,
            comment,
        )

    def submit_absence_browser(
        self,
        target_date: date,
        attendance_type: AttendanceType,
        comment: str,
    ) -> WriteReceipt:
        try:
            from playwright.sync_api import (
                TimeoutError as PlaywrightTimeoutError,
                sync_playwright,
            )
        except ImportError:
            return WriteReceipt.blocked(
                "Playwright is required for this Hilanet tenant"
            )

        self.ensure_authenticated()
        messages = []
        try:
            with sync_playwright() as playwright:
                browser, page = self._open_browser_calendar(playwright)
                row_index, type_select = self._select_browser_row(
                    page,
                    target_date,
                )
                before_code = type_select.input_value()
                if before_code not in {"", "0"}:
                    browser.close()
                    return WriteReceipt.blocked(
                        "Hilan browser row already contains attendance"
                    )
                type_select.focus()
                type_select.select_option(value=attendance_type.code)
                if comment:
                    comment_input = page.locator(
                        "input[id*='Comment'][id*="
                        f"'_EmployeeReports_row_{row_index}_0'], "
                        "input[name*='Comment'][name*="
                        f"'_EmployeeReports_row_{row_index}_0']"
                    )
                    comment_fields = [
                        comment_input.nth(index)
                        for index in range(comment_input.count())
                        if comment_input.nth(index).is_visible()
                        and comment_input.nth(index).is_enabled()
                    ]
                    if not comment_fields:
                        browser.close()
                        return WriteReceipt.blocked(
                            "Hilan browser did not expose a comment field"
                        )
                    comment_fields[-1].fill(comment)

                save = page.locator(
                    "input[type='submit'][id$='_btnSave'], "
                    "input[type='submit'][name$='$btnSave'], "
                    "input[value='Save'], input[value='שמירה']"
                )
                save_buttons = [
                    save.nth(index)
                    for index in range(save.count())
                    if save.nth(index).is_visible()
                    and save.nth(index).is_enabled()
                ]
                if not save_buttons:
                    browser.close()
                    return WriteReceipt.blocked(
                        "Hilan browser did not expose an enabled Save"
                    )
                save_buttons[-1].click()
                page.wait_for_timeout(1000)
                dialog_clicked = False
                for _ in range(20):
                    page.wait_for_timeout(500)
                    visible_dialog = False
                    for frame in page.frames[1:]:
                        message_node = frame.locator("#messagePlace")
                        if message_node.count() == 0:
                            continue
                        try:
                            message = message_node.inner_text(
                                timeout=500
                            ).strip()
                        except PlaywrightTimeoutError:
                            continue
                        if message:
                            messages.append(server_text(message))
                        visible_dialog = True
                        if dialog_clicked:
                            continue
                        second = frame.locator("#secondButton")
                        if second.count() == 1 and second.is_visible():
                            second.click()
                            dialog_clicked = True
                            continue
                        affirmative = frame.locator(
                            "input[value='OK'], "
                            "input[value='Yes'], "
                            "input[value='אישור']"
                        )
                        visible_affirmative = [
                            affirmative.nth(index)
                            for index in range(affirmative.count())
                            if affirmative.nth(index).is_visible()
                        ]
                        if len(visible_affirmative) == 1:
                            visible_affirmative[0].click()
                            dialog_clicked = True
                            continue
                        browser.close()
                        return WriteReceipt.outcome_unknown(
                            "Hilan displayed an unrecognized confirmation dialog"
                        )
                    if visible_dialog:
                        continue
                    if dialog_clicked:
                        break
                try:
                    page.wait_for_load_state(
                        "networkidle",
                        timeout=10000,
                    )
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(1000)
                browser.close()
        except Exception as exc:
            return WriteReceipt.outcome_unknown(
                "Hilan browser operation failed: "
                f"{server_text(exc)}"
            )

        message = " | ".join(messages)
        lowered = message.casefold()
        if any(
            marker in lowered
            for marker in (
                "overlapping report",
                "error",
                "failed",
                "שגיאה",
            )
        ):
            return WriteReceipt.rejected(message)
        return WriteReceipt.committed()

    def browser_read_type(self, target_date: date) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ProtocolError(
                "Playwright is required for browser proof"
            ) from exc
        self.ensure_authenticated()
        with sync_playwright() as playwright:
            browser, page = self._open_browser_calendar(playwright)
            _, type_select = self._select_browser_row(page, target_date)
            code = type_select.input_value()
            browser.close()
            return code

    def browser_probe_types(
        self,
        target_date: date,
        attendance_types: Iterable[AttendanceType],
    ) -> list[dict]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ProtocolError(
                "Playwright is required for type probing"
            ) from exc
        self.ensure_authenticated()
        with sync_playwright() as playwright:
            browser, page = self._open_browser_calendar(playwright)
            _, type_select = self._select_browser_row(page, target_date)
            original_code = type_select.input_value()
            if original_code not in {"", "0"}:
                browser.close()
                raise SafetyError(
                    "type probing requires an unreported default row"
                )
            option_codes = set(
                type_select.locator("option").evaluate_all(
                    "(options) => options.map((option) => option.value)"
                )
            )
            results = []
            for attendance_type in attendance_types:
                result = {
                    **attendance_type.to_dict(),
                    "status": "unavailable",
                    "extra_input_types": [],
                }
                if attendance_type.code not in option_codes:
                    results.append(result)
                    continue
                try:
                    type_select.select_option(value=attendance_type.code)
                    page.wait_for_timeout(50)
                    result["status"] = (
                        "selectable"
                        if type_select.input_value() == attendance_type.code
                        else "selection_failed"
                    )
                    result["extra_input_types"] = sorted(
                        set(
                            page.locator(
                                "input[type='file']:visible, "
                                "input[required]:visible, "
                                "select[required]:visible, "
                                "textarea[required]:visible"
                            ).evaluate_all(
                                """(elements) => elements.map((element) =>
                                    element.type || element.tagName.toLowerCase()
                                )"""
                            )
                        )
                    )
                except Exception as exc:
                    result["status"] = "selection_failed"
                    result["error"] = server_text(exc)
                finally:
                    try:
                        type_select.select_option(value=original_code)
                    except Exception:
                        browser.close()
                        raise ProtocolError(
                            "Hilan row could not be reset after type probing"
                        )
                results.append(result)
            browser.close()
            return results

    def _open_browser_calendar(self, playwright):
        browser = playwright.chromium.launch(
            channel="msedge",
            headless=True,
        )
        context = browser.new_context(
            locale="en-IL",
            timezone_id="Asia/Jerusalem",
        )
        context.add_cookies(
            [
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path or "/",
                    "secure": bool(cookie.secure),
                    "httpOnly": bool(
                        cookie.has_nonstandard_attr("HttpOnly")
                    ),
                }
                for cookie in self.cookie_jar
            ]
        )
        page = context.new_page()
        page.goto(
            urllib.parse.urljoin(
                self.config.base_url,
                CALENDAR_PATH,
            ),
            wait_until="domcontentloaded",
            timeout=30000,
        )
        if "login" in page.url.casefold():
            browser.close()
            raise ProtocolError(
                "authenticated browser session was not accepted"
            )
        self._assert_browser_account(page)
        return browser, page

    def _select_browser_row(self, page, target_date: date):
        day_index = HilanProtocol.selected_day_index(target_date)
        cell = page.locator(f'td[days="{day_index}"]')
        if cell.count() != 1:
            raise ProtocolError(
                "Hilan browser did not expose the exact target date"
            )
        cell.click()
        page.wait_for_timeout(500)
        selected_days = page.locator(
            "#ctl00_mp_RefreshSelectedDays, "
            "input[value='Days selected'], "
            "input[value='Selected Days'], "
            "input[value='ימים נבחרים']"
        )
        if selected_days.count() == 0:
            raise ProtocolError(
                "Hilan browser did not expose Selected Days"
            )
        selected_days.first.click()
        page.wait_for_timeout(1500)
        row_index = self._browser_row_index(page, target_date)
        select = page.locator(
            "select[id*='Symbol'][id*="
            f"'_EmployeeReports_row_{row_index}_0'], "
            "select[name*='Symbol'][name*="
            f"'_EmployeeReports_row_{row_index}_0']"
        )
        editable = [
            select.nth(index)
            for index in range(select.count())
            if select.nth(index).is_visible()
            and select.nth(index).is_enabled()
        ]
        if not editable:
            raise ProtocolError("Hilan browser row is not editable")
        return row_index, editable[-1]

    def _assert_browser_account(self, page) -> None:
        if self.identity is None:
            raise AuthenticationError(
                "Hilan browser session is not authenticated"
            )
        current_item = page.locator(
            'input[name="ctl00$mp$Strip$hCurrentItemId"]'
        )
        values = {
            current_item.nth(index).input_value()
            for index in range(current_item.count())
        }
        if values != {self.identity["long_id"]}:
            raise ProtocolError(
                "Hilan browser session selected a different employee"
            )

    @staticmethod
    def _browser_row_index(page, target_date: date) -> str:
        expected_ddmm = target_date.strftime("%d/%m")
        matches = []
        date_cells = page.locator(
            "td[id*='_cellOf_ReportDate_row_']"
        )
        for index in range(date_cells.count()):
            candidate = date_cells.nth(index)
            raw = (
                candidate.get_attribute("ov")
                or candidate.get_attribute("title")
                or candidate.inner_text()
            )
            match = re.search(r"\d{1,2}/\d{1,2}", raw or "")
            if not match:
                continue
            try:
                parsed = datetime.strptime(
                    match.group(),
                    "%d/%m",
                ).strftime("%d/%m")
            except ValueError:
                continue
            if parsed != expected_ddmm:
                continue
            id_match = re.search(
                r"_cellOf_ReportDate_row_(\d+)",
                candidate.get_attribute("id") or "",
            )
            if id_match:
                matches.append(id_match.group(1))
        if len(matches) != 1:
            raise ProtocolError(
                "Hilan browser did not expose one exact target row"
            )
        return matches[0]


class HilanBackend:
    def __init__(self, config: AppConfig, password: str):
        self.config = config
        self.session = HilanSession(config, password)
        self._types: list[AttendanceType] | None = None

    def account_context(self) -> str:
        self.session.ensure_authenticated()
        if self.session.identity is None:
            raise AuthenticationError(
                "Hilan session did not provide an employee identity"
            )
        return "|".join(
            (
                self.config.tenant,
                self.config.username,
                self.session.identity["organization_id"],
                self.session.identity["long_id"],
                str(self.session.identity["short_id"]),
            )
        )

    def resolve_type(self, requested: str) -> AttendanceType:
        if self._types is None:
            self._types = self.session.attendance_types()
        return HilanProtocol.resolve_type(self._types, requested)

    def inspect(self, target_date: date) -> DaySnapshot:
        return self.session.inspect(target_date)

    def submit_absence(
        self,
        target_date: date,
        attendance_type: AttendanceType,
        comment: str,
        *,
        today: date,
    ) -> WriteReceipt:
        return self.session.submit_absence(
            target_date,
            attendance_type,
            comment,
            today=today,
        )

    def verify_type(
        self,
        target_date: date,
        attendance_type: AttendanceType,
    ) -> bool:
        return (
            self.session.browser_read_type(target_date)
            == attendance_type.code
        )


def make_service() -> AttendanceService:
    config = load_config()
    credentials = WindowsCredentialStore()
    password = credentials.read(config)
    backend = HilanBackend(config, password)
    signing_key = credentials.preview_key(config)
    return AttendanceService(
        backend,
        PreviewStore(preview_root(), signing_key=signing_key),
    )


def print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))


def format_attendance_types(
    attendance_types: Iterable[AttendanceType],
) -> list[dict]:
    return [
        {
            "code": item.code,
            "name": item.name,
            "localized_name": item.localized_name,
        }
        for item in attendance_types
    ]


def command_setup(args) -> dict:
    tenant = HilanProtocol.validate_tenant(args.tenant)
    username = args.username.strip()
    if not username:
        raise SafetyError("username is required")
    password = getpass.getpass("Hilanet password: ")
    confirmation = getpass.getpass("Repeat password: ")
    if not password or password != confirmation:
        raise SafetyError("passwords did not match")
    config = AppConfig(tenant, username)
    credentials = WindowsCredentialStore()
    credentials.write(config, password)
    credentials.preview_key(config)
    save_config(config)
    return {"status": "configured", "tenant": tenant}


def command_doctor(_args) -> dict:
    config = load_config()
    password = WindowsCredentialStore().read(config)
    backend = HilanBackend(config, password)
    types = backend.session.attendance_types()
    return {
        "status": "ready",
        "tenant": config.tenant,
        "attendance_types": format_attendance_types(types),
    }


def command_types(_args) -> dict:
    config = load_config()
    password = WindowsCredentialStore().read(config)
    backend = HilanBackend(config, password)
    return {
        "status": "ready",
        "tenant": config.tenant,
        "attendance_types": format_attendance_types(
            backend.session.attendance_types()
        ),
    }


def command_probe_types(args) -> dict:
    config = load_config()
    password = WindowsCredentialStore().read(config)
    session = HilanSession(config, password)
    attendance_types = session.attendance_types()
    target_date = build_plan([args.date], "probe").dates[0]
    return {
        "status": "ready",
        "date": target_date.isoformat(),
        "write_performed": False,
        "attendance_types": session.browser_probe_types(
            target_date,
            attendance_types,
        ),
    }


def command_preview(args) -> dict:
    plan = build_plan(args.date, args.type, args.comment)
    return make_service().preview(plan)


def command_execute(args) -> dict:
    return make_service().execute(args.preview_id, args.confirm)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Private runner for the attendance-reporter skill"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup")
    setup.add_argument("--tenant", required=True)
    setup.add_argument("--username", required=True)
    setup.set_defaults(handler=command_setup)

    doctor = subparsers.add_parser("doctor")
    doctor.set_defaults(handler=command_doctor)

    types = subparsers.add_parser("types")
    types.set_defaults(handler=command_types)

    probe_types = subparsers.add_parser("probe-types")
    probe_types.add_argument("--date", required=True)
    probe_types.set_defaults(handler=command_probe_types)

    preview = subparsers.add_parser("preview")
    preview.add_argument("--date", action="append", required=True)
    preview.add_argument("--type", required=True)
    preview.add_argument("--comment", default="")
    preview.set_defaults(handler=command_preview)

    execute = subparsers.add_parser("execute")
    execute.add_argument("--preview-id", required=True)
    execute.add_argument("--confirm", required=True)
    execute.set_defaults(handler=command_execute)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        print_json(args.handler(args))
        return 0
    except AttendanceError as exc:
        print_json({"status": "error", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
