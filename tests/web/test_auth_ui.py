import re
import uuid

import httpx
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.web
PW = "correct-horse-1"
SESSION_ENDED = "Your session ended — please log in again."


def _email():
    return f"u{uuid.uuid4().hex[:10]}@example.com"


def _signup_and_confirm(page, live, email, household="Maple House"):
    page.goto("/app/#/signup")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(PW)
    page.get_by_label("Household name").fill(household)
    page.get_by_role("button", name="Create account").click()
    expect(page.get_by_text("Check your email")).to_be_visible()
    link = live.outbox[-1].body.split("\n")[1]
    assert "/app/#/confirm?token=" in link
    page.goto("/app/#/confirm?token=" + link.split("token=")[1])
    expect(page.get_by_text("Email confirmed")).to_be_visible()
    assert "token=" not in page.url


def _login(page, email, password=PW):
    page.goto("/app/#/login")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Log in").click()


def test_signup_confirm_login_lands_on_overview(page, live):
    email = _email()
    _signup_and_confirm(page, live, email)
    _login(page, email)
    expect(page).to_have_url(re.compile(r"#/overview$"))
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    expect(page.get_by_text("Maple House").first).to_be_visible()
    expect(page.get_by_role("button", name="Log out")).to_be_visible()
    expect(page.get_by_text("Not a medical device").first).to_be_visible()
    # sessionStorage only: nothing in localStorage or cookies
    assert page.evaluate("localStorage.length") == 0
    assert page.context.cookies() == []


def test_signup_timezone_defaults_to_browser(page, live):
    page.goto("/app/#/signup")
    browser_tz = page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
    expect(page.get_by_label("Timezone")).to_have_value(browser_tz)


def test_wrong_password_shows_server_message(page, live):
    email = _email()
    _signup_and_confirm(page, live, email)
    _login(page, email, "wrong-password-1")
    expect(page.get_by_role("alert")).to_have_text("invalid credentials")
    expect(page).to_have_url(re.compile(r"#/login"))


def test_session_survives_reload_but_not_new_context(page, new_page, live):
    email = _email()
    _signup_and_confirm(page, live, email)
    _login(page, email)
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    page.reload()
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    other = new_page()
    other.goto("/app/#/overview")
    expect(other).to_have_url(re.compile(r"#/login"))
    expect(other.get_by_role("button", name="Log in")).to_be_visible()


def test_401_returns_to_login_with_message(page, live):
    email = _email()
    _signup_and_confirm(page, live, email)
    _login(page, email)
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    page.evaluate("sessionStorage.setItem('ha.token', 'not-a-valid-token')")
    page.reload()
    expect(page).to_have_url(re.compile(r"#/login"))
    expect(page.get_by_role("status")).to_have_text(SESSION_ENDED)
    assert page.evaluate("sessionStorage.getItem('ha.token')") is None


def test_log_out_clears_session(page, live):
    email = _email()
    _signup_and_confirm(page, live, email)
    _login(page, email)
    page.get_by_role("button", name="Log out").click()
    expect(page).to_have_url(re.compile(r"#/login"))
    page.goto("/app/#/overview")
    expect(page).to_have_url(re.compile(r"#/login"))


def test_reset_password_flow(page, live):
    email = _email()
    _signup_and_confirm(page, live, email)
    page.goto("/app/#/reset")
    page.get_by_label("Email").fill(email)
    page.get_by_role("button", name="Send reset link").click()
    expect(page.get_by_role("status")).to_contain_text("reset email")
    link = live.outbox[-1].body.split("\n")[1]
    assert "/app/#/reset?token=" in link
    page.goto("/app/#/reset?token=" + link.split("token=")[1])
    expect(page.get_by_label("New password", exact=True)).to_be_visible()
    assert "token=" not in page.url
    page.get_by_label("New password", exact=True).fill("another-horse-2")
    page.get_by_role("button", name="Set password").click()
    expect(page.get_by_role("status")).to_contain_text("Password changed")
    _login(page, email, "another-horse-2")
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()


def test_household_name_is_text_not_html(page, live):
    email = _email()
    _signup_and_confirm(page, live, email, household="<img src=x onerror=alert(1)>")
    _login(page, email)
    expect(page.get_by_text("<img src=x onerror=alert(1)>").first).to_be_visible()
    assert page.locator("img[src=x]").count() == 0


def test_reload_after_confirm_shows_no_error(page, live):
    _signup_and_confirm(page, live, _email())
    page.reload()
    expect(page.get_by_role("heading", name="Confirm your email")).to_be_visible()
    assert page.get_by_role("alert").count() == 0


def test_a_stale_requests_401_does_not_end_the_new_session(page, live):
    email = _email()
    _signup_and_confirm(page, live, email)
    _login(page, email)
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    status = page.evaluate("""async () => {
      const api = await import('/app/js/api.js');
      const good = sessionStorage.getItem('ha.token');
      sessionStorage.setItem('ha.token', 'an-old-session');
      const call = api.request('GET', '/auth/me').then(() => 200, (e) => e.status);
      sessionStorage.setItem('ha.token', good);  // a new login before the answer arrives
      return call;
    }""")
    assert status == 401
    expect(page).to_have_url(re.compile(r"#/overview$"))
    assert page.evaluate("sessionStorage.getItem('ha.token')") not in (None, "an-old-session")


def test_session_ended_message_survives_parallel_401s(page, live):
    email = _email()
    _signup_and_confirm(page, live, email)
    _login(page, email)
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    page.evaluate("""async () => {
      const api = await import('/app/js/api.js');
      sessionStorage.setItem('ha.token', 'expired');
      await Promise.allSettled([1, 2, 3].map(() => api.request('GET', '/auth/me')));
    }""")
    expect(page).to_have_url(re.compile(r"#/login$"))
    expect(page.get_by_role("status")).to_have_text(SESSION_ENDED)
    # worst case: two 401s handled in one task (a direct render, then the hashchange one)
    page.goto("/app/#/overview")
    page.evaluate("t => sessionStorage.setItem('ha.token', t)", _token(page, live, email))
    page.goto("/app/#/overview")
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    page.evaluate("""async () => {
      const auth = await import('/app/js/auth.js');
      const api = await import('/app/js/api.js');
      auth.logout(api.SESSION_ENDED);
      auth.logout(api.SESSION_ENDED);
    }""")
    expect(page).to_have_url(re.compile(r"#/login$"))
    expect(page.get_by_role("status")).to_have_text(SESSION_ENDED)


def _token(page, live, email):
    r = httpx.post(f"{live.base_url}/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.mark.parametrize("hash_", ["#/login", "#/signup"])
def test_logged_in_visit_to_login_or_signup_goes_to_overview(page, live, hash_):
    email = _email()
    _signup_and_confirm(page, live, email)
    _login(page, email)
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    page.goto(f"/app/{hash_}")
    expect(page).to_have_url(re.compile(r"#/overview$"))
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    page.goto("/app/#/reset")  # reset (like join and confirm) stays reachable
    expect(page.get_by_role("button", name="Send reset link")).to_be_visible()
