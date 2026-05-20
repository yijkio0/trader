from django.urls import path
from . import views

urlpatterns = [
    path('',              views.index,          name='index'),
    path('api/status/',   views.api_status,     name='api_status'),
    path('api/trades/',   views.api_trades,     name='api_trades'),
    path('api/daily/',    views.api_daily,      name='api_daily'),
    path('api/signals/',  views.api_signals,    name='api_signals'),
    path('api/heartbeat/',views.api_heartbeat,  name='api_heartbeat'),
    path('api/crashes/',  views.api_crashes,    name='api_crashes'),
    path('api/control/',  views.api_control,    name='api_control'),
    path('api/config/',   views.api_config,     name='api_config'),
]
