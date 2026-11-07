"""HTTP-level checks of the dashboard mount (no browser needed)."""

CSP = (
    "default-src 'self'; style-src 'self' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; connect-src 'self' ws: wss:; img-src 'self' data:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


async def test_root_redirects_to_app(client):
    r = await client.get("/")
    assert r.status_code == 307
    assert r.headers["location"] == "/app/"


async def test_app_without_slash_redirects(client):
    r = await client.get("/app")
    assert r.status_code in (307, 308)
    assert r.headers["location"].endswith("/app/")


async def test_app_serves_index_with_security_headers(client):
    r = await client.get("/app/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert r.headers["content-security-policy"] == CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    assert 'src="js/app.js"' in r.text


async def test_js_module_served_with_headers(client):
    r = await client.get("/app/js/api.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_mount_does_not_shadow_api(client):
    assert (await client.get("/auth/me")).status_code == 401
    assert (await client.get("/metrics")).status_code == 200


async def test_reset_link_points_at_dashboard(client, outbox, signed_up):
    await signed_up(email="r@example.com")
    r = await client.post("/auth/password-reset/request", json={"email": "r@example.com"})
    assert r.status_code == 202
    assert "/app/#/reset?token=" in outbox[-1].body
