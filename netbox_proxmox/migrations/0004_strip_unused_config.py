import re

from django.db import migrations

# keep in sync with sync.used_config()
CONFIG_KEYS = ('ostype', 'onboot', 'smbios1')
USED = re.compile(r'^((scsi|virtio|sata|ide)\d+|rootfs|mp\d+|net\d+)$')


def strip_unused_config(apps, schema_editor):
    """0.2.0/0.2.1 stored the full guest config incl. free-text notes; keep only what the sync uses."""
    ProxmoxObject = apps.get_model('netbox_proxmox', 'ProxmoxObject')
    for obj in ProxmoxObject.objects.filter(kind__in=('qemu', 'lxc')).only('pk', 'data'):
        config = (obj.data or {}).get('config')
        if not config:
            continue
        kept = {k: v for k, v in config.items() if k in CONFIG_KEYS or USED.match(k)}
        if kept != config:
            obj.data['config'] = kept
            obj.save(update_fields=['data'])


class Migration(migrations.Migration):
    dependencies = [('netbox_proxmox', '0003_sync_overview_orphans')]
    operations = [migrations.RunPython(strip_unused_config, migrations.RunPython.noop)]
