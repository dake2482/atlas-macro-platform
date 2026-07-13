from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("research.urls")),
]

handler404 = "research.views.page_not_found"
