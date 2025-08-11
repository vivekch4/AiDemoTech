from django.urls import path
from .views import *

urlpatterns = [
    path('', ChatView.as_view(), name='chat'),
    path('chat/', ChatView.as_view(), name='chat'),
    path('api/chat/', ChatAPI.as_view(), name='chat_api'),
    path('api/chat/delete/<int:session_id>/', ChatDeleteAPI.as_view(), name='chat_delete_api'),
    path('api/download_from_qr/', download_from_qr, name='download_from_qr'),
]
