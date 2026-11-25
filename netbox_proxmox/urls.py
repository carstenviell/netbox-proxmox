from django.urls import include, path

from utilities.urls import get_model_urls

from . import views

app_name = 'netbox_proxmox'

urlpatterns = (
    path('connections/', include(get_model_urls('netbox_proxmox', 'proxmoxconnection', detail=False))),
    path('connections/<int:pk>/', include(get_model_urls('netbox_proxmox', 'proxmoxconnection'))),
    path('connections/<int:pk>/test/', views.TestConnectionView.as_view(), name='test_connection'),

    # sync overview: Proxmox objects from the last fetch
    path('sync/', views.SyncOverviewView.as_view(), name='proxmoxobject_list'),
    path('sync/fetch/', views.FetchView.as_view(), name='fetch'),
    path('sync/bulk-sync/', views.BulkSyncView.as_view(), name='bulk_sync'),
    path('sync/bulk-ignore/', views.BulkIgnoreView.as_view(ignore=True), name='bulk_ignore'),
    path('sync/bulk-unignore/', views.BulkIgnoreView.as_view(ignore=False), name='bulk_unignore'),
    path('sync/<int:pk>/assign/', views.AssignView.as_view(), name='assign'),
    path('sync/<int:pk>/ignore/', views.ToggleIgnoreView.as_view(), name='toggle_ignore'),

    # orphans: NetBox objects without Proxmox counterpart
    path('orphans/', views.OrphanListView.as_view(), {'kind': 'vms'}, name='orphans'),
    path('orphans/<str:kind>/', views.OrphanListView.as_view(), name='orphan_list'),
    path('orphans/<str:kind>/bulk-decommission/', views.OrphanBulkView.as_view(action='decommission'),
         name='orphan_bulk_decommission'),
    path('orphans/<str:kind>/bulk-ignore/', views.OrphanBulkView.as_view(action='ignore'), name='orphan_bulk_ignore'),
    path('orphans/<str:kind>/<int:pk>/assign/', views.OrphanAssignView.as_view(), name='orphan_assign'),
    path('orphans/<str:kind>/<int:pk>/ignore/', views.OrphanToggleIgnoreView.as_view(), name='orphan_toggle_ignore'),
)
