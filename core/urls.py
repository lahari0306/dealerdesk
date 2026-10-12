from django.urls import path

from . import order_views, sync, views

urlpatterns = [
    path("health/", views.health),
    path("products/", views.ProductListCreate.as_view()),
    path("products/<str:sku>/", views.ProductDetail.as_view()),
    path("inventory/<str:sku>/", views.InventoryUpdate.as_view()),
    path("dealers/", views.DealerListCreate.as_view()),
    path("dealers/<int:pk>/", views.DealerDetail.as_view()),
    path("orders/", order_views.OrderListCreate.as_view()),
    path("orders/<str:order_number>/", order_views.OrderDetail.as_view()),
    path("orders/<str:order_number>/confirm/", order_views.OrderAction.as_view(action="confirm")),
    path("orders/<str:order_number>/dispatch/", order_views.OrderAction.as_view(action="dispatch")),
    path("orders/<str:order_number>/deliver/", order_views.OrderAction.as_view(action="deliver")),
    path("orders/<str:order_number>/cancel/", order_views.OrderAction.as_view(action="cancel")),
    path("sync/supplier/", sync.SupplierSync.as_view()),
]