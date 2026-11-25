from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views import View

from django.utils.translation import gettext_lazy

from netbox.ui import attrs, layout
from netbox.ui.panels import CommentsPanel, ObjectAttributesPanel, ObjectsTablePanel
from extras.ui.panels import CustomFieldsPanel, TagsPanel
from netbox.views import generic
from utilities.views import register_model_view

from . import filtersets, forms, tables
from .client import ProxmoxClient, ProxmoxError
from .jobs import ProxmoxSyncJob
from .models import ProxmoxConnection, ProxmoxObject


class SuperuserMixin:
    """The connection decides where the API token secret is sent - changes are for superusers only."""
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


@register_model_view(ProxmoxConnection, 'list', path='', detail=False)
class ProxmoxConnectionListView(generic.ObjectListView):
    queryset = ProxmoxConnection.objects.select_related('cluster', 'site')
    filterset = filtersets.ProxmoxConnectionFilterSet
    filterset_form = forms.ProxmoxConnectionFilterForm
    table = tables.ProxmoxConnectionTable


class ConnectionPanel(ObjectAttributesPanel):
    name = attrs.TextAttr('name')
    url = attrs.TextAttr('url', label=gettext_lazy('API URL'))
    token_id = attrs.TextAttr('token_id', label=gettext_lazy('API token ID'), style='font-monospace')
    token_secret_env = attrs.TextAttr('token_secret_env', label=gettext_lazy('Token secret variable'), style='font-monospace')
    verify_tls = attrs.BooleanAttr('verify_tls', label=gettext_lazy('Verify TLS'))
    enabled = attrs.BooleanAttr('enabled', label=gettext_lazy('Enabled'))
    cluster = attrs.RelatedObjectAttr('cluster', linkify=True, label=gettext_lazy('Cluster'))
    last_sync = attrs.DateTimeAttr('last_sync', label=gettext_lazy('Last sync'))
    description = attrs.TextAttr('description')


class DefaultsPanel(ObjectAttributesPanel):
    site = attrs.RelatedObjectAttr('site', linkify=True, label=gettext_lazy('Site'))
    node_role = attrs.RelatedObjectAttr('node_role', linkify=True, label=gettext_lazy('Node role'))
    node_device_type = attrs.RelatedObjectAttr('node_device_type', linkify=True, label=gettext_lazy('Node device type'))
    vm_role = attrs.RelatedObjectAttr('vm_role', linkify=True, label=gettext_lazy('VM role'))
    container_role = attrs.RelatedObjectAttr('container_role', linkify=True, label=gettext_lazy('Container role'))
    tenant = attrs.RelatedObjectAttr('tenant', linkify=True, label=gettext_lazy('Tenant'))


@register_model_view(ProxmoxConnection)
class ProxmoxConnectionView(generic.ObjectView):
    queryset = ProxmoxConnection.objects.all()
    template_name = 'netbox_proxmox/proxmoxconnection.html'
    layout = layout.SimpleLayout(
        left_panels=[ConnectionPanel(), TagsPanel()],
        right_panels=[DefaultsPanel(title=gettext_lazy('Defaults for new objects')), CommentsPanel(), CustomFieldsPanel()],
        bottom_panels=[ObjectsTablePanel('netbox_proxmox.proxmoxobject',
                                         filters={'connection_id': lambda ctx: ctx['object'].pk},
                                         title=gettext_lazy('Proxmox objects'))],
    )


@register_model_view(ProxmoxConnection, 'add', detail=False)
@register_model_view(ProxmoxConnection, 'edit')
class ProxmoxConnectionEditView(SuperuserMixin, generic.ObjectEditView):
    queryset = ProxmoxConnection.objects.all()
    form = forms.ProxmoxConnectionForm


@register_model_view(ProxmoxConnection, 'delete')
class ProxmoxConnectionDeleteView(SuperuserMixin, generic.ObjectDeleteView):
    queryset = ProxmoxConnection.objects.all()


@register_model_view(ProxmoxConnection, 'bulk_delete', path='delete', detail=False)
class ProxmoxConnectionBulkDeleteView(SuperuserMixin, generic.BulkDeleteView):
    queryset = ProxmoxConnection.objects.all()
    filterset = filtersets.ProxmoxConnectionFilterSet
    table = tables.ProxmoxConnectionTable


class TestConnectionView(SuperuserMixin, View):
    def post(self, request, pk):
        conn = get_object_or_404(ProxmoxConnection, pk=pk)
        try:
            version = ProxmoxClient.from_connection(conn).version()
        except ProxmoxError as exc:
            messages.error(request, _('Connection failed: %(err)s') % {'err': exc})
        else:
            messages.success(request, _('Connected, Proxmox VE %(version)s.') % {'version': version.get('version', '?')})
        return redirect(conn.get_absolute_url())


class SyncNowView(PermissionRequiredMixin, View):
    permission_required = 'netbox_proxmox.change_proxmoxobject'

    def post(self, request, pk):
        conn = get_object_or_404(ProxmoxConnection.objects.restrict(request.user, 'view'), pk=pk)
        job = ProxmoxSyncJob.enqueue(user=request.user, connection_id=conn.pk)
        messages.success(request, _('Sync of %(name)s enqueued.') % {'name': conn})
        return redirect(job.get_absolute_url())


class ProxmoxObjectListView(generic.ObjectListView):
    queryset = ProxmoxObject.objects.select_related('connection', 'device', 'virtual_machine')
    filterset = filtersets.ProxmoxObjectFilterSet
    filterset_form = forms.ProxmoxObjectFilterForm
    table = tables.ProxmoxObjectTable
    actions = ()  # read-only, maintained by the sync
