[English](CHANGELOG.md) | **Deutsch**

# Changelog

Format angelehnt an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/), Versionen nach [SemVer](https://semver.org/lang/de/).

## [Unveröffentlicht]
### Offen
- Gegen eine echte Proxmox-VE-Instanz verifizieren (bisher nur mit nachgebildeten API-Antworten getestet), danach Release 0.1.0.

### Neu
- Proxmox-Verbindungen (mehrere, Token-Secret per Umgebungsvariable `PROXMOX_*`, Bearbeiten nur für Superuser) mit *Verbindung testen* und *Jetzt abgleichen*.
- Abgleich von Cluster, Nodes (Geräte inkl. Interfaces/IPs), VMs und LXC-Containern (virtuelle Maschinen inkl. Node-Zuordnung, die Migrationen folgt, Disks, Interfaces mit MAC, VLAN und IPs, Plattform, VM-Typ, Autostart, Seriennummer, Tags).
- Zuordnung über die gespeicherte VMID-Verknüpfung, danach eindeutiger Name; Custom Field `proxmox_vmid`.
- Vorhandene IP-Adressen werden wiederverwendet; in Proxmox entfernte Objekte werden offline gesetzt und als stale markiert, nie gelöscht.
- Täglicher System-Job (`sync_interval_min`) mit Auslastungs-Snapshot je Node/VM/Container und Cluster-Summe; Karte *Proxmox* an Geräten, VMs und Clustern.
- Sync-Übersicht mit Filtern; Änderungen landen im NetBox-Changelog.
- Deutsche Übersetzung.
