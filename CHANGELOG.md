**English** | [Deutsch](CHANGELOG.de.md)

# Changelog

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow [SemVer](https://semver.org/).

## [Unreleased]

## [0.2.0] – 2026-11-25
### Changed
- **Fetch and sync are separate:** *Fetch from Proxmox* (formerly *Sync now*) only reads Proxmox and writes nothing to NetBox. New objects are created only when selected in the sync overview; the daily run updates already linked objects but never creates new ones.
- Unique name matches are linked when fetching; the NetBox object is changed on sync.

### Added
- Sync overview: all Proxmox objects with state (new, changes pending, in sync, ambiguous, error, gone) and a field-by-field list of pending changes, computed by a dry run of the real sync (rolled back).
- Checkboxes with *Sync selected*, *Ignore selected*, *Unignore selected*; per row *Assign* (to an existing NetBox object) and *Ignore*.
- Orphans page: NetBox VMs/nodes in a Proxmox cluster without Proxmox counterpart, with *Assign*, *Ignore* and *Decommission selected*.
- Manual syncs run with the permissions of the requesting user (`add`/`change` on the device/VM, constraints apply).
- Migration `0003`.

## [0.1.1] – 2026-11-25
### Security
- IP addresses reported from inside a guest (QEMU guest agent, LXC) are capped at 16 per interface and 64 per VM and never claim an IP documented as reserved/deprecated.
- Name matching only considers objects without a cluster or in this Proxmox cluster (nodes: also only at the connection's site); same-named objects of other clusters are no longer taken over.
- Proxmox tags only attach tags created by the sync (marked by their description); existing NetBox tags with the same name are skipped. Migration `0002` marks the tags created by 0.1.0.
- The API client no longer follows HTTP redirects.
- Warning on the connection page while TLS verification is off; README explains the Proxmox CA bundle.
- Minimum `requests` version raised to 2.32.4 (CVE-2024-47081).

## [0.1.0] – 2026-11-25
Verified against Proxmox VE 9.2 (2 nodes, 4 VMs, 19 LXC containers).

### Added
- Proxmox connections (multiple, token secret via `PROXMOX_*` environment variable, superuser-only editing) with *Test connection* and *Sync now*.
- Sync of cluster, nodes (devices incl. interfaces/IPs), VMs and LXC containers (virtual machines incl. node assignment that follows migrations, disks, interfaces with MAC, VLAN and IPs, platform, VM type, start on boot, serial, tags).
- Matching via stored VMID link, then unique name; custom field `proxmox_vmid`.
- Existing IP addresses are reused; objects gone from Proxmox are set offline and marked stale, never deleted.
- Daily system job (`sync_interval_min`) with usage snapshot per node/VM/container and cluster total; *Proxmox* card on devices, VMs and clusters.
- Sync overview with filters; changes are written to the NetBox changelog.
- German translation.
