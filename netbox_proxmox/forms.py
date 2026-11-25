from django import forms
from django.utils.translation import gettext_lazy as _

from dcim.models import DeviceRole, DeviceType, Site
from netbox.forms import NetBoxModelFilterSetForm, NetBoxModelForm
from tenancy.models import Tenant
from utilities.forms.fields import CommentField, DynamicModelChoiceField
from utilities.forms.rendering import FieldSet

from .models import ProxmoxConnection, ProxmoxObject


class ProxmoxConnectionForm(NetBoxModelForm):
    site = DynamicModelChoiceField(label=_('Site'), queryset=Site.objects.all())
    node_role = DynamicModelChoiceField(label=_('Node role'), queryset=DeviceRole.objects.all())
    node_device_type = DynamicModelChoiceField(label=_('Node device type'), queryset=DeviceType.objects.all())
    vm_role = DynamicModelChoiceField(label=_('VM role'), queryset=DeviceRole.objects.all(), required=False,
                                      query_params={'vm_role': True})
    container_role = DynamicModelChoiceField(label=_('Container role'), queryset=DeviceRole.objects.all(),
                                             required=False, query_params={'vm_role': True})
    tenant = DynamicModelChoiceField(label=_('Tenant'), queryset=Tenant.objects.all(), required=False)
    comments = CommentField()

    fieldsets = (
        FieldSet('name', 'url', 'token_id', 'token_secret_env', 'verify_tls', 'ca_bundle', 'timeout', 'enabled',
                 name=_('Connection')),
        FieldSet('site', 'node_role', 'node_device_type', 'vm_role', 'container_role', 'tenant',
                 name=_('Defaults for new objects')),
        FieldSet('description', 'tags', name=_('Other')),
    )

    class Meta:
        model = ProxmoxConnection
        fields = ('name', 'url', 'token_id', 'token_secret_env', 'verify_tls', 'ca_bundle', 'timeout', 'enabled',
                  'site', 'node_role', 'node_device_type', 'vm_role', 'container_role', 'tenant', 'description',
                  'comments', 'tags')


class ProxmoxConnectionFilterForm(NetBoxModelFilterSetForm):
    model = ProxmoxConnection


class ProxmoxObjectFilterForm(forms.Form):
    q = forms.CharField(required=False, label=_('Search'))
    ignored = forms.NullBooleanField(required=False, label=_('Ignored'), widget=forms.Select(choices=(
        ('', '---------'), ('true', _('Yes')), ('false', _('No')))))
    # plain field: the plugin has no REST API for the dynamic (API-backed) selector
    connection_id = forms.ModelMultipleChoiceField(queryset=ProxmoxConnection.objects.all(), required=False,
                                                   label=_('Connection'))
    kind = forms.MultipleChoiceField(choices=ProxmoxObject.KIND_CHOICES, required=False, label=_('Type'))
    state = forms.MultipleChoiceField(choices=ProxmoxObject.STATE_CHOICES, required=False, label=_('State'))
    fieldsets = (FieldSet('q', 'connection_id', 'kind', 'state', 'ignored'),)


class AssignNetBoxObjectForm(forms.Form):
    """Sync overview: link a Proxmox object to an existing, not yet linked NetBox device (node) or VM."""
    target = forms.ModelChoiceField(queryset=None, label=_('NetBox object'))

    def __init__(self, *args, link=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from dcim.models import Device
        from virtualization.models import VirtualMachine
        model = Device if link.kind == 'node' else VirtualMachine
        # only objects the user may change (permission constraints included) and that are not linked yet
        self.fields['target'].queryset = model.objects.restrict(user, 'change').filter(proxmox_object__isnull=True)
        self.fields['target'].label = model._meta.verbose_name.capitalize()


class AssignProxmoxObjectForm(forms.Form):
    """Orphans: link a NetBox device/VM to a Proxmox object that has no NetBox object yet."""
    link = forms.ModelChoiceField(queryset=None, label=_('Proxmox object'))

    def __init__(self, *args, target=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        kinds = ['node'] if target._meta.model_name == 'device' else ['qemu', 'lxc']
        self.fields['link'].queryset = ProxmoxObject.objects.restrict(user, 'change').filter(
            kind__in=kinds, device__isnull=True, virtual_machine__isnull=True).select_related('connection')
        self.fields['link'].label_from_instance = lambda o: f'{o.name} ({o.connection}, {o.get_kind_display()} {o.proxmox_id})'
