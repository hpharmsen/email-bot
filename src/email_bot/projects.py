"""Project-configuratie laden uit config.toml.

Het ontvangende e-mailadres routeert naar het juiste project.
hp@harmsen.nl is altijd impliciet whitelisted.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

OWNER_EMAIL = 'hp@harmsen.nl'
EMAIL_DOMAIN_SUFFIX = '@harmsen.nl'


@dataclass(frozen=True)
class Project:
    name: str
    path: Path
    email: str
    whitelist: frozenset[str]
    live_url: str | None
    alias_name: str
    publish_script: str | None

    @property
    def alias_from(self) -> str:
        return f'{self.alias_name} <{self.email}>'


def load_projects(config_path: Path) -> dict[str, Project]:
    """Returns {receiving_email: Project}. Validate alles strikt bij laden."""
    if not config_path.exists():
        raise FileNotFoundError(f'config.toml ontbreekt: {config_path}')
    data = tomllib.loads(config_path.read_text())
    raw = data.get('projects', {})
    if not raw:
        raise ValueError('config.toml bevat geen [projects.*] secties.')

    by_email: dict[str, Project] = {}
    for name, cfg in raw.items():
        project = _build_project(name, cfg)
        if project.email in by_email:
            raise ValueError(
                f'Dubbel ontvangend adres {project.email!r} '
                f'in projecten {by_email[project.email].name!r} en {name!r}.'
            )
        by_email[project.email] = project
    return by_email


def _build_project(name: str, cfg: dict) -> Project:
    for required in ('path', 'email'):
        if required not in cfg:
            raise ValueError(f'project {name!r}: veld {required!r} ontbreekt.')

    email = cfg['email'].strip().lower()
    if not email.endswith(EMAIL_DOMAIN_SUFFIX):
        raise ValueError(
            f'project {name!r}: email {email!r} moet eindigen op {EMAIL_DOMAIN_SUFFIX}.'
        )

    path = Path(cfg['path']).expanduser()
    if not path.is_absolute():
        raise ValueError(f'project {name!r}: path {path} moet absoluut zijn.')

    raw_whitelist = cfg.get('whitelist') or []
    whitelist = frozenset({addr.strip().lower() for addr in raw_whitelist if addr.strip()})
    whitelist = whitelist | {OWNER_EMAIL}

    return Project(
        name=name,
        path=path,
        email=email,
        whitelist=whitelist,
        live_url=cfg.get('live_url') or None,
        alias_name=cfg.get('alias_name') or name.capitalize(),
        publish_script=cfg.get('publish_script') or None,
    )
