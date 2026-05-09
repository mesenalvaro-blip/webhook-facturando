"""
Quick smoke test -- runs without a live server (uses TestClient).
Execute: python test_api.py  (from effort-sparring/)
"""

import sys, os, asyncio
sys.path.insert(0, os.path.dirname(__file__))

from fastapi.testclient import TestClient
from api.main import app
from api.database import init_db

# Initialize DB synchronously before tests
asyncio.run(init_db())

client = TestClient(app)


def ok(msg):
    print(f"[OK] {msg}")


def test_health():
    r = client.get("/health")
    assert r.status_code == 200, r.text
    ok(f"GET /health -> {r.json()}")


def test_segment_basic():
    payload = {
        "velocidad":     3.5,
        "inclinacion":   3.0,
        "peso":          72.0,
        "pace_objetivo": 360,
        "fc_actual":     155,
        "fc_max":        190,
        "fc_reposo":     48,
        "superficie":    "road",
    }
    r = client.post("/segment", json=payload)
    assert r.status_code == 200, r.text
    d = r.json()
    ok("POST /segment (road, 3% uphill, fc=155)")
    print(f"   Pace objetivo:    6:00 /km  (360 s/km)")
    print(f"   Pace ajustado:    {d['pace_ajustado_str']}  ({d['pace_ajustado']:.1f} s/km)")
    print(f"   Factor combinado: {d['factor_combinado']}")
    print(f"   Factor FC:        {d['factor_fc']}  (HRR {d['hrr_pct']}%  -> {d['zona_fc']})")
    print(f"   Factor pendiente: {d['factor_pendiente']}  (3% grade)")
    print(f"   Factor clima:     {d['factor_clima']}  (no GPS, neutral)")
    print(f"   Calorias/km:      {d['calorias_km']} kcal")
    print(f"   Carbs/hora:       {d['carbs_hora']} g")
    print(f"   Hidratacion/h:    {d['hidratacion_hora']} ml")
    assert d["pace_ajustado"] > 360, "Pace deberia ser mas lento por pendiente + FC alta"
    assert d["calorias_km"] > 0
    assert d["carbs_hora"] > 0
    assert d["hidratacion_hora"] > 0


def test_segment_trail():
    payload = {
        "velocidad":     3.0,
        "inclinacion":   0.0,
        "peso":          65.0,
        "pace_objetivo": 420,
        "fc_actual":     130,
        "fc_max":        185,
        "fc_reposo":     52,
        "superficie":    "trail",
    }
    r = client.post("/segment", json=payload)
    assert r.status_code == 200, r.text
    d = r.json()
    ok(f"POST /segment (trail) -> factor_superficie: {d['factor_superficie']}")
    assert d["factor_superficie"] == 1.08


def test_session_crud():
    # Create
    r = client.post("/session", json={
        "name": "Entrenamiento de prueba",
        "segments": [{"pace": 360, "distancia_km": 2.0}],
        "summary": {"distancia_total_km": 2.0, "tiempo_min": 12},
    })
    assert r.status_code == 201, r.text
    session_id = r.json()["id"]
    ok(f"POST /session -> id: {session_id}")

    # Retrieve
    r2 = client.get(f"/session/{session_id}")
    assert r2.status_code == 200, r2.text
    d = r2.json()
    assert d["name"] == "Entrenamiento de prueba"
    assert len(d["segments"]) == 1
    ok(f"GET /session/{{id}} -> name: {d['name']}")

    # 404
    r3 = client.get("/session/nonexistent-id")
    assert r3.status_code == 404
    ok("GET /session/{bad-id} -> 404 correcto")


def test_invalid_superficie():
    payload = {
        "velocidad": 3.5, "inclinacion": 0, "peso": 70,
        "pace_objetivo": 360, "fc_actual": 140, "fc_max": 190,
        "fc_reposo": 50, "superficie": "volcanic_lava",
    }
    r = client.post("/segment", json=payload)
    assert r.status_code == 422
    ok("POST /segment (superficie invalida) -> 422 correcto")


if __name__ == "__main__":
    print("\n=== Effort Sparring - Smoke Tests ===\n")
    test_health()
    test_segment_basic()
    test_segment_trail()
    test_session_crud()
    test_invalid_superficie()
    print("\n[PASS] Todos los tests pasaron.\n")
