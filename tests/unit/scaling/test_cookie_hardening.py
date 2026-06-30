"""
Tests for cookie hardening middleware.

Verifies that all Set-Cookie headers emitted by the application
have proper security flags (Secure, HttpOnly, SameSite).
"""

import pytest


# ---------------------------------------------------------------------------
# Unit under test
# ---------------------------------------------------------------------------

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', 'elitea_core'))

from unittest.mock import patch, MagicMock


# Mock pylon before importing module
sys.modules.setdefault('pylon', MagicMock())
sys.modules.setdefault('pylon.core', MagicMock())
sys.modules.setdefault('pylon.core.tools', MagicMock())
pylon_tools_mock = sys.modules['pylon.core.tools']
pylon_tools_mock.log = MagicMock()

from utils.cookie_hardening import (
    harden_set_cookie_header,
    register_cookie_hardening,
)


# ---------------------------------------------------------------------------
# Tests for harden_set_cookie_header
# ---------------------------------------------------------------------------

class TestHardenSetCookieHeader:
    """Tests for the header patching function."""

    def test_adds_httponly_when_missing(self):
        header = 'session_id=abc123; Path=/'
        result = harden_set_cookie_header(header)
        assert '; HttpOnly' in result

    def test_adds_secure_when_missing(self):
        header = 'session_id=abc123; Path=/'
        result = harden_set_cookie_header(header, secure=True)
        assert '; Secure' in result

    def test_adds_samesite_when_missing(self):
        header = 'session_id=abc123; Path=/'
        result = harden_set_cookie_header(header, samesite='Lax')
        assert '; SameSite=Lax' in result

    def test_does_not_duplicate_httponly(self):
        header = 'session_id=abc123; Path=/; HttpOnly'
        result = harden_set_cookie_header(header)
        assert result.count('HttpOnly') == 1

    def test_does_not_duplicate_secure(self):
        header = 'session_id=abc123; Path=/; Secure'
        result = harden_set_cookie_header(header, secure=True)
        assert result.count('Secure') == 1

    def test_does_not_duplicate_samesite(self):
        header = 'session_id=abc123; Path=/; SameSite=Strict'
        result = harden_set_cookie_header(header, samesite='Lax')
        # Should not add another SameSite
        assert result.count('SameSite') == 1
        # Preserves existing value
        assert 'SameSite=Strict' in result

    def test_case_insensitive_detection(self):
        header = 'session_id=abc123; path=/; httponly; secure; samesite=lax'
        result = harden_set_cookie_header(header, secure=True, samesite='Lax')
        # Should not add any flags since they're all present (case-insensitive)
        assert result == header

    def test_no_secure_when_disabled(self):
        header = 'session_id=abc123; Path=/'
        result = harden_set_cookie_header(header, secure=False)
        assert 'Secure' not in result

    def test_samesite_strict(self):
        header = 'csrf_token=xyz; Path=/'
        result = harden_set_cookie_header(header, samesite='Strict')
        assert '; SameSite=Strict' in result

    def test_samesite_none(self):
        header = 'third_party=xyz; Path=/'
        result = harden_set_cookie_header(header, samesite='None')
        assert '; SameSite=None' in result

    def test_handles_cookie_with_equals_in_value(self):
        header = 'data=base64==encoded; Path=/'
        result = harden_set_cookie_header(header)
        assert '; HttpOnly' in result
        assert '; Secure' in result
        assert 'data=base64==encoded' in result

    def test_handles_cookie_with_special_chars(self):
        header = 'pref=lang%3Den%26theme%3Ddark; Path=/; Max-Age=2592000'
        result = harden_set_cookie_header(header)
        assert '; HttpOnly' in result
        assert '; SameSite=Lax' in result
        assert 'Max-Age=2592000' in result

    def test_all_flags_added_to_bare_cookie(self):
        header = 'simple=value'
        result = harden_set_cookie_header(header, secure=True, samesite='Lax')
        assert '; HttpOnly' in result
        assert '; Secure' in result
        assert '; SameSite=Lax' in result

    def test_preserves_existing_attributes(self):
        header = 'id=abc; Path=/; Domain=.example.com; Max-Age=3600; Expires=Thu, 01 Jan 2099'
        result = harden_set_cookie_header(header)
        assert 'Path=/' in result
        assert 'Domain=.example.com' in result
        assert 'Max-Age=3600' in result
        assert '; HttpOnly' in result

    def test_semicolon_without_space_in_existing_flags(self):
        header = 'id=abc;HttpOnly;Secure'
        result = harden_set_cookie_header(header, secure=True, samesite='Lax')
        assert result.count('HttpOnly') == 1
        assert result.count('Secure') == 1
        assert '; SameSite=Lax' in result


# ---------------------------------------------------------------------------
# Tests for register_cookie_hardening
# ---------------------------------------------------------------------------

class TestRegisterCookieHardening:
    """Tests for the Flask middleware registration."""

    def _make_app(self):
        """Create a minimal Flask-like mock."""
        app = MagicMock()
        app.after_request = MagicMock(side_effect=lambda fn: fn)
        return app

    def _make_response(self, cookies):
        """Create a mock response with Set-Cookie headers."""
        response = MagicMock()

        _cookies = list(cookies)

        class FakeHeaders:
            def __contains__(self, name):
                if name == 'Set-Cookie':
                    return len(_cookies) > 0
                return False

            def getlist(self, name):
                if name == 'Set-Cookie':
                    return list(_cookies)
                return []

            def remove(self, name):
                if name == 'Set-Cookie':
                    _cookies.clear()

            def add(self, name, value):
                if name == 'Set-Cookie':
                    _cookies.append(value)

        response.headers = FakeHeaders()
        response._cookies = _cookies
        return response

    def test_registers_after_request_hook(self):
        app = self._make_app()
        register_cookie_hardening(app)
        app.after_request.assert_called_once()

    def test_hardens_single_cookie(self):
        app = self._make_app()
        register_cookie_hardening(app, secure=True, samesite='Lax')

        hook = app.after_request.call_args[0][0]
        response = self._make_response(['session=abc123; Path=/'])

        result = hook(response)
        assert len(result._cookies) == 1
        assert '; HttpOnly' in result._cookies[0]
        assert '; Secure' in result._cookies[0]
        assert '; SameSite=Lax' in result._cookies[0]

    def test_hardens_multiple_cookies(self):
        app = self._make_app()
        register_cookie_hardening(app, secure=True, samesite='Lax')

        hook = app.after_request.call_args[0][0]
        response = self._make_response([
            'session=abc; Path=/',
            'prefs=dark; Path=/; Max-Age=2592000',
        ])

        result = hook(response)
        assert len(result._cookies) == 2
        for cookie in result._cookies:
            assert '; HttpOnly' in cookie
            assert '; Secure' in cookie
            assert '; SameSite=Lax' in cookie

    def test_excludes_named_cookies(self):
        app = self._make_app()
        register_cookie_hardening(app, secure=True, excluded_names=['tracking'])

        hook = app.after_request.call_args[0][0]
        response = self._make_response([
            'session=abc; Path=/',
            'tracking=ga_id; Path=/',
        ])

        result = hook(response)
        assert len(result._cookies) == 2
        # session should be hardened
        assert '; HttpOnly' in result._cookies[0]
        # tracking should be left alone
        assert result._cookies[1] == 'tracking=ga_id; Path=/'

    def test_no_op_when_no_cookies(self):
        app = self._make_app()
        register_cookie_hardening(app, secure=True, samesite='Lax')

        hook = app.after_request.call_args[0][0]
        response = self._make_response([])

        result = hook(response)
        assert len(result._cookies) == 0

    def test_secure_false_for_local_dev(self):
        app = self._make_app()
        register_cookie_hardening(app, secure=False, samesite='Lax')

        hook = app.after_request.call_args[0][0]
        response = self._make_response(['session=abc; Path=/'])

        result = hook(response)
        assert '; Secure' not in result._cookies[0]
        assert '; HttpOnly' in result._cookies[0]

    def test_preserves_already_hardened_cookies(self):
        app = self._make_app()
        register_cookie_hardening(app, secure=True, samesite='Lax')

        hook = app.after_request.call_args[0][0]
        response = self._make_response([
            'session=abc; Path=/; HttpOnly; Secure; SameSite=Lax',
        ])

        result = hook(response)
        assert len(result._cookies) == 1
        cookie = result._cookies[0]
        assert cookie.count('HttpOnly') == 1
        assert cookie.count('Secure') == 1
        assert cookie.count('SameSite') == 1


# ---------------------------------------------------------------------------
# Tests for cookie audit (verify no non-auth cookies exist currently)
# ---------------------------------------------------------------------------

class TestCookieAudit:
    """
    Audit tests verifying the codebase doesn't set non-auth cookies.

    These tests verify that:
    1. The only application-level cookie is the Flask session (auth)
    2. No plugins call set_cookie directly
    3. The GA cookie in EliteaUI has proper flags
    """

    def test_auth_session_cookie_config_has_httponly(self):
        """Verify pylon_auth Flask config enforces HttpOnly."""
        import yaml
        config_path = os.path.join(
            os.path.dirname(__file__), '..', '..', '..',
            'pylon_auth', 'pylon.yml'
        )
        if not os.path.exists(config_path):
            pytest.skip("pylon_auth/pylon.yml not found at expected path")

        with open(config_path) as f:
            config = yaml.safe_load(f)

        app_config = config.get('application', {})
        assert app_config.get('SESSION_COOKIE_HTTPONLY') is True

    def test_auth_session_cookie_config_has_samesite(self):
        """Verify pylon_auth Flask config enforces SameSite."""
        import yaml
        config_path = os.path.join(
            os.path.dirname(__file__), '..', '..', '..',
            'pylon_auth', 'pylon.yml'
        )
        if not os.path.exists(config_path):
            pytest.skip("pylon_auth/pylon.yml not found at expected path")

        with open(config_path) as f:
            config = yaml.safe_load(f)

        app_config = config.get('application', {})
        assert app_config.get('SESSION_COOKIE_SAMESITE') == 'Lax'

    def test_auth_session_cookie_config_has_secure(self):
        """Verify pylon_auth Flask config references COOKIES_SECURE env var."""
        import yaml
        config_path = os.path.join(
            os.path.dirname(__file__), '..', '..', '..',
            'pylon_auth', 'pylon.yml'
        )
        if not os.path.exists(config_path):
            pytest.skip("pylon_auth/pylon.yml not found at expected path")

        with open(config_path) as f:
            raw = f.read()

        assert 'SESSION_COOKIE_SECURE' in raw
        assert 'COOKIES_SECURE' in raw

    def test_no_set_cookie_in_elitea_core(self):
        """Verify elitea_core source has no direct set_cookie calls."""
        import subprocess
        result = subprocess.run(
            ['grep', '-rn', 'set_cookie',
             os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', 'elitea_core')],
            capture_output=True, text=True
        )
        lines = [
            line for line in result.stdout.splitlines()
            if '__pycache__' not in line
            and 'test_' not in line
            and 'cookie_hardening.py' not in line
        ]
        assert len(lines) == 0, f"Unexpected set_cookie calls found:\n" + '\n'.join(lines)

    def test_no_set_cookie_in_pylon_main_plugins(self):
        """Verify pylon_main plugins don't directly set cookies."""
        import subprocess
        plugins_dir = os.path.join(
            os.path.dirname(__file__), '..', '..', '..',
            'pylon_main', 'plugins'
        )
        if not os.path.exists(plugins_dir):
            pytest.skip("pylon_main/plugins not found")

        result = subprocess.run(
            ['grep', '-rn', '--include=*.py', 'set_cookie', plugins_dir],
            capture_output=True, text=True
        )
        lines = [
            line for line in result.stdout.splitlines()
            if '__pycache__' not in line
            and 'test_' not in line
            and 'requirements/' not in line
            and 'cookie_hardening.py' not in line
        ]
        # The only expected match is in elitea_core plugin (runtime copy)
        # which should be the cookie_hardening itself or nothing
        unexpected = [l for l in lines if 'elitea_core' not in l]
        assert len(unexpected) == 0, (
            f"Unexpected set_cookie calls in pylon_main plugins:\n" + '\n'.join(unexpected)
        )

    def test_ga_cookie_has_secure_in_production(self):
        """Verify GA config sets secure flag in production mode."""
        ga_path = os.path.join(
            os.path.dirname(__file__), '..', '..', '..', '..',
            'EliteaUI', 'src', 'GA.js'
        )
        if not os.path.exists(ga_path):
            pytest.skip("EliteaUI/src/GA.js not found")

        with open(ga_path) as f:
            content = f.read()

        # In production (!DEV), the cookie flags should include 'secure'
        assert "samesite=none;secure" in content


# ---------------------------------------------------------------------------
# Tests for Flask integration (simulated)
# ---------------------------------------------------------------------------

class TestFlaskIntegration:
    """Simulate Flask after_request behavior with cookie hardening."""

    def test_session_cookie_not_double_flagged(self):
        """When Flask already sets flags, middleware doesn't duplicate."""
        header = (
            'elitea_auth_session=eyJ0eXAi...; '
            'Expires=Mon, 30 Jun 2026 12:00:00 GMT; '
            'HttpOnly; Path=/; SameSite=Lax; Secure'
        )
        result = harden_set_cookie_header(header, secure=True, samesite='Lax')
        assert result.count('HttpOnly') == 1
        assert result.count('Secure') == 1
        assert result.count('SameSite') == 1

    def test_hypothetical_new_cookie_gets_hardened(self):
        """If a new plugin sets a cookie without flags, middleware catches it."""
        header = 'new_feature_prefs=dark_mode; Path=/; Max-Age=2592000'
        result = harden_set_cookie_header(header, secure=True, samesite='Lax')
        assert '; HttpOnly' in result
        assert '; Secure' in result
        assert '; SameSite=Lax' in result
        # Preserves original attributes
        assert 'Max-Age=2592000' in result

    def test_domain_cookie_preserved(self):
        """Domain attribute is not affected by hardening."""
        header = 'session=abc; Path=/; Domain=.elitea.ai'
        result = harden_set_cookie_header(header, secure=True, samesite='Lax')
        assert 'Domain=.elitea.ai' in result
        assert '; HttpOnly' in result

    def test_empty_cookie_value(self):
        """Deleted cookies (empty value) still get flags."""
        header = 'old_cookie=; Path=/; Max-Age=0'
        result = harden_set_cookie_header(header, secure=True, samesite='Lax')
        assert '; HttpOnly' in result
        assert 'Max-Age=0' in result


# ---------------------------------------------------------------------------
# Edge case and security tests
# ---------------------------------------------------------------------------

class TestSecurityEdgeCases:
    """Edge cases and security scenarios."""

    def test_injection_attempt_in_cookie_name(self):
        """Cookie name with injection attempt still gets hardened safely."""
        header = 'evil; HttpOnly=value; Path=/'
        result = harden_set_cookie_header(header, secure=True, samesite='Lax')
        # HttpOnly appears in the "name" but the function should still add Secure/SameSite
        assert '; Secure' in result
        assert '; SameSite=Lax' in result

    def test_very_long_cookie(self):
        """Long cookie values don't break the function."""
        long_value = 'x' * 4096
        header = f'big_session={long_value}; Path=/'
        result = harden_set_cookie_header(header, secure=True, samesite='Lax')
        assert '; HttpOnly' in result
        assert '; Secure' in result
        assert long_value in result

    def test_unicode_in_cookie_value(self):
        """Unicode characters in cookie values are preserved."""
        header = 'pref=lang%3Dja%26region%3D日本; Path=/'
        result = harden_set_cookie_header(header, secure=True, samesite='Lax')
        assert '日本' in result
        assert '; HttpOnly' in result

    def test_multiple_equals_in_value(self):
        """Base64-encoded values with = padding are handled."""
        header = 'token=eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoiYWRtaW4ifQ==; Path=/'
        result = harden_set_cookie_header(header, secure=True, samesite='Lax')
        assert 'eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoiYWRtaW4ifQ==' in result
        assert '; HttpOnly' in result
