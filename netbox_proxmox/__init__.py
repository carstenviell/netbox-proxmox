from django.db.models.signals import post_migrate
from netbox.plugins import PluginConfig


class ProxmoxConfig(PluginConfig):
    name = 'netbox_proxmox'
    verbose_name = 'Proxmox'
    description = 'Import and sync Proxmox VE clusters, nodes, VMs and LXC containers (disks, interfaces, MAC/IP, utilization).'
    version = '0.3.0'
    author = 'Carsten Viell'
    base_url = 'proxmox'
    min_version = '4.7.0'
    default_settings = {
        'sync_interval_min': 1440,
    }

    def ready(self):
        super().ready()
        from . import jobs  # noqa: F401  (registers the system job)
        from .custom_fields import ensure_custom_fields
        post_migrate.connect(ensure_custom_fields, sender=self)


config = ProxmoxConfig
