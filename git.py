#!/usr/bin/env python3
"""
GH Autopilot v2 - gestore intelligente del tuo profilo GitHub (UI pystyle).

Funzioni principali
  * Ti riconosce: token (keyring o file protetto), cartella, preferenze, ultimo sync
  * Sync incrementale (ETag + pushed_at): clona le mancanti, aggiorna solo le novita'
  * Aggiornamento sicuro: fetch/prune, fast-forward, autostash, altre branch, fork con upstream
  * Pubblica modifiche locali (commit automatico + push) su tutte le repo
  * Download ZIP, backup mirror, stato repo locali
  * Crea repo (vuota o da cartella, con .gitignore/README/licenza/topics, anche in organizzazioni)
  * Sostituisce una repo esistente con un'intera cartella (3 modalita', backup automatico)
  * Gestione repo: info, rinomina, visibilita', descrizione/topics, archivia, branch di default, elimina
  * Barra di progresso con percentuale, timer, ETA, repo e file gestiti + report finale salvato

Requisiti: Python 3.9+, git. UI: pystyle (se manca ti propone di installarlo).

Uso:
  python gh_autopilot.py              # modalita' interattiva
  python gh_autopilot.py --sync       # sync intelligente non interattivo (cron)
  python gh_autopilot.py --sync --full
"""
from __future__ import annotations

import argparse
import base64
import difflib
import fnmatch
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

try:
    import keyring  # opzionale
except Exception:  # pragma: no cover
    keyring = None

# ----------------------------------------------------------------------------
# pystyle (con fallback senza colori se non installato)
# ----------------------------------------------------------------------------
try:
    from pystyle import Colors, Colorate, Center, System
    HAS_PYSTYLE = True
except ImportError:
    HAS_PYSTYLE = False

    class _Stub:
        def __getattr__(self, name):
            return lambda *a, **k: (a[1] if len(a) > 1 else (a[0] if a else ""))

    class _StubColors:
        def __getattr__(self, name):
            return ""

    Colors, Colorate, Center, System = _StubColors(), _Stub(), _Stub(), _Stub()


def ensure_pystyle():
    """Se pystyle manca, propone di installarlo e lo ricarica al volo."""
    global Colors, Colorate, Center, System, HAS_PYSTYLE
    if HAS_PYSTYLE:
        return
    print("Il modulo 'pystyle' (interfaccia grafica del terminale) non è installato.")
    if input("Installarlo ora con pip? [S/n]: ").strip().lower() not in ("", "s", "si", "sì", "y", "yes"):
        return
    for extra in ([], ["--user"], ["--break-system-packages"]):
        rc = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pystyle", *extra]).returncode
        if rc == 0:
            break
    try:
        from pystyle import Colors as C, Colorate as Co, Center as Ce, System as Sy
        Colors, Colorate, Center, System, HAS_PYSTYLE = C, Co, Ce, Sy, True
    except ImportError:
        print("Installazione non riuscita: proseguo senza colori.")


APP = "gh-autopilot"
API = "https://api.github.com"

# --- TOKEN GITHUB ----------------------------------------------------------
# Priorita': variabile d'ambiente GITHUB_TOKEN > questa variabile > keyring/file > GitHub CLI.
# ATTENZIONE: non caricare mai questo file su GitHub con il token dentro
# (lo script lo blocca comunque se lo trova in una cartella da pubblicare).
GITHUB_TOKEN = ""

if os.name == "nt":
    CONF_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP
    os.system("")  # abilita ANSI su Windows
else:
    CONF_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP
CONFIG_FILE = CONF_DIR / "config.json"
CACHE_FILE = CONF_DIR / "cache.json"
TOKEN_FILE = CONF_DIR / "token"
REPORT_DIR = CONF_DIR / "reports"

TTY = sys.stdout.isatty()
PAL: dict = {}
ICON: dict = {}
ME: dict = {}
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

NAMES = {"new": "nuove/clonate", "upd": "aggiornate", "ok": "ok", "skip": "invariate", "warn": "da controllare", "err": "errori"}

DEFAULTS = {
    "user": None,
    "base_dir": str(Path.home() / "GitHub"),
    "include_forks": True,
    "include_archived": False,
    "include_orgs": True,
    "sync_forks": True,
    "submodules": True,
    "auto_backup": True,  # mirror automatico prima di sostituzioni / eliminazioni
    "strategy": "safe",  # safe = non tocca branch divergenti | rebase = prova il rebase
    "workers": 4,
    "last_sync": None,
    "repo_state": {},
    "favorites": [],
    "recent": [],
    "usage": {},
}


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
def init_ui():
    global PAL, ICON
    keys = {
        "ok": getattr(Colors, "green", ""), "err": getattr(Colors, "red", ""),
        "warn": getattr(Colors, "yellow", ""), "info": getattr(Colors, "cyan", ""),
        "acc": getattr(Colors, "purple", ""), "hi": getattr(Colors, "white", ""),
        "dim": getattr(Colors, "gray", "\033[90m" if HAS_PYSTYLE else ""),
        "rst": getattr(Colors, "reset", "\033[0m" if HAS_PYSTYLE else ""),
    }
    PAL = keys if TTY else {k: "" for k in keys}
    ICON = {
        "new": paint("⬇", "info"), "upd": paint("↻", "ok"), "ok": paint("✓", "ok"),
        "skip": paint("·", "dim"), "warn": paint("!", "warn"), "err": paint("✗", "err"),
    }


def paint(text, key):
    return f"{PAL.get(key, '')}{text}{PAL.get('rst', '')}"


def grad(name, text, vertical=False):
    """Testo con gradiente pystyle (fallback: testo semplice)."""
    if not (TTY and HAS_PYSTYLE):
        return text
    try:
        g = getattr(Colors, name, None) or Colors.cyan_to_blue
        return (Colorate.Vertical if vertical else Colorate.Horizontal)(g, text)
    except Exception:
        return text


def vlen(s):
    return len(ANSI_RE.sub("", s))


def clip(s, width):
    if vlen(s) <= width:
        return s
    out, n = [], 0
    for tok in re.split(r"(\x1b\[[0-9;]*m)", s):
        if tok.startswith("\x1b["):
            out.append(tok)
            continue
        take = tok[: max(0, width - 1 - n)]
        out.append(take)
        n += len(take)
        if n >= width - 1:
            break
    return "".join(out) + "…" + PAL.get("rst", "")


def term_cols():
    return shutil.get_terminal_size((80, 24)).columns


def print_box(lines, title=""):
    lines = [str(l).replace("\t", " ") for l in lines]
    inner = max([vlen(l) for l in lines] + [vlen(title) + 2, 34])
    inner = min(inner, term_cols() - 4)
    b, r = PAL["acc"], PAL["rst"]
    if title:
        top = f"{b}╭─ {r}{grad('blue_to_purple', title)}{b} " + "─" * (inner + 2 - vlen(title) - 3) + f"╮{r}"
    else:
        top = f"{b}╭" + "─" * (inner + 2) + f"╮{r}"
    print(top)
    for l in lines:
        l = clip(l, inner)
        print(f"{b}│{r} {l}{' ' * (inner - vlen(l))} {b}│{r}")
    print(f"{b}╰" + "─" * (inner + 2) + f"╯{r}")


def say(kind, text):
    print(f"{ICON.get(kind, '')} {text}")


def banner():
    if TTY:
        try:
            System.Clear()
        except Exception:
            print("\033[2J\033[H", end="")
    art = (
        "┏━╸ ╻ ╻   ┏━┓ ╻ ╻ ╺┳╸ ┏━┓ ┏━┓ ╻ ╻   ┏━┓ ╺┳╸\n"
        "┃╺┓ ┣━┫   ┣━┫ ┃ ┃  ┃  ┃ ┃ ┣━┛ ┃ ┃   ┃ ┃  ┃ \n"
        "┗━┛ ╹ ╹   ╹ ╹ ┗━┛  ╹  ┗━┛ ╹   ╹ ┗━╸ ┗━┛  ╹ "
    )
    txt = grad("blue_to_purple", art, vertical=True)
    try:
        if TTY and HAS_PYSTYLE:
            txt = Center.XCenter(txt)
    except Exception:
        pass
    print("\n" + txt)
    sub = "sync · backup · crea · sostituisci · gestisci"
    pad = max(0, (term_cols() - len(sub)) // 2) if TTY else 0
    print(" " * pad + paint(sub, "dim") + "\n")


def fmt_t(sec):
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def hsize(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class Progress:
    """Barra con percentuale, timer, ETA, conteggio repo/file e fase corrente."""

    def __init__(self, label, total, unit="repo"):
        self.label, self.total, self.unit = label, max(int(total), 0), unit
        self.done, self.files, self.stage = 0, 0, ""
        self.t0 = time.time()
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._th = None
        self._f = 0

    def __enter__(self):
        if TTY:
            self._th = threading.Thread(target=self._loop, daemon=True)
            self._th.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._th:
            self._th.join(timeout=1)
        with self.lock:
            if TTY:
                self.render()
                sys.stdout.write("\n")
                sys.stdout.flush()
            else:
                print(f"{self.label}: {self.done}/{self.total} {self.unit}, {self.files} file, {fmt_t(time.time() - self.t0)}")
        return False

    def _loop(self):
        while not self._stop.wait(0.12):
            with self.lock:
                self._f += 1
                self.render()

    def render(self):
        if not TTY:
            return
        pct = (self.done / self.total) if self.total else 1.0
        pct = min(pct, 1.0)
        elapsed = time.time() - self.t0
        eta = elapsed / self.done * (self.total - self.done) if 0 < self.done < self.total else 0
        parts = [f"{pct * 100:5.1f}%", f"{self.done}/{self.total} {self.unit}", f"◷ {fmt_t(elapsed)}"]
        if self.unit != "file":
            parts.append(f"{self.files} file")
        if eta:
            parts.append(f"ETA {fmt_t(eta)}")
        if self.stage:
            parts.append(self.stage)
        cols = term_cols()
        head = vlen(self.label) + 4
        while len(parts) > 2 and head + 8 + len(" │ ".join(parts)) + 2 > cols:
            parts.pop()
        tail = " │ ".join(parts)
        bw = max(8, min(36, cols - head - len(tail) - 4))
        filled = int(bw * pct)
        bar = (grad("cyan_to_blue", "█" * filled) if filled else "") + paint("░" * (bw - filled), "dim")
        spin = SPIN[self._f % len(SPIN)]
        sys.stdout.write("\r\033[2K" + f"{paint(spin, 'acc')} {paint(self.label, 'hi')} {bar} {tail}")
        sys.stdout.flush()

    def advance(self, n=1, files=0):
        with self.lock:
            self.done += n
            self.files += files
            self.render()

    def set_stage(self, text):
        with self.lock:
            self.stage = text
            self.render()

    def log(self, text):
        with self.lock:
            if TTY:
                sys.stdout.write("\r\033[2K")
            print(text)
            self.render()


# ----------------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------------
class Report:
    def __init__(self, title):
        self.title = title
        self.t0 = time.time()
        self.rows = []
        self.extra = {}
        self.elapsed = 0.0

    def add(self, name, status, msg="", files=0, secs=0.0):
        self.rows.append({"name": name, "status": status, "msg": msg, "files": int(files), "secs": round(secs, 2)})

    def finish(self):
        self.elapsed = time.time() - self.t0
        return self

    def to_dict(self):
        return {"title": self.title, "date": datetime.now().isoformat(timespec="seconds"),
                "elapsed": round(self.elapsed, 1), "rows": self.rows, "extra": self.extra}


def print_report(rep):
    rows = rep["rows"]
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    total_files = sum(r["files"] for r in rows)
    lines = [
        f"{paint('Durata', 'dim')} {fmt_t(rep['elapsed'])}   {paint('Elementi', 'dim')} {len(rows)}   "
        f"{paint('File gestiti', 'dim')} {paint(total_files, 'hi')}",
        "  ".join(f"{ICON[k]} {counts[k]} {NAMES[k]}" for k in NAMES if k in counts),
    ]
    worked = [r for r in rows if r["status"] != "skip"]
    if worked:
        avg = sum(r["secs"] for r in worked) / len(worked)
        lines.append(f"{paint('Media', 'dim')} {avg:.1f}s per elemento")
        slow = sorted(worked, key=lambda r: -r["secs"])[:3]
        if slow[0]["secs"] >= 1:
            lines.append(f"{paint('Più lenti', 'dim')} " + ", ".join(f"{r['name'].split('/')[-1]} ({r['secs']:.1f}s)" for r in slow))
        big = sorted([r for r in rows if r["files"]], key=lambda r: -r["files"])[:3]
        if big:
            lines.append(f"{paint('Più file', 'dim')} " + ", ".join(f"{r['name'].split('/')[-1]} ({r['files']})" for r in big))
    for k, v in (rep.get("extra") or {}).items():
        lines.append(f"{paint(k, 'dim')} {v}")
    problems = [r for r in rows if r["status"] in ("warn", "err")]
    if problems:
        lines += ["", paint("Da controllare:", "warn")]
        lines += [f" {ICON[r['status']]} {r['name']}: {r['msg']}" for r in problems[:15]]
        if len(problems) > 15:
            lines.append(f" ... e altri {len(problems) - 15}")
    print()
    print_box(lines, f"Report · {rep['title']}")


def save_report(rep):
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", rep["title"].lower()).strip("-")[:40]
        path = REPORT_DIR / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug}.json"
        path.write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
        for old in sorted(REPORT_DIR.glob("*.json"))[:-50]:
            old.unlink(missing_ok=True)
        print(paint(f"Report salvato: {path}", "dim"))
    except Exception:
        pass


def finish_report(rep):
    d = rep.finish().to_dict()
    print_report(d)
    save_report(d)
    return d


def show_reports():
    files = sorted(REPORT_DIR.glob("*.json"))[-10:][::-1] if REPORT_DIR.exists() else []
    if not files:
        say("skip", "Nessun report salvato.")
        return
    data = [load_json(f, {}) for f in files]
    lines = []
    for i, d in enumerate(data, 1):
        nfiles = sum(r.get("files", 0) for r in d.get("rows", []))
        lines.append(f"{paint(str(i).rjust(2), 'info')}  {d.get('date', '')[:16]}  {d.get('title', '')[:28]:<28} "
                     f"{len(d.get('rows', []))} el. · {nfiles} file · {fmt_t(d.get('elapsed', 0))}")
    print_box(lines, "Report precedenti")
    ch = ask("Numero per i dettagli (invio = indietro): ")
    if ch.isdigit() and 1 <= int(ch) <= len(data):
        print_report(data[int(ch) - 1])


# ----------------------------------------------------------------------------
# Utilita'
# ----------------------------------------------------------------------------
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_config():
    cfg = dict(DEFAULTS)
    cfg.update(load_json(CONFIG_FILE, {}))
    return cfg


def save_config(cfg):
    save_json(CONFIG_FILE, cfg)


def ask(prompt, default=""):
    try:
        v = input(f"{paint('›', 'acc')} {prompt}").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return v or default


def yesno(prompt, default=True):
    v = ask(f"{prompt} [{'S/n' if default else 's/N'}]: ").lower()
    if not v:
        return default
    return v in ("s", "si", "sì", "y", "yes")


def ago(iso):
    if not iso:
        return "mai"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        s = int((datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return "?"
    for limit, unit, div in ((60, "s", 1), (3600, "min", 60), (86400, "h", 3600), (86400 * 60, "g", 86400)):
        if s < limit:
            return f"{max(s // div, 0)}{unit} fa"
    return f"{s // (86400 * 30)} mesi fa"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def last_line(text):
    lines = [l.strip().replace("\t", " ") for l in (text or "").splitlines() if l.strip()]
    for l in lines:
        if l.lower().startswith(("fatal:", "error:")):
            return l[:160]
    return lines[-1][:160] if lines else "errore sconosciuto"


# ----------------------------------------------------------------------------
# Token
# ----------------------------------------------------------------------------
def load_token():
    t = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if t:
        return t.strip()
    if GITHUB_TOKEN.strip():
        return GITHUB_TOKEN.strip()
    if keyring:
        try:
            t = keyring.get_password(APP, "token")
            if t:
                return t
        except Exception:
            pass
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    if shutil.which("gh"):
        try:
            p = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
        except Exception:
            pass
    return None


def save_token(token):
    if keyring:
        try:
            keyring.set_password(APP, "token", token)
            return
        except Exception:
            pass
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token)
    try:
        TOKEN_FILE.chmod(0o600)
    except Exception:
        pass


def forget_token():
    if keyring:
        try:
            keyring.delete_password(APP, "token")
        except Exception:
            pass
    TOKEN_FILE.unlink(missing_ok=True)


# ----------------------------------------------------------------------------
# Client GitHub
# ----------------------------------------------------------------------------
class GHError(Exception):
    pass


class Abort(Exception):
    pass


def slim(r):
    return {
        "full_name": r["full_name"], "name": r["name"], "owner": r["owner"]["login"],
        "private": r["private"], "fork": r["fork"], "archived": r["archived"],
        "default_branch": r.get("default_branch") or "main", "pushed_at": r.get("pushed_at"),
        "clone_url": r["clone_url"], "html_url": r.get("html_url", ""),
        "description": r.get("description") or "", "language": r.get("language") or "",
        "can_push": (r.get("permissions") or {}).get("push", True),
    }


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, method, url, body=None, headers=None, stream_to=None):
        if not url.startswith("http"):
            url = API + url
        h = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": APP,
        }
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, data=data, headers=h, method=method)
                with urllib.request.urlopen(req, timeout=60) as r:
                    if stream_to is not None:
                        shutil.copyfileobj(r, stream_to)
                        return r.status, None, r.headers
                    raw = r.read()
                    return r.status, (json.loads(raw) if raw else None), r.headers
            except urllib.error.HTTPError as e:
                if e.code == 304:
                    return 304, None, e.headers
                raw = e.read()
                if e.code in (403, 429) and (e.headers.get("X-RateLimit-Remaining") == "0" or e.headers.get("Retry-After")):
                    reset = e.headers.get("X-RateLimit-Reset")
                    wait = int(e.headers.get("Retry-After") or (int(reset) - time.time() + 1 if reset else 30))
                    if wait <= 90 and attempt < 3:
                        time.sleep(max(wait, 1))
                        continue
                    raise GHError(f"Rate limit raggiunto, riprova tra {max(wait, 1)}s")
                if e.code >= 500 and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {"message": raw.decode(errors="replace")[:200]}
                return e.code, payload, e.headers
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == 3:
                    raise GHError(f"Errore di rete: {e}")
                time.sleep(2 ** attempt)
        raise GHError("Richiesta fallita")

    def me(self):
        st, data, _ = self.request("GET", "/user")
        if st != 200:
            raise GHError(f"Token non valido ({st}): {(data or {}).get('message')}")
        return data

    def orgs(self):
        st, data, _ = self.request("GET", "/user/orgs?per_page=100")
        return [o["login"] for o in data] if st == 200 and data else []

    def list_repos(self, cfg, force=False):
        aff = "owner,collaborator,organization_member" if cfg["include_orgs"] else "owner"
        params = urllib.parse.urlencode({"per_page": 100, "affiliation": aff, "sort": "pushed", "direction": "desc"})
        url = f"{API}/user/repos?{params}"
        cache = load_json(CACHE_FILE, {})
        key = f"{cfg['user']}|{aff}"
        headers = {}
        if not force and cache.get("key") == key and cache.get("etag"):
            headers["If-None-Match"] = cache["etag"]
        st, data, h = self.request("GET", url, headers=headers)
        if st == 304:
            raw = cache["repos"]
        elif st == 200:
            etag = h.get("ETag")
            raw = list(data)
            while True:
                m = re.search(r'<([^>]+)>;\s*rel="next"', h.get("Link", ""))
                if not m:
                    break
                st, data, h = self.request("GET", m.group(1))
                if st != 200:
                    raise GHError(f"Errore nel recupero repo ({st})")
                raw += data
            save_json(CACHE_FILE, {"key": key, "etag": etag, "repos": raw})
        else:
            raise GHError(f"Errore nel recupero repo ({st}): {(data or {}).get('message')}")
        repos = [slim(r) for r in raw]
        if not cfg["include_forks"]:
            repos = [r for r in repos if not r["fork"]]
        if not cfg["include_archived"]:
            repos = [r for r in repos if not r["archived"]]
        return sorted(repos, key=lambda r: r["full_name"].lower())


# ----------------------------------------------------------------------------
# Git
# ----------------------------------------------------------------------------
def git(args, cwd=None, token=None, timeout=1800, _retry=True):
    cmd = ["git"]
    if token:  # il token viaggia solo come header temporaneo, mai in URL o .git/config
        b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd += ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {b64}"]
    cmd += args
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    out = (p.stdout + p.stderr).rstrip()
    if p.returncode != 0 and _retry and cwd and "safe.directory" in out:
        # "dubious ownership": cartella di un altro utente (Termux, /storage/emulated, USB...)
        m = re.search(r"safe\.directory\s+(\S.*)", out)
        target = (m.group(1).strip().strip("'\"") if m else str(Path(cwd).resolve()))
        subprocess.run(["git", "config", "--global", "--add", "safe.directory", target], capture_output=True)
        return git(args, cwd, token, timeout, _retry=False)
    return p.returncode, out


def git_commit(path, msg):
    """Commit con identita' di ripiego se git non e' configurato."""
    ident = []
    if git(["config", "user.email"], path)[0] != 0:
        login = ME.get("login", "user")
        ident = ["-c", f"user.name={ME.get('name') or login}",
                 "-c", f"user.email={ME.get('email') or str(ME.get('id', 0)) + '+' + login + '@users.noreply.github.com'}"]
    return git(ident + ["commit", "-m", msg], path)


def local_path(cfg, repo):
    return Path(cfg["base_dir"]).expanduser() / repo["owner"] / repo["name"]


def count_files(path):
    n = 0
    for _, dirs, files in os.walk(path):
        if ".git" in dirs:
            dirs.remove(".git")
        n += len(files)
    return n


def head_sha(path):
    rc, out = git(["rev-parse", "HEAD"], path)
    return out.strip() if rc == 0 else None


def update_local(path, token, cfg):
    """Aggiorna una repo locale senza mai perdere lavoro. Ritorna (stato, msg, file_cambiati)."""
    before = head_sha(path)

    def done(st, msg):
        after = head_sha(path)
        files = 0
        if before and after and before != after:
            rc, out = git(["diff", "--name-only", before, after], path)
            files = len([l for l in out.splitlines() if l]) if rc == 0 else 0
        return st, msg, files

    fetch = ["fetch", "origin", "--prune", "--tags", "--quiet"]
    if cfg["submodules"]:
        fetch.append("--recurse-submodules=on-demand")
    rc, out = git(fetch, path, token)
    if rc:
        return "err", last_line(out), 0

    notes, changed = [], False
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], path)[1].strip()
    dirty = bool(git(["status", "--porcelain"], path)[1].strip())
    rc, counts = git(["rev-list", "--left-right", "--count", "HEAD...@{u}"], path)

    if branch == "HEAD":
        notes.append("HEAD detached")
    elif rc:
        notes.append("branch senza upstream")
    else:
        ahead, behind = (int(x) for x in counts.split())
        if behind and not ahead:
            pull = ["pull", "--ff-only", "--autostash", "--quiet"]
            if cfg["submodules"]:
                pull.append("--recurse-submodules=on-demand")
            rc, out = git(pull, path, token)
            if rc:
                return "warn", f"pull fallito: {last_line(out)}", 0
            changed = True
            notes.append(f"+{behind} commit" + (" (modifiche locali preservate)" if dirty else ""))
        elif behind and ahead:
            if cfg["strategy"] == "rebase" and not dirty:
                rc, out = git(["pull", "--rebase", "--quiet"], path, token)
                if rc:
                    git(["rebase", "--abort"], path)
                    return "warn", f"divergente ({ahead}↑ {behind}↓), rebase annullato per conflitti", 0
                changed = True
                notes.append(f"rebase su {behind} commit")
            else:
                return "warn", f"divergente ({ahead}↑ {behind}↓): serve intervento manuale", 0
        elif ahead:
            notes.append(f"{ahead} commit da pushare")
        if dirty:
            notes.append("modifiche non committate")

    _, refs = git(["for-each-ref", "--format=%(refname:short)|%(upstream:short)|%(upstream:track)", "refs/heads"], path)
    moved = 0
    for line in refs.splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        b, up, track = parts
        if b != branch and up and re.fullmatch(r"\[behind \d+\]", track.strip()):
            rc, _ = git(["fetch", "origin", f"{up.split('/', 1)[-1]}:{b}", "--quiet"], path, token)
            moved += 0 if rc else 1
    if moved:
        changed = True
        notes.append(f"{moved} altre branch aggiornate")

    return done("upd" if changed else "ok", ", ".join(notes) or "già aggiornata")


def sync_one(gh, token, cfg, repo, force):
    path = local_path(cfg, repo)
    state = cfg["repo_state"].get(repo["full_name"], {})

    if not (path / ".git").exists():
        if path.exists() and any(path.iterdir()):
            return "err", "cartella esistente non vuota (non è una repo git)", 0
        path.parent.mkdir(parents=True, exist_ok=True)
        args = ["clone", "--quiet", repo["clone_url"], str(path)]
        if cfg["submodules"]:
            args.insert(1, "--recurse-submodules")
        rc, out = git(args, token=token)
        return ("new", "clonata", count_files(path)) if rc == 0 else ("err", last_line(out), 0)

    if not force and not repo["fork"] and state.get("pushed_at") == repo["pushed_at"]:
        return "skip", "nessuna novità", 0

    prefix = ""
    if repo["fork"] and cfg["sync_forks"] and not repo["archived"]:
        st, data, _ = gh.request("POST", f"/repos/{repo['full_name']}/merge-upstream", {"branch": repo["default_branch"]})
        if st == 200 and (data or {}).get("merge_type") not in (None, "none"):
            prefix = "fork sincronizzato con upstream; "
        elif st == 409:
            prefix = "fork in conflitto con upstream; "
    st, msg, files = update_local(path, token, cfg)
    return st, prefix + msg, files


# ----------------------------------------------------------------------------
# Batch con progress e report
# ----------------------------------------------------------------------------
def run_batch(cfg, label, items, fn, unit="repo"):
    """fn(item) -> (stato, msg[, file]). Ritorna il report (dict)."""
    rep = Report(label)
    if not items:
        say("skip", "Niente da fare.")
        return finish_report(rep)

    def job(it):
        t = time.time()
        res = fn(it)
        return res[0], res[1], (res[2] if len(res) > 2 else 0), time.time() - t

    ex = ThreadPoolExecutor(max_workers=max(1, int(cfg["workers"])))
    with Progress(label, len(items), unit) as pg:
        try:
            futs = {ex.submit(job, it): it for it in items}
            for f in as_completed(futs):
                it = futs[f]
                try:
                    st, msg, files, secs = f.result()
                except Exception as e:  # una repo che fallisce non ferma le altre
                    st, msg, files, secs = "err", str(e), 0, 0.0
                rep.add(it["full_name"], st, msg, files, secs)
                if st != "skip":
                    extra = paint(f" (+{files} file)", "dim") if files else ""
                    pg.log(f"{ICON[st]} {it['full_name']:<40} {msg}{extra}")
                pg.advance(1, files)
            ex.shutdown(wait=True)
        except KeyboardInterrupt:
            pg.log(f"{ICON['warn']} Interrotto: attendo le operazioni in corso...")
            ex.shutdown(wait=True, cancel_futures=True)
            rep.extra["Interrotto"] = "sì"
    return finish_report(rep)


def do_sync(gh, token, cfg, repos, force=False, label="Sync"):
    rep = run_batch(cfg, label, repos, lambda r: sync_one(gh, token, cfg, r, force))
    by = {r["full_name"]: r for r in repos}
    for row in rep["rows"]:
        if row["status"] in ("new", "upd", "ok", "skip") and row["name"] in by:
            cfg["repo_state"][row["name"]] = {"pushed_at": by[row["name"]]["pushed_at"], "synced_at": now_iso()}
    cfg["last_sync"] = now_iso()
    save_config(cfg)


def download_zip(gh, cfg, repo):
    out_dir = Path(cfg["base_dir"]).expanduser() / "_zip"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{repo['owner']}-{repo['name']}-{datetime.now().strftime('%Y%m%d-%H%M')}.zip"
    tmp = dest.with_suffix(".part")
    with open(tmp, "wb") as f:
        st, _, _ = gh.request("GET", f"/repos/{repo['full_name']}/zipball", stream_to=f)
    if st != 200:
        tmp.unlink(missing_ok=True)
        return "err", f"HTTP {st}", 0
    tmp.replace(dest)
    try:
        import zipfile
        with zipfile.ZipFile(dest) as z:
            n = len([i for i in z.namelist() if not i.endswith("/")])
    except Exception:
        n = 0
    return "new", f"{dest.name} ({hsize(dest.stat().st_size)})", n


def mirror_backup(token, cfg, repo):
    dest = Path(cfg["base_dir"]).expanduser() / "_mirrors" / repo["owner"] / f"{repo['name']}.git"
    if dest.exists():
        rc, out = git(["remote", "update", "--prune"], dest, token)
        st, msg = ("upd", "mirror aggiornato") if rc == 0 else ("err", last_line(out))
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        rc, out = git(["clone", "--mirror", "--quiet", repo["clone_url"], str(dest)], token=token)
        st, msg = ("new", "mirror creato") if rc == 0 else ("err", last_line(out))
    n = 0
    if rc == 0:
        r2, tree = git(["ls-tree", "-r", "--name-only", "HEAD"], dest)
        n = len([l for l in tree.splitlines() if l]) if r2 == 0 else 0
    return st, msg, n


# ----------------------------------------------------------------------------
# Selezione repo
# ----------------------------------------------------------------------------
def match(repos, query):
    q = query.lower().split()
    if not q:
        return repos
    found = [r for r in repos if all(t in f"{r['full_name']} {r['description']} {r['language']}".lower() for t in q)]
    if found:
        return found
    names = {r["full_name"].lower(): r for r in repos}
    return [names[n] for n in difflib.get_close_matches(query.lower(), list(names), n=10, cutoff=0.4)]


PRESEL = {"repos": None}  # repo gia' scelte dall'interprete di linguaggio naturale


def order_repos(repos, cfg):
    fav = set(cfg.get("favorites", []))
    rec = {n: i for i, n in enumerate(cfg.get("recent", []))}
    return sorted(repos, key=lambda r: (r["full_name"] not in fav, rec.get(r["full_name"], 999), r["full_name"].lower()))


def repo_symbol(cfg, r):
    if not (local_path(cfg, r) / ".git").exists():
        return paint("○", "dim")
    st = cfg["repo_state"].get(r["full_name"], {})
    return paint("●", "ok") if st.get("pushed_at") == r["pushed_at"] else paint("◐", "warn")


def remember(cfg, sel):
    names = [r["full_name"] for r in sel]
    cfg["recent"] = (names + [n for n in cfg.get("recent", []) if n not in names])[:12]
    save_config(cfg)


def parse_numbers(text, limit):
    idx = set()
    for part in text.replace(",", " ").split():
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            return None
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        idx.update(range(min(a, b), max(a, b) + 1))
    return sorted(idx) if idx and all(1 <= i <= limit for i in idx) else []


def pick(repos, cfg, verb, single=False, initial=""):
    """Menu numerato delle repo: sempre visibile, a pagine, con filtro e preferiti."""
    if PRESEL["repos"] is not None:
        pre, PRESEL["repos"] = PRESEL["repos"], None
        return pre[:1] if single else pre
    pool = order_repos(repos, cfg)
    q, page = initial, 0
    while True:
        found = match(pool, q) if q else pool
        if not found:
            say("warn", f"Nessuna repo per '{q}'.")
            q = ""
            continue
        if single and q and len(found) == 1:
            say("info", f"Trovata: {found[0]['full_name']}")
            remember(cfg, found)
            return found
        ps = max(8, min(25, shutil.get_terminal_size((80, 24)).lines - 12))
        pages = (len(found) + ps - 1) // ps
        page = max(0, min(page, pages - 1))
        fav = set(cfg.get("favorites", []))
        w = len(str(len(found)))
        lines = []
        for i, r in enumerate(found[page * ps:(page + 1) * ps], page * ps + 1):
            star = paint("★", "warn") if r["full_name"] in fav else " "
            tags = paint(("◆" if r["private"] else " ") + ("⑂" if r["fork"] else " "), "dim")
            lines.append(f"{paint(str(i).rjust(w), 'info')} {repo_symbol(cfg, r)} {star}{tags} {r['full_name'][:36]:<36} {paint(r['language'], 'dim')}")
        lines.append(paint("● aggiornata ◐ con novità ○ non clonata ◆ privata ⑂ fork ★ preferita", "dim"))
        lines.append(paint("numeri: 1 3 5-8" + ("" if single else " · all") + " · n/p pagina · testo = filtro · *N preferita · 0 esci", "dim"))
        title = f"{verb.capitalize()} · {len(found)} repo" + (f" · pag {page + 1}/{pages}" if pages > 1 else "") + (f" · filtro '{q}'" if q else "")
        print_box(lines, title)
        sel = ask("Scelta: ").strip()
        low = sel.lower()
        if low in ("", "0", "q"):
            return []
        if low in ("n", ">"):
            page += 1
        elif low in ("p", "<"):
            page -= 1
        elif low in ("/", "reset"):
            q, page = "", 0
        elif low.startswith("/"):
            q, page = sel[1:].strip(), 0
        elif low.startswith("*"):
            nums = parse_numbers(low[1:], len(found))
            if nums:
                favs = list(cfg.get("favorites", []))
                for i in nums:
                    n = found[i - 1]["full_name"]
                    favs.remove(n) if n in favs else favs.append(n)
                cfg["favorites"] = favs
                save_config(cfg)
                pool = order_repos(repos, cfg)
            else:
                say("warn", "Numero non valido.")
        elif low == "all":
            if single:
                say("warn", "Qui serve una sola repo.")
                continue
            remember(cfg, found)
            return found
        else:
            nums = parse_numbers(low, len(found))
            if nums is None:  # non sono numeri: lo uso come filtro
                q, page = sel, 0
            elif not nums:
                say("warn", f"Numero non valido (1-{len(found)}).")
            elif single and len(nums) != 1:
                say("warn", "Seleziona una sola repo.")
            else:
                out = [found[i - 1] for i in nums]
                remember(cfg, out)
                return out


# ----------------------------------------------------------------------------
# Intelligenza: interprete di linguaggio naturale e suggerimenti
# ----------------------------------------------------------------------------
RULES = [
    ("0", r"\b(esci|exit|quit|chiudi)\b"),
    ("4", r"\b(push|commit)\b|(pubblic\w*|carica)\s+(le\s+)?(mie\s+)?(modific\w*|local\w*)"),
    ("8", r"\b(sostituisc\w*|rimpiazz\w*|sovrascriv\w*)"),
    ("9", r"\b(elimin\w*|cancell\w*|rinomin\w*|archivi\w*|gestisc\w*|visibilit\w*|rendi)\b"),
    ("7", r"\b(crea|creare|nuova repo|nuovo repo)\b"),
    ("5", r"\bzip\b"),
    ("6", r"\b(backup|mirror)\b"),
    ("10", r"\b(stato|status)\b"),
    ("11", r"\breport\b"),
    ("12", r"\b(impostazion\w*|settings|config\w*)\b"),
    ("13", r"\b(ricarica|refresh)\b"),
    ("SYNC", r"\b(clona|clonare|scarica|scaricare|aggiorna|aggiornare|sincronizza\w*|allinea\w*|sync|download|prendi)\b"),
]
STOP = set("il lo la le gli i un una uno di del dello della dei degli delle da dal dalla in nel nella per su sul sulla a al alla ai "
           "e ed o mia mio mie miei repo repos repository repository cartella progetto progetti ora adesso subito favore puoi "
           "potresti vorrei voglio fammi fai tutte tutto tutti all completo forzato".split())
VERB_RE = re.compile(r"(sostituisc|rimpiazz|sovrascriv|elimin|cancell|rinomin|archivi|gestisc|clon|scaric|aggiorn|sincroniz|allin|"
                     r"sync|zip|backup|mirror|crea|nuov|pubblic|push|commit|carica|stato|status|report|prendi|download)\w*")


def interpret(text, repos, cfg):
    """Capisce frasi come 'scarica emporio', 'sostituisci emporio46 con sitotest', 'elimina test'.
    Ritorna (codice_menu, repo_scelte|None). (None, None) = non capito."""
    t = text.lower().strip()
    code = next((c for c, rx in RULES if re.search(rx, t)), None)
    if not code:
        return None, None
    label = "sincronizza" if code == "SYNC" else dict((k, v) for _, items in SECTIONS for k, v in items).get(code, code)
    say("info", f"Ho capito: {label.split('(')[0].strip().lower()}")
    if code in ("0", "4", "7", "10", "11", "12", "13"):
        return code, None
    words = re.findall(r"[\w.\-]+", t)
    for sep in ("con", "da"):  # 'sostituisci X con cartella' -> tengo solo la parte prima
        if sep in words and code in ("8",):
            words = words[:words.index(sep)]
    query = " ".join(w for w in words if w not in STOP and not VERB_RE.fullmatch(w))
    whole = bool(re.search(r"\btutt[eio]\b|\ball\b", t))
    if code == "SYNC":
        if not query:
            return ("2" if (whole and re.search(r"complet|forz", t)) or whole else "1"), None
        code = "3"
    if not query:
        return code, (repos if whole and code in ("5", "6") else None)
    found = match(order_repos(repos, cfg), query)
    if not found:
        say("warn", f"Nessuna repo corrisponde a '{query}'.")
        return code, []
    single = code in ("8", "9")
    if single and len(found) > 1 or (not single and len(found) > 1 and not whole):
        sel = pick(repos, cfg, "scegli", single=single, initial=query)
        return code, sel
    remember(cfg, found[:1] if single else found)
    return code, found[:1] if single else found


def smart_hints(cfg, repos):
    missing, changed = pending(cfg, repos)
    parts = []
    if missing:
        parts.append(paint(f"{len(missing)} da clonare", "info"))
    if changed:
        parts.append(paint(f"{len(changed)} con novità", "ok"))
    if cfg.get("last_sync"):
        try:
            d = (datetime.now(timezone.utc) - datetime.fromisoformat(cfg["last_sync"])).days
            if d >= 3:
                parts.append(paint(f"ultimo sync {d} giorni fa", "warn"))
        except Exception:
            pass
    return (" · ".join(parts) + paint("  → premi invio per sincronizzare", "dim")) if parts else paint("Tutto allineato ✓", "ok")


# ----------------------------------------------------------------------------
# Stato e pubblicazione modifiche locali
# ----------------------------------------------------------------------------
def find_local_repos(cfg):
    base = Path(cfg["base_dir"]).expanduser()
    out = []
    if base.exists():
        for owner in sorted(base.iterdir()):
            if owner.is_dir() and not owner.name.startswith("_"):
                out += [r for r in sorted(owner.iterdir()) if (r / ".git").exists()]
    return out


def probe_local(p):
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], p)[1].strip()
    dirty = [l for l in git(["status", "--porcelain"], p)[1].splitlines() if l.strip()]
    rc, cnt = git(["rev-list", "--left-right", "--count", "HEAD...@{u}"], p)
    a, b = (int(x) for x in cnt.split()) if rc == 0 else (0, 0)
    return {"path": p, "branch": branch, "dirty": dirty, "ahead": a, "behind": b, "upstream": rc == 0}


def probe_all(cfg):
    paths = find_local_repos(cfg)
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(probe_local, paths))


def show_status(cfg):
    rows = probe_all(cfg)
    if not rows:
        say("warn", f"Nessuna repo locale in {cfg['base_dir']}")
        return
    lines, clean = [], 0
    for x in rows:
        flags = []
        if x["dirty"]:
            flags.append(paint(f"{len(x['dirty'])} modificati", "warn"))
        if x["ahead"]:
            flags.append(paint(f"{x['ahead']}↑ da pushare", "info"))
        if x["behind"]:
            flags.append(paint(f"{x['behind']}↓ da scaricare", "acc"))
        if flags:
            lines.append(f"{x['path'].parent.name}/{x['path'].name} [{x['branch']}] " + ", ".join(flags))
        else:
            clean += 1
    lines.append(paint(f"{len(rows)} repo locali · {clean} pulite e allineate", "ok"))
    print_box(lines, "Stato locale")


def auto_message(porcelain):
    c = {"modificati": 0, "aggiunti": 0, "rimossi": 0, "rinominati": 0}
    for l in porcelain:
        code = l[:2].strip()
        if code in ("??", "A"):
            c["aggiunti"] += 1
        elif code == "D":
            c["rimossi"] += 1
        elif code.startswith("R"):
            c["rinominati"] += 1
        else:
            c["modificati"] += 1
    detail = ", ".join(f"{v} {k}" for k, v in c.items() if v)
    return f"Auto-update: {len(porcelain)} file ({detail})"


def publish_local(cfg, token):
    todo = [x for x in probe_all(cfg) if x["dirty"] or x["ahead"]]
    if not todo:
        say("ok", "Tutto è già pubblicato: nessuna modifica locale.")
        return
    lines = [f"{x['path'].parent.name}/{x['path'].name} " + paint(f"{len(x['dirty'])} file, {x['ahead']} commit", "dim") for x in todo]
    print_box(lines, f"{len(todo)} repo con modifiche")
    if not yesno("Creo il commit automatico e faccio push?", True):
        return
    items = [{"full_name": f"{x['path'].parent.name}/{x['path'].name}", "probe": x} for x in todo]

    def fn(it):
        pr, path = it["probe"], it["probe"]["path"]
        if pr["behind"]:
            return "warn", f"il remoto è avanti di {pr['behind']} commit: fai prima il sync", 0
        if pr["dirty"]:
            git(["add", "-A"], path)
            rc, out = git_commit(path, auto_message(pr["dirty"]))
            if rc:
                return "err", f"commit: {last_line(out)}", 0
        push = ["push"] if pr["upstream"] else ["push", "-u", "origin", "HEAD"]
        rc, out = git(push, path, token)
        return ("upd", "pubblicata", len(pr["dirty"])) if rc == 0 else ("err", f"push: {last_line(out)}", 0)

    run_batch(cfg, "Pubblicazione", items, fn)


# ----------------------------------------------------------------------------
# Scansione cartelle (per crea / sostituisci)
# ----------------------------------------------------------------------------
IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
IGNORE_FILES = {".DS_Store", "Thumbs.db"}
SENSITIVE = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa", "id_ed25519", "id_ecdsa",
             "credentials.json", "secrets.json", "*.kdbx")
MAX_FILE = 100 * 1024 * 1024  # limite rigido di GitHub


def is_sensitive(rel):
    name = os.path.basename(rel).lower()
    if name.endswith((".example", ".sample", ".template", ".pub")):
        return False
    return any(fnmatch.fnmatch(name, p) for p in SENSITIVE)


SECRET_RE = re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|AKIA[0-9A-Z]{16}"
                       rb"|-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")


def has_secret(p, size):
    """True se il file contiene un token/chiave privata (file di testo fino a 512 KB)."""
    if size > 512 * 1024:
        return False
    try:
        return bool(SECRET_RE.search(p.read_bytes()))
    except OSError:
        return False


def collect_files(src):
    files = []
    if (src / ".git").exists():  # rispetta il .gitignore
        rc, out = git(["-c", "core.quotepath=off", "ls-files", "--cached", "--others", "--exclude-standard"], src)
        if rc == 0:
            files = [l for l in out.splitlines() if l]
    if not files:
        for root, dirs, fs in os.walk(src):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for f in fs:
                if f not in IGNORE_FILES:
                    files.append(os.path.relpath(os.path.join(root, f), src).replace(os.sep, "/"))
    good, large, sens, size = [], [], [], 0
    for rel in dict.fromkeys(files):
        p = src / rel
        if not (p.is_file() or p.is_symlink()):
            continue
        try:
            sz = p.lstat().st_size
        except OSError:
            continue
        if sz > MAX_FILE:
            large.append(rel)
            continue
        good.append(rel)
        size += sz
        if is_sensitive(rel) or has_secret(p, sz):
            sens.append(rel)
    return {"files": good, "size": size, "large": large, "sensitive": sens}


def copy_files(src, dst, files, pg):
    copied, failed = 0, []
    for rel in files:
        s, d = src / rel, dst / rel
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            if s.is_symlink():
                os.symlink(os.readlink(s), d)
            else:
                shutil.copy2(s, d)
            copied += 1
        except Exception as e:
            failed.append((rel, str(e)))
        pg.advance(1)
    return copied, failed


def wipe_dir(path):
    n = count_files(path)
    for item in path.iterdir():
        if item.name == ".git":
            continue
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)
    return n


def scan_summary(scan):
    lines = [f"File: {paint(len(scan['files']), 'hi')}   Dimensione: {paint(hsize(scan['size']), 'hi')}"]
    if scan["large"]:
        lines.append(paint(f"{len(scan['large'])} file > 100 MB saltati (limite GitHub)", "warn"))
    if scan["sensitive"]:
        lines.append(paint(f"{len(scan['sensitive'])} file sensibili:", "warn"))
        lines += [f"  {ICON['warn']} {s}" for s in scan["sensitive"][:6]]
        if len(scan["sensitive"]) > 6:
            lines.append(f"  ... e altri {len(scan['sensitive']) - 6}")
    return lines


def step(rep, pg, name, st, msg="", files=0, t0=None):
    rep.add(name, st, msg, files, (time.time() - t0) if t0 else 0)
    pg.log(f"{ICON[st]} {name:<22} {msg}")


# ----------------------------------------------------------------------------
# Crea repo
# ----------------------------------------------------------------------------
def valid_name(n):
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,100}", n)) and n not in (".", "..")


def gitignore_for(folder):
    parts = ["# OS / editor\n.DS_Store\nThumbs.db\n.idea/\n.vscode/\n*.log\n.env\n"]
    if any(folder.glob("*.py")) or (folder / "requirements.txt").exists() or (folder / "pyproject.toml").exists():
        parts.append("# Python\n__pycache__/\n*.py[cod]\n.venv/\nvenv/\n*.egg-info/\ndist/\nbuild/\n.pytest_cache/\n")
    if (folder / "package.json").exists():
        parts.append("# Node\nnode_modules/\ndist/\n.next/\nnpm-debug.log*\n")
    return "\n".join(parts)


def choose_owner(gh):
    orgs = gh.orgs()
    if not orgs:
        return None
    opts = [paint("0", "info") + f"  {ME.get('login')} (personale)"] + [paint(str(i), "info") + f"  {o}" for i, o in enumerate(orgs, 1)]
    print_box(opts, "Proprietario")
    ch = ask("Scelta [0]: ", "0")
    return orgs[int(ch) - 1] if ch.isdigit() and 1 <= int(ch) <= len(orgs) else None


def new_repo(gh, token, cfg):
    print_box([f"{paint('1', 'info')}  Repo vuota su GitHub (README/.gitignore/licenza opzionali)",
               f"{paint('2', 'info')}  Da una cartella locale (init + commit + push)"], "Crea repo")
    mode = ask("Modalità [2]: ", "2")
    folder, scan, files = None, None, []
    if mode == "2":
        folder = Path(ask("Cartella del progetto [.]: ", ".")).expanduser().resolve()
        if not folder.is_dir():
            say("err", "Cartella non valida.")
            return None
        scan = collect_files(folder)
        print_box(scan_summary(scan), "Analisi cartella")
        files = list(scan["files"])
    name = ask(f"Nome repo [{folder.name if folder else ''}]: ", folder.name if folder else "")
    if not valid_name(name):
        say("err", "Nome non valido (solo lettere, numeri, . _ -).")
        return None
    desc = ask("Descrizione (opzionale): ")
    private = yesno("Privata?", True)
    topics = [t.strip().lower().replace(" ", "-") for t in ask("Topics separati da virgola (opzionale): ").split(",") if t.strip()]
    org = choose_owner(gh)

    body = {"name": name, "description": desc, "private": private}
    skip_sens = False
    if mode != "2":
        body["auto_init"] = yesno("Creare un README iniziale?", True)
        gi = ask("Template .gitignore (es. Python, Node; invio = nessuno): ")
        lic = ask("Licenza (mit, apache-2.0, gpl-3.0; invio = nessuna): ")
        if gi:
            body["gitignore_template"] = gi
        if lic:
            body["license_template"] = lic
    else:
        if scan["sensitive"]:
            skip_sens = yesno("Escludere i file sensibili dal primo commit?", True)

    rep = Report(f"Crea repo {name}")
    try:
      with Progress("Creazione", 5 if mode == "2" else 2, "fasi") as pg:
        t0 = time.time()
        pg.set_stage("API GitHub")
        st, data, _ = gh.request("POST", f"/orgs/{org}/repos" if org else "/user/repos", body)
        if st != 201:
            msg = f"{(data or {}).get('message')} {' '.join(str(e.get('message', e)) for e in (data or {}).get('errors', []))}".strip()
            step(rep, pg, "Creazione su GitHub", "err", msg, 0, t0)
            finish_report(rep)
            return None
        step(rep, pg, "Creazione su GitHub", "new", data["html_url"], 0, t0)
        pg.advance(1)
        if topics:
            gh.request("PUT", f"/repos/{data['full_name']}/topics", {"names": topics})
        repo = slim(data)
        if mode != "2":
            t0 = time.time()
            pg.set_stage("clone")
            if yesno("Clonarla subito in locale?", True):
                st2, msg, n = sync_one(gh, token, cfg, repo, True)
                step(rep, pg, "Clone locale", st2, msg, n, t0)
            pg.advance(1)
        else:
            url = data["clone_url"]
            t0 = time.time()
            pg.set_stage("preparazione")
            if not (folder / ".gitignore").exists() and yesno("Manca il .gitignore: lo genero?", True):
                (folder / ".gitignore").write_text(gitignore_for(folder), encoding="utf-8")
            if not (folder / "README.md").exists() and yesno("Manca il README: lo genero?", True):
                (folder / "README.md").write_text(f"# {name}\n\n{desc}\n", encoding="utf-8")
            fresh = not (folder / ".git").exists()
            if fresh:
                git(["init"], folder)
                git(["symbolic-ref", "HEAD", "refs/heads/main"], folder)
            if skip_sens:
                ex = folder / ".git" / "info" / "exclude"
                ex.parent.mkdir(parents=True, exist_ok=True)
                with open(ex, "a", encoding="utf-8") as fh:
                    fh.write("\n# gh-autopilot: file sensibili\n" + "\n".join("/" + s for s in scan["sensitive"]) + "\n")
            if git(["remote", "get-url", "origin"], folder)[0] == 0:
                git(["remote", "set-url", "origin", url], folder)
            else:
                git(["remote", "add", "origin", url], folder)
            step(rep, pg, "Preparazione", "ok", "init e remote pronti", 0, t0)
            pg.advance(1)

            t0 = time.time()
            pg.set_stage("add")
            rc, out = git(["add", "-A"], folder)
            n = len([l for l in git(["diff", "--cached", "--name-only"], folder)[1].splitlines() if l])
            pg.files = n
            step(rep, pg, "Staging file", "ok" if rc == 0 else "err", f"{n} file" if rc == 0 else last_line(out), n, t0)
            if rc != 0:
                raise Abort(False)
            pg.advance(1)

            t0 = time.time()
            pg.set_stage("commit")
            if git(["rev-parse", "HEAD"], folder)[0] != 0 or n:
                rc, out = git_commit(folder, "Initial commit" if fresh else "Update from gh-autopilot")
                step(rep, pg, "Commit", "ok" if rc == 0 else "warn", "creato" if rc == 0 else last_line(out), 0, t0)
            else:
                step(rep, pg, "Commit", "skip", "niente da committare", 0, t0)
            pg.advance(1)

            t0 = time.time()
            pg.set_stage("push")
            rc, out = git(["push", "-u", "origin", "HEAD"], folder, token)
            step(rep, pg, "Push", "upd" if rc == 0 else "err", "pubblicato" if rc == 0 else last_line(out), 0, t0)
            pg.advance(1)
    except Abort:
        pass
    rep.extra["URL"] = data["html_url"]
    finish_report(rep)
    return repo


# ----------------------------------------------------------------------------
# Sostituisci repo con cartella
# ----------------------------------------------------------------------------
def replace_repo(gh, token, cfg, repos):
    sel = pick(repos, cfg, "sostituire", single=True)
    if not sel:
        return False
    repo = sel[0]
    if not repo["can_push"]:
        say("err", "Non hai i permessi di scrittura su questa repo.")
        return False
    src = Path(ask("Cartella sorgente: ")).expanduser().resolve()
    if not src.is_dir():
        say("err", "Cartella non valida.")
        return False
    scan = collect_files(src)
    print_box(scan_summary(scan), "Analisi cartella")
    exclude = set(scan["large"])
    if scan["sensitive"] and yesno("Escludere i file sensibili?", True):
        exclude |= set(scan["sensitive"])
    files = [f for f in scan["files"] if f not in exclude]
    if not files:
        say("err", "Nessun file da caricare.")
        return False

    print_box([
        f"{paint('1', 'info')}  Mantieni cronologia  {paint('nuovo commit che sostituisce tutto il contenuto', 'dim')}",
        f"{paint('2', 'info')}  Reset totale         {paint('storia azzerata + force push (DISTRUTTIVO)', 'err')}",
        f"{paint('3', 'info')}  Nuovo branch         {paint('push su un branch separato, nulla viene toccato', 'dim')}",
    ], f"Sostituisci {repo['full_name']}")
    mode = ask("Modalità [1]: ", "1")
    if mode not in ("1", "2", "3"):
        return False
    branch = repo["default_branch"]
    if mode == "3":
        branch = ask("Nome del nuovo branch: ", "replace-" + datetime.now().strftime("%Y%m%d-%H%M"))
    msg = ask("Messaggio di commit: ", f"Replace content with {src.name}")

    if mode == "2":
        say("warn", paint("Cronologia, tag e commit remoti di questo branch andranno persi.", "warn"))
        if ask(f"Scrivi '{repo['full_name']}' per confermare: ") != repo["full_name"]:
            say("skip", "Annullato.")
            return False
    elif not yesno(f"Sostituire il contenuto di {repo['full_name']} ({branch}) con {len(files)} file?", True):
        return False
    backup = mode in ("1", "2") and (cfg["auto_backup"] or yesno("Fare prima un backup mirror?", True))

    rep = Report(f"Sostituisci {repo['full_name']}")
    tmp = Path(tempfile.mkdtemp(prefix="ghap-"))
    work = tmp / "repo"
    result = True
    try:
        with Progress("Sostituzione", len(files), "file") as pg:
            if backup:
                t0 = time.time()
                pg.set_stage("backup")
                st, m, n = mirror_backup(token, cfg, repo)
                step(rep, pg, "Backup mirror", st, m, n, t0)
                if st == "err":
                    raise Abort(False)

            t0 = time.time()
            pg.set_stage("preparazione")
            if mode in ("1", "3"):
                rc, out = git(["clone", "--quiet", repo["clone_url"], str(work)], token=token)
                if rc:
                    step(rep, pg, "Clone", "err", last_line(out), 0, t0)
                    raise Abort(False)
                if git(["rev-parse", "HEAD"], work)[0] != 0:  # repo vuota
                    git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], work)
                elif mode == "3":
                    git(["checkout", "-B", branch], work)
                step(rep, pg, "Clone temporaneo", "ok", "pronto", 0, t0)
                t0 = time.time()
                pg.set_stage("pulizia")
                removed = wipe_dir(work)
                step(rep, pg, "Pulizia contenuto", "ok", f"{removed} file rimossi", removed, t0)
            else:
                work.mkdir(parents=True)
                git(["init"], work)
                git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], work)
                git(["remote", "add", "origin", repo["clone_url"]], work)
                step(rep, pg, "Repo temporanea", "ok", "inizializzata da zero", 0, t0)

            t0 = time.time()
            pg.set_stage("copia")
            copied, failed = copy_files(src, work, files, pg)
            pg.files = copied
            step(rep, pg, "Copia file", "ok" if not failed else "warn",
                 f"{copied} copiati" + (f", {len(failed)} falliti" if failed else ""), copied, t0)
            for rel, e in failed[:5]:
                rep.add(rel, "err", e)

            t0 = time.time()
            pg.set_stage("commit")
            git(["add", "-A"], work)
            if not git(["status", "--porcelain"], work)[1].strip() and mode != "2":
                step(rep, pg, "Commit", "skip", "il contenuto è già identico", 0, t0)
                raise Abort(True)
            rc, out = git_commit(work, msg)
            if rc:
                step(rep, pg, "Commit", "err", last_line(out), 0, t0)
                raise Abort(False)
            step(rep, pg, "Commit", "ok", msg[:50], 0, t0)

            t0 = time.time()
            pg.set_stage("push")
            push = ["push", "origin", f"HEAD:refs/heads/{branch}"]
            if mode == "2":
                push.insert(1, "--force")
            rc, out = git(push, work, token)
            if rc:
                step(rep, pg, "Push", "err", last_line(out), 0, t0)
                raise Abort(False)
            step(rep, pg, "Push", "upd", f"branch {branch}" + (" (force)" if mode == "2" else ""), 0, t0)
    except Abort as a:
        result = bool(a.args[0])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    rep.extra.update({"Repo": repo["full_name"], "Branch": branch, "File esclusi": len(exclude)})
    finish_report(rep)
    if not result:
        return False
    cfg["repo_state"].pop(repo["full_name"], None)
    save_config(cfg)
    if (local_path(cfg, repo) / ".git").exists():
        say("warn", "La copia locale è ora obsoleta: usa 'Sync' per riallinearla" +
            (" (con reset totale conviene rieliminarla e riclonarla)." if mode == "2" else "."))
    return True


# ----------------------------------------------------------------------------
# Gestione repo
# ----------------------------------------------------------------------------
def patch_repo(gh, full, body):
    st, data, _ = gh.request("PATCH", f"/repos/{full}", body)
    if st == 200:
        return data
    errs = " ".join(str(e.get("message", e)) for e in (data or {}).get("errors", []))
    say("err", f"{(data or {}).get('message')} {errs}".strip())
    return None


def manage_repo(gh, token, cfg, repos):
    sel = pick(repos, cfg, "gestire", single=True)
    if not sel:
        return False
    repo, changed = sel[0], False
    while True:
        full = repo["full_name"]
        print_box([f"{paint(k, 'info')}  {t}" for k, t in (
            ("1", "Informazioni"), ("2", "Rinomina"), ("3", "Cambia visibilità (pubblica/privata)"),
            ("4", "Modifica descrizione e topics"), ("5", "Archivia / ripristina"),
            ("6", "Cambia branch di default"), ("7", "Elimina repo"), ("0", "Indietro"))], f"Gestisci · {full}")
        ch = ask("Azione [0]: ", "0")
        if ch == "0":
            return changed
        if ch == "1":
            st, d, _ = gh.request("GET", f"/repos/{full}")
            if st != 200:
                say("err", (d or {}).get("message", "errore"))
                continue
            lic = (d.get("license") or {}).get("spdx_id") or "-"
            print_box([
                f"{paint('Descrizione', 'dim')} {d.get('description') or '-'}",
                f"{paint('Visibilità ', 'dim')} {'privata' if d['private'] else 'pubblica'}   {paint('Branch', 'dim')} {d['default_branch']}",
                f"{paint('Linguaggio ', 'dim')} {d.get('language') or '-'}   {paint('Licenza', 'dim')} {lic}",
                f"{paint('Dimensione ', 'dim')} {hsize(d['size'] * 1024)}   ★ {d['stargazers_count']}   ⑂ {d['forks_count']}   issue {d['open_issues_count']}",
                f"{paint('Ultimo push', 'dim')} {ago(d.get('pushed_at'))}   {paint('Topics', 'dim')} {', '.join(d.get('topics') or []) or '-'}",
                f"{paint('URL', 'dim')} {d['html_url']}",
            ], full)
        elif ch == "2":
            new = ask(f"Nuovo nome [{repo['name']}]: ", repo["name"])
            if new == repo["name"] or not valid_name(new):
                continue
            d = patch_repo(gh, full, {"name": new})
            if d:
                old_path = local_path(cfg, repo)
                cfg["repo_state"].pop(full, None)
                repo.update(name=d["name"], full_name=d["full_name"], clone_url=d["clone_url"], html_url=d["html_url"])
                new_path = local_path(cfg, repo)
                if (old_path / ".git").exists() and not new_path.exists():
                    old_path.rename(new_path)
                    git(["remote", "set-url", "origin", d["clone_url"]], new_path)
                    say("ok", "Cartella locale rinominata e remote aggiornato.")
                say("ok", f"Rinominata in {d['full_name']}")
                changed = True
        elif ch == "3":
            to_private = not repo["private"]
            if yesno(f"Rendere {full} {'PRIVATA' if to_private else 'PUBBLICA'}?", False):
                d = patch_repo(gh, full, {"private": to_private})
                if d:
                    repo["private"] = d["private"]
                    say("ok", "Visibilità aggiornata.")
                    changed = True
        elif ch == "4":
            desc = ask("Nuova descrizione (invio = invariata): ")
            if desc and patch_repo(gh, full, {"description": desc}):
                say("ok", "Descrizione aggiornata.")
            tp = ask("Topics separati da virgola (invio = invariati): ")
            if tp:
                names = [t.strip().lower().replace(" ", "-") for t in tp.split(",") if t.strip()]
                st, d, _ = gh.request("PUT", f"/repos/{full}/topics", {"names": names})
                say("ok" if st == 200 else "err", "Topics aggiornati." if st == 200 else (d or {}).get("message", "errore"))
            changed = True
        elif ch == "5":
            arch = not repo["archived"]
            if yesno(f"{'Archiviare' if arch else 'Ripristinare'} {full}?", True):
                d = patch_repo(gh, full, {"archived": arch})
                if d:
                    repo["archived"] = d["archived"]
                    say("ok", "Archiviata (sola lettura)." if arch else "Ripristinata.")
                    changed = True
        elif ch == "6":
            b = ask(f"Nuovo branch di default [{repo['default_branch']}]: ", repo["default_branch"])
            if b != repo["default_branch"] and patch_repo(gh, full, {"default_branch": b}):
                repo["default_branch"] = b
                say("ok", "Branch di default aggiornato.")
                changed = True
        elif ch == "7":
            say("warn", paint("L'eliminazione è definitiva (serve lo scope 'delete_repo' sul token).", "warn"))
            if ask(f"Scrivi '{full}' per eliminare: ") != full:
                say("skip", "Annullato.")
                continue
            if cfg["auto_backup"] or yesno("Backup mirror prima di eliminare?", True):
                st, m, n = mirror_backup(token, cfg, repo)
                say(st, f"Backup: {m} ({n} file)")
                if st == "err" and not yesno("Backup fallito: elimino comunque?", False):
                    continue
            st, d, _ = gh.request("DELETE", f"/repos/{full}")
            if st == 204:
                say("ok", "Repo eliminata.")
                cfg["repo_state"].pop(full, None)
                save_config(cfg)
                return True
            say("err", f"{(d or {}).get('message', st)} - il token potrebbe non avere lo scope delete_repo")


# ----------------------------------------------------------------------------
# Impostazioni, login, menu
# ----------------------------------------------------------------------------
def settings(cfg):
    toggles = [
        ("include_forks", "Includi fork"), ("include_archived", "Includi repo archiviate"),
        ("include_orgs", "Includi repo di organizzazioni"), ("sync_forks", "Sincronizza fork con upstream"),
        ("submodules", "Gestisci submodule"), ("auto_backup", "Backup automatico prima di sostituzioni/eliminazioni"),
    ]
    while True:
        n = len(toggles) + 2
        lines = [f"{paint('1', 'info')}  Cartella base: {cfg['base_dir']}"]
        lines += [f"{paint(str(i), 'info')}  {label}: {paint('sì', 'ok') if cfg[k] else paint('no', 'err')}"
                  for i, (k, label) in enumerate(toggles, 2)]
        lines += [f"{paint(str(n), 'info')}  Strategia branch divergenti: {cfg['strategy']} (safe/rebase)",
                  f"{paint(str(n + 1), 'info')}  Operazioni parallele: {cfg['workers']}",
                  f"{paint(str(n + 2), 'info')}  Dimentica token e logout", f"{paint('0', 'info')}  Indietro"]
        print_box(lines, "Impostazioni")
        ch = ask("Scelta [0]: ", "0")
        if ch == "0":
            save_config(cfg)
            return
        if not ch.isdigit():
            continue
        i = int(ch)
        if i == 1:
            cfg["base_dir"] = str(Path(ask("Nuova cartella: ", cfg["base_dir"])).expanduser())
        elif 2 <= i < n:
            k = toggles[i - 2][0]
            cfg[k] = not cfg[k]
        elif i == n:
            cfg["strategy"] = "rebase" if cfg["strategy"] == "safe" else "safe"
        elif i == n + 1:
            v = ask("Numero di worker (1-16): ", str(cfg["workers"]))
            cfg["workers"] = min(max(int(v), 1), 16) if v.isdigit() else cfg["workers"]
        elif i == n + 2:
            forget_token()
            cfg["user"] = None
            save_config(cfg)
            say("ok", "Token dimenticato. Al prossimo avvio ti verrà richiesto.")
            sys.exit(0)
        save_config(cfg)


def login(cfg):
    token = load_token()
    if token:
        try:
            me = GitHub(token).me()
            cfg["user"] = me["login"]
            return token, me
        except GHError as e:
            say("warn", f"Token salvato non valido: {e}")
    print_box(["Primo accesso: crea un token su https://github.com/settings/tokens",
               "classic: scope 'repo' (+ 'delete_repo' se vuoi eliminare repo)",
               "fine-grained: Contents + Administration in lettura/scrittura"], "Benvenuto")
    while True:
        token = getpass.getpass("› Incolla il token (non verrà mostrato): ").strip()
        if not token:
            sys.exit("Annullato.")
        try:
            me = GitHub(token).me()
            break
        except GHError as e:
            say("err", str(e))
    save_token(token)
    cfg["user"] = me["login"]
    save_config(cfg)
    say("ok", f"Connesso come {me['login']}. Token memorizzato in modo sicuro.")
    return token, me


def pending(cfg, repos):
    missing, changed = [], []
    for r in repos:
        if not (local_path(cfg, r) / ".git").exists():
            missing.append(r)
        elif cfg["repo_state"].get(r["full_name"], {}).get("pushed_at") != r["pushed_at"]:
            changed.append(r)
    return missing, changed


SECTIONS = [
    ("Sincronizzazione", [("1", "Sync intelligente  (invio)"), ("2", "Sync completo forzato"),
                          ("3", "Scegli repo da clonare/aggiornare"), ("4", "Pubblica modifiche locali (commit+push)")]),
    ("Download e backup", [("5", "Scarica ZIP"), ("6", "Backup mirror completo")]),
    ("Crea e sostituisci", [("7", "Crea repo (vuota o da cartella)"), ("8", "Sostituisci repo con una cartella"),
                            ("9", "Gestisci repo (rinomina, visibilità, elimina...)")]),
    ("Strumenti", [("10", "Stato repo locali"), ("11", "Report precedenti"), ("12", "Impostazioni"),
                   ("13", "Ricarica elenco da GitHub"), ("0", "Esci")]),
]


def show_menu(cfg=None, repos=None):
    if cfg is not None:
        print(f"\n{paint('»', 'acc')} {smart_hints(cfg, repos)}")
        top = sorted(cfg.get("usage", {}).items(), key=lambda kv: -kv[1])[:3]
        names = dict((k, v) for _, items in SECTIONS for k, v in items)
        if top:
            print(paint("  più usati: " + " · ".join(f"{k} {names.get(k, '').split('(')[0].strip()}" for k, _ in top), "dim"))
        print(paint("  scrivi anche a parole: 'scarica emporio', 'sostituisci emporio46', 'elimina test', 'zip tutte'", "dim"))
    lines = []
    for title, items in SECTIONS:
        lines.append(grad("purple_to_blue", title.upper()))
        lines += [f"  {paint(k.rjust(2), 'info')}  {t}" for k, t in items]
        lines.append("")
    print_box(lines[:-1], "Menu")


def main():
    global TTY
    ap = argparse.ArgumentParser(description="GH Autopilot")
    ap.add_argument("--sync", action="store_true", help="sync non interattivo")
    ap.add_argument("--full", action="store_true", help="con --sync: controlla tutte le repo")
    args = ap.parse_args()

    if not shutil.which("git"):
        sys.exit("git non trovato: installalo e riprova.")
    if not args.sync and TTY:
        ensure_pystyle()
    init_ui()

    cfg = load_config()
    Path(cfg["base_dir"]).expanduser().mkdir(parents=True, exist_ok=True)

    if args.sync:
        token = load_token()
        if not token:
            sys.exit("Nessun token salvato: avvia prima in modalità interattiva.")
        gh = GitHub(token)
        ME.update(gh.me())
        cfg["user"] = ME["login"]
        do_sync(gh, token, cfg, gh.list_repos(cfg), force=args.full)
        return

    banner()
    token, me = login(cfg)
    ME.update(me)
    gh = GitHub(token)
    try:
        repos = gh.list_repos(cfg)
    except GHError as e:
        sys.exit(str(e))
    missing, changed = pending(cfg, repos)
    welcome(me, cfg, repos, missing, changed)

    valid = {k for _, items in SECTIONS for k, _ in items}
    while True:
        show_menu(cfg, repos)
        raw = ask("Scelta [1]: ", "1")
        try:
            if raw in valid:
                ch, PRESEL["repos"] = raw, None
            else:
                ch, PRESEL["repos"] = interpret(raw, repos, cfg)
                if ch is None:
                    found = match(order_repos(repos, cfg), raw)
                    if not found:
                        say("warn", "Non ho capito. Usa un numero del menu o frasi come 'scarica <repo>'.")
                        continue
                    say("info", f"{len(found)} repo trovate per '{raw}'.")
                    act = ask("Cosa faccio? s=sync z=zip b=backup g=gestisci (invio = niente): ").lower()
                    ch = {"s": "3", "z": "5", "b": "6", "g": "9"}.get(act)
                    if ch is None:
                        continue
                    PRESEL["repos"] = pick(repos, cfg, "gestire", single=True, initial=raw) if ch == "9" else found
            if PRESEL["repos"] == []:  # ricerca senza risultati o scelta annullata
                PRESEL["repos"] = None
                continue
            cfg["usage"] = {**cfg.get("usage", {}), ch: cfg.get("usage", {}).get(ch, 0) + 1}
            save_config(cfg)
            if ch == "1":
                missing, changed = pending(cfg, repos)
                do_sync(gh, token, cfg, missing + changed, force=False)
            elif ch == "2":
                do_sync(gh, token, cfg, repos, force=True, label="Sync completo")
            elif ch == "3":
                sel = pick(repos, cfg, "sincronizzare")
                if sel:
                    do_sync(gh, token, cfg, sel, force=True, label="Sync selezione")
            elif ch == "4":
                publish_local(cfg, token)
            elif ch == "5":
                sel = pick(repos, cfg, "scaricare in ZIP")
                if sel:
                    run_batch(cfg, "Download ZIP", sel, lambda r: download_zip(gh, cfg, r))
            elif ch == "6":
                sel = pick(repos, cfg, "il backup mirror")
                if sel:
                    run_batch(cfg, "Backup mirror", sel, lambda r: mirror_backup(token, cfg, r))
            elif ch == "7":
                if new_repo(gh, token, cfg):
                    repos = gh.list_repos(cfg, force=True)
            elif ch == "8":
                if replace_repo(gh, token, cfg, repos):
                    repos = gh.list_repos(cfg, force=True)
            elif ch == "9":
                if manage_repo(gh, token, cfg, repos):
                    repos = gh.list_repos(cfg, force=True)
            elif ch == "10":
                show_status(cfg)
            elif ch == "11":
                show_reports()
            elif ch == "12":
                settings(cfg)
                repos = gh.list_repos(cfg, force=True)
            elif ch == "13":
                repos = gh.list_repos(cfg, force=True)
                say("ok", f"{len(repos)} repo caricate.")
            elif ch == "0":
                goodbye()
                return
        except GHError as e:
            say("err", str(e))
        except KeyboardInterrupt:
            print()
            say("warn", "Operazione annullata.")
        finally:
            PRESEL["repos"] = None


# ============================================================================
# GH Autopilot - PATCH UI (pystyle)
#
# COME SI USA (2 minuti):
#   1) Incolla TUTTO questo file in git.py, subito PRIMA della riga
#          if __name__ == "__main__":
#      Le funzioni qui sotto hanno lo stesso nome di quelle originali:
#      in Python vince l'ultima definizione, quindi sostituiscono le vecchie
#      senza dover cancellare nulla (le vecchie puoi eliminarle quando vuoi).
#   2) In main() cambia queste 2 cose:
#
#      a) il box "Bentornato" (le righe print_box([...], "Bentornato")) diventa:
#             welcome(me, cfg, repos, missing, changed)
#
#      b) in fondo, nel ramo ch == "0":
#             print(grad("blue_to_purple", "A presto!"))
#         diventa:
#             goodbye()
#
# Funziona anche senza pystyle (colori pieni al posto dei gradienti).
# ============================================================================
import unicodedata

# ============================================================================
# BLOCCO 1 · Helper grafici (incolla vicino alle altre funzioni UI)
# ============================================================================
STATUS_COL = {"new": "info", "upd": "mint", "ok": "ok", "skip": "dim", "warn": "warn", "err": "err"}

# (gradiente andata, gradiente ritorno): la cornice dei box chiude un anello continuo di colore
BOX_GRAD = {
    "acc": ("blue_to_purple", "purple_to_blue"),
    "info": ("cyan_to_blue", "blue_to_cyan"),
    "ok": ("green_to_cyan", "cyan_to_green"),
    "warn": ("yellow_to_red", "red_to_yellow"),
    "err": ("red_to_purple", "purple_to_red"),
}

LANG_COL = {
    "Python": (86, 156, 214), "JavaScript": (241, 224, 90), "TypeScript": (49, 120, 198),
    "HTML": (227, 76, 38), "CSS": (139, 92, 246), "Shell": (137, 224, 81), "Java": (230, 150, 40),
    "Kotlin": (169, 123, 255), "C": (150, 150, 170), "C++": (243, 75, 125), "Go": (0, 173, 216),
    "Rust": (222, 165, 132), "Vue": (65, 184, 131), "EJS": (166, 202, 80), "SCSS": (198, 83, 140),
}


def rgb(r, g, b):
    return f"\033[38;2;{r};{g};{b}m" if TTY else ""


def _cw(ch):
    """Larghezza in celle di un carattere (emoji/CJK = 2, combinanti = 0)."""
    if ch in "\u200b\u200d\ufe0f" or unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def vlen(s):
    return sum(_cw(c) for c in ANSI_RE.sub("", s))


def clip(s, width):
    if vlen(s) <= width:
        return s
    out, n = [], 0
    for tok in re.split(r"(\x1b\[[0-9;]*m)", s):
        if tok.startswith("\x1b["):
            out.append(tok)
            continue
        for ch in tok:
            w = _cw(ch)
            if n + w > width - 1:
                return "".join(out) + "…" + PAL.get("rst", "")
            out.append(ch)
            n += w
    return "".join(out)


def fit(s, width):
    """Taglia/riempie a una larghezza esatta (utile per colonne allineate)."""
    s = clip(s, width)
    return s + " " * max(0, width - vlen(s))


def grad(name, text, vertical=False, diag=False):
    """Testo con gradiente pystyle (orizzontale, verticale o diagonale). Fallback: testo semplice."""
    if not (TTY and HAS_PYSTYLE):
        return text
    try:
        g = getattr(Colors, name, None) or Colors.cyan_to_blue
        fn = Colorate.Diagonal if diag else (Colorate.Vertical if vertical else Colorate.Horizontal)
        return fn(g, ANSI_RE.sub("", text)) + PAL.get("rst", "")
    except Exception:
        return text


def gpaint(name, text, key="acc", **kw):
    """Gradiente se pystyle c'e', altrimenti colore pieno."""
    g = grad(name, text, **kw)
    if g != text:
        return g
    return "\n".join(paint(l, key) for l in text.split("\n"))


def hr(width=None, name="blue_to_purple"):
    return gpaint(name, "━" * (width or min(term_cols() - 4, 60)))


def wrap_items(items, width, sep="   "):
    rows, cur = [], ""
    for it in items:
        cand = it if not cur else cur + sep + it
        if cur and vlen(cand) > width:
            rows.append(cur)
            cur = it
        else:
            cur = cand
    return rows + ([cur] if cur else [])


def stack_bar(items, width):
    """Barra impilata proporzionale. items = [(conteggio, chiave_colore), ...]"""
    items = [(c, k) for c, k in items if c > 0]
    total = sum(c for c, _ in items)
    if not total:
        return paint("─" * width, "dim")
    ws = [max(1, round(width * c / total)) for c, _ in items]
    ws[ws.index(max(ws))] += width - sum(ws)
    return "".join(paint("█" * max(w, 0), k) for (_, k), w in zip(items, ws))


# ============================================================================
# BLOCCO 2 · init_ui, say, print_box
# ============================================================================
def init_ui():
    global PAL, ICON
    keys = {
        "ok": rgb(80, 250, 123), "err": rgb(255, 85, 85), "warn": rgb(255, 184, 108),
        "info": rgb(139, 233, 253), "acc": rgb(189, 147, 249), "pink": rgb(255, 121, 198),
        "mint": rgb(94, 234, 212), "hi": "\033[1;97m" if TTY else "",
        "dim": rgb(120, 124, 153), "rst": "\033[0m" if TTY else "",
    }
    PAL = keys if TTY else {k: "" for k in keys}
    ICON = {
        "new": paint("⬇", "info"), "upd": paint("↻", "mint"), "ok": paint("✔", "ok"),
        "skip": paint("·", "dim"), "warn": paint("▲", "warn"), "err": paint("✖", "err"),
        "info": paint("●", "info"),
    }


def say(kind, text):
    print(f" {ICON.get(kind, ICON.get('info', ''))} {text}")


def print_box(lines, title="", kind="acc", footer=""):
    """Box arrotondato con cornice a gradiente continuo. kind: acc|info|ok|warn|err."""
    lines = [str(l).replace("\t", " ") for l in lines] or [""]
    inner = max([vlen(l) for l in lines] + [vlen(title) + 4, vlen(footer) + 4, 34])
    inner = min(inner, term_cols() - 4)
    fwd, rev = BOX_GRAD.get(kind, BOX_GRAD["acc"])
    key = kind if kind in PAL else "acc"
    rst = PAL["rst"]

    def edge(g, s, vertical=False):
        return gpaint(g, s, key, vertical=vertical)

    head = f"╭─ {title} " if title else "╭"
    foot = f"╰─ {footer} " if footer else "╰"
    top = head + "─" * max(1, inner + 3 - vlen(head)) + "╮"
    bottom = foot + "─" * max(1, inner + 3 - vlen(foot)) + "╯"
    bars = "\n".join("│" for _ in lines)
    n = len(lines)
    lb = (edge(fwd, bars, True).split("\n") + ["│"] * n)[:n]
    rb = (edge(rev, bars, True).split("\n") + ["│"] * n)[:n]

    print(edge(fwd, top))
    for l, a, b in zip(lines, lb, rb):
        l = clip(l, inner)
        print(f"{a}{rst} {l}{' ' * (inner - vlen(l))} {b}{rst}")
    print(edge(rev, bottom))


# ============================================================================
# BLOCCO 3 · banner (con logo grande adattivo)
# ============================================================================
_GLYPHS = {
    "A": [" █████╗ ", "██╔══██╗", "███████║", "██╔══██║", "██║  ██║", "╚═╝  ╚═╝"],
    "U": ["██╗   ██╗", "██║   ██║", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    "T": ["████████╗", "╚══██╔══╝", "   ██║   ", "   ██║   ", "   ██║   ", "   ╚═╝   "],
    "O": [" ██████╗ ", "██╔═══██╗", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    "P": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔═══╝ ", "██║     ", "╚═╝     "],
    "I": ["██╗", "██║", "██║", "██║", "██║", "╚═╝"],
    "L": ["██╗     ", "██║     ", "██║     ", "██║     ", "███████╗", "╚══════╝"],
}


def _big(word):
    rows = [""] * 6
    for ch in word:
        for i, part in enumerate(_GLYPHS[ch]):
            rows[i] += part
    return [r.rstrip() for r in rows]


def banner(animate=True):
    if TTY:
        try:
            System.Clear()
        except Exception:
            print("\033[2J\033[H", end="")
        try:
            System.Title("GH Autopilot")
        except Exception:
            pass
    cols = term_cols()
    big = _big("AUTOPILOT")
    compact = ["┏━╸ ╻ ╻   ┏━┓ ╻ ╻ ╺┳╸ ┏━┓ ┏━┓ ╻ ╻   ┏━┓ ╺┳╸",
               "┃╺┓ ┣━┫   ┣━┫ ┃ ┃  ┃  ┃ ┃ ┣━┛ ┃ ┃   ┃ ┃  ┃ ",
               "┗━┛ ╹ ╹   ╹ ╹ ┗━┛  ╹  ┗━┛ ╹   ╹ ┗━╸ ┗━┛  ╹ "]
    if cols >= max(map(len, big)) + 2:
        art, tag = big, "◆  G H  ◆"
    elif cols >= 44:
        art, tag = compact, ""
    else:
        art, tag = ["GH AUTOPILOT"], ""
    w = max(len(l) for l in art)

    def center(text, plain_len):
        return " " * max(0, (cols - plain_len) // 2) if TTY else ""

    colored = gpaint("blue_to_purple", "\n".join(art), "acc", diag=True).split("\n")
    print()
    if tag:
        print(center(tag, len(tag)) + gpaint("cyan_to_blue", tag, "info"))
    pad = center("", w)
    for line in colored:
        print(pad + line)
        if animate and TTY:
            time.sleep(0.035)
    sub = "sync  ·  backup  ·  crea  ·  sostituisci  ·  gestisci"
    if len(sub) > cols - 2:
        sub = "sync · backup · crea · gestisci"
    print("\n" + center(sub, len(sub)) + gpaint("purple_to_blue", sub, "dim"))
    ver = f"v2 · python {sys.version_info.major}.{sys.version_info.minor} · pystyle {'on' if HAS_PYSTYLE else 'off'}"
    print(center(ver, len(ver)) + paint(ver, "dim"))
    line_w = min(cols - 4, max(w, 30))
    print(center("", line_w) + hr(line_w) + "\n")


# ============================================================================
# BLOCCO 4 · Barra di progresso (sostituisce Progress.render senza toccare la classe)
# ============================================================================
def progress_render(self):
    if not TTY:
        return
    pct = min((self.done / self.total) if self.total else 1.0, 1.0)
    finished = self.done >= self.total
    elapsed = time.time() - self.t0
    eta = elapsed / self.done * (self.total - self.done) if 0 < self.done < self.total else 0
    parts = [f"{self.done}/{self.total} {self.unit}", f"◷ {fmt_t(elapsed)}"]
    if self.unit != "file":
        parts.append(f"{self.files} file")
    if eta:
        parts.append(f"ETA {fmt_t(eta)}")
    if self.stage:
        parts.append(clip(self.stage, 24))
    cols = term_cols()
    fixed = vlen(self.label) + 13  # spinner, spazi, percentuale
    while len(parts) > 1 and fixed + len(" · ".join(parts)) + 8 > cols - 1:
        parts.pop()
    tail = " · ".join(parts)
    bw = max(8, min(40, cols - fixed - len(tail) - 1))
    pos = pct * bw
    full = bw if finished else min(int(pos), bw - 1)
    head = "" if finished else "╸"
    filled = "━" * full + head
    rest = "─" * (bw - len(filled))
    bar = gpaint("green_to_cyan" if finished else "cyan_to_blue", filled, "ok" if finished else "info") + paint(rest, "dim")
    spin = paint("✔", "ok") if finished else paint(SPIN[self._f % len(SPIN)], "acc")
    pc = paint(f"{pct * 100:5.1f}%", "ok" if finished else "hi")
    line = f"{spin} {paint(self.label, 'hi')} {bar} {pc}  {paint(tail, 'dim')}"
    sys.stdout.write("\r\033[2K" + clip(line, cols - 1))
    sys.stdout.flush()


Progress.render = progress_render


# ============================================================================
# BLOCCO 5 · Report
# ============================================================================
def print_report(rep):
    rows = rep["rows"]
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    n = len(rows)
    total_files = sum(r["files"] for r in rows)
    errs, warns = counts.get("err", 0), counts.get("warn", 0)
    kind = "err" if errs else "warn" if warns else "ok"
    if errs:
        verdict = paint(f"✖ {errs} con errori", "err")
    elif warns:
        verdict = paint(f"▲ {warns} da controllare", "warn")
    else:
        verdict = paint("✔ Tutto a posto", "ok")
    width = min(term_cols() - 6, 64)
    lines = [f"{verdict}  {paint('in', 'dim')} {paint(fmt_t(rep['elapsed']), 'hi')}"]
    if n:
        order = [k for k in NAMES if counts.get(k)]
        lines.append(stack_bar([(counts[k], STATUS_COL[k]) for k in order], max(10, min(40, width))))
        lines += wrap_items([f"{ICON[k]} {paint(counts[k], 'hi')} {paint(NAMES[k], 'dim')}" for k in order], width)
    lines += ["", f"{paint('Elementi', 'dim')} {paint(n, 'hi')}   {paint('File gestiti', 'dim')} {paint(total_files, 'hi')}"]
    worked = [r for r in rows if r["status"] != "skip"]
    if worked:
        avg = sum(r["secs"] for r in worked) / len(worked)
        lines.append(f"{paint('Media', 'dim')} {paint(f'{avg:.1f}s', 'hi')} per elemento")
        slow = sorted(worked, key=lambda r: -r["secs"])[:3]
        if slow[0]["secs"] >= 1:
            lines.append(f"{paint('Più lenti', 'dim')} " + ", ".join(f"{r['name'].split('/')[-1]} ({r['secs']:.1f}s)" for r in slow))
        big = sorted([r for r in rows if r["files"]], key=lambda r: -r["files"])[:3]
        if big:
            lines.append(f"{paint('Più file', 'dim')} " + ", ".join(f"{r['name'].split('/')[-1]} ({r['files']})" for r in big))
    for k, v in (rep.get("extra") or {}).items():
        lines.append(f"{paint(k, 'dim')} {v}")
    problems = [r for r in rows if r["status"] in ("warn", "err")]
    if problems:
        lines += ["", paint("Da controllare", "warn")]
        lines += [f" {ICON[r['status']]} {paint(r['name'], 'hi')}  {paint(r['msg'], STATUS_COL[r['status']])}" for r in problems[:15]]
        if len(problems) > 15:
            lines.append(paint(f" … e altri {len(problems) - 15}", "dim"))
    print()
    print_box(lines, f"Report · {rep['title']}", kind=kind, footer=str(rep.get("date", ""))[:16].replace("T", " "))


def show_reports():
    files = sorted(REPORT_DIR.glob("*.json"))[-10:][::-1] if REPORT_DIR.exists() else []
    if not files:
        say("skip", "Nessun report salvato.")
        return
    data = [load_json(f, {}) for f in files]
    lines = []
    for i, d in enumerate(data, 1):
        rws = d.get("rows", [])
        st = "err" if any(r.get("status") == "err" for r in rws) else "warn" if any(r.get("status") == "warn" for r in rws) else "ok"
        nfiles = sum(r.get("files", 0) for r in rws)
        when = str(d.get("date", ""))[5:16].replace("T", " ")
        stats = "%d el · %d file · %s" % (len(rws), nfiles, fmt_t(d.get("elapsed", 0)))
        lines.append(f"{paint(str(i).rjust(2), 'info')} {ICON[st]} {paint(when, 'dim')}  "
                     f"{fit(paint(d.get('title', ''), 'hi'), 22)} {paint(stats, 'dim')}")
    lines.append(paint("numero = dettagli · invio = indietro", "dim"))
    print_box(lines, "Report precedenti", kind="info")
    ch = ask("Numero per i dettagli (invio = indietro): ")
    if ch.isdigit() and 1 <= int(ch) <= len(data):
        print_report(data[int(ch) - 1])


# ============================================================================
# BLOCCO 6 · ask / yesno
# ============================================================================
def ask(prompt, default=""):
    p = f"{paint('❯', 'acc')} {prompt}"
    if TTY and os.name != "nt":  # i codici colore non devono falsare il cursore di readline
        p = ANSI_RE.sub(lambda m: f"\001{m.group(0)}\002", p)
    try:
        v = input(p).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return v or default


def yesno(prompt, default=True):
    hint = (paint("[", "dim") + paint("S", "hi") + paint("/n]", "dim")) if default \
        else (paint("[s/", "dim") + paint("N", "hi") + paint("]", "dim"))
    v = ask(f"{prompt} {hint}: ").lower()
    if not v:
        return default
    return v in ("s", "si", "sì", "y", "yes")


# ============================================================================
# BLOCCO 7 · run_batch (log per repo allineato e mai più largo del terminale)
# ============================================================================
def run_batch(cfg, label, items, fn, unit="repo"):
    """fn(item) -> (stato, msg[, file]). Ritorna il report (dict)."""
    rep = Report(label)
    if not items:
        say("skip", "Niente da fare.")
        return finish_report(rep)

    def job(it):
        t = time.time()
        res = fn(it)
        return res[0], res[1], (res[2] if len(res) > 2 else 0), time.time() - t

    def fmt_name(full):
        owner, _, repo = full.partition("/")
        return paint(owner + "/", "dim") + paint(repo, "hi") if repo else paint(full, "hi")

    namew = max(12, min(34, max(len(i["full_name"]) for i in items), term_cols() // 2 - 4))
    ex = ThreadPoolExecutor(max_workers=max(1, int(cfg["workers"])))
    with Progress(label, len(items), unit) as pg:
        try:
            futs = {ex.submit(job, it): it for it in items}
            for f in as_completed(futs):
                it = futs[f]
                try:
                    st, msg, files, secs = f.result()
                except Exception as e:  # una repo che fallisce non ferma le altre
                    st, msg, files, secs = "err", str(e), 0, 0.0
                rep.add(it["full_name"], st, msg, files, secs)
                if st != "skip":
                    extra = paint(f" · {files} file", "dim") if files else ""
                    mcol = {"err": "err", "warn": "warn"}.get(st)
                    pg.log(clip(f" {ICON[st]} {fit(fmt_name(it['full_name']), namew)} {paint(msg, mcol) if mcol else msg}{extra}", term_cols() - 1))
                pg.advance(1, files)
            ex.shutdown(wait=True)
        except KeyboardInterrupt:
            pg.log(f" {ICON['warn']} Interrotto: attendo le operazioni in corso...")
            ex.shutdown(wait=True, cancel_futures=True)
            rep.extra["Interrotto"] = "sì"
    return finish_report(rep)


# ============================================================================
# BLOCCO 8 · pick (lista repo con colonne allineate, pallino linguaggio, paginazione)
# ============================================================================
def _repo_line(cfg, r, idx, w, fav, namew, show_lang):
    star = paint("★", "warn") if r["full_name"] in fav else " "
    tags = (paint("◆", "pink") if r["private"] else " ") + (paint("⑂", "mint") if r["fork"] else " ")
    own = (ME or {}).get("login")
    owner, _, name = r["full_name"].partition("/")
    label = paint(name, "hi") if owner == own else paint(owner + "/", "dim") + paint(name, "hi")
    lang = r.get("language") or ""
    col = LANG_COL.get(lang)
    dot = (rgb(*col) + "●" + PAL["rst"]) if col and TTY else paint("●" if lang else " ", "dim")
    ltxt = " " + paint(lang[:11], "dim") if (show_lang and lang) else ""
    return f"{paint(str(idx).rjust(w), 'info')} {repo_symbol(cfg, r)} {star}{tags} {fit(label, namew)} {dot}{ltxt}"


def pick(repos, cfg, verb, single=False, initial=""):
    """Menu numerato delle repo: sempre visibile, a pagine, con filtro e preferiti."""
    if PRESEL["repos"] is not None:
        pre, PRESEL["repos"] = PRESEL["repos"], None
        return pre[:1] if single else pre
    pool = order_repos(repos, cfg)
    q, page = initial, 0
    while True:
        found = match(pool, q) if q else pool
        if not found:
            say("warn", f"Nessuna repo per '{q}'.")
            q = ""
            continue
        if single and q and len(found) == 1:
            say("info", f"Trovata: {found[0]['full_name']}")
            remember(cfg, found)
            return found
        ps = max(8, min(25, shutil.get_terminal_size((80, 24)).lines - 12))
        pages = (len(found) + ps - 1) // ps
        page = max(0, min(page, pages - 1))
        fav = set(cfg.get("favorites", []))
        w = len(str(len(found)))
        chunk = found[page * ps:(page + 1) * ps]
        own = (ME or {}).get("login")
        longest = max(len(r["full_name"]) if r["full_name"].split("/")[0] != own else len(r["name"]) for r in chunk)
        room = term_cols() - 4 - w - 12
        want = min(longest, 36)
        show_lang = room - want >= 14
        namew = max(10, min(want, room - (13 if show_lang else 0)))
        lines = [_repo_line(cfg, r, i, w, fav, namew, show_lang) for i, r in enumerate(chunk, page * ps + 1)]
        lines.append("")
        legend = [f"{paint('●', 'ok')}{paint('aggiornata', 'dim')}", f"{paint('◐', 'warn')}{paint('novità', 'dim')}",
                  f"{paint('○', 'dim')}{paint('non clonata', 'dim')}", f"{paint('◆', 'pink')}{paint('privata', 'dim')}",
                  f"{paint('⑂', 'mint')}{paint('fork', 'dim')}", f"{paint('★', 'warn')}{paint('pref.', 'dim')}"]
        keys = [("1 3 5-8", "scegli")] + ([] if single else [("all", "tutte")]) + [("n/p", "pagina"), ("testo", "filtra"), ("*N", "★"), ("0", "esci")]
        lines += wrap_items(legend, term_cols() - 6, " ")
        lines += wrap_items([paint(k, "info") + " " + paint(d, "dim") for k, d in keys], term_cols() - 6, " · ")
        title = f"{verb.capitalize()} · {len(found)} repo" + (f" · filtro '{q}'" if q else "")
        footer = f"◂ p   pag {page + 1}/{pages}   n ▸" if pages > 1 else ""
        print_box(lines, title, kind="info", footer=footer)
        sel = ask("Scelta: ").strip()
        low = sel.lower()
        if low in ("", "0", "q"):
            return []
        if low in ("n", ">"):
            page += 1
        elif low in ("p", "<"):
            page -= 1
        elif low in ("/", "reset"):
            q, page = "", 0
        elif low.startswith("/"):
            q, page = sel[1:].strip(), 0
        elif low.startswith("*"):
            nums = parse_numbers(low[1:], len(found))
            if nums:
                favs = list(cfg.get("favorites", []))
                for i in nums:
                    n = found[i - 1]["full_name"]
                    favs.remove(n) if n in favs else favs.append(n)
                cfg["favorites"] = favs
                save_config(cfg)
                pool = order_repos(repos, cfg)
            else:
                say("warn", "Numero non valido.")
        elif low == "all":
            if single:
                say("warn", "Qui serve una sola repo.")
                continue
            remember(cfg, found)
            return found
        else:
            nums = parse_numbers(low, len(found))
            if nums is None:  # non sono numeri: lo uso come filtro
                q, page = sel, 0
            elif not nums:
                say("warn", f"Numero non valido (1-{len(found)}).")
            elif single and len(nums) != 1:
                say("warn", "Seleziona una sola repo.")
            else:
                out = [found[i - 1] for i in nums]
                remember(cfg, out)
                return out


# ============================================================================
# BLOCCO 9 · show_status
# ============================================================================
def show_status(cfg):
    rows = probe_all(cfg)
    if not rows:
        say("warn", f"Nessuna repo locale in {cfg['base_dir']}")
        return
    rows.sort(key=lambda x: (not x["dirty"], not x["ahead"], not x["behind"], str(x["path"]).lower()))
    flagged = [x for x in rows if x["dirty"] or x["ahead"] or x["behind"]]
    clean = len(rows) - len(flagged)
    lines = []
    if flagged:
        names = [f"{x['path'].parent.name}/{x['path'].name}" for x in flagged]
        namew = max(10, min(max(map(len, names)), term_cols() - 4 - 22))
        for x, nm in zip(flagged, names):
            owner, _, repo = nm.partition("/")
            chips = []
            if x["dirty"]:
                chips.append(paint(f"✎{len(x['dirty'])}", "warn"))
            if x["ahead"]:
                chips.append(paint(f"↑{x['ahead']}", "info"))
            if x["behind"]:
                chips.append(paint(f"↓{x['behind']}", "pink"))
            lines.append(f"{ICON['warn']} {fit(paint(owner + '/', 'dim') + paint(repo, 'hi'), namew)} "
                         f"{paint(clip(x['branch'], 14), 'acc')} {' '.join(chips)}")
        lines += [""] + wrap_items([paint("✎ modificati", "dim"), paint("↑ da pushare", "dim"), paint("↓ da scaricare", "dim")], term_cols() - 6)
    else:
        lines.append(f"{ICON['ok']} {paint('Tutte le repo sono pulite e allineate', 'ok')}")
    lines.append(stack_bar([(clean, "ok"), (len(flagged), "warn")], max(10, min(40, term_cols() - 10))))
    lines += wrap_items([f"{paint(len(rows), 'hi')} {paint('repo locali', 'dim')}", f"{paint(clean, 'ok')} {paint('a posto', 'dim')}",
                         f"{paint(len(flagged), 'warn' if flagged else 'dim')} {paint('da guardare', 'dim')}"], term_cols() - 6, " · ")
    print_box(lines, "Stato locale", kind="warn" if flagged else "ok")


# ============================================================================
# BLOCCO 10 · welcome, goodbye, menu  (SECTIONS resta com'e')
# ============================================================================
SECTION_ICON = {"Sincronizzazione": "⟳", "Download e backup": "⇩", "Crea e sostituisci": "✚", "Strumenti": "⚙"}


def welcome(me, cfg, repos, missing, changed):
    h = datetime.now().hour
    hello = "Buongiorno" if 5 <= h < 13 else "Buon pomeriggio" if 13 <= h < 18 else "Buonasera" if 18 <= h < 23 else "Buonanotte"
    name = me.get("name") or me["login"]
    uptodate = max(len(repos) - len(missing) - len(changed), 0)
    lines = [
        f"{hello}, {gpaint('cyan_to_blue', name, 'hi')} {paint('@' + me['login'], 'dim')}",
        f"{paint('Ultimo sync', 'dim')} {paint(ago(cfg['last_sync']), 'hi')}",
        "",
        stack_bar([(uptodate, "ok"), (len(changed), "warn"), (len(missing), "info")], max(10, min(40, term_cols() - 10))),
        *wrap_items([f"{ICON['ok']} {paint(uptodate, 'hi')} {paint('allineate', 'dim')}",
                     f"{paint('↻', 'warn')} {paint(len(changed), 'hi')} {paint('novità', 'dim')}",
                     f"{ICON['new']} {paint(len(missing), 'hi')} {paint('da clonare', 'dim')}"], min(term_cols() - 6, 60)),
        f"{paint(len(repos), 'hi')} {paint('repo', 'dim')} · {paint(sum(1 for r in repos if r['private']), 'pink')} {paint('private', 'dim')} · "
        f"{paint(sum(1 for r in repos if r['fork']), 'mint')} {paint('fork', 'dim')}",
    ]
    print_box(lines, "Bentornato", kind="ok")


def goodbye():
    print("\n " + gpaint("blue_to_purple", "✦ Alla prossima! Le tue repo sono in buone mani.", "acc") + "\n")


def _menu_item(k, t):
    base, sep, hint = t.partition("(")
    hint = paint("(" + hint, "dim") if sep else ""
    key = paint(k.rjust(2), "err" if k == "0" else "info")
    return f" {key} {paint('│', 'dim')} {base.strip()} {hint}".rstrip()


def show_menu(cfg=None, repos=None):
    footer = ""
    if cfg is not None:
        cols = term_cols() - 1
        print("\n" + clip(f" {paint('➤', 'acc')} {smart_hints(cfg, repos)}", cols))
        top = sorted(cfg.get("usage", {}).items(), key=lambda kv: -kv[1])[:3]
        names = dict((k, v) for _, items in SECTIONS for k, v in items)
        if top:
            chips = "  ".join(paint(k, "info") + paint(" " + names.get(k, "").split("(")[0].strip().lower(), "dim") for k, _ in top)
            print(clip(f" {paint('★', 'warn')} {paint('più usati', 'dim')}  {chips}", cols))
        tip = " ✎ prova anche: 'scarica emporio', 'zip tutte'" if cols < 70 else " ✎ scrivi anche a parole: 'scarica emporio', 'sostituisci emporio46', 'zip tutte'"
        print(clip(paint(tip, "dim"), cols))
        footer = f"ultimo sync {ago(cfg['last_sync'])}"
    lines = []
    for title, items in SECTIONS:
        lines.append(gpaint("purple_to_blue", f"{SECTION_ICON.get(title, '•')} {title.upper()}", "acc"))
        lines += [_menu_item(k, t) for k, t in items]
        lines.append("")
    print_box(lines[:-1], "Menu", footer=footer)


# ============================================================================
# BLOCCO 11 · settings (interruttori ON/OFF colorati)
# ============================================================================
def _switch(on):
    return paint("●━ ON ", "ok") if on else paint("━○ OFF", "dim")


def settings(cfg):
    toggles = [
        ("include_forks", "Includi fork"), ("include_archived", "Includi repo archiviate"),
        ("include_orgs", "Includi repo di organizzazioni"), ("sync_forks", "Sincronizza fork con upstream"),
        ("submodules", "Gestisci submodule"), ("auto_backup", "Backup automatico prima di sostituzioni/eliminazioni"),
    ]
    while True:
        n = len(toggles) + 2
        num = lambda i: paint(str(i).rjust(2), "info")
        lines = [f"{num(1)} {paint('│', 'dim')} {paint('Cartella base', 'dim')}  {paint(cfg['base_dir'], 'hi')}", ""]
        lines += [f"{num(i)} {paint('│', 'dim')} {_switch(cfg[k])} {label}" for i, (k, label) in enumerate(toggles, 2)]
        lines += ["",
                  f"{num(n)} {paint('│', 'dim')} {paint('Strategia branch divergenti', 'dim')}  "
                  f"{paint(cfg['strategy'], 'acc')} {paint('(safe/rebase)', 'dim')}",
                  f"{num(n + 1)} {paint('│', 'dim')} {paint('Operazioni parallele', 'dim')}  {paint(cfg['workers'], 'hi')}",
                  f"{num(n + 2)} {paint('│', 'dim')} {paint('Dimentica token e logout', 'err')}",
                  f"{paint(' 0', 'err')} {paint('│', 'dim')} Indietro"]
        print_box(lines, "Impostazioni", kind="info")
        ch = ask("Scelta [0]: ", "0")
        if ch == "0":
            save_config(cfg)
            return
        if not ch.isdigit():
            continue
        i = int(ch)
        if i == 1:
            cfg["base_dir"] = str(Path(ask("Nuova cartella: ", cfg["base_dir"])).expanduser())
        elif 2 <= i < n:
            k = toggles[i - 2][0]
            cfg[k] = not cfg[k]
        elif i == n:
            cfg["strategy"] = "rebase" if cfg["strategy"] == "safe" else "safe"
        elif i == n + 1:
            v = ask("Numero di worker (1-16): ", str(cfg["workers"]))
            cfg["workers"] = min(max(int(v), 1), 16) if v.isdigit() else cfg["workers"]
        elif i == n + 2:
            forget_token()
            cfg["user"] = None
            save_config(cfg)
            say("ok", "Token dimenticato. Al prossimo avvio ti verrà richiesto.")
            sys.exit(0)
        save_config(cfg)
       
if __name__ == "__main__":
    main()