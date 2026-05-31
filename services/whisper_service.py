import base64, asyncio, tempfile, os
from groq import AsyncGroq
from core.config import settings

client = AsyncGroq(api_key=settings.GROQ_API_KEY)

async def transcribe_audio(audio_base64: str) -> dict:
    """
    Transcribe audio using Groq Whisper API.
    Input: base64 encoded audio (webm/wav/mp3)
    Output: {"text": "transcribed text", "success": True}
    """
    if not audio_base64:
        return {"text": "", "success": False, "error": "No audio provided"}
    
    tmp_path = None
    try:
        # Decode base64 to bytes
        audio_bytes = base64.b64decode(audio_base64)
        
        if len(audio_bytes) < 1000:
            return {"text": "", "success": False, "error": "Audio too short"}
        
        # Write to temp file
        with tempfile.NamedTemporaryFile(
            suffix=".webm", delete=False, 
            dir=tempfile.gettempdir()
        ) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        
        # Send to Groq Whisper
        with open(tmp_path, "rb") as audio_file:
            response = await asyncio.wait_for(
                client.audio.transcriptions.create(
                    file=("audio.webm", audio_file, "audio/webm"),
                    model="whisper-large-v3",
                    language="en",
                    response_format="text",
                    temperature=0.0,
                    prompt="Umm, let me think... Currently I am pursuing my degree in Artificial Intelligence. My project is about..."
                ),
                timeout=6.0
            )
        
        text = str(response).strip() if response else ""
        
        if not text:
            return {"text": "", "success": False, "error": "Empty transcription"}
        
        print(f"✅ Whisper transcribed: '{text[:80]}'")
        return {"text": text, "success": True}
    
    except asyncio.TimeoutError:
        print("⚠️ Whisper timeout after 6s")
        return {"text": "", "success": False, "error": "Timeout"}
    
    except Exception as e:
        print(f"⚠️ Whisper error: {e}")
        return {"text": "", "success": False, "error": str(e)}
    
    finally:
        # Always clean up temp file
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except:
                pass


async def transcribe_with_fallback(
    audio_base64: str, 
    fallback_text: str = ""
) -> str:
    """
    Try Whisper first, use fallback_text if fails.
    Returns final transcript string.
    """
    if audio_base64:
        result = await transcribe_audio(audio_base64)
        if result["success"] and result["text"]:
            text = result["text"].strip()
            # Filter common Whisper hallucinations on silence
            lower_text = text.lower()
            hallucinations = [
                "thank you", "thanks for watching", "subscribe", 
                "i don't know.", "this court will not be able",
                "amara.org", "by the way", "you"
            ]
            
            # If the entire text is just a common hallucination and we have a fallback, use fallback
            is_hallucination = False
            if len(text) < 30:
                for h in hallucinations:
                    if h in lower_text:
                        is_hallucination = True
                        break
                        
            if not is_hallucination and len(text) > 0:
                return text
            else:
                print(f"⚠️ Filtered likely Whisper hallucination: '{text}'")
    
    # Fallback to browser Speech Recognition text
    if fallback_text:
        print(f"ℹ️ Using browser speech fallback: '{fallback_text[:60]}'")
        return fallback_text
    
    return ""