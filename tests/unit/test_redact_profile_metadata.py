import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scoutout.core.redact import redact
from scoutout.detectors import scan_cell
from scoutout.metadata_detector import column_scores, tokens
from scoutout.profiler import is_free_text, profile, shape

# --- redact --------------------------------------------------------------------------


def test_redact_spec_example():
    text = "müşteri 10000000146, tel 0532 123 45 67"
    out = redact(text, scan_cell(text).spans)
    assert out == "müşteri ***********, tel 05** *** ** **"


def test_redact_masks_names_and_emails_outside_spans():
    text = "Ahmet Yılmaz e-posta ahmet@ornek.com yazdı"
    out = redact(text, scan_cell(text).spans)
    assert "Ahmet" not in out and "Yılmaz" not in out and "ornek" not in out and "yazdı" in out


def test_redact_without_spans_keeps_only_shape():
    assert redact("Ab 12-3") == "** **-*"


@given(st.text(alphabet=st.characters(categories=["Nd", "Lu", "Ll", "Zs", "Po"]), max_size=200))
def test_redact_never_leaks_digit_runs(text):
    for s in (redact(text), redact(text, scan_cell(text).spans or [])):
        assert not re.search(r"\d{3}", s)


def test_redact_truncates_long_text_around_finding():
    text = "x " * 300 + "TC 10000000146 " + "y " * 300
    out = redact(text, scan_cell(text).spans, max_len=80)
    assert len(out) <= 82 and "***********" in out and "10000000146" not in out


# --- profiler ------------------------------------------------------------------------


def test_shape_examples_from_spec():
    assert shape("Ahmet Yılmaz") == "Xxxxx Xxxxxx"
    assert shape("0532 123 45 67") == "dddd ddd dd dd"


def test_profile_basic_stats():
    p = profile(["0532 123 45 67", "0533 111 22 33", None, ""])
    assert p.n == 4 and p.null_rate == 0.25 and p.empty_rate == pytest.approx(1 / 3)
    assert p.top_shapes[0] == ("dddd ddd dd dd", pytest.approx(2 / 3, abs=1e-3))
    assert p.vector().shape == (11 + 64 + 32,)


def test_free_text_heuristic():
    assert is_free_text(profile(["Müşteri şubeyi aradı, bilgi verildi ve kayıt açıldı."] * 5))
    assert not is_free_text(profile(["10000000146"] * 5))


# --- metadata --------------------------------------------------------------------------


def test_tokens():
    assert tokens("TcKimlikNo") == ["tc", "kimlik", "no"]
    assert tokens("customer_GSM_no") == ["customer", "gsm", "no"]
    assert tokens("IBANNumber") == ["iban", "number"]


NAMES = {
    "TCKN": ["TCKN", "TcKimlikNo", "TC_KIMLIK_NO", "IdentityNo", "NationalId", "MusteriTckn", "TcNo"],
    "VKN": ["VKN", "VergiNo", "TaxNo", "VergiKimlikNo", "FirmaVkn"],
    "IBAN": ["Iban", "IBAN_NO", "MusteriIban", "HesapIban"],
    "PAN": ["CardNo", "KartNo", "PAN", "CardNumber", "KrediKartNo"],
    "PHONE": ["GsmNo", "Telefon", "CepTel", "PhoneNumber", "MobilePhone", "TelNo", "Msisdn"],
    "EMAIL": ["Email", "EPosta", "E_MAIL", "MailAdresi"],
    "ADDRESS": ["Adres", "Address", "EvAdresi", "IsAdres"],
    "GIVEN_NAME": ["Ad", "Adi", "FirstName", "Isim"],
    "SURNAME": ["Soyad", "Soyadi", "LastName", "Surname"],
    "BIRTH_DATE": ["DogumTarihi", "BirthDate", "DOB", "DogumTar"],
    "PERSON_NAME": ["AdSoyad", "FullName", "MusteriAdi", "CustomerName"],
    "PASSPORT": ["PasaportNo", "PassportNumber"],
    "PLATE": ["Plaka", "AracPlaka"],
    "SWIFT": ["SwiftKodu", "BicCode"],
}


@pytest.mark.parametrize("dtype, name", [(t, n) for t, names in NAMES.items() for n in names])
def test_metadata_finds_expected_type(dtype, name):
    scores = column_scores(name)
    assert scores.get(dtype, 0) >= 0.5, scores


@pytest.mark.parametrize("name", ["Aciklama", "CreatedAt", "Amount", "BranchCode", "Status", "Kod", "RefNo", "Segment"])
def test_metadata_quiet_on_neutral_names(name):
    assert max(column_scores(name).values(), default=0) < 0.5


def test_type_fit_adjusts_score():
    assert column_scores("TCKN", "char", 11)["TCKN"] > column_scores("TCKN", "int", None)["TCKN"]
