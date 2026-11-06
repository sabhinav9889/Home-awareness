import pytest

# (method, path template, json body). Templates may use {a_member_id}; later tasks add
# placeholders to CONTEXT_KEYS' builder below. Every household-scoped route MUST be listed
# here in the same commit that adds it.
HOUSEHOLD_SCOPED_ROUTES: list[tuple[str, str, dict | None]] = [
    ("DELETE", "/households/me/members/{a_member_id}", None),
]


@pytest.mark.parametrize(("method", "template", "body"), HOUSEHOLD_SCOPED_ROUTES)
async def test_other_household_resources_are_404(client, two_households, method, template, body):
    headers_a, headers_b = two_households
    a_me = (await client.get("/auth/me", headers=headers_a)).json()
    b_me = (await client.get("/auth/me", headers=headers_b)).json()
    ctx = {"a_member_id": a_me["id"]}

    path = template.format(**ctx)
    r = await client.request(method, path, headers=headers_b, json=body)
    assert r.status_code == 404, r.text

    # A's resource is untouched and B's ids never appear in A's listing.
    listing = (await client.get("/households/me", headers=headers_a)).json()
    ids = {m["id"] for m in listing["members"]}
    assert a_me["id"] in ids
    assert b_me["id"] not in ids
    assert (await client.get("/auth/me", headers=headers_a)).status_code == 200
