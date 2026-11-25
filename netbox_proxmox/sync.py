"""
Proxmox -> NetBox sync. Proxmox is the source for the fields set here; everything else on the NetBox objects
(tenant, role after creation, description, comments, other tags, custom fields) is left alone.

Matching: existing ProxmoxObject link -> unique name -> create. Objects gone from Proxmox are never deleted,
only marked stale and set offline.
"""
import ipaddress
import re

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


# -- NetBox write helpers ------------------------------------------------------------------
def apply(obj, **fields):
    """Set fields and save (with changelog snapshot) only if something changed. Returns True if saved."""
    if obj.pk and all(getattr(obj, k) == v for k, v in fields.items()):
        return False
    if obj.pk:
        obj.snapshot()
    for k, v in fields.items():
        setattr(obj, k, v)
    obj.full_clean()
    obj.save()
    return True


def get_or_create_named(model, name, **extra):
    """Lookup by slug or name (case-insensitive), create if missing. For types/platforms/tags."""
    slug = slugify(name)[:100]
    obj = model.objects.filter(slug=slug).first() or model.objects.filter(name__iexact=name).first()
    if obj is None:
        obj = model(name=name, slug=slug, **extra)
        obj.full_clean()
        obj.save()
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
        obj.snapshot()
        obj.tags.add(*to_add)


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
    candidates = list(queryset.filter(name__iexact=name, proxmox_object__isnull=True)[:20])
    if len(candidates) > 1:
        narrowed = [c for c in candidates if narrow(c)]
        if len(narrowed) == 1:
            return narrowed[0], None
        return None, _('%(n)d NetBox objects named %(name)s – assign manually or rename.') % {
            'n': len(candidates), 'name': name}
    return (candidates[0] if candidates else None), None


# -- sync ------------------------------------------------------------------------------------
class Sync:
    def __init__(self, conn, client, logger):
        self.conn, self.client, self.logger = conn, client, logger
        self.counts = {'created': 0, 'updated': 0, 'unchanged': 0, 'ambiguous': 0, 'error': 0, 'stale': 0, 'moved': 0}
        self.now = timezone.now()

    def run(self):
        resources = self.client.get('cluster', 'resources') or []
        self.cluster = self.sync_cluster()
        node_storage, cluster_storage = storage_usage(resources)
        nodes = [r for r in resources if r.get('type') == 'node']
        guests = [r for r in resources if r.get('type') in ('qemu', 'lxc') and not r.get('template')]
        seen = set()
        self.node_devices = {}
        for r in nodes:
            u = usage(r)
            if r['node'] in node_storage:
                u['disk_used'], u['disk_total'] = node_storage[r['node']]
            seen.add(('node', r['node']))
            link = self.guarded('node', r['node'], r['node'], r['node'], u, lambda link, r=r: self.sync_node(link, r))
            if link.device_id:
                self.node_devices[r['node']] = link.device
        for r in guests:
            seen.add((r['type'], str(r['vmid'])))
            name = r.get('name') or f"{r['type']}-{r['vmid']}"
            self.guarded(r['type'], r['vmid'], name, r.get('node', ''), usage(r),
                         lambda link, r=r, name=name: self.sync_guest(link, r, name))
        self.cluster_snapshot(nodes, cluster_storage)
        self.mark_stale(seen)
        # queryset update: bookkeeping, not worth a changelog entry per run
        type(self.conn).objects.filter(pk=self.conn.pk).update(last_sync=self.now)
        return self.counts

    def guarded(self, kind, pid, name, node, usage_fields, fn):
        """Run fn(link) in its own transaction; a failure marks only this object as 'error'."""
        link, _created = ProxmoxObject.objects.get_or_create(
            connection=self.conn, kind=kind, proxmox_id=str(pid), defaults={'state': 'ok'})
        try:
            with transaction.atomic():
                state, message = fn(link)
        except Exception as exc:  # noqa: BLE001 - one broken object must not stop the run
            link = ProxmoxObject.objects.get(pk=link.pk)
            state, message = 'error', '; '.join(getattr(exc, 'messages', None) or [str(exc)])
            self.logger.error(f'{kind} {pid} ({name}): {message}')
        self.counts['updated' if state == 'ok-changed' else 'unchanged' if state == 'ok' else state] += 1
        link.state = 'ok' if state.startswith('ok') else state
        link.name, link.node, link.message, link.last_seen = name, node, message[:500], self.now
        for k, v in usage_fields.items():
            setattr(link, k, v)
        link.save()
        return link

    @staticmethod
    def link_target(link, obj):
        field = 'device' if isinstance(obj, Device) else 'virtual_machine'
        if getattr(link, field + '_id') != obj.pk:
            setattr(link, field, obj)
            link.save()

    # -- cluster -------------------------------------------------------------------------------
    def sync_cluster(self):
        status = self.client.get('cluster', 'status') or []
        name = next((s['name'] for s in status if s.get('type') == 'cluster'), None) or self.conn.name
        cluster_type = get_or_create_named(ClusterType, 'Proxmox VE')
        cluster = self.conn.cluster or Cluster.objects.filter(name__iexact=name, type=cluster_type).first()
        with transaction.atomic():
            if cluster is None:
                cluster = Cluster(type=cluster_type, scope=self.conn.site, tenant=self.conn.tenant)
                self.logger.info(_('Creating cluster %(name)s') % {'name': name})
            apply(cluster, name=name, status='active')
        if self.conn.cluster_id != cluster.pk:
            self.conn.cluster = cluster
            type(self.conn).objects.filter(pk=self.conn.pk).update(cluster=cluster)
        return cluster

    def cluster_snapshot(self, nodes, storage):
        online = [n for n in nodes if n.get('status') == 'online']
        cpus = sum(n.get('maxcpu') or 0 for n in online)
        ProxmoxObject.objects.update_or_create(connection=self.conn, kind='cluster', proxmox_id='cluster',
                                               defaults={
            'name': self.cluster.name, 'state': 'ok', 'last_seen': self.now, 'cpu_count': cpus,
            'cpu_usage': round(sum((n.get('cpu') or 0) * (n.get('maxcpu') or 0) for n in online) / cpus * 100, 1)
            if cpus else None,
            'mem_used': sum(n.get('mem') or 0 for n in online), 'mem_total': sum(n.get('maxmem') or 0 for n in online),
            'disk_used': storage[0], 'disk_total': storage[1], 'uptime': None,
        })

    # -- nodes ---------------------------------------------------------------------------------
    def sync_node(self, link, r):
        name, online = r['node'], r.get('status') == 'online'
        scope = Q(cluster__isnull=True) | Q(cluster=self.cluster)
        device, ambiguous = match(link, Device.objects.filter(scope, site=self.conn.site), name, lambda d: True)
        if ambiguous:
            return 'ambiguous', ambiguous
        fields = {'cluster': self.cluster, 'status': 'active' if online else 'offline'}
        if online:
            version = (self.client.get('nodes', name, 'status') or {}).get('pveversion', '')
            m = re.search(r'pve-manager/(\d+\.\d+)', version)
            if m:
                fields['platform'] = get_or_create_named(Platform, f'Proxmox VE {m.group(1)}')
        created = device is None
        if created:
            device = Device(name=name, site=self.conn.site, role=self.conn.node_role,
                            device_type=self.conn.node_device_type, tenant=self.conn.tenant)
            self.logger.info(_('Creating device %(name)s') % {'name': name})
        changed = apply(device, **fields)
        self.link_target(link, device)
        if online:
            changed |= self.sync_node_interfaces(device, self.client.get('nodes', name, 'network') or [])
        return ('created', '') if created else ('ok-changed' if changed else 'ok', '')

    def sync_node_interfaces(self, device, network):
        """Create missing interfaces (existing ones keep their type) and sync their IPs."""
        changed = False
        for n in network:
            if n.get('type') == 'alias' or not n.get('iface'):
                continue
            iface = device.interfaces.filter(name=n['iface']).first()
            if iface is None:
                iface = Interface(device=device, name=n['iface'], type=NODE_IFACE_TYPES.get(n.get('type'), 'other'))
                changed |= apply(iface, enabled=bool(n.get('active', 1)), description=n.get('comments', '')[:200])
            ips = [ip for ip in (usable_ip(n.get('cidr')), usable_ip(n.get('cidr6'))) if ip]
            sync_ips(device, iface, ips, self.logger)
        set_primary_ips(device, IPAddress.objects.filter(interface__device=device).order_by('pk'))
        return changed

    # -- guests --------------------------------------------------------------------------------
    def sync_guest(self, link, r, name):
        kind, vmid, node = r['type'], r['vmid'], r.get('node')
        scope = Q(cluster__isnull=True) | Q(cluster=self.cluster)
        vm, ambiguous = match(link, VirtualMachine.objects.filter(scope), name, lambda v: v.cluster_id == self.cluster.pk)
        if ambiguous:
            return 'ambiguous', ambiguous
        try:
            config = self.client.get('nodes', node, kind, vmid, 'config') or {}
        except ProxmoxError as exc:  # e.g. node offline: keep what NetBox has, only update status/usage
            config = None
            self.logger.warning(f'{kind} {vmid} ({name}): {exc}')
        fields = {
            'name': name[:64], 'cluster': self.cluster, 'status': VM_STATUS.get(r.get('status'), 'offline'),
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
            self.logger.info(_('Creating virtual machine %(name)s') % {'name': name})
        old_host = None if created else vm.device
        changed = apply(vm, **fields)
        if old_host and vm.device and old_host != vm.device:
            self.counts['moved'] += 1
            self.logger.info(_('%(name)s moved from %(old)s to %(new)s') % {'name': name, 'old': old_host,
                                                                              'new': vm.device})
        if vm.custom_field_data.get(CF_VMID) != vmid:
            vm.snapshot()
            vm.custom_field_data[CF_VMID] = vmid
            vm.save()
        self.link_target(link, vm)
        add_tags(vm, r.get('tags'), self.logger)
        if config is not None:
            changed |= self.sync_disks(vm, parse_disks(config, kind))
            changed |= self.sync_vm_interfaces(vm, parse_nics(config, kind), self.live_ips(kind, node, vmid, r))
        return ('created', '') if created else ('ok-changed' if changed else 'ok', '')

    def host_for(self, vm, node, name):
        """Node device the guest currently runs on (follows migrations). If that node is not linked in NetBox,
        keep the previous assignment instead of clearing it - as long as it still belongs to this cluster."""
        host = self.node_devices.get(node)
        if host and host.cluster_id == self.cluster.pk:
            return host
        self.logger.warning(_('%(name)s: node %(node)s is not linked to a device in cluster %(cluster)s – '
                              'host assignment unchanged.') % {'name': name, 'node': node, 'cluster': self.cluster})
        previous = vm.device if vm is not None else None
        return previous if previous and previous.cluster_id == self.cluster.pk else None

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

    def sync_disks(self, vm, disks):
        changed = False
        wanted = dict(disks)
        for disk in vm.virtualdisks.all():
            if disk.name not in wanted and (QEMU_DISK.match(disk.name) or LXC_DISK.match(disk.name)):
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
            sync_ips(vm, iface, [], self.logger)  # deleting the interface would delete its IPs
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
            if nic['mac'] and str(getattr(iface.primary_mac_address, 'mac_address', '')).lower() != nic['mac']:
                iface.set_primary_mac_address_from_value(nic['mac'])
                changed = True
            ips = live.get(nic['mac'], []) if live is not None else (
                [ip for ip in map(usable_ip, nic['static']) if ip] or None)
            if ips is not None:
                limit = min(MAX_IPS_PER_IFACE, budget)
                if len(ips) > limit:
                    self.logger.warning(_('%(vm)s %(iface)s: %(n)d IP addresses reported, only %(limit)d taken.') % {
                        'vm': vm, 'iface': nic['name'], 'n': len(ips), 'limit': limit})
                    ips = ips[:limit]
                budget -= len(ips)
                sync_ips(vm, iface, ips, self.logger, guest=True)
        set_primary_ips(vm, IPAddress.objects.filter(vminterface__virtual_machine=vm).order_by('pk'))
        return changed

    # -- stale ---------------------------------------------------------------------------------
    def mark_stale(self, seen):
        for link in self.conn.proxmox_objects.exclude(kind='cluster').exclude(state='stale'):
            if (link.kind, link.proxmox_id) in seen:
                continue
            target = link.target
            with transaction.atomic():
                if target is not None:
                    apply(target, status='offline')
                link.state, link.message = 'stale', _('No longer present in Proxmox')
                link.save()
            self.counts['stale'] += 1
            self.logger.warning(f'{link.kind} {link.proxmox_id} ({link.name}): ' + link.message)


def sync_connection(conn, client, logger):
    return Sync(conn, client, logger).run()
