**English** | [Deutsch](README.de.md)

# NetBox Proxmox

[NetBox](https://github.com/netbox-community/netbox) plugin that imports and keeps in sync **Proxmox VE** clusters,
nodes, virtual machines (QEMU) and containers (LXC) via the Proxmox API – including disks, network interfaces,
MAC and IP addresses and a daily usage snapshot.

## Features

- **Fetch, review, then sync:** *Fetch from Proxmox* only reads Proxmox – nothing is created or changed in NetBox.
  The **sync overview** lists every node, VM and container with its state and the **pending changes** field by field
  (e.g. `memory 4096 → 8192`, `device pve-a → pve-b`, `+ virtual disk scsi1`). Tick one or more objects and
  *Sync selected* creates or updates exactly those; or *Assign* a Proxmox object to an existing NetBox object, or
  *Ignore* it.
- **Orphans:** NetBox VMs and nodes in a Proxmox cluster without Proxmox counterpart (deleted in Proxmox, or never
  linked) – *Assign* to a Proxmox object, *Ignore*, or *Decommission selected*.
- **Multiple Proxmox connections** (one per cluster or standalone node), each with *Test connection* and
  *Fetch from Proxmox*.
- **Cluster** → NetBox cluster (type *Proxmox VE*), found by name or created.
- **Nodes** → devices in that cluster: status, platform *Proxmox VE x.y*, interfaces (bridges, bonds, VLANs) with IPs,
  primary IP. Missing nodes are created with the defaults of the connection (site, role, device type).
- **VMs and containers** → virtual machines:
  - Cluster plus the **node they run on** (`device`). Migrations inside the cluster are followed on every sync.
    The site and location come from the node device.
  - Status, VM type *Proxmox QEMU* / *Proxmox LXC*, vCPUs, memory, start on boot, platform (from `ostype`),
    serial (SMBIOS UUID), Proxmox tags (added, never removed; only tags created by the sync – an existing
    NetBox tag with the same name is skipped, since tags can drive permissions, config contexts or event rules).
  - Virtual disks with size.
  - Interfaces `net0…` with MAC address, MTU, enabled state, bridge, and untagged VLAN (only if exactly one
    matching VLAN exists).
  - IP addresses from the QEMU guest agent or the LXC runtime (stopped containers: static IPs from the config).
- **Matching:** stored link (VMID) → unique exact name → suggestion → new. A unique match with exactly the same
  name is linked right away when fetching (the NetBox object itself is only changed when it is synced). Automatic
  name matching only considers objects without a cluster or in this Proxmox cluster (nodes: also only at the
  connection's site) – a same-named VM in another cluster (e.g. VMware) is never taken over automatically. If a name
  is ambiguous, the object is left alone and reported.
- **Suggestions:** if exactly one unlinked NetBox object has the same name apart from upper/lower case, or the same
  name in another cluster, it is shown as a *Suggestion* instead of creating a duplicate. *Accept* links it (the
  sync then applies name and cluster from Proxmox, shown as pending changes first), *Reject* remembers the decision
  and the object is treated as new. Suggestions are skipped by *Sync selected* until decided. NetBox does not allow
  two VMs in one cluster whose names differ only in case: rejecting such a suggestion leads to *Error* until one of
  them is renamed.
- **The Proxmox VMID is stored** in the plugin's link table (key: connection + VMID, because VMIDs are only unique
  per cluster). It is also shown in the read-only custom field `proxmox_vmid` on VMs. The link survives renames
  and migrations.
- **Proxmox is authoritative** for the fields above. Tenant, description, comments, other tags and custom fields
  stay untouched. Role and tenant are only set when an object is created.
- **IP addresses:** an existing IP (same address, global VRF) is reused instead of duplicated. IPs assigned to
  another object are skipped. IPs no longer reported are unassigned, never deleted. Primary IPv4/IPv6 is only
  set when it is empty. IPs reported from inside a guest (guest agent, container) are capped at 16 per interface
  and 64 per VM, and never claim an IP documented as *reserved* or *deprecated*.
- **Gone from Proxmox:** objects are marked *stale*, listed as orphans and set *offline* by the daily run – never
  deleted.
- **Usage snapshot** (daily): CPU %, memory and storage used/total per node, VM and container, plus the cluster
  total (shared storage counted once). It is shown in a *Proxmox* card on devices, VMs and clusters, next to
  the resources allocated to active VMs.
- **Changelog:** all changes appear in the NetBox changelog. Scheduled runs use the inactive service user
  `proxmox-sync`, manual runs the user who started them.
- **Daily run:** fetches all enabled connections and updates the **already linked** objects with pending changes
  (CPU, memory, disks, node, IPs, usage). New Proxmox objects are **never created automatically** – they wait in
  the sync overview. Ignored objects are never written.
- Read-only: the plugin never writes to Proxmox.

## Requirements

- NetBox 4.7 or later
- Proxmox VE 7/8/9 with an API token

## Installation

The plugin is not published on PyPI. Install it from the GitHub release archive – this works without `git`,
also in the netbox-docker image. Replace `v0.3.0` with the [release](https://github.com/carstenviell/netbox-proxmox/releases) you want.

**NetBox installed from source (systemd):**

```bash
sudo /opt/netbox/venv/bin/pip install https://github.com/carstenviell/netbox-proxmox/archive/refs/tags/v0.3.0.tar.gz

# keep the plugin when NetBox is upgraded (upgrade.sh reinstalls local_requirements.txt)
echo "https://github.com/carstenviell/netbox-proxmox/archive/refs/tags/v0.3.0.tar.gz" | sudo tee -a /opt/netbox/local_requirements.txt
```

**netbox-docker:** add to `plugin_requirements.txt`

```text
https://github.com/carstenviell/netbox-proxmox/archive/refs/tags/v0.3.0.tar.gz
```

and to `Dockerfile-Plugins` (build `netbox`, `netbox-worker` and `netbox-housekeeping` from it via `docker-compose.override.yml`):

```dockerfile
FROM netboxcommunity/netbox:v4.7
COPY ./plugin_requirements.txt /opt/netbox/
RUN /usr/local/bin/uv pip install -r /opt/netbox/plugin_requirements.txt
```

Enable the plugin in `configuration.py` (Docker: `configuration/plugins.py`):

```python
PLUGINS = [
    # ...
    'netbox_proxmox',
]
```

Apply the migrations and restart **both** services:

```bash
cd /opt/netbox/netbox
sudo /opt/netbox/venv/bin/python manage.py migrate
sudo systemctl restart netbox netbox-rq
```

Docker: `docker compose build --no-cache && docker compose up -d` – migrations run automatically at start.

## Configuration

Optional settings (this is the default, in minutes):

```python
PLUGINS_CONFIG = {
    "netbox_proxmox": {
        "sync_interval_min": 1440,
    },
}
```

**Proxmox API token** (read-only):

```bash
pveum user add netbox@pve
pveum aclmod / -user netbox@pve -role PVEAuditor
pveum user token add netbox@pve sync --privsep 0
```

`PVEAuditor` is enough for inventory and usage. VM IP addresses come from the QEMU guest agent and additionally
need `VM.GuestAgent.Audit` (Proxmox VE 9) or `VM.Monitor` (Proxmox VE 8). Example: a custom role with
`VM.GuestAgent.Audit` on `/vms`.

**Token secret:** it is never stored in the database. Provide it as an environment variable whose name starts
with `PROXMOX_` (default `PROXMOX_TOKEN_SECRET`, one variable per connection if needed). Set it for **both** the
web service and the worker (Docker: `env/netbox.env`; systemd: `systemctl edit netbox` and `netbox-rq`,
`Environment=PROXMOX_TOKEN_SECRET=…` under `[Service]`, then `daemon-reload` and restart. In systemd, `%` must
be written as `%%`).

**TLS:** keep certificate verification on. For the self-signed Proxmox certificate, copy the cluster CA
`/etc/pve/pve-root-ca.pem` from a node to the NetBox host (Docker: mount it into `netbox` and `netbox-worker`) and
enter its path as *CA bundle path*. The API URL must then match a name in the certificate (usually the node's host
name or IP). The connection page shows a warning while verification is off.

**Connection** (*Plugins → Proxmox connections → Add*, superusers only):
- API URL (`https://host:8006`), token ID (`user@realm!tokenname`), the name of the secret variable, and TLS
  verification or a CA bundle path.
- Defaults for new objects: site (also the scope of a new cluster), node role, node device type, VM role,
  container role, tenant.

## Usage

- *Test connection* on the connection page shows the Proxmox VE version.
1. *Plugins → Sync overview → Fetch from Proxmox* (or the button on a connection) reads Proxmox in the background
   (log under *Jobs*). Nothing changes in NetBox.
2. Review the list. States: *New* (no NetBox object), *Changes pending* (linked, the column lists what would
   change), *In sync*, *Ambiguous* (several NetBox objects with that name – use *Assign*), *Suggestion* (a NetBox object with the
   same name apart from case or in another cluster – *Accept* or *Reject*), *Error* (the message
   shows why, e.g. a missing permission or a validation error), *Gone from Proxmox*.
3. Tick objects and *Sync selected*. Sync nodes together with (or before) their VMs, so the VMs get their host.
4. *Assign* links a Proxmox object to an existing NetBox object; *Ignore* hides it from syncs (filter *Ignored*).
5. *Plugins → Proxmox orphans* lists NetBox VMs/nodes without Proxmox counterpart (tabs *VMs and containers* /
   *Nodes*).

The system job runs every `sync_interval_min` minutes (see *Daily run* above).

## Permissions

| Action | Permission |
|---|---|
| View connections | `netbox_proxmox.view_proxmoxconnection` |
| Add/edit/delete connections, test connection | **Superuser** (the connection decides where the token is sent) |
| View sync overview and *Proxmox* cards | `netbox_proxmox.view_proxmoxobject` (linked NetBox objects are only shown with `view` on them; the list of pending changes only with `change_proxmoxobject`) |
| Fetch, assign, ignore | `netbox_proxmox.change_proxmoxobject` |
| Sync selected | `netbox_proxmox.change_proxmoxobject`, plus `add`/`change`/`delete` on **every object the sync touches**: device/VM, VM interfaces, virtual disks, IP addresses, MAC addresses and – when they are new – tags, platforms, cluster type and cluster. Constraints apply (e.g. IPs only in certain VRFs). A missing permission rolls back the whole object and shows *Error* with the reason. The daily run is not affected. |
| Ignore orphans | `netbox_proxmox.add_ignoredorphan` and `delete_ignoredorphan` |
| Decommission orphans | `change` on the device/VM |

## Language

The UI is in English with a German translation. NetBox uses the language of the user's profile (or browser).

## Upgrade

Install the new release archive (see Installation), run `manage.py migrate` and restart `netbox` and `netbox-rq`.

**0.1.x → 0.2.0:** the sync no longer creates new objects automatically. After the upgrade, *Fetch from Proxmox*
once; already synced objects show *In sync*, new ones wait in the sync overview.

## License

Apache-2.0, see [LICENSE](LICENSE).
