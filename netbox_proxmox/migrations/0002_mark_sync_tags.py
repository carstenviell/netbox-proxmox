from django.db import migrations

TAG_MARKER = 'Created by the Proxmox sync'


def mark_sync_tags(apps, schema_editor):
    """0.1.0 created tags without a marker. Mark those that are only used on VMs linked by the Proxmox sync,
    so the sync keeps attaching them; tags also used elsewhere stay NetBox-managed."""
    Tag = apps.get_model('extras', 'Tag')
    TaggedItem = apps.get_model('extras', 'TaggedItem')
    ContentType = apps.get_model('contenttypes', 'ContentType')
    ProxmoxObject = apps.get_model('netbox_proxmox', 'ProxmoxObject')
    vm_ct = ContentType.objects.filter(app_label='virtualization', model='virtualmachine').first()
    if vm_ct is None:
        return
    linked = set(ProxmoxObject.objects.filter(virtual_machine__isnull=False).values_list('virtual_machine_id', flat=True))
    for tag in Tag.objects.filter(description=''):
        items = list(TaggedItem.objects.filter(tag=tag).values_list('content_type_id', 'object_id'))
        if items and all(ct == vm_ct.pk and oid in linked for ct, oid in items):
            tag.description = TAG_MARKER
            tag.save(update_fields=['description'])


class Migration(migrations.Migration):
    dependencies = [
        ('netbox_proxmox', '0001_initial'),
        ('extras', '0001_initial'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]
    operations = [migrations.RunPython(mark_sync_tags, migrations.RunPython.noop)]
