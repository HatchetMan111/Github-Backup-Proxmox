# 📦 GitHub-Backup-Manager – Proxmox LXC

Lokale Web-App für Proxmox VE, die **alle Repos eines GitHub-Accounts als 1:1-Mirror herunterlädt** – komplett lokal, keine Cloud, kein externer Dienst. Nachgebaut im Stil von [mmahingu.com/github-backup-manager](https://mmahingu.com/github-backup-manager/) (dort eine Windows-App) als leichtgewichtige LXC-Appliance: **Python 3 stdlib, keine pip-Abhängigkeiten**.

## ✨ Features

- **Mirror-Backup** aller Branches & Tags (`git clone --mirror`) nach `repos/<owner>/<name>.git`
- **3 Presets:** ① nur Code · ② Code + Wiki · ③ Komplett (+ Issues & Releases als JSON-Export)
- **Private Repos** via PAT (BYOK) – der Token wird nur lokal in `config.json` (0600) gespeichert, nie ins Log geschrieben und nie in die `.git/config` der Mirrors (git holt ihn über `GIT_ASKPASS`)
- **Web-UI** mit Konto-Verwaltung, Repo-Scan (inkl. Auswahl & dauerhafter Ausschlussliste), **Warteschlange mit Live-Log** und Stop-Button
- **Täglicher Auto-Backup-Timer** (systemd, Uhrzeit in der UI einstellbar)
- **Snapshots** als `git bundle` (eine Datei pro Snapshot, ideal für externe Medien)
- **Übersicht** mit Größe, Branch-/Tag-Count, letztem Commit und Backup-Datum

## 🚀 Installation (Einzeiler auf dem Proxmox-Host als root)

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/GitHub-Backup-Proxmox/main/install/github-backup.sh)"
```

Am Ende zeigt der Installer die finale URL (`http://<CT-IP>:4242`), das generierte Root-Passwort und alle weiteren Befehle.

### Optionen

Optionen einfach **hinter dem schließenden Anführungszeichen** anhängen:

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/GitHub-Backup-Proxmox/main/install/github-backup.sh)" --id 210 --rootfs 32 --port 4242
```

| Option | Default | Bedeutung |
|---|---|---|
| `--id ID` | nächste freie ab 200 | CT-ID |
| `--hostname NAME` | `github-backup` | Container-Hostname |
| `--storage NAME` | `local-lvm` | Storage für rootfs |
| `--rootfs GB` | `16` | **Disk – bei großen Accounts erhöhen, hier landen alle Mirrors!** |
| `--cores N` / `--memory MB` | 2 / 2048 | Ressourcen |
| `--bridge BR` | `vmbr0` | Bridge |
| `--ip CIDR` / `--gateway GW` | dhcp | statische IP |
| `--port PORT` | `4242` | Web-UI-Port |
| `--password PW` | generiert | Root-Passwort |
| `--force` | – | existierenden Container löschen & neu anlegen (Backups weg!) |
| `--yes` / `-y` | – | Rückfragen überspringen |

### Update / Status / Deinstallation

```bash
# App aktualisieren (config.json, repos/ und snapshots/ bleiben erhalten)
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/GitHub-Backup-Proxmox/main/install/github-backup.sh)" --update

# Status
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/GitHub-Backup-Proxmox/main/install/github-backup.sh)" --status

# Komplett entfernen (Container inkl. aller Backups löschen!)
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/GitHub-Backup-Proxmox/main/install/github-backup.sh)" --uninstall
```

## 🔄 Restore

Ein Mirror ist direkt nutzbar (alle Branches & Tags, bare Repo):

```bash
# Komplettes Repo wiederherstellen
git clone /pfad/im/container/repos/<owner>/<repo>.git

# Aus einem Snapshot-Bundle
git clone /pfad/im/container/snapshots/<owner>/<repo>-<datum>.bundle
```

Außerhalb des Containers: `pct pull <ctid> /opt/github-backup/repos/... ./` oder einen Mountpoint für `/opt/github-backup` setzen (`pct set <ctid> -mp0 <storage>:0,mp=/opt/github-backup`) – **vor** der Installation angelegen, dann überlebt der Backup-Bestand sogar Container-Recreates.

## ❓ FAQ – wo finde ich was und wie richte ich es ein?

*(Diese FAQ gibt es auch direkt in der Web-UI: Button **❓ Hilfe** oben rechts, bzw. der „was ist das / woher?“-Link neben dem PAT-Feld.)*

### 🔑 PAT: Was ist das, wo bekomme ich es, wie richte ich es ein?

**PAT** = „Personal Access Token“ – ein Passwort-Ersatz für die GitHub-API.
- **Ohne PAT:** nur öffentliche Repos, max. 60 Anfragen/Stunde
- **Mit PAT:** auch private Repos + 5.000 Anfragen/Stunde

**Schritt für Schritt (Classic-Token, empfohlen):**

1. Auf [github.com](https://github.com) einloggen → oben rechts Profilbild → **Settings**
2. Ganz unten links: **Developer settings**
3. **Personal access tokens** → **Tokens (classic)** → **Generate new token (classic)**
4. Notiz z. B. „GitHub-Backup“, Ablaufdatum wählen
5. Haken bei **repo** (alle Unterpunkte) – das reicht
6. „Generate token“ → den angezeigten Token (`ghp_…`) **sofort kopieren** – er wird nur einmal angezeigt!
7. In der Web-UI ins **PAT-Feld** einfügen → **💾 Speichern**

Der Token bleibt ausschließlich lokal im Container (`/opt/github-backup/config.json`, Rechte 0600), wird nie geloggt und nie an Dritte gesendet. Widerrufen jederzeit unter [github.com/settings/tokens](https://github.com/settings/tokens).

*Alternative „Fine-grained tokens“:* Repository access → *All repositories*; Permissions → *Contents: Read-only* + *Metadata: Read-only* (für Preset ③ zusätzlich *Issues: Read-only*).

### 👤 Konto einrichten

**Konto-Name** = GitHub-Benutzername. Ohne PAT: es werden die öffentlichen Repos dieses Namens gesichert. Mit PAT: automatisch die Repos des Token-Inhabers (inkl. privater). Mehrere Konten einfach per „+ Konto hinzufügen“.

### 💾 Wo landen die Backups und wie restore ich?

Im Container unter `/opt/github-backup/repos/<owner>/<repo>.git` (Mirror, alle Branches & Tags):

```bash
git clone /opt/github-backup/repos/<owner>/<repo>.git    # im Container
git clone /pfad/zu/<repo>-<datum>.bundle                 # aus einem Snapshot
pct pull <CT-ID> /opt/github-backup/repos/... ./         # auf den Proxmox-Host holen
```

### 🚫 Repos dauerhaft überspringen

Beim Scan die **🚫-Spalte** anhaken → dauerhafte Ausschlussliste, gilt auch für den täglichen Timer.

### ⏰ Täglicher Auto-Backup

`ghbackup.timer` sichert täglich um die in der UI eingestellte Stunde alle Konten (außer Ausschlussliste). `Persistent=true` holt verpasste Läufe nach Reboot nach. Timer-Status: `pct exec <CT-ID> -- systemctl list-timers ghbackup*`

## 🧰 Fehlersuche

- Installer bricht ab → er gibt die **komplette Fehlerkette** aus (Zeile, Befehl, Exit-Code, Container-Logs) und speichert alles in `/tmp/github-backup-installer-*.log`
- Vollständiger Trace: Einzeiler mit `--debug` erneut ausführen
- Service-Logs im Container: `pct exec <ctid> -- journalctl -u ghbackup -e`
- Setup-Log im Container: `/var/log/ghbackup-setup.log`

## 📁 Struktur

```
install/github-backup.sh    # Einzeiler-Installer (Community-Scripts-Stil)
app/server.py               # Web-UI + Backup-Engine (nur Python-Stdlib)
app/cli.py                  # Einstiegspunkt für den täglichen Timer-Job
app/static/index.html       # Web-UI
app/ghbackup.service        # systemd-Unit (Restart=always, onboot via CT)
app/ghbackup-cli.service    # Unit für den Timer-Lauf
app/ghbackup.timer          # täglicher Auto-Backup
```

## ⚙️ Im Container installierte Komponenten

- `ghbackup.service` – Web-UI auf Port 4242 (bind 0.0.0.0, `Restart=always`, `After=network-online.target`)
- `ghbackup.timer` + `ghbackup-cli.service` – täglicher Backup-Lauf aller Konten (außer Ausschlussliste), `Persistent=true` (holt verpasste Läufe nach Reboot nach)
- Daten unter `/opt/github-backup/`: `config.json` (0600, enthält PAT), `repos/`, `snapshots/`, `logs/`, `state.json`
