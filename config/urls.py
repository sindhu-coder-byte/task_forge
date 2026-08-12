"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path,include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('allauth.urls')),
    path('api/v1/', include('core.api.urls')),
    path('', include('core.urls')),
]

# Task attachments (MEDIA_URL) have nowhere else to be served from — there's no
# S3/Cloudinary storage backend configured — so this route is needed in
# production too, not just DEBUG. Gating it behind DEBUG (the Django default)
# meant every uploaded attachment link 404'd as soon as DEBUG=False.
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
