"""Behavioral tests for scripts/alert.sh, driven as a real subprocess.

No real mail is ever sent: a fake ``curl`` executable is placed first on
PATH and stands in for the real one, so these tests pin observable
behavior (exit code, stdout/stderr, what curl was invoked with) without
touching a network or reimplementing the script's own logic in Python.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ALERT_SCRIPT = REPO_ROOT / "scripts" / "alert.sh"

VALID_ENV = {
    "ALERT_EMAIL_TO": "operator@example.com",
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "songmaker@example.com",
    "SMTP_PASSWORD": "correct-horse-battery-staple",
}


def _write_env_file(path: Path, values: dict[str, str]) -> Path:
    env_file = path / ".env"
    env_file.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
    )
    return env_file


def _install_fake_curl(bin_dir: Path, exit_code: int = 0, error_message: str = "") -> Path:
    bin_dir.mkdir(exist_ok=True)
    fake_curl = bin_dir / "curl"
    fake_curl.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        'echo "$@" >> "${FAKE_CURL_ARGS_FILE:-/dev/null}"\n'
        'if [[ -n "${FAKE_CURL_CAPTURE_STDIN_FILE:-}" ]]; then\n'
        '    cat > "$FAKE_CURL_CAPTURE_STDIN_FILE"\n'
        "else\n"
        "    cat > /dev/null\n"
        "fi\n"
        f'if [[ "{exit_code}" != "0" ]]; then\n'
        f'    echo "{error_message}"\n'
        f"    exit {exit_code}\n"
        "fi\n"
        "exit 0\n",
    )
    fake_curl.chmod(fake_curl.stat().st_mode | stat.S_IEXEC)
    return fake_curl


def _run_alert(
    tmp_path: Path,
    args: list[str],
    env_values: dict[str, str] | None,
    fake_curl_exit_code: int = 0,
    fake_curl_error_message: str = "",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    _install_fake_curl(bin_dir, fake_curl_exit_code, fake_curl_error_message)

    env: dict[str, str] = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
    if env_values is not None:
        env_file = _write_env_file(tmp_path, env_values)
        env["SONGMAKER_ALERT_ENV_FILE"] = str(env_file)
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [str(ALERT_SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def test_sends_mail_via_curl_on_success(tmp_path: Path) -> None:
    args_file = tmp_path / "curl-args.txt"
    stdin_file = tmp_path / "curl-stdin.txt"

    result = _run_alert(
        tmp_path,
        ["GPU worker offline", "acestep-worker-0 has not reported in 10 minutes."],
        VALID_ENV,
        extra_env={
            "FAKE_CURL_ARGS_FILE": str(args_file),
            "FAKE_CURL_CAPTURE_STDIN_FILE": str(stdin_file),
        },
    )

    assert result.returncode == 0
    assert "sent" in result.stdout
    assert VALID_ENV["ALERT_EMAIL_TO"] in result.stdout

    curl_args = args_file.read_text()
    assert VALID_ENV["SMTP_HOST"] in curl_args
    assert VALID_ENV["SMTP_PORT"] in curl_args
    assert VALID_ENV["ALERT_EMAIL_TO"] in curl_args

    message = stdin_file.read_text()
    assert "Subject: GPU worker offline" in message
    assert "acestep-worker-0 has not reported in 10 minutes." in message


def test_missing_env_file_fails_loud(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(ALERT_SCRIPT), "subject", "body"],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "SONGMAKER_ALERT_ENV_FILE": str(tmp_path / "does-not-exist.env"),
        },
        timeout=10,
    )

    assert result.returncode != 0
    assert ".env" in result.stderr


@pytest.mark.parametrize("missing_key", sorted(VALID_ENV))
def test_missing_config_value_fails_loud_and_named(tmp_path: Path, missing_key: str) -> None:
    incomplete_env = {k: v for k, v in VALID_ENV.items() if k != missing_key}

    result = _run_alert(
        tmp_path,
        ["subject", "body"],
        incomplete_env,
    )

    assert result.returncode != 0
    assert missing_key in result.stderr
    assert "refusing" in result.stderr


def test_smtp_failure_reports_named_error_without_sending(tmp_path: Path) -> None:
    result = _run_alert(
        tmp_path,
        ["subject", "body"],
        VALID_ENV,
        fake_curl_exit_code=35,
        fake_curl_error_message="curl: (35) SSL connect error",
    )

    assert result.returncode != 0
    assert "SMTP send" in result.stderr
    assert "failed" in result.stderr
    assert "curl: (35) SSL connect error" in result.stderr
    assert "sent" not in result.stdout


def test_smtp_failure_never_leaks_password_in_output(tmp_path: Path) -> None:
    result = _run_alert(
        tmp_path,
        ["subject", "body"],
        VALID_ENV,
        fake_curl_exit_code=1,
        fake_curl_error_message=f"auth rejected for password {VALID_ENV['SMTP_PASSWORD']}",
    )

    assert result.returncode != 0
    assert VALID_ENV["SMTP_PASSWORD"] not in result.stderr
    assert VALID_ENV["SMTP_PASSWORD"] not in result.stdout
    assert "[REDACTED]" in result.stderr


def test_wrong_argument_count_fails_with_usage(tmp_path: Path) -> None:
    result = _run_alert(tmp_path, ["only-one-arg"], VALID_ENV)

    assert result.returncode != 0
    assert "usage" in result.stderr
