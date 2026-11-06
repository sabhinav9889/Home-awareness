"""Tenant-isolation harness. Every household-scoped route is exercised here.

How a later task extends this file (append only, in the same commit as the feature):

1. Route: append `(method, "/path/{a_<name>_id}", body_or_None)` to HOUSEHOLD_SCOPED_ROUTES.
   `{a_<name>_id}` / `{b_<name>_id}` are ids of resources owned by household A / B.
2. Resource: write `async def create_<name>(client, headers, outbox, tag) -> dict[str, int]`
   that creates one resource in the household owning `headers` and returns
   `{"<name>": id}`; append it to RESOURCE_CREATORS. The harness creates one for A and one
   for B and exposes them as the placeholders `a_<name>_id` and `b_<name>_id`.
3. Listing: append `(path, extractor, "<name>")` to LISTING_ROUTES, where extractor maps the
   listing's JSON to the set of ids it contains. A's listing must contain `a_<name>_id` and
   never `b_<name>`.

Each scoped route is checked in this order: B requests A's resource -> must be Tenant's exact
404 body (a typo'd route returns FastAPI's different 404 body and fails); then A makes the
same request -> must succeed (positive control proving the route exists and B's attempt did
not touch the resource). Because A's request may be destructive, it runs last.
"""

from collections.abc import Awaitable, Callable

import pytest

PW = "correct-horse-1"
TENANT_404 = {"detail": "not found"}

HOUSEHOLD_SCOPED_ROUTES: list[tuple[str, str, dict | None]] = [
    ("DELETE", "/households/me/members/{a_member_id}", None),
]

Creator = Callable[..., Awaitable[dict[str, int]]]


async def create_member(client, headers, outbox, tag) -> dict[str, int]:
    """Invite a new (non-owner) member into the household owning `headers`."""
    r = await client.post("/households/me/invites", headers=headers)
    token = r.json()["invite_url"].split("invite=")[1]
    email = f"member-{tag}@example.com"
    r = await client.post(
        "/auth/signup", json={"email": email, "password": PW, "invite_token": token}
    )
    assert r.status_code == 201, r.text
    return {"member": r.json()["member_id"]}


RESOURCE_CREATORS: list[Creator] = [create_member]

# (path, extractor(json) -> set of ids, resource name)
LISTING_ROUTES: list[tuple[str, Callable[[dict], set[int]], str]] = [
    ("/households/me", lambda j: {m["id"] for m in j["members"]}, "member"),
]


@pytest.fixture
async def scoped_ids(client, two_households, outbox) -> dict[str, int]:
    headers_a, headers_b = two_households
    ids: dict[str, int] = {}
    for creator in RESOURCE_CREATORS:
        for side, headers in (("a", headers_a), ("b", headers_b)):
            made = await creator(client, headers, outbox, f"{side}-{creator.__name__}")
            ids.update({f"{side}_{name}_id": v for name, v in made.items()})
    return ids


@pytest.mark.parametrize(("method", "template", "body"), HOUSEHOLD_SCOPED_ROUTES)
async def test_other_household_resources_are_404(
    client, two_households, scoped_ids, method, template, body
):
    headers_a, headers_b = two_households
    path = template.format(**scoped_ids)

    r = await client.request(method, path, headers=headers_b, json=body)
    assert r.status_code == 404 and r.json() == TENANT_404, r.text

    # Positive control: the route exists and the resource was untouched by B's attempt.
    r = await client.request(method, path, headers=headers_a, json=body)
    assert r.status_code < 400, r.text


@pytest.mark.parametrize(("path", "extract", "name"), LISTING_ROUTES)
async def test_listings_never_leak_other_household_ids(
    client, two_households, scoped_ids, path, extract, name
):
    headers_a, headers_b = two_households
    a_ids = extract((await client.get(path, headers=headers_a)).json())
    b_ids = extract((await client.get(path, headers=headers_b)).json())
    assert scoped_ids[f"a_{name}_id"] in a_ids
    assert scoped_ids[f"b_{name}_id"] in b_ids
    assert scoped_ids[f"b_{name}_id"] not in a_ids
    assert scoped_ids[f"a_{name}_id"] not in b_ids
