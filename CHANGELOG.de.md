[English](CHANGELOG.md) | **Deutsch**

# Changelog

Format angelehnt an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/), Versionen nach [SemVer](https://semver.org/lang/de/).

## [Unveröffentlicht]

## [0.3.1] – 2026-11-26
### Sicherheit
- Der tägliche Lauf schreibt keine Objekte mehr, die von Hand verknüpft (*Zuordnen*, *Übernehmen*) und nie manuell synchronisiert wurden. Bisher konnte ein Benutzer, der nur die VM ändern darf, sie verknüpfen, und der nächste tägliche Lauf (ohne Rechteprüfung) schrieb alles – einschließlich Löschen von Interfaces/Disks, IP-Adressen und Verschieben in den Proxmox-Cluster. Solche Objekte müssen jetzt einmal per *Ausgewählte synchronisieren* abgeglichen werden, das die Rechte des Benutzers an jedem berührten Objekt prüft. Automatisch per exaktem Namen verknüpfte und bereits synchronisierte Objekte bleiben im täglichen Lauf (Migration `0006` übernimmt vorhandene Verknüpfungen).
- Die Bestätigung für *Übernehmen* nennt Cluster und Mandant des vorgeschlagenen Objekts und warnt, wenn es in einem anderen Cluster liegt und beim Sync verschoben würde.

## [0.3.0] – 2026-11-26
### Neu
- **Namensvorschläge:** Hat eine Proxmox-VM, ein Container oder ein Node keinen exakten Namenstreffer, aber genau ein
  nicht verknüpftes NetBox-Objekt mit gleichem Namen in anderer Groß-/Kleinschreibung oder gleichnamig in einem
  anderen Cluster, zeigt die Sync-Übersicht es als *Vorschlag* mit den Buttons *Übernehmen* und *Ablehnen*.
  Übernehmen verknüpft das Objekt (Name und Cluster aus Proxmox erscheinen als ausstehende Änderungen); Ablehnen wird
  gespeichert, und das Objekt gilt als neu. Migration `0005`.

### Geändert
- Die automatische Verknüpfung über den Namen verlangt jetzt exakt gleiche Namen (mit Groß-/Kleinschreibung); Namen,
  die sich nur in der Schreibweise unterscheiden, werden zum Vorschlag statt still verknüpft.
- *Ausgewählte synchronisieren* überspringt Vorschläge: Bisher wurde eine VM, deren Namensvetter in einem anderen
  Cluster lag, ein zweites Mal angelegt.

## [0.2.2] – 2026-11-26
### Sicherheit
- *Ausgewählte synchronisieren* prüft jetzt die Rechte des auslösenden Benutzers an **jedem** Objekt, das der Abgleich anlegt, ändert oder löscht (IP-Adressen, VM-Interfaces, virtuelle Disks, MAC-Adressen, Tags, Plattformen, VM-Typen, Cluster-Typ, Cluster), inklusive Constraints. Vorher wurde nur das Gerät bzw. die VM selbst geprüft, sodass z. B. IPs ohne IPAM-Rechte angelegt werden konnten. Ein fehlendes Recht rollt das ganze Objekt zurück (*Fehler* mit Grund). Der tägliche Lauf ist unverändert.
- Der Abruf speichert nur noch die Schlüssel der Gast-Konfiguration, die der Abgleich nutzt; Freitext-Notizen (`description`), Hostname, DNS-Einstellungen usw. werden nicht mehr gespeichert. Migration `0004` entfernt sie aus vorhandenen Daten.
- Sync-Übersicht: Verknüpfte NetBox-Objekte sehen nur Benutzer, die sie ansehen dürfen; die Liste der ausstehenden Änderungen nur mit `change_proxmoxobject`.
- Orphans ignorieren erfordert `add_ignoredorphan`/`delete_ignoredorphan`.

### Behoben
- Fehlerhafte Auswahlwerte führen nicht mehr zu HTTP 500; Weiterleitungsparameter werden URL-kodiert.

## [0.2.1] – 2026-11-25
### Behoben
- Die Sync-Übersicht brach mit `TypeError: args or kwargs must be provided` (Server Error) ab, sobald ein Objekt ausstehende Änderungen hatte (z. B. neue Objekte nach dem ersten Abruf). Ursache: `format_html()` ohne Argumente, seit Django 6 (NetBox 4.7) ein Fehler. Regressionstest ergänzt.

## [0.2.0] – 2026-11-25
### Geändert
- **Abrufen und Synchronisieren getrennt:** *Von Proxmox abrufen* (vorher *Jetzt abgleichen*) liest nur aus Proxmox und schreibt nichts in NetBox. Neue Objekte werden nur angelegt, wenn sie in der Sync-Übersicht ausgewählt werden; der tägliche Lauf aktualisiert bereits verknüpfte Objekte, legt aber nie neue an.
- Eindeutige Namenstreffer werden beim Abruf verknüpft; das NetBox-Objekt ändert sich erst beim Synchronisieren.

### Neu
- Sync-Übersicht: alle Proxmox-Objekte mit Zustand (neu, Änderungen ausstehend, abgeglichen, mehrdeutig, Fehler, entfernt) und Feld-für-Feld-Liste der ausstehenden Änderungen, berechnet per Probelauf des echten Abgleichs (mit Rollback).
- Checkboxen mit *Ausgewählte synchronisieren*, *Ausgewählte ignorieren*, *Ignorieren aufheben*; je Zeile *Zuordnen* (zu einem bestehenden NetBox-Objekt) und *Ignorieren*.
- Orphans-Seite: NetBox-VMs/-Nodes in einem Proxmox-Cluster ohne Proxmox-Gegenstück, mit *Zuordnen*, *Ignorieren* und *Ausgewählte außer Betrieb setzen*.
- Manuelle Abgleiche laufen mit den Rechten des auslösenden Benutzers (`add`/`change` am Gerät bzw. an der VM, Constraints greifen).
- Migration `0003`.

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
