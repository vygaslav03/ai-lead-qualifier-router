import pytest

from app.config import DATA_DIR
from app.matcher import ContractorDirectory
from app.models import LeadExtraction


@pytest.fixture(scope="module")
def directory():
    return ContractorDirectory(DATA_DIR / "contractors.csv")


def lead(category, city, state=None):
    return LeadExtraction(problem_summary="x", service_category=category, city=city, state=state)


def test_picks_highest_rated_available(directory):
    m = directory.match(lead("plumbing", "Houston", "TX"))
    assert m.contractor.id == "C001"
    assert [c.id for c in m.alternatives] == ["C004"]


def test_skips_unavailable_even_if_top_rated(directory):
    m = directory.match(lead("electrical", "Chicago", "IL"))
    assert m.contractor.id == "C012"
    assert [c.id for c in m.unavailable] == ["C011"]


def test_suburb_maps_to_metro(directory):
    m = directory.match(lead("plumbing", "Scottsdale", "AZ"))
    assert m.contractor.id == "C016"
    assert "served by the Phoenix team" in m.reason


def test_suburb_with_wrong_state_is_not_mapped(directory):
    assert directory.resolve_metro("Aurora", "IL") is None   # Aurora IL is not Denver
    assert directory.resolve_metro("Aurora", "CO") == ("Denver", "CO")


def test_city_matching_is_case_insensitive(directory):
    assert directory.match(lead("painting", "atlanta")).contractor.id == "C008"


@pytest.mark.parametrize(
    "category,city,state,reason",
    [
        ("hvac", "Austin", "TX", "No coverage in Austin, TX"),
        ("flooring", "Denver", "CO", "No flooring contractor in Denver, CO"),
        ("electrical", "Houston", "TX", "All electrical contractors in Houston, TX are unavailable"),
        ("electrical", None, None, "City is unknown"),
        ("other", "Dallas", "TX", "Service category is unclear"),
    ],
)
def test_no_match_reasons(directory, category, city, state, reason):
    m = directory.match(lead(category, city, state))
    assert not m.matched and m.reason == reason


def test_exclude_ids_gives_next_best(directory):
    m = directory.match(lead("plumbing", "Houston", "TX"), exclude_ids={"C001"})
    assert m.contractor.id == "C004"
    m = directory.match(lead("plumbing", "Houston", "TX"), exclude_ids={"C001", "C004"})
    assert not m.matched and m.reason == "No other plumbing contractors in Houston, TX"
