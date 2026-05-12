"""
Effort Sparring — Pace Calculation Engine
==========================================
Models used:
  - Karvonen (HRR) for heart-rate zones
  - Minetti (2002) grade-cost coefficients for slope penalty
  - Pandolf (1977) terrain coefficients for surface penalty
  - Broeckner caloric expenditure via VO2 estimation
  - Jeukendrup macro oxidation by zone (carbs, fat, protein)
  - Sawka sweat-rate model for hydration
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import math


# ---------------------------------------------------------------------------
# Heart-rate zones (Karvonen / % HRR)
# ---------------------------------------------------------------------------

ZONE_THRESHOLDS = [
    (0.50, "Zona 1 - Recuperacion"),
    (0.60, "Zona 2 - Base aerobica"),
    (0.70, "Zona 3 - Tempo"),
    (0.80, "Zona 4 - Umbral"),
    (0.90, "Zona 5 - VO2max"),
    (1.01, "Zona 6 - Anaerobico"),
]

# Macro fractions by zone (must sum to 1.0 per zone)
# Based on Jeukendrup fat/carb crossover + ~5% protein across all zones
# Carbs: 4 kcal/g  |  Fat: 9 kcal/g  |  Protein: 4 kcal/g
MACRO_FRACTIONS_BY_ZONE = {
    #                          carbs   fat    protein
    "Zona 1 - Recuperacion":  (0.30,  0.65,  0.05),
    "Zona 2 - Base aerobica": (0.45,  0.50,  0.05),
    "Zona 3 - Tempo":         (0.60,  0.35,  0.05),
    "Zona 4 - Umbral":        (0.75,  0.20,  0.05),
    "Zona 5 - VO2max":        (0.87,  0.08,  0.05),
    "Zona 6 - Anaerobico":    (0.92,  0.03,  0.05),
}

# Legacy carb fraction kept for internal use
CARB_FRACTION_BY_ZONE = {z: v[0] for z, v in MACRO_FRACTIONS_BY_ZONE.items()}

# Surface multipliers (Pandolf terrain coefficients normalised to flat road)
SURFACE_FACTORS = {
    "road":       1.00,
    "track":      1.00,
    "trail":      1.08,
    "sand":       1.22,
    "snow":       1.18,
    "grass":      1.06,
    "treadmill":  0.98,
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class WeatherData:
    temperature_c: float = 20.0
    humidity_pct: float  = 60.0
    wind_speed_ms: float = 0.0
    wind_dir_deg: float  = 0.0
    precipitation_mm: float = 0.0
    apparent_temp_c: float = 20.0


@dataclass
class SegmentInput:
    # Pace
    velocidad_ms: float          # m/s — current or target speed
    pace_objetivo_s_km: float    # s/km — planned pace
    # Physiological
    peso_kg: float
    fc_actual: float
    fc_max: float
    fc_reposo: float
    # Terrain
    inclinacion_pct: float       # % grade (positive = uphill)
    superficie: str = "road"     # see SURFACE_FACTORS
    # Location (optional, used for weather/elevation fetch)
    lat: Optional[float] = None
    lng: Optional[float] = None
    # Pre-fetched weather (filled by API layer if GPS provided)
    weather: WeatherData = field(default_factory=WeatherData)
    # Pre-fetched elevation-derived grade (overrides inclinacion_pct if set)
    elevation_grade_pct: Optional[float] = None


@dataclass
class SegmentOutput:
    pace_ajustado_s_km: float    # adjusted pace in s/km
    pace_ajustado_str: str       # "MM:SS /km"
    factor_combinado: float      # multiplicative load factor (1.0 = neutral)
    factor_fc: float
    factor_pendiente: float
    factor_superficie: float
    factor_clima: float
    calorias_km: float           # kcal per km
    calorias_hora: float         # kcal per hour at adjusted pace
    carbs_hora: float            # g carbs oxidised/hour (from glycogen + intake)
    grasas_hora: float           # g fat/hour
    proteinas_hora: float        # g protein/hour
    ingesta_carbs_hora: float    # g carbs recommended to EAT/hour (gut-limited)
    hidratacion_hora: float      # ml fluid/hour
    zona_fc: str
    hrr_pct: float               # % heart-rate reserve
    velocidad_ajustada_ms: float


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------

def hrr_percent(fc_actual: float, fc_max: float, fc_reposo: float) -> float:
    """Heart-Rate Reserve % (Karvonen)."""
    hrr = fc_max - fc_reposo
    if hrr <= 0:
        return 0.0
    return max(0.0, min(1.0, (fc_actual - fc_reposo) / hrr))


def hr_zone(hrr_pct: float) -> str:
    for threshold, name in ZONE_THRESHOLDS:
        if hrr_pct <= threshold:
            return name
    return ZONE_THRESHOLDS[-1][1]


def grade_factor_minetti(grade_pct: float) -> float:
    """
    Minetti (2002) metabolic cost on slope, normalised to flat.
    Returns a multiplier on energy cost (>1 = costs more).
    Valid range: -45% to +45%.
    """
    g = max(-0.45, min(0.45, grade_pct / 100.0))
    # Minetti polynomial for Cmet (J/kg/m)
    # Cmet(i) = 155.4i^5 - 30.4i^4 - 43.3i^3 + 46.3i^2 + 19.5i + 3.6
    cost_grade = (
        155.4 * g**5
        - 30.4 * g**4
        - 43.3 * g**3
        + 46.3 * g**2
        + 19.5 * g
        + 3.6
    )
    cost_flat = 3.6  # J/kg/m at g=0
    return max(0.5, cost_grade / cost_flat)


def surface_factor(superficie: str) -> float:
    return SURFACE_FACTORS.get(superficie.lower(), 1.0)


def weather_factor(w: WeatherData) -> float:
    """
    Combined weather penalty:
      - Heat stress: apparent temp above 15°C adds cost
      - Wind: headwind adds ~1% per 3 m/s above 3 m/s
      - Rain: minor friction/cooling factor
    Returns multiplier (1.0 = neutral conditions ~15°C, no wind).
    """
    # Temperature: thermoregulation cost
    temp_pen = 0.0
    t = w.apparent_temp_c
    if t > 15:
        temp_pen = 0.004 * (t - 15)      # +0.4% per °C above 15
    elif t < 5:
        temp_pen = 0.002 * (5 - t)       # +0.2% per °C below 5

    # Humidity amplifies heat penalty (dew-point approximation)
    if t > 20 and w.humidity_pct > 60:
        temp_pen += 0.001 * (w.humidity_pct - 60) * (t - 20) / 10

    # Wind (simplified — assumes headwind worst case)
    wind_pen = max(0.0, 0.003 * (w.wind_speed_ms - 3))

    # Rain / wet surface
    rain_pen = 0.01 if w.precipitation_mm > 0.2 else 0.0

    return 1.0 + temp_pen + wind_pen + rain_pen


def hr_effort_factor(hrr_pct: float) -> float:
    """
    Converts cardiac drift from target pace into a pace-adjustment factor.
    If HRR% > 0.75 (hard), slow down. If < 0.55 (easy), speed up.
    Neutral band: 0.55–0.75 → factor 1.0.
    """
    if hrr_pct <= 0.55:
        return 0.94    # room to go faster (up to 6% faster)
    if hrr_pct <= 0.75:
        return 1.0     # in target band
    # Above threshold: exponential penalty
    excess = hrr_pct - 0.75
    return 1.0 + 2.0 * excess   # +2% pace cost per 1% HRR over 0.75


def estimate_vo2(velocity_ms: float, grade_pct: float = 0.0) -> float:
    """
    VO2 in ml/kg/min using ACSM running equation.
    VO2 = 0.2 * speed_m_min + 0.9 * speed_m_min * (%grade/100) + 3.5
    """
    v = velocity_ms * 60  # m/min
    g = max(0.0, grade_pct / 100.0)   # only positive grade in ACSM
    return 0.2 * v + 0.9 * v * g + 3.5


def calories_per_km(
    peso_kg: float,
    velocity_ms: float,
    grade_pct: float,
    factor_terreno: float,
) -> float:
    """
    kcal per km. Uses ACSM VO2 equation → caloric equivalent (1 L O2 ≈ 5 kcal).
    Only terrain factors (slope × surface × climate) are applied — NOT the HR
    factor, because HR reflects cardiovascular load, not mechanical energy cost
    per km at a given speed.
    Velocity used is the actual running speed (not the adjusted/recommended pace).
    """
    vo2 = estimate_vo2(velocity_ms, grade_pct)          # ml/kg/min
    time_min_per_km = 1000 / (velocity_ms * 60)         # min/km
    kcal_per_km = (vo2 * peso_kg * time_min_per_km) / 200
    return round(kcal_per_km * factor_terreno, 1)


def macros_per_hour(zona_fc: str, kcal_per_km: float, pace_s_km: float) -> tuple:
    """
    Returns (carbs_g, fat_g, protein_g, kcal_total) per hour.
    Carbs: 4 kcal/g | Fat: 9 kcal/g | Protein: 4 kcal/g
    """
    fracs = MACRO_FRACTIONS_BY_ZONE.get(zona_fc, (0.60, 0.35, 0.05))
    km_per_hour = 3600 / pace_s_km
    kcal_hour = kcal_per_km * km_per_hour
    carbs_g   = round(kcal_hour * fracs[0] / 4.0, 1)
    fat_g     = round(kcal_hour * fracs[1] / 9.0, 1)
    protein_g = round(kcal_hour * fracs[2] / 4.0, 1)
    return carbs_g, fat_g, protein_g, round(kcal_hour, 1)


def recommended_carb_intake(carbs_oxidized: float, hrr_pct: float) -> float:
    """
    Carbs recommended to actually EAT per hour during exercise.
    Capped by gut absorption limits (Burke & Jeukendrup):
      - Single transporter (glucose only): ~60 g/h
      - Dual transporter (glucose + fructose 2:1): ~90 g/h
    Below 50% HRR glycogen stores are sufficient — no intake needed.
    Returns 0–90 g/h.
    """
    if hrr_pct < 0.50:
        return 0.0
    elif hrr_pct < 0.60:
        return round(min(carbs_oxidized, 30.0), 1)   # Zone 2: up to 30 g/h
    elif hrr_pct < 0.75:
        return round(min(carbs_oxidized, 60.0), 1)   # Zone 3: up to 60 g/h
    else:
        return round(min(carbs_oxidized, 90.0), 1)   # Zone 4+: up to 90 g/h


def hydration_per_hour(
    peso_kg: float,
    temp_c: float,
    hrr_pct: float,
) -> float:
    """
    ml fluid/hour — Sawka model simplified.
    Calibrated downward 20% to match Garmin sweat-loss estimates
    (Garmin session 11-May-2026: 333 ml / 20 min active = ~1000 ml/h).
    """
    base = 300
    weight_factor = 3.0 * peso_kg        # heavier → more sweat
    heat_factor = max(0, (temp_c - 15) * 12)
    intensity_factor = hrr_pct * 150
    total = base + weight_factor + heat_factor + intensity_factor
    return round(min(total, 1500), 0)    # cap at 1.5 L/h


def seconds_to_pace(total_seconds: float) -> str:
    mins = int(total_seconds) // 60
    secs = int(total_seconds) % 60
    return f"{mins}:{secs:02d} /km"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calculate_segment(inp: SegmentInput) -> SegmentOutput:
    """Main calculation. Returns a fully populated SegmentOutput."""

    # Use elevation-derived grade if provided
    grade = inp.elevation_grade_pct if inp.elevation_grade_pct is not None else inp.inclinacion_pct

    # Individual factors
    f_fc       = hr_effort_factor(hrr_percent(inp.fc_actual, inp.fc_max, inp.fc_reposo))
    f_pendiente = grade_factor_minetti(grade)
    f_superficie = surface_factor(inp.superficie)
    f_clima    = weather_factor(inp.weather)

    factor_combinado = f_fc * f_pendiente * f_superficie * f_clima

    # Terrain-only factor for caloric cost:
    # HR affects pace but NOT energy cost per km at a given speed
    factor_terreno = f_pendiente * f_superficie * f_clima

    # Adjusted pace (s/km) — higher factor = slower pace
    pace_ajustado = inp.pace_objetivo_s_km * factor_combinado
    pace_ajustado = max(120, min(pace_ajustado, 900))    # 2:00–15:00 /km

    # Adjusted velocity
    v_ajustada = 1000 / pace_ajustado  # m/s

    # Physiological outputs
    hrr = hrr_percent(inp.fc_actual, inp.fc_max, inp.fc_reposo)
    zona = hr_zone(hrr)

    # Calories: use actual running speed + terrain-only factor
    actual_pace_s_km = 1000 / inp.velocidad_ms
    kcal = calories_per_km(inp.peso_kg, inp.velocidad_ms, grade, factor_terreno)
    carbs, fat, protein, kcal_hora = macros_per_hour(zona, kcal, actual_pace_s_km)
    ingesta_carbs = recommended_carb_intake(carbs, hrr)
    hidra = hydration_per_hour(inp.peso_kg, inp.weather.apparent_temp_c, hrr)

    return SegmentOutput(
        pace_ajustado_s_km     = round(pace_ajustado, 1),
        pace_ajustado_str      = seconds_to_pace(pace_ajustado),
        factor_combinado       = round(factor_combinado, 4),
        factor_fc              = round(f_fc, 4),
        factor_pendiente       = round(f_pendiente, 4),
        factor_superficie      = round(f_superficie, 4),
        factor_clima           = round(f_clima, 4),
        calorias_km            = kcal,
        calorias_hora          = kcal_hora,
        carbs_hora             = carbs,
        grasas_hora            = fat,
        proteinas_hora         = protein,
        ingesta_carbs_hora     = ingesta_carbs,
        hidratacion_hora       = hidra,
        zona_fc                = zona,
        hrr_pct                = round(hrr * 100, 1),
        velocidad_ajustada_ms  = round(v_ajustada, 3),
    )
