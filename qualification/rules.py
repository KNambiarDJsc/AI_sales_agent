from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from config.settings import QUALIFICATION_DIR
from qualification.schema import QualificationRules


@lru_cache
def load_rules(script_id: str) -> QualificationRules:
    path = Path(QUALIFICATION_DIR) / "rules.yaml"
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if raw.get("script_id") != script_id:
        # PLACEHOLDER: today there's one rules.yaml. Once the client has multiple
        # campaigns, switch this to config/qualification/<script_id>.yaml and drop
        # this check.
        raise ValueError(f"No qualification rules found for script_id={script_id!r}")
    return QualificationRules(**raw)
