from django.urls import include, path

from utilities.urls import get_model_urls

from . import views

app_name = 'netbox_proxmox'

urlpatterns = (
    path('connections/', include(get_model_urls('netbox_proxmox', 'proxmoxconnection', detail=False))),
    path('connections/<int:pk>/', include(get_model_urls('netbox_proxmox', 'proxmoxconnection'))),
    path('connections/<int:pk>/test/', views.TestConnectionView.as_view(), name='test_connection'),
    path('connections/<int:pk>/sync/', views.SyncNowView.as_view(), name='sync_now'),
    path('objects/', views.ProxmoxObjectListView.as_view(), name='proxmoxobject_list'),
)
