Task: Score the supplied candidate items for this run.
Apply the system scoring rubric to each item and return only the final JSON contract.
items={{ items_json }}
Output contract:
{"items":[{"symbol":"600001","technical_score":90,"score_reasons":["trend is above the short and medium moving averages","volume confirms the move"],"risk_notes":["watch for next-day follow-through"]}]}
