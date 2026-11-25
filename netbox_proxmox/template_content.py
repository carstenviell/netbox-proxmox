from django.db.models import Sum

from netbox.plugins import PluginTemplateExtension

from .models import ProxmoxObject


class ProxmoxPanel(PluginTemplateExtension):
    """Last sync result and usage snapshot. DB only, no live API call."""
    models = ['dcim.device', 'virtualization.virtualmachine', 'virtualization.cluster']

    def left_page(self):
        obj, user = self.context['object'], self.context['request'].user
        if not user.has_perm('netbox_proxmox.view_proxmoxobject'):
            return ''
        qs = ProxmoxObject.objects.select_related('connection')
        if obj._meta.model_name == 'cluster':
            link = qs.filter(kind='cluster', connection__cluster=obj).first()
            allocated = obj.virtual_machines.filter(status='active').aggregate(
                vcpus=Sum('vcpus'), memory=Sum('memory'), disk=Sum('disk')) if link else None
        else:
            field = 'device' if obj._meta.model_name == 'device' else 'virtual_machine'
            link, allocated = qs.filter(**{field: obj}).first(), None
        if link is None:
            return ''
        return self.render('netbox_proxmox/panel.html', extra_context={'link': link, 'allocated': allocated})


template_extensions = [ProxmoxPanel]
