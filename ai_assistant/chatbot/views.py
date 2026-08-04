# views.py
import json
import base64
import requests
import traceback
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.views.generic import TemplateView
from rest_framework.views import APIView
from rest_framework.response import Response
from django.core.exceptions import PermissionDenied

from google.generativeai import GenerativeModel, configure
from .models import ChatSession, ChatMessage
from .constants import DEFAULT_PDF_MIME_TYPE, SYSTEM_PROMPT, API_KEY, get_default_pdf_base64
import re
from .constants import get_default_pdf_base64, DEFAULT_PDF_MIME_TYPE

DEFAULT_PDF_B64 = get_default_pdf_base64()



try:
    configure(api_key=API_KEY)
except Exception as config_error:
    print("Failed to configure API:", config_error)


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

def clean_text_for_tts(text: str) -> str:
    """Remove markdown formatting for TTS output."""
    if not text:
        return text
    # Remove **bold** and __underline__
    text = re.sub(r"[\*\*_]{2}(.*?)\1{2}", r"\1", text)
    # Remove *italics* and _italics_
    text = re.sub(r"[\*_](.*?)\1", r"\1", text)
    # Remove markdown headings (#, ##, etc.)
    text = re.sub(r"^#+ (.*?)$", r"\1", text, flags=re.MULTILINE)
    # Remove markdown links [text](url)
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)
    # Remove inline code `code`
    text = re.sub(r"`(.*?)`", r"\1", text)
    # Clean extra spaces and newlines
    text = re.sub(r"\s+", " ", text).strip()
    return text

@method_decorator(csrf_exempt, name='dispatch')
class ChatAPI(APIView):
    def post(self, request):
        try:
            user = request.user if request.user.is_authenticated else None
            session_id = request.data.get('session_id')
            input_text = request.data.get('message', '').strip()
            file_data = request.data.get('file')
            language = request.data.get('language', 'en-US')  # Default to English

            # === 1. Validate language ===
            if language not in ['en-US', 'hi-IN', 'ja-JP', 'ko-KR']:

                return Response({"error": "Unsupported language."}, status=400)

            # === 2. System instruction based on language ===
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

            # === 3. Get or create session ===
            # === 3. Get or create session ===
            if session_id:
                session = get_object_or_404(ChatSession, id=session_id, user=user)
                is_new_session = not session.messages.exists()
            else:
                session = ChatSession.objects.create(user=user, title="New Chat")
                is_new_session = True

            # === 4. Save user message ===
            # === 4. Save user message ===
            if input_text or file_data:
                msg = ChatMessage.objects.create(
                    session=session,
                    is_user=True,
                    text=input_text if input_text else None
                )
                if file_data:
                    file_data = json.loads(file_data)
                    msg.file_data = file_data['data']
                    msg.file_mime_type = file_data['mimeType']
                    msg.save()
                elif is_new_session:
                    # No file attached by user, but this is a new chat — auto-attach default PDF
                    msg.file_data = get_default_pdf_base64()
                    msg.file_mime_type = DEFAULT_PDF_MIME_TYPE
                    msg.save()
                    file_data = {
                        "data": msg.file_data,
                        "mimeType": msg.file_mime_type
                    }

            # === 5. Create model with system instruction ===
            model = GenerativeModel(
                model_name="gemini-2.5-flash",
                system_instruction=f"{SYSTEM_PROMPT}\n\n{language_instruction}"
            )

            # Build history
            history = []
            for m in session.messages.all().order_by("created_at"):
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

            chat = model.start_chat(history=history)

            # === 6. Force user input into correct language context ===
            wrapped_input = (
                f"The user is communicating in {lang_name}. "
                f"Respond ONLY in {lang_name}, never switch to another language.\n\n"
                f"{input_text}" if input_text else None
            )

            # === 7. Send new message ===
            if input_text:
                result = chat.send_message(wrapped_input)
            elif file_data:
                result = chat.send_message([
                    {"inline_data": {
                        "mime_type": file_data["mimeType"],
                        "data": file_data["data"]
                    }},
                    {"text": f"Respond ONLY in {lang_name}, never switch language."}
                ])
            else:
                return Response({"error": "No input provided."}, status=400)

            # === 8. Save model reply ===
            ChatMessage.objects.create(session=session, is_user=False, text=result.text)

            # === 9. Generate title ===
            if session.title == "New Chat" and input_text:
                words = input_text.split()
                short_title = " ".join(words[:6])  # first 6 words
                session.title = short_title[:50]   # max 50 chars
                session.save()

            # === 10. TTS clean output ===
            tts_text = clean_text_for_tts(result.text)

            return Response({
                "response": result.text,
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
        if "/d/" not in qr_url:
            return JsonResponse({"error": "Invalid Drive link"}, status=400)
        file_id = qr_url.split("/d/")[1].split("/")[0]
        direct_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        res = requests.get(direct_url)
        if res.status_code == 200:
            file_base64 = base64.b64encode(res.content).decode("utf-8")
            return JsonResponse({"success": True, "data": file_base64, "mime_type": "application/pdf", "name": "Scanned_QR_Document.pdf"})
        return JsonResponse({"error": "Failed to download file"}, status=500)
    except Exception:
        print("QR Download error:", traceback.format_exc())
        return JsonResponse({"error": "Something went wrong. Please try again later."}, status=500)
    
    

import os
from django.conf import settings
from django.http import FileResponse, Http404
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny


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