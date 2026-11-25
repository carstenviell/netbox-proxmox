from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from netbox.object_actions import ObjectAction


class _Bulk(ObjectAction):
    """multi=True also turns on the row checkboxes. The target views check the real permissions."""
    multi = True
    permissions_required = {'view'}
    template_name = 'netbox_proxmox/buttons/bulk_apply.html'
    url_name = None
    icon = 'mdi-check'

    @classmethod
    def get_url(cls, obj):
        if obj._meta.model_name == 'proxmoxobject':
            return reverse(f'plugins:netbox_proxmox:{cls.url_name}')
        kind = 'devices' if obj._meta.model_name == 'device' else 'vms'
        return reverse(f'plugins:netbox_proxmox:{cls.url_name}', kwargs={'kind': kind})

    @classmethod
    def get_context(cls, context, obj):
        return {'icon': cls.icon}


class BulkSyncSelected(_Bulk):
    name, label, url_name, icon = 'proxmox_bulk_sync', _('Sync selected'), 'bulk_sync', 'mdi-sync'


class BulkIgnoreSelected(_Bulk):
    name, label, url_name, icon = 'proxmox_bulk_ignore', _('Ignore selected'), 'bulk_ignore', 'mdi-eye-off'


class BulkUnignoreSelected(_Bulk):
    name, label, url_name, icon = 'proxmox_bulk_unignore', _('Unignore selected'), 'bulk_unignore', 'mdi-eye'


class BulkDecommissionOrphans(_Bulk):
    name, label, url_name, icon = ('proxmox_orphan_decommission', _('Decommission selected'),
                                   'orphan_bulk_decommission', 'mdi-power-plug-off')


class BulkIgnoreOrphans(_Bulk):
    name, label, url_name, icon = 'proxmox_orphan_ignore', _('Ignore selected'), 'orphan_bulk_ignore', 'mdi-eye-off'
