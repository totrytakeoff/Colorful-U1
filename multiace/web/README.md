# multiACE Web

Web-Frontend für multiACE - gemeinsame Foundation für Browser-UI und
spätere Mobile-App. Backend ist eine schlanke FastAPI-Schicht; Frontend
ist Vue 3 vom CDN, kein Build-Schritt nötig.

## Architektur

```
Browser / Mobile-App
        │
        ▼
   nginx :80/:443  ──┬──► /              (Mainsail/Fluidd)
                    ├──► /screen/...    (paxx fb-http :8092)
                    ├──► /server/...    (Moonraker :7125)
                    └──► /multiace/     (FastAPI :7126, dieser Service)
```

Auth läuft komplett über `auth_request /auth_check` → Moonraker
`/access/user`. Der FastAPI-Service vertraut allen Requests, die durch
nginx zu ihm durchkommen - keine eigene User-/Token-Logik.

## API-Endpoints

| Methode | Pfad                  | Zweck                                      |
|---------|-----------------------|--------------------------------------------|
| GET     | `/multiace/api/health`| Liveness / Versionsstempel                 |
| GET     | `/multiace/api/version`| Build-Info (web, moonraker_url, cfg_path) |
| GET     | `/multiace/api/aces`  | ACE- + Slot-Status (live von Moonraker)    |
| POST    | `/multiace/api/macro` | Führt G-Code-Macro aus (z. B. `A_LOAD`)    |
| GET     | `/multiace/api/config`| Liest `ace.cfg`                            |
| PUT     | `/multiace/api/config`| Schreibt `ace.cfg` (Backup `.bak`, optional Klipper-Restart) |
| POST    | `/multiace/api/preflight`| Analysiert G-Code, matched Slicer-Farben gegen live ACE-Slots |
| POST    | `/multiace/api/preflight/print`| Postprocess + Upload + Start über die aktuelle Printer-Anordnung |
| WS      | `/multiace/ws`        | Live-Push der ACE-States (Intervall ~1 s)  |

Display-Mirror wird **nicht** durch FastAPI proxied - das Frontend redet
direkt mit `/screen/snapshot` und `/screen/touch` (paxx fb-http). Mobile
Apps machen es analog.

## G-Code Preflight

Die Upload-Schaltfläche im Header führt mehrfarbige G-Code-Dateien zuerst
durch `/multiace/api/preflight`. Für den Single-Toolhead-ACE-MVP ist nur
**Printer layout (as loaded)** druckbar. Optimize- und Layer-Layouts bleiben
bewusst deaktiviert, bis native+ACE- und Multi-Head-Koordination stabil sind.

Die Mapping-Tabelle zeigt die echte Laufzeitroute:

```text
ACE <ace> Slot <slot> -> T<physical head> <- Slicer T<n>
```

Für ein Setup `ACE 0 -> HEAD 3` müssen alle vier ACE-Slots `target_head=3`
melden. Beim Drucken schreibt der Postprozessor explizite Befehle wie
`ACE_SWAP_HEAD HEAD=3 ACE=0 SLOT=2` in die hochgeladene Datei. Dadurch können
printer-seitige Parameter wie `swap_retract_length` geändert und nach einem
Klipper-Restart mit derselben G-Code-Datei erneut getestet werden; erneutes
Slicen ist dafür nicht erforderlich.

## Verzeichnislayout

```
multiace/web/
  backend/                FastAPI service
    main.py
    requirements.txt
  frontend/               Statische SPA (Vue 3 vom CDN, kein npm)
    index.html
    app.js
    style.css
    manifest.webmanifest
    icon.svg
  deploy/
    S98multiace-web                init-Skript (busybox)
    multiace-web.nginx.conf        nginx-Location-Block
  README.md
```

## Installation

`bash install_multiace.sh --install-web` legt alles ab und startet den
Service unmittelbar. Beim Umweg über `--install-web` werden zusätzlich:

- `backend/` und `frontend/` nach `/home/lava/multiace_web/` kopiert
- `requirements.txt` per `pip install --user` für `lava` installiert
- `deploy/multiace-web.nginx.conf` nach `/etc/nginx/fluidd.d/`
- `deploy/S98multiace-web` nach `/etc/init.d/` (`chmod +x`)
- nginx + multiace-web manuell gestartet
- nach `/multiace/` im Browser ist alles erreichbar

### Boot-Zeit-Caveat

Snapmaker-U1-rcS expandiert `/etc/init.d/S??*` **vor** dem
overlay-mount. Ein post-Install-Skript landet im Overlay und ist beim
nächsten Boot für rcS unsichtbar. Workarounds:

1. **Manueller Re-Start nach Reboot** - `S98multiace-web start`
2. **Firmware-Build-Integration** - Skript in den paxx-Overlay-Build
   übernehmen (PR upstream)
3. **Spawn aus Klipper** (analog `multiace_v2d.py`) - wäre ein
   zukünftiges Refactor.

Für v1 dieser Foundation reicht Variante 1: `install_multiace.sh
--install-web` startet den Service direkt; nach jedem Reboot manuell
neustarten oder Firmware-Build erweitern.

## Entwicklung

Backend lokal starten (außerhalb des Druckers):

```bash
cd multiace/web/backend
pip install -r requirements.txt
MOONRAKER_URL=http://printer.local MULTIACE_CFG_PATH=/tmp/test-ace.cfg \
  python -m uvicorn main:app --host 0.0.0.0 --port 7126 --reload
```

Frontend serven (statisch, ohne Build):

```bash
cd multiace/web/frontend
python3 -m http.server 8000
```

Dann `http://localhost:8000` aufrufen - passe ggf. die `API`-Konstante
in `app.js` an, wenn Backend auf einem anderen Origin läuft.

## Mobile-App-Pfad

Schritt 1 (heute): PWA-installierbar - `manifest.webmanifest` aktiviert
"Zum Startbildschirm hinzufügen".

Schritt 2 (später): Native App in React Native oder Flutter konsumiert
exakt dieselben Endpoints. Auth läuft über
`access/oneshot_token` → Moonraker. Keine zusätzlichen
Backend-Endpoints nötig - die Foundation hier ist genau das, was die
mobile App braucht.
