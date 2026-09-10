import sqlite3

from fastapi.testclient import TestClient

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.main import app


def clear_database() -> None:
    initialize_database()
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            DELETE FROM extraction_results;
            DELETE FROM source_materials;
            DELETE FROM inspiration_sets;
            """
        )


def test_inspiration_create_contract():
    clear_database()
    with TestClient(app) as client:
        response = client.post(
            "/api/inspirations",
            json={"cityHint": "北京", "textItems": ["北京 预算 3000"], "socialLinks": []},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["inspirationSetId"].startswith("insp_")
    assert body["status"] == "extracting"
    assert len(body["sourceMaterialIds"]) == 1


def test_create_inspiration_binds_source_materials():
    clear_database()
    with TestClient(app) as client:
        material_response = client.post(
            "/api/source-materials",
            json={"kind": "screenshot", "rawText": "故宫博物院 攻略截图", "thumbnailPath": "thumbs/a.txt"},
        )
        material_id = material_response.json()["sourceMaterialId"]

        response = client.post(
            "/api/inspirations",
            json={
                "cityHint": "北京",
                "textItems": ["预算 3000 元"],
                "socialLinks": ["https://example.com/guide"],
                "sourceMaterialIds": [material_id],
            },
        )

    assert response.status_code == 200
    inspiration_id = response.json()["inspirationSetId"]
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM source_materials WHERE inspiration_set_id = ?",
            (inspiration_id,),
        ).fetchone()[0]
        bound_id = connection.execute(
            "SELECT inspiration_set_id FROM source_materials WHERE id = ?",
            (material_id,),
        ).fetchone()[0]

    assert count == 3
    assert bound_id == inspiration_id


def test_create_inspiration_rejects_missing_source_material_without_orphan_inspiration():
    clear_database()
    with TestClient(app) as client:
        response = client.post(
            "/api/inspirations",
            json={"cityHint": "北京", "sourceMaterialIds": ["mat_missing"]},
        )

    assert response.status_code == 400
    assert "Source material not found" in response.json()["detail"]

    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        inspiration_count = connection.execute("SELECT COUNT(*) FROM inspiration_sets").fetchone()[0]
        material_count = connection.execute("SELECT COUNT(*) FROM source_materials").fetchone()[0]

    assert inspiration_count == 0
    assert material_count == 0


def test_upload_source_material_stores_thumbnail_and_cache_policy():
    clear_database()
    with TestClient(app) as client:
        response = client.post(
            "/api/source-materials/upload",
            data={"kind": "scenery_photo", "saveOriginal": "false"},
            files={"file": ("scene.jpg", b"fake image bytes", "image/jpeg")},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "scenery_photo"
    assert body["thumbnailUrl"].endswith(".placeholder.txt")
    assert body["thumbnailPlaceholder"] is True
    assert body["originalRetention"] == "temporary_cache"


def test_upload_source_material_rejects_unsupported_content_type():
    clear_database()
    with TestClient(app) as client:
        response = client.post(
            "/api/source-materials/upload",
            data={"kind": "screenshot", "saveOriginal": "false"},
            files={"file": ("notes.txt", b"not an image", "text/plain")},
        )

    assert response.status_code == 400
    assert "Unsupported file type" in response.json()["detail"]


def test_upload_source_material_rejects_empty_file():
    clear_database()
    with TestClient(app) as client:
        response = client.post(
            "/api/source-materials/upload",
            data={"kind": "screenshot", "saveOriginal": "false"},
            files={"file": ("empty.png", b"", "image/png")},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Uploaded file is empty"


def test_upload_source_material_rejects_oversized_file():
    clear_database()
    with TestClient(app) as client:
        response = client.post(
            "/api/source-materials/upload",
            data={"kind": "screenshot", "saveOriginal": "false"},
            files={"file": ("large.png", b"0" * (8 * 1024 * 1024 + 1), "image/png")},
        )

    assert response.status_code == 400
    assert "8 MB size limit" in response.json()["detail"]


def test_extract_reads_saved_source_materials_without_request_body():
    clear_database()
    with TestClient(app) as client:
        material_response = client.post(
            "/api/source-materials",
            json={"kind": "screenshot", "rawText": "上海 外滩 拍照攻略", "thumbnailPath": "thumbs/b.txt"},
        )
        response = client.post(
            "/api/inspirations",
            json={"sourceMaterialIds": [material_response.json()["sourceMaterialId"]]},
        )
        inspiration_id = response.json()["inspirationSetId"]

        extract_response = client.post(f"/api/inspirations/{inspiration_id}/extract")

    assert extract_response.status_code == 200
    body = extract_response.json()
    assert body["cityCandidates"] == ["上海"]
    assert body["poiCandidates"][0]["name"] == "外滩"
    assert body["itineraryDraft"]["days"][0]["segments"]


def test_extract_aggregates_multiple_source_materials():
    clear_database()
    with TestClient(app) as client:
        first = client.post(
            "/api/source-materials",
            json={"kind": "screenshot", "rawText": "广州塔 打卡", "thumbnailPath": "thumbs/c.txt"},
        ).json()["sourceMaterialId"]
        second = client.post(
            "/api/source-materials",
            json={"kind": "image_set", "rawText": "预算 800 元 轻松不赶", "thumbnailPath": "thumbs/d.txt"},
        ).json()["sourceMaterialId"]
        response = client.post("/api/inspirations", json={"sourceMaterialIds": [first, second]})
        extract_response = client.post(f"/api/inspirations/{response.json()['inspirationSetId']}/extract")

    body = extract_response.json()
    assert body["cityCandidates"] == ["广州"]
    assert "轻松不赶路" in body["styleTags"]
    assert body["budgetClues"] == ["预算 800 元 轻松不赶"]


def test_default_provider_failure_falls_back_to_mock(monkeypatch):
    clear_database()
    monkeypatch.setenv("PROVIDER_MODE", "default")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/inspirations",
                json={"cityHint": "深圳", "textItems": ["深圳湾公园 拍照"], "socialLinks": []},
            )
            extract_response = client.post(f"/api/inspirations/{response.json()['inspirationSetId']}/extract")
    finally:
        get_settings.cache_clear()

    body = extract_response.json()
    assert body["fallbackUsed"] is True
    assert body["providerName"] == "mock-vision-provider"
    assert "默认视觉 provider 不可用" in body["userVisibleCaveat"]

    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT provider_name, fallback_used, provider_failure_reason, user_visible_caveat
            FROM extraction_results
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()

    assert row[0] == "mock-vision-provider"
    assert row[1] == 1
    assert row[2] == "Default vision provider is not configured."
    assert "默认视觉 provider 不可用" in row[3]
