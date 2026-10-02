"""The plain-words summary of what anonymising does to a file. No display needed."""
import pytest

from scrubdicom import demo_data
from scrubdicom.app import intake, plain, preview
from scrubdicom.profiles import Profile


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    d = tmp_path_factory.mktemp("plain") / "scans"
    demo_data.main(d)
    it = intake.scan_folder(d)
    smith = next(u for u in it.units if u.key == "1234567")
    cta = next(s for s in smith.series if s.verdict == "keep")
    return cta.sample


def test_every_series_has_a_sample_file_and_coronary_ones_use_a_middle_slice(sample):
    assert sample is not None and sample.is_file() and sample.name == "IM0061.dcm", "the middle of 120 slices, not the first"


def test_default_profile_in_plain_words(sample):
    changes, more = plain.plain_changes(preview.header_diff(sample, "DEMO-002"))
    by = {c.label: c for c in changes}
    assert (by["Name"].before, by["Name"].after, by["Name"].state) == ("SMITH JOHN", "DEMO-002", "replaced")
    assert by["Hospital number"].before == "1234567" and by["Hospital number"].after == "DEMO-002"
    assert (by["Date of birth"].before, by["Date of birth"].after) == ("14 Mar 1961", "removed")
    assert by["Scan date"].before == "22 May 2019" and by["Scan date"].after == "1 Jan 1900 (dummy)"
    assert by["Sex"].before == "male" and by["Sex"].state == "removed"
    assert by["Age"].before == "58 years" and by["Age"].state == "removed"
    assert by["Referring doctor"].before == "BLOGGS J Dr" and by["Referring doctor"].state == "removed"
    assert by["Hospital"].before == "Example Hospital" and by["Hospital"].state == "removed"
    assert by["Scanner"].before == "SIEMENS SOMATOM Force" and by["Scanner"].state == "removed"
    assert by["Other IDs"].state == "removed" and by["Address"].state == "removed" and by["Comments"].state == "removed"
    assert not any(c.state == "kept" for c in changes), "the default keeps nothing that identifies"
    assert "other fields were removed or replaced" in more and "hidden vendor tags" in more and "every original UID" in more


def test_kept_things_are_said_plainly(sample):
    prof = Profile(name="x", keep_sex=True, keep_age_5y=True, keep_manufacturer=True, shift_dates=True)
    changes, _ = plain.plain_changes(preview.header_diff(sample, "DEMO-002", profile=prof))
    by = {c.label: c for c in changes}
    assert by["Sex"].state == "kept" and by["Sex"].after == "kept"
    assert by["Age"].after == "55 years" and by["Age"].state == "replaced", "58 becomes the 55 to 59 band"
    assert by["Scanner"].state == "kept"
    assert by["Scan date"].after.endswith("(moved)") and by["Scan date"].after != "22 May 2019 (moved)"
    assert by["Date of birth"].state == "removed", "never kept, whatever the profile"


def test_formatting_helpers():
    assert plain.nice_date("20210110") == "10 Jan 2021" and plain.nice_date("not a date") == "not a date"
    assert plain.nice("JONES^MARY^^Mrs", "name") == "JONES MARY Mrs" and plain.nice("030M", "age") == "30 months"
    assert plain.nice("", "text") == "" and plain.nice("F", "sex") == "female"
    assert plain.plain_changes([]) == ([], "")
