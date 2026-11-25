import copy
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
from .models import ProxmoxConnection, ProxmoxObject
from .sync import TAG_MARKER, agent_ips, parse_disks, parse_nics, size_mb, storage_usage, sync_connection

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
        with changelog_context(self.user):
            return sync_connection(self.conn, FakeClient(self.api), LOG)

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
        self.assertEqual((counts['created'], counts['updated'], counts['unchanged']), (0, 0, 4))

    def test_existing_vm_matched_by_name(self):
        vm = VirtualMachine.objects.create(name='WEB01', site=self.site)
        self.run_sync()
        self.assertEqual(VirtualMachine.objects.filter(name__iexact='web01').count(), 1)
        vm.refresh_from_db()
        self.assertEqual((vm.name, vm.proxmox_object.proxmox_id), ('web01', '101'))

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
        self.assertEqual(VirtualMachine.objects.filter(name='web01').count(), 2)

    def test_existing_netbox_tag_is_not_attached(self):  # LOW-01
        Tag.objects.create(name='prod', slug='prod')
        self.run_sync()
        web = VirtualMachine.objects.get(name='web01')
        self.assertEqual(list(web.tags.names()), ['web'])
        self.assertEqual(Tag.objects.get(slug='web').description, TAG_MARKER)
        self.run_sync()  # sync-created tags keep being used
        self.assertEqual(list(VirtualMachine.objects.get(name='web01').tags.names()), ['web'])


class ViewTests(TestCase):
    def test_overview_requires_permission(self):
        user = User.objects.create_user(username='viewer')
        self.client.force_login(user)
        self.assertEqual(self.client.get('/plugins/proxmox/objects/').status_code, 403)
        grant(user, ProxmoxObject, ['view'])
        self.assertEqual(self.client.get('/plugins/proxmox/objects/').status_code, 200)

    def test_pages_render(self):
        SyncTests.setUpTestData.__func__(SyncTests)
        conn = ProxmoxConnection.objects.create(
            name='lab', url='https://pve.example.local:8006', token_id='root@pam!netbox', site=SyncTests.site,
            node_role=SyncTests.role, node_device_type=SyncTests.dt)
        admin = User.objects.create_user(username='admin2', is_superuser=True)
        with changelog_context(admin):
            sync_connection(conn, FakeClient(copy.deepcopy(API)), LOG)
        conn.refresh_from_db()
        self.client.force_login(admin)
        web = VirtualMachine.objects.get(name='web01')
        for url in (conn.get_absolute_url(), '/plugins/proxmox/connections/', '/plugins/proxmox/connections/add/',
                    '/plugins/proxmox/objects/', web.get_absolute_url(), web.device.get_absolute_url(),
                    conn.cluster.get_absolute_url()):
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
