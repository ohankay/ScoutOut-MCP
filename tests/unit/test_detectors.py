import pytest
from hypothesis import given
from hypothesis import strategies as st

from scoutout.detectors import scan_cell
from scoutout.detectors.generic import pan_brand
from scoutout.detectors.normalize import digit_candidates, fold, tr_lower
from scoutout.detectors.validators.checksums import tckn_check_digits


def types(text, **ctx):
    return {s.data_type for s in scan_cell(text, ctx=ctx).spans}


# --- normalisation ----------------------------------------------------------------

def test_turkish_case_and_folding():
    assert tr_lower("IŞIK İSTANBUL") == "ışık istanbul"
    assert fold("IŞIK Çağrı Gündüz") == "isik cagri gunduz"
    assert fold("ÖZGÜR") == "ozgur"


@given(st.text())
def test_fold_is_idempotent(s):
    assert fold(fold(s)) == fold(s)


def test_candidates_join_single_consistent_separator():
    digits = {c.digits for c in digit_candidates("tel 0532 123 45 67")}
    assert "05321234567" in digits


def test_candidates_boundary_prevents_merging_separate_numbers():
    # different separators, comma, double space -> never one number
    for text in ("no: 12345, 678901", "12345  678901", "12345-678 901", "12345\n678901"):
        assert "12345678901" not in {c.digits for c in digit_candidates(text)}


def test_two_adjacent_identifiers_still_found_individually():
    a, b = "10000000146", "12345678950"
    assert {"TCKN"} == types(f"{a} {b}")
    assert len([s for s in scan_cell(f"{a} {b}").spans if s.data_type == "TCKN"]) == 2


# --- detectors: positives ------------------------------------------------------------

@pytest.mark.parametrize(
    "text, dtype",
    [
        ("10000000146", "TCKN"),
        ("Müşteri TC 10000000146 ile doğrulandı.", "TCKN"),
        ("99" + "1234567" + tckn_check_digits("991234567"), "YKN"),
        ("4540536920", "VKN"),
        ("TR330006100519786457841326", "IBAN"),
        ("IBAN: TR33 0006 1005 1978 6457 8413 26", "IBAN"),
        ("4111111111111111", "PAN"),
        ("kart 5555-5555-5555-4444 iptal", "PAN"),
        ("5528-XXXX-XXXX-3304", "MASKED_PAN"),
        ("454360******1234", "MASKED_PAN"),
        ("0532 123 45 67", "PHONE"),
        ("+90 532 1234567", "PHONE"),
        ("(0545) 723-00-65", "PHONE"),
        ("(0212) 345 67 89", "PHONE"),
        ("ali.veli42@hotmail.com", "EMAIL"),
        ("Gültepe Mah. Menderes Cad. No:146 D:26 Nilüfer/Konya", "ADDRESS"),
        ("Ayşe", "GIVEN_NAME"),
        ("Yılmaz", "SURNAME"),
        ("Bugün Ahmet Yılmaz aradı", "PERSON_NAME"),
        ("pasaport no U12345678", "PASSPORT"),
        ("araç plaka 34 ABC 123", "PLATE"),
        ("SWIFT: TGBATRISXXX", "SWIFT"),
        ("192.168.10.25", "IP"),
        ("00:1A:2B:3C:4D:5E", "MAC"),
    ],
)
def test_detects(text, dtype):
    assert dtype in types(text)


def test_birth_date_needs_column_context():
    assert "BIRTH_DATE" in types("1985-04-02", is_date_column=True, column_folded="dogumtarihi")
    assert "BIRTH_DATE" not in types("1985-04-02", is_date_column=True, column_folded="createdat")


# --- detectors: negatives -------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "10000000145",  # bad TCKN check digit
        "01234567890",  # TCKN cannot start with 0
        "TR330006100519786457841327",  # bad IBAN check digits
        "4111111111111112",  # bad Luhn
        "1234567812345670",  # Luhn-valid but no known IIN
        "Tutar 1250,00 TL iade edildi.",
        "Randevu 14:30 olarak planlandı.",
        "Talep 2025/118 numaralı kayıtla açıldı.",
        "TRANSFER",  # 8 upper-case letters, no SWIFT context
        "kod U12345678 gönderildi",  # passport shape inside text without context
        "999.1.2.3",
    ],
)
def test_does_not_detect(text):
    assert not types(text)


def test_pan_brands():
    assert pan_brand("4111111111111111") == "VISA"
    assert pan_brand("5555555555554444") == "MASTERCARD"
    assert pan_brand("2221000000000009") == "MASTERCARD"
    assert pan_brand("378282246310005") == "AMEX"
    assert pan_brand("9792000000000000") == "TROY"
    assert pan_brand("1234567812345670") is None


def test_candidate_counts_for_chance_correction():
    f = scan_cell("86507882991")  # a random 11-digit reference: shaped like a TCKN
    assert f.candidates["TCKN"] == 1
