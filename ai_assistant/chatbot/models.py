from django.db import models
from django.contrib.auth.models import User

class ChatSession(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True)
    title = models.CharField(max_length=255, default="New Chat")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.title} ({self.created_at})"

class ChatMessage(models.Model):
    session = models.ForeignKey(ChatSession, related_name="messages", on_delete=models.CASCADE)
    is_user = models.BooleanField(default=True)
    text = models.TextField(blank=True, null=True)
    file_data = models.TextField(blank=True, null=True)  # Base64 encoded file
    file_mime_type = models.CharField(max_length=100, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Gemini token usage for the request that produced this AI reply (empty on user messages)
    prompt_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    thinking_tokens = models.PositiveIntegerField(null=True, blank=True)
    total_tokens = models.PositiveIntegerField(null=True, blank=True)
    latency_seconds = models.FloatField(null=True, blank=True)
    model_name = models.CharField(max_length=100, blank=True, null=True)     # model that answered
    api_key_label = models.CharField(max_length=20, blank=True, null=True)   # "key1", "key2", ... (never the key)
    attempts = models.PositiveSmallIntegerField(null=True, blank=True)       # >1 means a fallback was used

    def __str__(self):
        return f"{'User' if self.is_user else 'AI'}: {self.text or 'File'}"
