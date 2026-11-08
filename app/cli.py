#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub-Backup-Manager – CLI-Wrapper für den täglichen systemd-Timer.

Läuft als `ghbackup-cli.service` und aktualisiert alle Backups aller
konfigurierten Konten (alles außer der Ausschlussliste). Die Logik lebt in
server.py – hier steht nur der Einstiegspunkt, damit Timer und Web-UI
exakt dieselbe Engine nutzen.
"""

import sys

from server import main

if __name__ == "__main__":
    sys.exit(main(["backup-all"] + sys.argv[1:]))
