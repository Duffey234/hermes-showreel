from hermes_showreel.sample import _fake_key as fake_key
from hermes_showreel import redact


def mk(*parts):  # build secret-shaped test strings at runtime (keeps the repo scan clean)
    return "".join(parts)


CASES = [
    mk("OPENAI_API_KEY=", "sk-", "proj-", "A1b2C3d4E5f6G7h8I9j0K1l2"),
    mk("token ", "ghp_", "a" * 20, "B" * 16),
    mk("export AWS_ACCESS_KEY_ID=", "AKIA", "ABCDEFGHIJKLMNOP"),
    mk("curl -H 'Author", "ization: Bearer ", "abcDEF123456ghiJKL789", "'"),
    mk("postgres://admin:", "hunter2pass", "@db.internal:5432/app"),
    mk("key=", "AIza", "S" * 35),
    mk("xi-api-key: ", "sk_", "0123456789abcdef" * 3),
    mk('{"password": "', "correct-horse-battery", '"}'),
    mk("PRIVATE_KEY=0x", "ab" * 32),
    mk("-----BEG", "IN OPENSSH ", "PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH ", "PRIVATE KEY-----"),
    mk("https://api.example.com/v1?api_key=", "Zx81kq0mmA77", "&q=1"),
    mk("slack ", "xoxb-", "1234567890-abcdefghij"),
    mk("jwt ", "eyJhbGciOiJIUzI1NiJ9", ".", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", ".", "dozjgNryP4J3jVmNHl0w5N"),
    mk("weird ", "Qm9vT3h4Uk9MTHlLWnRwZ0h2SkdWcWZ6", "YXI1bTlE"),
]


def test_known_secret_shapes_are_masked():
    for raw in CASES:
        clean, n = redact.scrub(raw)
        assert n >= 1, raw
        assert redact.MASK in clean or "[email]" in clean, (raw, clean)


def test_secret_values_never_survive():
    secrets = ["A1b2C3d4E5f6G7h8I9j0K1l2", "hunter2pass", "correct-horse-battery", "Zx81kq0mmA77",
               "b3BlbnNzaC1rZXktdjEAAAAA"]
    blob = "\n".join(CASES)
    clean, _ = redact.scrub(blob)
    for s in secrets:
        assert s not in clean


def test_sample_fake_key_is_masked():
    clean, n = redact.scrub(f"use {fake_key()} please")
    assert fake_key() not in clean and n >= 1


def test_ordinary_dev_output_is_kept():
    keep = [
        "[main 3f9c2ab] snake: wrap at the edges",
        "commit 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b",
        "4 passed in 0.10s",
        "tokens_used=1520",
        "src/components/SnakeBoard.tsx",
        "npm install react@18.3.1",
        "max_tokens: 4096",
    ]
    for line in keep:
        clean, n = redact.scrub(line)
        assert clean == line and n == 0, (line, clean)


def test_home_paths_and_emails(monkeypatch):
    monkeypatch.setenv("HOME", "/home/alice")
    clean, _ = redact.scrub("open /home/alice/projects/x.py and mail alice@example.com")
    assert "/home/alice" not in clean and "~/projects/x.py" in clean
    assert "alice@example.com" not in clean and "[email]" in clean
    clean2, _ = redact.scrub("mail alice@example.com", emails=False)
    assert "alice@example.com" in clean2


def test_scrub_obj_masks_secret_keys():
    obj = {"api_key": "plainvalue123", "nested": [{"token": "abc123456789"}], "max_tokens": 4096, "path": "a.py"}
    clean, n = redact.scrub_obj(obj)
    assert clean["api_key"] == redact.MASK and clean["nested"][0]["token"] == redact.MASK
    assert clean["max_tokens"] == 4096 and clean["path"] == "a.py" and n == 2


def test_scrub_never_raises_on_odd_input():
    for v in (None, 123, b"bytes", {"a": 1}, "x" * 200000):
        redact.scrub(v)
