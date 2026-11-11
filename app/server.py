#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub-Backup-Manager – lokale Web-App & Backup-Engine (nur Python-Stdlib).

Spiegelt alle Repos eines GitHub-Accounts (1:1 inkl. Branches/Tags) nach
/repos/<owner>/<name>.git, optional plus Wiki und Issues-/Releases-Export.

Aufruf:
  python3 server.py serve        # Web-UI starten (Default-Port 4242)
  python3 server.py backup-all   # Timer-Job: alle Konten aktualisieren
  python3 server.py status       # Kurzstatus für Scripts/systemd

Konfiguration/Daten liegen unter GITHUB_BACKUP_HOME (Standard: /opt/github-backup).
Ein PAT wird ausschließlich lokal in config.json (0600) gespeichert – nie im Repo,
nie in Logs (in API-Antworten maskiert).
"""

import fcntl
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── Pfade & Konstanten ─────────────────────────────────────────────────────
BASE_DIR = os.environ.get("GITHUB_BACKUP_HOME", "/opt/github-backup")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
REPOS_DIR = os.path.join(BASE_DIR, "repos")
SNAPSHOTS_DIR = os.path.join(BASE_DIR, "snapshots")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
LOCK_PATH = os.path.join(BASE_DIR, "backup.lock")
ASKPASS_PATH = os.path.join(BASE_DIR, "askpass.sh")
TIMER_UNIT_PATH = "/etc/systemd/system/ghbackup.timer"

PORT = int(os.environ.get("GITHUB_BACKUP_PORT", "4242"))
APP_VERSION = "1.1.7"
API_BASE = os.environ.get("GITHUB_API_BASE", "https://api.github.com")  # Override für Tests/Enterprise

PRESETS = ("code", "wiki", "full")   # ① nur Code ② Code+Wiki ③ Komplett
TOKEN_MASK = "••••••"
CLONE_TIMEOUT = 3600                  # Sekunden pro git-Operation
API_TIMEOUT = 30                      # Sekunden pro GitHub-API-Request

# ── Kleine Helfer ──────────────────────────────────────────────────────────
def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_dirs():
    for d in (BASE_DIR, REPOS_DIR, SNAPSHOTS_DIR, LOGS_DIR):
        os.makedirs(d, exist_ok=True)


def load_config():
    cfg = {
        "accounts": [],            # [{name, token, include_forks}]
        "preset": "code",
        "timer_hour": 3,
        "excluded": [],            # full_names, die NICHT gesichert werden
    }
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            cfg.update({k: stored[k] for k in cfg if k in stored})
    except FileNotFoundError:
        pass
    except Exception as exc:  # kaputte Config nicht verschweigen
        print("WARN: config.json unlesbar (%s) – nutze Defaults" % exc, file=sys.stderr)
    cfg["preset"] = cfg["preset"] if cfg["preset"] in PRESETS else "code"
    try:
        cfg["timer_hour"] = max(0, min(23, int(cfg["timer_hour"])))
    except (TypeError, ValueError):
        cfg["timer_hour"] = 3
    if not isinstance(cfg["accounts"], list):
        cfg["accounts"] = []
    if not isinstance(cfg["excluded"], list):
        cfg["excluded"] = []
    return cfg


def save_config(cfg):
    fd_tmp = CONFIG_PATH + ".tmp"
    with open(fd_tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.chmod(fd_tmp, 0o600)  # Token bleibt nur für den Besitzer lesbar
    os.replace(fd_tmp, CONFIG_PATH)


def mask_token(token):
    if not token:
        return ""
    return TOKEN_MASK + token[-4:]


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"repos": {}, "snapshots": [], "exports": [], "last_run": None}


def save_state(state):
    fd_tmp = STATE_PATH + ".tmp"
    with open(fd_tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(fd_tmp, STATE_PATH)


STATE_LOCK = threading.Lock()  # Job-Thread & HTTP-Thread greifen beide auf STATE zu

# ── Askpass (Token bleibt aus .git/config fern) ────────────────────────────
def ensure_askpass():
    """git holt Zugangsdaten über dieses Skript (GIT_ASKPASS) – so landet der
    Token NICHT in der .git/config der Mirrors."""
    try:
        with open(ASKPASS_PATH, "w", encoding="utf-8") as f:
            f.write(
                "#!/bin/sh\n"
                "# GitHub-Backup-Manager: fragt git nach Zugangsdaten\n"
                'case "$1" in\n'
                '  Username*) echo "x-access-token" ;;\n'
                '  *) echo "${GITHUB_BACKUP_TOKEN}" ;;\n'
                "esac\n"
            )
        os.chmod(ASKPASS_PATH, 0o700)
    except OSError as exc:
        print("WARN: askpass.sh konnte nicht geschrieben werden: %s" % exc, file=sys.stderr)

# ── GitHub-API ─────────────────────────────────────────────────────────────
def _parse_link_next(link_header):
    """Liest aus einem Link-Header die rel="next"-URL (Cursor-Pagination)."""
    if not link_header:
        return None
    for part in link_header.split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        if any('rel="next"' in s or "rel='next'" in s for s in segments[1:]):
            return segments[0].strip().strip("<>")
    return None


def _friendly_net_error(err, host="api.github.com"):
    """Übersetzt rohe Netzwerkfehler in verständliche, handlungsrelevante Meldungen."""
    low = str(err).lower()
    if "name or service not known" in low or "name resolution" in low or "no address" in low:
        return ("DNS-Auflösung von %s fehlgeschlagen (%s). "
                "Mögliche Ursachen: kein Internet, flaky WLAN/DNS oder falscher DNS-Server. "
                "Prüfen: 'ping -c1 %s' und 'cat /etc/resolv.conf' – "
                "bei kurzem DNS-Aussetzer einfach erneut scannen." % (host, err, host))
    if "timed out" in low or "timeout" in low:
        return ("Netzwerk-Timeout bei %s (%s) – "
                "Verbindung instabil? Erneut versuchen." % (host, err))
    return str(err)


# GitHub-Hosts, deren Auflösung wir selbst übernehmen (lokale DNS-Server –
# AdGuard/Router – antworten in manchen Netzen zeitweise NXDOMAIN, woraufhin
# glibc die nachfolgenden Nameserver nie anfragt. Beweis: strace 2026-11-11).
GITHUB_HOSTS = ("api.github.com", "github.com", "codeload.github.com")
PUBLIC_RESOLVERS = ("1.1.1.1", "8.8.8.8", "9.9.9.9")
HOSTS_MARKER = "# GitHub-Backup-Manager"


def _dns_query_a(server, name, timeout=4):
    """Rohe UDP-DNS-Anfrage (A-Record) direkt an einen öffentlichen Resolver –
    BYPASS für das lokale DNS. Liefert die erste A-IP oder None."""
    try:
        query = struct.pack(">HHHHHH", 0x4321, 0x0100, 1, 0, 0, 0)
        for part in name.split("."):
            query += bytes([len(part)]) + part.encode()
        query += b"\x00" + struct.pack(">HH", 1, 1)  # QTYPE=A, QCLASS=IN
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(query, (server, 53))
        data, _ = sock.recvfrom(512)
        sock.close()
        rcode = data[3] & 0x0F
        if rcode != 0:
            return None
        ancount = struct.unpack(">H", data[6:8])[0]
        if ancount == 0:
            return None
        # Antwort-Sektion parsen: Namen überspringen (Pointer-Kompression), A-RR lesen
        offset = 12
        while data[offset] != 0:            # Question-Name
            if data[offset] & 0xC0:
                offset += 1
                break
            offset += data[offset] + 1
        offset += 5                          # Null-Byte + QTYPE + QCLASS
        for _ in range(ancount):
            if data[offset] & 0xC0:          # Pointer auf Namen
                offset += 2
            else:
                while data[offset] != 0:
                    offset += data[offset] + 1
                offset += 1
            rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[offset:offset + 10])
            offset += 10
            if rtype == 1 and rdlen == 4:    # A-Record
                return ".".join(str(b) for b in data[offset:offset + 4])
            offset += rdlen
    except (OSError, struct.error, IndexError):
        return None
    return None


def _public_dns_resolve(host):
    """Resolve `host` über öffentliche Resolver (1.1.1.1 → 8.8.8.8 → 9.9.9.9)."""
    for resolver in PUBLIC_RESOLVERS:
        ip = _dns_query_a(resolver, host)
        if ip:
            return ip
    return None


def refresh_github_hosts():
    """Pinnt die GitHub-IPs in /etc/hosts (nur root). Danach geht getaddrinfo
    für api.github.com/github.com/codeload.github.com immer über die hosts-Datei
    – komplett unabhängig vom lokalen DNS-Server. Gilt auch für `git clone`."""
    if os.geteuid() != 0:
        return False
    entries = []
    for host in GITHUB_HOSTS:
        ip = _public_dns_resolve(host)
        if ip:
            entries.append((ip, host))
    if not entries:
        return False
    try:
        with open("/etc/hosts", "r", encoding="utf-8") as f:
            content = f.read()
        kept = [line for line in content.splitlines() if HOSTS_MARKER not in line]
        pinned = ["%s %s  %s" % (ip, host, HOSTS_MARKER) for ip, host in entries]
        with open("/etc/hosts", "w", encoding="utf-8") as f:
            f.write("\n".join(kept).rstrip("\n") + "\n" + "\n".join(pinned) + "\n")
        print("GitHub-Hosts in /etc/hosts gepinnt (%d Einträge, öffentliche "
              "Resolver): %s" % (len(entries), ", ".join(h for _, h in entries)),
              flush=True)
        return True
    except OSError:
        return False


_LAST_DNS_HEAL = 0.0  # Selbstheilung max. 1x pro Stunde anfassen


def _heal_dns():
    """DNS-Selbstheilung im Container (App läuft dort als root): schreibt
    /etc/resolv.conf auf öffentliche Resolver um, wenn die Auflösung gerade tot
    ist. UMSCHREIBEN statt Anhängen: antwortet der erste Nameserver mit NXDOMAIN
    (z. B. AdGuard-Filterlisten) oder gar nicht (flaky Router-DNS), fragt glibc
    die nachfolgenden gar nicht mehr – angehängte Resolver wären wirkungslos.
    Backup unter resolv.conf.bak. Nicht-Root (lokale Testinstanz) und Fehler
    werden still ignoriert."""
    global _LAST_DNS_HEAL
    if os.geteuid() != 0:
        return False
    if time.time() - _LAST_DNS_HEAL < 3600:
        return False
    try:
        with open("/etc/resolv.conf", "r", encoding="utf-8") as f:
            content = f.read()
        if "1.1.1.1" in content and "8.8.8.8" in content:
            return False
        with open("/etc/resolv.conf.bak", "w", encoding="utf-8") as f:
            f.write(content + "\n# (Backup vor DNS-Selbstheilung)\n")
        with open("/etc/resolv.conf", "w", encoding="utf-8") as f:
            f.write("# GitHub-Backup-Manager DNS-Selbstheilung (Backup: resolv.conf.bak)\n"
                    "nameserver 1.1.1.1\nnameserver 8.8.8.8\nnameserver 9.9.9.9\n")
        _LAST_DNS_HEAL = time.time()
        print("DNS-Selbstheilung: /etc/resolv.conf auf öffentliche Resolver "
              "umgeschrieben (Backup: resolv.conf.bak)", flush=True)
        return True
    except OSError:
        return False


def gh_request(path, token=None, params=None, url=None, retries=4):
    """Ein GET auf api.github.com. Liefert (status, daten|fehlermeldung, next_url).
    `url` überschreibt path+params (wird für Cursor-Pagination genutzt).
    `path` darf auch eine absolute URL sein (Link-Header liefert absolute
    next-URLs — der Bug von 2026-11-11: https://api.github.comhttps://...).
    Transiente Netzfehler (DNS/Timeout/5xx) werden mit Backoff wiederholt."""
    if url is None:
        if path.startswith(("http://", "https://")):
            url = path                      # absolute URL — nicht präfixieren!
        else:
            url = API_BASE + path
            if params:
                url += "?" + urllib.parse.urlencode(params)
    last_err = "unbekannt"
    try:
        fail_host = urllib.parse.urlparse(url).hostname or "api.github.com"
    except ValueError:
        fail_host = "api.github.com"
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={
            "User-Agent": "GitHub-Backup-Manager/" + APP_VERSION,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return resp.status, data, _parse_link_next(resp.headers.get("Link"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("message", "")
            except Exception:
                detail = ""
            detail = detail or exc.reason
            if exc.code >= 500 and attempt < retries - 1:   # GitHub-seitig transient
                last_err = "HTTP %s: %s" % (exc.code, detail)
                time.sleep(1 + attempt)
                continue
            return exc.code, detail, None
        except Exception as exc:                            # DNS, Timeout, ...
            last_err = "%s: %s" % (type(exc).__name__, exc)
            if attempt < retries - 1:
                _heal_dns()                                 # Container-Selbstheilung
                time.sleep(min(1 + attempt, 8))             # 1, 2, 4, 8 s Backoff
    return 0, _friendly_net_error(last_err, fail_host), None


def gh_paginate(path, token=None, params=None, max_pages=150):
    """Paginiert durch eine GitHub-List-Resource – folgt dem Link-Header
    (rel="next"), damit auch Endpoints mit Cursor-Pagination über das
    page-basierte Limit hinaus komplett geliefert werden."""
    items, url = [], None
    params = dict(params or {})
    params.setdefault("per_page", 100)
    for _ in range(max_pages):
        if url is None:
            status, data, nxt = gh_request(path, token, params)
        else:
            status, data, nxt = gh_request(url, token)
        if status != 200:
            return status, data  # Fehler weiterreichen
        if not isinstance(data, list) or not data:
            break
        items.extend(data)
        if nxt is None:
            break
        url = nxt
    return 200, items


def scan_account(account):
    """Liefert (repos, fehler) für einen Account. Mit Token: /user/repos
    (inkl. private, zählt auch Forks/Owner-Flag), sonst öffentliche Repos."""
    token = (account.get("token") or "").strip()
    name = (account.get("name") or "").strip()
    include_forks = bool(account.get("include_forks"))
    if token:
        status, me, _nxt = gh_request("/user", token)
        if status != 200:
            return None, "Token-Prüfung fehlgeschlagen (HTTP %s): %s" % (status, me)
        if not isinstance(me, dict):
            return None, "Unerwartete API-Antwort auf /user (kein Objekt)"
        login = me.get("login", name or "?")
        status, repos = gh_paginate("/user/repos", token,
                                    {"type": "owner", "sort": "full_name"})
        if status != 200:
            return None, "Repo-Liste fehlgeschlagen (HTTP %s): %s" % (status, repos)
        scanned = login
    else:
        if not name:
            return None, "Kein Konto-Name angegeben"
        status, repos = gh_paginate("/users/%s/repos" % urllib.parse.quote(name), None,
                                    {"type": "owner", "sort": "full_name"})
        if status != 200:
            if status == 404:
                return None, "Account '%s' nicht gefunden" % name
            if status == 403:
                return None, "Rate-Limit erreicht (60/h anonym) – PAT in der UI eintragen"
            return None, "Repo-Liste fehlgeschlagen (HTTP %s): %s" % (status, repos)
        scanned = name
    if not isinstance(repos, list):
        return None, "Unerwartete API-Antwort (Repo-Liste ist kein Array)"
    out = []
    for r in repos:
        out.append({
            "full_name": r.get("full_name", ""),
            "private": bool(r.get("private")),
            "fork": bool(r.get("fork")),
            "size_kb": r.get("size", 0),
            "updated_at": r.get("updated_at", ""),
            "has_wiki": bool(r.get("has_wiki")),
            "archived": bool(r.get("archived")),
        })
    if not include_forks:
        out = [r for r in out if not r["fork"]]
    return {"scanned": scanned, "repos": out}, None

# ── Git-Operationen ────────────────────────────────────────────────────────
def git_env(token):
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"          # niemals auf Eingabe warten
    env["GIT_ASKPASS"] = ASKPASS_PATH
    env["GITHUB_BACKUP_TOKEN"] = token or ""
    return env


def run_git(args, token, cwd=None, timeout=CLONE_TIMEOUT):
    # http.lowSpeed* : abbrechen, wenn der Transfer <1 KB/s für 60 s fällt
    # (statt endlos an einer gestorbenen Verbindung zu hängen).
    return subprocess.run(
        ["git", "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60"] + args,
        cwd=cwd, env=git_env(token),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", timeout=timeout,
    )


def mirror_path(full_name, suffix=""):
    owner, _, name = full_name.partition("/")
    safe_owner = owner.replace(".", "_") if owner in (".", "..") else owner
    return os.path.join(REPOS_DIR, safe_owner, name + suffix + ".git")


def last_stderr(proc, n=6):
    lines = (proc.stderr or "").strip().splitlines()
    return "\n".join(lines[-n:]) if lines else "(keine Ausgabe)"


def backup_single_repo(full_name, token, preset, log):
    """Sichert ein Repo als Mirror. Rückgabe: (status, meta, hinweis)
    status: ok | warn | fail"""
    owner, _, name = full_name.partition("/")
    meta = {}
    git_url = "https://github.com/%s.git" % full_name
    mdir = mirror_path(full_name)

    # 1) Code-Mirror (neu klonen oder aktualisieren)
    if os.path.isdir(mdir) and os.path.exists(os.path.join(mdir, "HEAD")):
        log("↻  %s: Aktualisiere Mirror ..." % full_name)
        proc = run_git(["-C", mdir, "remote", "update", "--prune"], token)
        if proc.returncode != 0:
            return "fail", meta, "git remote update fehlgeschlagen:\n" + last_stderr(proc)
    else:
        if os.path.isdir(mdir):  # kaputtes Halbfertig-Verzeichnis
            log("!  %s: Unvollständiger Mirror vorhanden – klone neu" % full_name)
            shutil.rmtree(mdir, ignore_errors=True)
        os.makedirs(os.path.dirname(mdir), exist_ok=True)
        log("↓  %s: Klone Mirror (alle Branches & Tags) ..." % full_name)
        proc = run_git(["clone", "--mirror", "--progress", git_url, mdir], token)
        if proc.returncode != 0:
            shutil.rmtree(mdir, ignore_errors=True)
            stderr = (proc.stderr or "").lower()
            if "not found" in stderr or "repository not found" in stderr:
                return "fail", meta, "Repo nicht erreichbar (gelöscht oder privat ohne Token?)"
            return "fail", meta, "git clone fehlgeschlagen:\n" + last_stderr(proc)

    # 2) Metadaten aus dem Mirror lesen
    try:
        branches = run_git(["-C", mdir, "for-each-ref", "refs/heads", "--format=%(refname)"], token)
        tags = run_git(["-C", mdir, "for-each-ref", "refs/tags", "--format=%(refname)"], token)
        last = run_git(["-C", mdir, "log", "-1", "--format=%cI"], token)
        meta["branches"] = len([x for x in (branches.stdout or "").splitlines() if x.strip()])
        meta["tags"] = len([x for x in (tags.stdout or "").splitlines() if x.strip()])
        meta["last_commit"] = (last.stdout or "").strip()[:10]
        meta["size_kb"] = int(subprocess.run(["du", "-sk", mdir], stdout=subprocess.PIPE,
                                             text=True).stdout.split()[0])
    except Exception as exc:
        log("!  %s: Metadaten unvollständig (%s)" % (full_name, exc))

    # 3) Preset: Wiki spiegeln
    wiki_note = ""
    if preset in ("wiki", "full"):
        wdir = mirror_path(full_name, ".wiki")
        wurl = "https://github.com/%s.wiki.git" % full_name
        if os.path.isdir(wdir) and os.path.exists(os.path.join(wdir, "HEAD")):
            proc = run_git(["-C", wdir, "remote", "update", "--prune"], token)
        else:
            if os.path.isdir(wdir):
                shutil.rmtree(wdir, ignore_errors=True)
            os.makedirs(os.path.dirname(wdir), exist_ok=True)
            proc = run_git(["clone", "--mirror", wurl, wdir], token)
        if proc.returncode == 0:
            meta["wiki"] = True
        else:
            shutil.rmtree(wdir, ignore_errors=True)
            wiki_note = "kein Wiki vorhanden"

    # 4) Preset 'full': Issues & Releases als JSON exportieren
    if preset == "full":
        target_dir = os.path.dirname(mdir)
        issues, releases = [], []
        status, data = gh_paginate("/repos/%s/issues" % full_name, token,
                                   {"state": "all"})
        if status == 200:
            issues = [i for i in data if "pull_request" not in i]  # nur echte Issues
        else:
            wiki_note = (wiki_note + "; " if wiki_note else "") + \
                "Issues-Export fehlgeschlagen (HTTP %s)" % status
        status, data = gh_paginate("/repos/%s/releases" % full_name, token)
        if status == 200:
            releases = data
        else:
            wiki_note = (wiki_note + "; " if wiki_note else "") + \
                "Releases-Export fehlgeschlagen (HTTP %s)" % status
        try:
            with open(os.path.join(target_dir, name + "-issues.json"), "w",
                      encoding="utf-8") as f:
                json.dump(issues, f, indent=2, ensure_ascii=False)
            with open(os.path.join(target_dir, name + "-releases.json"), "w",
                      encoding="utf-8") as f:
                json.dump(releases, f, indent=2, ensure_ascii=False)
            meta["issues"] = len(issues)
            meta["releases"] = len(releases)
        except OSError as exc:
            log("!  %s: JSON-Export nicht schreibbar (%s)" % (full_name, exc))

    if wiki_note:
        return "warn", meta, wiki_note
    return "ok", meta, ""

# ── Job-Verwaltung (Warteschlange + Live-Log) ──────────────────────────────
class JobState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.stop_requested = False
        self.current = None
        self.total = 0
        self.done = 0
        self.results = {"ok": 0, "warn": 0, "fail": 0}
        self.started_at = None
        self.log = deque(maxlen=2000)
        self.log_fh = None
        self.lock_fd = None

    def add_log(self, line):
        stamped = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), line)
        with self.lock:
            self.log.append(stamped)
            if self.log_fh:
                try:
                    self.log_fh.write(stamped + "\n")
                    self.log_fh.flush()
                except OSError:
                    pass

    def tail(self, n=60):
        with self.lock:
            return list(self.log)[-n:]

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "stop_requested": self.stop_requested,
                "current": self.current,
                "total": self.total,
                "done": self.done,
                "results": dict(self.results),
                "started_at": self.started_at,
            }


JOB = JobState()


def try_lock():
    """Exklusiv-Sperre, damit Web-Job und Timer-CLI sich nicht in die Quere kommen."""
    fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return None
    return fd


def token_for_repo(full_name, cfg):
    owner = full_name.partition("/")[0].lower()
    for acc in cfg.get("accounts", []):
        if (acc.get("name") or "").lower() == owner and (acc.get("token") or "").strip():
            return acc["token"].strip()
    for acc in cfg.get("accounts", []):     # Fallback: erster Token überhaupt
        if (acc.get("token") or "").strip():
            return acc["token"].strip()
    return None


def run_backup_job(repo_list, preset, cfg, source="ui"):
    """Führt die Warteschlange aus (blockiert). Voraussetzung: Lock gehalten."""
    global STATE
    JOB.running = True
    JOB.stop_requested = False
    JOB.total = len(repo_list)
    JOB.done = 0
    JOB.results = {"ok": 0, "warn": 0, "fail": 0}
    JOB.current = None
    JOB.started_at = now_iso()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = os.path.join(LOGS_DIR, "backup-%s.log" % stamp)
    try:
        JOB.log_fh = open(log_path, "a", encoding="utf-8")
    except OSError:
        JOB.log_fh = None
        log_path = None

    JOB.add_log("=== Backup gestartet (%s, Modus: %s, %d Repos) ===" %
                (source, preset, len(repo_list)))
    state_updates = {}
    try:
        for full_name in repo_list:
            if JOB.stop_requested:
                JOB.add_log("■  Stop angefordert – breche nach %d/%d Repos ab"
                            % (JOB.done, JOB.total))
                break
            JOB.current = full_name
            token = token_for_repo(full_name, cfg)
            try:
                status, meta, note = backup_single_repo(full_name, token, preset, JOB.add_log)
            except subprocess.TimeoutExpired:
                status, meta, note = "fail", {}, "Timeout nach %d s" % CLONE_TIMEOUT
            except Exception as exc:  # unvorhergesehen – volle Kette ins Log
                status, meta, note = "fail", {}, "%s: %s" % (type(exc).__name__, exc)
            JOB.results[status] += 1
            JOB.done += 1
            icon = {"ok": "✔", "warn": "⚠", "fail": "✘"}[status]
            line = "%s  %s: %s" % (icon, full_name,
                                   status if status != "ok" else "gesichert")
            if note:
                line += " (%s)" % note
            JOB.add_log(line)
            meta.update({"last_backup": now_iso(), "status": status, "note": note})
            state_updates[full_name] = meta
            with STATE_LOCK:
                STATE["repos"].update(state_updates)
                STATE["last_run"] = now_iso()
                save_state(STATE)
        JOB.add_log("=== Fertig: %d ok, %d mit Hinweisen, %d fehlerhaft ==="
                    % (JOB.results["ok"], JOB.results["warn"], JOB.results["fail"]))
    finally:
        JOB.current = None
        JOB.running = False
        if JOB.log_fh:
            JOB.log_fh.close()
            JOB.log_fh = None


def start_job(repo_list, preset, source="ui"):
    if not repo_list:
        return False, "Keine Repos ausgewählt"
    if preset not in PRESETS:
        preset = "code"
    fd = try_lock()
    if fd is None:
        return False, "Es läuft bereits ein Backup (Web-Job oder Timer)"
    t = threading.Thread(target=_job_thread, args=(repo_list, preset, fd, source),
                         daemon=True)
    t.start()
    return True, "Backup gestartet (%d Repos)" % len(repo_list)


def _job_thread(repo_list, preset, fd, source):
    JOB.lock_fd = fd
    try:
        run_backup_job(repo_list, preset, load_config(), source)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()
        JOB.lock_fd = None


def backup_all_cli():
    """Timer-/CLI-Modus: alle Konten scannen und alles (außer Ausschlussliste) sichern."""
    refresh_github_hosts()          # Timer-Lauf: GitHub-IPs frisch pinnen
    cfg = load_config()
    if not cfg["accounts"]:
        print("Keine Konten konfiguriert – nichts zu tun. Erst in der Web-UI einrichten.")
        return 0
    all_repos = []
    for acc in cfg["accounts"]:
        scanned, err = scan_account(acc)
        if err:
            print("FEHLER bei Account '%s': %s" % (acc.get("name"), err))
            continue
        skipped = set(cfg["excluded"])
        repos = [r["full_name"] for r in scanned["repos"] if r["full_name"] not in skipped]
        print("Account '%s': %d Repos (ausgeschlossen: %d)"
              % (scanned["scanned"], len(repos), len(scanned["repos"]) - len(repos)))
        all_repos.extend(repos)
    if not all_repos:
        print("Keine Repos zu sichern.")
        return 0
    fd = try_lock()
    if fd is None:
        print("Es läuft bereits ein Backup – übersprungen.")
        return 0
    try:
        run_backup_job(all_repos, cfg["preset"], cfg, source="timer")
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()
    return 1 if JOB.results["fail"] else 0

# ── Komplett-Export (Katastrophenschutz) ───────────────────────────────────
EXPORT_DIR = os.path.join(BASE_DIR, "export")
EXPORT_RE = re.compile(r"^github-backup-\d{8}-\d{6}\.tar\.gz$")


def create_export():
    """Packt alle Mirrors als eine .tar.gz nach export/ — die Datei, die man
    auf NAS/USB/den eigenen PC kopiert, falls GitHub oder der Container weg ist."""
    if JOB.running:
        return None, "Während eines Backups kein Export möglich"
    os.makedirs(EXPORT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname = "github-backup-%s.tar.gz" % stamp
    target = os.path.join(EXPORT_DIR, fname)
    try:
        proc = subprocess.run(["tar", "czf", target, "-C", BASE_DIR, "repos"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=7200)
    except subprocess.TimeoutExpired:
        return None, "Export-Timeout nach 2 h (repos zu groß? Per pct pull kopieren)"
    if proc.returncode != 0:
        return None, "tar fehlgeschlagen: %s" % (proc.stderr or "").strip()[-300:]
    size_kb = int(subprocess.run(["du", "-sk", target], stdout=subprocess.PIPE,
                                 text=True).stdout.split()[0])
    entry = {"name": fname, "file": target, "size_kb": size_kb, "created": now_iso()}
    with STATE_LOCK:
        STATE.setdefault("exports", []).append(entry)
        STATE["exports"] = STATE["exports"][-50:]
        save_state(STATE)
    return entry, None


def serve_export(name):
    """Streamt eine Export-Datei (nur aus export/, nur gültige Namen)."""
    safe = os.path.basename(name)
    if not EXPORT_RE.match(safe):
        return None, None
    path = os.path.join(EXPORT_DIR, safe)
    if not os.path.isfile(path):
        return None, None
    size = os.path.getsize(path)
    fh = open(path, "rb")
    return fh, size


# ── Snapshots ──────────────────────────────────────────────────────────────
def create_snapshot(full_name):
    mdir = mirror_path(full_name)
    if not (os.path.isdir(mdir) and os.path.exists(os.path.join(mdir, "HEAD"))):
        return None, "Kein Backup von %s vorhanden" % full_name
    owner = full_name.partition("/")[0]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = full_name.partition("/")[2]
    sdir = os.path.join(SNAPSHOTS_DIR, owner)
    os.makedirs(sdir, exist_ok=True)
    sfile = os.path.join(sdir, "%s-%s.bundle" % (name, stamp))
    proc = run_git(["-C", mdir, "bundle", "create", sfile, "--all"], None)
    if proc.returncode != 0:
        return None, "git bundle fehlgeschlagen:\n" + last_stderr(proc)
    entry = {"repo": full_name, "file": sfile,
             "size_kb": int(subprocess.run(["du", "-sk", sfile], stdout=subprocess.PIPE,
                                           text=True).stdout.split()[0]),
             "created": now_iso()}
    with STATE_LOCK:
        STATE.setdefault("snapshots", []).append(entry)
        STATE["snapshots"] = STATE["snapshots"][-500:]
        save_state(STATE)
    return entry, None

# ── Timer-Zeit ins systemd-Unit-File schreiben (best effort) ───────────────
def apply_timer_hour(hour):
    if not (os.path.exists(TIMER_UNIT_PATH) and os.access(TIMER_UNIT_PATH, os.W_OK)):
        return False
    try:
        with open(TIMER_UNIT_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        new_line = "OnCalendar=*-*-* %02d:00:00" % hour
        new_content, n = re.subn(r"^OnCalendar=.*$", new_line, content,
                                 count=1, flags=re.MULTILINE)
        if n == 0:
            return False
        with open(TIMER_UNIT_PATH, "w", encoding="utf-8") as f:
            f.write(new_content)
        subprocess.run(["systemctl", "daemon-reload"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False

# ── HTTP-Server ────────────────────────────────────────────────────────────
STATE = load_state()

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # Journal nicht mit Access-Logs fluten
        if "/api/" not in (args[0] if args else ""):
            return

    # ── Antworten ──
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, rel_path):
        path = os.path.normpath(os.path.join(STATIC_DIR, rel_path.lstrip("/")))
        if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
            self._send_json({"error": "nicht gefunden"}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # ── Routing ──
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_file("index.html")
        elif self.path == "/api/health":
            self._send_json({"ok": True, "version": APP_VERSION})
        elif self.path == "/api/state":
            self._send_json(self.api_state())
        elif self.path.startswith("/api/export/download?"):
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query)
            name = (query.get("name") or [""])[0]
            fh, size = serve_export(name)
            if fh is None:
                self._send_json({"error": "Export nicht gefunden"}, 404)
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition",
                                 "attachment; filename=%s" % os.path.basename(name))
                self.end_headers()
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            finally:
                fh.close()
        elif self.path.startswith("/static/"):
            self._send_file(self.path[len("/static/"):])
        else:
            self._send_json({"error": "nicht gefunden"}, 404)

    def do_POST(self):
        body = self._read_body()
        if self.path == "/api/config":
            self._send_json(self.api_config(body))
        elif self.path == "/api/scan":
            self._send_json(self.api_scan(body))
        elif self.path == "/api/backup/start":
            self._send_json(self.api_backup_start(body))
        elif self.path == "/api/backup/stop":
            JOB.stop_requested = True
            self._send_json({"ok": True, "message": "Stop angefordert – "
                             "aktuelles Repo läuft zu Ende"})
        elif self.path == "/api/snapshot":
            self._send_json(self.api_snapshot(body))
        elif self.path == "/api/excluded":
            self._send_json(self.api_excluded(body))
        elif self.path == "/api/export":
            self._send_json(self.api_export_create())
        else:
            self._send_json({"error": "nicht gefunden"}, 404)

    # ── API-Logik ──
    def api_state(self):
        cfg = load_config()
        masked = dict(cfg)
        masked["accounts"] = [dict(a, token=mask_token(a.get("token", "")))
                              for a in cfg.get("accounts", [])]
        with STATE_LOCK:
            state_copy = json.loads(json.dumps(STATE))  # tiefe Kopie für sichere Serialisierung
        return {
            "version": APP_VERSION,
            "config": masked,
            "job": JOB.snapshot(),
            "log": JOB.tail(60),
            "state": state_copy,
            "data_dir": BASE_DIR,
            "timer_unit_managed": os.access(TIMER_UNIT_PATH, os.W_OK) if os.path.exists(TIMER_UNIT_PATH) else False,
        }

    def api_config(self, body):
        cfg = load_config()
        accounts = body.get("accounts")
        if isinstance(accounts, list):
            cleaned = []
            for i, acc in enumerate(accounts):
                name = str(acc.get("name", "")).strip()
                if not name:
                    continue
                token = acc.get("token")
                old_token = ""
                if i < len(cfg["accounts"]):
                    old_token = cfg["accounts"][i].get("token", "")
                # Maskierten Platzhalter oder fehlenden Wert → alten Token behalten
                if token is None or (isinstance(token, str) and
                                     (token == TOKEN_MASK or token.startswith(TOKEN_MASK))):
                    token = old_token
                cleaned.append({
                    "name": name,
                    "token": str(token or "").strip(),
                    "include_forks": bool(acc.get("include_forks")),
                })
            cfg["accounts"] = cleaned
        if body.get("preset") in PRESETS:
            cfg["preset"] = body["preset"]
        if "timer_hour" in body:
            try:
                hour = max(0, min(23, int(body["timer_hour"])))
                if hour != cfg["timer_hour"]:
                    cfg["timer_hour"] = hour
                    apply_timer_hour(hour)
            except (TypeError, ValueError):
                pass
        save_config(cfg)
        return {"ok": True, "message": "Konfiguration gespeichert"}

    def api_scan(self, body):
        refresh_github_hosts()      # vor jedem Scan: IP-Pinnung auffrischen
        cfg = load_config()
        try:
            idx = int(body.get("account", 0))
            acc = cfg["accounts"][idx]
        except (TypeError, ValueError, IndexError):
            return {"ok": False, "error": "Unbekanntes Konto"}
        scanned, err = scan_account(acc)
        if err:
            return {"ok": False, "error": err}
        excluded = set(cfg.get("excluded", []))
        for r in scanned["repos"]:
            r["excluded"] = r["full_name"] in excluded
        return {"ok": True, "scanned": scanned["scanned"],
                "total": len(scanned["repos"]), "repos": scanned["repos"]}

    def api_backup_start(self, body):
        repos = body.get("repos")
        if not isinstance(repos, list):
            return {"ok": False, "error": "repos-Liste fehlt"}
        repos = [str(r) for r in repos if str(r).strip()]
        preset = body.get("preset") or load_config()["preset"]
        ok, msg = start_job(repos, preset, source="Web-UI")
        return {"ok": ok, "message": msg}

    def api_snapshot(self, body):
        full_name = str(body.get("repo", "")).strip()
        if not full_name:
            return {"ok": False, "error": "repo fehlt"}
        if JOB.running:
            return {"ok": False, "error": "Während eines Backups kein Snapshot möglich"}
        entry, err = create_snapshot(full_name)
        if err:
            return {"ok": False, "error": err}
        return {"ok": True, "snapshot": entry}

    def api_export_create(self):
        entry, err = create_export()
        if err:
            return {"ok": False, "error": err}
        return {"ok": True, "export": entry}

    def api_excluded(self, body):
        cfg = load_config()
        full_name = str(body.get("repo", "")).strip()
        if not full_name:
            return {"ok": False, "error": "repo fehlt"}
        excluded = set(cfg.get("excluded", []))
        if body.get("excluded"):
            excluded.add(full_name)
        else:
            excluded.discard(full_name)
        cfg["excluded"] = sorted(excluded)
        save_config(cfg)
        return {"ok": True, "excluded": cfg["excluded"]}


def serve():
    ensure_dirs()
    ensure_askpass()
    refresh_github_hosts()          # GitHub-IPs pinnen → unabhängig vom lokalen DNS
    global STATE
    STATE = load_state()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print("GitHub-Backup-Manager %s → http://0.0.0.0:%d (Daten: %s)"
          % (APP_VERSION, PORT, BASE_DIR), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def status():
    job = JOB.snapshot()
    state = load_state()
    print(json.dumps({
        "version": APP_VERSION,
        "running": job["running"],
        "repos_backed_up": len(state.get("repos", {})),
        "last_run": state.get("last_run"),
        "snapshots": len(state.get("snapshots", [])),
    }, ensure_ascii=False))
    return 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "serve"
    if cmd == "serve":
        serve()
        return 0
    if cmd == "backup-all":
        return backup_all_cli()
    if cmd == "status":
        return status()
    print("Unbekannter Befehl: %s (nutze serve | backup-all | status)" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main())
