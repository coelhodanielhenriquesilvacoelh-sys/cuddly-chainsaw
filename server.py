from fastapi import FastAPI, APIRouter, HTTPException, Header, Depends
from fastapi.responses import Response
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from elevenlabs.client import ElevenLabs
from elevenlabs import VoiceSettings
import os
import logging
import base64
import hashlib
import random
import string
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Optional, List
import uuid
from datetime import datetime, timezone


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Configure logging early so routes can use logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# ElevenLabs client
ELEVENLABS_API_KEY = os.environ.get('ELEVENLABS_API_KEY')
eleven_client = ElevenLabs(api_key=ELEVENLABS_API_KEY) if ELEVENLABS_API_KEY else None

# Admin password (for code management)
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'ecoquest2026')

# Default Portuguese-friendly voice (multilingual). "Rachel" is a common default.
DEFAULT_VOICE_ID = "Xb7hH8MSUJpSbSDYk0k2"  # Alice - Clear, Engaging Educator (multilingual)

# Create the main app without a prefix
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")


# ---------- Models ----------
class StatusCheck(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StatusCheckCreate(BaseModel):
    client_name: str


class TTSRequest(BaseModel):
    text: str
    voice_id: Optional[str] = None
    stability: float = 0.5
    similarity_boost: float = 0.75


class TTSResponse(BaseModel):
    audio_base64: str
    text: str
    cached: bool = False


class ScoreRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    player_name: str
    total_energy: int
    phases_completed: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ScoreCreate(BaseModel):
    player_name: str
    total_energy: int
    phases_completed: int


# ---------- Routes ----------
@api_router.get("/")
async def root():
    return {"message": "EcoEnergy Quest API", "tts_enabled": eleven_client is not None}


@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    status_obj = StatusCheck(**input.model_dump())
    doc = status_obj.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()
    await db.status_checks.insert_one(doc)
    return status_obj


@api_router.post("/tts/generate", response_model=TTSResponse)
async def generate_tts(req: TTSRequest):
    """Generate PT-BR narration using ElevenLabs. Caches by (text, voice) hash in MongoDB."""
    if eleven_client is None:
        raise HTTPException(status_code=503, detail="TTS service not configured (missing API key).")

    voice_id = req.voice_id or DEFAULT_VOICE_ID
    cache_key = hashlib.sha256(f"{voice_id}::{req.text}".encode()).hexdigest()

    # Check cache first
    cached = await db.tts_cache.find_one({"cache_key": cache_key}, {"_id": 0})
    if cached:
        return TTSResponse(audio_base64=cached["audio_base64"], text=req.text, cached=True)

    try:
        audio_iter = eleven_client.text_to_speech.convert(
            text=req.text,
            voice_id=voice_id,
            model_id="eleven_multilingual_v2",
            voice_settings=VoiceSettings(
                stability=req.stability,
                similarity_boost=req.similarity_boost,
                style=0.0,
                use_speaker_boost=True,
            ),
            output_format="mp3_44100_128",
        )
        audio_bytes = b"".join(chunk for chunk in audio_iter)
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

        # Cache
        await db.tts_cache.insert_one({
            "cache_key": cache_key,
            "voice_id": voice_id,
            "text": req.text,
            "audio_base64": audio_b64,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

        return TTSResponse(audio_base64=audio_b64, text=req.text, cached=False)
    except Exception as e:
        logger.exception("TTS generation failed")
        raise HTTPException(status_code=500, detail=f"TTS generation failed: {str(e)}")


@api_router.post("/scores", response_model=ScoreRecord)
async def save_score(input: ScoreCreate):
    score_obj = ScoreRecord(**input.model_dump())
    doc = score_obj.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()
    await db.scores.insert_one(doc)
    return score_obj


@api_router.get("/scores/top")
async def top_scores(limit: int = 10):
    cursor = db.scores.find({}, {"_id": 0}).sort("total_energy", -1).limit(limit)
    scores = await cursor.to_list(limit)
    return {"scores": scores}


# ---------- Access Code (student login) ----------
class AccessCode(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    code: str
    label: str = ""
    max_uses: int = 0  # 0 = unlimited
    uses_count: int = 0
    active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AccessCodeCreate(BaseModel):
    label: str = ""
    max_uses: int = 0


class AccessVerifyRequest(BaseModel):
    name: str
    code: str


def generate_code_string() -> str:
    # ECO-XXXX format with unambiguous alphabet
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "ECO-" + "".join(random.choices(alphabet, k=4))


def require_admin(x_admin_password: Optional[str] = Header(default=None)):
    if not x_admin_password or x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    return True


@api_router.post("/admin/login")
async def admin_login(body: dict):
    pwd = (body or {}).get("password", "")
    if pwd != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Senha incorreta")
    return {"ok": True}


@api_router.post("/admin/codes", response_model=AccessCode)
async def create_code(input: AccessCodeCreate, _: bool = Depends(require_admin)):
    for _attempt in range(10):
        code_str = generate_code_string()
        existing = await db.access_codes.find_one({"code": code_str})
        if not existing:
            break
    else:
        raise HTTPException(status_code=500, detail="Could not generate unique code")
    obj = AccessCode(code=code_str, label=input.label or "", max_uses=max(0, input.max_uses))
    doc = obj.model_dump()
    doc["created_at"] = doc["created_at"].isoformat()
    await db.access_codes.insert_one(doc)
    return obj


@api_router.get("/admin/codes")
async def list_codes(_: bool = Depends(require_admin)):
    cursor = db.access_codes.find({}, {"_id": 0}).sort("created_at", -1).limit(500)
    codes = await cursor.to_list(500)
    return {"codes": codes}


@api_router.delete("/admin/codes/{code_id}")
async def delete_code(code_id: str, _: bool = Depends(require_admin)):
    res = await db.access_codes.delete_one({"id": code_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Code not found")
    return {"ok": True}


@api_router.post("/admin/codes/{code_id}/toggle")
async def toggle_code(code_id: str, _: bool = Depends(require_admin)):
    doc = await db.access_codes.find_one({"id": code_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Code not found")
    new_active = not bool(doc.get("active", True))
    await db.access_codes.update_one({"id": code_id}, {"$set": {"active": new_active}})
    return {"ok": True, "active": new_active}


@api_router.get("/admin/usages/{code_id}")
async def code_usages(code_id: str, _: bool = Depends(require_admin)):
    cursor = db.code_usages.find({"code_id": code_id}, {"_id": 0}).sort("used_at", -1).limit(200)
    items = await cursor.to_list(200)
    return {"usages": items}


@api_router.post("/access/verify")
async def verify_access(req: AccessVerifyRequest):
    name = (req.name or "").strip()
    code = (req.code or "").strip().upper()
    if not name or not code:
        raise HTTPException(status_code=400, detail="Nome e chave são obrigatórios")
    doc = await db.access_codes.find_one({"code": code}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Chave inválida")
    if not doc.get("active", True):
        raise HTTPException(status_code=403, detail="Chave desativada")
    max_uses = int(doc.get("max_uses") or 0)
    uses_count = int(doc.get("uses_count") or 0)
    if max_uses > 0 and uses_count >= max_uses:
        raise HTTPException(status_code=403, detail="Chave atingiu o limite de usos")
    await db.access_codes.update_one({"code": code}, {"$inc": {"uses_count": 1}})
    await db.code_usages.insert_one({
        "id": str(uuid.uuid4()),
        "code_id": doc["id"],
        "code": code,
        "player_name": name,
        "used_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"ok": True, "name": name, "code": code, "label": doc.get("label", "")}


# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
