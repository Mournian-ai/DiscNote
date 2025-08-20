#!/usr/bin/env python3
# app.py — Simple Twitch Monitor (FastAPI + EventSubWS) — single-file version
# Run:
#   pip install fastapi uvicorn httpx twitchAPI python-multipart
#   python app.py
#
# Endpoints:
#   /         - public monitor (light/dark, toasts, go-to-channel links)
#   /admin    - admin (HTTP Basic: default admin/changeme)
#   /api/...  - JSON APIs
#   /ws       - realtime updates

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

import httpx
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

# TwitchAPI
from twitchAPI.twitch import Twitch
from twitchAPI.eventsub.websocket import EventSubWebsocket

# ---------------- Storage ----------------
STORE_PATH = Path("store.json")

DEFAULT_STORE = {
    "admin": {"username": "admin", "password": "changeme"},
    "twitch": {
        "client_id": "",
        "client_secret": "",
        "token_obtained_at": 0,
        "expires_in": 0
    },
    "discord": {"webhook": ""},
    "streamers": {
        # "login": { user_id, display_name, is_live, last_live, started_at, title, game_id, game_name }
    }
}

def load_store() -> Dict[str, Any]:
    if STORE_PATH.exists():
        try:
            return json.loads(STORE_PATH.read_text())
        except Exception:
            pass
    STORE_PATH.write_text(json.dumps(DEFAULT_STORE, indent=2))
    return json.loads(STORE_PATH.read_text())

def save_store(s: Dict[str, Any]):
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2))
    tmp.replace(STORE_PATH)

store = load_store()

# ---------------- App & Auth ----------------
app = FastAPI()
security = HTTPBasic()

def check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    username = store["admin"]["username"]
    password = store["admin"]["password"]
    if credentials.username == username and credentials.password == password:
        return True
    # Small delay to slow brute force
    time.sleep(0.3)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": "Basic"},
    )

# ---------------- Twitch / EventSub State ----------------
twitch: Optional[Twitch] = None
es: Optional[EventSubWebsocket] = None
GAME_CACHE: Dict[str, str] = {}       # game_id -> game_name
WS_CLIENTS: List[WebSocket] = []      # connected browsers

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def iso_to_hhmm(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z","+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return iso_str or ""

async def ensure_twitch_client():
    """Create Twitch client if creds exist. App access token is managed by SDK."""
    global twitch
    cid = store["twitch"]["client_id"]
    csec = store["twitch"]["client_secret"]
    if not cid or not csec:
        return None
    if twitch is None:
        twitch = await Twitch(cid, csec)
    return twitch

async def resolve_login_to_user(login: str) -> Optional[Dict[str, str]]:
    client = await ensure_twitch_client()
    if client is None:
        return None
    res = await client.get_users(logins=[login])
    data = res.get("data", [])
    if not data:
        return None
    u = data[0]
    return {"user_id": u["id"], "display_name": u["display_name"]}

async def game_name_for(game_id: Optional[str]) -> Optional[str]:
    if not game_id:
        return None
    if game_id in GAME_CACHE:
        return GAME_CACHE[game_id]
    client = await ensure_twitch_client()
    if client is None:
        return None
    res = await client.get_games(game_ids=[game_id])
    data = res.get("data", [])
    if data:
        name = data[0]["name"]
        GAME_CACHE[game_id] = name
        return name
    return None

async def push_update_to_clients():
    """Broadcast full state to all connected web clients."""
    payload = {"type": "full_update", "streamers": store["streamers"]}
    dead = []
    for ws in WS_CLIENTS:
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            dead.append(ws)
    for d in dead:
        try:
            WS_CLIENTS.remove(d)
        except ValueError:
            pass

# ---------------- Discord ----------------
async def discord_notify_online(login: str, s: Dict[str, Any]):
    webhook = store["discord"].get("webhook") or ""
    if not webhook:
        return
    title = s.get("title") or "is live!"
    game = s.get("game_name") or ""
    started = iso_to_hhmm(s.get("started_at", ""))
    desc = f"**{s.get('display_name', login)}** went live.\n\n**Title:** {title}\n**Game:** {game}\n**Live since:** {started}\nhttps://twitch.tv/{login}"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            await client.post(webhook, json={"content": desc})
        except Exception:
            pass

async def discord_notify_offline(login: str, s: Dict[str, Any]):
    webhook = store["discord"].get("webhook") or ""
    if not webhook:
        return
    last = iso_to_hhmm(s.get("last_live", ""))
    desc = f"**{s.get('display_name', login)}** went offline at {last}.\nhttps://twitch.tv/{login}"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            await client.post(webhook, json={"content": desc})
        except Exception:
            pass

# ---------------- EventSub handlers ----------------
async def on_online(data: dict):
    uid = data["event"]["broadcaster_user_id"]
    for login, s in store["streamers"].items():
        if s.get("user_id") == uid:
            s["is_live"] = True
            s["started_at"] = data["event"]["started_at"]
            save_store(store)
            await push_update_to_clients()
            await discord_notify_online(login, s)
            break

async def on_offline(data: dict):
    uid = data["event"]["broadcaster_user_id"]
    for login, s in store["streamers"].items():
        if s.get("user_id") == uid:
            s["is_live"] = False
            s["last_live"] = now_iso()
            save_store(store)
            await push_update_to_clients()
            await discord_notify_offline(login, s)
            break

async def on_channel_update(data: dict):
    uid = data["event"]["broadcaster_user_id"]
    title = data["event"].get("title")
    game_id = data["event"].get("category_id")
    for login, s in store["streamers"].items():
        if s.get("user_id") == uid:
            if title is not None:
                s["title"] = title
            if game_id:
                s["game_id"] = game_id
                s["game_name"] = await game_name_for(game_id)
            save_store(store)
            await push_update_to_clients()
            break

# ---------------- EventSub lifecycle ----------------
es: Optional[EventSubWebsocket] = None

async def start_eventsub():
    """Starts EventSub WS and subscribes for all tracked streamers."""
    global es
    client = await ensure_twitch_client()
    if client is None:
        return
    if es is not None:
        return
    es = EventSubWebsocket(client)
    await es.listen()

    # Subscribe to events for each streamer
    for login, s in list(store["streamers"].items()):
        uid = s.get("user_id")
        if not uid:
            continue
        await es.listen_stream_online(broadcaster_user_id=uid, callback=on_online)
        await es.listen_stream_offline(broadcaster_user_id=uid, callback=on_offline)
        await es.listen_channel_update(broadcaster_user_id=uid, callback=on_channel_update)

    asyncio.create_task(es.run())

async def resubscribe_all():
    """Rebuild EventSub subs (rarely needed)."""
    global es
    if es:
        try:
            await es.stop()
        except Exception:
            pass
        es = None
    await start_eventsub()

# ---------------- Minimal CSS/JS/HTML ----------------
BASE_CSS = """
:root{
  --bg:#0b0b0d; --fg:#eaeaf0; --muted:#9aa0aa; --ok:#22c55e; --bad:#94a3b8; --card:#141419; --accent:#7aa2f7;
  --input-bg:#0e0e12; --input-bd:rgba(255,255,255,.08); --btn-bg:#1f2937; --btn-fg:#eaeaf0; --btn-bd:rgba(255,255,255,.10);
  --shadow:0 10px 24px rgba(0,0,0,.35);
}
html.light{
  --bg:#f7f7fb; --fg:#0b0b0d; --muted:#4b5563; --ok:#16a34a; --bad:#6b7280; --card:#ffffff; --accent:#2563eb;
  --input-bg:#ffffff; --input-bd:rgba(0,0,0,.12); --btn-bg:#f3f4f6; --btn-fg:#0b0b0d; --btn-bd:rgba(0,0,0,.12);
  --shadow:0 8px 24px rgba(0,0,0,.08);
}
*{box-sizing:border-box}
body{margin:0; font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,Noto Sans,Arial; background:var(--bg); color:var(--fg);}
header{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;background:var(--card);position:sticky;top:0;border-bottom:1px solid var(--input-bd);z-index:10}
h1{margin:0;font-size:18px}
.container{max-width:920px;margin:20px auto;padding:0 14px}
.card{background:var(--card);border:1px solid var(--input-bd);border-radius:14px;box-shadow:var(--shadow);padding:16px}
.section{margin-bottom:16px}
.grid{display:grid;gap:10px}
.row{display:grid;grid-template-columns:1.2fr .8fr 1.6fr;align-items:center;gap:10px;padding:12px;border-bottom:1px solid var(--input-bd)}
.row:first-child{font-weight:700}
.row:last-child{border-bottom:0}
.badge{display:inline-block;padding:4px 8px;border-radius:999px;font-weight:600;font-size:12px;border:1px solid var(--input-bd)}
.badge.online{background:rgba(34,197,94,.15);color:var(--ok)}
.badge.offline{background:rgba(148,163,184,.12);color:var(--bad)}
.meta{color:var(--muted);font-size:13px}
.name{font-weight:600}
.title{font-weight:500}
a{color:var(--accent);text-decoration:none}
.actions{display:flex;gap:8px;flex-wrap:wrap}
.btn{appearance:none;background:var(--btn-bg);color:var(--btn-fg);border:1px solid var(--btn-bd);padding:8px 12px;border-radius:10px;cursor:pointer}
.btn.primary{background:var(--accent);color:white;border-color:transparent}
.input{width:100%;padding:9px 11px;border-radius:10px;border:1px solid var(--input-bd);background:var(--input-bg);color:var(--fg)}
.label{font-weight:600;margin-bottom:6px;display:block}
.kv{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.toggle{cursor:pointer;user-select:none;border:1px solid var(--input-bd);padding:6px 10px;border-radius:10px}
.toast-container{position:fixed;right:16px;top:70px;display:flex;flex-direction:column;gap:8px;z-index:1000}
.toast{background:var(--card);border:1px solid var(--input-bd);box-shadow:var(--shadow);border-radius:12px;padding:10px 12px}
.toast b{color:var(--ok)}
"""

THEME_JS = """
<script>
(function(){
  var saved = localStorage.getItem('theme') || 'dark';
  if(saved === 'light'){ document.documentElement.classList.add('light'); }
})();
function toggleTheme(){
  var isLight = document.documentElement.classList.toggle('light');
  localStorage.setItem('theme', isLight ? 'light' : 'dark');
}
</script>
"""

INDEX_BODY = """
  <header>
    <h1>Twitch User Monitor</h1>
    <div class="actions">
      <a class="btn" href="/admin">Admin</a>
      <span class="toggle" onclick="toggleTheme()">🌗 Theme</span>
    </div>
  </header>

  <div class="container">
    <div class="card">
      <div class="row"><div>Name</div><div>Status</div><div>Details</div></div>
      <div id="rows"></div>
    </div>
  </div>

  <div id="toasts" class="toast-container"></div>

<script>
let state = {};
let prevLive = {}; // login -> boolean

function fmt(s){ return s || ''; }
function when(s){ if(!s) return ''; try{ return new Date(s).toUTCString().replace(':00 GMT',' UTC'); }catch(e){ return s; } }

function notify(html, timeout=5000){
  const box = document.getElementById('toasts');
  const t = document.createElement('div');
  t.className = 'toast';
  t.innerHTML = html;
  box.appendChild(t);
  setTimeout(()=>{ t.style.opacity='0'; t.style.transition='opacity .25s'; setTimeout(()=>t.remove(), 300); }, timeout);
}

function render(){
  const rows = document.getElementById('rows');
  rows.innerHTML = '';
  const entries = Object.entries(state).sort((a,b)=> a[0].localeCompare(b[0]));

  for(const [login, s] of entries){
    const live = !!s.is_live;
    const badge = live ? '<span class="badge online">Online</span>' : '<span class="badge offline">Offline</span>';
    const name = s.display_name || login;

    let details = '';
    if(live){
      details = `
        <div class="title">${fmt(s.title)}</div>
        <div class="meta">Game: ${fmt(s.game_name)} · Live since: ${when(s.started_at)} ·
          <a href="https://twitch.tv/${login}" target="_blank" rel="noopener">Go to channel</a>
        </div>`;
    }else{
      details = `<div class="meta">Last live on ${when(s.last_live) || '—'} ·
        <a href="https://twitch.tv/${login}" target="_blank" rel="noopener">Go to channel</a>
      </div>`;
    }

    const row = document.createElement('div');
    row.className = 'row';
    row.innerHTML = `
      <div class="name">${name} <span class="meta">@${login}</span></div>
      <div>${badge}</div>
      <div>${details}</div>`;
    rows.appendChild(row);
  }
}

function handleIncoming(newState){
  // Show toasts for newly-live channels
  for(const [login, s] of Object.entries(newState)){
    const wasLive = !!prevLive[login];
    const isLive = !!s.is_live;
    if(isLive && !wasLive){
      const dn = s.display_name || login;
      const title = fmt(s.title);
      notify(`<b>${dn} is LIVE now</b><div class="meta">${title ? title : ''}</div>`);
    }
  }
  prevLive = Object.fromEntries(Object.entries(newState).map(([k,v])=>[k, !!v.is_live]));
  state = newState;
  render();
}

async function fetchOnce(){
  const r = await fetch('/api/status');
  const j = await r.json();
  handleIncoming(j.streamers || {});
}

let ws;
function connectWS(){
  ws = new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws');
  ws.onmessage = (ev)=>{
    try{
      const msg = JSON.parse(ev.data);
      if(msg.type==='full_update'){ handleIncoming(msg.streamers || {}); }
    }catch(e){}
  };
  ws.onclose = ()=>setTimeout(connectWS, 1500);
}

fetchOnce(); connectWS();
</script>
"""

ADMIN_BODY = """
  <header>
    <h1>Admin</h1>
    <div class="actions">
      <a class="btn" href="/">Home</a>
      <span class="toggle" onclick="toggleTheme()">🌗 Theme</span>
    </div>
  </header>

  <div class="container">
    <div class="card section">
      <h3 style="margin:0 0 10px 0">Twitch Credentials</h3>
      <form method="post" action="/admin/twitch">
        <div class="kv">
          <label class="label">Client ID
            <input class="input" name="client_id" value="">
          </label>
          <label class="label">Client Secret
            <input class="input" name="client_secret" value="">
          </label>
        </div>
        <div class="actions" style="margin-top:10px">
          <button class="btn primary" type="submit">Save & Fetch App Token</button>
        </div>
        <div class="meta" style="margin-top:8px">Token is fetched on demand; we store/refresh automatically.</div>
      </form>
    </div>

    <div class="card section">
      <h3 style="margin:0 0 10px 0">Discord Webhook</h3>
      <form method="post" action="/admin/webhook">
        <label class="label">Webhook URL
          <input class="input" name="webhook" value="">
        </label>
        <div class="actions" style="margin-top:10px">
          <button class="btn primary" type="submit">Save Webhook</button>
          <button class="btn" formaction="/admin/webhook/test" formmethod="post">Send Test</button>
        </div>
      </form>
    </div>

    <div class="card">
      <h3 style="margin:0 0 10px 0">Streamers</h3>
      <form method="post" action="/admin/streamers/add" style="margin-bottom:12px">
        <label class="label">Login name (e.g. mournian)
          <input class="input" name="login">
        </label>
        <div class="actions" style="margin-top:10px">
          <button class="btn primary" type="submit">Add</button>
        </div>
      </form>
      <div id="list" class="grid"></div>
    </div>
  </div>

<script>
(async function(){
  const r = await fetch('/api/status');
  const s = await r.json();
  const list = document.getElementById('list');
  list.innerHTML = '';
  const entries = Object.entries(s.streamers || {}).sort((a,b)=>a[0].localeCompare(b[0]));
  for(const [login, data] of entries){
    const live = !!data.is_live;
    const badge = live ? '<span class="badge online">Online</span>' : '<span class="badge offline">Offline</span>';
    const dn = data.display_name || login;
    const row = document.createElement('div');
    row.className = 'row';
    row.style.gridTemplateColumns = '1fr auto auto';
    row.innerHTML = `
      <div>
        <b>${dn}</b> <span class="meta">@${login}</span>
        <div class="meta">
          <a href="https://twitch.tv/${login}" target="_blank" rel="noopener">Go to channel</a>
        </div>
      </div>
      <div style="display:flex;align-items:center">${badge}</div>
      <form method="post" action="/admin/streamers/remove" class="actions">
        <input type="hidden" name="login" value="${login}">
        <button class="btn" type="submit">Remove</button>
      </form>
    `;
    list.appendChild(row);
  }
})();
</script>
"""

# ---------------- Routes ----------------
@app.get("/", response_class=HTMLResponse)
async def index():
    head = (
        "<!doctype html><html><head><meta charset='utf-8'/>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
        "<title>Twitch User Monitor</title>"
        f"<style>{BASE_CSS}</style>{THEME_JS}</head><body>"
    )
    return head + INDEX_BODY + "</body></html>"

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(authorized: bool = Depends(check_auth)):
    head = (
        "<!doctype html><html><head><meta charset='utf-8'/>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
        "<title>Admin</title>"
        f"<style>{BASE_CSS}</style>{THEME_JS}</head><body>"
    )
    return head + ADMIN_BODY + "</body></html>"

@app.post("/admin/twitch")
async def admin_twitch(
    client_id: str = Form(...),
    client_secret: str = Form(...),
    authorized: bool = Depends(check_auth),
):
    store["twitch"]["client_id"] = client_id.strip()
    store["twitch"]["client_secret"] = client_secret.strip()
    save_store(store)

    # Trigger token acquisition by calling a cheap endpoint
    try:
        client = await ensure_twitch_client()
        if client:
            # a harmless call that forces token flow if needed
            await client.get_games(game_names=["Fortnite"])
            store["twitch"]["token_obtained_at"] = int(time.time())
            save_store(store)
    except Exception:
        pass
    return RedirectResponse("/admin", status_code=302)

@app.post("/admin/webhook")
async def admin_webhook(webhook: str = Form(...), authorized: bool = Depends(check_auth)):
    store["discord"]["webhook"] = webhook.strip()
    save_store(store)
    return RedirectResponse("/admin", status_code=302)

@app.post("/admin/webhook/test")
async def admin_webhook_test(authorized: bool = Depends(check_auth)):
    await discord_notify_online("test_channel", {"display_name":"Test","title":"Hello","game_name":"Demo","started_at":now_iso()})
    return RedirectResponse("/admin", status_code=302)

@app.post("/admin/streamers/add")
async def admin_streamer_add(login: str = Form(...), authorized: bool = Depends(check_auth)):
    login = login.strip().lower()
    if not login:
        return RedirectResponse("/admin", status_code=302)

    user = await resolve_login_to_user(login)
    if not user:
        return PlainTextResponse("User not found", status_code=400)

    store["streamers"][login] = {
        "user_id": user["user_id"],
        "display_name": user["display_name"],
        "is_live": False,
        "last_live": store["streamers"].get(login, {}).get("last_live", ""),
        "started_at": "",
        "title": "",
        "game_id": "",
        "game_name": ""
    }
    save_store(store)

    # Subscribe to events for this streamer
    await start_eventsub()
    if es:
        await es.listen_stream_online(broadcaster_user_id=user["user_id"], callback=on_online)
        await es.listen_stream_offline(broadcaster_user_id=user["user_id"], callback=on_offline)
        await es.listen_channel_update(broadcaster_user_id=user["user_id"], callback=on_channel_update)

    await push_update_to_clients()
    return RedirectResponse("/admin", status_code=302)

@app.post("/admin/streamers/remove")
async def admin_streamer_remove(login: str = Form(...), authorized: bool = Depends(check_auth)):
    login = login.strip().lower()
    if login in store["streamers"]:
        del store["streamers"][login]
        save_store(store)
        await push_update_to_clients()
        # For perfect hygiene, you could resubscribe_all() to drop old subs.
    return RedirectResponse("/admin", status_code=302)

@app.get("/api/status")
async def api_status():
    return {"streamers": store["streamers"]}

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    WS_CLIENTS.append(ws)
    # Send initial state
    await ws.send_text(json.dumps({"type":"full_update", "streamers": store["streamers"]}))
    try:
        while True:
            # Keep connection open; we don't expect incoming messages
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        try:
            WS_CLIENTS.remove(ws)
        except ValueError:
            pass

# ---------------- Startup ----------------
@app.on_event("startup")
async def on_startup():
    await ensure_twitch_client()
    await start_eventsub()

# ---------------- Main ----------------
if __name__ == "__main__":
    # Bind to 0.0.0.0 for servers; use 127.0.0.1 locally if you prefer.
    uvicorn.run(app, host="0.0.0.0", port=8000)
