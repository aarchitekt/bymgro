# bymgro

Eine sehr einfache, mobile-first Gym-Trainings-App: ein Workout gerade jetzt
durchziehen (ein Push/Pull-Split), dabei Gewichte & Wiederholungen pro Satz
loggen, hinterher den Fortschritt über die Zeit sehen. Dazu ein eingebauter
Pausen-Timer im Stil einer mechanischen Eieruhr (aufziehen, loslassen, läuft
von selbst ab) und ein Profil mit Körpermaßen.

## Was drinsteckt

- `backend/main.py` — FastAPI-Server, alle `/api/...`-Routen.
- `backend/storage.py` — SQLite (eine einzige Datei `bymgro.db`, entsteht
  automatisch beim ersten Start). Beim allerersten Start wird die Datenbank
  aus `backend/seed_data.json` befüllt — das ist der aus deinem bisherigen
  Trainingsplan (Excel) exportierte Verlauf, damit die App von Anfang an
  deine echten Gewichte/Historie zeigt statt bei Null anzufangen.
- `frontend/index.html` — die komplette Oberfläche in einer Datei (HTML +
  CSS + JS, kein Build-Schritt, bewusst einfach gehalten wie beim
  Schwesterprojekt).

## Lokal starten

```
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

Dann im Browser: **http://127.0.0.1:8000**

`--host 0.0.0.0` sorgt dafür, dass du die App auch vom Handy im selben WLAN
erreichst, unter `http://<Mac-LAN-IP>:8000` (LAN-IP via
`ipconfig getifaddr en0` im Terminal).

## Aufbau (Stand Update 1.3)

Statt einer Tab-Leiste unten gibt es jetzt kleine, feste Icons direkt auf
dem jeweiligen Screen (im Stil des Schwesterprojekts memori-mvp):

- **Workout** (Startbildschirm, Mitte) — zeigt eine Übung nach der anderen,
  je mit Gewicht/Wdh.-Stepper und Satz-Buttons zum Abhaken (nochmal tippen
  hakt wieder ab). Navigation zwischen Übungen über kleine Pfeile
  links/rechts der Punkte-Anzeige. Rudern erfasst eine Dauer in Minuten
  statt Gewicht. Der nächste Trainingstag (Push/Pull) wird automatisch aus
  dem letzten abgeschlossenen Workout abgeleitet, lässt sich vor dem Start
  aber manuell umschalten. Von hier aus: Stift oben links = Trainingsplan,
  Uhr oben Mitte = Timer, Balken oben rechts = Fortschritt, Personen- und
  Zahnrad-Icon unten = Sozial/Einstellungen (führen vorerst zur selben
  Seite).
- **Timer** — feine Striche, kein umschließender Kreis, eine Umdrehung = 1
  Minute. Mit dem Finger nach links ziehen zieht auf, Loslassen startet den
  Countdown; ein schlichter weißer Zeiger (ohne Punkt in der Mitte) läuft
  dabei sichtbar im Uhrzeigersinn nach rechts ab.
- **Fortschritt** — vollflächige, vertikal swipebare Ansicht. Erste Seite
  ist ein **Trainingskalender** (Monatsübung mit Push/Pull-Markierung,
  Pfeile zum Blättern), danach Körpergewicht, danach eine Kurve pro Übung
  (x-Achse = Zeit). Alle bisherigen Trainingsdaten sind von Anfang an da.
- **Einstellungen / Sozial** (eine Seite für beide Icons) — Level/XP-Leiste,
  Streak, dein Freundes-Code zum Teilen, Freunde per Code hinzufügen,
  Rangliste nach Level/XP, Einstiegspunkte zu **Ernährung & Supplements**,
  **Habit-Tracker** und **Erfolge**, Körpermaße-Felder, ein **Ziel** als
  Auswahl-Chips (z. B. Skinny Fat weg, Shredded werden, Crazy Bulk, Team
  Condi), eine explizite Hell/Dunkel-Auswahl und die Versionsnummer ganz
  unten.
- **Trainingsplan-Editor** (Übungen hinzufügen/entfernen/umsortieren) —
  über das Stift-Icon auf dem Workout-Screen erreichbar.

Alle Icons sind handgezeichnete, einfarbige SVGs — keine Emojis.

## Deployment

Siehe `CLAUDE.md` für den Dev/Main-Workflow (lokal entwickeln, erst auf
explizite Ansage hin auf GitHub pushen/Railway deployen).
