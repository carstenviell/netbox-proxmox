[English](README.md) | **Deutsch**

# NetBox Proxmox

[NetBox](https://github.com/netbox-community/netbox)-Plugin, das **Proxmox-VE**-Cluster, Nodes, virtuelle Maschinen
(QEMU) und Container (LXC) über die Proxmox-API importiert und aktuell hält. Dazu gehören Disks,
Netzwerkkarten, MAC- und IP-Adressen und ein täglicher Auslastungs-Snapshot.

## Funktionen

- **Mehrere Proxmox-Verbindungen** (je Cluster oder Einzel-Node), jeweils mit *Verbindung testen* und *Jetzt abgleichen*.
- **Cluster** → NetBox-Cluster (Typ *Proxmox VE*), per Name gefunden oder angelegt.
- **Nodes** → Geräte in diesem Cluster: Status, Plattform *Proxmox VE x.y*, Interfaces (Bridges, Bonds, VLANs) mit IPs,
  primäre IP. Fehlende Nodes werden mit den Standardwerten der Verbindung angelegt (Standort, Rolle, Gerätetyp).
- **VMs und Container** → virtuelle Maschinen:
  - Cluster und zusätzlich der **Node, auf dem sie laufen** (`device`). Migrationen innerhalb des Clusters werden
    bei jedem Abgleich nachgezogen. Standort und Location ergeben sich über das Node-Gerät.
  - Status, VM-Typ *Proxmox QEMU* / *Proxmox LXC*, vCPUs, RAM, Autostart, Plattform (aus `ostype`),
    Seriennummer (SMBIOS-UUID), Proxmox-Tags (werden ergänzt, nie entfernt). Verwendet werden nur Tags, die der
    Abgleich selbst angelegt hat. Ein gleichnamiges NetBox-Tag wird übersprungen, weil Tags Rechte, Config Contexts
    oder Event Rules steuern können.
  - Virtuelle Disks mit Größe.
  - Interfaces `net0…` mit MAC-Adresse, MTU, Aktiv-Status, Bridge und untagged VLAN (nur wenn genau ein
    passendes VLAN existiert).
  - IP-Adressen aus dem QEMU Guest Agent bzw. der LXC-Laufzeit (gestoppte Container: statische IPs aus der
    Konfiguration).
- **Zuordnung:** gespeicherte Verknüpfung (VMID) → eindeutiger Name → Neuanlage. Vorhandene Objekte werden über
  den Namen gefunden und aktualisiert, nie doppelt angelegt. Der Namensabgleich berücksichtigt nur Objekte ohne
  Cluster oder im eigenen Proxmox-Cluster (Nodes zusätzlich nur am Standort der Verbindung). Eine gleichnamige VM in
  einem anderen Cluster (z. B. VMware) wird also nie übernommen. Bei mehrdeutigem Namen bleibt das Objekt
  unangetastet und wird gemeldet.
- **Die Proxmox-VMID wird gespeichert**, und zwar in der Verknüpfungstabelle des Plugins. Der Schlüssel ist
  Verbindung + VMID, weil VMIDs nur pro Cluster eindeutig sind. Zusätzlich steht sie im schreibgeschützten Custom
  Field `proxmox_vmid` an VMs. Die Verknüpfung übersteht Umbenennungen und Migrationen.
- **Proxmox ist führend** für die oben genannten Felder. Mandant, Beschreibung, Kommentare, andere Tags und
  Custom Fields bleiben unberührt. Rolle und Mandant werden nur beim Anlegen gesetzt.
- **IP-Adressen:** Eine vorhandene IP (gleiche Adresse, globales VRF) wird wiederverwendet statt doppelt angelegt.
  IPs, die einem anderen Objekt zugewiesen sind, werden übersprungen. Nicht mehr gemeldete IPs werden nur gelöst,
  nie gelöscht. Primäre IPv4/IPv6 wird nur gesetzt, wenn sie leer ist. IPs, die aus dem Inneren eines Gasts kommen
  (Guest Agent, Container), werden auf 16 je Interface und 64 je VM begrenzt und übernehmen nie eine IP, die als
  *reserved* oder *deprecated* dokumentiert ist.
- **In Proxmox entfernt:** Objekte werden auf *offline* gesetzt und in der Sync-Übersicht als *stale* markiert,
  nie gelöscht.
- **Auslastungs-Snapshot** (täglich): CPU %, RAM und Speicher belegt/gesamt je Node, VM und Container sowie die
  Cluster-Summe (Shared Storage einfach gezählt). Er erscheint in einer Karte *Proxmox* an Geräten, VMs und
  Clustern, neben den Ressourcen, die aktiven VMs zugewiesen sind.
- **Changelog:** Alle Änderungen erscheinen im NetBox-Changelog. Geplante Läufe laufen unter dem inaktiven
  Dienstbenutzer `proxmox-sync`, manuelle Läufe unter dem auslösenden Benutzer.
- **Sync-Übersicht** mit Filtern (Verbindung, Typ, Zustand) zum Bereinigen von *mehrdeutig*, *Fehler* und *stale*.
- Nur lesend: Das Plugin schreibt nie nach Proxmox.

## Voraussetzungen

- NetBox 4.7 oder neuer
- Proxmox VE 7/8/9 mit API-Token

## Installation

Das Plugin ist nicht auf PyPI veröffentlicht. Installiert wird es aus dem GitHub-Release-Archiv. Das funktioniert
ohne `git`, auch im netbox-docker-Image. `v0.1.1` durch das gewünschte [Release](https://github.com/carstenviell/netbox-proxmox/releases) ersetzen.

**NetBox aus dem Quellcode (systemd):**

```bash
sudo /opt/netbox/venv/bin/pip install https://github.com/carstenviell/netbox-proxmox/archive/refs/tags/v0.1.1.tar.gz

# Plugin bei NetBox-Upgrades behalten (upgrade.sh installiert local_requirements.txt neu)
echo "https://github.com/carstenviell/netbox-proxmox/archive/refs/tags/v0.1.1.tar.gz" | sudo tee -a /opt/netbox/local_requirements.txt
```

**netbox-docker:** in `plugin_requirements.txt` eintragen

```text
https://github.com/carstenviell/netbox-proxmox/archive/refs/tags/v0.1.1.tar.gz
```

und in `Dockerfile-Plugins` (daraus `netbox`, `netbox-worker` und `netbox-housekeeping` über `docker-compose.override.yml` bauen):

```dockerfile
FROM netboxcommunity/netbox:v4.7
COPY ./plugin_requirements.txt /opt/netbox/
RUN /usr/local/bin/uv pip install -r /opt/netbox/plugin_requirements.txt
```

Plugin in `configuration.py` aktivieren (Docker: `configuration/plugins.py`):

```python
PLUGINS = [
    # ...
    'netbox_proxmox',
]
```

Migrationen ausführen und **beide** Dienste neu starten:

```bash
cd /opt/netbox/netbox
sudo /opt/netbox/venv/bin/python manage.py migrate
sudo systemctl restart netbox netbox-rq
```

Docker: `docker compose build --no-cache && docker compose up -d`. Die Migrationen laufen beim Start automatisch.

## Konfiguration

Optionale Einstellung (Standardwert, in Minuten):

```python
PLUGINS_CONFIG = {
    "netbox_proxmox": {
        "sync_interval_min": 1440,
    },
}
```

**Proxmox-API-Token** (nur lesend):

```bash
pveum user add netbox@pve
pveum aclmod / -user netbox@pve -role PVEAuditor
pveum user token add netbox@pve sync --privsep 0
```

`PVEAuditor` genügt für Bestand und Auslastung. Die IP-Adressen von VMs kommen aus dem QEMU Guest Agent und
brauchen zusätzlich `VM.GuestAgent.Audit` (Proxmox VE 9) bzw. `VM.Monitor` (Proxmox VE 8). Beispiel: eigene
Rolle mit `VM.GuestAgent.Audit` auf `/vms`.

**Token-Secret:** Es wird nie in der Datenbank gespeichert. Es wird als Umgebungsvariable übergeben, deren Name
mit `PROXMOX_` beginnt (Standard `PROXMOX_TOKEN_SECRET`, bei Bedarf eine je Verbindung). Die Variable muss für
**beide** Dienste gesetzt sein, Webdienst und Worker:
- Docker: `env/netbox.env`
- systemd: `systemctl edit netbox` und `netbox-rq`, unter `[Service]` die Zeile
  `Environment=PROXMOX_TOKEN_SECRET=…`, danach `daemon-reload` und Neustart. In systemd muss `%` als `%%`
  geschrieben werden.

**TLS:** Die Zertifikatsprüfung eingeschaltet lassen. Für das selbstsignierte Proxmox-Zertifikat die Cluster-CA
`/etc/pve/pve-root-ca.pem` von einem Node auf den NetBox-Host kopieren (Docker: in `netbox` und `netbox-worker`
einbinden) und ihren Pfad als *Pfad zum CA-Bundle* eintragen. Die API-URL muss dann zu einem Namen im Zertifikat
passen (meist Hostname oder IP des Nodes). Solange die Prüfung aus ist, zeigt die Verbindungsseite eine Warnung.

**Verbindung** (*Plugins → Proxmox-Verbindungen → Hinzufügen*, nur Superuser):
- API-URL (`https://host:8006`), Token-ID (`benutzer@realm!tokenname`), Name der Secret-Variable, TLS-Prüfung
  bzw. Pfad zu einem CA-Bundle.
- Standardwerte für neue Objekte: Standort (auch Scope eines neuen Clusters), Node-Rolle, Node-Gerätetyp,
  VM-Rolle, Container-Rolle, Mandant.

## Benutzung

- *Verbindung testen* auf der Verbindungsseite zeigt die Proxmox-VE-Version.
- *Jetzt abgleichen* startet den Abgleich dieser Verbindung als Hintergrund-Job (Log unter *Jobs*). Der System-Job
  gleicht alle aktiven Verbindungen alle `sync_interval_min` Minuten ab.
- *Plugins → Proxmox-Sync-Übersicht* listet jedes Proxmox-Objekt mit NetBox-Objekt, Zustand und Auslastung.
  Typische Nacharbeit:
  - *mehrdeutig*: doppelte NetBox-Objekte umbenennen oder zusammenführen, der nächste Abgleich verknüpft sie.
  - *Fehler*: Die Meldung zeigt den Validierungsfehler (z. B. Node-Gerät an einem anderen Standort als der Cluster).
  - *stale*: Das Objekt gibt es in Proxmox nicht mehr, in NetBox außer Betrieb nehmen oder löschen.

## Berechtigungen

| Aktion | Berechtigung |
|---|---|
| Verbindungen ansehen | `netbox_proxmox.view_proxmoxconnection` |
| Verbindungen anlegen/ändern/löschen, Verbindung testen | **Superuser** (die Verbindung bestimmt, wohin das Token geht) |
| Sync-Übersicht und Karten *Proxmox* ansehen | `netbox_proxmox.view_proxmoxobject` |
| *Jetzt abgleichen* | `netbox_proxmox.change_proxmoxobject` |

## Sprache

Die Oberfläche ist englisch mit deutscher Übersetzung. NetBox nutzt die Sprache aus dem Benutzerprofil (bzw. des Browsers).

## Upgrade

Neues Release-Archiv installieren (siehe Installation), `manage.py migrate` ausführen, `netbox` und `netbox-rq` neu starten.

## Lizenz

Apache-2.0, siehe [LICENSE](LICENSE).
