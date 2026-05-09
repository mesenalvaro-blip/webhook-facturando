"""
Effort Sparring — FastAPI Server
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contextlib import asynccontextmanager
from typing import Optional, List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from engine.pace_engine import (
    SegmentInput,
    WeatherData,
    calculate_segment,
)
from api.weather import fetch_weather, fetch_elevation
from api.database import init_db, create_session, get_session, list_sessions


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="Effort Sparring",
    description="Motor de pace ajustado por esfuerzo real para corredores",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class SegmentRequest(BaseModel):
    velocidad: float = Field(..., description="Velocidad actual en m/s", gt=0, le=12)
    inclinacion: float = Field(0.0, description="Inclinación en % (positivo = subida)")
    peso: float = Field(..., description="Peso corporal en kg", gt=20, le=300)
    pace_objetivo: float = Field(..., description="Pace objetivo en s/km (ej. 360 = 6:00/km)", gt=100, le=900)
    fc_actual: float = Field(..., description="FC actual en lpm", gt=30, le=250)
    fc_max: float = Field(..., description="FC máxima en lpm", gt=100, le=250)
    fc_reposo: float = Field(..., description="FC en reposo en lpm", gt=30, le=100)
    superficie: str = Field("road", description="Tipo de superficie: road, trail, sand, snow, grass, track, treadmill")
    lat: Optional[float] = Field(None, description="Latitud GPS (activa clima y elevación reales)")
    lng: Optional[float] = Field(None, description="Longitud GPS")

    @field_validator("superficie")
    @classmethod
    def validate_superficie(cls, v: str) -> str:
        valid = {"road", "track", "trail", "sand", "snow", "grass", "treadmill"}
        if v.lower() not in valid:
            raise ValueError(f"superficie debe ser una de: {', '.join(sorted(valid))}")
        return v.lower()

    @field_validator("fc_actual")
    @classmethod
    def fc_actual_below_max(cls, v, info):
        return v


class SegmentResponse(BaseModel):
    pace_ajustado: float
    pace_ajustado_str: str
    factor_combinado: float
    factor_fc: float
    factor_pendiente: float
    factor_superficie: float
    factor_clima: float
    calorias_km: float
    carbs_hora: float
    hidratacion_hora: float
    zona_fc: str
    hrr_pct: float
    velocidad_ajustada_ms: float
    weather_used: Optional[dict] = None
    elevation_m: Optional[float] = None


class SessionCreateRequest(BaseModel):
    name: Optional[str] = None
    segments: List[dict] = Field(default_factory=list)
    summary: Optional[dict] = None


class SessionResponse(BaseModel):
    id: str
    created_at: str
    name: Optional[str]
    segments: Optional[List[dict]] = None
    summary: Optional[dict] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/", tags=["Health"])
async def root():
    return {"status": "ok", "app": "Effort Sparring", "version": "1.0.0"}


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "healthy"}


@app.post("/segment", response_model=SegmentResponse, tags=["Engine"])
async def segment(req: SegmentRequest):
    """
    Calcula el pace ajustado por esfuerzo real para un segmento de carrera.

    Si se proveen `lat` y `lng`:
    - Se consulta Open-Meteo para clima actual (temperatura, humedad, viento, lluvia)
    - Se consulta Open-Meteo Elevation para elevación real
    """
    weather = WeatherData()
    weather_dict = None
    elevation_m = None
    elevation_grade = None

    if req.lat is not None and req.lng is not None:
        weather, elevation_m = await _fetch_geo(req.lat, req.lng)
        weather_dict = {
            "temperature_c":    weather.temperature_c,
            "apparent_temp_c":  weather.apparent_temp_c,
            "humidity_pct":     weather.humidity_pct,
            "wind_speed_ms":    weather.wind_speed_ms,
            "precipitation_mm": weather.precipitation_mm,
        }
        # elevation_grade stays None (single point — can't compute grade without two points)

    inp = SegmentInput(
        velocidad_ms          = req.velocidad,
        inclinacion_pct       = req.inclinacion,
        pace_objetivo_s_km    = req.pace_objetivo,
        peso_kg               = req.peso,
        fc_actual             = req.fc_actual,
        fc_max                = req.fc_max,
        fc_reposo             = req.fc_reposo,
        superficie            = req.superficie,
        lat                   = req.lat,
        lng                   = req.lng,
        weather               = weather,
        elevation_grade_pct   = elevation_grade,
    )

    out = calculate_segment(inp)

    return SegmentResponse(
        pace_ajustado         = out.pace_ajustado_s_km,
        pace_ajustado_str     = out.pace_ajustado_str,
        factor_combinado      = out.factor_combinado,
        factor_fc             = out.factor_fc,
        factor_pendiente      = out.factor_pendiente,
        factor_superficie     = out.factor_superficie,
        factor_clima          = out.factor_clima,
        calorias_km           = out.calorias_km,
        carbs_hora            = out.carbs_hora,
        hidratacion_hora      = out.hidratacion_hora,
        zona_fc               = out.zona_fc,
        hrr_pct               = out.hrr_pct,
        velocidad_ajustada_ms = out.velocidad_ajustada_ms,
        weather_used          = weather_dict,
        elevation_m           = elevation_m,
    )


@app.post("/session", response_model=SessionResponse, status_code=201, tags=["Sessions"])
async def post_session(req: SessionCreateRequest):
    """Guarda una sesión de entrenamiento con sus segmentos."""
    result = await create_session(req.name, req.segments, req.summary)
    return SessionResponse(**result)


@app.get("/session/{session_id}", response_model=SessionResponse, tags=["Sessions"])
async def get_session_endpoint(session_id: str):
    """Recupera una sesión por ID."""
    session = await get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    return SessionResponse(**session)


@app.get("/sessions", response_model=List[SessionResponse], tags=["Sessions"])
async def get_sessions():
    """Lista las últimas 20 sesiones."""
    sessions = await list_sessions()
    return [SessionResponse(**s) for s in sessions]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _fetch_geo(lat: float, lng: float):
    """Concurrent fetch of weather and elevation."""
    import asyncio
    weather, elevation = await asyncio.gather(
        fetch_weather(lat, lng),
        fetch_elevation(lat, lng),
    )
    return weather, elevation


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
