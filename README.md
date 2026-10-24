# ChurchTools & WorshipTools Sync Gateway

[![Docker](https://img.shields.io/badge/Docker-Ready-blue.svg)](https://www.docker.com/)
[![Python](https://img.shields.io/badge/Python-3.13-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Ein eigenständiger, produktionsreifer Docker-Container für die **vollautomatische, bidirektionale Synchronisation** zwischen **ChurchTools** und **WorshipTools (Presenter & Planning)** sowie ein API-Gateway für Folien- und Medien-Uploads.

Entwickelt für Kirchengemeinden, die ChurchTools für die Gemeinde- und Gottesdienstplanung nutzen und WorshipTools Presenter für die Präsentation im Gottesdienst einsetzen.

---

## Inhaltsverzeichnis
- [1. Was macht dieser Service?](#1-was-macht-dieser-service)
- [2. Synchronisations-Features](#2-synchronisations-features)
- [3. Schnellstart mit Docker](#3-schnellstart-mit-docker)
- [4. Konfiguration](#4-konfiguration)
  - [A. Umgebungsvariablen (.env)](#a-umgebungsvariablen-env)
  - [B. Mapping-Konfiguration (config.yaml)](#b-mapping-konfiguration-configyaml)
- [5. API-Referenz](#5-api-referenz)
  - [Service-Abfragen](#service-abfragen-für-automationen)
  - [Medien- & Folien-Uploads](#medien--und-folien-uploads)
  - [Synchronisations-Steuerung](#synchronisations-steuerung)
- [6. Linux Headless Folien-Rendering](#6-linux-headless-folien-rendering)
- [7. Externe Automations-Integrationen (n8n / Make / Webhooks)](#7-externe-automations-integrationen-n8n--make--webhooks)

---

## 1. Was macht dieser Service?

Die Software läuft im Hintergrund (z. B. auf einem Linux-Server via Docker) und hält beide Systeme synchron:

```
┌────────────────────────┐                    ┌────────────────────────┐
│      ChurchTools       │◄─── Bidirektional ─►│      WorshipTools      │
│  (Termine, Agenden,    │     Synchronisation │  (Presenter & Planning │
│   Dienste, Liedarchiv) │                     │   Firestore & Storage) │
└───────────▲────────────┘                    └───────────▲────────────┘
            │                                             │
            └─────────────────┬───────────────────────────┘
                              │
               ┌──────────────┴──────────────┐
               │    worshiptools-api         │
               │   (REST API & Automation)   │
               └─────────────────────────────┘
```

1. **Hintergrund-Synchronisation:** Gleicht in regelmäßigen Abständen (Standard: alle 2 Minuten) Termine, Lied-Metadaten, Ablaufpläne (Cues ↔ Agendapunkte) und Team-Zusagen ab.
2. **Folien- & Medien-Gateway:** Ermöglicht den direkten Upload von Grafiken (JPG, PNG) und PowerPoint-Folien (`.pptx`) in die Presenter-App – inklusive automatischer Konvertierung der Einzelfolien auf dem Server.
3. **Automations-Endpunkte:** Bietet Schnittstellen für externe Workflows (z. B. n8n, Make, Webhooks) mit getrennten Abfrage-Linien für ChurchTools und WorshipTools.

---

## 2. Synchronisations-Features

### Gottesdienste & Events
- **Same-Day Matching:** Gleicht Termine am selben Datum zuverlässig ab.
- **Konfliktlösung (Last-Writer-Wins):** Der Zeitstempel des letzten Autors entscheidet. Bei zeitgleichen Änderungen (innerhalb von 60 Sekunden) hat ChurchTools Vorrang.
- **Schutz von Sammelordnern:** Mehrtägige Termin-Container in WorshipTools (z. B. Sammlungen mit mehr als 3 Terminen) werden geschützt und niemals versehentlich in ChurchTools zerlegt oder dupliziert.
- **Feld-Mapping:** 
  - ChurchTools Termin-Titel ↔ WorshipTools Service-Typ
  - ChurchTools Untertitel / Thema ↔ WorshipTools Service-Name

### Lieder & Arrangements
- Synchronisiert Titel, Autoren, CCLI-Nummer, Tonart und Tempo (BPM).
- In WorshipTools erstellte Lieder werden in ChurchTools unter der Kategorie *„Verschiedenes“* mit Standard-Arrangement automatisch angelegt.

### Abläufe / Agenden
- WorshipTools Cues ↔ ChurchTools Event-Agenda.
- **Überschriften-Erhalt:** Gruppen wie *Werbung* oder *Main* werden als ChurchTools `header`-Elemente abgebildet.
- Lieder werden mit den passenden Arrangements verknüpft, Predigt und Lesungen als Text-Elemente synchronisiert.

### Team / Dienste
- Weist Dienstzusagen aus ChurchTools automatisch den Rollen in WorshipTools zu (z. B. *Klavier* ➔ *Piano*, *Ton* ➔ *Tontechnik* gemäß `config.yaml`).

---

## 3. Schnellstart mit Docker

### 1. Repository klonen
```bash
git clone https://github.com/deine-gemeinde/worshiptools-api.git
cd worshiptools-api
```

### 2. Konfigurationsdateien anlegen
```bash
cp .env.example .env
cp config.example.yaml config.yaml
```

Passe die Werte in `.env` und `config.yaml` mit deinen Zugangsdaten an.

### 3. Container starten
```bash
docker compose up -d --build
```

### 4. Logs prüfen
```bash
docker compose logs -f
```

---

## 4. Konfiguration

### A. Umgebungsvariablen (`.env`)

Kopiere `.env.example` nach `.env`:

```ini
# ChurchTools Konfiguration
CT_URL=https://deine-gemeinde.church.tools
CT_TOKEN=dein_churchtools_login_token

# WorshipTools Konfiguration
WORSHIPTOOLS_EMAIL=admin@deine-gemeinde.de
WORSHIPTOOLS_PASSWORD=dein_worshiptools_passwort
WORSHIPTOOLS_ACCOUNT_ID=deine-worshiptools-account-uuid

# API-Sicherheit (Optional)
# Wenn gesetzt, müssen alle API-Aufrufe mit "Authorization: Bearer <KEY>" authentifiziert werden.
SYNC_API_KEY=

# Server & Speicher
PORT=5000
SYNC_STATE_DB=/data/sync_state.sqlite
CONFIG_PATH=config.yaml
```

> **Tipp zum ChurchTools-Token:** Du findest deinen Login-Token in ChurchTools unter deinem Benutzerprofil ➔ Einstellungen ➔ Berechtigungen / API-Token.

---

### B. Mapping-Konfiguration (`config.yaml`)

In der `config.yaml` legst du fest, welcher Kalender synchronisiert wird und wie Rollen und Gottesdiensttypen gemappt werden:

```yaml
churchtools:
  calendar: "Gottesdienst"      # Name des Ziel-Kalenders in ChurchTools
  sync_days_ahead: 60           # Synchronisations-Zeitraum in Tagen
  sync_timezone: Europe/Berlin

# Zuordnung der Service-Typen zwischen ChurchTools und WorshipTools
# "ChurchTools Event-Titel": "WorshipTools Service-Typ"
service_types:
  "Gottesdienst": "Gottesdienst"
  "Abendgottesdienst": "Abendgottesdienst"
  "Familiengottesdienst": "Familiengottesdienst"
  "Jugendgottesdienst": "Jugendgottesdienst"

default_template: "Gottesdienst"

sync:
  interval_seconds: 120        # Hintergrund-Sync alle 2 Minuten
  tie_window_seconds: 60       # Zeitfenster bei gleichzeitigen Änderungen
  delete_mode: "off"           # "off" (empfohlen, sicher), "ct_only", "both"
  max_deletes_per_run: 3
  missing_runs_before_delete: 2

# Zuordnung von Diensten aus ChurchTools zu Rollen in WorshipTools
# "ChurchTools Dienst": "WorshipTools Rolle"
role_map:
  "Predigt": "Prediger"
  "Moderation": "Moderation"
  "Leitung": "Leitung"
  "Ton": "Tontechnik"
  "Licht": "Lichttechnik"
  "Video": "Stream"
  "Klavier": "Piano"
  "Orgel": "Piano"
  "Band": "Musik allgemein"

people_map: {}
```

---

## 5. API-Referenz

### Service-Abfragen (für Automationen)

#### 1. `GET /api/churchtools/services`
Liefert anstehende Gottesdienste aus **ChurchTools**.

- **Query-Parameter:**
  - `?filter_subtitle=1`: Liefert nur Termine mit eingetragenem Thema/Untertitel.
  - `?all=1` bzw. `?force=1`: Umgeht den internen Gesehen-Filter (liefert alle Termine).
  - `?days=60`: Betrachteter Zeitraum in Tagen (Standard: 60).
- **Beispiel-Response:**
  ```json
  {
    "success": true,
    "source": "churchtools",
    "count": 1,
    "services": [
      {
        "id": 56,
        "appointment_id": 321170,
        "type": "Gottesdienst",
        "title": "Gottesdienst",
        "subtitle": "Ein Zeichen der Hoffnung",
        "name": "Ein Zeichen der Hoffnung",
        "has_subtitle": true,
        "date": ["2026-10-25T09:00:00Z"]
      }
    ]
  }
  ```

#### 2. `GET /api/worshiptools/services`
Liefert anstehende Gottesdienste aus **WorshipTools** (WorshipTools-Linie).
- Gleiche Datenstruktur wie die ChurchTools-Linie mit `source: "worshiptools"`.
- Löst Service-Typ-IDs automatisch in Klarnamen auf und filtert Multi-Date Container heraus.

#### 3. `GET /api/new-services`
Kombinierter Endpunkt. Über den Parameter `?source=churchtools` (Standard) oder `?source=worshiptools` kann die gewünschte Quelle gewählt werden.

---

### Medien- und Folien-Uploads

#### 1. `POST /api/upload` (Datei-Upload)
Lädt eine Datei direkt in WorshipTools hoch. Erkennt den Dateityp automatisch:
- **Präsentationen (`.pptx`, `.ppt`):** Werden automatisch in Folienbilder zerlegt und in die Presenter-Collection `slides` eingetragen.
- **Grafiken (`.jpg`, `.jpeg`, `.png`):** Werden mit generierter Vorschau in die Mediathek hochgeladen.
- **Sonstige Dateien:** Werden im Account-Dateispeicher abgelegt.

```bash
curl -X POST http://localhost:5000/api/upload \
  -H "Authorization: Bearer <SYNC_API_KEY>" \
  -F "file=@Praesentation.pptx"
```

#### 2. `POST /api/upload-urls` (Batch-URL-Upload)
Lädt Dateien direkt von URLs herunter und importiert sie in WorshipTools. Ideal für Webhooks und Cloud-Exporte:

```bash
curl -X POST http://localhost:5000/api/upload-urls \
  -H "Authorization: Bearer <SYNC_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Gottesdienst 25.10.2026",
    "files": [
      { "name": "Title_Folie.png", "url": "https://mein-speicher.de/title.png" },
      { "name": "Werbung.pptx", "url": "https://mein-speicher.de/werbung.pptx" }
    ]
  }'
```

---

### Synchronisations-Steuerung

- **`POST /api/sync`**: Löst sofort einen manuellen Synchronisationslauf aus (`?dry_run=1` für Trockenlauf).
- **`GET /api/sync/status`**: Zeigt den Status und das Ergebnis des letzten Laufs an.

---

## 6. Linux Headless Folien-Rendering

WorshipTools Presenter benötigt für jede PowerPoint-Präsentation hochauflösende Einzelbilder (`slide_id.jpg`) und Thumbnails (`slide_id_thumbnail.jpg`).

Im Docker-Container ist dieser Prozess vollständig vorinstalliert:
- **LibreOffice Impress (`soffice`)** und **`poppler-utils` (`pdftoppm`)** rendern die Präsentationen headless auf Linux in gestochen scharfe JPEGs (150 DPI).
- Standard-Schriftarten (`fonts-dejavu-core`, `fonts-liberation`) sorgen für sauberes Schriftbild.
- Die API registriert die Folien mit dem aktuellen Presenter 2026 Schema (`type: "slides"`) und internen `presenter://`-URIs.

---

## 7. Externe Automations-Integrationen (n8n / Make / Webhooks)

Über die REST-API lässt sich der Dienst nahtlos in Automationsplattformen wie [n8n](https://n8n.io/), Make oder eigene Skripte einbinden:
- **Gottesdienst-Abfrage:** Periodischer Abruf von GET /api/churchtools/services oder GET /api/worshiptools/services zur Weiterverarbeitung anstehender Termine.
- **Folien- & Medien-Upload:** Übergabe von generierten oder exportierten Dateien per POST /api/upload-urls direkt an WorshipTools.

---

## Lizenz

Veröffentlicht unter der [MIT-Lizenz](LICENSE). Kann von jeder Gemeinde frei verwendet und angepasst werden.
