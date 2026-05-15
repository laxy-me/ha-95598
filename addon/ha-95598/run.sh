#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH=/data/options.json

json_get() {
  python3 - "$1" "$2" <<'PY'
import json
import sys
from pathlib import Path

key = sys.argv[1]
default = sys.argv[2]
path = Path("/data/options.json")
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    data = {}
value = data.get(key, default)
if isinstance(value, bool):
    print("true" if value else "false")
elif value is None:
    print("")
else:
    print(value)
PY
}

json_dump() {
  python3 - "$1" <<'PY'
import json
import sys
from pathlib import Path

key = sys.argv[1]
path = Path("/data/options.json")
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    data = {}
print(json.dumps(data.get(key, []), ensure_ascii=False))
PY
}

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "Add-on options not found at ${CONFIG_PATH}" >&2
  exit 1
fi

export ACCOUNT="$(json_get account "")"
export PASSWORD="$(json_get password "")"
export IGNORE_USER_ID="$(json_get ignore_user_id "")"
export MQTT_HOST="$(json_get mqtt_host core-mosquitto)"
export MQTT_PORT="$(json_get mqtt_port 1883)"
export MQTT_USERNAME="$(json_get mqtt_username "")"
export MQTT_PASSWORD="$(json_get mqtt_password "")"
export JOB_START_TIME="$(json_get job_start_time 07:00)"
export JOB_TIMES="$(json_get job_times 2)"
export RETRY_TIMES_LIMIT="$(json_get retry_times_limit 3)"
export RETRY_WAIT_TIME_OFFSET_UNIT="$(json_get retry_wait_time_offset_unit 5)"
export REPUBLISH_INTERVAL_MINUTES="$(json_get republish_interval_minutes 60)"
export DAILY_USAGE_WINDOW_DAYS="$(json_get daily_usage_window_days 7)"
export PUBLISH_TOU_DETAIL_SENSORS="$(json_get publish_tou_detail_sensors false)"
export TOU_PRICE_CONFIG="$(json_get tou_price_config config/tou_price_config.json)"
export NOTIFIER="$(json_get notifier none)"
export TG_BOT_TOKEN="$(json_get tg_bot_token "")"
export TG_CHAT_ID="$(json_get tg_chat_id "")"
export TG_API_BASE_URL="$(json_get tg_api_base_url https://api.telegram.org)"
export STALE_DATA_ALERT_DAYS="$(json_get stale_data_alert_days 2)"
export CAPTCHA_POINT_CLICK_MAX_REFRESHES="$(json_get captcha_point_click_max_refreshes 4)"
export LOGIN_FALLBACK="$(json_get login_fallback qrcode)"
export QR_CODE_LOGIN_REFRESH_LIMIT="$(json_get qr_code_login_refresh_limit 1)"
export TRACE_RETENTION_DAYS="$(json_get trace_retention_days 7)"
export LLM_API_KEY="$(json_get llm_api_key "")"
export LLM_PROVIDER="$(json_get llm_provider zhipu)"
export LLM_MODEL="$(json_get llm_model "")"
export LLM_BASE_URL="$(json_get llm_base_url "")"
# Ingress port for the small QR HTTP server. Must match config.yaml's
# ingress_port. The python qr_server reads this env var.
export INGRESS_PORT=8099

# TOU rate override: only export when user actually set a nonzero peak rate.
# Otherwise leave unset so tou_price.py falls back to the JSON config file.
TOU_PEAK_RATE_VALUE="$(json_get tou_peak_rate 0)"
if [[ "${TOU_PEAK_RATE_VALUE}" != "0" && "${TOU_PEAK_RATE_VALUE}" != "0.0" ]]; then
  export TOU_PEAK_RATE="${TOU_PEAK_RATE_VALUE}"
  TOU_VALLEY_RATE_VALUE="$(json_get tou_valley_rate 0)"
  if [[ "${TOU_VALLEY_RATE_VALUE}" != "0" && "${TOU_VALLEY_RATE_VALUE}" != "0.0" ]]; then
    export TOU_VALLEY_RATE="${TOU_VALLEY_RATE_VALUE}"
  fi
  TOU_FLAT_RATE_VALUE="$(json_get tou_flat_rate 0)"
  if [[ "${TOU_FLAT_RATE_VALUE}" != "0" && "${TOU_FLAT_RATE_VALUE}" != "0.0" ]]; then
    export TOU_FLAT_RATE="${TOU_FLAT_RATE_VALUE}"
  fi
  TOU_TIP_RATE_VALUE="$(json_get tou_tip_rate 0)"
  if [[ "${TOU_TIP_RATE_VALUE}" != "0" && "${TOU_TIP_RATE_VALUE}" != "0.0" ]]; then
    export TOU_TIP_RATE="${TOU_TIP_RATE_VALUE}"
  fi
  # 阶梯电价 ladder
  export TOU_TIER_SCOPE="$(json_get tou_tier_scope month)"
  TIER2_LIMIT="$(json_get tou_tier_2_limit_kwh 0)"
  if [[ "${TIER2_LIMIT}" != "0" && "${TIER2_LIMIT}" != "0.0" ]]; then
    export TOU_TIER_2_LIMIT_KWH="${TIER2_LIMIT}"
    TIER3_LIMIT="$(json_get tou_tier_3_limit_kwh 0)"
    if [[ "${TIER3_LIMIT}" != "0" && "${TIER3_LIMIT}" != "0.0" ]]; then
      export TOU_TIER_3_LIMIT_KWH="${TIER3_LIMIT}"
    fi
    SURCHARGE2="$(json_get tou_tier_2_surcharge 0)"
    if [[ "${SURCHARGE2}" != "0" && "${SURCHARGE2}" != "0.0" ]]; then
      export TOU_TIER_2_SURCHARGE="${SURCHARGE2}"
    fi
    SURCHARGE3="$(json_get tou_tier_3_surcharge 0)"
    if [[ "${SURCHARGE3}" != "0" && "${SURCHARGE3}" != "0.0" ]]; then
      export TOU_TIER_3_SURCHARGE="${SURCHARGE3}"
    fi
  fi
fi

LOGIN_CREDENTIALS_JSON="$(json_dump login_credentials)"
if [[ "${LOGIN_CREDENTIALS_JSON}" != "[]" ]]; then
  export LOGIN_CREDENTIALS="${LOGIN_CREDENTIALS_JSON}"
fi

# Start Xvfb in background instead of `xvfb-run`.
#
# `xvfb-run` deadlocks when running as PID 1 inside a container: it uses
# `trap '' USR1` + `exec Xvfb` + `wait` to detect when the X server is
# ready. The trick depends on the parent (xvfb-run) receiving SIGUSR1 to
# break out of `wait`, but bash running as PID 1 treats unhandled
# signals (including USR1) as SIG_IGN, so the signal never interrupts
# `wait`. xvfb-run blocks forever in sigsuspend, no python child is
# ever spawned, and the add-on appears running with ~23 MB RSS and
# zero stdout.
#
# Running Xvfb directly in the background and pointing DISPLAY at it
# avoids the signal dance entirely.
DISPLAY_NUM=99
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}" 2>/dev/null || true
Xvfb ":${DISPLAY_NUM}" -screen 0 1920x1080x24 -nolisten tcp &
XVFB_PID=$!
trap 'kill ${XVFB_PID} 2>/dev/null || true' EXIT
export DISPLAY=":${DISPLAY_NUM}"

# Give Xvfb a moment to set up its socket so chromium doesn't race it.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if [ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
    break
  fi
  sleep 0.5
done

exec python3 -m scripts.main
