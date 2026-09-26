from app.models.costing_template import BOQScheduleTotal


def test_boq_schedule_total_fields():
    row = BOQScheduleTotal(tender_id=1, schedule_code="A",
                           stated_total=5975127.60, advertised_value=60879392.16)
    assert row.schedule_code == "A"
    assert row.stated_total == 5975127.60
    assert row.advertised_value == 60879392.16
