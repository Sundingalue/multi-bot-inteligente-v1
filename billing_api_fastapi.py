from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from datetime import datetime
import firebase_admin
from firebase_admin import db

billing_router = APIRouter()

def record_openai_usage(bot_name: str, model_name: str, input_tokens: int, output_tokens: int):
    # Esta es la misma lógica que tenías, pero en una función separada
    # para que pueda ser llamada desde cualquier parte de la app.
    if not bot_name or not model_name or not input_tokens or not output_tokens:
        print("⚠️ No se pudo registrar el uso de OpenAI: datos incompletos.")
        return

    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ref = db.reference(f"billing/usage/{bot_name}")
        data = ref.get() or {}

        # Inicializar si no existe
        data.setdefault("bot_name", bot_name)
        data.setdefault("input_tokens", 0)
        data.setdefault("output_tokens", 0)
        data.setdefault("cost", 0.0) # Asumir un costo o calcularlo

        # Acumular tokens
        data["input_tokens"] += input_tokens
        data["output_tokens"] += output_tokens

        # Guardar en Firebase
        ref.set(data)

        # Puedes agregar lógica para registrar cada evento de uso en un subnodo
        history_ref = db.reference(f"billing/history/{bot_name}")
        history_ref.push({
            "timestamp": timestamp,
            "model": model_name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens
        })

    except Exception as e:
        print(f"❌ Error al registrar el uso de OpenAI en Firebase: {e}")

@billing_router.get("/status")
def get_status():
    # Lógica para obtener el estado de la API, por ejemplo, si está activa
    return JSONResponse(content={"status": "ok", "service": "billing_api"})

# Puedes agregar más rutas de API si las necesitas en el futuro

# Por ejemplo:
@billing_router.get("/usage/{bot_name}")
def get_usage_by_bot(bot_name: str):
    try:
        ref = db.reference(f"billing/usage/{bot_name}")
        data = ref.get()
        return JSONResponse(content=data or {})
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)