import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


ROOT_DIR = Path(__file__).resolve().parent.parent.parent


@dataclass
class TariffSelection:
    version: str
    season_rule_name: str
    tiers: list[dict[str, Any]]


class TimeOfUsePriceResolver:
    def __init__(self, config_path: Optional[str] = None) -> None:
        self.config_path = self._resolve_config_path(config_path)
        self._config_cache: Optional[dict[str, Any]] = None

    def _resolve_config_path(self, config_path: Optional[str]) -> Path:
        config_path = config_path or os.getenv("TOU_PRICE_CONFIG")
        if config_path:
            path = Path(config_path)
            if path.is_absolute():
                return path
            return ROOT_DIR / path

        return ROOT_DIR / "config" / "tou_price_config.json"

    def _load_config(self) -> dict[str, Any]:
        if self._config_cache is not None:
            return self._config_cache

        env_config = self._config_from_env()
        if env_config is not None:
            self._config_cache = env_config
            return self._config_cache

        if not self.config_path.exists():
            logging.warning("TOU price config not found: %s", self.config_path)
            self._config_cache = {"versions": []}
            return self._config_cache

        with open(self.config_path, "r", encoding="utf-8") as file:
            self._config_cache = json.load(file)
        return self._config_cache

    @staticmethod
    def _config_from_env() -> Optional[dict[str, Any]]:
        """If TOU_PEAK_RATE / TOU_VALLEY_RATE etc are set, synthesize a
        single-tier all-year config from them. This lets users skip the
        per-province example config file and just put the two rates from
        their bill into the add-on options.

        Set ``TOU_PEAK_RATE`` to enable. ``TOU_VALLEY_RATE``,
        ``TOU_FLAT_RATE``, ``TOU_TIP_RATE`` default to the peak rate
        when unset (works for households with peak/valley only).
        """
        peak_raw = (os.getenv("TOU_PEAK_RATE") or "").strip()
        if not peak_raw:
            return None
        try:
            peak = float(peak_raw)
        except ValueError:
            logging.warning("TOU_PEAK_RATE %r is not a number; ignoring env override.", peak_raw)
            return None

        def _f(name: str, default: float) -> float:
            raw = (os.getenv(name) or "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                logging.warning("%s %r is not a number; using %s.", name, raw, default)
                return default

        valley = _f("TOU_VALLEY_RATE", peak)
        flat = _f("TOU_FLAT_RATE", peak)
        tip = _f("TOU_TIP_RATE", peak)
        logging.info(
            "TOU rate env override active: peak=%.4f valley=%.4f flat=%.4f tip=%.4f",
            peak, valley, flat, tip,
        )
        return {
            "versions": [
                {
                    "version": "env_override",
                    "validfrom": "1970-01-01",
                    "validuntil": "2099-12-31",
                    "season_rules": [
                        {
                            "name": "all_year",
                            "months": list(range(1, 13)),
                            "tiers": [
                                {
                                    "up_to": None,
                                    "rates": {
                                        "valley": valley,
                                        "flat": flat,
                                        "peak": peak,
                                        "tip": tip,
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    def get_selection_for_date(self, date_text: str) -> Optional[TariffSelection]:
        if not date_text:
            return None
        target_date = datetime.strptime(date_text, "%Y-%m-%d").date()
        target_month = target_date.month

        for version in self._load_config().get("versions", []):
            valid_from = datetime.strptime(version["validfrom"], "%Y-%m-%d").date()
            valid_until = datetime.strptime(version["validuntil"], "%Y-%m-%d").date()
            if not (valid_from <= target_date <= valid_until):
                continue

            for season_rule in version.get("season_rules", []):
                if target_month not in season_rule.get("months", []):
                    continue
                return TariffSelection(
                    version=version.get("version", ""),
                    season_rule_name=season_rule.get("name", ""),
                    tiers=season_rule.get("tiers", []),
                )
        return None

    def calculate_daily_charge(
        self,
        date_text: str,
        valley_usage: Any,
        flat_usage: Any,
        peak_usage: Any,
        tip_usage: Any,
        month_usage_before: Any = 0,
    ) -> Optional[float]:
        selection = self.get_selection_for_date(date_text)
        if selection is None:
            return None

        try:
            valley = float(valley_usage or 0)
            flat = float(flat_usage or 0)
            peak = float(peak_usage or 0)
            tip = float(tip_usage or 0)
            month_before = float(month_usage_before or 0)
        except (TypeError, ValueError):
            logging.warning("Failed to parse TOU usage values for %s", date_text)
            return None

        if not selection.tiers:
            logging.info("TOU price config matched %s but tiers are not configured yet", selection.version)
            return None

        total_usage = valley + flat + peak + tip
        if total_usage <= 0:
            return 0.0

        proportions = {
            "valley": valley / total_usage,
            "flat": flat / total_usage,
            "peak": peak / total_usage,
            "tip": tip / total_usage,
        }

        remaining_total = total_usage
        current_usage = month_before
        total = 0.0

        for tier in selection.tiers:
            tier_limit = tier.get("up_to")
            if tier_limit is None:
                tier_kwh = remaining_total
            else:
                tier_limit = float(tier_limit)
                if current_usage >= tier_limit:
                    continue
                tier_kwh = min(remaining_total, tier_limit - current_usage)

            if tier_kwh <= 0:
                continue

            rates = tier.get("rates", {})
            total += tier_kwh * proportions["valley"] * float(rates.get("valley", 0.0))
            total += tier_kwh * proportions["flat"] * float(rates.get("flat", 0.0))
            total += tier_kwh * proportions["peak"] * float(rates.get("peak", 0.0))
            total += tier_kwh * proportions["tip"] * float(rates.get("tip", 0.0))

            remaining_total -= tier_kwh
            current_usage += tier_kwh
            if remaining_total <= 1e-9:
                break

        if remaining_total > 1e-9:
            logging.warning("TOU ladder calculation did not consume all usage for %s", date_text)
            return None

        return round(total, 2)
