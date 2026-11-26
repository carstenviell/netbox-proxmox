from django.db import migrations, models


def trust_written_links(apps, schema_editor):
    """MEDIUM-04: keep the daily run for links that were written before (in sync, or the NetBox object already
    carries the sync's marks). Links only assigned/accepted and never synced must be synced manually once."""
    ProxmoxObject = apps.get_model('netbox_proxmox', 'ProxmoxObject')
    trusted = []
    for link in ProxmoxObject.objects.exclude(device__isnull=True, virtual_machine__isnull=True) \
            .select_related('connection', 'device', 'virtual_machine'):
        if link.state == 'ok':
            trusted.append(link.pk)
        elif link.virtual_machine_id:
            if str((link.virtual_machine.custom_field_data or {}).get('proxmox_vmid')) == link.proxmox_id:
                trusted.append(link.pk)
        elif link.connection.cluster_id and link.device.cluster_id == link.connection.cluster_id:
            trusted.append(link.pk)
    ProxmoxObject.objects.filter(pk__in=trusted).update(synced=True)


class Migration(migrations.Migration):
    dependencies = [('netbox_proxmox', '0005_suggestions')]
    operations = [
        migrations.AddField(
            model_name='proxmoxobject',
            name='synced',
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.RunPython(trust_written_links, migrations.RunPython.noop),
    ]
