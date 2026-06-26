#!/usr/bin/env python3
"""Summarize Managed Virtual Services Google Chat time logs into Google Sheets.

Requires:
- Hermes Google OAuth token at ~/AppData/Local/hermes/google_token.json with scopes:
  chat.messages.readonly, chat.memberships.readonly, drive, spreadsheets
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import ssl
import sys
import time as time_module
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

HERMES_HOME = Path.home() / "AppData" / "Local" / "hermes"
# Support GitHub Actions: if GOOGLE_TOKEN_PATH is set, use it instead of the default.
TOKEN_PATH = Path(os.environ.get("GOOGLE_TOKEN_PATH", str(HERMES_HOME / "google_token.json")))
ENV_PATH = Path(os.environ.get("GOOGLE_ENV_PATH", str(HERMES_HOME / ".env")))
DEFAULT_SPACE_ID = os.environ.get("CHAT_SPACE_ID", "AAQAvfYSh9k")
SPACE = f"spaces/{DEFAULT_SPACE_ID}"


def space_name(space_id_or_name: str | None = None) -> str:
    raw = (space_id_or_name or SPACE).strip()
    return raw if raw.startswith("spaces/") else f"spaces/{raw}"

TZ = ZoneInfo("Asia/Manila")
WEBHOOK_ENV = "CHAT_WEBHOOK_URL"

SCOPES = [
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.memberships.readonly",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

SPECIAL_SUFFIX_NAMES: dict[str, str] = {}

NO_LOGIN_REQUIRED: set[str] = set()

COMPANY_NAME = "Managed Virtual Services"
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "ayel@managedvirtualservices.com")
ROOT_FOLDER_NAME = os.environ.get("ROOT_FOLDER_NAME", "Managed Virtual Services Employee Timesheets")
SHARE_DOMAIN = os.environ.get("SHARE_DOMAIN", "managedvirtualservices.com")
# Keep Ayel as owner, but explicitly share with the working MVS account and the two AGG accounts requested by Wrenz.
DIRECT_SHARE_EMAILS = [
    e.strip()
    for e in os.environ.get(
        "DIRECT_SHARE_EMAILS",
        "wrenz@managedvirtualservices.com,aggcomputers@aggdoors.com.au,ayel@aggdoors.com.au",
    ).split(",")
    if e.strip()
]
DIRECT_SHARE_ROLE = os.environ.get("DIRECT_SHARE_ROLE", "writer")

# Google Chat currently exposes user IDs, not display names, for this Space.
# These stable IDs were mapped from the user's sample time-log export.
SENDER_NAME_OVERRIDES = {
    # Google Chat hides display names for this MVS space, so map stable sender IDs.
    "users/107793674604203499940": "Aliyah Ayco",
    "users/111498914659577072555": "Wrenz Laylo",
    "users/113867615250966443965": "Elaissa / DREWS VA Trainee",
    # Space manager/admin; keep visible if they ever post a log.
    "users/107931712986491593833": "MVS Manager",
}

TIME_RE = r"(\d{1,2}:\d{2}|\d{3,4}|\d{1,2})"

EVENT_PATTERNS = [
    # Include common short forms and typos seen in chat logs.
    ("arrival", re.compile(r"\b(?:arrive|arrived|arrival|arive|arived|arrv|arrvd)\b\s*[-:]?\s*" + TIME_RE, re.I)),
    ("login", re.compile(r"\b(?:log\s*in|login|logged\s*in|logn|lgin|in|start|started)\b\s*[-:]?\s*" + TIME_RE, re.I)),
    ("lunch", re.compile(r"\b(?:lunch|luch|lnch|break|brk)\b\s*[-:]?\s*" + TIME_RE, re.I)),
    ("back", re.compile(r"\b(?:back|bck|bak|returned?|return|rtrn)\b\s*[-:]?\s*" + TIME_RE, re.I)),
    ("out", re.compile(r"\b(?:out|oyt|logout|log\s*out|clock\s*out|end|ended|done|finish|finished)\b\s*[-:]?\s*" + TIME_RE, re.I)),
]

@dataclass
class Entry:
    employee: str
    sender_id: str
    event: str
    minutes: int | None
    text: str
    created: datetime

@dataclass
class EmployeeDay:
    employee: str
    arrivals: list[int] = field(default_factory=list)
    logins: list[int] = field(default_factory=list)
    lunches: list[int] = field(default_factory=list)
    backs: list[int] = field(default_factory=list)
    outs: list[int] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)
    unclear: list[str] = field(default_factory=list)


def load_env_file() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def creds() -> Credentials:
    c = Credentials.from_authorized_user_file(str(TOKEN_PATH))
    if c.expired and c.refresh_token:
        c.refresh(Request())
        TOKEN_PATH.write_text(c.to_json(), encoding="utf-8")
    return c


RETRYABLE_GOOGLE_STATUSES = {429, 500, 502, 503, 504}
RETRYABLE_TRANSPORT_ERRORS = (
    TimeoutError,
    socket.timeout,
    ssl.SSLError,
    ConnectionError,
)


def execute_google(request, *, attempts: int = 5, label: str = "Google API") -> Any:
    """Execute a Google API request with retry/backoff for transient failures.

    GitHub Actions occasionally gets short-lived Sheets/Drive backend or transport
    failures. Without retry, one temporary HTTP 503 or socket read timeout marks the
    whole scheduled time-log job as failed even though a later attempt usually
    succeeds.
    """
    for attempt in range(1, attempts + 1):
        try:
            return request.execute()
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if status not in RETRYABLE_GOOGLE_STATUSES or attempt >= attempts:
                raise
            delay = min(60, 2 ** attempt)
            print(f"Warning: {label} returned HTTP {status}; retrying in {delay}s ({attempt}/{attempts})")
            time_module.sleep(delay)
        except RETRYABLE_TRANSPORT_ERRORS as e:
            if attempt >= attempts:
                raise
            delay = min(60, 2 ** attempt)
            print(f"Warning: {label} hit transient transport error {type(e).__name__}: {e}; retrying in {delay}s ({attempt}/{attempts})")
            time_module.sleep(delay)


def local_date_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time.min, tzinfo=TZ)
    end = start + timedelta(days=1)
    return start, end


def rfc3339(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_time_to_minutes(s: str) -> int | None:
    s = s.strip().lower().replace(" ", "")
    s = re.sub(r"[^0-9:]", "", s)
    if not s:
        return None
    if ":" in s:
        h, m = s.split(":", 1)
        if not h or not m:
            return None
        hour, minute = int(h), int(m[:2])
    else:
        if len(s) <= 2:
            hour, minute = int(s), 0
        elif len(s) == 3:
            hour, minute = int(s[0]), int(s[1:])
        elif len(s) == 5 and int(s[:2]) <= 23 and int(s[2:4]) <= 59:
            # Common typo in Chat logs: "12011" should be treated as 12:01.
            hour, minute = int(s[:2]), int(s[2:4])
        else:
            hour, minute = int(s[:-2]), int(s[-2:])
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def fmt_minutes(m: int | None) -> str:
    if m is None:
        return ""
    h, minute = divmod(m, 60)
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{minute:02d} {suffix}"


def duration_text(minutes: int | None) -> str:
    if minutes is None:
        return ""
    h, m = divmod(max(0, minutes), 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def extract_suffix_name(text: str) -> tuple[str | None, str]:
    # Handles messages like "Arrived 6:45 -W", "Lunch 10:01 - W", and "Out 4:00 A".
    m = re.search(r"\b-?\s*([WA])\s*$", text.strip(), re.I)
    if not m:
        return None, text
    code = m.group(1).upper()
    cleaned = re.sub(r"\b-?\s*[WA]\s*$", "", text.strip(), flags=re.I).strip()
    return SPECIAL_SUFFIX_NAMES.get(code), cleaned


def fetch_members(chat, space: str | None = None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    page_token = None
    parent = space_name(space)
    while True:
        kwargs = {"parent": parent, "pageSize": 1000}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = execute_google(chat.spaces().members().list(**kwargs), label="Chat members list")
        for mem in resp.get("memberships", []):
            member = mem.get("member", {})
            user_name = member.get("name", "")
            display = member.get("displayName") or member.get("formattedName") or user_name
            if user_name:
                mapping[user_name] = display
        page_token = resp.get("nextPageToken")
        if not page_token:
            return mapping


def fetch_messages(chat, start: datetime, end: datetime, space: str | None = None) -> list[dict[str, Any]]:
    filt = f'create_time > "{rfc3339(start)}" AND create_time < "{rfc3339(end)}"'
    messages: list[dict[str, Any]] = []
    page_token = None
    parent = space_name(space)
    while True:
        kwargs = {"parent": parent, "pageSize": 1000, "filter": filt, "orderBy": "create_time ASC"}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = execute_google(chat.spaces().messages().list(**kwargs), label="Chat messages list")
        messages.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return messages


def parse_entries(messages: list[dict[str, Any]], member_names: dict[str, str]) -> list[Entry]:
    entries: list[Entry] = []
    for m in messages:
        sender = m.get("sender", {})
        sender_id = sender.get("name", "")
        sender_name = SENDER_NAME_OVERRIDES.get(sender_id) or sender.get("displayName") or member_names.get(sender_id) or sender_id or "Unknown"
        text = (m.get("argumentText") or m.get("text") or "").strip()
        if not text:
            continue
        if re.match(r"^(Daily|Weekly) Time Log Summary\b", text, re.I):
            continue
        created = datetime.fromisoformat(m["createTime"].replace("Z", "+00:00")).astimezone(TZ)

        # Parse each line independently. MVS uses separate employee accounts, so
        # there is no shared-account suffix expansion by default.
        lines = [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()] or [text]
        for line in lines:
            override_name, clean_text = extract_suffix_name(line)
            employees = [override_name] if override_name else [sender_name]
            found_for_line = False
            for employee in employees:
                found = False
                for event, pat in EVENT_PATTERNS:
                    for match in pat.finditer(clean_text):
                        minutes = parse_time_to_minutes(match.group(1))
                        if minutes is not None:
                            entries.append(Entry(employee, sender_id, event, minutes, line, created))
                            found = True
                            found_for_line = True
                if not found:
                    # Keep unclear time-log-ish messages visible for review, including typo variants.
                    if re.search(r"\b(arrive|arive|arrv|log|lgin|lunch|luch|lnch|break|brk|back|bck|bak|out|oyt|start|end|done|finish)\b", line, re.I):
                        entries.append(Entry(employee, sender_id, "unclear", None, line, created))
    return entries


def group_day(entries: list[Entry]) -> dict[str, EmployeeDay]:
    out: dict[str, EmployeeDay] = {}
    for e in entries:
        rec = out.setdefault(e.employee, EmployeeDay(employee=e.employee))
        rec.raw.append(f"{e.created.strftime('%H:%M')} — {e.text}")
        if e.event == "arrival": rec.arrivals.append(e.minutes)  # type: ignore[arg-type]
        elif e.event == "login": rec.logins.append(e.minutes)  # type: ignore[arg-type]
        elif e.event == "lunch": rec.lunches.append(e.minutes)  # type: ignore[arg-type]
        elif e.event == "back": rec.backs.append(e.minutes)  # type: ignore[arg-type]
        elif e.event == "out": rec.outs.append(e.minutes)  # type: ignore[arg-type]
        else: rec.unclear.append(e.text)
    for rec in out.values():
        normalize_am_pm(rec)
    return dict(sorted(out.items(), key=lambda kv: kv[0].lower()))


def normalize_am_pm(rec: EmployeeDay) -> None:
    """Fix common noon ambiguity: Lunch 12:05, Back 1:00 means 1:00 PM."""
    adjusted_backs: list[int] = []
    for idx, back in enumerate(rec.backs):
        lunch = rec.lunches[idx] if idx < len(rec.lunches) else None
        if lunch is not None and lunch >= 12 * 60 and back < 4 * 60:
            back += 12 * 60
        adjusted_backs.append(back)
    rec.backs = adjusted_backs

    adjusted_outs: list[int] = []
    start = min(rec.logins or rec.arrivals or [0])
    for out_min in rec.outs:
        if start and out_min < start and out_min < 6 * 60:
            out_min += 12 * 60
        adjusted_outs.append(out_min)
    rec.outs = adjusted_outs


def calc_break_minutes(rec: EmployeeDay) -> int | None:
    if not rec.lunches or not rec.backs:
        return None
    total = 0
    for i, lunch in enumerate(rec.lunches):
        back = rec.backs[i] if i < len(rec.backs) else None
        if back is not None and back >= lunch:
            total += back - lunch
    return total if total else None


def effective_out_minutes(rec: EmployeeDay) -> int | None:
    if not rec.outs:
        return None
    return max(rec.outs)


def calc_work_minutes(rec: EmployeeDay) -> int | None:
    if not rec.outs or not (rec.logins or rec.arrivals):
        return None
    # Prefer login as official start. If no login is provided, fall back to arrival
    # so shared-account W/A entries can still produce totals while flagging missing login.
    start = min(rec.logins or rec.arrivals)
    end = effective_out_minutes(rec)
    if end is None or end < start:
        return None
    br = calc_break_minutes(rec) or 0
    return max(0, end - start - br)


def effective_arrival_minutes(rec: EmployeeDay) -> int | None:
    """Use the logged arrival, or infer arrival from login when arrival was forgotten."""
    if rec.arrivals:
        return min(rec.arrivals)
    if rec.logins:
        return min(rec.logins)
    return None


def employee_issues(rec: EmployeeDay) -> list[str]:
    issues = []
    # If an employee forgot to write "arrived" but did write "log in", HR wants
    # arrival marked the same as login instead of flagging the day as missing.
    if not rec.arrivals and not rec.logins: issues.append("missing arrival")
    if not rec.logins and rec.employee not in NO_LOGIN_REQUIRED:
        issues.append("missing login")
    if rec.lunches and len(rec.backs) < len(rec.lunches): issues.append("missing back from lunch")
    if rec.backs and len(rec.lunches) < len(rec.backs): issues.append("back without lunch")
    if not rec.outs: issues.append("missing out/logout")
    for i, lunch in enumerate(rec.lunches):
        if i >= len(rec.backs):
            continue
        back = rec.backs[i]
        if back >= lunch:
            br = back - lunch
            if br > 75:
                issues.append(f"long break {duration_text(br)}")
            elif br < 20:
                issues.append(f"short break {duration_text(br)}")
    issues.extend(rec.unclear)
    return issues


def day_rows(day: date, grouped: dict[str, EmployeeDay]) -> list[list[Any]]:
    rows: list[list[Any]] = [[
        "Date", "Employee", "Arrival", "Login", "Lunch start", "Back", "Break duration",
        "Out/Logout", "Out used for total", "Total hours worked", "Issues / Missing", "Raw entries"
    ]]
    for name, rec in grouped.items():
        rows.append([
            day.isoformat(),
            name,
            ", ".join(fmt_minutes(x) for x in sorted(rec.arrivals)),
            ", ".join(fmt_minutes(x) for x in sorted(rec.logins)),
            ", ".join(fmt_minutes(x) for x in sorted(rec.lunches)),
            ", ".join(fmt_minutes(x) for x in sorted(rec.backs)),
            duration_text(calc_break_minutes(rec)),
            ", ".join(fmt_minutes(x) for x in sorted(rec.outs)),
            fmt_minutes(effective_out_minutes(rec)),
            duration_text(calc_work_minutes(rec)),
            "; ".join(employee_issues(rec)),
            "\n".join(rec.raw),
        ])
    return rows

def drive_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def find_drive_item(drive, name: str, mime_type: str, parent_id: str | None = None) -> str | None:
    terms = [f"mimeType='{mime_type}'", f"name='{drive_escape(name)}'", "trashed=false"]
    if parent_id:
        terms.append(f"'{parent_id}' in parents")
    q = " and ".join(terms)
    resp = execute_google(drive.files().list(q=q, pageSize=1, fields="files(id,name,webViewLink)"), label="Drive files list")
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def set_permissions(drive, file_id: str) -> None:
    try:
        meta = execute_google(drive.files().get(fileId=file_id, fields="owners(emailAddress)"), label="Drive file metadata")
        owners = {o.get("emailAddress") for o in meta.get("owners", [])}
        perms = execute_google(drive.permissions().list(fileId=file_id, fields="permissions(id,emailAddress,role,type,domain)"), label="Drive permissions list")
        existing = perms.get("permissions", [])
        has_domain = False
        for p in existing:
            if p.get("type") == "domain" and p.get("domain") == SHARE_DOMAIN and p.get("role") == "reader":
                has_domain = True
                break

        if OWNER_EMAIL not in owners:
            # Google requires notification email for ownership transfers. This makes
            # Ayel the true owner instead of only an editor.
            execute_google(drive.permissions().create(
                fileId=file_id,
                body={"type": "user", "role": "owner", "emailAddress": OWNER_EMAIL},
                transferOwnership=True,
                sendNotificationEmail=True,
                emailMessage="Managed Virtual Services time-log automation transferred ownership to Ayel.",
                fields="id,role,emailAddress,pendingOwner,type",
            ), label="Drive transfer ownership")
        if not has_domain:
            execute_google(drive.permissions().create(
                fileId=file_id,
                body={"type": "domain", "role": "reader", "domain": SHARE_DOMAIN},
                sendNotificationEmail=False,
            ), label="Drive domain permission create")
        existing_emails = {p.get("emailAddress") for p in existing if p.get("type") == "user"}
        for email in DIRECT_SHARE_EMAILS:
            if email == OWNER_EMAIL or email in existing_emails:
                continue
            try:
                execute_google(drive.permissions().create(
                    fileId=file_id,
                    body={"type": "user", "role": DIRECT_SHARE_ROLE, "emailAddress": email},
                    # Cross-org and not-yet-Google-account emails may require notification.
                    sendNotificationEmail=True,
                ), label=f"Drive direct permission create {email}")
            except Exception as e:
                print(f"Warning: Failed direct share for {email} on {file_id}: {e}")
    except Exception as e:
        print(f"Warning: Failed to set permissions/ownership for {file_id}: {e}")


def ensure_folder(drive, name: str, parent_id: str | None = None) -> str:
    folder_mime = "application/vnd.google-apps.folder"
    existing = find_drive_item(drive, name, folder_mime, parent_id)
    if existing:
        set_permissions(drive, existing)
        return existing
    body = {"name": name, "mimeType": folder_mime}
    if parent_id:
        body["parents"] = [parent_id]
    fid = execute_google(drive.files().create(body=body, fields="id"), label="Drive folder create")["id"]
    set_permissions(drive, fid)
    return fid


def week_bounds(target: date) -> tuple[date, date]:
    # Work weeks run Saturday through Friday, including Sunday, matching the AGG setup.
    # Python weekday(): Monday=0 ... Saturday=5, Sunday=6.
    week_start = target - timedelta(days=(target.weekday() - 5) % 7)
    return week_start, week_start + timedelta(days=6)


def folder_for_date(drive, target: date) -> str:
    root = ensure_folder(drive, ROOT_FOLDER_NAME)
    month = ensure_folder(drive, f"{target:%Y-%m} {target:%B}", root)
    monday, friday = week_bounds(target)
    return ensure_folder(drive, f"Week {monday.isoformat()} to {friday.isoformat()}", month)


def folder_for_day(drive, target: date) -> str:
    week_folder_id = folder_for_date(drive, target)
    return ensure_folder(drive, f"Day {target.isoformat()}", week_folder_id)


def drive_folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def folder_for_employee(drive, parent_folder_id: str, employee: str | None = None) -> str:
    return ensure_folder(drive, safe_title_part(employee) if employee else "All Employees", parent_folder_id)


def find_spreadsheet(drive, title: str, parent_id: str | None = None) -> str | None:
    return find_drive_item(drive, title, "application/vnd.google-apps.spreadsheet", parent_id)


def col_letter(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out or "A"


def create_or_update_sheet(sheets, drive, title: str, tab: str, rows: list[list[Any]], parent_id: str | None = None, value_input_option: str = "RAW") -> tuple[str, str]:
    sid = find_spreadsheet(drive, title, parent_id)
    if sid is None:
        resp = execute_google(sheets.spreadsheets().create(body={"properties": {"title": title}, "sheets": [{"properties": {"title": tab}}]}), label="Sheets spreadsheet create")
        sid = resp["spreadsheetId"]
        if parent_id:
            execute_google(drive.files().update(fileId=sid, addParents=parent_id, removeParents="root", fields="id,parents"), label="Drive move spreadsheet")
    meta = execute_google(sheets.spreadsheets().get(spreadsheetId=sid), label="Sheets spreadsheet metadata")
    tabs = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if tab not in tabs:
        execute_google(sheets.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [{"addSheet": {"properties": {"title": tab}}}]}), label="Sheets add tab")
    width = max((len(r) for r in rows), default=1)
    last_col = col_letter(width)
    rng = f"'{tab}'!A1:{last_col}{max(1, len(rows))}"
    execute_google(sheets.spreadsheets().values().clear(spreadsheetId=sid, range=f"'{tab}'!A:{last_col}", body={}), label="Sheets clear values")
    execute_google(sheets.spreadsheets().values().update(spreadsheetId=sid, range=rng, valueInputOption=value_input_option, body={"values": rows}), label="Sheets update values")
    set_permissions(drive, sid)
    url = f"https://docs.google.com/spreadsheets/d/{sid}/edit"
    return sid, url


def build_day_summary(day: date, grouped: dict[str, EmployeeDay], sheet_url: str, day_folder_url: str) -> str:
    lines = [f"Daily Time Log Summary — {day.isoformat()}", ""]
    if not grouped:
        lines.append("No time-log messages found for today.")
    else:
        for name, rec in grouped.items():
            parts = []
            if rec.arrivals: parts.append(f"arrived {fmt_minutes(min(rec.arrivals))}")
            if rec.logins: parts.append(f"logged in {fmt_minutes(min(rec.logins))}")
            if rec.lunches: parts.append(f"lunch {fmt_minutes(min(rec.lunches))}")
            if rec.backs: parts.append(f"back {fmt_minutes(max(rec.backs))}")
            br = calc_break_minutes(rec)
            if br is not None: parts.append(f"break {duration_text(br)}")
            work = calc_work_minutes(rec)
            if work is not None:
                parts.append(f"total {duration_text(work)}")
            else:
                parts.append("total incomplete")
            lines.append(f"• {name}: " + ("; ".join(parts) if parts else "entries found, needs review"))
    lines += ["", f"Day folder: {day_folder_url}", f"All Employees sheet: {sheet_url}"]
    return "\n".join(lines)


def post_webhook(text: str) -> bool:
    load_env_file()
    url = os.getenv(WEBHOOK_ENV, "").strip()
    if not url or "..." in url or not url.startswith("https://"):
        print(f"Warning: Missing or invalid {WEBHOOK_ENV}; skipped Google Chat post")
        return False
    print(f"Webhook check: length={len(url)}, has_key={'key=' in url}, has_token={'token=' in url}")
    data = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        print(f"Warning: Google Chat webhook post failed with HTTP {e.code}: {e.reason}. Body: {body}")
        return False
    except Exception as e:
        print(f"Warning: Google Chat webhook post failed: {type(e).__name__}: {e}")
        return False


def parse_hhmm(value: str) -> int:
    h, m = value.split(":", 1)
    return int(h) * 60 + int(m)


def existing_summary_post_after_threshold(chat, target: date, mode: str, threshold_hhmm: str) -> bool:
    """Return True when a matching summary was already posted at/after threshold.

    Earlier manual/test summaries do not block the scheduled post. Example: a daily
    summary posted at 8:00 AM will not suppress the 4:20 PM scheduled post, but a
    summary posted at/after 4:05 PM will suppress duplicate posting.
    """
    start, end = local_date_bounds(target)
    threshold = parse_hhmm(threshold_hhmm)
    if mode == "weekly":
        monday, friday = week_bounds(target)
        title = f"Weekly Time Log Summary — {monday.isoformat()} to {friday.isoformat()}"
    else:
        title = f"Daily Time Log Summary — {target.isoformat()}"
    for m in fetch_messages(chat, start, end):
        text = (m.get("argumentText") or m.get("text") or "").strip()
        if not text.startswith(title):
            continue
        created = datetime.fromisoformat(m["createTime"].replace("Z", "+00:00")).astimezone(TZ)
        created_minutes = created.hour * 60 + created.minute
        if created_minutes >= threshold:
            print(f"Skipped Google Chat post: {mode} summary already posted at/after {threshold_hhmm} for {target.isoformat()}")
            return True
    return False


def safe_title_part(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9 ._-]+", "", s).strip()
    return re.sub(r"\s+", " ", s) or "Unknown"


def weekly_title(week_start: date, week_end: date, employee: str) -> str:
    who = safe_title_part(employee)
    return f"MVS Time Sheet - Week {week_start.isoformat()} to {week_end.isoformat()} - {who}"



def gsheet_time(minutes: int | None) -> str:
    if minutes is None:
        return "00:00:00"
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


def weekly_timesheet_rows(employee: str, week_start: date, week_end: date, by_day: dict[date, EmployeeDay]) -> list[list[Any]]:
    rows = [["" for _ in range(17)] for _ in range(21)]
    # Header/meta section matching the provided Excel timesheet layout.
    rows[6][0] = "Employee Name"
    rows[6][2] = employee
    rows[6][9] = f"Date: {week_start:%b %d} - {week_end:%b %d, %Y}"
    rows[7][0] = "Company"
    rows[7][2] = COMPANY_NAME
    rows[8][0] = "Staff No."
    rows[12] = [
        "", "DATE", "WEEKDAY", "ARRIVAL TIME", "START ", "BREAK", "TIME IN", "TIME OUT",
        "TIME IN ", "FINISH", "TOTAL ", "REGULAR HOURS", "NOTES", "OVERTIME WEEKDAYS",
        "SATURDAYS", "HOLIDAY", "SUNDAYS",
    ]
    for idx in range(7):
        d = week_start + timedelta(days=idx)
        r = 13 + idx
        rec = by_day.get(d)
        row_no = r + 1
        rows[r][1] = d.isoformat()
        rows[r][2] = d.strftime("%A")
        if rec:
            start = min(rec.logins or rec.arrivals) if (rec.logins or rec.arrivals) else None
            finish = effective_out_minutes(rec)
            complete = start is not None and finish is not None
            rows[r][3] = gsheet_time(effective_arrival_minutes(rec))
            rows[r][4] = gsheet_time(start)
            # Keep the template column names unchanged. Use the first break/lunch
            # pair in BREAK + first TIME IN, and the second pair in TIME OUT +
            # second TIME IN.
            rows[r][5] = gsheet_time(rec.lunches[0] if len(rec.lunches) >= 1 else None)
            rows[r][6] = gsheet_time(rec.backs[0] if len(rec.backs) >= 1 else None)
            rows[r][7] = gsheet_time(rec.lunches[1] if len(rec.lunches) >= 2 else None)
            rows[r][8] = gsheet_time(rec.backs[1] if len(rec.backs) >= 2 else None)
            rows[r][9] = gsheet_time(finish)
            rows[r][10] = (
                f"=IF(F{row_no}=0,J{row_no}-E{row_no},"
                f"IF(H{row_no}=0,(F{row_no}-E{row_no})+(J{row_no}-G{row_no}),"
                f"(F{row_no}-E{row_no})+(H{row_no}-G{row_no})+(J{row_no}-I{row_no})))"
            ) if complete else ""
            rows[r][12] = "; ".join(employee_issues(rec))
            # HR wants these hour-allocation columns as decimal hours, not hh:mm.
            if d.weekday() < 5 and complete:
                rows[r][11] = f"=MIN(8,K{row_no}*24)"
                rows[r][13] = f"=MAX(0,K{row_no}*24-L{row_no})"
            elif d.weekday() == 5 and complete:
                rows[r][14] = f"=K{row_no}*24"
            elif d.weekday() == 6 and complete:
                rows[r][16] = f"=K{row_no}*24"
        else:
            rows[r][3] = "00:00:00"
            rows[r][4] = "00:00:00"
            rows[r][5] = "00:00:00"
            rows[r][6] = "00:00:00"
            rows[r][7] = "00:00:00"
            rows[r][8] = "00:00:00"
            rows[r][9] = "00:00:00"
    total_r = 20
    rows[total_r][12] = "TOTAL"
    rows[total_r][11] = "=SUM(L14:L20)"
    rows[total_r][13] = "=SUM(N14:N20)"
    rows[total_r][14] = "=SUM(O14:O20)"
    rows[total_r][15] = "=SUM(P14:P20)"
    rows[total_r][16] = "=SUM(Q14:Q20)"
    return rows


def get_sheet_id(sheets, spreadsheet_id: str, tab: str) -> int:
    meta = execute_google(sheets.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets(properties(sheetId,title))"), label="Sheets tab metadata")
    for sh in meta.get("sheets", []):
        props = sh.get("properties", {})
        if props.get("title") == tab:
            return props["sheetId"]
    raise RuntimeError(f"Sheet tab not found: {tab}")


def apply_weekly_template_format(sheets, spreadsheet_id: str, tab: str) -> None:
    sheet_id = get_sheet_id(sheets, spreadsheet_id, tab)
    def color(hex_rgb: str) -> dict[str, float]:
        hex_rgb = hex_rgb.lstrip('#')
        return {"red": int(hex_rgb[0:2], 16)/255, "green": int(hex_rgb[2:4], 16)/255, "blue": int(hex_rgb[4:6], 16)/255}
    requests: list[dict[str, Any]] = []
    # Clear previous merges and recreate core template merges.
    requests.append({"unmergeCells": {"range": {"sheetId": sheet_id}}})
    for sr, er, sc, ec in [(6,7,0,2),(6,7,2,9),(6,9,9,11),(7,8,0,2),(7,8,2,9),(8,9,0,2),(8,9,2,9)]:
        requests.append({"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": sr, "endRowIndex": er, "startColumnIndex": sc, "endColumnIndex": ec}, "mergeType": "MERGE_ALL"}})
    # Freeze header area.
    requests.append({"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 13}}, "fields": "gridProperties.frozenRowCount"}})
    # Column widths similar to Excel template.
    widths = {0: 45, 1: 135, 2: 120, 3: 140, 4: 100, 5: 100, 6: 100, 7: 100, 8: 100, 9: 100, 10: 100, 11: 115, 12: 180, 13: 130, 14: 100, 15: 100, 16: 100}
    for col, px in widths.items():
        requests.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": col, "endIndex": col+1}, "properties": {"pixelSize": px}, "fields": "pixelSize"}})
    # Header row colors.
    header_colors = {
        1: 'EA9999', 2: 'A4C2F4', 3: 'FF00FF', 4: 'B6D7A8', 5: 'F9CB9C', 6: 'D5A6BD', 7: 'D5A6BD',
        8: 'B6D7A8', 9: 'A4C2F4', 10: 'FFE599', 11: '00FF00', 12: 'FF0000', 13: 'D9D9D9', 14: 'D9D9D9', 15: 'D9D9D9', 16: 'D9D9D9'
    }
    # Base formatting for A7:Q21.
    requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 6, "endRowIndex": 21, "startColumnIndex": 0, "endColumnIndex": 17}, "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP", "textFormat": {"fontFamily": "Arial", "fontSize": 10}, "borders": {"top": {"style": "SOLID"}, "bottom": {"style": "SOLID"}, "left": {"style": "SOLID"}, "right": {"style": "SOLID"}}}}, "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment,wrapStrategy,textFormat,borders)"}})
    for col, rgb in header_colors.items():
        requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 12, "endRowIndex": 13, "startColumnIndex": col, "endColumnIndex": col+1}, "cell": {"userEnteredFormat": {"backgroundColor": color(rgb), "textFormat": {"bold": True}}}, "fields": "userEnteredFormat(backgroundColor,textFormat.bold)"}})
    # Bold meta labels and employee/date cells.
    requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 6, "endRowIndex": 9, "startColumnIndex": 0, "endColumnIndex": 11}, "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}}, "fields": "userEnteredFormat.textFormat.bold"}})
    # Time/date/decimal number formats.
    requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 13, "endRowIndex": 20, "startColumnIndex": 1, "endColumnIndex": 2}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}}, "fields": "userEnteredFormat.numberFormat"}})
    requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 13, "endRowIndex": 20, "startColumnIndex": 3, "endColumnIndex": 11}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "TIME", "pattern": "hh:mm"}}}, "fields": "userEnteredFormat.numberFormat"}})
    # HR allocation columns are decimal hours: REGULAR HOURS, OVERTIME, SATURDAYS, HOLIDAY, SUNDAYS.
    for col in (11, 13, 14, 15, 16):
        requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 13, "endRowIndex": 21, "startColumnIndex": col, "endColumnIndex": col+1}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0.00"}}}, "fields": "userEnteredFormat.numberFormat"}})
    execute_google(sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}), label="Sheets format batchUpdate")


def collect_week_employee_records(chat, target: date, member_names: dict[str, str]) -> tuple[date, date, dict[str, dict[date, EmployeeDay]]]:
    monday, saturday = week_bounds(target)
    records: dict[str, dict[date, EmployeeDay]] = defaultdict(dict)
    d = monday
    while d <= saturday:
        start, end = local_date_bounds(d)
        grouped = group_day(parse_entries(fetch_messages(chat, start, end), member_names))
        for name, rec in grouped.items():
            records[name][d] = rec
        d += timedelta(days=1)
    return monday, saturday, records


def probe_chat_access() -> str:
    c = creds()
    chat = build("chat", "v1", credentials=c)
    member_names = fetch_members(chat)
    lines = [f"Chat access OK for {space_name()}", "", "Members visible to API:"]
    for user_id, display in sorted(member_names.items(), key=lambda kv: kv[1].lower()):
        lines.append(f"- {display}: {user_id}")
    if not member_names:
        lines.append("- No members returned; add sender IDs to SENDER_NAME_OVERRIDES after a message read test if names are hidden.")
    return "\n".join(lines)


def process_weekly_employee_timesheets(target: date) -> str:
    c = creds()
    chat = build("chat", "v1", credentials=c)
    sheets = build("sheets", "v4", credentials=c)
    drive = build("drive", "v3", credentials=c)
    member_names = fetch_members(chat)
    monday, saturday, records = collect_week_employee_records(chat, target, member_names)
    week_folder_id = folder_for_date(drive, target)
    created: list[str] = []
    for employee in sorted(records):
        employee_folder_id = folder_for_employee(drive, week_folder_id, employee)
        title = weekly_title(monday, saturday, employee)
        rows = weekly_timesheet_rows(employee, monday, saturday, records[employee])
        sid, url = create_or_update_sheet(sheets, drive, title, "Weekly Timesheet", rows, employee_folder_id, value_input_option="USER_ENTERED")
        apply_weekly_template_format(sheets, sid, "Weekly Timesheet")
        created.append(f"• {employee}: {url}")

    lines = [f"Weekly employee timesheets updated — {monday.isoformat()} to {saturday.isoformat()}", "", f"Week folder: {drive_folder_url(week_folder_id)}"]
    if created:
        lines += ["", "Employee sheets:"] + created
    else:
        lines.append("No employee logs found for this week yet.")
    return "\n".join(lines)


def week_catchup_dates(target: date) -> list[date]:
    """Dates from Saturday through target, covering the full Sat-Fri work week."""
    week_start, _ = week_bounds(target)
    days = []
    d = week_start
    while d <= target:
        days.append(d)
        d += timedelta(days=1)
    return days


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["daily", "weekly", "probe"], default="daily")
    p.add_argument("--date", help="Target date YYYY-MM-DD. Defaults to today in Asia/Manila.")
    p.add_argument("--catch-up", action="store_true", help="Accepted for compatibility; MVS updates the weekly employee timesheets.")
    args = p.parse_args()
    target = date.fromisoformat(args.date) if args.date else datetime.now(TZ).date()
    if args.mode == "probe":
        print(probe_chat_access())
        return
    # MVS mode: only create/update employee weekly Sheets. No Google Chat posting.
    print(process_weekly_employee_timesheets(target))

if __name__ == "__main__":
    main()
