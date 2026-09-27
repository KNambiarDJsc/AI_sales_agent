from services.lead_import.importer import guess_column_mapping, import_leads

CSV_SAMPLE = (
    "Name,Phone Number,Company\n"
    "Asha Rao,9876543210,Rao Textiles\n"
    "Vikram Shah,+91 98765 43211,Shah Traders\n"
    "Duplicate Row,9876543210,Rao Textiles\n"
    "Bad Number,not-a-phone,Nowhere Inc\n"
)


def test_guess_column_mapping_finds_common_headers():
    mapping = guess_column_mapping(["Name", "Phone Number", "Company"])
    assert mapping["phone"] == "Phone Number"
    assert mapping["contact_name"] == "Name"
    assert mapping["business_name"] == "Company"


def test_import_leads_normalizes_dedupes_and_reports_errors():
    mapping = {"phone": "Phone Number", "contact_name": "Name", "business_name": "Company"}
    rows, report = import_leads(CSV_SAMPLE.encode("utf-8"), "leads.csv", mapping, default_region="IN")

    assert report.total_rows == 4
    assert report.valid_rows == 2
    assert report.duplicate_within_file == 1
    assert len(report.errors) == 1
    assert "not-a-phone" in report.errors[0].reason

    phones = {row.phone_e164 for row in rows}
    assert phones == {"+919876543210", "+919876543211"}
    for row in rows:
        assert row.dedupe_key == row.phone_e164
