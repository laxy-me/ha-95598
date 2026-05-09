from scripts.fetchers.vue_state import (
    normalize_balance,
    normalize_bill_detail,
    normalize_bill_summary,
    normalize_usage,
)


def test_normalize_balance_reads_frontend_balance_fields():
    components = [
        {
            "data": {
                "mixinGetYuEdata": {
                    "amtTime": "2026-05-08 00:00:00",
                    "sumMoney": "21.14",
                    "prepayBal": "38.78",
                    "estiAmt": "17.64",
                    "totalPq": "166.0",
                    "consNo": "5110000000000",
                }
            }
        }
    ]

    assert normalize_balance(components) == {
        "as_of": "2026-05-08 00:00:00",
        "balance": 21.14,
        "prepay_balance": 38.78,
        "estimated_amount": 17.64,
        "history_owe": None,
        "penalty": None,
        "total_usage": 166.0,
        "user_id": "5110000000000",
        "raw": components[0]["data"]["mixinGetYuEdata"],
    }


def test_normalize_usage_reads_year_month_and_daily_rows():
    components = [
        {
            "data": {
                "powerData": {
                    "dataInfo": {"totalEleCost": "300.58", "totalEleNum": "661", "year": "2026"},
                    "mothEleList": [
                        {"month": "202604", "monthEleCost": "71.67", "monthEleNum": "166", "begDate": "2026-04-01"}
                    ],
                },
                "tariffC": "34.67",
                "start": "2026-05-02",
                "end": "2026-05-08",
            }
        },
        {
            "data": {
                "tableData": [
                    {
                        "day": "2026-05-07",
                        "dayElePq": "4.8",
                        "thisVPq": "1.99",
                        "thisNPq": "1.48",
                        "thisPPq": "1.34",
                        "thisTPq": "0",
                    }
                ]
            }
        },
    ]

    result = normalize_usage(components)

    assert result["yearly_usage"] == 661.0
    assert result["yearly_charge"] == 300.58
    assert result["months"][0]["month"] == "2026-04"
    assert result["months"][0]["usage"] == 166.0
    assert result["daily"][0]["date"] == "2026-05-07"
    assert result["daily"][0]["valley_usage"] == 1.99


def test_normalize_bill_summary_flattens_month_list():
    components = [
        {
            "data": {
                "selectYear": 2026,
                "optionalYearArray": ["2026", "2025"],
                "billNumberList": [
                    {
                        "consNoDst": "5110000000000",
                        "billList": [
                            {
                                "ym": "202604",
                                "consNo": "5110000000000",
                                "monthList": [
                                    {
                                        "amt": "71.67",
                                        "pq": "166",
                                        "calcId": "abc",
                                        "begDate": "2026/04/01",
                                        "endDate": "2026/04/30",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        }
    ]

    result = normalize_bill_summary(components)

    assert result["year"] == "2026"
    assert result["available_years"] == ["2026", "2025"]
    assert result["bills"][0]["month"] == "2026-04"
    assert result["bills"][0]["usage"] == 166.0
    assert result["bills"][0]["charge"] == 71.67


def test_normalize_bill_detail_reads_tou_usage_and_charge_segments():
    bill = {
        "ym": "202604",
        "basicInfo": {
            "consNo": "5110000000000",
            "begDate": "2026/04/01",
            "endDate": "2026/04/30",
            "monthPq": "166",
            "monthAmt": "71.67",
            "yearPq": "661",
            "yearAmt": "300.58",
        },
        "pvQtyList": [{"flatQty": "53", "peakQty": "57", "valQty": "56", "sharpQty": ""}],
        "prcGroupList": [
            {
                "amtList": [
                    {"amtName": "峰", "pq": "57", "price": "0.5224", "amount": "29.78"},
                    {"amtName": "平", "pq": "53", "price": "0.5224", "amount": "27.69"},
                    {"amtName": "谷", "pq": "56", "price": "0.2535", "amount": "14.2"},
                ]
            }
        ],
    }
    result = normalize_bill_detail([{"data": {"billList": [bill]}}])

    assert result["month"] == "2026-04"
    assert result["usage"] == 166.0
    assert result["charge"] == 71.67
    assert result["flat_usage"] == 53.0
    assert result["peak_usage"] == 57.0
    assert result["valley_usage"] == 56.0
    assert [item["period"] for item in result["charge_segments"]] == ["peak", "flat", "valley"]
