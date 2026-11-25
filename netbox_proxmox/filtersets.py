import django_filters
from django.db.models import Q

from netbox.filtersets import NetBoxModelFilterSet

from .models import ProxmoxConnection, ProxmoxObject


class ProxmoxConnectionFilterSet(NetBoxModelFilterSet):
    class Meta:
        model = ProxmoxConnection
        fields = ('id', 'name', 'enabled', 'site_id', 'cluster_id')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(url__icontains=value))


class ProxmoxObjectFilterSet(django_filters.FilterSet):
    q = django_filters.CharFilter(method='search')
    connection_id = django_filters.ModelMultipleChoiceFilter(queryset=ProxmoxConnection.objects.all())
    kind = django_filters.MultipleChoiceFilter(choices=ProxmoxObject.KIND_CHOICES)
    state = django_filters.MultipleChoiceFilter(choices=ProxmoxObject.STATE_CHOICES)
    ignored = django_filters.BooleanFilter()

    class Meta:
        model = ProxmoxObject
        fields = ('id', 'proxmox_id', 'node', 'device_id', 'virtual_machine_id')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(proxmox_id__iexact=value) | Q(node__icontains=value))
