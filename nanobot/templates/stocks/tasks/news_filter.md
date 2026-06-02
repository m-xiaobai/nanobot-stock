Task: Assess recent material negative news risk for the supplied prescreened symbols.
items={{ items_json }}
lookback_days={{ lookback_days }}
Output contract:
{"items":[{"symbol":"600001","allowed":true,"decision":"PASS","matched_categories":[],"negative_news_flags":[],"risk_notes":[],"evidence":[]},{"symbol":"600002","allowed":false,"decision":"REJECT","matched_categories":["regulatory investigation or administrative penalty"],"negative_news_flags":["CSRC investigation"],"risk_notes":["recent material negative news within 3 days"],"evidence":[{"date":"2026-06-01","title":"公司收到证监会立案告知书","category":"regulatory investigation or administrative penalty","severity":"high"}]}],"partial_failures":[]}
