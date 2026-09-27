"""
9router Import Tool — Local Server
Import Codex OAuth connections into both 9router SQLite and n9router db.json.
"""

import sqlite3
import json
import os
import sys
import shutil
import uuid
import base64
import hashlib
import secrets
import webbrowser
import threading
import time
import socketserver
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode
try:
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError
except ImportError:
    from urllib2 import Request, urlopen, URLError, HTTPError

# Force UTF-8 console output on Windows, otherwise logging emoji/arrow output
# from auto_login.py can crash with cp1252 charmap errors.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PORT = int(os.environ.get("SHOPTAIKHOAN_PORT", "9876"))
SOCKET_PATH = os.environ.get("SHOPTAIKHOAN_SOCKET", "")
PUBLIC_URL = os.environ.get("SHOPTAIKHOAN_PUBLIC_URL", "")
OAUTH_CALLBACK_PORT = 1455
PROVIDER = "codex"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.environ.get("SHOPTAIKHOAN_DATA_DIR", SCRIPT_DIR)
AUTO_LOGIN_EXECUTABLE = os.environ.get("SHOPTAIKHOAN_AUTO_LOGIN_EXECUTABLE", "")
try:
    os.makedirs(RUNTIME_DIR, exist_ok=True)
except OSError:
    RUNTIME_DIR = SCRIPT_DIR
WEB_LOGIN_HISTORY_FILE = os.path.join(RUNTIME_DIR, "web_login_history.json")
WEB_LOGIN_HISTORY_LIMIT = 500
WEB_LOGIN_STATE_FILE = os.path.join(RUNTIME_DIR, "web_login_state.json")
DEFAULT_WEB_LOGIN_LINK = "https://chatgpt.com/api/auth/session"

# OAuth config — same as 9router/Codex CLI
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_SCOPE = "openid profile email offline_access"
OAUTH_REDIRECT_URI = "http://localhost:{}/auth/callback".format(OAUTH_CALLBACK_PORT)

# Pending OAuth state
_oauth_pending = {}  # state -> {code_verifier, created_at}

# Storage locations:
# - 9router SQLite: ~/.9router/db/data.sqlite
# - 9router JSON:   ~/.9router/data/db.json
# - n9router JSON:  ~/.n9router/db.json
def find_sqlite():
    candidates = [
        os.path.join(os.path.expanduser("~"), ".9router", "db", "data.sqlite"),
        os.path.join(os.environ.get("APPDATA", ""), "9router", "db", "data.sqlite"),
        os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "9router", "db", "data.sqlite"),
        os.path.join(os.path.expanduser("~"), "Library", "Application Support", "9router", "db", "data.sqlite"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]


def unique_paths(paths):
    seen = set()
    out = []
    for p in paths:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def normalize_web_login_link(supplied_link):
    """Return a validated optional secondary URL; blank explicitly disables it."""
    if supplied_link is None:
        return DEFAULT_WEB_LOGIN_LINK
    link_url = str(supplied_link).strip()
    if not link_url:
        return ""
    parsed_link = urlparse(link_url)
    if parsed_link.scheme not in ("http", "https") or not parsed_link.netloc:
        raise ValueError("Link phải bắt đầu bằng http:// hoặc https://")
    return link_url


def resolve_web_login_link(payload):
    """Resolve the optional secondary tab from an explicit UI toggle and URL."""
    if not isinstance(payload, dict):
        raise ValueError("Invalid web login request")
    if payload.get("openLinkEnabled") is False:
        return ""
    supplied_link = payload.get("linkUrl") if "linkUrl" in payload else payload.get("link")
    return normalize_web_login_link(supplied_link)


def get_json_candidates():
    return unique_paths([
        # 9router v0.5+ root-level db.json (PRIMARY — used by current 9router)
        os.path.join(os.environ.get("APPDATA", ""), "9router", "db.json"),
        os.path.join(os.path.expanduser("~"), ".9router", "db.json"),
        # 9router JSON layouts seen in older versions
        os.path.join(os.path.expanduser("~"), ".9router", "data", "db.json"),
        os.path.join(os.environ.get("APPDATA", ""), "9router", "data", "db.json"),
        os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "9router", "data", "db.json"),
        os.path.join(os.path.expanduser("~"), "Library", "Application Support", "9router", "data", "db.json"),
        # n9router JSON layouts
        os.path.join(os.path.expanduser("~"), ".n9router", "db.json"),
        os.path.join(os.environ.get("APPDATA", ""), "n9router", "db.json"),
        os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "n9router", "db.json"),
        os.path.join(os.path.expanduser("~"), "Library", "Application Support", "n9router", "db.json"),
    ])


def find_json_paths():
    return [p for p in get_json_candidates() if os.path.exists(p)]


SQLITE_PATH = find_sqlite()
JSON_PATHS = find_json_paths()
DB_JSON_PATH = JSON_PATHS[0] if JSON_PATHS else get_json_candidates()[0]
BACKUP_ROOT = os.path.join(RUNTIME_DIR, "backups")
# OAuth workers may finish at the same time. Protect the full mutation and
# verification sequence so SQLite and JSON always stay consistent.
_storage_lock = threading.RLock()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sqlite_exists():
    return bool(SQLITE_PATH and os.path.exists(SQLITE_PATH))


def json_exists():
    return bool(JSON_PATHS)


def storage_exists():
    return sqlite_exists() or json_exists()


def backup_file(path, label, ext):
    if not path or not os.path.exists(path):
        return None
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bk = os.path.join(BACKUP_ROOT, "backup-{}-{}.{}".format(label, ts, ext))
    shutil.copy2(path, bk)
    return bk


def build_data_blob(conn):
    now = now_iso()
    raw_refresh = conn.get("refreshToken") or ""
    if isinstance(raw_refresh, str) and raw_refresh.startswith("eyJ"):
        raw_refresh = ""
    return {
        "accessToken": conn.get("accessToken") or "",
        "refreshToken": raw_refresh,
        "idToken": conn.get("idToken") or "",
        "expiresAt": conn.get("expiresAt") or "",
        "expiresIn": conn.get("expiresIn") if isinstance(conn.get("expiresIn"), int) else 0,
        "testStatus": conn.get("testStatus") or "active",
        "lastUsedAt": conn.get("lastUsedAt") or now,
        "consecutiveUseCount": 0,
        "backoffLevel": 0,
        "providerSpecificData": conn.get("providerSpecificData") or {},
        "lastError": None,
        "lastErrorAt": None,
    }


def build_json_connection(conn, priority=None, existing=None):
    now = now_iso()
    email_val = conn.get("email") or ""
    name = conn.get("name") or email_val or "Unknown"
    base = dict(existing or {})
    base.update({
        "id": str(uuid.uuid4()),
        "provider": PROVIDER,
        "authType": "oauth",
        "name": name,
        "email": email_val,
        "priority": priority if priority is not None else base.get("priority", 1),
        "isActive": True,
        "updatedAt": now,
    })
    base.update(build_data_blob(conn))
    if not base.get("createdAt"):
        base["createdAt"] = now
    return base


def load_db_json(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("providerConnections"), list):
        data["providerConnections"] = []
    return data


def save_db_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get_sqlite_connections():
    if not sqlite_exists():
        return []
    db = sqlite3.connect(SQLITE_PATH, timeout=30)
    cur = db.cursor()
    cur.execute("SELECT id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt FROM providerConnections WHERE provider=? ORDER BY priority", (PROVIDER,))
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    result = []
    for r in rows:
        row = dict(zip(cols, r))
        try:
            row["data"] = json.loads(row["data"]) if row["data"] else {}
        except Exception:
            row["data"] = {}
        result.append(row)
    db.close()
    return result


def get_json_connections():
    if not JSON_PATHS:
        return []
    data = load_db_json(JSON_PATHS[0])
    if not data:
        return []
    result = []
    for conn in data.get("providerConnections", []):
        if conn.get("provider") == PROVIDER:
            row = dict(conn)
            row["data"] = build_data_blob(row)
            result.append(row)
    return sorted(result, key=lambda x: int(x.get("priority") or 999999))


def get_connections():
    json_conns = get_json_connections()
    sqlite_conns = get_sqlite_connections()
    # Prefer showing n9router when available, but keep SQLite visible as fallback.
    return json_conns or sqlite_conns


def import_connections_sqlite(connections):
    if not sqlite_exists():
        return 0, 0, ["9router SQLite not found: {}".format(SQLITE_PATH)]

    backup_file(SQLITE_PATH, "9router-sqlite", "sqlite")
    db = sqlite3.connect(SQLITE_PATH, timeout=30)
    cur = db.cursor()
    cur.execute("SELECT id, email, priority FROM providerConnections WHERE provider=?", (PROVIDER,))
    existing_map = {}
    for row in cur.fetchall():
        if row[1]:
            existing_map[row[1].lower().strip()] = {"id": row[0], "priority": row[2]}

    inserted = 0
    replaced = 0
    errors = []
    now = now_iso()
    # New accounts append after the current last Codex connection. Existing
    # accounts keep their current position when their tokens are replaced.
    cur.execute("SELECT COALESCE(MAX(priority), 0) FROM providerConnections WHERE provider=?", (PROVIDER,))
    next_new_priority = int(cur.fetchone()[0] or 0) + 1
    for conn in connections:
        email = (conn.get("email") or conn.get("name") or "").lower().strip()
        name = conn.get("name") or conn.get("email") or "Unknown"
        email_val = conn.get("email") or ""
        data_json = json.dumps(build_data_blob(conn), ensure_ascii=False)
        try:
            if email and email in existing_map:
                old = existing_map[email]
                cur.execute("DELETE FROM providerConnections WHERE id=? AND provider=?", (old["id"], PROVIDER))
                new_id = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO providerConnections (id,provider,authType,name,email,priority,isActive,data,createdAt,updatedAt) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (new_id, PROVIDER, "oauth", name, email_val, old["priority"], 1, data_json, now, now))
                replaced += 1
            else:
                new_id = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO providerConnections (id,provider,authType,name,email,priority,isActive,data,createdAt,updatedAt) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (new_id, PROVIDER, "oauth", name, email_val, next_new_priority, 1, data_json, now, now))
                next_new_priority += 1
                inserted += 1
        except Exception as e:
            errors.append("sqlite {}: {}".format(email_val, str(e)))

    db.commit()
    db.close()
    return inserted, replaced, errors


def import_connections_json_path(connections, json_path):
    data = load_db_json(json_path)
    if data is None:
        return 0, 0, ["JSON db not found: {}".format(json_path)]

    backup_file(json_path, "json-db", "json")
    all_conns = data.get("providerConnections", [])
    existing_codex = []
    other_conns = []
    existing_map = {}
    for conn in all_conns:
        if conn.get("provider") == PROVIDER:
            existing_codex.append(conn)
            email = (conn.get("email") or conn.get("name") or "").lower().strip()
            if email:
                existing_map[email] = conn
        else:
            other_conns.append(conn)

    inserted = 0
    replaced = 0
    errors = []
    incoming = []
    incoming_keys = set()
    for conn in connections:
        try:
            email = (conn.get("email") or conn.get("name") or "").lower().strip()
            if not email:
                errors.append("json Missing email")
                continue
            incoming_keys.add(email)
            old = existing_map.get(email)
            if old:
                incoming.append(build_json_connection(conn, priority=old.get("priority", 1), existing=old))
                replaced += 1
            else:
                incoming.append(build_json_connection(conn, priority=1))
                inserted += 1
        except Exception as e:
            errors.append("json {}: {}".format(conn.get("email") or "?", str(e)))

    kept_codex = []
    for conn in existing_codex:
        email = (conn.get("email") or conn.get("name") or "").lower().strip()
        if email not in incoming_keys:
            kept_codex.append(conn)

    # Preserve existing order and append only genuinely new accounts at the end.
    replacements = {
        (conn.get("email") or conn.get("name") or "").lower().strip(): conn
        for conn in incoming
    }
    merged_codex = []
    used = set()
    for old in existing_codex:
        key = (old.get("email") or old.get("name") or "").lower().strip()
        if key in replacements:
            merged_codex.append(replacements[key])
            used.add(key)
        else:
            merged_codex.append(old)
    for conn in incoming:
        key = (conn.get("email") or conn.get("name") or "").lower().strip()
        if key not in used and key not in existing_map:
            merged_codex.append(conn)
            used.add(key)
    for idx, conn in enumerate(merged_codex, start=1):
        conn["priority"] = idx

    data["providerConnections"] = other_conns + merged_codex
    save_db_json(json_path, data)
    return inserted, replaced, errors


def import_connections_json(connections):
    total_inserted = 0
    total_replaced = 0
    errors = []
    if not JSON_PATHS:
        return 0, 0, ["No JSON db found. Candidates: {}".format("; ".join(get_json_candidates()))]
    for json_path in JSON_PATHS:
        ins, rep, errs = import_connections_json_path(connections, json_path)
        total_inserted += ins
        total_replaced += rep
        errors.extend(errs)
    return total_inserted, total_replaced, errors


def import_connections(connections):
    """Write all 9router stores as one serialized local operation."""
    with _storage_lock:
        if not storage_exists():
            return 0, 0, [
                "No 9router/n9router database found",
                "9router SQLite: {}".format(SQLITE_PATH),
                "JSON candidates: {}".format("; ".join(get_json_candidates())),
            ]

        total_inserted = 0
        total_replaced = 0
        errors = []
        if sqlite_exists():
            ins, rep, errs = import_connections_sqlite(connections)
            total_inserted += ins
            total_replaced += rep
            errors.extend(errs)
        if json_exists():
            ins, rep, errs = import_connections_json(connections)
            total_inserted += ins
            total_replaced += rep
            errors.extend(errs)
        return total_inserted, total_replaced, errors


def verify_sqlite_emails(connections):
    """Return True only when every incoming email exists in live 9router SQLite."""
    with _storage_lock:
        if not sqlite_exists():
            return False, [], ["9router SQLite not found: {}".format(SQLITE_PATH)]
        expected = sorted(set(
            (conn.get("email") or conn.get("name") or "").lower().strip()
            for conn in connections
            if (conn.get("email") or conn.get("name") or "").strip()
        ))
        if not expected:
            return False, [], ["No email available for SQLite verification"]
        db = sqlite3.connect(SQLITE_PATH, timeout=30)
        try:
            placeholders = ",".join("?" for _ in expected)
            rows = db.execute(
                "SELECT lower(trim(email)) FROM providerConnections "
                "WHERE provider=? AND lower(trim(email)) IN ({})".format(placeholders),
                [PROVIDER] + expected,
            ).fetchall()
            found = sorted(set(row[0] for row in rows if row[0]))
        finally:
            db.close()
        missing = sorted(set(expected) - set(found))
        errors = ["Email not found in SQLite after commit: {}".format(x) for x in missing]
        return not missing, found, errors


def delete_connection(conn_id):
    deleted = False
    if sqlite_exists():
        backup_file(SQLITE_PATH, "9router-sqlite", "sqlite")
        db = sqlite3.connect(SQLITE_PATH)
        cur = db.cursor()
        cur.execute("DELETE FROM providerConnections WHERE id=? AND provider=?", (conn_id, PROVIDER))
        deleted = cur.rowcount > 0 or deleted
        db.commit()
        db.close()
    if json_exists():
        for json_path in JSON_PATHS:
            data = load_db_json(json_path)
            backup_file(json_path, "json-db", "json")
            before = len(data.get("providerConnections", []))
            data["providerConnections"] = [
                c for c in data.get("providerConnections", [])
                if not (c.get("id") == conn_id and c.get("provider") == PROVIDER)
            ]
            if len(data.get("providerConnections", [])) != before:
                pri = 1
                for conn in data["providerConnections"]:
                    if conn.get("provider") == PROVIDER:
                        conn["priority"] = pri
                        pri += 1
                save_db_json(json_path, data)
                deleted = True
    return deleted


# =============================================================================
# OAuth PKCE Flow
# =============================================================================

def generate_pkce():
    """Generate PKCE code_verifier and code_challenge (S256)."""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def build_authorize_url():
    """Build OAuth authorize URL with PKCE."""
    code_verifier, code_challenge = generate_pkce()
    state = secrets.token_urlsafe(16)
    _oauth_pending[state] = {"code_verifier": code_verifier, "created_at": time.time()}

    params = {
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": OAUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "codex_cli_rs",
    }
    return OAUTH_AUTHORIZE_URL + "?" + urlencode(params), state


def exchange_code_for_tokens(code, state):
    """Exchange authorization code for tokens."""
    pending = _oauth_pending.pop(state, None)
    if not pending:
        return None, "Invalid or expired state"

    body = json.dumps({
        "grant_type": "authorization_code",
        "client_id": OAUTH_CLIENT_ID,
        "code": code,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "code_verifier": pending["code_verifier"],
    }).encode("utf-8")

    req = Request(OAUTH_TOKEN_URL, data=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
    })

    try:
        resp = urlopen(req, timeout=30)
        tokens = json.loads(resp.read().decode("utf-8"))
        return tokens, None
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        return None, "Token exchange failed ({}): {}".format(e.code, err_body)
    except Exception as e:
        return None, "Token exchange error: {}".format(str(e))


def tokens_to_connection(tokens):
    """Convert OAuth token response to a 9router connection dict."""
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    id_token = tokens.get("id_token", "")
    expires_in = tokens.get("expires_in", 864000)

    # Decode JWT to get email and account info
    email = ""
    account_id = ""
    plan_type = ""
    try:
        parts = access_token.split(".")
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        prof = decoded.get("https://api.openai.com/profile", {})
        auth = decoded.get("https://api.openai.com/auth", {})
        email = prof.get("email", "")
        account_id = auth.get("chatgpt_account_id", "")
        plan_type = auth.get("chatgpt_plan_type", "")
    except:
        pass

    now = now_iso()
    expires_at = datetime.fromtimestamp(
        time.time() + expires_in, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    return {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "idToken": id_token or "",
        "expiresAt": expires_at,
        "expiresIn": expires_in,
        "testStatus": "active",
        "lastUsedAt": now,
        "consecutiveUseCount": 0,
        "backoffLevel": 0,
        "providerSpecificData": {
            "chatgptAccountId": account_id,
            "chatgptPlanType": plan_type,
        },
        "lastError": None,
        "lastErrorAt": None,
        "email": email,
        "name": email,
        "provider": PROVIDER,
        "authType": "oauth",
    }


# OAuth callback result storage
_oauth_result = {"status": "idle"}  # idle | waiting | success | error


class OAuthCallbackHandler(SimpleHTTPRequestHandler):
    """Handles the OAuth callback on port 1455."""
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        global _oauth_result
        parsed = urlparse(self.path)
        if parsed.path == "/auth/callback":
            params = parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            state = params.get("state", [None])[0]
            error = params.get("error", [None])[0]

            if error:
                _oauth_result = {"status": "error", "error": error}
                self._send_html("<h2>Login failed</h2><p>{}</p><p>You can close this tab.</p>".format(error))
                return

            if not code or not state:
                _oauth_result = {"status": "error", "error": "Missing code or state"}
                self._send_html("<h2>Error</h2><p>Missing code or state parameter.</p>")
                return

            # Exchange code for tokens
            tokens, err = exchange_code_for_tokens(code, state)
            if err:
                _oauth_result = {"status": "error", "error": err}
                self._send_html("<h2>Token exchange failed</h2><p>{}</p>".format(err))
                return
            # Convert to connection and import
            conn = tokens_to_connection(tokens)
            email = conn.get("email", "Unknown")

            try:
                ins, rep, errs = import_connections([conn])
                _oauth_result = {
                    "status": "success",
                    "email": email,
                    "plan": conn.get("providerSpecificData", {}).get("chatgptPlanType", ""),
                    "hasRefresh": bool(conn.get("refreshToken")),
                    "inserted": ins,
                    "replaced": rep,
                }
                self._send_html(
                    '<h2 style="color:#22c55e">Login successful!</h2>'
                    '<p><strong>{}</strong> — {}</p>'
                    '<p>Refresh token: {}</p>'
                    '<p>Imported into 9router. You can close this tab.</p>'
                    '<script>setTimeout(()=>window.close(),3000)</script>'.format(
                        email,
                        conn.get("providerSpecificData", {}).get("chatgptPlanType", "free"),
                        "Yes" if conn.get("refreshToken") else "No",
                    )
                )
            except Exception as e:
                _oauth_result = {"status": "error", "error": str(e)}
                self._send_html("<h2>Import failed</h2><p>{}</p>".format(str(e)))
        else:
            self.send_response(404)
            self.end_headers()

    def _send_html(self, body):
        html = '<!DOCTYPE html><html><head><meta charset="utf-8"><title>9router OAuth</title>'
        html += '<style>body{font-family:system-ui;background:#1a1a2e;color:#e0e0e0;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}'
        html += '.card{background:#16213e;padding:40px;border-radius:16px;text-align:center;max-width:500px;box-shadow:0 8px 32px rgba(0,0,0,.3)}</style>'
        html += '</head><body><div class="card">{}</div></body></html>'.format(body)
        content = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content)


def start_oauth_callback_server():
    """Start the OAuth callback server on port 1455 in a background thread."""
    try:
        server = HTTPServer(("127.0.0.1", OAUTH_CALLBACK_PORT), OAuthCallbackHandler)
        server.timeout = 120  # 2 minute timeout
        # Handle one request then stop
        def serve():
            server.handle_request()
            server.server_close()
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        return True
    except OSError as e:
        print("  [!] Cannot start callback server on port {}: {}".format(OAUTH_CALLBACK_PORT, e))
        return False


# =============================================================================
# Auto-Login Worker (runs auto_login.py via subprocess)
# =============================================================================

_auto_login_status = {
    "running": False,
    "total": 0,
    "current": 0,
    "currentEmail": "",
    "done": 0,
    "failed": 0,
    "results": [],
    "stopped": False,
    "pid": None,
    "mode": "",
    "workersRequested": 3,
    "workersRunning": 0,
    "activeAccounts": [],
}
_auto_login_proc = None
_auto_login_lock = threading.Lock()

def new_web_login_status(total=0, workers=0, link_url="", run_id="", running=True):
    active_workers = min(max(0, int(workers or 0)), max(0, int(total or 0)))
    return {
        "running": bool(running),
        "paused": False,
        "interrupted": False,
        "total": max(0, int(total or 0)),
        "done": 0,
        "failed": 0,
        "results": [],
        "activeAccounts": [],
        "webReady": False,
        "webSessions": 0,
        "completed": False,
        "stopped": False,
        "pid": None,
        "linkUrl": str(link_url or ""),
        "workersRequested": max(0, int(workers or 0)),
        "workersRunning": active_workers,
        "queued": max(0, int(total or 0)),
        "processed": 0,
        "retries": 0,
        "retryCounts": {},
        "logs": [],
        "logSequence": 0,
        "runId": str(run_id or ""),
        "startedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z") if running else "",
        "finishedAt": "",
    }


_web_login_status = new_web_login_status(running=False)
_web_login_proc = None
_web_login_lock = threading.RLock()
_web_login_history_lock = threading.RLock()
_web_login_state_lock = threading.RLock()
_web_login_control_lock = threading.Lock()


def _utc_timestamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_web_login_result(result):
    return {
        "runId": str(result.get("runId") or "")[:80],
        "index": max(0, int(result.get("index") or 0)),
        "email": str(result.get("email") or "")[:320],
        "status": "success" if result.get("status") == "success" else "error",
        "webOpened": bool(result.get("webOpened")),
        "linkOpened": bool(result.get("linkOpened")),
        "chatgptReloaded": bool(result.get("chatgptReloaded")),
        "personalAccountVerified": bool(result.get("personalAccountVerified")),
        "error": str(result.get("error") or "")[:500],
        "startedAt": str(result.get("startedAt") or "")[:60],
        "finishedAt": str(result.get("finishedAt") or "")[:60],
        "durationSeconds": max(0, int(result.get("durationSeconds") or 0)),
    }


def _safe_web_login_log(entry):
    return {
        "sequence": max(0, int(entry.get("sequence") or 0)),
        "time": str(entry.get("time") or "")[:60],
        "level": entry.get("level") if entry.get("level") in ("info", "success", "error", "warning") else "info",
        "message": str(entry.get("message") or "")[:500],
        "email": str(entry.get("email") or "")[:320],
        "stage": str(entry.get("stage") or "")[:80],
    }


def safe_web_login_status(status):
    """Allow-list the durable run state; credentials and secondary URLs are excluded."""
    safe = new_web_login_status(running=False)
    safe.update({
        "running": bool(status.get("running")),
        "paused": bool(status.get("paused")),
        "interrupted": bool(status.get("interrupted")),
        "total": max(0, int(status.get("total") or 0)),
        "done": max(0, int(status.get("done") or 0)),
        "failed": max(0, int(status.get("failed") or 0)),
        "results": [_safe_web_login_result(item) for item in status.get("results", []) if isinstance(item, dict)][:500],
        "activeAccounts": [str(email)[:320] for email in status.get("activeAccounts", [])][:100],
        "webReady": bool(status.get("webReady")),
        "webSessions": max(0, int(status.get("webSessions") or 0)),
        "completed": bool(status.get("completed")),
        "stopped": bool(status.get("stopped")),
        "pid": int(status.get("pid")) if status.get("pid") else None,
        "linkUrl": "",
        "workersRequested": max(0, int(status.get("workersRequested") or 0)),
        "workersRunning": max(0, int(status.get("workersRunning") or 0)),
        "queued": max(0, int(status.get("queued") or 0)),
        "processed": max(0, int(status.get("processed") or 0)),
        "retries": max(0, int(status.get("retries") or 0)),
        "retryCounts": {
            str(key)[:80]: max(0, int(value or 0))
            for key, value in dict(status.get("retryCounts") or {}).items()
        },
        "logs": [_safe_web_login_log(item) for item in status.get("logs", []) if isinstance(item, dict)][-300:],
        "logSequence": max(0, int(status.get("logSequence") or 0)),
        "runId": str(status.get("runId") or "")[:80],
        "startedAt": str(status.get("startedAt") or "")[:60],
        "finishedAt": str(status.get("finishedAt") or "")[:60],
    })
    return safe


def persist_web_login_status(status=None, current_only=False):
    """Atomically persist a snapshot, optionally only for the active run object."""
    with _web_login_lock:
        target = status if status is not None else _web_login_status
        if current_only and target is not _web_login_status:
            return None
        snapshot = safe_web_login_status(target)
        directory = os.path.dirname(WEB_LOGIN_STATE_FILE) or "."
        os.makedirs(directory, exist_ok=True)
        temp_path = WEB_LOGIN_STATE_FILE + ".tmp"
        with _web_login_state_lock:
            with open(temp_path, "w", encoding="utf-8") as output:
                json.dump(snapshot, output, indent=2, ensure_ascii=False)
            if os.name != "nt":
                os.chmod(temp_path, 0o600)
            os.replace(temp_path, WEB_LOGIN_STATE_FILE)
        return snapshot


def load_web_login_status():
    with _web_login_state_lock:
        try:
            with open(WEB_LOGIN_STATE_FILE, "r", encoding="utf-8") as source:
                payload = json.load(source)
            if not isinstance(payload, dict):
                return new_web_login_status(running=False)
            return safe_web_login_status(payload)
        except (OSError, ValueError, TypeError):
            return new_web_login_status(running=False)


def _write_web_login_history(entries):
    directory = os.path.dirname(WEB_LOGIN_HISTORY_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = WEB_LOGIN_HISTORY_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as output:
        json.dump(entries, output, indent=2, ensure_ascii=False)
    if os.name != "nt":
        os.chmod(temp_path, 0o600)
    os.replace(temp_path, WEB_LOGIN_HISTORY_FILE)


def load_web_login_history():
    """Load safe account attempt history; corrupt files fail closed to empty."""
    with _web_login_history_lock:
        try:
            with open(WEB_LOGIN_HISTORY_FILE, "r", encoding="utf-8") as source:
                payload = json.load(source)
            if not isinstance(payload, list):
                return []
            return [entry for entry in payload if isinstance(entry, dict)][:WEB_LOGIN_HISTORY_LIMIT]
        except (OSError, ValueError, TypeError):
            return []


def record_web_login_history(result):
    """Persist an allow-listed result without password, TOTP, token or session data."""
    safe = {
        "id": str(result.get("id") or uuid.uuid4()),
        "runId": str(result.get("runId") or ""),
        "index": int(result.get("index") or 0),
        "email": str(result.get("email") or "")[:320],
        "status": "success" if result.get("status") == "success" else "error",
        "error": str(result.get("error") or "")[:500],
        "startedAt": str(result.get("startedAt") or ""),
        "finishedAt": str(result.get("finishedAt") or _utc_timestamp()),
        "durationSeconds": max(0, int(result.get("durationSeconds") or 0)),
        "linkOpened": bool(result.get("linkOpened")),
        "chatgptReloaded": bool(result.get("chatgptReloaded")),
        "personalAccountVerified": bool(result.get("personalAccountVerified")),
    }
    with _web_login_history_lock:
        entries = load_web_login_history()
        entries.insert(0, safe)
        _write_web_login_history(entries[:WEB_LOGIN_HISTORY_LIMIT])
    return safe


def clear_web_login_history():
    with _web_login_history_lock:
        _write_web_login_history([])


def append_web_login_log(level, message, email="", stage="", status=None):
    """Append one bounded, structured event for near-real-time UI polling."""
    should_persist = False
    with _web_login_lock:
        target = status if status is not None else _web_login_status
        sequence = int(target.get("logSequence") or 0) + 1
        target["logSequence"] = sequence
        logs = target.setdefault("logs", [])
        logs.append({
            "sequence": sequence,
            "time": _utc_timestamp(),
            "level": level if level in ("info", "success", "error", "warning") else "info",
            "message": str(message or "")[:500],
            "email": str(email or "")[:320],
            "stage": str(stage or "")[:80],
        })
        del logs[:-300]
        should_persist = target is _web_login_status
        if should_persist:
            persist_web_login_status(target, current_only=True)


def get_web_login_status():
    with _web_login_lock:
        return json.loads(json.dumps(_web_login_status, ensure_ascii=False))


def get_web_login_status_response():
    """Return live state plus non-persistent capabilities of this server build."""
    status = get_web_login_status()
    status["browserControls"] = True
    return status


def build_auto_login_command(accounts_file, workers):
    """Build the automation command for source and packaged app modes."""
    worker_count = str(max(1, int(workers)))
    if AUTO_LOGIN_EXECUTABLE:
        return [AUTO_LOGIN_EXECUTABLE, accounts_file, "--workers", worker_count]
    return [
        sys.executable,
        os.path.join(SCRIPT_DIR, "auto_login.py"),
        accounts_file,
        "--workers",
        worker_count,
    ]


def build_web_login_command(accounts_file, workers, link_url=""):
    """Build direct web-login arguments, omitting the secondary-tab flag when disabled."""
    args = build_auto_login_command(accounts_file, workers)
    args.extend(["--headed", "--web-only"])
    if link_url:
        args.extend(["--open-link", link_url])
    return args


def _auto_login_worker(accounts, headed=True, workers=3):
    """Background worker that runs the parallel auto_login subprocess."""
    global _auto_login_status, _auto_login_proc
    import subprocess
    try:
        workers = int(workers)
    except (TypeError, ValueError):
        workers = 3
    workers = max(1, workers)
    active_workers = min(workers, len(accounts))
    tmp_file = os.path.join(RUNTIME_DIR, "_tmp_accounts.txt")
    with open(tmp_file, "w", encoding="utf-8") as output:
        for account in accounts:
            output.write("{}\n".format(account))
    if os.name != "nt":
        os.chmod(tmp_file, 0o600)
    _auto_login_status = {
        "running": True,
        "total": len(accounts),
        "current": 0,
        "currentEmail": "",
        "done": 0,
        "failed": 0,
        "results": [],
        "stopped": False,
        "pid": None,
        "mode": "headed" if headed else "headless",
        "workersRequested": workers,
        "workersRunning": active_workers,
        "activeAccounts": [],
    }
    result_by_email = {}

    # Run the source script during development or the bundled worker executable.
    if not AUTO_LOGIN_EXECUTABLE and not os.path.exists(os.path.join(SCRIPT_DIR, "auto_login.py")):
        _auto_login_status["running"] = False
        _auto_login_status["results"].append({"email": "?", "status": "error", "error": "auto_login.py not found"})
        return

    args = build_auto_login_command(tmp_file, workers)
    if headed:
        args.append("--headed")

    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            cwd=SCRIPT_DIR,
            env=env,
        )
        with _auto_login_lock:
            _auto_login_proc = proc
            _auto_login_status["pid"] = proc.pid
        print("  [auto] Mode: {} | PID: {}".format("headed" if headed else "headless", proc.pid))
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            print("  [auto] {}".format(line))

            # Deterministic events are safe even when subprocess worker logs interleave.
            if line.startswith("EVENT|"):
                parts = line.split("|")
                kind = parts[1] if len(parts) > 1 else ""
                email = parts[2].strip() if len(parts) > 2 else ""
                metadata = {}
                for item in parts[3:]:
                    if "=" in item:
                        key, value = item.split("=", 1)
                        metadata[key] = value
                active = _auto_login_status["activeAccounts"]
                if kind == "START" and email:
                    if email not in active:
                        active.append(email)
                    _auto_login_status["currentEmail"] = email
                    _auto_login_status["current"] = min(_auto_login_status["total"], len(active) + _auto_login_status["done"] + _auto_login_status["failed"])
                elif kind in ("SUCCESS", "ERROR") and email:
                    if email in active:
                        active.remove(email)
                    if result_by_email.get(email) not in ("success", "error"):
                        status = "success" if kind == "SUCCESS" else "error"
                        result_by_email[email] = status
                        if status == "success":
                            _auto_login_status["done"] += 1
                        else:
                            _auto_login_status["failed"] += 1
                        _auto_login_status["results"].append({
                            "email": email,
                            "status": status,
                            "plan": metadata.get("plan", ""),
                            "hasRefresh": metadata.get("refresh") == "yes",
                            "imported": status == "success",
                            "error": metadata.get("error", ""),
                        })
                continue

            # Compatibility parsing for any legacy progress line.
            if line.startswith("[") and "/" in line[:10] and "]" in line[:10]:
                # [1/3] email@...
                try:
                    bracket = line.split("]")[0].replace("[", "")
                    cur, total = bracket.split("/")
                    email = line.split("]")[1].strip()
                    _auto_login_status["current"] = int(cur)
                    _auto_login_status["currentEmail"] = email
                except:
                    pass

            # OAuth token acquisition is only an intermediate state. Keep its
            # metadata, but do not increment success until SQLite is verified.
            if line.startswith("✅ ") and "|" in line and "plan=" in line:
                parts = line.split("|")
                email = parts[0].replace("✅", "").strip()
                meta = result_by_email.get(email) if isinstance(result_by_email.get(email), dict) else {}
                for part in parts:
                    if "plan=" in part:
                        meta["plan"] = part.split("=", 1)[1].strip()
                    if "rt=yes" in part:
                        meta["hasRefresh"] = True
                result_by_email[email] = meta

            if line.startswith("IMPORT_OK|"):
                parts = line.split("|", 4)
                email = parts[1].strip() if len(parts) > 1 else ""
                plan = parts[2].strip() if len(parts) > 2 else ""
                has_rt = len(parts) > 3 and parts[3].strip() == "yes"
                if email and result_by_email.get(email) != "success":
                    result_by_email[email] = "success"
                    _auto_login_status["done"] += 1
                    _auto_login_status["results"].append({
                        "email": email,
                        "status": "success",
                        "plan": plan,
                        "hasRefresh": has_rt,
                        "imported": True,
                    })

            if line.startswith("IMPORT_FAIL|"):
                parts = line.split("|", 2)
                email = parts[1].strip() if len(parts) > 1 else (_auto_login_status.get("currentEmail") or "?")
                error = parts[2].strip() if len(parts) > 2 else "Unknown 9router import error"
                if email and result_by_email.get(email) != "error":
                    result_by_email[email] = "error"
                    _auto_login_status["failed"] += 1
                    _auto_login_status["results"].append({
                        "email": email,
                        "status": "error",
                        "error": "OAuth OK, import 9router failed: {}".format(error),
                        "imported": False,
                    })

            if line.startswith("❌ ") and "|" not in line:
                err_email = _auto_login_status.get("currentEmail", "?")
                err_msg = line.replace("❌", "").strip()
                if err_email and result_by_email.get(err_email) not in ("success", "error"):
                    result_by_email[err_email] = "error"
                    _auto_login_status["failed"] += 1
                    _auto_login_status["results"].append({
                        "email": err_email,
                        "status": "error",
                        "error": err_msg,
                    })

        proc.wait()
    except Exception as e:
        _auto_login_status["results"].append({"email": "?", "status": "error", "error": str(e)})
    finally:
        with _auto_login_lock:
            _auto_login_proc = None
            _auto_login_status["pid"] = None
        _auto_login_status["running"] = False
        # Cleanup temp file
        try:
            os.remove(tmp_file)
        except:
            pass


def start_auto_login(accounts, headed=True, workers=3):
    """Start auto-login in a background thread with a user-selected worker count."""
    if _auto_login_status.get("running"):
        return
    thread = threading.Thread(target=_auto_login_worker, args=(accounts, headed, workers), daemon=True)
    thread.start()


def _web_login_worker(accounts, workers=3, link_url="", run_id=None):
    """Run direct ChatGPT web login independently from Codex OAuth/import."""
    global _web_login_status, _web_login_proc
    import subprocess
    workers = max(1, int(workers))
    active_workers = min(workers, len(accounts))
    with _web_login_lock:
        if run_id and _web_login_status.get("runId") == run_id:
            status = _web_login_status
        else:
            run_id = str(run_id or uuid.uuid4())
            status = new_web_login_status(len(accounts), workers, link_url, run_id=run_id)
            _web_login_status = status
    tmp_file = os.path.join(RUNTIME_DIR, "_tmp_web_accounts_{}.txt".format(run_id))
    with open(tmp_file, "w", encoding="utf-8") as output:
        for account in accounts:
            output.write("{}\n".format(account))
    if os.name != "nt":
        os.chmod(tmp_file, 0o600)
    result_by_task = {}
    link_by_email = {}
    reload_by_email = {}
    started_by_task = {}
    started_tasks = set()
    active_by_task = {}
    append_web_login_log(
        "info",
        "Bắt đầu phiên với {} tài khoản, tối đa {} luồng".format(len(accounts), active_workers),
        status=status,
    )
    args = build_web_login_command(tmp_file, workers, link_url)
    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            cwd=SCRIPT_DIR,
            env=env,
            start_new_session=os.name != "nt",
        )
        with _web_login_lock:
            if _web_login_status is status:
                _web_login_proc = proc
            status["pid"] = proc.pid
        if _web_login_status is status:
            persist_web_login_status(status, current_only=True)
        print("  [web] Direct ChatGPT login | PID: {}".format(proc.pid))
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            print("  [web] {}".format(line))
            if line.startswith("WEB_READY|"):
                try:
                    count = int(line.split("|", 1)[1].strip() or 0)
                except (TypeError, ValueError):
                    count = 0
                status["webReady"] = count > 0
                status["webSessions"] = count
                status["completed"] = True
                status["finishedAt"] = _utc_timestamp()
                append_web_login_log(
                    "success",
                    "Đã xử lý đủ danh sách; {} phiên ChatGPT đang được giữ mở".format(count),
                    status=status,
                )
                continue
            if line.startswith("WEB_LINK_OPEN|"):
                parts = line.split("|", 2)
                if len(parts) >= 3:
                    link_by_email[parts[1].strip()] = parts[2].strip()
                continue
            if line.startswith("WEB_LINK_FAIL|"):
                parts = line.split("|", 3)
                if len(parts) >= 3:
                    link_by_email[parts[1].strip()] = ""
                continue
            if line.startswith("WEB_CHATGPT_RELOAD|"):
                parts = line.split("|", 1)
                if len(parts) == 2:
                    reload_by_email[parts[1].strip()] = True
                continue
            if line.startswith("WEB_CHATGPT_RELOAD_FAIL|"):
                parts = line.split("|", 2)
                if len(parts) >= 2:
                    reload_by_email[parts[1].strip()] = False
                continue
            if not line.startswith("EVENT|"):
                continue
            parts = line.split("|")
            kind = parts[1] if len(parts) > 1 else ""
            email = parts[2].strip() if len(parts) > 2 else ""
            metadata = {}
            for item in parts[3:]:
                if "=" in item:
                    key, value = item.split("=", 1)
                    metadata[key] = value
            try:
                task_index = int(metadata.get("index") or 0)
            except (TypeError, ValueError):
                task_index = 0
            task_key = task_index or email.lower()
            if kind == "QUEUED" and email:
                append_web_login_log("info", "Đã thêm vào hàng đợi", email=email, stage="queued", status=status)
            elif kind == "START" and email:
                if task_key not in started_tasks:
                    started_tasks.add(task_key)
                    status["queued"] = max(0, status.get("queued", 0) - 1)
                started_by_task.setdefault(task_key, time.time())
                active_by_task[task_key] = email
                status["activeAccounts"] = list(active_by_task.values())
                status["workersRunning"] = len(active_by_task)
                attempt = metadata.get("attempt") or "1"
                append_web_login_log(
                    "info",
                    "Bắt đầu đăng nhập lần {}".format(attempt),
                    email=email,
                    stage="start",
                    status=status,
                )
            elif kind == "PHASE" and email:
                phase_stage = metadata.get("stage", "")
                if phase_stage == "personal_account_verified":
                    phase_level = "success"
                elif phase_stage in ("manual_check", "link_error", "personal_account_unverified"):
                    phase_level = "warning"
                else:
                    phase_level = "info"
                append_web_login_log(
                    phase_level,
                    metadata.get("message") or phase_stage or "Đang xử lý",
                    email=email,
                    stage=phase_stage,
                    status=status,
                )
            elif kind == "RETRY" and email:
                active_by_task.pop(task_key, None)
                status["activeAccounts"] = list(active_by_task.values())
                status["workersRunning"] = len(active_by_task)
                status["retries"] = int(status.get("retries") or 0) + 1
                retry_counts = status.setdefault("retryCounts", {})
                retry_key = str(task_key)
                retry_counts[retry_key] = int(retry_counts.get(retry_key) or 0) + 1
                delay = metadata.get("delay") or "0"
                next_attempt = metadata.get("nextAttempt") or "?"
                error = metadata.get("error") or "Lần đăng nhập trước chưa thành công"
                append_web_login_log(
                    "warning",
                    "{}; tự thử lại lần {} sau {} giây".format(error, next_attempt, delay),
                    email=email,
                    stage="retry",
                    status=status,
                )
            elif kind in ("SUCCESS", "ERROR") and email and task_key not in result_by_task:
                active_by_task.pop(task_key, None)
                status["activeAccounts"] = list(active_by_task.values())
                status["workersRunning"] = len(active_by_task)
                terminal_status = "success" if kind == "SUCCESS" else "error"
                result_by_task[task_key] = terminal_status
                status["done" if terminal_status == "success" else "failed"] += 1
                if terminal_status == "success":
                    status["webSessions"] = status["done"]
                status["processed"] = status["done"] + status["failed"]
                finished_at = _utc_timestamp()
                duration_seconds = max(0, int(time.time() - started_by_task.get(task_key, time.time())))
                result = {
                    "runId": run_id,
                    "index": task_index,
                    "email": email,
                    "status": terminal_status,
                    "webOpened": terminal_status == "success",
                    "linkOpened": metadata.get("link") == "yes" or bool(link_by_email.get(email)),
                    "linkUrl": link_by_email.get(email, status.get("linkUrl", "")),
                    "chatgptReloaded": metadata.get("reloaded") == "yes" or reload_by_email.get(email, False),
                    "personalAccountVerified": metadata.get("personal") == "yes",
                    "error": metadata.get("error", ""),
                    "startedAt": datetime.fromtimestamp(
                        started_by_task.get(task_key, time.time()), timezone.utc
                    ).isoformat().replace("+00:00", "Z"),
                    "finishedAt": finished_at,
                    "durationSeconds": duration_seconds,
                }
                status["results"].append(result)
                record_web_login_history(result)
                append_web_login_log(
                    (
                        "success"
                        if terminal_status == "success" and result["personalAccountVerified"]
                        else ("warning" if terminal_status == "success" else "error")
                    ),
                    (
                        "Đăng nhập thành công · Đã xác minh Personal account"
                        if terminal_status == "success" and result["personalAccountVerified"]
                        else (
                            "Đã vào ChatGPT · Chưa xác minh Personal account"
                            if terminal_status == "success"
                            else (metadata.get("error") or "Đăng nhập thất bại")
                        )
                    ),
                    email=email,
                    stage=terminal_status,
                    status=status,
                )
        proc.wait()
        if proc.stdout:
            proc.stdout.close()
    except Exception as error:
        status["results"].append({"email": "?", "status": "error", "error": str(error)})
        status["failed"] += 1
        append_web_login_log("error", "Tiến trình automation bị lỗi: {}".format(error), status=status)
    finally:
        proc = locals().get("proc")
        try:
            if proc and proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        finalize_web_login_status(status, proc=proc)
        try:
            os.remove(tmp_file)
        except Exception:
            pass


def finalize_web_login_status(status, proc=None):
    """Finalize only this run object; stale workers cannot mutate the active run."""
    global _web_login_proc
    with _web_login_lock:
        is_current = _web_login_status is status
        if is_current and (proc is None or _web_login_proc is proc):
            _web_login_proc = None
        status["pid"] = None
        status["running"] = False
        status["paused"] = False
        status["workersRunning"] = 0
        status["activeAccounts"] = []
        status["processed"] = status["done"] + status["failed"]
        if not status.get("finishedAt"):
            status["finishedAt"] = _utc_timestamp()
        status["completed"] = status["processed"] >= status["total"]
        status["interrupted"] = not status["completed"] and not status.get("stopped")
    if not status.get("stopped"):
        if status["interrupted"]:
            append_web_login_log(
                "error",
                "Tiến trình đăng nhập đã dừng ngoài dự kiến; dữ liệu phiên đã được giữ lại",
                stage="interrupted",
                status=status,
            )
        else:
            append_web_login_log(
                "success" if status["failed"] == 0 else "warning",
                "Phiên đã kết thúc: {} thành công, {} lỗi".format(status["done"], status["failed"]),
                status=status,
            )
    if is_current:
        persist_web_login_status(status, current_only=True)
    return is_current


def start_web_login(accounts, workers=3, link_url=""):
    global _web_login_status
    with _web_login_lock:
        if _web_login_status.get("running") or _web_login_status.get("paused") or _web_login_status.get("webReady"):
            return False
        run_id = str(uuid.uuid4())
        _web_login_status = new_web_login_status(len(accounts), workers, link_url, run_id=run_id)
        status = _web_login_status
    persist_web_login_status(status, current_only=True)
    thread = threading.Thread(target=_web_login_worker, args=(accounts, workers, link_url, run_id), daemon=True)
    thread.start()
    return True


def send_web_login_control(action, index=None):
    """Send one validated browser command to the active automation process."""
    if action not in ("focus", "rearrange"):
        raise ValueError("Unsupported browser control action")
    command = {"action": action}
    if action == "focus":
        try:
            index = int(index)
        except (TypeError, ValueError):
            raise ValueError("Browser index must be a positive integer")
        if index < 1:
            raise ValueError("Browser index must be a positive integer")
        command["index"] = index

    with _web_login_lock:
        proc = _web_login_proc
    if not proc or proc.poll() is not None or not proc.stdin:
        return False
    try:
        payload = json.dumps(command, ensure_ascii=True) + "\n"
        with _web_login_control_lock:
            proc.stdin.write(payload)
            proc.stdin.flush()
        return True
    except (BrokenPipeError, OSError, ValueError):
        return False


def _signal_web_login_process(sig):
    with _web_login_lock:
        proc = _web_login_proc
        status = _web_login_status
        pid = proc.pid if proc else status.get("pid")
    if not pid or os.name == "nt":
        return False
    try:
        import signal
        os.killpg(os.getpgid(pid), sig)
        return True
    except (OSError, ProcessLookupError):
        return False


def pause_web_login():
    import signal
    with _web_login_lock:
        status = _web_login_status
        if not status.get("running") or status.get("paused") or status.get("completed"):
            return False
    if not _signal_web_login_process(signal.SIGSTOP):
        return False
    with _web_login_lock:
        if _web_login_status is status:
            status["paused"] = True
    append_web_login_log("warning", "Phiên đã tạm dừng", stage="paused", status=status)
    return True


def resume_web_login():
    import signal
    with _web_login_lock:
        status = _web_login_status
        if not status.get("running") or not status.get("paused"):
            return False
    if not _signal_web_login_process(signal.SIGCONT):
        return False
    with _web_login_lock:
        if _web_login_status is status:
            status["paused"] = False
    append_web_login_log("info", "Phiên đã tiếp tục", stage="resumed", status=status)
    return True


def restore_web_login_status():
    """Restore durable logs/results while never claiming a detached process is live."""
    global _web_login_status
    restored = load_web_login_status()
    if not restored.get("runId"):
        return False
    was_active = restored.get("running") or restored.get("paused")
    restored["running"] = False
    restored["paused"] = False
    restored["pid"] = None
    restored["workersRunning"] = 0
    restored["activeAccounts"] = []
    restored["webReady"] = False
    restored["webSessions"] = 0
    if was_active and not restored.get("completed") and not restored.get("stopped"):
        restored["interrupted"] = True
    with _web_login_lock:
        _web_login_status = restored
    append_web_login_log(
        "warning" if restored.get("interrupted") else "info",
        "Đã khôi phục log và tiến độ từ lần chạy trước",
        stage="restored",
        status=restored,
    )
    return True


def stop_web_login():
    """Stop only the direct ChatGPT web-login process and its Chrome children."""
    global _web_login_proc
    proc = None
    with _web_login_lock:
        proc = _web_login_proc
        was_paused = bool(_web_login_status.get("paused"))
    killed = False
    if proc:
        try:
            if os.name == "nt":
                killed = _kill_pid_tree(proc.pid)
            else:
                import signal
                process_group = os.getpgid(proc.pid)
                if was_paused:
                    os.killpg(process_group, signal.SIGCONT)
                os.killpg(process_group, signal.SIGTERM)
                killed = True
            proc.wait(timeout=5)
        except Exception:
            try:
                if os.name != "nt":
                    import signal
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
                proc.wait(timeout=3)
                killed = True
            except Exception:
                pass
    with _web_login_lock:
        status = _web_login_status
        status["running"] = False
        status["paused"] = False
        status["stopped"] = True
        status["webReady"] = False
        status["webSessions"] = 0
        status["activeAccounts"] = []
        status["workersRunning"] = 0
        status["finishedAt"] = _utc_timestamp()
        if _web_login_proc is proc:
            _web_login_proc = None
        status["pid"] = None
    append_web_login_log("warning", "Phiên đã bị hủy theo yêu cầu người dùng", stage="stopped", status=status)
    persist_web_login_status(status, current_only=True)
    return killed


def _kill_pid(pid):
    """Force kill a single PID on Windows."""
    if not pid:
        return False
    try:
        import subprocess
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
        return True
    except Exception:
        return False


def _kill_pid_tree(pid):
    """Force kill a PID and all its children."""
    if not pid:
        return False
    try:
        import subprocess
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
        return True
    except Exception:
        return False


def _kill_playwright_browsers():
    """Kill Chrome/Chromium spawned by Playwright. Safe: only kills processes
    whose command line contains --disable-blink-features=AutomationControlled
    which is unique to our tool, never present in normal Chrome."""
    if os.name != "nt":
        return 0
    import subprocess
    import tempfile
    killed = 0
    marker = "--disable-blink-features=AutomationControlled"

    # Method 1: Write a temp .ps1 script to avoid quoting hell
    ps_script = os.path.join(SCRIPT_DIR, "_kill_browsers.ps1")
    try:
        ps_code = """$marker = "{marker}"
$procs = Get-CimInstance Win32_Process | Where-Object {{
  ($_.Name -like "chrome*" -or $_.Name -like "chromium*") -and
  $_.CommandLine -and
  $_.CommandLine.Contains($marker)
}}
$count = 0
foreach ($p in $procs) {{
  try {{
    Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
    $count++
    Write-Host "KILLED:$($p.ProcessId)"
  }} catch {{}}
}}
Write-Host "TOTAL:$count"
""".format(marker=marker)
        with open(ps_script, "w", encoding="utf-8") as f:
            f.write(ps_code)

        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("KILLED:"):
                pid_str = line.split(":")[1]
                print("  [stop] Killed browser PID {}".format(pid_str))
                killed += 1
            elif line.startswith("TOTAL:"):
                total = int(line.split(":")[1])
                if total > killed:
                    killed = total
    except Exception as e:
        print("  [stop] PS script error: {}".format(e))
    finally:
        try:
            os.remove(ps_script)
        except Exception:
            pass

    # Method 2 fallback: wmic direct terminate
    if killed == 0:
        try:
            wmic_filter = "Name like 'chrome%' and CommandLine like '%{}%'".format(marker)
            result = subprocess.run(
                ["wmic", "process", "where", wmic_filter, "call", "terminate"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=10,
            )
            out = result.stdout or ""
            if "ReturnValue = 0" in out:
                killed += out.count("ReturnValue = 0")
                print("  [stop] wmic terminated {} browser process(es)".format(killed))
        except Exception as e:
            print("  [stop] wmic fallback error: {}".format(e))

    return killed


def stop_auto_login():
    """Force-stop every auto-login worker, browser, and active UI connection."""
    global _auto_login_proc
    killed = False
    pid = None
    proc = None
    with _auto_login_lock:
        proc = _auto_login_proc
        if proc:
            pid = proc.pid

    # Kill the complete Python -> Playwright -> Chrome process tree first.
    # This must happen before updating UI state so Stop All means nothing keeps
    # trying to log in or holding an OAuth callback connection in the background.
    if pid:
        killed = _kill_pid_tree(pid) or killed
        try:
            if proc:
                proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=3)
                killed = True
            except Exception as error:
                print("  [stop] subprocess cleanup failed: {}".format(error))

    # A browser can occasionally outlive its Playwright parent, especially if a
    # callback/CAPTCHA is open. Sweep twice to catch children that detach during
    # the process-tree shutdown.
    killed_browsers = _kill_playwright_browsers()
    time.sleep(0.35)
    killed_browsers += _kill_playwright_browsers()

    _auto_login_status["running"] = False
    _auto_login_status["stopped"] = True
    _auto_login_status["current"] = 0
    _auto_login_status["currentEmail"] = ""
    _auto_login_status["workersRunning"] = 0
    _auto_login_status["activeAccounts"] = []
    _auto_login_status["killedBrowsers"] = killed_browsers
    _auto_login_status["results"].append({
        "email": "?",
        "status": "stopped",
        "error": "Stopped by user — all active workers disconnected",
    })
    with _auto_login_lock:
        _auto_login_proc = None
        _auto_login_status["pid"] = None
    print("  [stop] All workers disconnected. process={}, browsers={}".format(killed, killed_browsers))
    return killed or killed_browsers > 0


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SCRIPT_DIR, **kwargs)

    def log_message(self, fmt, *args):
        print("  [HTTP] {} {}".format(self.command, self.path))

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/api/health":
            self._json({
                "ok": True,
                "storage": "multi",
                "sqlite": sqlite_exists(),
                "sqlitePath": SQLITE_PATH or "",
                "json": json_exists(),
                "jsonPaths": JSON_PATHS,
                "jsonCandidates": get_json_candidates(),
                "path": JSON_PATHS[0] if JSON_PATHS else (SQLITE_PATH or ""),
            })
            return
        if p == "/api/connections":
            c = get_connections()
            self._json({"connections": c, "count": len(c)})
            return
        if p == "/api/oauth/status":
            self._json(_oauth_result)
            return
        if p == "/api/oauth/auto-status":
            self._json(_auto_login_status)
            return
        if p == "/api/web-login/status":
            self._json(get_web_login_status_response())
            return
        if p == "/api/web-login/history":
            history = load_web_login_history()
            self._json({"history": history, "count": len(history)})
            return
        if p == "/" or p == "/index.html":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/import":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                conns = data.get("connections", [])
                if not conns:
                    self._json({"error": "No connections"}, 400)
                    return
                # Keep import and verification inside one storage critical section.
                with _storage_lock:
                    ins, rep, errs = import_connections(conns)
                    verified, verified_emails, verify_errors = verify_sqlite_emails(conns)
                all_errors = errs + verify_errors
                response = {
                    "inserted": ins,
                    "replaced": rep,
                    "errors": all_errors,
                    "total": ins + rep,
                    "sqliteVerified": verified,
                    "verifiedEmails": verified_emails,
                    "sqlitePath": SQLITE_PATH,
                }
                self._json(response, 200 if verified else 500)
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        if p == "/api/oauth/start":
            global _oauth_result
            _oauth_result = {"status": "waiting"}
            # Start callback server
            if not start_oauth_callback_server():
                _oauth_result = {"status": "error", "error": "Port {} in use".format(OAUTH_CALLBACK_PORT)}
                self._json({"error": "Cannot start callback server"}, 500)
                return
            # Build authorize URL and open browser
            auth_url, state = build_authorize_url()
            webbrowser.open(auth_url)
            self._json({"ok": True, "authUrl": auth_url, "state": state})
            return
        if p == "/api/oauth/auto-login":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                accounts = data.get("accounts", [])
                if not accounts:
                    self._json({"error": "No accounts"}, 400)
                    return
                try:
                    workers = int(data.get("workers", 3))
                except (TypeError, ValueError):
                    self._json({"error": "Workers must be a positive integer"}, 400)
                    return
                if workers < 1:
                    self._json({"error": "Workers must be a positive integer"}, 400)
                    return
                # Always show Chromium for reliable interactive login/captcha handling.
                headed = True
                start_auto_login(accounts, headed=headed, workers=workers)
                self._json({
                    "ok": True,
                    "total": len(accounts),
                    "mode": "headed",
                    "workersRequested": workers,
                    "workersRunning": min(workers, len(accounts)),
                })
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        if p == "/api/oauth/auto-stop":
            killed = stop_auto_login()
            self._json({"ok": True, "killed": killed})
            return
        if p == "/api/web-login/start":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                accounts = data.get("accounts", [])
                if not accounts:
                    self._json({"error": "No accounts"}, 400)
                    return
                try:
                    workers = int(data.get("workers", 3))
                except (TypeError, ValueError):
                    self._json({"error": "Workers must be a positive integer"}, 400)
                    return
                if workers < 1:
                    self._json({"error": "Workers must be a positive integer"}, 400)
                    return
                try:
                    link_url = resolve_web_login_link(data)
                except ValueError as error:
                    self._json({"error": str(error)}, 400)
                    return
                if not start_web_login(accounts, workers=workers, link_url=link_url):
                    self._json({"error": "ChatGPT web login is already running"}, 409)
                    return
                with _web_login_lock:
                    run_id = _web_login_status.get("runId", "")
                self._json({
                    "ok": True,
                    "runId": run_id,
                    "total": len(accounts),
                    "workersRequested": workers,
                    "workersRunning": min(workers, len(accounts)),
                    "linkUrl": link_url,
                })
            except Exception as error:
                self._json({"error": str(error)}, 500)
            return
        if p == "/api/web-login/stop":
            self._json({"ok": True, "killed": stop_web_login()})
            return
        if p == "/api/web-login/focus":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                index = int(data.get("index") or 0)
                if not send_web_login_control("focus", index=index):
                    self._json({"error": "Browser session is not available"}, 409)
                    return
                append_web_login_log(
                    "info",
                    "Đang đưa cửa sổ #{} lên trước".format(index),
                    stage="window_focus_requested",
                )
                self._json({"ok": True, "index": index})
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._json({"error": str(error)}, 400)
            return
        if p == "/api/web-login/rearrange":
            if not send_web_login_control("rearrange"):
                self._json({"error": "Browser sessions are not available"}, 409)
                return
            append_web_login_log(
                "info",
                "Đang sắp xếp lại toàn bộ cửa sổ Chrome",
                stage="window_relayout_requested",
            )
            self._json({"ok": True})
            return
        if p == "/api/web-login/pause":
            paused = pause_web_login()
            self._json({"ok": paused, "paused": paused}, 200 if paused else 409)
            return
        if p == "/api/web-login/resume":
            resumed = resume_web_login()
            self._json({"ok": resumed, "paused": False}, 200 if resumed else 409)
            return
        self._json({"error": "Not found"}, 404)

    def do_DELETE(self):
        p = urlparse(self.path).path
        if p == "/api/web-login/history":
            clear_web_login_history()
            self._json({"ok": True, "history": [], "count": 0})
            return
        if p.startswith("/api/connections/"):
            cid = p.split("/")[-1]
            self._json({"deleted": delete_connection(cid)})
            return
        self._json({"error": "Not found"}, 404)


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main():
    restore_web_login_status()
    print("=" * 50)
    print("  9router Import Tool")
    print("=" * 50)
    if storage_exists():
        print("  9router SQLite:  {}".format(SQLITE_PATH if sqlite_exists() else "not found"))
        if JSON_PATHS:
            print("  JSON databases:")
            for p in JSON_PATHS:
                print("    - {}".format(p))
        else:
            print("  JSON databases: not found")
        print("  Import target:   all detected databases")
    else:
        print("  [!] No 9router/n9router database found!")
        print("  9router SQLite:  {}".format(SQLITE_PATH))
        print("  JSON candidates:")
        for p in get_json_candidates():
            print("    - {}".format(p))
        print("  Install/run 9router or n9router first, then restart this tool.")
    print("  URL: {}".format(PUBLIC_URL or "http://localhost:{}".format(PORT)))
    print("=" * 50)
    print("  Press Ctrl+C to stop")
    print()

    if SOCKET_PATH:
        socket_path = os.path.abspath(os.path.expanduser(SOCKET_PATH))
        os.makedirs(os.path.dirname(socket_path), exist_ok=True)
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        server = ThreadingUnixHTTPServer(socket_path, Handler)
        server.server_name, server.server_port = "localhost", 0
    else:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    if not SOCKET_PATH and os.environ.get("SHOPTAIKHOAN_NO_OPEN_BROWSER") != "1":
        def open_browser():
            import time
            time.sleep(0.8)
            webbrowser.open("http://localhost:{}".format(PORT))
        threading.Thread(target=open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if _web_login_status.get("running") or _web_login_status.get("webReady"):
            stop_web_login()
        print("\nStopped.")
    finally:
        server.server_close()
        if SOCKET_PATH:
            try:
                os.unlink(SOCKET_PATH)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    main()
