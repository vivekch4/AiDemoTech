# views.py
import os
import re
import json
import base64
import requests
import traceback

from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.http import JsonResponse, FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.views.generic import TemplateView
from django.core.exceptions import PermissionDenied
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from .models import ChatSession, ChatMessage
from .constants import (
    DEFAULT_PDF_MIME_TYPE,
    SYSTEM_PROMPT,
    get_default_pdf_base64,
)
from .gemini_router import (
    AllModelsFailed,
    MAX_CALLS_PER_MINUTE,
    calls_in_last_minute,
    logger,
    send_with_fallback,
)

RATE_LIMIT_MESSAGE = "Too many requests right now. Please wait about 30 seconds and try again."
BUSY_MESSAGE = "The AI service is very busy right now. Please try again in a minute."

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def clean_text_for_tts(text: str) -> str:
    """Remove markdown formatting for TTS output."""
    if not text:
        return text
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)          # **bold** / __bold__
    text = re.sub(r"(\*|_)(.*?)\1", r"\2", text)              # *italics* / _italics_
    text = re.sub(r"^#+ (.*?)$", r"\1", text, flags=re.MULTILINE)  # headings
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)           # links
    text = re.sub(r"`(.*?)`", r"\1", text)                    # inline code
    text = re.sub(r"\s+", " ", text).strip()
    return text


def rollback_failed_turn(session, msg, session_created):
    """Remove the user message (and a brand-new empty session) when Gemini fails,
    so a retry does not leave duplicate or unanswered messages."""
    try:
        if msg is not None:
            msg.delete()
        if session_created and session is not None and not session.messages.exists():
            session.delete()
    except Exception:
        logger.error("Rollback failed:\n%s", traceback.format_exc())


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
class ChatView(TemplateView):
    template_name = "chat.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user if self.request.user.is_authenticated else None
        session_id = self.request.GET.get('session_id')

        if session_id:
            session = get_object_or_404(ChatSession, id=session_id, user=user)
            context['active_session_id'] = session.id
            context['messages'] = session.messages.all().order_by('created_at')
        else:
            context['active_session_id'] = None
            context['messages'] = []

        context['chat_sessions'] = ChatSession.objects.filter(user=user).order_by('-updated_at')
        return context


def peak_per_minute(replies):
    """Highest number of requests and tokens inside any 60-second window."""
    peak_rpm = peak_tpm = 0
    start = 0
    window_tokens = 0
    for end, reply in enumerate(replies):
        window_tokens += reply.total_tokens or 0
        while (reply.created_at - replies[start].created_at).total_seconds() >= 60:
            window_tokens -= replies[start].total_tokens or 0
            start += 1
        peak_rpm = max(peak_rpm, end - start + 1)
        peak_tpm = max(peak_tpm, window_tokens)
    return peak_rpm, peak_tpm


class TokenUsageView(TemplateView):
    """Token usage report: every chat's total, plus a per-message breakdown of one chat."""
    template_name = "usage.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user if self.request.user.is_authenticated else None

        sessions = (
            ChatSession.objects.filter(user=user)
            .annotate(
                requests=Count("messages", filter=Q(messages__is_user=False, messages__total_tokens__isnull=False)),
                prompt_tokens=Coalesce(Sum("messages__prompt_tokens"), 0),
                output_tokens=Coalesce(Sum("messages__output_tokens"), 0),
                thinking_tokens=Coalesce(Sum("messages__thinking_tokens"), 0),
                total_tokens=Coalesce(Sum("messages__total_tokens"), 0),
            )
            .order_by("-updated_at")
        )
        context["sessions"] = sessions
        context["grand_total"] = sum(s.total_tokens for s in sessions)
        context["grand_requests"] = sum(s.requests for s in sessions)

        # Live RPM / TPM across all chats (successful requests in the last 60 seconds)
        recent = ChatMessage.objects.filter(
            is_user=False,
            total_tokens__isnull=False,
            created_at__gte=timezone.now() - timedelta(seconds=60),
        )
        context["current_rpm"] = recent.count()
        context["current_tpm"] = recent.aggregate(t=Coalesce(Sum("total_tokens"), 0))["t"]
        context["max_rpm"] = MAX_CALLS_PER_MINUTE

        session_id = self.request.GET.get("session_id")
        if session_id:
            session = get_object_or_404(sessions, id=session_id)
            turns = []
            pending_user = None
            for m in session.messages.order_by("created_at"):
                if m.is_user:
                    pending_user = m
                    continue
                turns.append({
                    "number": len(turns) + 1,
                    "question": pending_user,
                    "reply": m,
                    "has_file": bool(pending_user and pending_user.file_data),
                })
                pending_user = None

            replies = [t["reply"] for t in turns if t["reply"].total_tokens is not None]
            running = 0
            for t in turns:
                running += t["reply"].total_tokens or 0
                t["running_total"] = running

            context["active_session"] = session
            context["turns"] = turns
            context["untracked"] = len(turns) - len(replies)
            context["peak_rpm"], context["peak_tpm"] = peak_per_minute(replies)
            context["max_turn_tokens"] = max([r.total_tokens for r in replies], default=0)
        return context


@method_decorator(csrf_exempt, name='dispatch')
class ChatAPI(APIView):
    def post(self, request):
        session = None
        msg = None
        session_created = False

        try:
            user = request.user if request.user.is_authenticated else None
            session_id = request.data.get('session_id')
            input_text = (request.data.get('message', '') or '').strip()
            file_data = request.data.get('file')
            language = request.data.get('language', 'en-US')  # Default to English

            # === 1. Validate language ===
            if language not in ['en-US', 'hi-IN', 'ja-JP', 'ko-KR']:
                return Response({"error": "Unsupported language."}, status=400)

            # === 2. Validate input ===
            if not input_text and not file_data:
                return Response({"error": "No input provided.", "success": False}, status=400)

            if file_data:
                try:
                    file_data = json.loads(file_data) if isinstance(file_data, str) else file_data
                    file_data["data"], file_data["mimeType"]
                except (ValueError, KeyError, TypeError):
                    return Response({"error": "Invalid file data.", "success": False}, status=400)

            # === 3. Local guard (protects the Gemini free-tier quota) ===
            current_calls = calls_in_last_minute()
            if current_calls >= MAX_CALLS_PER_MINUTE:
                logger.warning(
                    "BLOCKED locally: %s calls in the last minute (max %s)",
                    current_calls, MAX_CALLS_PER_MINUTE,
                )
                return Response({"error": RATE_LIMIT_MESSAGE, "success": False}, status=429)

            # === 4. System instruction based on language ===
            if language == "ja-JP":
                lang_name = "Japanese (日本語)"
                language_instruction = "You must ALWAYS respond ONLY in Japanese (日本語). Never use English. Keep answers concise and natural."
            elif language == "hi-IN":
                lang_name = "Hindi (हिंदी)"
                language_instruction = "You must ALWAYS respond ONLY in Hindi (हिंदी). Never use English. Keep answers concise and natural."
            elif language == "ko-KR":
                lang_name = "Korean (한국어)"
                language_instruction = "You must ALWAYS respond ONLY in Korean (한국어). Never use English. Keep answers concise and natural."
            else:
                lang_name = "English"
                language_instruction = "You must ALWAYS respond ONLY in English. Never use any other language. Keep answers concise and natural."

            # === 5. Get or create session ===
            if session_id:
                session = get_object_or_404(ChatSession, id=session_id, user=user)
                is_new_session = not session.messages.exists()
            else:
                session = ChatSession.objects.create(user=user, title="New Chat")
                session_created = True
                is_new_session = True

            # === 6. Save user message ===
            msg = ChatMessage.objects.create(
                session=session,
                is_user=True,
                text=input_text if input_text else None
            )
            if file_data:
                msg.file_data = file_data['data']
                msg.file_mime_type = file_data['mimeType']
                msg.save()
            elif is_new_session:
                # No file attached, but this is a new chat - auto-attach the default PDF
                msg.file_data = get_default_pdf_base64()
                msg.file_mime_type = DEFAULT_PDF_MIME_TYPE
                msg.save()
                file_data = {
                    "data": msg.file_data,
                    "mimeType": msg.file_mime_type
                }

            # === 7. System instruction (the model is picked in send_with_fallback) ===
            system_instruction = f"{SYSTEM_PROMPT}\n\n{language_instruction}"

            # History from PREVIOUS messages only (the current message is sent below).
            history = []
            for m in session.messages.exclude(id=msg.id).order_by("created_at"):
                role = "user" if m.is_user else "model"
                parts = []
                if m.text:
                    parts.append({"text": m.text})
                if m.file_data:
                    parts.append({
                        "inline_data": {
                            "mime_type": m.file_mime_type,
                            "data": m.file_data
                        }
                    })
                if parts:
                    history.append({"role": role, "parts": parts})

            # === 8. Build the current message (file + text) ===
            current_parts = []
            if file_data:
                current_parts.append({
                    "inline_data": {
                        "mime_type": file_data["mimeType"],
                        "data": file_data["data"]
                    }
                })
            if input_text:
                current_parts.append({
                    "text": (
                        f"The user is communicating in {lang_name}. "
                        f"Respond ONLY in {lang_name}, never switch to another language.\n\n"
                        f"{input_text}"
                    )
                })
            else:
                current_parts.append({"text": f"Respond ONLY in {lang_name}, never switch language."})

            # === 9. Send to Gemini (switches model on "high demand", key on "quota exceeded") ===
            try:
                gemini = send_with_fallback(system_instruction, history, current_parts, session_id=session.id)
            except AllModelsFailed as e:
                rollback_failed_turn(session, msg, session_created)
                if e.rate_limited:
                    return Response({"error": RATE_LIMIT_MESSAGE, "success": False}, status=429)
                return Response({"error": BUSY_MESSAGE, "success": False}, status=503)
            except Exception:
                rollback_failed_turn(session, msg, session_created)
                raise
            result = gemini.response

            # === 10. Read reply (can raise ValueError if the response was blocked/empty) ===
            try:
                reply_text = result.text
            except ValueError:
                reply_text = "Sorry, I could not generate a response for that. Please rephrase and try again."

            # === 11. Save model reply (with token usage for the /usage/ page) ===
            usage = getattr(result, "usage_metadata", None)
            ChatMessage.objects.create(
                session=session,
                is_user=False,
                text=reply_text,
                prompt_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
                thinking_tokens=getattr(usage, "thoughts_token_count", 0) or 0,
                total_tokens=getattr(usage, "total_token_count", 0) or 0,
                latency_seconds=gemini.latency,
                model_name=gemini.model_name,
                api_key_label=gemini.key_label,
                attempts=gemini.attempts,
            )

            # === 12. Generate title ===
            if session.title == "New Chat" and input_text:
                words = input_text.split()
                short_title = " ".join(words[:6])  # first 6 words
                session.title = short_title[:50]   # max 50 chars
                session.save()

            # === 13. TTS clean output ===
            tts_text = clean_text_for_tts(reply_text)

            return Response({
                "response": reply_text,
                "tts_response": tts_text,
                "session_id": session.id,
                "success": True
            })

        except Exception:
            print("Unexpected error:", traceback.format_exc())
            return Response(
                {"error": "Something went wrong. Please try again later.", "success": False},
                status=500
            )


@method_decorator(csrf_exempt, name="dispatch")
class ChatDeleteAPI(APIView):
    def delete(self, request, session_id):
        try:
            user = request.user if request.user.is_authenticated else None
            session = get_object_or_404(ChatSession, id=session_id, user=user)
            session.delete()
            return Response({"success": True, "message": "Chat session deleted successfully."})
        except PermissionDenied:
            return Response({"success": False, "error": "Access denied."}, status=403)
        except Exception:
            print("Delete error:", traceback.format_exc())
            return Response({"success": False, "error": "Something went wrong. Please try again later."}, status=500)


@csrf_exempt
def download_from_qr(request):
    if request.method != "POST":
        return JsonResponse({"error": "Unsupported method."}, status=405)
    try:
        data = json.loads(request.body)
        qr_url = data.get("qr_url")
        if not qr_url or "/d/" not in qr_url:
            return JsonResponse({"error": "Invalid Drive link"}, status=400)
        file_id = qr_url.split("/d/")[1].split("/")[0]
        direct_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        res = requests.get(direct_url, timeout=30)
        if res.status_code == 200:
            file_base64 = base64.b64encode(res.content).decode("utf-8")
            return JsonResponse({"success": True, "data": file_base64, "mime_type": "application/pdf", "name": "Scanned_QR_Document.pdf"})
        return JsonResponse({"error": "Failed to download file"}, status=500)
    except Exception:
        print("QR Download error:", traceback.format_exc())
        return JsonResponse({"error": "Something went wrong. Please try again later."}, status=500)


class PDFPreviewView(APIView):
    """
    Serves chatbot/assets/SERVO_PANEL.pdf so it can be
    opened/rendered inline in a new browser tab.
    """
    permission_classes = [AllowAny]  # tighten later if needed

    def get(self, request):
        pdf_path = os.path.join(settings.BASE_DIR, "chatbot", "assets", "SERVO_PANEL.pdf")

        if not os.path.exists(pdf_path):
            raise Http404("PDF not found.")

        response = FileResponse(open(pdf_path, "rb"), content_type="application/pdf")
        response["Content-Disposition"] = 'inline; filename="SERVO_PANEL.pdf"'
        return response