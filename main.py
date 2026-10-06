import os, re, json, sys, time, secrets, shutil, threading, subprocess, urllib.request, urllib.parse
from collections import deque
from pathlib import Path
import psutil
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

DATA = Path(os.getenv("DATA_DIR", "./data"))
BOTS = DATA / "bots"
DB = DATA / "bots.json"
BOTS.mkdir(parents=True, exist_ok=True)
PASSWORD = os.environ["PANEL_PASSWORD"]
TG_TOKEN = os.getenv("NOTIFY_BOT_TOKEN")
TG_CHAT = os.getenv("NOTIFY_CHAT_ID")
NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

bots = json.loads(DB.read_text()) if DB.exists() else {}
procs, logs, busy = {}, {}, set()


def save():
    DB.write_text(json.dumps(bots, indent=1))


def log(name, line):
    line = re.sub(r"\d{8,10}:[A-Za-z0-9_-]{35}", "<token>", line.rstrip("\n"))
    logs.setdefault(name, deque(maxlen=800)).append(line)


def notify(text):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text[:4000]}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data, timeout=10)
    except Exception:
        pass


def run(name, cmd):
    log(name, "$ " + " ".join(cmd))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in p.stdout:
        log(name, line)
    return p.wait()


PIPMAP = {"telebot": "pyTelegramBotAPI", "telegram": "python-telegram-bot", "dotenv": "python-dotenv",
          "PIL": "Pillow", "bs4": "beautifulsoup4", "yaml": "PyYAML", "cv2": "opencv-python-headless",
          "sklearn": "scikit-learn", "dateutil": "python-dateutil", "jwt": "PyJWT", "Crypto": "pycryptodome",
          "serial": "pyserial", "attr": "attrs", "magic": "python-magic", "OpenSSL": "pyOpenSSL", "git": "GitPython",
          "docx": "python-docx", "dns": "dnspython", "socks": "PySocks", "psycopg2": "psycopg2-binary",
          "MySQLdb": "mysqlclient", "Cryptodome": "pycryptodomex", "nacl": "PyNaCl", "usb": "pyusb",
          "zmq": "pyzmq", "skimage": "scikit-image", "fitz": "PyMuPDF"}


def autofix(name, pkg):
    try:
        notify(f"🔧 {name}: kurang library {pkg}, gue install otomatis")
        d = BOTS / name
        if run(name, [str(d / "venv/bin/pip"), "install", pkg]) == 0:
            with open(req_path(d / "app"), "a") as f:
                f.write("\n" + pkg + "\n")
            bots[name]["next_try"] = 0
            bots[name]["last_error"] = ""
        else:
            notify(f"❌ {name}: gagal install {pkg}")
    finally:
        busy.discard(name)
        save()


def req_path(app):
    r = app / "requirements.txt"
    return r if r.exists() else next(app.rglob("requirements.txt"), r)


def install(name):
    b, d = bots[name], BOTS / name
    app = d / "app"
    try:
        d.mkdir(exist_ok=True)
        if b["repo"].startswith("http"):
            if (app / ".git").exists():
                rc = run(name, ["git", "-C", str(app), "pull"])
            else:
                shutil.rmtree(app, ignore_errors=True)
                rc = run(name, ["git", "clone", "--depth", "1", b["repo"], str(app)])
            if rc:
                raise RuntimeError("git gagal")
        if not (d / "venv").exists() and run(name, [sys.executable, "-m", "venv", str(d / "venv")]):
            raise RuntimeError("gagal bikin venv")
        req = req_path(app)
        pip = str(d / "venv/bin/pip")
        if req.exists() and run(name, [pip, "install", "-r", str(req)]):
            log(name, "pip -r gagal, coba satu-satu...")
            for line in req.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    run(name, [pip, "install", line])
        b["installed"] = True
        log(name, "== install selesai ==")
    except Exception as e:
        log(name, f"== install error: {e} ==")
        notify(f"❌ [{name}] install gagal: {e}")
    finally:
        busy.discard(name)
        save()


def alive(name):
    p = procs.get(name)
    return p is not None and p.poll() is None


def pump(name, p):
    for line in p.stdout:
        log(name, line)
        if "Traceback" in line or re.search(r"\b\w*(Error|Exception)\b", line):
            bots[name]["last_error"] = line.strip()[:300]
    log(name, f"== proses berhenti (kode {p.wait()}) ==")


def start(name):
    b, d = bots[name], BOTS / name
    if alive(name):
        return
    if not b.get("installed"):
        raise HTTPException(400, "Install dulu")
    skip = ("PANEL_PASSWORD", "NOTIFY_BOT_TOKEN", "NOTIFY_CHAT_ID")
    env = {k: v for k, v in os.environ.items() if k not in skip}
    env.update(b["env"])
    env["PYTHONUNBUFFERED"] = "1"
    p = subprocess.Popen([str(d / "venv/bin/python"), "-u", b["entry"]], cwd=d / "app", env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    procs[name] = p
    b["desired"], b["started_at"], b["last_error"] = "running", time.time(), ""
    save()
    threading.Thread(target=pump, args=(name, p), daemon=True).start()


def stop(name, desired="stopped"):
    bots[name]["desired"] = desired
    save()
    p = procs.get(name)
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(8)
        except subprocess.TimeoutExpired:
            p.kill()


def supervisor():
    while True:
        time.sleep(5)
        for name, b in list(bots.items()):
            if name in busy or b.get("desired") != "running" or alive(name) or not b.get("installed"):
                continue
            mm = re.search(r"No module named '([\w.]+)'", b.get("last_error", ""))
            if mm:
                mod = mm.group(1).split(".")[0]
                fx = b.setdefault("fixes", {})
                if fx.get(mod, 0) < 2:
                    fx[mod] = fx.get(mod, 0) + 1
                    busy.add(name)
                    threading.Thread(target=autofix, args=(name, PIPMAP.get(mod, mod)), daemon=True).start()
                    continue
            if time.time() < b.get("next_try", 0):
                continue
            if time.time() - b.get("started_at", 0) > 300:
                b["restarts"] = 0
            b["restarts"] = b.get("restarts", 0) + 1
            b["next_try"] = time.time() + min(300, 5 * 2 ** min(b["restarts"], 6))
            p = procs.get(name)
            tail = "\n".join(list(logs.get(name, []))[-8:])
            if p:
                notify(f"⚠️ {name} berhenti (kode {p.returncode}), restart #{b['restarts']}\n\n{tail}")
            try:
                start(name)
            except Exception:
                pass


threading.Thread(target=supervisor, daemon=True).start()
PAGE = r"""<!doctype html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Panel bot</title>
<style>
:root{--bg:#eef1f4;--card:#fff;--ink:#1b2430;--mute:#5d6b7a;--line:#d6dce3;--accent:#0f766e;--bad:#b42318;--warn:#b54708;--ok:#067647}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.app{display:grid;grid-template-columns:280px 1fr;min-height:100vh}
aside{background:var(--card);border-right:1px solid var(--line);padding:16px;display:flex;flex-direction:column;gap:10px}
main{padding:20px;max-width:900px}
h1{font-size:18px;margin:0 0 6px}h2{margin:0;font-size:22px}
.item{display:flex;align-items:center;gap:8px;padding:9px 10px;border-radius:6px;cursor:pointer}
.item small{margin-left:auto;color:var(--mute)}
.item.on{background:#e3f1ef}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mute)}
.running{background:var(--ok)}.crashed{background:var(--bad)}.installing{background:var(--warn)}
button{font:inherit;padding:8px 14px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink);cursor:pointer}
button.main{background:var(--accent);border-color:var(--accent);color:#fff}
button.danger{color:var(--bad)}
button:focus-visible,input:focus-visible,textarea:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.full{width:100%}
header{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.pill{padding:2px 10px;border-radius:99px;background:var(--line);font-size:13px}
.mute{color:var(--mute)}
.err{background:#fdecea;border-left:3px solid var(--bad);padding:8px 12px;margin:10px 0;word-break:break-word}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}
label{display:block;margin:14px 0 4px;font-weight:600}
textarea,input{width:100%;font:inherit;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--card)}
textarea{font-family:ui-monospace,Menlo,monospace;font-size:13px}
pre{background:#14181f;color:#d7dde5;padding:12px;border-radius:6px;height:340px;overflow:auto;font-size:12.5px;margin:0;white-space:pre-wrap;word-break:break-all}
dialog{border:1px solid var(--line);border-radius:10px;width:min(440px,92vw)}
[hidden]{display:none!important}
#login{position:fixed;inset:0;background:var(--bg);display:grid;place-items:center;z-index:9}
#login form{background:var(--card);padding:24px;border-radius:10px;border:1px solid var(--line);display:grid;gap:12px;width:min(320px,90vw)}
@media(max-width:720px){.app{grid-template-columns:1fr}aside{border-right:0;border-bottom:1px solid var(--line)}}
</style></head><body>
<div id="login" hidden><form onsubmit="doLogin(event)"><h2>Masuk ke panel</h2>
<input id="pw" type="password" placeholder="Password panel" autocomplete="current-password"><button class="main">Masuk</button></form></div>
<div class="app">
<aside><h1>Panel bot</h1><div id="list"></div>
<button class="main full" onclick="document.querySelector('#new').showModal()">Tambah bot</button></aside>
<main>
<p id="empty" class="mute">Pilih bot di samping, atau tambah bot baru.</p>
<section id="detail" hidden>
<header><h2 id="d-name"></h2><span id="d-status" class="pill"></span></header>
<p class="mute" id="d-meta"></p>
<p id="d-err" class="err" hidden></p>
<div class="btns">
<button class="main" onclick="act('start')">Start</button>
<button onclick="act('stop')">Stop</button>
<button onclick="act('restart')">Restart</button>
<button onclick="act('install')">Install / update</button>
<button class="danger" onclick="del()">Hapus</button></div>
<label for="d-env">Environment (satu baris satu KEY=VALUE)</label>
<textarea id="d-env" rows="4"></textarea>
<div class="btns"><button onclick="saveEnv()">Simpan env</button></div>
<label>Log</label><pre id="d-log"></pre>
</section></main></div>
<dialog id="new"><form method="dialog" onsubmit="addBot(event)">
<h2>Tambah bot</h2>
<label for="n-name">Nama</label><input id="n-name" required placeholder="bot-toko">
<label for="n-repo">URL repo Git</label><input id="n-repo" required placeholder="https://github.com/user/bot.git">
<label for="n-entry">File utama</label><input id="n-entry" value="main.py">
<label for="n-env">Environment</label><textarea id="n-env" rows="3" placeholder="BOT_TOKEN=123:abc"></textarea>
<div class="btns"><button class="main">Tambah dan install</button><button type="button" onclick="document.querySelector('#new').close()">Batal</button></div>
</form></dialog>
<script>
const $=s=>document.querySelector(s);
let KEY=localStorage.getItem('k')||'',cur=null,bots=[];
const parseEnv=t=>Object.fromEntries(t.split('\n').map(l=>l.trim()).filter(l=>l&&l.includes('=')).map(l=>{const i=l.indexOf('=');return[l.slice(0,i).trim(),l.slice(i+1).trim()]}));
const fmtEnv=e=>Object.entries(e).map(([k,v])=>k+'='+v).join('\n');
async function api(path,opt={}){
 const r=await fetch('/api'+path,{...opt,headers:{'X-Panel-Key':KEY,'Content-Type':'application/json'}});
 if(r.status==401){$('#login').hidden=false;throw 0}
 const j=await r.json().catch(()=>({}));
 if(!r.ok){alert(j.detail||'Gagal');throw 0}
 return j}
function doLogin(e){e.preventDefault();KEY=$('#pw').value;localStorage.setItem('k',KEY);$('#login').hidden=true;refresh()}
function renderList(){
 $('#list').innerHTML=bots.map(b=>`<div class="item ${b.name==cur?'on':''}" onclick="sel('${b.name}')"><i class="dot ${b.status}"></i>${b.name}<small>${b.status}</small></div>`).join('')}
function sel(n){cur=n;const b=bots.find(x=>x.name==n);$('#d-env').value=fmtEnv(b.env);renderList();renderDetail();loadLogs()}
function renderDetail(){
 const b=bots.find(x=>x.name==cur);
 $('#empty').hidden=!!b;$('#detail').hidden=!b;if(!b)return;
 $('#d-name').textContent=b.name;$('#d-status').textContent=b.status;
 const bits=[b.entry,b.repo];
 if(b.ram_mb!=null)bits.push(b.ram_mb+' MB RAM');
 if(b.uptime!=null)bits.push('aktif '+Math.floor(b.uptime/60)+' menit');
 if(b.restarts)bits.push(b.restarts+' kali restart');
 $('#d-meta').textContent=bits.join(' | ');
 $('#d-err').hidden=!b.last_error;$('#d-err').textContent='Error terakhir: '+b.last_error}
async function loadLogs(){
 if(!cur)return;const pre=$('#d-log');
 const near=pre.scrollHeight-pre.scrollTop-pre.clientHeight<60;
 pre.textContent=(await api('/bots/'+cur+'/logs')).lines.join('\n');
 if(near)pre.scrollTop=pre.scrollHeight}
async function refresh(){try{bots=await api('/bots');renderList();renderDetail();loadLogs()}catch(e){}}
async function act(a){await api('/bots/'+cur+'/'+a,{method:'POST'});refresh()}
async function saveEnv(){await api('/bots/'+cur+'/env',{method:'PUT',body:JSON.stringify({env:parseEnv($('#d-env').value)})});alert('Env disimpan. Restart bot supaya berlaku.');refresh()}
async function del(){if(confirm('Hapus bot '+cur+' beserta filenya?')){await api('/bots/'+cur,{method:'DELETE'});cur=null;refresh()}}
async function addBot(e){
 const name=$('#n-name').value.trim();
 try{await api('/bots',{method:'POST',body:JSON.stringify({name,repo:$('#n-repo').value.trim(),entry:$('#n-entry').value.trim()||'main.py',env:parseEnv($('#n-env').value)})});
 await refresh();sel(name)}catch(x){e.preventDefault();return}}
refresh();setInterval(refresh,3000);
</script></body></html>
"""
app = FastAPI()


def auth(x_panel_key: str = Header("")):
    if not secrets.compare_digest(x_panel_key, PASSWORD):
        raise HTTPException(401, "Password salah")


def info(name):
    b = bots[name]
    if name in busy:
        status = "installing"
    elif alive(name):
        status = "running"
    else:
        status = "crashed" if b.get("desired") == "running" else "stopped"
    ram = up = None
    if status == "running":
        try:
            ram = psutil.Process(procs[name].pid).memory_info().rss // 2**20
            up = int(time.time() - b.get("started_at", time.time()))
        except Exception:
            pass
    return {"name": name, "repo": b["repo"], "entry": b["entry"], "env": b["env"], "status": status,
            "installed": b.get("installed", False), "last_error": b.get("last_error", ""),
            "restarts": b.get("restarts", 0), "ram_mb": ram, "uptime": up}


class NewBot(BaseModel):
    name: str
    repo: str
    entry: str = "main.py"
    env: dict[str, str] = {}


class EnvBody(BaseModel):
    env: dict[str, str]


@app.get("/")
def index():
    return HTMLResponse(PAGE)


@app.get("/api/bots", dependencies=[Depends(auth)])
def list_bots():
    return [info(n) for n in bots]


@app.post("/api/bots", dependencies=[Depends(auth)])
def add_bot(body: NewBot):
    if not NAME_RE.match(body.name):
        raise HTTPException(400, "Nama: huruf kecil, angka, - atau _ (maks 32)")
    if body.name in bots:
        raise HTTPException(409, "Nama sudah dipakai")
    bots[body.name] = {"repo": body.repo, "entry": body.entry, "env": body.env, "desired": "stopped"}
    save()
    busy.add(body.name)
    threading.Thread(target=install, args=(body.name,), daemon=True).start()
    return info(body.name)


@app.post("/api/bots/{name}/{action}", dependencies=[Depends(auth)])
def act(name: str, action: str):
    if name not in bots:
        raise HTTPException(404, "Bot tidak ada")
    if action == "install":
        if name in busy:
            raise HTTPException(409, "Sedang install")
        busy.add(name)
        threading.Thread(target=install, args=(name,), daemon=True).start()
    elif action == "start":
        bots[name]["next_try"] = 0
        start(name)
    elif action == "stop":
        stop(name)
    elif action == "restart":
        stop(name, "running")
        bots[name]["next_try"] = 0
        start(name)
    else:
        raise HTTPException(400, "Aksi tidak dikenal")
    return info(name)


@app.put("/api/bots/{name}/env", dependencies=[Depends(auth)])
def set_env(name: str, body: EnvBody):
    if name not in bots:
        raise HTTPException(404, "Bot tidak ada")
    bots[name]["env"] = body.env
    save()
    return info(name)


@app.get("/api/bots/{name}/logs", dependencies=[Depends(auth)])
def get_logs(name: str):
    return {"lines": list(logs.get(name, []))}


@app.delete("/api/bots/{name}", dependencies=[Depends(auth)])
def delete_bot(name: str):
    if name not in bots:
        raise HTTPException(404, "Bot tidak ada")
    stop(name)
    shutil.rmtree(BOTS / name, ignore_errors=True)
    del bots[name]
    save()
    return {"ok": True}


# ---------- Bot Telegram: menu tombol, bahasa biasa, deteksi otomatis ----------
import urllib.error

HELP = """Cara pakai:
- Kirim file .py atau .zip bot lu ke chat ini. Gue deteksi library dan setting-nya, lalu pasang dan jalanin sendiri.
- Ngomong biasa: "restart bot1", "matiin semua", "kenapa bot2 mati?", "log bot1".
- Atau pencet menu: /menu

Kalau ada library kurang, gue install otomatis lalu restart.
/batal - batalin proses yang lagi jalan"""

TOKEN_RE = r"\d{8,10}:[A-Za-z0-9_-]{35}"
STOP = {"ke", "di", "buat", "untuk", "library", "lib", "paket", "package", "dong", "aja", "ya", "tolong", "bot", "ulang", "lagi"}
ACTS = [
    ("diag", r"kenapa|knp|\bkok\b|\bwhy\b|error|crash|rusak|masalah|gagal|\bmati\b|\bdown\b|diem|(gak|ga|nggak|tidak) (jalan|bisa|respon|bales|ngerespon)"),
    ("log", r"\blogs?\b|riwayat"),
    ("restart", r"restart|mulai ulang|ulang|reload|segarin"),
    ("stop", r"\bstop\b|matiin|matikan|berhenti|\boff\b|hentikan"),
    ("run", r"jalanin|jalankan|nyalain|nyalakan|hidupin|hidupkan|\bstart\b|\brun\b|\bon\b|mulai"),
    ("del", r"hapus|delete|buang|remove"),
    ("status", r"status|\bcek\b|check|gimana|keadaan|\binfo\b"),
    ("list", r"daftar|\blist\b|bot apa|semua bot|punya bot|bot gue|bot gua"),
    ("add", r"tambah|nambah|bikin bot|pasang bot|upload"),
]
ICON = {"running": "🟢", "stopped": "⚪", "crashed": "🔴", "installing": "🟡"}
MAIN = [[("📋 Bot gue", "m:list"), ("➕ Tambah bot", "m:add")], [("❓ Bantuan", "m:help")]]
state, FIX = {}, {}


def tg(method, **p):
    data = urllib.parse.urlencode(p).encode()
    r = urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data, timeout=40)
    return json.loads(r.read())


def show(text, rows=None, mid=None):
    p = {"chat_id": TG_CHAT, "text": text[:4000]}
    if rows:
        p["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": a, "callback_data": b} for a, b in row] for row in rows]})
    if mid:
        try:
            return tg("editMessageText", message_id=mid, **p)
        except urllib.error.HTTPError as e:
            if b"not modified" in e.read():
                return
        except Exception:
            pass
    return tg("sendMessage", **p)


def fetch_file(file_id):
    path = tg("getFile", file_id=file_id)["result"]["file_path"]
    return urllib.request.urlopen(f"https://api.telegram.org/file/bot{TG_TOKEN}/{path}", timeout=60).read()


def parse_env(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.replace("export ", "").strip()
        if re.match(r"^\w+$", k):
            out[k] = v.strip().strip("\"'")
    return out


def detect_libs(content, local=()):
    import ast
    try:
        tree = ast.parse(content.decode("utf-8", "ignore"))
    except Exception:
        return []
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            mods.add(n.module.split(".")[0])
    return [PIPMAP.get(x, x) for x in sorted(mods) if x not in sys.stdlib_module_names and x not in local]


def token_vars(text):
    names = re.findall(r"(?:getenv|environ(?:\.get)?)\s*[\(\[]\s*[\"'](\w+)[\"']", text)
    found = [n for n in dict.fromkeys(names) if "TOKEN" in n.upper()]
    return found[:1] if found else ["BOT_TOKEN", "TOKEN", "TELEGRAM_BOT_TOKEN", "API_TOKEN"]


def analyze(fname, content):
    low, texts, libs, env = fname.lower(), "", set(), {}
    if low.endswith(".py"):
        texts = content.decode("utf-8", "ignore")
        libs = set(detect_libs(content))
    elif low.endswith(".zip"):
        import io, zipfile
        try:
            z = zipfile.ZipFile(io.BytesIO(content))
            names = [n for n in z.namelist() if not n.endswith("/") and "site-packages" not in n and "__MACOSX" not in n]
            pys = [n for n in names if n.endswith(".py")]
            local = {os.path.basename(n)[:-3] for n in pys} | {p for n in names for p in n.split("/")[:-1]}
            for n in pys:
                t = z.read(n)
                if len(t) < 500000:
                    libs |= set(detect_libs(t, local))
                    texts += t.decode("utf-8", "ignore") + "\n"
            for n in names:
                if os.path.basename(n) == ".env":
                    env.update(parse_env(z.read(n).decode("utf-8", "ignore")))
        except Exception:
            pass
    has_token = bool(re.search(TOKEN_RE, texts)) or any("TOKEN" in k.upper() and v for k, v in env.items())
    return {"libs": sorted(libs), "env": env, "tvars": token_vars(texts), "has_token": has_token}


def derive_name(fname):
    return re.sub(r"[^a-z0-9_-]+", "-", os.path.splitext(fname)[0].lower()).strip("-")[:32] or "bot"


def apply_upload(name, fname, content, env, pips, tvars=None):
    if name in busy:
        return "Masih install, tunggu dulu."
    low = fname.lower()
    d = BOTS / name
    app_dir = d / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    b = bots.setdefault(name, {"repo": "(upload)", "entry": "main.py", "env": {}, "desired": "stopped"})
    b["env"].update(env)
    if tvars:
        b["tvars"] = tvars
    if low.endswith(".zip"):
        zp = d / "upload.zip"
        zp.write_bytes(content)
        shutil.unpack_archive(str(zp), str(app_dir), "zip")
        pys = sorted(app_dir.rglob("*.py"), key=lambda p: (p.name not in ("main.py", "bot.py", "app.py"), len(p.parts)))
        if not pys:
            return "Gak ada file .py di zip itu."
        b["entry"] = str(pys[0].relative_to(app_dir))
    elif low.endswith(".py"):
        (app_dir / fname).write_bytes(content)
        b["entry"] = fname
    elif low.endswith(".txt"):
        (app_dir / "requirements.txt").write_bytes(content)
    if pips:
        req = req_path(app_dir)
        have = set()
        if req.exists():
            have = {re.split(r"[<>=!~\[; ]", l.strip(), maxsplit=1)[0].lower().replace("_", "-")
                    for l in req.read_text().splitlines() if l.strip() and not l.startswith("#")}
        new = [p for p in pips if p.lower().replace("_", "-") not in have]
        if new:
            with open(req, "a") as f:
                f.write("\n" + "\n".join(new) + "\n")
    save()
    busy.add(name)
    threading.Thread(target=install_and_run, args=(name,), daemon=True).start()
    return None


def install_and_run(name):
    install(name)
    b = bots[name]
    if b.get("installed") and (BOTS / name / "app" / b["entry"]).exists():
        try:
            stop(name, "running")
            b["next_try"] = 0
            start(name)
            show(f"✅ {name} jalan.", [[("Buka bot", f"b:{name}"), ("📜 Log", f"a:log:{name}")]])
        except Exception as e:
            show(f"❌ {name} gagal jalan: {e}")
    elif b.get("installed"):
        show(f"{name}: requirements terpasang.")


def emoji(name):
    return ICON.get(info(name)["status"], "⚪")


def main_menu(mid=None):
    show("Mau ngapain? 👇", MAIN, mid)


def list_view(mid=None):
    if not bots:
        return show("Belum ada bot. Kirim file .py atau .zip bot lu ke sini, gue pasang otomatis.", [[("⬅️ Menu", "m:main")]], mid)
    rows = [[(f"{emoji(n)} {n}", f"b:{n}")] for n in bots] + [[("⬅️ Menu", "m:main")]]
    show("Pilih bot:", rows, mid)


def bot_view(name, mid=None, note=""):
    i = info(name)
    t = f"{ICON.get(i['status'], '⚪')} {name} ({i['status']})"
    if i["ram_mb"] is not None:
        t += f"\nRAM {i['ram_mb']} MB, aktif {(i['uptime'] or 0) // 60} menit"
    if i["restarts"]:
        t += f"\nRestart otomatis: {i['restarts']}x"
    if i["last_error"]:
        t += f"\nError terakhir: {i['last_error'][:150]}"
    if note:
        t = note + "\n\n" + t
    rows = [[("▶️ Jalanin", f"a:run:{name}"), ("⏹ Stop", f"a:stop:{name}"), ("🔄 Restart", f"a:restart:{name}")],
            [("📜 Log", f"a:log:{name}"), ("🩺 Cek masalah", f"a:diag:{name}")],
            [("📦 Library", f"a:pip:{name}"), ("⚙️ Env", f"a:env:{name}"), ("🔑 Token", f"a:tok:{name}")],
            [("🗑 Hapus", f"a:del:{name}"), ("⬅️ Kembali", "m:list")]]
    show(t, rows, mid)


def do(name, action, mid=None, note=None):
    try:
        act(name, {"run": "start"}.get(action, action))
    except HTTPException as e:
        return bot_view(name, mid, f"⚠️ {e.detail}")
    bot_view(name, mid, note or {"run": "▶️ Dijalanin", "stop": "⏹ Dihentikan", "restart": "🔄 Direstart"}[action])


def log_view(name, mid=None):
    body = "\n".join(list(logs.get(name, []))[-20:])[-3500:] or "Log kosong"
    show(f"📜 {name}\n{body}", [[("↻ Refresh", f"a:log:{name}"), ("⬅️ Bot", f"b:{name}")]], mid)


def diag_view(name, mid=None):
    b, i = bots[name], info(name)
    blob = "\n".join(list(logs.get(name, []))[-40:]) + "\n" + b.get("last_error", "")
    tips, rows = [], []
    m = re.search(r"No module named '([\w.]+)'", blob)
    if m:
        mod = m.group(1).split(".")[0]
        FIX[name] = PIPMAP.get(mod, mod)
        tips.append(f"Library {FIX[name]} belum terpasang.")
        rows.append([(f"📦 Install {FIX[name]}", f"a:fix:{name}")])
    if re.search(r"Unauthorized|InvalidToken|Invalid token", blob):
        tips.append("Token ditolak Telegram (salah atau udah di-revoke).")
        rows.append([("🔑 Ganti token", f"a:tok:{name}")])
    if re.search(r"terminated by other getUpdates", blob):
        tips.append("Token ini lagi dipakai di tempat lain (bot jalan dobel). Matiin yang satunya.")
    k = re.search(r"KeyError: '(\w+)'", blob)
    if k:
        tips.append(f"Variabel {k.group(1)} belum diisi.")
        rows.append([("⚙️ Isi env", f"a:env:{name}")])
    if re.search(r"NetworkError|TimedOut|ConnectError|ConnectionError", blob):
        tips.append("Koneksi ke Telegram lagi bermasalah, biasanya sementara.")
    if re.search(r"SyntaxError|IndentationError", blob):
        tips.append("Ada salah tulis di kode script (SyntaxError), perlu diperbaiki di filenya.")
    if not tips:
        if i["status"] == "running":
            tips.append("Bot hidup dan nyambung. Kalau gak bales, kemungkinan logika script-nya (perintah yang dikenal, ID admin, atau env belum diisi).")
        elif b.get("last_error"):
            tips.append("Error terakhir: " + b["last_error"][:200])
        else:
            tips.append("Gak ada error di log.")
    rows += [[("🔄 Restart", f"a:restart:{name}"), ("📜 Log", f"a:log:{name}")], [("⬅️ Bot", f"b:{name}")]]
    show(f"🩺 {name} ({i['status']})\n\n" + "\n".join("• " + t for t in tips), rows, mid)


def pip_libs(name, libs):
    if not libs:
        return show("Format: /pip nama lib1 lib2")
    if name in busy:
        return show("Masih install, tunggu dulu.")
    with open(req_path(BOTS / name / "app"), "a") as f:
        f.write("\n" + "\n".join(libs) + "\n")
    busy.add(name)
    threading.Thread(target=install_and_run, args=(name,), daemon=True).start()
    show(f"📦 Install {', '.join(libs)} ke {name}, lalu restart.", [[("⬅️ Bot", f"b:{name}")]])


def go(name, fname, content, env, pips, tvars, token=None):
    if token:
        for v in tvars:
            env[v] = token
    err = apply_upload(name, fname, content, env, pips, tvars)
    if err:
        return show(err, MAIN)
    lib = "dari requirements.txt" if fname == "requirements.txt" else (", ".join(pips) or "gak ada tambahan")
    show(f"✅ {name} ditambah\n📦 Library: {lib}\n🔑 Env: {', '.join(env) or '-'}\n\nLagi install dan jalanin. Kalau ada library kurang, gue install sendiri.",
         [[("Buka bot", f"b:{name}"), ("📜 Log", f"a:log:{name}")]])


def on_file(m):
    doc = m["document"]
    fname = os.path.basename(doc.get("file_name") or "file").replace(" ", "_")
    low = fname.lower()
    if not low.endswith((".py", ".zip", ".txt")):
        return show("Kirim file .py, .zip, atau requirements.txt ya.", MAIN)
    cap = (m.get("caption") or "").split()
    if cap and cap[0].split("@")[0].lower() == "/add":
        cap = cap[1:]
    name, env, pips, token = None, {}, [], None
    for w in cap:
        if w.startswith("pip="):
            pips += [x for x in w[4:].split(",") if x]
        elif "=" in w:
            k, v = w.split("=", 1)
            env[k] = v
        elif re.search(TOKEN_RE, w):
            token = re.search(TOKEN_RE, w).group(0)
        elif NAME_RE.match(w.lower()) and not name:
            name = w.lower()
    content = fetch_file(doc["file_id"])
    if low.endswith(".txt"):
        target = name or (list(bots)[0] if len(bots) == 1 else None)
        if target in bots:
            return go(target, "requirements.txt", content, {}, [], None)
        if not bots:
            return show("Belum ada bot buat requirements ini. Kirim file bot-nya dulu.", MAIN)
        state.clear()
        state.update(kind="req", content=content)
        return show("Requirements ini buat bot yang mana?", [[(n, f"r:{n}")] for n in bots] + [[("Batal", "m:cancel")]])
    an = analyze(fname, content)
    name = name or derive_name(fname)
    if name in bots and bots[name]["repo"] != "(upload)":
        name += "-up"
    env = {**an["env"], **env}
    pips = list(dict.fromkeys(an["libs"] + pips))
    if not token and not an["has_token"] and not any("TOKEN" in k.upper() for k in env):
        state.clear()
        state.update(kind="token", name=name, fname=fname, content=content, env=env, pips=pips, tvars=an["tvars"])
        return show(f"📥 {fname} diterima. Bot-nya gue namain {name}.\nSisa 1 hal: kirim token bot-nya (dari BotFather).",
                    [[("Lewati, gak butuh token", "t:skip"), ("Batal", "m:cancel")]])
    go(name, fname, content, env, pips, an["tvars"], token)


def finish_token(token):
    s = dict(state)
    state.clear()
    go(s["name"], s["fname"], s["content"], s["env"], s["pips"], s["tvars"], token)


def state_text(text):
    t, kind, name = text.strip(), state.get("kind"), state.get("name")
    if t.lower() in ("batal", "cancel"):
        state.clear()
        return show("Dibatalin.", MAIN)
    if kind in ("token", "tok"):
        if not re.match(r"^\d+:[\w-]+$", t):
            return show("Itu kayaknya bukan token. Contoh: 123456:ABC-xyz. Kirim ulang, atau pencet Batal.",
                        [[("Batal", "m:cancel")]])
        if kind == "token":
            return finish_token(t)
        for v in bots[name].get("tvars") or ["BOT_TOKEN"]:
            bots[name]["env"][v] = t
        save()
        state.clear()
        return do(name, "restart", None, "🔑 Token diganti.")
    if kind == "pip":
        state.clear()
        return pip_libs(name, [x for x in t.replace(",", " ").split() if x])
    if kind == "env":
        pairs = dict(x.split("=", 1) for x in t.split() if "=" in x)
        state.clear()
        if not pairs:
            return show("Format: KEY=VALUE (boleh banyak, pisah spasi).", [[("⬅️ Bot", f"b:{name}")]])
        bots[name]["env"].update(pairs)
        save()
        return do(name, "restart", None, "⚙️ Env disimpan.")
    state.clear()
    nlu(text)


def action(a, name, mid=None):
    if a in ("run", "stop", "restart"):
        do(name, a, mid)
    elif a == "status":
        bot_view(name, mid)
    elif a == "log":
        log_view(name, mid)
    elif a == "diag":
        diag_view(name, mid)
    elif a in ("pip", "env", "tok"):
        state.clear()
        state.update(kind=a, name=name)
        prompt = {"pip": "Ketik library yang mau dipasang (pisah koma/spasi). Contoh: requests aiogram",
                  "env": "Kirim isi env: KEY=VALUE (boleh banyak, pisah spasi). Pesannya gue hapus.",
                  "tok": "Kirim token baru dari BotFather. Pesannya gue hapus."}[a]
        show(f"{name}: {prompt}", [[("Batal", f"c:{name}")]], mid)
    elif a == "del":
        show(f"Yakin hapus {name} beserta filenya?", [[("Ya, hapus", f"a:delok:{name}"), ("Batal", f"b:{name}")]], mid)
    elif a == "delok":
        delete_bot(name)
        list_view(mid)
    elif a == "fix":
        pkg = FIX.get(name)
        if not pkg or name in busy:
            return bot_view(name, mid, "Masih sibuk atau gak ada yang diperbaiki.")
        busy.add(name)
        threading.Thread(target=autofix, args=(name, pkg), daemon=True).start()
        bot_view(name, mid, f"🔧 Install {pkg}, nanti otomatis jalan lagi.")


def nlu(text):
    t = text.lower().strip()
    if re.fullmatch(r"(halo|hai|hi|menu|start|p|woi|oi|tes|test)\W*", t):
        return main_menu()
    names = [n for n in bots if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", t)]
    m = re.search(r"\b(?:install|pip)\s+([\w\-.=<>, ]+)", t)
    if m and names:
        words = [w for w in re.split(r"[\s,]+", m.group(1)) if w and w not in names and w not in STOP]
        if words:
            return pip_libs(names[0], words)
    a = next((x for x, rx in ACTS if re.search(rx, t)), None)
    if a == "add":
        return show("Kirim aja file .py atau .zip bot lu ke chat ini, gue pasang otomatis.", MAIN)
    if a == "list" and not names:
        return list_view()
    allw = bool(re.search(r"\b(semua|semuanya|all)\b", t))
    if not names:
        if a is None or not bots:
            return show("Gue belum nangkep 😅 Coba pencet menu, atau kirim file bot lu langsung.", MAIN)
        if allw and a in ("run", "stop", "restart"):
            names = list(bots)
        elif len(bots) == 1:
            names = list(bots)
        else:
            return show("Bot yang mana?", [[(f"{emoji(n)} {n}", f"a:{a}:{n}")] for n in list(bots)[:20]])
    n = names[0]
    if a in ("run", "stop", "restart"):
        if len(names) > 1:
            res = []
            for x in names:
                try:
                    act(x, {"run": "start"}.get(a, a))
                    res.append(f"{x}: ok")
                except HTTPException as e:
                    res.append(f"{x}: {e.detail}")
            return show("\n".join(res), [[("📋 Bot gue", "m:list")]])
        return do(n, a)
    if a in ("diag", "log", "del"):
        return action(a, n)
    bot_view(n)


def command(text):
    parts = text.split()
    cmd, args = parts[0].split("@")[0].lower(), parts[1:]
    if cmd in ("/start", "/menu"):
        return main_menu()
    if cmd == "/help":
        return show(HELP, [[("⬅️ Menu", "m:main")]])
    if cmd == "/bots":
        return list_view()
    if cmd == "/batal":
        state.clear()
        return show("Dibatalin.", MAIN)
    try:
        if cmd == "/add":
            if len(args) < 2:
                return show("Format: /add nama repo [file] [KEY=VAL ...]\nAtau kirim file .py/.zip langsung.")
            rest, entry = args[2:], "main.py"
            if rest and "=" not in rest[0]:
                entry = rest.pop(0)
            env = dict(a.split("=", 1) for a in rest if "=" in a)
            add_bot(NewBot(name=args[0].lower(), repo=args[1], entry=entry, env=env))
            return show(f"{args[0]} ditambah, lagi install. Cek: /logs {args[0]}")
        if not args:
            return show("Sebutin nama bot-nya. Contoh: " + cmd + " bot1")
        name = args[0].lower()
        if name not in bots:
            return show("Bot tidak ada. Cek /bots")
        if cmd in ("/run", "/stop", "/restart"):
            return do(name, cmd[1:])
        if cmd == "/logs":
            return log_view(name)
        if cmd == "/pip":
            return pip_libs(name, [x for x in " ".join(args[1:]).replace(",", " ").split() if x])
        if cmd == "/env":
            bots[name]["env"].update(dict(a.split("=", 1) for a in args[1:] if "=" in a))
            save()
            return show(f"Env disimpan. /restart {name} supaya berlaku.")
        if cmd == "/install":
            act(name, "install")
            return show(f"{name}: install dimulai. Cek /logs {name}")
        if cmd == "/del":
            delete_bot(name)
            return show(f"{name} dihapus", MAIN)
        show("Perintah tidak dikenal. Coba /menu")
    except HTTPException as e:
        show(f"Gagal: {e.detail}")


def on_callback(cb):
    mid = cb["message"]["message_id"]
    k = cb.get("data", "").split(":")
    if k[0] == "m":
        if k[1] == "main":
            main_menu(mid)
        elif k[1] == "list":
            list_view(mid)
        elif k[1] == "add":
            show("Kirim file .py atau .zip bot lu ke chat ini. Gue deteksi library dan setting-nya, lalu jalanin otomatis.", [[("⬅️ Menu", "m:main")]], mid)
        elif k[1] == "help":
            show(HELP, [[("⬅️ Menu", "m:main")]], mid)
        elif k[1] == "cancel":
            state.clear()
            main_menu(mid)
    elif k[0] == "b" and k[1] in bots:
        bot_view(k[1], mid)
    elif k[0] == "c" and k[1] in bots:
        state.clear()
        bot_view(k[1], mid)
    elif k[0] == "a" and len(k) >= 3 and k[2] in bots:
        action(k[1], k[2], mid)
    elif k[0] == "t" and state.get("kind") == "token":
        finish_token(None)
    elif k[0] == "r" and state.get("kind") == "req" and k[1] in bots:
        content = state["content"]
        state.clear()
        go(k[1], "requirements.txt", content, {}, [], None)


def tg_loop():
    offset = 0
    while True:
        try:
            for u in tg("getUpdates", offset=offset, timeout=30).get("result", []):
                offset = u["update_id"] + 1
                try:
                    cb = u.get("callback_query")
                    if cb:
                        if str(cb["message"]["chat"]["id"]) == str(TG_CHAT):
                            tg("answerCallbackQuery", callback_query_id=cb["id"])
                            on_callback(cb)
                        continue
                    m = u.get("message") or {}
                    if str(m.get("chat", {}).get("id")) != str(TG_CHAT):
                        continue
                    text = m.get("text") or m.get("caption") or ""
                    secret = bool(re.search(TOKEN_RE, text)) or text.startswith(("/add", "/env")) or state.get("kind") in ("token", "env", "tok")
                    if m.get("document"):
                        on_file(m)
                    elif text.startswith("/"):
                        command(text)
                    elif state:
                        state_text(text)
                    elif text:
                        nlu(text)
                    if secret and text:
                        try:
                            tg("deleteMessage", chat_id=TG_CHAT, message_id=m["message_id"])
                        except Exception:
                            pass
                except Exception as e:
                    show(f"Error: {e}")
        except Exception:
            time.sleep(5)


if TG_TOKEN and TG_CHAT:
    threading.Thread(target=tg_loop, daemon=True).start()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
