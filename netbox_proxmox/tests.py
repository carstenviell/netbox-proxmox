import copy
from collections import Counter
from unittest import mock
import ipaddress
import logging

from django.test import TestCase

from core.models import ObjectChange
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from ipam.models import IPAddress
from tenancy.models import Tenant
from users.models import User
from extras.models import Tag
from virtualization.models import Cluster, ClusterType, VirtualMachine

from .client import ProxmoxClient, ProxmoxError
from .custom_fields import CF_VMID, ensure_custom_fields
from .jobs import changelog_context
from .models import IgnoredOrphan, ProxmoxConnection, ProxmoxObject
from .sync import (TAG_MARKER, Sync, agent_ips, assign, discover, parse_disks, parse_nics, reject_suggestion, size_mb,
                   storage_usage)

LOG = logging.getLogger('netbox_proxmox.tests')
MAC1, MAC2 = 'bc:24:11:00:00:01', 'bc:24:11:00:00:02'
G = 1024 ** 3

# Self-made minimal API answers
API = {
    ('cluster', 'status'): [{'type': 'cluster', 'name': 'pve-lab'}, {'type': 'node', 'name': 'pve1'}],
    ('cluster', 'resources'): [
        {'type': 'node', 'node': 'pve1', 'status': 'online', 'cpu': 0.25, 'maxcpu': 8, 'mem': 8 * G, 'maxmem': 32 * G},
        {'type': 'node', 'node': 'pve2', 'status': 'online', 'cpu': 0.75, 'maxcpu': 8, 'mem': 16 * G, 'maxmem': 32 * G},
        {'type': 'storage', 'node': 'pve1', 'storage': 'local', 'status': 'available', 'shared': 0,
         'disk': 10 * G, 'maxdisk': 100 * G},
        {'type': 'storage', 'node': 'pve1', 'storage': 'ceph', 'status': 'available', 'shared': 1,
         'disk': 500 * G, 'maxdisk': 1000 * G},
        {'type': 'storage', 'node': 'pve2', 'storage': 'ceph', 'status': 'available', 'shared': 1,
         'disk': 500 * G, 'maxdisk': 1000 * G},
        {'type': 'qemu', 'vmid': 101, 'name': 'web01', 'node': 'pve1', 'status': 'running', 'maxcpu': 4,
         'maxmem': 4 * G, 'cpu': 0.1, 'mem': G, 'tags': 'prod;web'},
        {'type': 'lxc', 'vmid': 200, 'name': 'ct01', 'node': 'pve2', 'status': 'stopped', 'maxcpu': 2, 'maxmem': G},
        {'type': 'qemu', 'vmid': 900, 'name': 'tpl', 'node': 'pve1', 'template': 1},
    ],
    ('nodes', 'pve1', 'status'): {'pveversion': 'pve-manager/8.2.4/faa83925c9641325'},
    ('nodes', 'pve2', 'status'): {'pveversion': 'pve-manager/8.2.4/faa83925c9641325'},
    ('nodes', 'pve1', 'network'): [{'iface': 'vmbr0', 'type': 'bridge', 'cidr': '192.168.10.11/24', 'active': 1}],
    ('nodes', 'pve2', 'network'): [{'iface': 'vmbr0', 'type': 'bridge', 'cidr': '192.168.10.12/24', 'active': 1}],
    ('nodes', 'pve1', 'qemu', 101, 'config'): {
        'ostype': 'l26', 'onboot': 1, 'smbios1': 'uuid=1b2c3d4e-0000-4000-8000-000000000101',
        'scsi0': 'ceph:vm-101-disk-0,size=32G', 'ide2': 'none,media=cdrom', 'net0': f'virtio={MAC1.upper()},bridge=vmbr0',
    },
    ('nodes', 'pve1', 'qemu', 101, 'agent', 'network-get-interfaces'): {'result': [
        {'name': 'lo', 'hardware-address': '00:00:00:00:00:00',
         'ip-addresses': [{'ip-address': '127.0.0.1', 'prefix': 8}]},
        {'name': 'eth0', 'hardware-address': MAC1, 'ip-addresses': [
            {'ip-address': '10.0.0.5', 'prefix': 24}, {'ip-address': 'fe80::1', 'prefix': 64}]},
    ]},
    ('nodes', 'pve2', 'lxc', 200, 'config'): {
        'ostype': 'debian', 'rootfs': 'local:200/vm-200-disk-0.raw,size=8G',
        'net0': f'name=eth0,bridge=vmbr0,hwaddr={MAC2},ip=10.0.0.6/24,type=veth',
    },
}


class FakeClient:
    def __init__(self, api):
        self.api = api

    def get(self, *parts):
        if parts not in self.api:
            raise ProxmoxError(f'HTTP 500: not available {parts}', status=500)
        return copy.deepcopy(self.api[parts])


def grant(user, model, actions, constraints=None):
    """NetBox evaluates permissions via ObjectPermission only (Django user_permissions are ignored)."""
    from django.contrib.contenttypes.models import ContentType
    from users.models import ObjectPermission
    perm = ObjectPermission.objects.create(name=f'test {model.__name__} {actions}', actions=actions,
                                           constraints=constraints)
    perm.object_types.add(ContentType.objects.get_for_model(model))
    perm.users.add(user)


def full_sync(conn, api, user):
    """Old one-step behaviour for the domain tests: fetch, then write everything new/pending."""
    counts = Counter(discover(conn, FakeClient(api), LOG))  # fetch runs outside the changelog context (as the job)
    with changelog_context(user):
        sync = Sync(conn, LOG)
        sync.write(list(conn.proxmox_objects.filter(state__in=['new', 'pending'])))
        sync.offline_stale()
    counts.update(sync.counts)
    return counts


class ParserTests(TestCase):
    def test_size(self):
        self.assertEqual(size_mb('32G'), 32768)
        self.assertEqual(size_mb('512M'), 512)
        self.assertEqual(size_mb('1T'), 1024 ** 2)
        self.assertEqual(size_mb('2048K'), 2)
        self.assertEqual(size_mb(str(1024 ** 2)), 1)  # bytes
        self.assertIsNone(size_mb('lots'))

    def test_disks(self):
        cfg = API[('nodes', 'pve1', 'qemu', 101, 'config')]
        self.assertEqual(parse_disks(cfg, 'qemu'), [('scsi0', 32768)])
        self.assertEqual(parse_disks({'rootfs': 'x,size=8G', 'mp0': 'y,size=1T', 'unused0': 'z'}, 'lxc'),
                         [('mp0', 1024 ** 2), ('rootfs', 8192)])

    def test_nics(self):
        q = parse_nics({'net1': 'e1000=AA:BB:CC:DD:EE:FF,bridge=vmbr1,tag=20,link_down=1,mtu=9000',
                        'net0': f'virtio={MAC1},bridge=vmbr0'}, 'qemu')
        self.assertEqual([n['name'] for n in q], ['net0', 'net1'])
        self.assertEqual(q[1]['mac'], 'aa:bb:cc:dd:ee:ff')
        self.assertEqual((q[1]['vlan'], q[1]['mtu'], q[1]['enabled']), (20, 9000, False))
        lxc = parse_nics(API[('nodes', 'pve2', 'lxc', 200, 'config')], 'lxc')[0]
        self.assertEqual((lxc['mac'], lxc['guest_name'], lxc['static']), (MAC2, 'eth0', ['10.0.0.6/24']))

    def test_agent_ips_skip_loopback_and_link_local(self):
        ips = agent_ips(API[('nodes', 'pve1', 'qemu', 101, 'agent', 'network-get-interfaces')]['result'])
        self.assertEqual(ips, {MAC1: [ipaddress.ip_interface('10.0.0.5/24')]})

    def test_storage_shared_counted_once(self):
        per_node, cluster = storage_usage(API[('cluster', 'resources')])
        self.assertEqual(per_node['pve1'], (10 * G, 100 * G))
        self.assertEqual(cluster, (510 * G, 1100 * G))


class SyncTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ensure_custom_fields()
        cls.site = Site.objects.create(name='Lab', slug='lab')
        mf = Manufacturer.objects.create(name='Generic', slug='generic')
        cls.dt = DeviceType.objects.create(manufacturer=mf, model='Server', slug='server')
        cls.role = DeviceRole.objects.create(name='Hypervisor', slug='hypervisor')
        cls.vm_role = DeviceRole.objects.create(name='VM', slug='vm', vm_role=True)
        cls.user = User.objects.create_user(username='tester')

    def setUp(self):
        self.api = copy.deepcopy(API)
        self.conn = ProxmoxConnection.objects.create(
            name='lab', url='https://pve.example.local:8006', token_id='root@pam!netbox', site=self.site,
            node_role=self.role, node_device_type=self.dt, vm_role=self.vm_role)

    def run_sync(self):
        return full_sync(self.conn, self.api, self.user)

    def fetch(self):
        return discover(self.conn, FakeClient(self.api), LOG)

    def link(self, pid):
        return ProxmoxObject.objects.get(connection=self.conn, proxmox_id=str(pid))

    def resource(self, vmid):
        return next(r for r in self.api[('cluster', 'resources')] if r.get('vmid') == vmid)

    def test_full_import(self):
        counts = self.run_sync()
        self.assertEqual(counts['created'], 4)
        self.assertEqual(counts['error'], 0)
        cluster = ProxmoxConnection.objects.get(pk=self.conn.pk).cluster
        self.assertEqual(cluster.name, 'pve-lab')
        pve1 = Device.objects.get(name='pve1')
        self.assertEqual((pve1.cluster, pve1.platform.name), (cluster, 'Proxmox VE 8.2'))
        self.assertEqual(str(pve1.primary_ip4.address), '192.168.10.11/24')

        web = VirtualMachine.objects.get(name='web01')
        self.assertEqual((web.cluster, web.device, web.status, web.role), (cluster, pve1, 'active', self.vm_role))
        self.assertEqual((web.vcpus, web.memory, web.disk, web.start_on_boot), (4, 4096, 32768, 'on'))
        self.assertEqual(web.serial, '1b2c3d4e-0000-4000-8000-000000000101')
        self.assertEqual(web.virtual_machine_type.name, 'Proxmox QEMU')
        self.assertEqual(web.custom_field_data[CF_VMID], 101)
        self.assertEqual(sorted(web.tags.names()), ['prod', 'web'])
        net0 = web.interfaces.get(name='net0')
        self.assertEqual(str(net0.primary_mac_address.mac_address).lower(), MAC1)
        self.assertEqual([str(ip.address) for ip in net0.ip_addresses.all()], ['10.0.0.5/24'])
        self.assertEqual(str(web.primary_ip4.address), '10.0.0.5/24')

        ct = VirtualMachine.objects.get(name='ct01')  # stopped: static IP from config
        self.assertEqual((ct.status, ct.device.name, ct.virtual_machine_type.name), ('offline', 'pve2', 'Proxmox LXC'))
        self.assertEqual(ct.interfaces.get(name='net0').description, 'eth0 / vmbr0')
        self.assertEqual(str(ct.primary_ip4.address), '10.0.0.6/24')
        self.assertFalse(VirtualMachine.objects.filter(name='tpl').exists())  # templates skipped

        snap = ProxmoxObject.objects.get(connection=self.conn, kind='cluster')
        self.assertEqual((snap.cpu_count, snap.cpu_usage, snap.disk_total), (16, 50.0, 1100 * G))

    def test_second_run_changes_nothing(self):
        self.run_sync()
        before = ObjectChange.objects.count()
        counts = self.run_sync()
        self.assertEqual(ObjectChange.objects.count(), before)
        self.assertEqual((counts['ok'], counts['pending'], counts['new'], counts['created']), (4, 0, 0, 0))

    def test_existing_vm_matched_by_name(self):
        vm = VirtualMachine.objects.create(name='web01', site=self.site, memory=1)
        self.run_sync()
        self.assertEqual(VirtualMachine.objects.filter(name__iexact='web01').count(), 1)
        vm.refresh_from_db()
        self.assertEqual((vm.memory, vm.proxmox_object.proxmox_id), (4096, '101'))

    def test_case_only_match_is_suggested_not_linked(self):
        vm = VirtualMachine.objects.create(name='WEB01', site=self.site)
        counts = self.run_sync()  # writes everything new/pending: the suggestion must not become a duplicate
        link = self.link(101)
        self.assertEqual((counts['suggested'], link.state, link.suggested_pk, link.virtual_machine),
                         (1, 'suggested', vm.pk, None))
        self.assertEqual(VirtualMachine.objects.filter(name__iexact='web01').count(), 1)
        with changelog_context(self.user):  # skipped also when selected explicitly
            Sync(self.conn, LOG).write([link])
        self.assertEqual(VirtualMachine.objects.filter(name__iexact='web01').count(), 1)
        # accepted: linked, preview shows the rename, Proxmox leads after the sync
        self.assertEqual(assign(link, vm, LOG), 'pending')
        link.refresh_from_db()
        self.assertIsNone(link.suggested_pk)
        self.assertIn('WEB01 → web01', '\n'.join(link.changes))
        with changelog_context(self.user):
            Sync(self.conn, LOG).write([link])
        vm.refresh_from_db()
        self.assertEqual(vm.name, 'web01')

    def test_other_cluster_is_suggested_and_reject_is_remembered(self):
        other = Cluster.objects.create(name='vmware', type=ClusterType.objects.create(name='VMware', slug='vmware'))
        vm = VirtualMachine.objects.create(name='web01', cluster=other)
        self.fetch()
        link = self.link(101)
        self.assertEqual((link.state, link.suggested_pk, link.virtual_machine), ('suggested', vm.pk, None))
        self.assertEqual(reject_suggestion(link, LOG), 'new')
        self.fetch()
        link = self.link(101)
        self.assertEqual((link.state, link.suggested_pk, link.rejected_pks), ('new', None, [vm.pk]))

    def test_no_suggestion_when_several_match(self):
        t1, t2 = Tenant.objects.create(name='A', slug='a'), Tenant.objects.create(name='B', slug='b')
        VirtualMachine.objects.create(name='WEB01', site=self.site, tenant=t1)
        VirtualMachine.objects.create(name='Web01', site=self.site, tenant=t2)
        self.fetch()
        self.assertEqual((self.link(101).state, self.link(101).suggested_pk), ('new', None))

    def test_ambiguous_name_is_not_touched(self):
        t1, t2 = Tenant.objects.create(name='A', slug='a'), Tenant.objects.create(name='B', slug='b')
        VirtualMachine.objects.create(name='web01', site=self.site, tenant=t1)
        VirtualMachine.objects.create(name='web01', site=self.site, tenant=t2)
        counts = self.run_sync()
        self.assertEqual(counts['ambiguous'], 1)
        self.assertEqual(ProxmoxObject.objects.get(kind='qemu', proxmox_id='101').state, 'ambiguous')
        self.assertEqual(VirtualMachine.objects.filter(name='web01').count(), 2)

    def test_migration_moves_host_and_unknown_node_keeps_it(self):
        self.run_sync()
        self.resource(101)['node'] = 'pve2'
        self.api[('nodes', 'pve2', 'qemu', 101, 'config')] = self.api[('nodes', 'pve1', 'qemu', 101, 'config')]
        counts = self.run_sync()
        self.assertEqual(counts['moved'], 1)
        self.assertEqual(VirtualMachine.objects.get(name='web01').device.name, 'pve2')
        self.resource(101)['node'] = 'pve9'  # node unknown to NetBox: keep the last known host
        self.run_sync()
        self.assertEqual(VirtualMachine.objects.get(name='web01').device.name, 'pve2')

    def test_gone_from_proxmox_is_stale_and_offline(self):
        self.run_sync()
        self.api[('cluster', 'resources')] = [r for r in self.api[('cluster', 'resources')] if r.get('vmid') != 101]
        counts = self.run_sync()
        self.assertEqual(counts['stale'], 1)
        self.assertEqual(ProxmoxObject.objects.get(kind='qemu', proxmox_id='101').state, 'stale')
        self.assertEqual(VirtualMachine.objects.get(name='web01').status, 'offline')

    def test_existing_ip_is_reused(self):
        ip = IPAddress.objects.create(address='10.0.0.5/24')
        self.run_sync()
        self.assertEqual(IPAddress.objects.filter(address__net_host='10.0.0.5').count(), 1)
        ip.refresh_from_db()
        self.assertEqual(ip.assigned_object.virtual_machine.name, 'web01')

    def test_removed_nic_releases_ip(self):
        self.run_sync()
        cfg = self.api[('nodes', 'pve1', 'qemu', 101, 'config')]
        del cfg['net0']
        self.run_sync()
        web = VirtualMachine.objects.get(name='web01')
        self.assertFalse(web.interfaces.exists())
        self.assertIsNone(web.primary_ip4)
        self.assertIsNone(IPAddress.objects.get(address__net_host='10.0.0.5').assigned_object_id)


    # -- security audit 2026-11-25 ------------------------------------------------------------
    def agent_result(self):
        return self.api[('nodes', 'pve1', 'qemu', 101, 'agent', 'network-get-interfaces')]['result']

    def test_guest_ip_count_is_capped(self):  # MEDIUM-01
        self.agent_result()[1]['ip-addresses'] = [{'ip-address': f'10.9.0.{i}', 'prefix': 24} for i in range(1, 41)]
        self.run_sync()
        web = VirtualMachine.objects.get(name='web01')
        self.assertEqual(web.interfaces.get(name='net0').ip_addresses.count(), 16)

    def test_guest_cannot_claim_reserved_ip(self):  # MEDIUM-01
        reserved = IPAddress.objects.create(address='10.0.0.5/24', status='reserved')
        self.run_sync()
        reserved.refresh_from_db()
        self.assertIsNone(reserved.assigned_object_id)
        self.assertIsNone(VirtualMachine.objects.get(name='web01').primary_ip4)

    def test_vm_of_other_cluster_is_not_taken_over(self):  # MEDIUM-02
        other = Cluster.objects.create(name='vmware', type=ClusterType.objects.create(name='VMware', slug='vmware'))
        foreign = VirtualMachine.objects.create(name='web01', cluster=other, status='active')
        self.run_sync()
        foreign.refresh_from_db()
        self.assertEqual((foreign.cluster, foreign.status), (other, 'active'))
        self.assertFalse(hasattr(foreign, 'proxmox_object'))
        # since 0.3.0 only a suggestion (no duplicate either); the user decides
        self.assertEqual(VirtualMachine.objects.filter(name='web01').count(), 1)
        self.assertEqual(self.link(101).state, 'suggested')

    def test_existing_netbox_tag_is_not_attached(self):  # LOW-01
        Tag.objects.create(name='prod', slug='prod')
        self.run_sync()
        web = VirtualMachine.objects.get(name='web01')
        self.assertEqual(list(web.tags.names()), ['web'])
        self.assertEqual(Tag.objects.get(slug='web').description, TAG_MARKER)
        self.run_sync()  # sync-created tags keep being used
        self.assertEqual(list(VirtualMachine.objects.get(name='web01').tags.names()), ['web'])


    # -- 0.2.0: fetch / review / sync ------------------------------------------------------------
    def counts_of(self):
        return (Device.objects.count(), VirtualMachine.objects.count(), IPAddress.objects.count(),
                ObjectChange.objects.count())

    def test_fetch_writes_nothing_to_netbox(self):
        before = self.counts_of()
        counts = self.fetch()
        self.assertEqual(self.counts_of(), before)
        self.assertEqual(counts['new'], 4)
        web = self.link(101)
        self.assertEqual((web.state, web.target), ('new', None))
        self.assertIn('+ virtual machine web01', web.changes)
        self.assertTrue(any('10.0.0.5/24' in c for c in web.changes))

    def test_pending_changes_are_listed_before_sync(self):
        self.run_sync()
        self.resource(101).update(maxmem=8 * G, node='pve2')
        cfg = self.api[('nodes', 'pve1', 'qemu', 101, 'config')]
        cfg['scsi1'] = 'ceph:vm-101-disk-1,size=10G'
        self.api[('nodes', 'pve2', 'qemu', 101, 'config')] = cfg
        self.fetch()
        web = self.link(101)
        self.assertEqual(web.state, 'pending')
        text = '\n'.join(web.changes)
        self.assertIn('memory 4096 → 8192', text)
        self.assertIn('pve1 → pve2', text)
        self.assertIn('+ virtual disk scsi1', text)
        self.assertEqual(VirtualMachine.objects.get(name='web01').memory, 4096)  # not written yet
        self.assertEqual(self.link(200).state, 'ok')

    def test_name_match_is_linked_but_not_changed(self):
        vm = VirtualMachine.objects.create(name='web01', site=self.site, memory=1)
        before = ObjectChange.objects.count()
        self.fetch()
        link = self.link(101)
        self.assertEqual((link.virtual_machine, link.state), (vm, 'pending'))
        vm.refresh_from_db()
        self.assertEqual((vm.memory, ObjectChange.objects.count()), (1, before))

    def test_sync_selected_only(self):
        self.fetch()
        with changelog_context(self.user):
            Sync(self.conn, LOG).write([self.link(101)])
        self.assertTrue(VirtualMachine.objects.filter(name='web01').exists())
        self.assertFalse(VirtualMachine.objects.filter(name='ct01').exists())
        self.assertFalse(Device.objects.exists())  # node not selected: web01 has no host yet
        self.assertEqual((self.link(101).state, self.link(200).state), ('ok', 'new'))

    def test_ignored_objects_are_never_written(self):
        self.fetch()
        ProxmoxObject.objects.filter(proxmox_id='101').update(ignored=True)
        with changelog_context(self.user):
            Sync(self.conn, LOG).write(list(self.conn.proxmox_objects.all()))
        self.assertFalse(VirtualMachine.objects.filter(name='web01').exists())
        self.assertTrue(VirtualMachine.objects.filter(name='ct01').exists())

    def test_daily_run_updates_linked_but_creates_nothing(self):
        self.run_sync()
        self.resource(101)['maxmem'] = 8 * G
        self.api[('cluster', 'resources')].append(
            {'type': 'lxc', 'vmid': 300, 'name': 'new-ct', 'node': 'pve2', 'status': 'stopped', 'maxmem': G})
        self.api[('nodes', 'pve2', 'lxc', 300, 'config')] = {'ostype': 'debian'}
        self.fetch()
        with changelog_context(None):  # like the system job
            Sync(self.conn, LOG).write(list(self.conn.proxmox_objects.filter(state='pending', ignored=False)))
        self.assertEqual(VirtualMachine.objects.get(name='web01').memory, 8192)
        self.assertFalse(VirtualMachine.objects.filter(name='new-ct').exists())
        self.assertEqual(self.link(300).state, 'new')

    def test_create_requires_add_permission(self):
        user = User.objects.create_user(username='operator')
        grant(user, ProxmoxObject, ['view', 'change'])
        grant(user, VirtualMachine, ['view', 'change'])  # no 'add'
        self.fetch()
        with changelog_context(user):
            Sync(self.conn, LOG, user=user).write([self.link(101)])
        self.assertFalse(VirtualMachine.objects.exists())
        self.assertEqual(self.link(101).state, 'error')

    def test_sync_checks_permissions_on_related_objects(self):  # audit 2026-11-26 MEDIUM-03
        """A user who may create VMs but not IP addresses must not get IPs (or other related objects) created."""
        user = User.objects.create_user(username='vm-admin')
        for model in (ProxmoxObject, VirtualMachine, Cluster):
            grant(user, model, ['view', 'add', 'change'])
        self.fetch()
        with changelog_context(user):
            Sync(self.conn, LOG, user=user).write([self.link(101)])
        self.assertFalse(IPAddress.objects.exists())
        self.assertFalse(VirtualMachine.objects.filter(name='web01').exists())  # whole object rolled back
        self.assertEqual(self.link(101).state, 'error')
        self.assertIn('not allowed', self.link(101).message)

    def test_sync_with_full_permissions_works(self):  # MEDIUM-03: the check must not block legitimate users
        from dcim.models import MACAddress, Platform
        from virtualization.models import VirtualDisk, VirtualMachineType, VMInterface
        user = User.objects.create_user(username='full')
        for model in (ProxmoxObject, VirtualMachine, Cluster, ClusterType, VMInterface, VirtualDisk, IPAddress,
                      MACAddress, Tag, Platform, VirtualMachineType):
            grant(user, model, ['view', 'add', 'change', 'delete'])
        self.fetch()
        with changelog_context(user):
            Sync(self.conn, LOG, user=user).write([self.link(101)])
        self.assertEqual(self.link(101).state, 'ok', self.link(101).message)
        self.assertEqual(str(VirtualMachine.objects.get(name='web01').primary_ip4.address), '10.0.0.5/24')

    def test_ip_constraints_are_enforced(self):  # MEDIUM-03: IPAM constraints apply to synced IPs
        from dcim.models import MACAddress, Platform
        from virtualization.models import VirtualDisk, VirtualMachineType, VMInterface
        user = User.objects.create_user(username='limited-ipam')
        for model in (ProxmoxObject, VirtualMachine, Cluster, ClusterType, VMInterface, VirtualDisk, MACAddress, Tag,
                      Platform, VirtualMachineType):
            grant(user, model, ['view', 'add', 'change', 'delete'])
        grant(user, IPAddress, ['view', 'add', 'change'], constraints={'status': 'reserved'})  # never matches
        self.fetch()
        with changelog_context(user):
            Sync(self.conn, LOG, user=user).write([self.link(101)])
        self.assertFalse(IPAddress.objects.exists())
        self.assertEqual(self.link(101).state, 'error')
        self.assertIn('IP address', self.link(101).message)

    def test_notes_are_not_stored(self):  # LOW-04
        cfg = self.api[('nodes', 'pve1', 'qemu', 101, 'config')]
        cfg.update(description='root password is hunter2', hostname='web01.internal', nameserver='10.0.0.1')
        self.fetch()
        stored = self.link(101).data['config']
        self.assertNotIn('description', stored)
        self.assertNotIn('nameserver', stored)
        self.assertEqual(set(stored), {'ostype', 'onboot', 'smbios1', 'scsi0', 'ide2', 'net0'})

    def test_update_requires_change_permission_with_constraints(self):
        self.run_sync()
        self.resource(101)['maxmem'] = 8 * G
        self.fetch()
        user = User.objects.create_user(username='operator')
        grant(user, VirtualMachine, ['view', 'change'], constraints={'name': 'ct01'})  # web01 excluded
        with changelog_context(user):
            Sync(self.conn, LOG, user=user).write([self.link(101)])
        self.assertEqual(VirtualMachine.objects.get(name='web01').memory, 4096)
        self.assertEqual(self.link(101).state, 'error')

    def test_manual_assignment(self):
        self.fetch()
        vm = VirtualMachine.objects.create(name='webserver-old', site=self.site)
        state = assign(self.link(101), vm, LOG)
        self.assertEqual((state, self.link(101).virtual_machine), ('pending', vm))
        vm.refresh_from_db()
        self.assertEqual(vm.name, 'webserver-old')  # only linked; written on sync
        with self.assertRaises(Exception):
            assign(self.link(200), vm, LOG)  # already linked to 101

    def test_orphans(self):
        from .views import orphan_queryset
        self.run_sync()
        cluster = ProxmoxConnection.objects.get(pk=self.conn.pk).cluster
        manual = VirtualMachine.objects.create(name='manual', cluster=cluster)
        self.api[('cluster', 'resources')] = [r for r in self.api[('cluster', 'resources')] if r.get('vmid') != 101]
        self.fetch()
        names = set(orphan_queryset(VirtualMachine).values_list('name', flat=True))
        self.assertEqual(names, {'web01', 'manual'})
        IgnoredOrphan.objects.create(virtual_machine=manual)
        self.assertEqual(set(orphan_queryset(VirtualMachine).values_list('name', flat=True)), {'web01'})
        self.assertEqual(set(orphan_queryset(VirtualMachine, ignored=True).values_list('name', flat=True)), {'manual'})


class ViewTests(TestCase):
    def test_overview_requires_permission(self):
        user = User.objects.create_user(username='viewer')
        self.client.force_login(user)
        self.assertEqual(self.client.get('/plugins/proxmox/sync/').status_code, 403)
        grant(user, ProxmoxObject, ['view'])
        self.assertEqual(self.client.get('/plugins/proxmox/sync/').status_code, 200)

    def test_pages_render(self):
        SyncTests.setUpTestData.__func__(SyncTests)
        conn = ProxmoxConnection.objects.create(
            name='lab', url='https://pve.example.local:8006', token_id='root@pam!netbox', site=SyncTests.site,
            node_role=SyncTests.role, node_device_type=SyncTests.dt)
        admin = User.objects.create_user(username='admin2', is_superuser=True)
        full_sync(conn, copy.deepcopy(API), admin)
        conn.refresh_from_db()
        self.client.force_login(admin)
        web = VirtualMachine.objects.get(name='web01')
        link = ProxmoxObject.objects.get(proxmox_id='200')
        for url in (conn.get_absolute_url(), '/plugins/proxmox/connections/', '/plugins/proxmox/connections/add/',
                    '/plugins/proxmox/sync/', '/plugins/proxmox/sync/?ignored=true', '/plugins/proxmox/orphans/',
                    '/plugins/proxmox/orphans/devices/', f'/plugins/proxmox/sync/{link.pk}/ignore/',
                    f'/plugins/proxmox/orphans/vms/{web.pk}/assign/', web.get_absolute_url(),
                    web.device.get_absolute_url(), conn.cluster.get_absolute_url()):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
        self.assertContains(self.client.get(web.get_absolute_url()), 'pve1')
        self.assertContains(self.client.get(conn.cluster.get_absolute_url()), '50.0 %')

    def test_connection_edit_superuser_only(self):
        user = User.objects.create_user(username='editor')
        grant(user, ProxmoxConnection, ['view', 'add', 'change'])
        self.client.force_login(user)
        self.assertEqual(self.client.get('/plugins/proxmox/connections/add/').status_code, 403)
        self.assertEqual(self.client.get('/plugins/proxmox/connections/').status_code, 200)


class ActionViewTests(TestCase):
    """POST actions of the sync overview and the orphans page."""
    def setUp(self):
        SyncTests.setUpTestData.__func__(SyncTests)
        self.conn = ProxmoxConnection.objects.create(
            name='lab', url='https://pve.example.local:8006', token_id='root@pam!netbox', site=SyncTests.site,
            node_role=SyncTests.role, node_device_type=SyncTests.dt)
        self.user = User.objects.create_user(username='op')
        grant(self.user, ProxmoxObject, ['view', 'change'])
        self.client.force_login(self.user)
        discover(self.conn, FakeClient(copy.deepcopy(API)), LOG)

    def test_bulk_sync_enqueues_only_selected(self):
        pk = ProxmoxObject.objects.get(proxmox_id='101').pk
        with mock.patch('netbox_proxmox.views.ProxmoxSyncJob.enqueue') as enqueue:
            enqueue.return_value.get_absolute_url.return_value = '/core/jobs/1/'
            self.client.post('/plugins/proxmox/sync/bulk-sync/', {'pk': [pk]})
        self.assertEqual(enqueue.call_args.kwargs['object_ids'], [pk])
        self.assertEqual(enqueue.call_args.kwargs['mode'], 'apply')

    def test_fetch_only_mode(self):
        with mock.patch('netbox_proxmox.views.ProxmoxSyncJob.enqueue') as enqueue:
            enqueue.return_value.get_absolute_url.return_value = '/core/jobs/1/'
            self.client.post('/plugins/proxmox/sync/fetch/')
        self.assertEqual(enqueue.call_args.kwargs['mode'], 'fetch')

    def test_overview_renders_pending_changes(self):
        """Rows with a change list (state new) - the 0.2.0 overview crashed on these (format_html without args)."""
        grant(self.user, ProxmoxObject, ['view'])
        many = ProxmoxObject.objects.get(proxmox_id='101')
        self.assertGreater(len(many.changes), 3)  # also covers the "+ n more" branch
        response = self.client.get('/plugins/proxmox/sync/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '+ virtual machine web01')
        self.assertContains(response, '<br>')

    def test_hidden_target_and_changes_for_viewers(self):  # LOW-05
        admin = User.objects.create_user(username='adm2', is_superuser=True)
        full_sync(self.conn, copy.deepcopy(API), admin)
        ProxmoxObject.objects.filter(proxmox_id='101').update(state='pending', changes=['virtual machine web01: device pve1 → pve2'])
        viewer = User.objects.create_user(username='viewer2')
        grant(viewer, ProxmoxObject, ['view'])
        grant(viewer, VirtualMachine, ['view'], constraints={'name': 'ct01'})  # may not see web01
        self.client.force_login(viewer)
        page = self.client.get('/plugins/proxmox/sync/').content.decode()
        self.assertIn('(no access)', page)
        self.assertIn('>ct01<', page)
        self.assertNotIn('pve1 → pve2', page)
        self.assertIn('1 change(s)', page)

    def test_malformed_pk_is_ignored(self):  # INFO-05
        response = self.client.post('/plugins/proxmox/sync/bulk-ignore/', {'pk': ['abc', '1 OR 1=1']})
        self.assertEqual(response.status_code, 302)

    def test_orphan_ignore_needs_add_and_delete(self):  # INFO-04
        admin = User.objects.create_user(username='adm3', is_superuser=True)
        full_sync(self.conn, copy.deepcopy(API), admin)
        cluster = ProxmoxConnection.objects.get(pk=self.conn.pk).cluster
        orphan = VirtualMachine.objects.create(name='manual', cluster=cluster)
        user = User.objects.create_user(username='half')
        grant(user, IgnoredOrphan, ['view', 'change'])
        grant(user, VirtualMachine, ['view', 'change'])
        self.client.force_login(user)
        self.assertEqual(self.client.post(f'/plugins/proxmox/orphans/vms/{orphan.pk}/ignore/').status_code, 403)
        self.assertEqual(self.client.post('/plugins/proxmox/orphans/vms/bulk-ignore/', {'pk': [orphan.pk]}).status_code, 403)
        self.assertFalse(IgnoredOrphan.objects.exists())

    def test_bulk_ignore_and_unignore(self):
        pk = ProxmoxObject.objects.get(proxmox_id='101').pk
        self.client.post('/plugins/proxmox/sync/bulk-ignore/', {'pk': [pk]})
        self.assertTrue(ProxmoxObject.objects.get(pk=pk).ignored)
        self.client.post('/plugins/proxmox/sync/bulk-unignore/', {'pk': [pk]})
        self.assertFalse(ProxmoxObject.objects.get(pk=pk).ignored)

    def test_assign_only_changeable_objects(self):
        allowed = VirtualMachine.objects.create(name='allowed', site=SyncTests.site)
        hidden = VirtualMachine.objects.create(name='hidden', site=SyncTests.site)
        grant(self.user, VirtualMachine, ['view', 'change'], constraints={'name': 'allowed'})
        pk = ProxmoxObject.objects.get(proxmox_id='101').pk
        self.client.post(f'/plugins/proxmox/sync/{pk}/assign/', {'target': hidden.pk})
        self.assertIsNone(ProxmoxObject.objects.get(pk=pk).virtual_machine)
        self.client.post(f'/plugins/proxmox/sync/{pk}/assign/', {'target': allowed.pk})
        self.assertEqual(ProxmoxObject.objects.get(pk=pk).virtual_machine, allowed)

    def test_accept_and_reject_suggestion(self):
        web = VirtualMachine.objects.create(name='WEB01', site=SyncTests.site)
        ct = VirtualMachine.objects.create(name='CT01', site=SyncTests.site)
        discover(self.conn, FakeClient(copy.deepcopy(API)), LOG)
        pk_web, pk_ct = ProxmoxObject.objects.get(proxmox_id='101').pk, ProxmoxObject.objects.get(proxmox_id='200').pk
        grant(self.user, VirtualMachine, ['view'])
        page = self.client.get('/plugins/proxmox/sync/')
        self.assertContains(page, f'/sync/{pk_web}/accept/')
        self.assertContains(page, web.get_absolute_url())
        # accepting needs change permission on the suggested VM
        self.assertEqual(self.client.post(f'/plugins/proxmox/sync/{pk_web}/accept/').status_code, 404)
        self.assertIsNone(ProxmoxObject.objects.get(pk=pk_web).virtual_machine)
        grant(self.user, VirtualMachine, ['change'])
        self.assertContains(self.client.get(f'/plugins/proxmox/sync/{pk_web}/accept/'), 'WEB01')
        self.client.post(f'/plugins/proxmox/sync/{pk_web}/accept/')
        link = ProxmoxObject.objects.get(pk=pk_web)
        self.assertEqual((link.virtual_machine, link.state), (web, 'pending'))
        self.client.post(f'/plugins/proxmox/sync/{pk_ct}/reject/')
        link = ProxmoxObject.objects.get(pk=pk_ct)
        self.assertEqual((link.state, link.suggested_pk, link.rejected_pks), ('new', None, [ct.pk]))
        self.assertEqual(self.client.post(f'/plugins/proxmox/sync/{pk_ct}/reject/').status_code, 404)

    def test_actions_require_permission(self):
        nobody = User.objects.create_user(username='nobody')
        self.client.force_login(nobody)
        pk = ProxmoxObject.objects.get(proxmox_id='101').pk
        for url in ('/plugins/proxmox/sync/fetch/', '/plugins/proxmox/sync/bulk-sync/',
                    '/plugins/proxmox/sync/bulk-ignore/', f'/plugins/proxmox/sync/{pk}/ignore/',
                    f'/plugins/proxmox/sync/{pk}/accept/', f'/plugins/proxmox/sync/{pk}/reject/'):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, {'pk': [pk]}).status_code, 403)

    def test_orphan_decommission_and_ignore(self):
        admin = User.objects.create_user(username='adm', is_superuser=True)
        full_sync(self.conn, copy.deepcopy(API), admin)
        cluster = ProxmoxConnection.objects.get(pk=self.conn.pk).cluster
        orphan = VirtualMachine.objects.create(name='manual', cluster=cluster, status='active')
        other = VirtualMachine.objects.create(name='manual2', cluster=cluster, status='active')
        self.client.force_login(admin)
        self.client.post('/plugins/proxmox/orphans/vms/bulk-decommission/', {'pk': [orphan.pk]})
        orphan.refresh_from_db()
        self.assertEqual(orphan.status, 'decommissioning')
        self.client.post(f'/plugins/proxmox/orphans/vms/{other.pk}/ignore/')
        self.assertTrue(IgnoredOrphan.objects.filter(virtual_machine=other).exists())
        web = VirtualMachine.objects.get(name='web01')  # linked, not an orphan: decommission must not touch it
        self.client.post('/plugins/proxmox/orphans/vms/bulk-decommission/', {'pk': [web.pk]})
        web.refresh_from_db()
        self.assertEqual(web.status, 'active')


class ClientTests(TestCase):
    def test_redirect_is_refused(self):  # LOW-02
        from unittest import mock
        import requests
        resp = requests.Response()
        resp.status_code, resp.headers['Location'] = 302, 'https://internal.example/'
        with mock.patch('netbox_proxmox.client.requests.get', return_value=resp) as get:
            with self.assertRaises(ProxmoxError):
                ProxmoxClient('https://pve.example:8006', 'a@pam!b', 'secret').version()
        self.assertIs(get.call_args.kwargs['allow_redirects'], False)

    def test_tls_warning_on_connection_page(self):  # LOW-03
        SyncTests.setUpTestData.__func__(SyncTests)
        conn = ProxmoxConnection.objects.create(
            name='lab', url='https://pve.example.local:8006', token_id='root@pam!netbox', site=SyncTests.site,
            node_role=SyncTests.role, node_device_type=SyncTests.dt, verify_tls=False)
        self.client.force_login(User.objects.create_user(username='su', is_superuser=True))
        self.assertContains(self.client.get(conn.get_absolute_url()), 'pve-root-ca.pem')
        conn.verify_tls = True
        conn.save()
        self.assertNotContains(self.client.get(conn.get_absolute_url()), 'pve-root-ca.pem')


class TranslationTests(TestCase):
    def test_compiled_catalog_is_current(self):
        """django.mo is shipped (target systems usually lack gettext) - it must contain every translated msgid."""
        import gettext
        import os
        import re
        base = os.path.join(os.path.dirname(__file__), 'locale', 'de', 'LC_MESSAGES')
        po = open(os.path.join(base, 'django.po'), encoding='utf-8').read()
        with open(os.path.join(base, 'django.mo'), 'rb') as f:
            catalog = gettext.GNUTranslations(f)._catalog
        missing = [m for m in re.findall(r'^msgid "(.+)"\nmsgstr "(?!")', po, re.M)
                   if m.replace('\\"', '"') not in catalog]
        self.assertEqual(missing, [], 'django.mo is outdated - run makemessages/msgfmt (see CLAUDE.md)')
        self.assertEqual(re.findall(r'^#, .*fuzzy.*\n(?:#\|.*\n)*msgid "(.*)"', po, re.M), [], 'fuzzy (guessed) translations - fix them')
        self.assertIn('Proxmox connections', catalog)
