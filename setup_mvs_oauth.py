#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

ROOT = Path(__file__).resolve().parent
HERMES_HOME = Path.home() / "AppData" / "Local" / "hermes"
CLIENT_SECRET = Path(os.environ.get("MVS_GOOGLE_CLIENT_SECRET", str(ROOT / "google_client_secret_mvs.json")))
TOKEN_PATH = ROOT / "google_token_mvs.json"
PENDING_PATH = ROOT / "google_oauth_pending_mvs.json"
REDIRECT_URI = "http://localhost:1"
SCOPES = [
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.memberships.readonly",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


def make_flow() -> Flow:
    if not CLIENT_SECRET.exists():
        raise SystemExit(f"Missing OAuth client secret: {CLIENT_SECRET}")
    return Flow.from_client_secrets_file(str(CLIENT_SECRET), scopes=SCOPES, redirect_uri=REDIRECT_URI)


def auth_url() -> None:
    flow = make_flow()
    url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="false",
        prompt="consent select_account",
    )
    PENDING_PATH.write_text(json.dumps({"state": state, "code_verifier": getattr(flow, "code_verifier", None)}, indent=2), encoding="utf-8")
    print(url)


def auth_code(code_or_url: str) -> None:
    flow = make_flow()
    if PENDING_PATH.exists():
        try:
            pending = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
            flow.oauth2session.state = pending.get("state")
            if pending.get("code_verifier"):
                flow.code_verifier = pending["code_verifier"]
        except Exception:
            pass
    flow.fetch_token(authorization_response=code_or_url if code_or_url.startswith("http") else None, code=None if code_or_url.startswith("http") else code_or_url)
    creds = flow.credentials
    payload = json.loads(creds.to_json())
    payload["type"] = "authorized_user"
    payload["scopes"] = SCOPES
    TOKEN_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if PENDING_PATH.exists():
        PENDING_PATH.unlink()
    print(f"Saved MVS token to {TOKEN_PATH}")


def check() -> None:
    if not TOKEN_PATH.exists():
        raise SystemExit(f"NOT_AUTHENTICATED: {TOKEN_PATH}")
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), scopes=SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        payload = json.loads(creds.to_json())
        payload["type"] = "authorized_user"
        payload["scopes"] = SCOPES
        TOKEN_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"AUTHENTICATED: valid={creds.valid} token={TOKEN_PATH}")


def main() -> None:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--auth-url", action="store_true")
    g.add_argument("--auth-code")
    g.add_argument("--check", action="store_true")
    args = p.parse_args()
    if args.auth_url:
        auth_url()
    elif args.auth_code:
        auth_code(args.auth_code)
    elif args.check:
        check()


if __name__ == "__main__":
    main()
