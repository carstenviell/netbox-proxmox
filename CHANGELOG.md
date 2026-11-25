**English** | [Deutsch](CHANGELOG.de.md)

# Changelog

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow [SemVer](https://semver.org/).

## [Unreleased]
### Open
- Verify against a real Proxmox VE instance (so far tested with mocked API answers only); release 0.1.0 afterwards.

### Added
- Proxmox connections (multiple, token secret via `PROXMOX_*` environment variable, superuser-only editing) with *Test connection* and *Sync now*.
- Sync of cluster, nodes (devices incl. interfaces/IPs), VMs and LXC containers (virtual machines incl. node assignment that follows migrations, disks, interfaces with MAC, VLAN and IPs, platform, VM type, start on boot, serial, tags).
- Matching via stored VMID link, then unique name; custom field `proxmox_vmid`.
- Existing IP addresses are reused; objects gone from Proxmox are set offline and marked stale, never deleted.
- Daily system job (`sync_interval_min`) with usage snapshot per node/VM/container and cluster total; *Proxmox* card on devices, VMs and clusters.
- Sync overview with filters; changes are written to the NetBox changelog.
- German translation.
