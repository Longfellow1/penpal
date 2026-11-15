"""
Tests for privacy detector patterns and desensitization.
Covers all secret formats that need to be redacted.
"""
import pytest
from penpal.foundation.detectors import detect, has_sensitive_content
from penpal.foundation.desensitizer import desensitize, desensitize_json_value


class TestUUIDDetection:
    """Test UUID v4 pattern detection."""

    def test_detects_uuid_v4_lowercase(self):
        """Should detect UUID v4 in lowercase."""
        text = "API key is 93897140-abe8-450d-bdf8-cf2218b3715d"
        detections = detect(text)
        assert len(detections) == 1
        assert detections[0].pattern_name == "uuid_v4"

    def test_detects_uuid_v4_uppercase(self):
        """Should detect UUID v4 in uppercase."""
        text = "KEY: 93897140-ABE8-450D-BDF8-CF2218B3715D"
        detections = detect(text)
        assert len(detections) == 1
        assert detections[0].pattern_name == "uuid_v4"

    def test_does_not_detect_short_hex(self):
        """Should not detect incomplete hex strings as UUID."""
        text = "hash: 93897140-abe8-450d"
        detections = detect(text)
        assert len(detections) == 0

    def test_desensitize_removes_uuid(self):
        """Should replace UUID with [REDACTED]."""
        text = "Key: 93897140-abe8-450d-bdf8-cf2218b3715d secret"
        result = desensitize(text)
        assert "[REDACTED]" in result
        assert "93897140" not in result


class TestGitHubPATDetection:
    """Test GitHub Personal Access Token patterns."""

    def test_detects_ghp_classic_pat(self):
        """Should detect classic ghp_ PAT."""
        text = "token " + "ghp" + "_" + "rw1om51ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"
        detections = detect(text)
        assert len(detections) >= 1
        assert any(d.pattern_name == "github_pat" for d in detections)

    def test_detects_github_pat_fine_grained(self):
        """Should detect fine-grained github_pat_ token."""
        text = "github" + "_" + "pat" + "_" + "1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        detections = detect(text)
        assert len(detections) >= 1
        assert any(d.pattern_name == "github_pat" for d in detections)

    def test_desensitize_removes_ghp(self):
        """Should replace GitHub PAT with [REDACTED]."""
        text = "My token is " + "ghp" + "_" + "rw1om51ABCDEFGHIJKLMNOPQRSTUVWXYZabcd" + " ok"
        result = desensitize(text)
        assert "[REDACTED]" in result
        assert "ghp_" not in result
        assert "rw1om51" not in result


class TestOpenAIKeyDetection:
    """Test OpenAI/Anthropic API key patterns."""

    def test_detects_sk_anthropic_key(self):
        """Should detect sk-ant- anthropic key."""
        text = "key=sk-ant-abcdefghijklmnopqrst1234567890"
        detections = detect(text)
        assert len(detections) >= 1
        assert any(d.pattern_name == "openai_key" for d in detections)

    def test_detects_sk_openai_key(self):
        """Should detect sk- openai key."""
        text = "SK-aBcDeFgHiJkLmNoPqRsT12345678901234567890"
        detections = detect(text)
        assert len(detections) >= 1
        assert any(d.pattern_name == "openai_key" for d in detections)

    def test_desensitize_removes_sk_key(self):
        """Should replace sk- key with [REDACTED]."""
        text = "My key: sk-ant-abcdefghijklmnopqrst1234567890 end"
        result = desensitize(text)
        assert "[REDACTED]" in result
        assert "sk-" not in result


class TestSlackTokenDetection:
    """Test Slack token patterns."""

    def test_detects_slack_bot_token(self):
        """Should detect xoxb- slack bot token."""
        text = "bot token: xoxb-1234567890ABCDEFGHIJKLMNOPQRSTUVabcd"
        detections = detect(text)
        assert len(detections) >= 1
        # May be detected as api_key or slack_token, both are fine
        assert any(d.pattern_name in ("slack_token", "api_key") for d in detections)

    def test_detects_slack_user_token(self):
        """Should detect xoxp- slack user token."""
        text = "xoxp-123456789ABCDEFGHIJKLMNOP-QRST-UVWX"
        detections = detect(text)
        assert len(detections) >= 1
        assert any(d.pattern_name == "slack_token" for d in detections)

    def test_desensitize_removes_slack_token(self):
        """Should replace slack token with [REDACTED]."""
        text = "Token is xoxb-1234567890ABCDEFGHIJKLMNOPQRSTUVabcd and works"
        result = desensitize(text)
        assert "[REDACTED]" in result
        assert "xoxb-" not in result


class TestAWSKeyDetection:
    """Test AWS Access Key patterns."""

    def test_detects_aws_access_key(self):
        """Should detect AKIA AWS access key."""
        text = "AWS key " + "AKI" + "A1234567890ABCDEF" + " in use"
        detections = detect(text)
        assert len(detections) >= 1
        assert any(d.pattern_name == "aws_key" for d in detections)

    def test_desensitize_removes_aws_key(self):
        """Should replace AWS key with [REDACTED]."""
        text = "Access key: " + "AKI" + "A1234567890ABCDEF" + " secret"
        result = desensitize(text)
        assert "[REDACTED]" in result
        assert "AKIA" not in result


class TestJWTDetection:
    """Test JWT token pattern detection."""

    def test_detects_jwt_token(self):
        """Should detect JWT tokens."""
        text = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        detections = detect(text)
        # JWT may also match other patterns
        assert len(detections) >= 1

    def test_desensitize_removes_jwt(self):
        """Should replace JWT with [REDACTED]."""
        text = "Token: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result = desensitize(text)
        assert "[REDACTED]" in result
        assert "eyJ" not in result


class TestMultipleSecrets:
    """Test detection and desensitization of multiple secrets."""

    def test_detects_multiple_secrets_in_text(self):
        """Should detect multiple different secret types."""
        text = "UUID: 93897140-abe8-450d-bdf8-cf2218b3715d and token " + "ghp" + "_" + "rw1om51ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"
        detections = detect(text)
        assert len(detections) >= 2

    def test_desensitize_multiple_secrets(self):
        """Should redact all secret types."""
        text = "Key 93897140-abe8-450d-bdf8-cf2218b3715d and token " + "ghp" + "_" + "rw1om51ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        result = desensitize(text)
        assert "93897140" not in result
        assert "ghp_" not in result
        assert result.count("[REDACTED]") >= 2


class TestFalsePositivesAndEdgeCases:
    """Test that legitimate content is not mistakenly redacted."""

    def test_uuid_not_detected_in_commit_hash(self):
        """UUIDs should not match incomplete hex strings (edge case)."""
        # Commit hash is only 40 chars, not a valid UUID
        text = "Commit: abc123def456ghi789jkl012mno345pqr678"
        detections = detect(text)
        # This should not match UUID pattern (incorrect format)
        uuid_detections = [d for d in detections if d.pattern_name == "uuid_v4"]
        assert len(uuid_detections) == 0

    def test_normal_hex_not_flagged_as_secret(self):
        """Normal 40-char hex strings without context should not be redacted."""
        text = "Hash: 356a192b7913b04c54574d18c28d46e6395428ab"
        detections = detect(text)
        # This is a normal SHA1 hash, not a secret - should not match any secret patterns
        # Only email/phone/id_card/bank_card patterns should match, not token patterns
        secret_detections = [d for d in detections if "token" in d.pattern_name or "key" in d.pattern_name]
        assert len(secret_detections) == 0

    def test_normal_email_is_preserved_by_default(self):
        """Email detection uses separate redact_emails flag."""
        text = "Contact: user@example.com"
        result = desensitize(text, redact_emails=False)
        assert "user@example.com" in result

    def test_has_sensitive_content_returns_bool(self):
        """has_sensitive_content should return bool."""
        assert has_sensitive_content("key 93897140-abe8-450d-bdf8-cf2218b3715d") is True
        assert has_sensitive_content("just normal text") is False


class TestDesensitizeWithFlags:
    """Test desensitize with different redaction flags."""

    def test_redact_tokens_false_preserves_secrets(self):
        """When redact_tokens=False, token patterns should not be redacted."""
        text = "Token: " + "ghp" + "_" + "rw1om51ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"
        result = desensitize(text, redact_tokens=False)
        assert "ghp_" in result

    def test_redact_tokens_true_redacts_all_token_patterns(self):
        """When redact_tokens=True, all token patterns should be redacted."""
        text = ("ghp" + "_" + "rw1om51ABCDEFGHIJKLMNOPQRSTUVWXYZabcd" + " " +
                "xox" + "b" + "-" + "1234567890ABCDEFGHIJKLMNOPQRSTUVabcd")
        result = desensitize(text, redact_tokens=True)
        assert "ghp_" not in result
        assert "xoxb-" not in result


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sudo ln -sf /tmp/a /tmp/b", "[REDACTED_SHELL]"),
        ("export DEEPSEEK_API_KEY=\"secret\"", "[REDACTED_SHELL]"),
        ("brew install cc-switch", "[REDACTED_SHELL]"),
        ("cat >> ~/.zshrc <<'EOF'", "[REDACTED_SHELL]"),
        ("echo >> ~/.bash_profile", "[REDACTED_SHELL]"),
        ("Last login: Mon Apr 27 09:15:50 on ttys009", "[REDACTED_SHELL]"),
        ("==> Installing Cask cc-switch", "[REDACTED_SHELL]"),
        ("which cc-switch", "[REDACTED_SHELL]"),
    ],
)
def test_desensitize_redacts_shell_command_lines(text: str, expected: str):
    assert desensitize(text) == expected


def test_desensitize_json_value_recurses_through_nested_structures():
    fake_ghp = "g" + "hp_" + "rw1om51ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"
    fake_slack = "x" + "oxb-" + "1234567890ABCDEFGHIJKLMNOPQRSTUVabcd"
    fake_openai = "s" + "k-" + "ant-abcdefghijklmnopqrst1234567890"
    payload = {
        "email": "alice@example.com",
        "nested": {
            "token": fake_ghp,
            "items": [
                "safe",
                fake_slack,
                {"secret": fake_openai},
            ],
        },
        "count": 3,
        "flag": True,
    }

    result = desensitize_json_value(payload)

    assert result["email"] == "***@example.com"
    assert result["nested"]["token"] == "[REDACTED]"
    assert result["nested"]["items"][0] == "safe"
    assert result["nested"]["items"][1] == "[REDACTED]"
    assert result["nested"]["items"][2]["secret"] == "[REDACTED]"
    assert result["count"] == 3
    assert result["flag"] is True


# All document numbers below are made up: they follow the format, not any real person.
PASSPORT_POSITIVES = [
    ("E12345678", "E12345678"),
    ("EA1234567", "EA1234567"),
    ("G12345678", "G12345678"),
    ("P1234567", "P1234567"),
    ("S12345678", "S12345678"),
    ("D1234567", "D1234567"),
    ("C12345678", "C12345678"),
    ("CB1234567", "CB1234567"),
    ("H12345678", "H12345678"),
    ("M1234567890", "M1234567890"),
    ("我的护照号是E12345678，下周出发", "E12345678"),
    ("港澳通行证：CB1234567。", "CB1234567"),
    ("回乡证H1234567890到期了", "H1234567890"),
    ("台胞证号码：0123456789", "0123456789"),
    ("Passport No: X1234567B", "X1234567B"),
    ("passport number is 987654321", "987654321"),
    ("PASSPORT #K12345678", "K12345678"),
    ("Travel Document No. AB123456", "AB123456"),
    ("证件号为 TW12345678", "TW12345678"),
    ("通行证号码是 T98765432", "T98765432"),
    ("My passport is E12345678. See you", "E12345678"),  # sentence-ending dot
    ("护照号E12345678。", "E12345678"),
]

PASSPORT_NEGATIVES = [
    "E2E12345",  # end-to-end test tag
    "G1234567890123",  # too long for any format
    "commit e1234567 fixes it",  # lowercase git short sha
    "sha E1234567",  # one digit short
    "release v1.2.3 and 2.10.0",
    "D1234567-ABCD-1234",  # uppercase UUID fragment
    "colour #D1234567",
    "test_E12345678",  # identifier
    "E12345678abc",  # longer alphanumeric run
    "build 1.E12345678.3",  # version-like
    "SM-G9980 RTX4090 XPS13-9310",  # product models
    "DE12345678",  # letter before the prefix
    "H1234567",  # home-return permit needs 8-10 digits
    "M12345678901",  # 11 digits
    "护照到期日 2027-03-01",  # keyword but no number right after it
    "护照 1234",  # too few digits
    "passport photo booth",  # keyword followed by a plain word
    "证件号后四位 ABCD12",  # keyword, then words, not the number
    "技术交流S12345678.pdf",  # document serial in a file name
    "scan_E12345678.jpg and EA1234567.png",  # file names
]


@pytest.mark.parametrize("text,number", PASSPORT_POSITIVES)
def test_passport_like_numbers_are_masked(text, number):
    detections = detect(text)
    assert [d.pattern_name for d in detections if text[d.start:d.end] == number]
    result = desensitize(text)
    assert number not in result
    assert "[PASSPORT]" in result


@pytest.mark.parametrize("text", PASSPORT_NEGATIVES)
def test_code_and_product_strings_are_not_passports(text):
    assert not [d for d in detect(text) if d.replacement == "[PASSPORT]"]


def test_passport_mask_keeps_surrounding_chinese_context():
    assert desensitize("我的护照号是E12345678，下周出发") == "我的护照号是[PASSPORT]，下周出发"
    assert desensitize("Passport No: X1234567B, expires soon") == "Passport No: [PASSPORT], expires soon"


def test_passport_is_not_a_secret_class_pattern():
    # Imported sources only get credential-class masking; document numbers follow id_card there.
    from penpal.foundation.desensitizer import redact_secrets

    assert redact_secrets("护照号是E12345678") == "护照号是E12345678"


def test_captured_clipboard_text_is_stored_without_passport_number(tmp_path):
    from penpal.capture.manager import CaptureManager
    from penpal.capture.normalizer import normalize_clipboard_event
    from penpal.config import Config
    from penpal.store import buckets
    from penpal.store.db import close, get_conn, init_db

    close()
    buckets.reset_cache()
    init_db(tmp_path / "capture.db")
    try:
        mgr = CaptureManager(Config())
        mgr._process_event(normalize_clipboard_event(
            "我的护照号是E12345678，下周出发", app_name="Notes", ts_start="2026-03-02T10:00:01+00:00"))
        rows = [r[0] for r in get_conn().execute("SELECT content_text FROM raw_events WHERE source = 'clipboard'")]
        assert rows and all("E12345678" not in r and "[PASSPORT]" in r for r in rows)
        dump = "\n".join(get_conn().iterdump())
        assert "E12345678" not in dump
    finally:
        close()
        buckets.reset_cache()


def test_redact_documents_masks_only_document_numbers():
    from penpal.foundation.desensitizer import redact_documents

    # Synthetic: the ID passes GB 11643, the card passes Luhn.
    text = "护照号E12345678，身份证110101199003071233，卡6222021234567890128，邮箱alice@example.com，电话13812345678"
    assert redact_documents(text) == (
        "护照号[PASSPORT]，身份证[ID_CARD]，卡[BANK_CARD]，邮箱alice@example.com，电话13812345678"
    )
    assert redact_documents("Passport No: X1234567B") == "Passport No: [PASSPORT]"
    assert redact_documents("commit e1234567 on 2026-01-01") == "commit e1234567 on 2026-01-01"
    assert redact_documents("") == ""
