from django.utils.translation import gettext_lazy as _

from netbox.plugins.navigation import PluginMenuButton, PluginMenuItem

menu_items = (
    PluginMenuItem(link='plugins:netbox_proxmox:proxmoxconnection_list', link_text=_('Proxmox connections'),
                   permissions=['netbox_proxmox.view_proxmoxconnection'],
                   buttons=(PluginMenuButton('plugins:netbox_proxmox:proxmoxconnection_add', _('Add'), 'mdi mdi-plus',
                                             permissions=['netbox_proxmox.add_proxmoxconnection']),)),
    PluginMenuItem(link='plugins:netbox_proxmox:proxmoxobject_list', link_text=_('Proxmox sync overview'),
                   permissions=['netbox_proxmox.view_proxmoxobject']),
)
