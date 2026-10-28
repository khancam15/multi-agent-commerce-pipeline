from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import scoped_env

ALL = {key: f"value-of-{key}" for key in scoped_env.SECRET_KEYS}


def test_each_script_keeps_only_its_own_secrets() -> None:
    env = scoped_env.scoped_environ("scripts/brand_crew.py", {**ALL, "BRAND_NAME": "Benjaire"})
    assert env["ANTHROPIC_API_KEY"] and env["SERPER_API_KEY"]
    assert env["STRIPE_SECRET_KEY"] == "" and env["EMAIL_SMTP_PASS"] == ""
    assert all(env[k] == "" for k in scoped_env.CANVA)
    assert env["BRAND_NAME"] == "Benjaire"


def test_order_delivery_gets_stripe_and_email_but_no_model_keys() -> None:
    env = scoped_env.scoped_environ("scripts/order_delivery.py", ALL)
    assert env["STRIPE_SECRET_KEY"] and env["EMAIL_SMTP_PASS"]
    assert env["ANTHROPIC_API_KEY"] == "" and env["SERPER_API_KEY"] == ""


def test_withheld_keys_are_set_blank_so_load_dotenv_cannot_refill_them() -> None:
    env = scoped_env.scoped_environ("scripts/storefront.py", {})
    assert set(scoped_env.SECRET_KEYS) <= env.keys()
    assert all(env[k] == "" for k in scoped_env.SECRET_KEYS)


def test_unlisted_script_gets_no_secrets() -> None:
    env = scoped_env.scoped_environ("scripts/something_new.py", ALL)
    assert all(env[k] == "" for k in scoped_env.SECRET_KEYS)


def test_scoping_is_off_unless_switched_on() -> None:
    assert not scoped_env.scoping_enabled({}, {})
    assert not scoped_env.scoping_enabled({}, {"AGENT_ENV_SCOPING": "false"})
    assert scoped_env.scoping_enabled({}, {"AGENT_ENV_SCOPING": "true"})
    assert scoped_env.scoping_enabled({"AGENT_ENV_SCOPING": "1"}, {})


def test_every_script_the_shell_runs_is_mapped() -> None:
    used = set()
    for shell in ("run.sh", "loop.sh"):
        used |= set(re.findall(r"scoped_env\.py'?,? '?scripts/(\w+\.py)", (ROOT / shell).read_text()))
    assert used, "run.sh and loop.sh should start scripts through scoped_env.py"
    assert used <= scoped_env.AGENT_SECRETS.keys(), used - scoped_env.AGENT_SECRETS.keys()


def test_wrapper_runs_the_script_with_the_scoped_environment(tmp_path: Path) -> None:
    probe = tmp_path / "seo_analyzer.py"
    probe.write_text("import os\nprint(repr(os.environ.get('SERPER_API_KEY')), repr(os.environ.get('STRIPE_SECRET_KEY')))\n")
    env = {"PATH": "/usr/bin:/bin", **ALL, "AGENT_ENV_SCOPING": "true"}
    out = subprocess.run([sys.executable, str(SCRIPTS_DIR / "scoped_env.py"), str(probe), "x"],
                         env=env, capture_output=True, text=True, check=True).stdout
    assert out.strip() == "'value-of-SERPER_API_KEY' ''"


def test_wrapper_changes_nothing_when_off(tmp_path: Path) -> None:
    probe = tmp_path / "seo_analyzer.py"
    probe.write_text("import os\nprint(repr(os.environ.get('STRIPE_SECRET_KEY')))\n")
    env = {"PATH": "/usr/bin:/bin", **ALL, "AGENT_ENV_SCOPING": "false"}
    out = subprocess.run([sys.executable, str(SCRIPTS_DIR / "scoped_env.py"), str(probe)],
                         env=env, capture_output=True, text=True, check=True).stdout
    assert out.strip() == "'value-of-STRIPE_SECRET_KEY'"
