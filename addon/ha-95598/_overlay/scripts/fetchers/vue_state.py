"""Helpers for reading normalized data from 95598 Vue page state."""

from __future__ import annotations

from typing import Any


SELECTED_VUE_DATA_SCRIPT = """
const clone = (value) => {
  try { return JSON.parse(JSON.stringify(value)); } catch (e) { return null; }
};
const wantedKeys = [
  'mixinGetYuEdata',
  'consInfoobj',
  'consInfo',
  'electric',
  'powerData',
  'mothData',
  'tableData',
  'tableData_t',
  'sevenEleList',
  'sevenEleList_t',
  'new_sevenEleList',
  'tariffC',
  'start',
  'end',
  'queryYear',
  'activeName',
  'billNumberList',
  'BillList',
  'billList',
  'billMonth',
  'NewtotalBillProvince',
  'optionalYearArray',
  'selectYear',
  'listData'
];
return Array.from(document.querySelectorAll('*'))
  .map((el, index) => {
    const vm = el.__vue__;
    if (!vm) return null;
    const data = {};
    wantedKeys.forEach((key) => {
      if (Object.prototype.hasOwnProperty.call(vm, key)) {
        data[key] = clone(vm[key]);
      }
    });
    if (!Object.keys(data).length) return null;
    return {
      index,
      tag: el.tagName,
      id: el.id || '',
      className: String(el.className || '').slice(0, 160),
      text: (el.innerText || el.textContent || '').trim().slice(0, 500),
      data
    };
  })
  .filter(Boolean);
"""


def selected_vue_data(driver) -> list[dict[str, Any]]:
    return driver.execute_script(SELECTED_VUE_DATA_SCRIPT)


def normalize_balance(components: list[dict[str, Any]]) -> dict[str, Any]:
    raw = _first_data_value(components, "mixinGetYuEdata") or {}
    return {
        "as_of": raw.get("amtTime"),
        "balance": _safe_float(raw.get("sumMoney")),
        "prepay_balance": _safe_float(raw.get("prepayBal")),
        "estimated_amount": _safe_float(raw.get("estiAmt")),
        "history_owe": _safe_float(raw.get("historyOwe")),
        "penalty": _safe_float(raw.get("penalty")),
        "total_usage": _safe_float(raw.get("totalPq")),
        "user_id": raw.get("consNo"),
        "raw": raw,
    }


def normalize_usage(components: list[dict[str, Any]]) -> dict[str, Any]:
    power_data = _first_data_value(components, "powerData") or _first_data_value(components, "tableData_t") or {}
    info = power_data.get("dataInfo") or {}
    month_rows = power_data.get("mothEleList") or _first_data_value(components, "mothData") or []

    daily_rows = []
    for key in ("tableData", "new_sevenEleList", "sevenEleList"):
        for row in _data_values(components, key):
            if isinstance(row, list) and row and any(item.get("thisVPq") is not None for item in row if isinstance(item, dict)):
                daily_rows = row
                break
        if daily_rows:
            break

    return {
        "year": str(info.get("year") or _first_data_value(components, "queryYear") or ""),
        "yearly_usage": _safe_float(info.get("totalEleNum")),
        "yearly_charge": _safe_float(info.get("totalEleCost")),
        "recent_total_usage": _safe_float(_first_data_value(components, "tariffC")),
        "daily_range": {
            "start": _first_data_value(components, "start"),
            "end": _first_data_value(components, "end"),
        },
        "months": [_normalize_usage_month(row) for row in month_rows if isinstance(row, dict)],
        "daily": [_normalize_daily_row(row) for row in daily_rows if isinstance(row, dict) and _normalize_daily_row(row)],
        "raw": power_data,
    }


def normalize_bill_summary(components: list[dict[str, Any]]) -> dict[str, Any]:
    account_rows = _first_data_value(components, "billNumberList") or []
    if not account_rows:
        list_data = _first_data_value(components, "listData")
        account_rows = [list_data] if isinstance(list_data, dict) else []

    bills = []
    for account in account_rows:
        if not isinstance(account, dict):
            continue
        for bill in account.get("billList") or []:
            if not isinstance(bill, dict):
                continue
            month_item = (bill.get("monthList") or [{}])[0]
            bills.append(
                {
                    "month": _normalize_ym(bill.get("ym")),
                    "user_id": bill.get("consNo") or account.get("consNoDst"),
                    "usage": _safe_float(month_item.get("pq")),
                    "charge": _safe_float(month_item.get("amt")),
                    "calc_id": month_item.get("calcId"),
                    "begin_date": month_item.get("begDate"),
                    "end_date": month_item.get("endDate"),
                    "issue_date": month_item.get("issuDate"),
                    "settle_type": month_item.get("billSettleType"),
                    "settle_name": month_item.get("billSettleName"),
                }
            )
    return {
        "year": str(_first_data_value(components, "selectYear") or ""),
        "available_years": _first_data_value(components, "optionalYearArray") or [],
        "bills": bills,
    }


def normalize_bill_detail(components: list[dict[str, Any]]) -> dict[str, Any]:
    bill = (_first_data_value(components, "billList") or [{}])[0]
    if not isinstance(bill, dict):
        bill = {}
    basic = bill.get("basicInfo") or {}
    pv_qty = (bill.get("pvQtyList") or [{}])[0]
    charge_segments = _bill_charge_segments(bill.get("prcGroupList") or [])
    return {
        "month": _normalize_ym(bill.get("ym")),
        "user_id": basic.get("consNo"),
        "begin_date": basic.get("begDate"),
        "end_date": basic.get("endDate"),
        "usage": _safe_float(basic.get("monthPq")),
        "charge": _safe_float(basic.get("monthAmt")),
        "year_usage": _safe_float(basic.get("yearPq")),
        "year_charge": _safe_float(basic.get("yearAmt")),
        "valley_usage": _safe_float(pv_qty.get("valQty")),
        "flat_usage": _safe_float(pv_qty.get("flatQty")),
        "peak_usage": _safe_float(pv_qty.get("peakQty")),
        "tip_usage": _safe_float(pv_qty.get("sharpQty")),
        "charge_segments": charge_segments,
        "raw": bill,
    }


def _first_data_value(components: list[dict[str, Any]], key: str) -> Any:
    for component in components:
        data = component.get("data") or {}
        if key in data:
            return data[key]
    return None


def _data_values(components: list[dict[str, Any]], key: str) -> list[Any]:
    return [(component.get("data") or {}).get(key) for component in components if key in (component.get("data") or {})]


def _normalize_usage_month(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "month": _normalize_ym(row.get("month")),
        "usage": _safe_float(row.get("monthEleNum")),
        "charge": _safe_float(row.get("monthEleCost")),
        "begin_date": row.get("begDate"),
        "end_date": row.get("endDate"),
        "meter_read_time": row.get("mrDate"),
        "is_max": bool(row.get("max")),
    }


def _normalize_daily_row(row: dict[str, Any]) -> dict[str, Any] | None:
    date = str(row.get("day") or "").strip()
    if not date:
        return None
    return {
        "date": date,
        "usage": _safe_float(row.get("dayElePq"), default=0.0),
        "valley_usage": _safe_float(row.get("thisVPq"), default=0.0),
        "flat_usage": _safe_float(row.get("thisNPq"), default=0.0),
        "peak_usage": _safe_float(row.get("thisPPq"), default=0.0),
        "tip_usage": _safe_float(row.get("thisTPq"), default=0.0),
        "charge": _safe_float(row.get("thisAmt")),
    }


def _bill_charge_segments(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments = []
    for group in groups:
        for item in group.get("amtList") or []:
            name = str(item.get("amtName") or "")
            segments.append(
                {
                    "name": name,
                    "usage": _safe_float(item.get("pq")),
                    "price": _safe_float(item.get("price")),
                    "charge": _safe_float(item.get("amount")),
                    "period": _period_from_name(name),
                }
            )
    return segments


def _period_from_name(name: str) -> str | None:
    if "尖" in name:
        return "tip"
    if "峰" in name:
        return "peak"
    if "平" in name:
        return "flat"
    if "谷" in name:
        return "valley"
    return None


def _normalize_ym(value: Any) -> str:
    text = str(value or "").strip().replace("/", "-")
    if len(text) == 6 and text.isdigit():
        return f"{text[:4]}-{text[4:]}"
    if len(text) >= 7:
        return text[:7]
    return text


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        text = str(value).strip()
        if text in ("", "-", "—", "None"):
            return default
        return float(text)
    except (TypeError, ValueError):
        return default
