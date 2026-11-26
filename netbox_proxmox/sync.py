"""
Proxmox -> NetBox sync. Proxmox is the source for the fields set here; everything else on the NetBox objects
(tenant, role after creation, description, comments, other tags, custom fields) is left alone.

Two phases (0.2.0):
- discover(): fetch from Proxmox and store the raw data per object in ProxmoxObject.data. Nothing is written to
  NetBox; the pending changes are computed by running the real write code in a savepoint that is rolled back
  (preview == result, because both run on the same stored data).
- write(): apply the stored data of selected (or, daily, of all linked) objects.

Matching: existing ProxmoxObject link -> unique name (linked right away) -> new. Objects gone from Proxmox are never
deleted, only marked stale (daily run: set offline) and listed as orphans.
"""
import ipaddress
import logging
import re
from collections import Counter
from contextvars import ContextVar

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _

from dcim.models import Device, Interface, Platform
from extras.models import Tag
from ipam.models import VLAN, IPAddress
from virtualization.models import Cluster, ClusterType, VirtualDisk, VirtualMachine, VirtualMachineType, VMInterface

from .client import ProxmoxError
from .custom_fields import CF_VMID
from .models import ProxmoxObject

MIB = 1024 ** 2
SIZE_UNITS = {'': 1 / MIB, 'K': 1 / 1024, 'M': 1, 'G': 1024, 'T': 1024 ** 2}  # no suffix = bytes
QEMU_DISK = re.compile(r'^(scsi|virtio|sata|ide)\d+$')
LXC_DISK = re.compile(r'^(rootfs|mp\d+)$')
NET_KEY = re.compile(r'^net\d+$')
NIC_MODELS = {'virtio', 'e1000', 'e1000e', 'rtl8139', 'vmxnet3', 'i82551', 'i82557b', 'i82559er', 'ne2k_isa',
              'ne2k_pci', 'pcnet'}
OSTYPE_NAMES = {
    'l24': 'Linux', 'l26': 'Linux', 'win11': 'Windows 11/2022/2025', 'win10': 'Windows 10/2016/2019',
    'win8': 'Windows 8/2012', 'win7': 'Windows 7/2008r2', 'w2k8': 'Windows Vista/2008', 'wvista': 'Windows Vista/2008',
    'w2k3': 'Windows 2003', 'wxp': 'Windows XP', 'w2k': 'Windows 2000', 'solaris': 'Solaris',
    'archlinux': 'Arch Linux', 'opensuse': 'openSUSE', 'nixos': 'NixOS', 'centos': 'CentOS',
}
NODE_IFACE_TYPES = {'bridge': 'bridge', 'OVSBridge': 'bridge', 'bond': 'lag', 'OVSBond': 'lag',
                    'vlan': 'virtual', 'OVSIntPort': 'virtual', 'OVSPort': 'virtual'}
MAX_IPS_PER_IFACE = 16  # guest agent data comes from inside the VM: cap what one guest can write to IPAM
MAX_IPS_PER_VM = 64
GUEST_SKIP_STATUS = ('reserved', 'deprecated')  # documented IPs a guest must not claim
TAG_MARKER = 'Created by the Proxmox sync'  # only tags carrying this description are attached by the sync
VM_STATUS = {'running': 'active', 'stopped': 'offline', 'paused': 'paused', 'suspended': 'paused'}


# -- pure helpers (tested) -----------------------------------------------------------------
def parse_kv(value):
    """'virtio=AA:BB,bridge=vmbr0,tag=10' -> dict. A leading item without '=' gets the key ''."""
    out = {}
    for part in str(value or '').split(','):
        k, sep, v = part.partition('=')
        out[k.strip() if sep else ''] = v.strip() if sep else k.strip()
    return out


def size_mb(value):
    """Proxmox size ('32G', '512M', '1T', '1048576K') -> MiB (NetBox disk unit), None if unknown."""
    m = re.fullmatch(r'(\d+(?:\.\d+)?)([KMGT]?)', str(value or '').strip())
    if not m:
        return None
    return int(float(m.group(1)) * SIZE_UNITS[m.group(2)])


def parse_disks(config, kind):
    """[(name, size_mb)] for data disks; CD-ROMs, cloud-init drives and unused volumes are skipped."""
    pattern = QEMU_DISK if kind == 'qemu' else LXC_DISK
    disks = []
    for key, value in config.items():
        if not pattern.match(key):
            continue
        opts = parse_kv(value)
        if opts.get('media') == 'cdrom' or 'cloudinit' in str(value):
            continue
        size = size_mb(opts.get('size'))
        if size:
            disks.append((key, size))
    return sorted(disks)


def parse_nics(config, kind):
    """[{name, mac, bridge, vlan, mtu, enabled, guest_name, static}] from netX entries."""
    nics = []
    for key, value in config.items():
        if not NET_KEY.match(key):
            continue
        opts = parse_kv(value)
        if kind == 'qemu':
            model = next((k for k in opts if k in NIC_MODELS), None)
            mac = opts.get(model) if model else None
        else:
            mac = opts.get('hwaddr')
        static = [opts[k] for k in ('ip', 'ip6') if opts.get(k) and opts[k] not in ('dhcp', 'auto', 'manual')]
        nics.append({
            'name': key, 'mac': (mac or '').lower() or None, 'bridge': opts.get('bridge', ''),
            'vlan': int(opts['tag']) if opts.get('tag', '').isdigit() else None,
            'mtu': int(opts['mtu']) if opts.get('mtu', '').isdigit() and int(opts['mtu']) > 1 else None,
            'enabled': opts.get('link_down') != '1', 'guest_name': opts.get('name', ''), 'static': static,
        })
    return sorted(nics, key=lambda n: int(n['name'][3:]))


def usable_ip(value):
    """ip_interface for addresses worth documenting (no loopback / link-local), else None."""
    try:
        ip = ipaddress.ip_interface(value)
    except ValueError:
        return None
    return None if ip.ip.is_loopback or ip.ip.is_link_local else ip


def agent_ips(result):
    """QEMU guest agent network-get-interfaces -> {mac: [ip_interface]}."""
    out = {}
    for iface in result or []:
        mac = (iface.get('hardware-address') or '').lower()
        for a in iface.get('ip-addresses') or []:
            ip = usable_ip(f"{a.get('ip-address')}/{a.get('prefix')}")
            if mac and ip:
                out.setdefault(mac, []).append(ip)
    return out


def lxc_ips(interfaces):
    """LXC /interfaces -> {mac: [ip_interface]}."""
    out = {}
    for iface in interfaces or []:
        mac = (iface.get('hwaddr') or '').lower()
        for key in ('inet', 'inet6'):
            for value in str(iface.get(key) or '').split():
                ip = usable_ip(value)
                if mac and ip:
                    out.setdefault(mac, []).append(ip)
    return out


CONFIG_KEYS = ('ostype', 'onboot', 'smbios1')


def used_config(config):
    """Only the config keys the sync uses. Notes (description), hostname, DNS etc. are not stored - free-text
    notes often contain credentials and would otherwise end up in every NetBox database dump."""
    return {k: v for k, v in config.items()
            if k in CONFIG_KEYS or QEMU_DISK.match(k) or LXC_DISK.match(k) or NET_KEY.match(k)}


def platform_name(ostype):
    if not ostype or ostype in ('other', 'unmanaged'):
        return None
    return OSTYPE_NAMES.get(ostype, ostype.capitalize())


def usage(resource):
    """Usage snapshot fields from a /cluster/resources entry."""
    return {
        'cpu_usage': round(float(resource.get('cpu') or 0) * 100, 1),
        'cpu_count': resource.get('maxcpu'), 'mem_used': resource.get('mem'), 'mem_total': resource.get('maxmem'),
        'disk_used': resource.get('disk'), 'disk_total': resource.get('maxdisk'), 'uptime': resource.get('uptime'),
    }


def storage_usage(resources):
    """{node: (used, total)} of local storages and the cluster total (shared storages counted once)."""
    per_node, shared = {}, {}
    for r in resources:
        if r.get('type') != 'storage' or r.get('status') != 'available':
            continue
        used, total = r.get('disk') or 0, r.get('maxdisk') or 0
        if r.get('shared'):
            shared[r.get('storage')] = (used, total)
        else:
            u, t = per_node.get(r['node'], (0, 0))
            per_node[r['node']] = (u + used, t + total)
    cluster = tuple(sum(x) for x in zip((0, 0), *per_node.values(), *shared.values()))
    return per_node, cluster


# -- change recording (preview) -------------------------------------------------------------
_RECORD = ContextVar('netbox_proxmox_record', default=None)
MAX_CHANGES = 100


def record(text):
    """Note a change while a preview is recording (no-op otherwise)."""
    rec = _RECORD.get()
    if rec is not None and len(rec) < MAX_CHANGES:
        rec.append(str(text))


def _fmt(value):
    return '—' if value is None or value == '' else str(value)


def _label(obj):
    return f'{obj._meta.verbose_name} {obj}'


def _field_label(obj, name):
    try:
        return str(getattr(obj._meta.get_field(name), 'verbose_name', name))
    except Exception:  # noqa: BLE001 - label only
        return name


_USER = ContextVar('netbox_proxmox_user', default=None)  # set during manual writes; None = system run


def permit(obj, action):
    """Raise PermissionDenied unless the user of a manual sync may <action> obj (permission constraints included).
    Covers every object the sync touches (IPs, interfaces, disks, MACs, tags, platforms, types), not only the VM."""
    user = _USER.get()
    if user is None:
        return
    if not type(obj).objects.restrict(user, action).filter(pk=obj.pk).exists():
        text = {'add': _('You are not allowed to create %(obj)s.'), 'change': _('You are not allowed to change %(obj)s.'),
                'delete': _('You are not allowed to delete %(obj)s.')}[action]
        raise PermissionDenied(text % {'obj': _label(obj)})


def error_text(exc):
    return '; '.join(getattr(exc, 'messages', None) or [str(exc)])


# -- NetBox write helpers ------------------------------------------------------------------
def apply(obj, **fields):
    """Set fields and save (with changelog snapshot) only if something changed. Returns True if saved."""
    new = obj.pk is None
    if not new and all(getattr(obj, k) == v for k, v in fields.items()):
        return False
    old = {} if new else {k: getattr(obj, k) for k in fields}
    if not new:
        permit(obj, 'change')  # constraints on the current state
        obj.snapshot()
    for k, v in fields.items():
        setattr(obj, k, v)
    obj.full_clean()
    obj.save()
    permit(obj, 'add' if new else 'change')  # constraints on the new state
    if new:
        record(f'+ {_label(obj)}')
    else:
        for k, v in fields.items():
            if old[k] != v:
                record(f'{_label(obj)}: {_field_label(obj, k)} {_fmt(old[k])} → {_fmt(v)}')
    return True


def get_or_create_named(model, name, **extra):
    """Lookup by slug or name (case-insensitive), create if missing. For types/platforms/tags."""
    slug = slugify(name)[:100]
    obj = model.objects.filter(slug=slug).first() or model.objects.filter(name__iexact=name).first()
    if obj is None:
        obj = model(name=name, slug=slug, **extra)
        obj.full_clean()
        obj.save()
        permit(obj, 'add')
    return obj


def add_tags(obj, tags, logger):
    """Attach Proxmox tags (never remove). Only tags created by the sync are used: an existing NetBox tag with
    the same name may drive permissions, config contexts or event rules and must not be set from Proxmox."""
    to_add = []
    for name in (t.strip() for t in str(tags or '').split(';')):
        if not name or obj.tags.filter(slug=slugify(name)).exists():
            continue
        tag = Tag.objects.filter(slug=slugify(name)[:100]).first() or Tag.objects.filter(name__iexact=name).first()
        if tag is None:
            tag = get_or_create_named(Tag, name, description=TAG_MARKER)
        elif tag.description != TAG_MARKER:
            logger.warning(_('%(obj)s: tag %(tag)s exists in NetBox and is not managed by the Proxmox sync – '
                             'skipped.') % {'obj': obj, 'tag': tag})
            continue
        to_add.append(tag)
    if to_add:
        permit(obj, 'change')
        obj.snapshot()
        obj.tags.add(*to_add)
        for tag in to_add:
            record(f'{_label(obj)}: + tag {tag}')


def sync_ips(owner, iface, addresses, logger, guest=False):
    """Make the interface carry exactly `addresses` (ip_interface list). Existing global-VRF IPs are reused;
    IPs assigned to another object are left alone. IPs no longer reported are unassigned, never deleted.
    guest=True (data from inside a VM/container): reserved/deprecated IPs are not claimed."""
    wanted = {str(ip.ip): ip for ip in addresses}
    current = {str(ip.address.ip): ip for ip in iface.ip_addresses.all()}
    for host, ip in wanted.items():
        if host in current:
            continue
        existing = IPAddress.objects.filter(address__net_host=host, vrf__isnull=True).first()
        if existing is None:
            apply(IPAddress(address=str(ip), status='active'), assigned_object=iface)
        elif guest and existing.status in GUEST_SKIP_STATUS:
            logger.warning(_('%(owner)s: %(ip)s is documented as %(status)s in NetBox – not assigned.') % {
                'owner': owner, 'ip': host, 'status': existing.status})
        elif existing.assigned_object_id is None:
            apply(existing, assigned_object=iface)
        else:
            logger.warning(_('%(owner)s: %(ip)s is already assigned to %(other)s – skipped.') % {
                'owner': owner, 'ip': host, 'other': existing.assigned_object})
    for host, ip in current.items():
        if host not in wanted:
            for field in ('primary_ip4', 'primary_ip6'):
                if getattr(owner, field + '_id') == ip.pk:
                    apply(owner, **{field: None})
            apply(ip, assigned_object=None)


def set_primary_ips(owner, assigned):
    """Fill empty primary IPv4/IPv6 with the first assigned address of that family."""
    changes = {}
    for field, family in (('primary_ip4', 4), ('primary_ip6', 6)):
        if getattr(owner, field + '_id') is None:
            ip = next((ip for ip in assigned if ip.family == family), None)
            if ip:
                changes[field] = ip
    if changes:
        apply(owner, **changes)


# -- matching --------------------------------------------------------------------------------
def match(link, queryset, name, narrow):
    """(object, None) | (None, None) for "create" | (None, message) when ambiguous.
    `queryset` limits name matching to objects that may belong to this Proxmox cluster - never take over an
    object of another cluster that just happens to have the same name."""
    if link.target:
        return link.target, None
    candidates = list(queryset.filter(name=name, proxmox_object__isnull=True)[:20])  # exact only, see suggest()
    if len(candidates) > 1:
        narrowed = [c for c in candidates if narrow(c)]
        if len(narrowed) == 1:
            return narrowed[0], None
        return None, _('%(n)d NetBox objects named %(name)s – assign manually or rename.') % {
            'n': len(candidates), 'name': name}
    return (candidates[0] if candidates else None), None


def suggest(queryset, name, rejected):
    """The one not yet linked object whose name matches apart from case - in any cluster. Only shown as a
    suggestion: the user decides (and needs change permission on it), nothing is linked automatically."""
    candidates = list(queryset.filter(name__iexact=name, proxmox_object__isnull=True)
                      .exclude(pk__in=rejected or []).values_list('pk', flat=True)[:2])
    return candidates[0] if len(candidates) == 1 else None


# -- sync ------------------------------------------------------------------------------------
class _Rollback(Exception):
    """Ends a preview savepoint."""


PREVIEW_LOG = logging.getLogger('netbox_proxmox.preview')  # previews must not fill the job log with "creating …"


class Sync:
    def __init__(self, conn, logger, client=None, user=None):
        self.conn, self.logger, self.client, self.user = conn, logger, client, user
        self.counts = Counter()
        self.now = timezone.now()
        self.dry = False

    @property
    def log(self):
        return PREVIEW_LOG if self.dry else self.logger

    # == phase 1: fetch from Proxmox (writes only ProxmoxObject) ===============================
    def discover(self):
        resources = self.client.get('cluster', 'resources') or []
        status = self.client.get('cluster', 'status') or []
        cluster_name = next((s['name'] for s in status if s.get('type') == 'cluster'), None) or self.conn.name
        node_storage, cluster_storage = storage_usage(resources)
        nodes = [r for r in resources if r.get('type') == 'node']
        guests = [r for r in resources if r.get('type') in ('qemu', 'lxc') and not r.get('template')]
        self.cluster_snapshot(cluster_name, nodes, cluster_storage)
        seen = set()
        for r in nodes:  # nodes first: guests are previewed against the linked node devices
            u = usage(r)
            if r['node'] in node_storage:
                u['disk_used'], u['disk_total'] = node_storage[r['node']]
            seen.add(('node', r['node']))
            self.prepare('node', r['node'], r['node'], r['node'], u, self.fetch_node(r))
        for r in guests:
            seen.add((r['type'], str(r['vmid'])))
            name = (r.get('name') or f"{r['type']}-{r['vmid']}")[:64]
            self.prepare(r['type'], r['vmid'], name, r.get('node', ''), usage(r), self.fetch_guest(r))
        self.mark_stale(seen)
        type(self.conn).objects.filter(pk=self.conn.pk).update(last_sync=self.now)  # bookkeeping, no changelog
        return dict(self.counts)

    def fetch_node(self, r):
        payload = {'resource': r}
        if r.get('status') == 'online':
            try:
                payload['pveversion'] = (self.client.get('nodes', r['node'], 'status') or {}).get('pveversion', '')
                payload['network'] = self.client.get('nodes', r['node'], 'network') or []
            except ProxmoxError as exc:
                self.logger.warning(f"node {r['node']}: {exc}")
        return payload

    def fetch_guest(self, r):
        kind, vmid, node = r['type'], r['vmid'], r.get('node')
        payload = {'resource': r, 'config': None, 'live': None}
        try:
            payload['config'] = used_config(self.client.get('nodes', node, kind, vmid, 'config') or {})
        except ProxmoxError as exc:  # e.g. node offline: keep what NetBox has, only status/usage
            self.logger.warning(f'{kind} {vmid}: {exc}')
        live = self.live_ips(kind, node, vmid, r)
        if live is not None:
            payload['live'] = {mac: [str(ip) for ip in ips] for mac, ips in live.items()}
        return payload

    def live_ips(self, kind, node, vmid, r):
        """{mac: [ip]} from the guest, or None if not available (then IPs stay untouched)."""
        if r.get('status') != 'running':
            return None
        try:
            if kind == 'qemu':
                return agent_ips((self.client.get('nodes', node, 'qemu', vmid, 'agent', 'network-get-interfaces')
                                  or {}).get('result'))
            return lxc_ips(self.client.get('nodes', node, 'lxc', vmid, 'interfaces'))
        except ProxmoxError as exc:  # agent not installed/running: normal, not an error
            self.logger.debug(f'{kind} {vmid}: no IP data ({exc})')
            return None

    def prepare(self, kind, pid, name, node, usage_fields, payload):
        link, _created = ProxmoxObject.objects.get_or_create(
            connection=self.conn, kind=kind, proxmox_id=str(pid), defaults={'state': 'new'})
        link.name, link.node, link.last_seen, link.data = name, node, self.now, payload
        for k, v in usage_fields.items():
            setattr(link, k, v)
        ambiguous = ''
        link.suggested_pk = None
        if link.target is None:
            obj, ambiguous = match(link, self.match_queryset(kind), name, self.narrow(kind))
            if obj is not None:  # unique name match: link right away, NetBox itself is not changed here
                setattr(link, 'device' if kind == 'node' else 'virtual_machine', obj)
                self.logger.info(_('%(name)s linked to the existing NetBox object by name.') % {'name': name})
            elif not ambiguous:
                link.suggested_pk = suggest(self.suggest_queryset(kind), name, link.rejected_pks)
        link.save()
        if ambiguous:
            ProxmoxObject.objects.filter(pk=link.pk).update(state='ambiguous', changes=[], message=ambiguous[:500])
            self.counts['ambiguous'] += 1
        elif link.suggested_pk:
            ProxmoxObject.objects.filter(pk=link.pk).update(state='suggested', changes=[], message=_(
                'A NetBox object with this name apart from case or in another cluster exists – accept or reject '
                'the suggestion.'))
            self.counts['suggested'] += 1
        else:
            self.evaluate(link)

    def evaluate(self, link):
        """Preview the link and store state + pending changes."""
        linked = link.target is not None
        changes, error = self.preview(link)
        state = 'error' if error else 'pending' if linked and changes else 'ok' if linked else 'new'
        ProxmoxObject.objects.filter(pk=link.pk).update(state=state, changes=changes, message=error[:500])
        self.counts[state] += 1
        return state

    def preview(self, link):
        """Run the real write code in a savepoint and roll it back, recording what would change.
        Runs without request context: no changelog entries, no webhooks/event rules."""
        changes = []
        token = _RECORD.set(changes)
        self.dry = True
        try:
            with transaction.atomic():
                self.process(link)
                raise _Rollback
        except _Rollback:
            return changes, ''
        except Exception as exc:  # noqa: BLE001 - shown as error state, the user decides
            return changes, error_text(exc)
        finally:
            self.dry = False
            _RECORD.reset(token)

    def existing_cluster(self):
        cid = type(self.conn).objects.filter(pk=self.conn.pk).values_list('cluster_id', flat=True).first()
        if cid:
            return Cluster.objects.filter(pk=cid).first()
        return Cluster.objects.filter(name__iexact=self.cluster_name(), type__slug='proxmox-ve').first()

    def cluster_name(self):
        return self.conn.proxmox_objects.filter(kind='cluster').values_list('name', flat=True).first() or self.conn.name

    def match_queryset(self, kind):
        """Name matching only within this cluster or among objects without a cluster (never another cluster)."""
        cluster = self.existing_cluster()
        scope = Q(cluster__isnull=True) | Q(cluster=cluster) if cluster else Q(cluster__isnull=True)
        if kind == 'node':
            return Device.objects.filter(scope, site=self.conn.site)
        return VirtualMachine.objects.filter(scope)

    def suggest_queryset(self, kind):
        """Suggestions may come from any cluster, nodes only from the connection's site."""
        return Device.objects.filter(site=self.conn.site) if kind == 'node' else VirtualMachine.objects.all()

    def narrow(self, kind):
        cluster = self.existing_cluster()
        return lambda obj: cluster is not None and obj.cluster_id == cluster.pk

    def cluster_snapshot(self, name, nodes, storage):
        online = [n for n in nodes if n.get('status') == 'online']
        cpus = sum(n.get('maxcpu') or 0 for n in online)
        ProxmoxObject.objects.update_or_create(connection=self.conn, kind='cluster', proxmox_id='cluster', defaults={
            'name': name, 'state': 'ok', 'last_seen': self.now, 'cpu_count': cpus,
            'cpu_usage': round(sum((n.get('cpu') or 0) * (n.get('maxcpu') or 0) for n in online) / cpus * 100, 1)
            if cpus else None,
            'mem_used': sum(n.get('mem') or 0 for n in online), 'mem_total': sum(n.get('maxmem') or 0 for n in online),
            'disk_used': storage[0], 'disk_total': storage[1], 'uptime': None,
        })

    def mark_stale(self, seen):
        """Gone from Proxmox: linked objects become stale (orphans), unlinked rows are dropped."""
        for link in self.conn.proxmox_objects.exclude(kind='cluster'):
            if (link.kind, link.proxmox_id) in seen:
                continue
            if link.target is None:
                link.delete()
            elif link.state != 'stale':
                ProxmoxObject.objects.filter(pk=link.pk).update(
                    state='stale', changes=[], message=_('No longer present in Proxmox'))
                self.counts['stale'] += 1
                self.logger.warning(f'{link.kind} {link.proxmox_id} ({link.name}): no longer present in Proxmox')

    # == phase 2: write the fetched data to NetBox ===============================================
    def write(self, links):
        """Apply the stored data of the given links (nodes first). Ignored, ambiguous, suggested and stale ones are
        skipped: a suggestion must be accepted or rejected first, otherwise a duplicate would be created."""
        order = {'node': 0}
        token = _USER.set(self.user)
        try:
            return self._write(sorted(links, key=lambda x: order.get(x.kind, 1)))
        finally:
            _USER.reset(token)

    def _write(self, links):
        for link in links:
            if link.ignored or link.kind == 'cluster' or link.state in ('ambiguous', 'suggested', 'stale') or not link.data:
                self.counts['skipped'] += 1
                continue
            try:
                with transaction.atomic():
                    result = self.process(link)
            except Exception as exc:  # noqa: BLE001 - one broken object must not stop the run
                message = error_text(exc)
                ProxmoxObject.objects.filter(pk=link.pk).update(state='error', message=message[:500])
                self.counts['error'] += 1
                self.logger.error(f'{link.kind} {link.proxmox_id} ({link.name}): {message}')
                continue
            ProxmoxObject.objects.filter(pk=link.pk).update(state='ok', changes=[], message='')
            self.counts[result] += 1
        return dict(self.counts)

    def offline_stale(self):
        """Daily run: NetBox objects whose Proxmox counterpart is gone are set offline (never deleted)."""
        for link in self.conn.proxmox_objects.filter(state='stale'):
            target = link.target
            if target is not None and target.status != 'offline':
                with transaction.atomic():
                    apply(target, status='offline')
                self.counts['offline'] += 1

    def process(self, link):
        """Write link.data to NetBox. Returns 'created' | 'updated' | 'unchanged'."""
        self.cluster = self.ensure_cluster()
        if link.kind == 'node':
            return self.sync_node(link, link.data)
        return self.sync_guest(link, link.data)

    def ensure_cluster(self):
        cluster = self.existing_cluster()
        token = _RECORD.set(None)  # cluster bookkeeping is not part of one object's preview
        try:
            if cluster is None:
                cluster = Cluster(type=get_or_create_named(ClusterType, 'Proxmox VE'), scope=self.conn.site,
                                  tenant=self.conn.tenant)
                self.log.info(_('Creating cluster %(name)s') % {'name': self.cluster_name()})
            apply(cluster, name=self.cluster_name(), status='active')
        finally:
            _RECORD.reset(token)
        if self.conn.cluster_id != cluster.pk:
            type(self.conn).objects.filter(pk=self.conn.pk).update(cluster=cluster)
        return cluster

    @staticmethod
    def check_change(obj):
        """The user must be allowed to change the synced object itself, even if only its components change."""
        if obj is not None:
            permit(obj, 'change')

    @staticmethod
    def link_target(link, obj):
        field = 'device' if isinstance(obj, Device) else 'virtual_machine'
        if getattr(link, field + '_id') != obj.pk:
            setattr(link, field, obj)
            link.save()

    # -- nodes ---------------------------------------------------------------------------------
    def sync_node(self, link, payload):
        r = payload['resource']
        name, online = r['node'], r.get('status') == 'online'
        device = Device.objects.filter(pk=link.device_id).first() if link.device_id else None  # fresh copy
        self.check_change(device)
        fields = {'cluster': self.cluster, 'status': 'active' if online else 'offline'}
        m = re.search(r'pve-manager/(\d+\.\d+)', payload.get('pveversion') or '')
        if m:
            fields['platform'] = get_or_create_named(Platform, f'Proxmox VE {m.group(1)}')
        created = device is None
        if created:
            device = Device(name=name, site=self.conn.site, role=self.conn.node_role,
                            device_type=self.conn.node_device_type, tenant=self.conn.tenant)
            self.log.info(_('Creating device %(name)s') % {'name': name})
        changed = apply(device, **fields)
        self.link_target(link, device)
        if 'network' in payload:
            changed |= self.sync_node_interfaces(device, payload['network'])
        return 'created' if created else 'updated' if changed else 'unchanged'

    def sync_node_interfaces(self, device, network):
        """Create missing interfaces (existing ones keep their type) and sync their IPs."""
        changed = False
        for n in network:
            if n.get('type') == 'alias' or not n.get('iface'):
                continue
            iface = device.interfaces.filter(name=n['iface']).first()
            if iface is None:
                iface = Interface(device=device, name=n['iface'], type=NODE_IFACE_TYPES.get(n.get('type'), 'other'))
                changed |= apply(iface, enabled=bool(n.get('active', 1)), description=(n.get('comments') or '')[:200])
            ips = [ip for ip in (usable_ip(n.get('cidr')), usable_ip(n.get('cidr6'))) if ip]
            sync_ips(device, iface, ips, self.log)
        set_primary_ips(device, IPAddress.objects.filter(interface__device=device).order_by('pk'))
        return changed

    # -- guests --------------------------------------------------------------------------------
    def sync_guest(self, link, payload):
        r, config = payload['resource'], payload.get('config')
        kind, vmid, node, name = r['type'], r['vmid'], r.get('node'), link.name
        vm = VirtualMachine.objects.filter(pk=link.virtual_machine_id).first() if link.virtual_machine_id else None
        self.check_change(vm)
        fields = {
            'name': name, 'cluster': self.cluster, 'status': VM_STATUS.get(r.get('status'), 'offline'),
            'device': self.host_for(vm, node, name),
            'virtual_machine_type': get_or_create_named(VirtualMachineType, 'Proxmox ' + ('QEMU' if kind == 'qemu' else 'LXC')),
            'vcpus': r.get('maxcpu') or None, 'memory': (r.get('maxmem') or 0) // MIB or None,
        }
        if config is not None:
            platform = platform_name(config.get('ostype'))
            fields['platform'] = get_or_create_named(Platform, platform) if platform else None
            fields['start_on_boot'] = 'on' if str(config.get('onboot')) == '1' else 'off'
            if kind == 'qemu':
                fields['serial'] = parse_kv(config.get('smbios1')).get('uuid', '')[:50]
        created = vm is None
        if created:
            vm = VirtualMachine(role=self.conn.vm_role if kind == 'qemu' else self.conn.container_role,
                                tenant=self.conn.tenant)
            self.log.info(_('Creating virtual machine %(name)s') % {'name': name})
        old_host = None if created else vm.device
        changed = apply(vm, **fields)
        if old_host and vm.device and old_host != vm.device and not self.dry:
            self.counts['moved'] += 1
            self.log.info(_('%(name)s moved from %(old)s to %(new)s') % {'name': name, 'old': old_host, 'new': vm.device})
        if vm.custom_field_data.get(CF_VMID) != vmid:
            permit(vm, 'change')
            vm.snapshot()
            vm.custom_field_data[CF_VMID] = vmid
            vm.save()
        self.link_target(link, vm)
        add_tags(vm, r.get('tags'), self.log)
        if config is not None:
            changed |= self.sync_disks(vm, parse_disks(config, kind))
            live = payload.get('live')
            if live is not None:
                live = {mac: [ipaddress.ip_interface(ip) for ip in ips] for mac, ips in live.items()}
            changed |= self.sync_vm_interfaces(vm, parse_nics(config, kind), live)
        return 'created' if created else 'updated' if changed else 'unchanged'

    def host_for(self, vm, node, name):
        """Node device the guest currently runs on (follows migrations). If that node is not linked in NetBox,
        keep the previous assignment instead of clearing it - as long as it still belongs to this cluster."""
        device_id = self.conn.proxmox_objects.filter(kind='node', proxmox_id=node, device__isnull=False) \
            .values_list('device_id', flat=True).first()
        host = Device.objects.filter(pk=device_id).first() if device_id else None
        if host and host.cluster_id == self.cluster.pk:
            return host
        self.log.warning(_('%(name)s: node %(node)s is not linked to a device in cluster %(cluster)s – '
                           'host assignment unchanged.') % {'name': name, 'node': node, 'cluster': self.cluster})
        previous = vm.device if vm is not None else None
        return previous if previous and previous.cluster_id == self.cluster.pk else None

    def sync_disks(self, vm, disks):
        changed = False
        wanted = dict(disks)
        for disk in vm.virtualdisks.all():
            if disk.name not in wanted and (QEMU_DISK.match(disk.name) or LXC_DISK.match(disk.name)):
                permit(disk, 'delete')
                record(f'− {_label(disk)}')
                disk.snapshot()
                disk.delete()
                changed = True
        for name, size in disks:
            disk = vm.virtualdisks.filter(name=name).first() or VirtualDisk(virtual_machine=vm, name=name)
            changed |= apply(disk, size=size)
        return changed

    def sync_vm_interfaces(self, vm, nics, live):
        changed, budget = False, MAX_IPS_PER_VM
        wanted = {n['name'] for n in nics}
        for iface in vm.interfaces.filter(name__regex=r'^net\d+$').exclude(name__in=wanted):
            sync_ips(vm, iface, [], self.log)  # deleting the interface would delete its IPs
            permit(iface, 'delete')
            record(f'− {_label(iface)}')
            iface.snapshot()
            iface.delete()
            changed = True
        for nic in nics:
            iface = vm.interfaces.filter(name=nic['name']).first() or VMInterface(virtual_machine=vm, name=nic['name'])
            desc = ' / '.join(x for x in (nic['guest_name'], nic['bridge']) if x)
            fields = {'enabled': nic['enabled'], 'mtu': nic['mtu'], 'description': desc[:200]}
            if nic['vlan']:
                vlans = list(VLAN.objects.filter(Q(site__isnull=True) | Q(site_id=vm.site_id), vid=nic['vlan'])[:2])
                if len(vlans) == 1:
                    fields.update(mode='access', untagged_vlan=vlans[0])
            changed |= apply(iface, **fields)
            old_mac = str(getattr(iface.primary_mac_address, 'mac_address', '') or '').lower()
            if nic['mac'] and old_mac != nic['mac']:
                known = iface.mac_addresses.filter(mac_address=nic['mac']).exists()
                permit(iface, 'change')
                iface.set_primary_mac_address_from_value(nic['mac'])
                permit(iface.primary_mac_address, 'change' if known else 'add')
                record(f'{_label(iface)}: MAC {_fmt(old_mac)} → {nic["mac"]}')
                changed = True
            ips = live.get(nic['mac'], []) if live is not None else (
                [ip for ip in map(usable_ip, nic['static']) if ip] or None)
            if ips is not None:
                limit = min(MAX_IPS_PER_IFACE, budget)
                if len(ips) > limit:
                    self.log.warning(_('%(vm)s %(iface)s: %(n)d IP addresses reported, only %(limit)d taken.') % {
                        'vm': vm, 'iface': nic['name'], 'n': len(ips), 'limit': limit})
                    ips = ips[:limit]
                budget -= len(ips)
                sync_ips(vm, iface, ips, self.log, guest=True)
        set_primary_ips(vm, IPAddress.objects.filter(vminterface__virtual_machine=vm).order_by('pk'))
        return changed


# -- entry points ----------------------------------------------------------------------------
def discover(conn, client, logger):
    """Fetch from Proxmox; writes only ProxmoxObject (state, pending changes, usage)."""
    return Sync(conn, logger, client=client).discover()


def write_objects(links, logger, user=None):
    """Apply the fetched data of the given ProxmoxObjects. user=None: system run without permission checks."""
    counts = Counter()
    by_conn = {}
    for link in links:
        by_conn.setdefault(link.connection, []).append(link)
    for conn, items in by_conn.items():
        counts.update(Sync(conn, logger, user=user).write(items))
    return dict(counts)


def assign(link, target, logger):
    """Link a Proxmox object to an existing NetBox object (manual). Only the link is stored; the pending
    changes are previewed and written by the next sync."""
    field = 'device' if link.kind == 'node' else 'virtual_machine'
    model = Device if link.kind == 'node' else VirtualMachine
    if not isinstance(target, model):
        raise ValidationError(_('A %(kind)s can only be assigned to a %(model)s.') % {
            'kind': link.get_kind_display(), 'model': model._meta.verbose_name})
    other = ProxmoxObject.objects.filter(**{field: target}).exclude(pk=link.pk).first()
    if other is not None:
        raise ValidationError(_('%(obj)s is already linked to %(other)s.') % {'obj': target, 'other': other})
    setattr(link, field, target)
    link.suggested_pk = None
    link.save()
    return Sync(link.connection, logger).evaluate(link)


def reject_suggestion(link, logger):
    """Do not suggest this object again; the Proxmox object is then treated as new."""
    link.rejected_pks = [*link.rejected_pks, link.suggested_pk]
    link.suggested_pk = None
    link.save()
    return Sync(link.connection, logger).evaluate(link)
