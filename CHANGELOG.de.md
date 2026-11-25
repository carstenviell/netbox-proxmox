[English](CHANGELOG.md) | **Deutsch**

# Changelog

Format angelehnt an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/), Versionen nach [SemVer](https://semver.org/lang/de/).

## [Unveröffentlicht]

## [0.1.1] – 2026-11-25
### Sicherheit
- IP-Adressen aus dem Inneren eines Gasts (QEMU Guest Agent, LXC) werden auf 16 je Interface und 64 je VM begrenzt und übernehmen nie eine als reserved/deprecated dokumentierte IP.
- Der Namensabgleich berücksichtigt nur Objekte ohne Cluster oder im eigenen Proxmox-Cluster (Nodes zusätzlich nur am Standort der Verbindung); gleichnamige Objekte anderer Cluster werden nicht mehr übernommen.
- Proxmox-Tags hängen nur noch Tags an, die der Abgleich selbst angelegt hat (markiert über die Beschreibung); gleichnamige NetBox-Tags werden übersprungen. Migration `0002` markiert die von 0.1.0 angelegten Tags.
- Der API-Client folgt keinen HTTP-Weiterleitungen mehr.
- Warnhinweis auf der Verbindungsseite, solange die TLS-Prüfung aus ist; README beschreibt das Proxmox-CA-Bundle.
- Mindestversion von `requests` auf 2.32.4 angehoben (CVE-2024-47081).

## [0.1.0] – 2026-11-25
Getestet mit Proxmox VE 9.2 (2 Nodes, 4 VMs, 19 LXC-Container).

### Neu
- Proxmox-Verbindungen (mehrere, Token-Secret per Umgebungsvariable `PROXMOX_*`, Bearbeiten nur für Superuser) mit *Verbindung testen* und *Jetzt abgleichen*.
- Abgleich von Cluster, Nodes (Geräte inkl. Interfaces/IPs), VMs und LXC-Containern (virtuelle Maschinen inkl. Node-Zuordnung, die Migrationen folgt, Disks, Interfaces mit MAC, VLAN und IPs, Plattform, VM-Typ, Autostart, Seriennummer, Tags).
- Zuordnung über die gespeicherte VMID-Verknüpfung, danach eindeutiger Name; Custom Field `proxmox_vmid`.
- Vorhandene IP-Adressen werden wiederverwendet; in Proxmox entfernte Objekte werden offline gesetzt und als stale markiert, nie gelöscht.
- Täglicher System-Job (`sync_interval_min`) mit Auslastungs-Snapshot je Node/VM/Container und Cluster-Summe; Karte *Proxmox* an Geräten, VMs und Clustern.
- Sync-Übersicht mit Filtern; Änderungen landen im NetBox-Changelog.
- Deutsche Übersetzung.
