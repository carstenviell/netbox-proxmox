import django_tables2 as tables
from django.template.defaultfilters import filesizeformat
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _

from dcim.tables import DeviceTable
from netbox.tables import NetBoxTable, columns
from virtualization.tables import VirtualMachineTable

from .models import ProxmoxConnection, ProxmoxObject

STATE_COLORS = {'new': 'info', 'pending': 'warning', 'ok': 'success', 'ambiguous': 'warning', 'error': 'danger',
                'stale': 'secondary'}
SHOWN_CHANGES = 3


class ProxmoxConnectionTable(NetBoxTable):
    name = tables.Column(linkify=True)
    cluster = tables.Column(linkify=True)
    site = tables.Column(linkify=True)
    enabled = columns.BooleanColumn()
    tags = columns.TagColumn(url_name='plugins:netbox_proxmox:proxmoxconnection_list')

    class Meta(NetBoxTable.Meta):
        model = ProxmoxConnection
        fields = ('pk', 'id', 'name', 'url', 'token_id', 'cluster', 'site', 'enabled', 'last_sync', 'description',
                  'tags', 'created', 'last_updated')
        default_columns = ('name', 'url', 'cluster', 'site', 'enabled', 'last_sync')


def _usage(used, total):
    if not total:
        return '—'
    return f'{filesizeformat(used or 0)} / {filesizeformat(total)} ({(used or 0) * 100 // total} %)'


class ProxmoxObjectTable(NetBoxTable):
    actions = columns.ActionsColumn(actions=())  # read-only: no edit/delete views
    connection = tables.Column(linkify=True)
    target = tables.Column(empty_values=(), orderable=False, verbose_name=_('NetBox object'),
                           linkify=lambda record: record.target.get_absolute_url() if record.target else None)
    state = tables.Column()
    changes = tables.Column(empty_values=(), orderable=False, verbose_name=_('Pending changes'))
    ignored = columns.BooleanColumn(verbose_name=_('Ignored'))
    row_actions = tables.TemplateColumn(template_name='netbox_proxmox/inc/row_actions.html', orderable=False,
                                        verbose_name='')
    memory = tables.Column(empty_values=(), orderable=False, verbose_name=_('Memory'))
    disk = tables.Column(empty_values=(), orderable=False, verbose_name=_('Disk'))

    class Meta(NetBoxTable.Meta):
        model = ProxmoxObject
        fields = ('pk', 'connection', 'kind', 'proxmox_id', 'name', 'node', 'target', 'state', 'changes', 'message',
                  'ignored', 'cpu_usage', 'cpu_count', 'memory', 'disk', 'last_seen', 'row_actions')
        default_columns = ('kind', 'proxmox_id', 'name', 'node', 'target', 'state', 'changes', 'cpu_usage',
                           'memory', 'last_seen', 'row_actions')

    def render_target(self, record):
        return record.target or '—'

    def render_state(self, record):
        return format_html('<span class="badge text-bg-{}" title="{}">{}</span>',
                           STATE_COLORS.get(record.state, 'secondary'), record.message, record.get_state_display())

    def render_changes(self, record):
        changes = record.changes or []
        if not changes:
            return '—'
        shown = format_html_join(format_html('<br>'), '{}', ((c,) for c in changes[:SHOWN_CHANGES]))
        more = len(changes) - SHOWN_CHANGES
        if more <= 0:
            return format_html('<span class="small">{}</span>', shown)
        return format_html('<span class="small">{}<br><span class="text-muted" title="{}">{}</span></span>', shown,
                           '\n'.join(changes), _('+ %(n)d more') % {'n': more})

    def render_memory(self, record):
        return _usage(record.mem_used, record.mem_total)

    def render_disk(self, record):
        return _usage(record.disk_used, record.disk_total)


class _OrphanColumns(tables.Table):
    proxmox_reason = tables.Column(empty_values=(), orderable=False, verbose_name=_('Proxmox'))
    orphan_actions = tables.TemplateColumn(template_name='netbox_proxmox/inc/orphan_row_actions.html',
                                           orderable=False, verbose_name='')

    def render_proxmox_reason(self, record):
        link = getattr(record, 'proxmox_object', None)
        if link is not None and link.state == 'stale':
            return format_html('<span class="badge text-bg-secondary" title="{}">{}</span>',
                               f'{link.connection} {link.get_kind_display()} {link.proxmox_id}', _('Gone from Proxmox'))
        return format_html('<span class="badge text-bg-warning">{}</span>', _('Not linked'))


ORPHAN_EXTRA = ('proxmox_reason', 'orphan_actions')


class OrphanVMTable(_OrphanColumns, VirtualMachineTable):
    class Meta(VirtualMachineTable.Meta):
        fields = VirtualMachineTable.Meta.fields + ORPHAN_EXTRA
        default_columns = ('name', 'status', 'cluster', 'device', 'role') + ORPHAN_EXTRA


class OrphanDeviceTable(_OrphanColumns, DeviceTable):
    class Meta(DeviceTable.Meta):
        fields = DeviceTable.Meta.fields + ORPHAN_EXTRA
        default_columns = ('name', 'status', 'site', 'cluster', 'role') + ORPHAN_EXTRA
