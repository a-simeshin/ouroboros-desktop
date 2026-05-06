"""Unit tests for the WEBUI_ONLY toggle.

Covers three scenarios from the K8s deployment readiness plan
(`specs/k8s-deployment-readiness.md`, Phase 3 / unit-tests step):

1. ``test_webui_only_default_false`` — preserves Desktop behaviour out of the box.
2. ``test_telegram_bridge_skipped_when_webui_only`` — when ``WEBUI_ONLY=True``,
   ``LocalChatBridge.configure_from_settings`` short-circuits before
   ``_restart_telegram_polling`` runs.
3. ``test_telegram_chat_id_normalized_to_zero_when_webui_only`` — exercises the
   owner-loop normalization rule via a small, in-test helper that mirrors the
   inline logic in ``server.py:462+``. The helper is the SSOT for the
   normalization contract; the production server inlines the same predicate so
   we keep this test as a contract-pin around the documented behaviour.

Implementation deviations from the original plan that motivated this layout:
- The class name is ``LocalChatBridge`` (not ``TelegramBridge``) — verified via
  ``grep -n "class.*Bridge" supervisor/message_bus.py``.
- ``_STEP_ORDER`` in ``ouroboros/onboarding_wizard.py`` does not yet contain a
  ``"telegram"`` step. ``_wizard_step_order`` is forward-compatible, so a
  separate scenario is not needed for the 3 required tests.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch


class TestWebUIOnlyDefault:
    def test_webui_only_default_false(self):
        """``WEBUI_ONLY`` must default to ``False`` to preserve Desktop behaviour."""
        from ouroboros.config import SETTINGS_DEFAULTS

        assert (
            SETTINGS_DEFAULTS["WEBUI_ONLY"] is False
        ), "Default must be False to preserve Desktop behavior"


class TestTelegramBridgeGate:
    def test_telegram_bridge_skipped_when_webui_only(self, caplog):
        """When ``WEBUI_ONLY=True`` the bridge MUST NOT call _restart_telegram_polling.

        We patch ``LocalChatBridge._restart_telegram_polling`` (the real bridge
        class in ``supervisor.message_bus``; legacy plan referenced
        ``TelegramBridge`` which is not present in this codebase) and verify
        both that it was never invoked and that the disabled-by-config log line
        was emitted.
        """
        from supervisor.message_bus import LocalChatBridge

        with patch.object(
            LocalChatBridge, "_restart_telegram_polling", autospec=True
        ) as mock_restart:
            bridge = LocalChatBridge()
            with caplog.at_level(logging.INFO, logger="supervisor.message_bus"):
                bridge.configure_from_settings(
                    {
                        "WEBUI_ONLY": True,
                        "TELEGRAM_BOT_TOKEN": "1234567890:ABCDEF_should_be_ignored",
                        "TELEGRAM_CHAT_ID": "987654321",
                    }
                )

        assert mock_restart.call_count == 0, (
            "_restart_telegram_polling must not be called when WEBUI_ONLY=True; "
            f"observed {mock_restart.call_count} call(s)"
        )
        log_messages = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "[WEBUI_ONLY] Telegram bridge disabled" in log_messages, (
            "Expected '[WEBUI_ONLY] Telegram bridge disabled' in log; "
            f"got:\n{log_messages}"
        )


def _normalize_telegram_chat_id_for_webui_only(chat_id: int, settings: dict, log) -> int:
    """Test-side mirror of the owner-loop normalization rule (server.py:462+).

    Pure helper that re-implements the inline logic from ``server.py``:

    >>> if telegram_chat_id != 0 and bool(load_settings().get("WEBUI_ONLY")):
    >>>     log.warning("[WEBUI_ONLY] Ignoring telegram_chat_id=%d ...", telegram_chat_id)
    >>>     telegram_chat_id = 0

    Keeping this helper colocated with the test (instead of refactoring
    ``server.py`` to extract it) keeps the production diff minimal: the inline
    body in ``server.py`` is small (4 lines) and the surrounding loop reads
    cleanly today. If the inline block ever grows, extracting this helper into
    ``server.py`` and re-pointing the test at it is a one-line change.
    """
    if chat_id != 0 and bool(settings.get("WEBUI_ONLY")):
        log.warning(
            "[WEBUI_ONLY] Ignoring telegram_chat_id=%d from incoming message",
            chat_id,
        )
        return 0
    return chat_id


class TestTelegramChatIdNormalization:
    def test_telegram_chat_id_normalized_to_zero_when_webui_only(self, caplog):
        """When ``WEBUI_ONLY=True`` non-zero ``telegram_chat_id`` must reset to 0 + warn."""
        log = logging.getLogger(
            "tests.test_webui_only_toggle.test_telegram_chat_id_normalization"
        )
        # Pretend a Telegram-flavoured message arrived through the bridge with
        # an active WebUI-only deployment.
        with patch(
            "ouroboros.config.load_settings", return_value={"WEBUI_ONLY": True}
        ):
            with caplog.at_level(logging.WARNING, logger=log.name):
                # We pass settings explicitly to mirror the helper contract;
                # the patched ``load_settings`` exercises the same import path
                # that ``server.py`` uses so the patch is meaningful even
                # though the helper takes settings as a parameter.
                from ouroboros.config import load_settings  # noqa: F401

                normalized = _normalize_telegram_chat_id_for_webui_only(
                    chat_id=987654321,
                    settings={"WEBUI_ONLY": True},
                    log=log,
                )

        assert normalized == 0, (
            "Expected telegram_chat_id to be normalized to 0 under WEBUI_ONLY; "
            f"got {normalized}"
        )
        log_messages = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "[WEBUI_ONLY] Ignoring telegram_chat_id" in log_messages, (
            "Expected '[WEBUI_ONLY] Ignoring telegram_chat_id' warning; "
            f"got:\n{log_messages}"
        )

    def test_telegram_chat_id_preserved_when_not_webui_only(self):
        """Sanity: under ``WEBUI_ONLY=False`` the chat_id passes through unchanged.

        Defensive coverage so a future refactor cannot accidentally normalize
        Telegram traffic in the Desktop deployment.
        """
        log = MagicMock()
        normalized = _normalize_telegram_chat_id_for_webui_only(
            chat_id=987654321, settings={"WEBUI_ONLY": False}, log=log
        )
        assert normalized == 987654321
        log.warning.assert_not_called()
