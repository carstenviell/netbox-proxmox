import django_tables2 as tables
from django.template.defaultfilters import filesizeformat
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from netbox.tables import NetBoxTable, columns

from .models import ProxmoxConnection, ProxmoxObject

STATE_COLORS = {'ok': 'success', 'created': 'info', 'ambiguous': 'warning', 'error': 'danger', 'stale': 'secondary'}


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
    memory = tables.Column(empty_values=(), orderable=False, verbose_name=_('Memory'))
    disk = tables.Column(empty_values=(), orderable=False, verbose_name=_('Disk'))

    class Meta(NetBoxTable.Meta):
        model = ProxmoxObject
        fields = ('pk', 'connection', 'kind', 'proxmox_id', 'name', 'node', 'target', 'state', 'message',
                  'cpu_usage', 'cpu_count', 'memory', 'disk', 'last_seen')
        default_columns = ('connection', 'kind', 'proxmox_id', 'name', 'node', 'target', 'state', 'cpu_usage',
                           'memory', 'last_seen')

    def render_target(self, record):
        return record.target or '—'

    def render_state(self, record):
        return format_html('<span class="badge text-bg-{}" title="{}">{}</span>',
                           STATE_COLORS.get(record.state, 'secondary'), record.message, record.get_state_display())

    def render_memory(self, record):
        return _usage(record.mem_used, record.mem_total)

    def render_disk(self, record):
        return _usage(record.disk_used, record.disk_total)
