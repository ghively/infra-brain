#!/usr/bin/env python3
"""Store infra-brain dashboard admin credential in 1Password.

Password is read from .env and passed to `op` via stdin JSON only — never on
argv, never printed. Verification reads the password back from 1Password via
the item JSON (NOT `--field password`, which returns mangled output on op
2.38.1) and authenticates against the live app; only status lines are printed.
"""
import json
import subprocess
import sys
import urllib.error
import urllib.request

ENV_PATH = "/home/operator/AgentsStuff/infra-brain/.env"
VAULT = "PersonalVault"
TITLE = "infra-brain Dashboard Admin"
APP_URL = "http://203.0.113.19:8001/dashboard2/"
LOGIN_URL = "http://localhost:8001/api/dashboard/login"
NOTE = ("ai_node tailnet only. Login lockout is 5 failed attempts per 5 minutes, "
        "returns HTTP 429.")


def run(cmd, inp=None):
    return subprocess.run(cmd, input=inp, capture_output=True, text=True)


def op_item_json():
    r = run(["op", "item", "get", TITLE, "--vault", VAULT, "--format", "json"])
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)


def main():
    # 1. Read ADMIN_PASSWORD from .env (never echoed).
    pw = None
    with open(ENV_PATH) as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("ADMIN_PASSWORD="):
                pw = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not pw:
        print("ITEM=error: ADMIN_PASSWORD not found in .env")
        sys.exit(1)

    # 2. Build login item JSON (note as both a field and top-level key so op
    #    actually persists it).
    item = {
        "category": "LOGIN",
        "title": TITLE,
        "notesPlain": NOTE,
        "fields": [
            {"id": "username", "type": "STRING", "value": "admin",
             "purpose": "USERNAME"},
            {"id": "password", "type": "CONCEALED", "value": pw,
             "purpose": "PASSWORD"},
            {"id": "notesPlain", "type": "STRING", "value": NOTE,
             "purpose": "NOTES"},
        ],
        "urls": [{"href": APP_URL}],
    }
    payload = json.dumps(item)

    # 3. Create or update, password via stdin.
    exists = op_item_json() is not None
    if exists:
        r = run(["op", "item", "edit", TITLE, "--vault", VAULT, "-"], inp=payload)
        status = "updated"
    else:
        r = run(["op", "item", "create", "--vault", VAULT, "-"], inp=payload)
        status = "created"
    if r.returncode != 0:
        print("ITEM=error: " + r.stderr.strip()[:300])
        sys.exit(1)

    # 4. Read password back via item JSON (--field password is unreliable).
    item = op_item_json()
    stored = None
    if item is not None:
        stored = next(
            (f.get("value") for f in item.get("fields", [])
             if f.get("id") == "password"), None)
    if not stored:
        print("ITEM=" + status)
        print("STORED_PW_AUTHENTICATES=no")
        print("LOGIN_RESPONSE=readback failed")
        sys.exit(1)

    # 5. Authenticate against the live app; password in request body only.
    body = json.dumps({"username": "admin", "password": stored}).encode()
    req = urllib.request.Request(
        LOGIN_URL, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    raw = None
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
    except Exception as e:  # noqa: BLE001
        print("ITEM=" + status)
        print("STORED_PW_AUTHENTICATES=no")
        print("LOGIN_RESPONSE=request error: " + str(e)[:120])
        sys.exit(1)

    auth = None
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "authenticated" in data:
            auth = bool(data["authenticated"])
    except (ValueError, TypeError):
        if "true" in raw.lower():
            auth = True
        elif "false" in raw.lower():
            auth = False

    print("ITEM=" + status)
    if auth is True:
        print("STORED_PW_AUTHENTICATES=yes")
        print("LOGIN_RESPONSE=authenticated true")
    elif auth is False:
        print("STORED_PW_AUTHENTICATES=no")
        print("LOGIN_RESPONSE=authenticated false")
    else:
        print("STORED_PW_AUTHENTICATES=no")
        print("LOGIN_RESPONSE=" + raw[:200])


if __name__ == "__main__":
    main()
