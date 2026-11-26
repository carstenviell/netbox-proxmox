from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('netbox_proxmox', '0004_strip_unused_config')]
    operations = [
        migrations.AddField(
            model_name='proxmoxobject',
            name='suggested_pk',
            field=models.PositiveIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='proxmoxobject',
            name='rejected_pks',
            field=models.JSONField(blank=True, default=list, editable=False),
        ),
    ]
