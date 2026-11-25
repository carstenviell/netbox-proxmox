from django.conf import settings
from django.utils import translation
from django.utils.translation import gettext_lazy as _

CF_VMID = 'proxmox_vmid'


def ensure_custom_fields(sender=None, **kwargs):
    """Read-only VMID field on virtual machines (visible in UI, API and filters). Created if missing, never
    modified. The authoritative link is ProxmoxObject; this is the visible copy."""
    from core.models import ObjectType
    from extras.models import CustomField
    from virtualization.models import VirtualMachine

    with translation.override(settings.LANGUAGE_CODE):
        label, description = str(_('Proxmox VMID')), str(_('Set by the Proxmox sync'))
    cf, _created = CustomField.objects.get_or_create(name=CF_VMID, defaults={
        'type': 'integer', 'label': label, 'description': description, 'group_name': 'Proxmox', 'weight': 200,
        'ui_editable': 'no',
    })
    cf.object_types.add(ObjectType.objects.get_for_model(VirtualMachine))
