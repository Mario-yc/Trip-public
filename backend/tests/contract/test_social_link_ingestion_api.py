import json

from fastapi.testclient import TestClient

from backend.tests.unit.test_public_source_reader import PUBLIC_IP, make_reader, response
from backend.tests.unit.test_xiaohongshu_public_reader import DESC, TOKEN, URL, page
from src.core.database import get_db
from src.main import app
from src.services.social_link_ingestion_service import SocialLinkIngestionService
from src.services.xiaohongshu_public_reader import XiaohongshuPublicReader


def install_reader(monkeypatch, handler, resolver=None):
    base, calls, _ = make_reader(handler)

    def service(db):
        return SocialLinkIngestionService(
            db,
            reader=XiaohongshuPublicReader(
                resolver=resolver or (lambda host, port: {PUBLIC_IP}),
                transport_factory=base.transport_factory,
            ),
        )

    monkeypatch.setattr("src.api.routes.source_materials.SocialLinkIngestionService", service)
    return calls


def test_social_note_route_persists_body_and_unread_images_without_share_token(monkeypatch):
    calls = install_reader(monkeypatch, lambda request: response(page()))
    with TestClient(app) as client:
        result = client.post(
            "/api/source-materials/social-link",
            json={"url": URL + f"?xsec_token={TOKEN}&xsec_source=pc_share&appuid=tracking"},
        )
        assert result.status_code == 200
        body = result.json()
        assert body["fetchStatus"] == "succeeded"
        assert body["canonicalUrl"] == URL
        assert DESC in body["extractedText"]
        assert TOKEN not in result.text
        database = get_db()
        db = next(database)
        try:
            row = dict(
                db.execute("SELECT * FROM source_materials WHERE id = ?", (body["sourceMaterialId"],)).fetchone()
            )
            metadata = json.loads(row["metadata_json"])
            assert row["link_url"] == URL
            assert row["original_path"] is None and row["thumbnail_path"] is None
            assert row["original_retention"] == "structured_only" and row["cache_status"] == "not_applicable"
            assert metadata["parserVersion"] == XiaohongshuPublicReader.PARSER_VERSION
            assert metadata["images"][0]["readStatus"] == "not_read"
            assert TOKEN not in json.dumps(row)
            for table in ("agent_plan_proposals", "itinerary_versions", "itinerary_patches", "route_options"):
                assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        finally:
            database.close()
    assert len(calls) == 1


def test_meta_only_social_route_does_not_persist_summary_as_body(monkeypatch):
    install_reader(
        monkeypatch, lambda request: response('<title>攻略</title><meta name="description" content="' + DESC + '">')
    )
    with TestClient(app) as client:
        result = client.post("/api/source-materials/social-link", json={"url": URL})
    assert result.status_code == 200
    assert result.json()["fetchStatus"] == "needs_user_material"
    assert result.json()["failureReason"] == "xhs_state_missing"
    assert result.json()["extractedText"] is None
    database = get_db()
    db = next(database)
    try:
        assert (
            db.execute(
                "SELECT raw_text FROM source_materials WHERE id = ?", (result.json()["sourceMaterialId"],)
            ).fetchone()[0]
            is None
        )
    finally:
        database.close()


def test_cn_route_private_redirect_is_rejected_before_second_http_and_database_write(monkeypatch):
    calls = install_reader(
        monkeypatch,
        lambda request: response(status=302, headers={"location": URL}),
        resolver=lambda host, port: {PUBLIC_IP if host == "xhslink.cn" else "127.0.0.1"},
    )
    with TestClient(app) as client:
        result = client.post("/api/source-materials/social-link", json={"url": "https://xhslink.cn/o/abc123"})
    assert result.status_code == 422
    assert "social_link_private_target" in result.text
    assert len(calls) == 1
    database = get_db()
    db = next(database)
    try:
        assert db.execute("SELECT COUNT(*) FROM source_materials").fetchone()[0] == 0
    finally:
        database.close()


def test_reflected_share_token_is_not_returned_or_persisted_as_note_content(monkeypatch):
    install_reader(monkeypatch, lambda request: response(page(desc=DESC + TOKEN)))
    with TestClient(app) as client:
        result = client.post("/api/source-materials/social-link", json={"url": URL + f"?xsec_token={TOKEN}"})
    assert result.status_code == 200
    assert result.json()["fetchStatus"] == "needs_user_material"
    assert result.json()["failureReason"] == "xhs_sensitive_content"
    assert result.json()["extractedText"] is None
    assert TOKEN not in result.text
    database = get_db()
    db = next(database)
    try:
        row = dict(
            db.execute("SELECT * FROM source_materials WHERE id = ?", (result.json()["sourceMaterialId"],)).fetchone()
        )
        metadata = json.loads(row["metadata_json"])
        assert row["raw_text"] is None
        assert metadata["contentFingerprint"] is None and metadata["title"] is None
        assert metadata["images"] == []
        assert TOKEN not in json.dumps(row)
    finally:
        database.close()


def test_repeated_note_body_reuses_identity_but_changed_body_is_a_new_material(monkeypatch):
    text = [DESC]
    install_reader(monkeypatch, lambda request: response(page(desc=text[0])))
    with TestClient(app) as client:
        first = client.post("/api/source-materials/social-link", json={"url": URL}).json()
        repeat = client.post("/api/source-materials/social-link", json={"url": URL + f"?xsec_token={TOKEN}"}).json()
        assert first["sourceMaterialId"] == repeat["sourceMaterialId"]
        text[0] += "\n此处增加了一段已经公开更新的注意事项。"
        changed = client.post("/api/source-materials/social-link", json={"url": URL}).json()
        assert changed["sourceMaterialId"] != first["sourceMaterialId"]
    database = get_db()
    db = next(database)
    try:
        assert db.execute("SELECT COUNT(*) FROM source_materials").fetchone()[0] == 2
    finally:
        database.close()
