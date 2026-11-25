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
import logging

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse

from dcim.filtersets import DeviceFilterSet
from dcim.forms import DeviceFilterForm
from dcim.models import Device
from utilities.request import safe_for_redirect
from virtualization.filtersets import VirtualMachineFilterSet
from virtualization.forms import VirtualMachineFilterForm
from virtualization.models import VirtualMachine

from .jobs import ProxmoxSyncJob
from .models import IgnoredOrphan, ProxmoxConnection, ProxmoxObject
from .object_actions import (BulkDecommissionOrphans, BulkIgnoreOrphans, BulkIgnoreSelected, BulkSyncSelected,
                             BulkUnignoreSelected)
from .sync import assign

CHANGE = 'netbox_proxmox.change_proxmoxobject'
LOG = logging.getLogger('netbox_proxmox')


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


def _return_url(request, default):
    url = request.POST.get('return_url') or request.GET.get('return_url')
    return url if url and safe_for_redirect(url) else reverse(default)


def _selected(request, queryset, filterset):
    """pks of the ticked rows, or all filtered rows ("select all")."""
    if request.POST.get('_all'):
        return list(filterset(request.GET, queryset=queryset).qs.values_list('pk', flat=True))
    return list(queryset.filter(pk__in=request.POST.getlist('pk')).values_list('pk', flat=True))


# -- sync overview (Proxmox side) ------------------------------------------------------------
class SyncOverviewView(generic.ObjectListView):
    queryset = ProxmoxObject.objects.exclude(kind='cluster').select_related('connection', 'device',
                                                                            'virtual_machine')
    filterset = filtersets.ProxmoxObjectFilterSet
    filterset_form = forms.ProxmoxObjectFilterForm
    table = tables.ProxmoxObjectTable
    template_name = 'netbox_proxmox/sync_list.html'
    actions = (BulkSyncSelected, BulkIgnoreSelected, BulkUnignoreSelected)

    def get(self, request, *args, **kwargs):
        if 'ignored' not in request.GET:  # default view: hide ignored objects
            request.GET = request.GET.copy()
            request.GET['ignored'] = 'false'
        return super().get(request, *args, **kwargs)

    def get_extra_context(self, request):
        return {'connections': ProxmoxConnection.objects.restrict(request.user, 'view').filter(enabled=True)}


class FetchView(PermissionRequiredMixin, View):
    """Fetch from Proxmox only: nothing is written to NetBox."""
    permission_required = CHANGE

    def post(self, request):
        connections = ProxmoxConnection.objects.restrict(request.user, 'view')
        conn_id = request.POST.get('connection_id')
        if conn_id:
            conn = get_object_or_404(connections, pk=conn_id)
            job = ProxmoxSyncJob.enqueue(user=request.user, mode='fetch', connection_id=conn.pk)
        else:
            job = ProxmoxSyncJob.enqueue(user=request.user, mode='fetch')
        messages.success(request, _('Fetching from Proxmox enqueued. Nothing is changed in NetBox yet.'))
        return redirect(job.get_absolute_url())


class BulkSyncView(PermissionRequiredMixin, View):
    """Write the fetched data of the selected objects (create or update) as the requesting user."""
    permission_required = CHANGE

    def post(self, request):
        ids = _selected(request, ProxmoxObject.objects.restrict(request.user, 'change').exclude(kind='cluster'),
                        filtersets.ProxmoxObjectFilterSet)
        if not ids:
            messages.warning(request, _('No objects selected.'))
            return redirect(_return_url(request, 'plugins:netbox_proxmox:proxmoxobject_list'))
        job = ProxmoxSyncJob.enqueue(user=request.user, mode='apply', object_ids=ids)
        messages.success(request, _('Sync of %(count)d object(s) enqueued.') % {'count': len(ids)})
        return redirect(job.get_absolute_url())


class BulkIgnoreView(PermissionRequiredMixin, View):
    permission_required = CHANGE
    ignore = True

    def post(self, request):
        qs = ProxmoxObject.objects.restrict(request.user, 'change').exclude(kind='cluster')
        ids = _selected(request, qs, filtersets.ProxmoxObjectFilterSet)
        count = qs.filter(pk__in=ids).update(ignored=self.ignore)
        msg = _('%(count)d object(s) ignored.') if self.ignore else _('%(count)d object(s) no longer ignored.')
        messages.success(request, msg % {'count': count})
        return redirect(_return_url(request, 'plugins:netbox_proxmox:proxmoxobject_list'))


class LinkMixin(PermissionRequiredMixin):
    permission_required = CHANGE

    def link(self, request, pk):
        return get_object_or_404(ProxmoxObject.objects.restrict(request.user, 'change').exclude(kind='cluster'),
                                 pk=pk)


class AssignView(LinkMixin, View):
    """Link a Proxmox object to an existing NetBox object. Only the link is stored; changes follow on sync."""
    template_name = 'netbox_proxmox/assign.html'

    def get(self, request, pk):
        link = self.link(request, pk)
        return render(request, self.template_name, {
            'link': link, 'form': forms.AssignNetBoxObjectForm(link=link, user=request.user)})

    def post(self, request, pk):
        link = self.link(request, pk)
        form = forms.AssignNetBoxObjectForm(request.POST, link=link, user=request.user)
        if form.is_valid():
            try:
                assign(link, form.cleaned_data['target'], LOG)
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, _('%(name)s linked to %(obj)s. Review the pending changes and sync.') % {
                    'name': link.name, 'obj': form.cleaned_data['target']})
                return redirect(_return_url(request, 'plugins:netbox_proxmox:proxmoxobject_list'))
        return render(request, self.template_name, {'link': link, 'form': form})


class ToggleIgnoreView(LinkMixin, View):
    template_name = 'netbox_proxmox/confirm.html'

    def get(self, request, pk):
        link = self.link(request, pk)
        question = _('Stop ignoring %(name)s?') if link.ignored else \
            _('Ignore %(name)s? It will not be written to NetBox, also not by the daily sync.')
        return render(request, self.template_name, {'question': question % {'name': link.name},
                                                    'return_url': _return_url(request, 'plugins:netbox_proxmox:proxmoxobject_list')})

    def post(self, request, pk):
        link = self.link(request, pk)
        ProxmoxObject.objects.filter(pk=link.pk).update(ignored=not link.ignored)
        return redirect(_return_url(request, 'plugins:netbox_proxmox:proxmoxobject_list'))


# -- orphans (NetBox side) -------------------------------------------------------------------
ORPHAN_MODELS = {'vms': VirtualMachine, 'devices': Device}


def orphan_queryset(model, ignored=False):
    """NetBox objects in a Proxmox cluster without a current Proxmox counterpart."""
    clusters = ProxmoxConnection.objects.exclude(cluster__isnull=True).values_list('cluster_id', flat=True)
    qs = model.objects.filter(Q(proxmox_object__isnull=True, cluster_id__in=clusters) | Q(proxmox_object__state='stale'))
    return qs.filter(proxmox_ignored__isnull=not ignored)


class OrphanListView(generic.ObjectListView):
    template_name = 'netbox_proxmox/orphan_list.html'
    actions = (BulkDecommissionOrphans, BulkIgnoreOrphans)
    kind = 'vms'

    def dispatch(self, request, *args, kind='vms', **kwargs):
        if kind not in ORPHAN_MODELS:
            raise Http404
        self.kind = kind
        model = ORPHAN_MODELS[kind]
        ignored = request.GET.get('ignored') == 'true'
        self.queryset = orphan_queryset(model, ignored).select_related('proxmox_object__connection')
        if kind == 'vms':
            self.table, self.filterset = tables.OrphanVMTable, VirtualMachineFilterSet
            self.filterset_form = VirtualMachineFilterForm
        else:
            self.table, self.filterset = tables.OrphanDeviceTable, DeviceFilterSet
            self.filterset_form = DeviceFilterForm
        return super().dispatch(request, *args, **kwargs)

    def get_extra_context(self, request):
        return {'kind': self.kind, 'show_ignored': request.GET.get('ignored') == 'true'}


class OrphanMixin(PermissionRequiredMixin):
    permission_required = 'netbox_proxmox.change_ignoredorphan'

    def target(self, request, kind, pk, action='change'):
        if kind not in ORPHAN_MODELS:
            raise Http404
        return get_object_or_404(ORPHAN_MODELS[kind].objects.restrict(request.user, action), pk=pk)


class OrphanAssignView(OrphanMixin, View):
    permission_required = CHANGE
    template_name = 'netbox_proxmox/orphan_assign.html'

    def get(self, request, kind, pk):
        target = self.target(request, kind, pk)
        return render(request, self.template_name, {
            'target': target, 'form': forms.AssignProxmoxObjectForm(target=target, user=request.user)})

    def post(self, request, kind, pk):
        target = self.target(request, kind, pk)
        form = forms.AssignProxmoxObjectForm(request.POST, target=target, user=request.user)
        if form.is_valid():
            link = form.cleaned_data['link']
            try:
                assign(link, target, LOG)
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, _('%(obj)s linked to %(name)s. Review the pending changes and sync.') % {
                    'obj': target, 'name': link.name})
                return redirect(reverse('plugins:netbox_proxmox:proxmoxobject_list') + f'?q={link.proxmox_id}')
        return render(request, self.template_name, {'target': target, 'form': form})


class OrphanToggleIgnoreView(OrphanMixin, View):
    template_name = 'netbox_proxmox/confirm.html'

    def get(self, request, kind, pk):
        target = self.target(request, kind, pk)
        ignored = hasattr(target, 'proxmox_ignored')
        question = _('Show %(obj)s as orphan again?') if ignored else _('Ignore %(obj)s on the orphans page?')
        return render(request, self.template_name, {
            'question': question % {'obj': target},
            'return_url': reverse('plugins:netbox_proxmox:orphan_list', kwargs={'kind': kind})})

    def post(self, request, kind, pk):
        target = self.target(request, kind, pk)
        field = 'device' if kind == 'devices' else 'virtual_machine'
        deleted, _x = IgnoredOrphan.objects.filter(**{field: target}).delete()
        if not deleted:
            IgnoredOrphan.objects.create(**{field: target})
        return redirect('plugins:netbox_proxmox:orphan_list', kind=kind)


class OrphanBulkView(OrphanMixin, View):
    action = 'ignore'

    def post(self, request, kind):
        if kind not in ORPHAN_MODELS:
            raise Http404
        model = ORPHAN_MODELS[kind]
        filterset = VirtualMachineFilterSet if kind == 'vms' else DeviceFilterSet
        targets = model.objects.filter(pk__in=_selected(
            request, orphan_queryset(model).restrict(request.user, 'change'), filterset))
        field = 'device' if kind == 'devices' else 'virtual_machine'
        count = 0
        for target in targets:
            if self.action == 'ignore':
                IgnoredOrphan.objects.get_or_create(**{field: target})
            elif target.status != 'decommissioning':
                target.snapshot()  # web request: changelog via NetBox's own request tracking
                target.status = 'decommissioning'
                target.full_clean()
                target.save()
            count += 1
        msg = _('%(count)d object(s) ignored.') if self.action == 'ignore' else \
            _('%(count)d object(s) set to decommissioning.')
        messages.success(request, msg % {'count': count})
        return redirect('plugins:netbox_proxmox:orphan_list', kind=kind)
