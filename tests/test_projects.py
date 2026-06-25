"""Tests voor config.toml loader."""
from __future__ import annotations

from pathlib import Path

import pytest

from email_bot.projects import OWNER_EMAIL, load_projects


def _write_toml(tmp_path: Path, content: str) -> Path:
    cfg = tmp_path / 'config.toml'
    cfg.write_text(content)
    return cfg


def test_load_basic_project(tmp_path: Path) -> None:
    cfg = _write_toml(tmp_path, """
[projects.bra1n]
path = "/Users/hp/ai/Bra1n/website"
email = "bra1n@harmsen.nl"
whitelist = ["michael@anderswinst.com"]
""")
    projects = load_projects(cfg)
    assert set(projects.keys()) == {'bra1n@harmsen.nl'}
    p = projects['bra1n@harmsen.nl']
    assert p.name == 'bra1n'
    assert p.path == Path('/Users/hp/ai/Bra1n/website')
    assert p.email == 'bra1n@harmsen.nl'
    assert 'michael@anderswinst.com' in p.whitelist
    assert OWNER_EMAIL in p.whitelist     # impliciet altijd whitelisted
    assert p.live_url is None
    assert p.alias_name == 'Bra1n'


def test_load_multiple_projects(tmp_path: Path) -> None:
    cfg = _write_toml(tmp_path, """
[projects.bra1n]
path = "/a"
email = "bra1n@harmsen.nl"

[projects.juf-marijke]
path = "/b"
email = "juf-marijke@harmsen.nl"
""")
    projects = load_projects(cfg)
    assert len(projects) == 2
    assert projects['bra1n@harmsen.nl'].name == 'bra1n'
    assert projects['juf-marijke@harmsen.nl'].name == 'juf-marijke'


def test_owner_always_in_whitelist_even_when_not_listed(tmp_path: Path) -> None:
    cfg = _write_toml(tmp_path, """
[projects.x]
path = "/x"
email = "x@harmsen.nl"
""")
    projects = load_projects(cfg)
    assert OWNER_EMAIL in projects['x@harmsen.nl'].whitelist


def test_whitelist_lowercased(tmp_path: Path) -> None:
    cfg = _write_toml(tmp_path, """
[projects.x]
path = "/x"
email = "X@Harmsen.NL"
whitelist = ["Foo@Bar.COM"]
""")
    projects = load_projects(cfg)
    p = list(projects.values())[0]
    assert p.email == 'x@harmsen.nl'
    assert 'foo@bar.com' in p.whitelist


def test_email_must_end_with_harmsen_nl(tmp_path: Path) -> None:
    cfg = _write_toml(tmp_path, """
[projects.x]
path = "/x"
email = "x@example.com"
""")
    with pytest.raises(ValueError, match='harmsen.nl'):
        load_projects(cfg)


def test_path_must_be_absolute(tmp_path: Path) -> None:
    cfg = _write_toml(tmp_path, """
[projects.x]
path = "relatief/pad"
email = "x@harmsen.nl"
""")
    with pytest.raises(ValueError, match='absoluut'):
        load_projects(cfg)


def test_duplicate_email_raises(tmp_path: Path) -> None:
    cfg = _write_toml(tmp_path, """
[projects.a]
path = "/a"
email = "shared@harmsen.nl"

[projects.b]
path = "/b"
email = "shared@harmsen.nl"
""")
    with pytest.raises(ValueError, match='Dubbel'):
        load_projects(cfg)


def test_missing_required_field_raises(tmp_path: Path) -> None:
    cfg = _write_toml(tmp_path, """
[projects.x]
path = "/x"
""")
    with pytest.raises(ValueError, match='email'):
        load_projects(cfg)


def test_no_projects_section_raises(tmp_path: Path) -> None:
    cfg = _write_toml(tmp_path, '')
    with pytest.raises(ValueError, match='projects'):
        load_projects(cfg)


def test_missing_config_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_projects(tmp_path / 'nope.toml')


def test_alias_from_format(tmp_path: Path) -> None:
    cfg = _write_toml(tmp_path, """
[projects.bra1n]
path = "/x"
email = "bra1n@harmsen.nl"
alias_name = "Bra1n"
""")
    p = list(load_projects(cfg).values())[0]
    assert p.alias_from == 'Bra1n <bra1n@harmsen.nl>'


def test_alias_name_defaults_to_capitalized_project(tmp_path: Path) -> None:
    cfg = _write_toml(tmp_path, """
[projects.juf-marijke]
path = "/x"
email = "juf-marijke@harmsen.nl"
""")
    p = list(load_projects(cfg).values())[0]
    assert p.alias_name == 'Juf-marijke'


def test_live_url_passthrough(tmp_path: Path) -> None:
    cfg = _write_toml(tmp_path, """
[projects.x]
path = "/x"
email = "x@harmsen.nl"
live_url = "https://example.com"
""")
    p = list(load_projects(cfg).values())[0]
    assert p.live_url == 'https://example.com'


def test_publish_script_passthrough(tmp_path: Path) -> None:
    cfg = _write_toml(tmp_path, """
[projects.x]
path = "/x"
email = "x@harmsen.nl"
publish_script = "publish.sh"
""")
    p = list(load_projects(cfg).values())[0]
    assert p.publish_script == 'publish.sh'


def test_publish_script_defaults_to_none(tmp_path: Path) -> None:
    cfg = _write_toml(tmp_path, """
[projects.x]
path = "/x"
email = "x@harmsen.nl"
""")
    p = list(load_projects(cfg).values())[0]
    assert p.publish_script is None
