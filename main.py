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
    logs.setdefault(name, deque(maxlen=800)).append(line.rstrip("\n"))


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
        req = app / "requirements.txt"
        if not req.exists():
            req = next(app.rglob("requirements.txt"), req)
        if req.exists() and run(name, [str(d / "venv/bin/pip"), "install", "-r", str(req)]):
            raise RuntimeError("pip install gagal")
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


# ---------- Kontrol lewat Telegram ----------
HELP = """Perintah:
/bots - daftar bot dan statusnya
/add nama repo [file] [KEY=VAL ...] - tambah bot
/env nama KEY=VAL ... - ubah env
/install nama - install atau update
/run nama - jalankan
/stop nama
/restart nama
/logs nama [jumlah baris]
/del nama - hapus bot

Kirim file .py (atau .zip / requirements.txt) dengan caption:
/add nama KEY=VAL pip=lib1,lib2"""


def tg(method, **p):
    data = urllib.parse.urlencode(p).encode()
    r = urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data, timeout=40)
    return json.loads(r.read())


def handle(text):
    parts = text.split()
    cmd, args = parts[0].split("@")[0].lower(), parts[1:]
    try:
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/bots":
            return "\n".join(f"{n}: {info(n)['status']}" for n in bots) or "Belum ada bot"
        if cmd == "/add":
            if len(args) < 2:
                return "Format: /add nama repo [file] [KEY=VAL ...]"
            rest, entry = args[2:], "main.py"
            if rest and "=" not in rest[0]:
                entry = rest.pop(0)
            env = dict(a.split("=", 1) for a in rest if "=" in a)
            add_bot(NewBot(name=args[0].lower(), repo=args[1], entry=entry, env=env))
            return f"{args[0]} ditambah, lagi install. Cek: /logs {args[0]}"
        if not args:
            return "Sebutin nama bot-nya. Contoh: " + cmd + " bot1"
        name = args[0].lower()
        if name not in bots:
            return "Bot tidak ada. Cek /bots"
        if cmd == "/env":
            bots[name]["env"].update(dict(a.split("=", 1) for a in args[1:] if "=" in a))
            save()
            return "Env disimpan. /restart " + name + " supaya berlaku."
        if cmd in ("/install", "/run", "/stop", "/restart"):
            act(name, {"/run": "start"}.get(cmd, cmd[1:]))
            return f"{name}: {cmd[1:]} oke. Cek /logs {name}"
        if cmd == "/logs":
            n = int(args[1]) if len(args) > 1 else 15
            return "\n".join(list(logs.get(name, []))[-n:]) or "Log kosong"
        if cmd == "/del":
            delete_bot(name)
            return f"{name} dihapus"
        return "Perintah tidak dikenal. /help"
    except HTTPException as e:
        return f"Gagal: {e.detail}"
    except Exception as e:
        return f"Error: {e}"


def say(t):
    tg("sendMessage", chat_id=TG_CHAT, text=t[:4000])


def install_and_run(name):
    install(name)
    b = bots[name]
    if b.get("installed") and (BOTS / name / "app" / b["entry"]).exists():
        try:
            stop(name, "running")
            b["next_try"] = 0
            start(name)
            say(f"✅ {name} jalan. Cek /logs {name}")
        except Exception as e:
            say(f"❌ {name} gagal jalan: {e}")
    elif b.get("installed"):
        say(f"{name}: requirements terpasang. Kirim file .py bot-nya.")


def handle_doc(m):
    cap = m.get("caption", "").split()
    if len(cap) < 2 or cap[0].split("@")[0].lower() != "/add":
        return "Kirim file dengan caption: /add nama [KEY=VAL ...] [pip=lib1,lib2]"
    name = cap[1].lower()
    if not NAME_RE.match(name):
        return "Nama: huruf kecil, angka, - atau _ (maks 32)"
    if name in busy:
        return "Masih install, tunggu dulu."
    env, pips = {}, []
    for a in cap[2:]:
        if a.startswith("pip="):
            pips += [x for x in a[4:].split(",") if x]
        elif "=" in a:
            k, v = a.split("=", 1)
            env[k] = v
    doc = m["document"]
    fname = os.path.basename(doc.get("file_name") or "file").replace(" ", "_")
    low = fname.lower()
    path = tg("getFile", file_id=doc["file_id"])["result"]["file_path"]
    content = urllib.request.urlopen(f"https://api.telegram.org/file/bot{TG_TOKEN}/{path}", timeout=60).read()
    d = BOTS / name
    app_dir = d / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    b = bots.setdefault(name, {"repo": "(upload)", "entry": "main.py", "env": {}, "desired": "stopped"})
    b["env"].update(env)
    if low.endswith(".zip"):
        zp = d / "upload.zip"
        zp.write_bytes(content)
        shutil.unpack_archive(str(zp), str(app_dir), "zip")
        pys = sorted(app_dir.rglob("*.py"), key=lambda p: (p.name not in ("main.py", "bot.py", "app.py"), len(p.parts)))
        if not pys:
            return "Gak ada file .py di zip itu"
        b["entry"] = str(pys[0].relative_to(app_dir))
    elif low.endswith(".py"):
        (app_dir / fname).write_bytes(content)
        b["entry"] = fname
    elif low.endswith(".txt"):
        (app_dir / "requirements.txt").write_bytes(content)
    else:
        return "Kirim .py, .zip, atau requirements.txt"
    if pips:
        with open(app_dir / "requirements.txt", "a") as f:
            f.write("\n" + "\n".join(pips) + "\n")
    save()
    busy.add(name)
    threading.Thread(target=install_and_run, args=(name,), daemon=True).start()
    return f"{name} diterima, lagi install dan dijalanin. Cek /logs {name}"


def tg_loop():
    offset = 0
    while True:
        try:
            for u in tg("getUpdates", offset=offset, timeout=30).get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                text = m.get("text") or m.get("caption") or ""
                if str(m.get("chat", {}).get("id")) != str(TG_CHAT) or not text:
                    continue
                reply = handle_doc(m) if m.get("document") else handle(text)
                if text.startswith(("/add", "/env")):
                    try:
                        tg("deleteMessage", chat_id=TG_CHAT, message_id=m["message_id"])
                    except Exception:
                        pass
                tg("sendMessage", chat_id=TG_CHAT, text=reply[:4000])
        except Exception:
            time.sleep(5)


if TG_TOKEN and TG_CHAT:
    threading.Thread(target=tg_loop, daemon=True).start()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
