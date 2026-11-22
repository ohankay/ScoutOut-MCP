"""Checksum validators: known vectors + cross-check against python-stdnum (independent oracle)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from stdnum import iban as std_iban
from stdnum import luhn as std_luhn
from stdnum.tr import tckimlik as std_tckn
from stdnum.tr import vkn as std_vkn

from scoutout.detectors.validators.checksums import (
    iban_check_digits,
    iban_valid,
    luhn_check_digit,
    luhn_valid,
    tckn_check_digits,
    tckn_valid,
    vkn_check_digit,
    vkn_valid,
    ykn_valid,
)

digits = lambda n: st.text("0123456789", min_size=n, max_size=n)  # noqa: E731


# --- known vectors ---------------------------------------------------------

@pytest.mark.parametrize("v", ["10000000146", "11111111110", "12345678950"])
def test_tckn_known_valid(v):
    assert tckn_valid(v)


@pytest.mark.parametrize("v", ["10000000145", "01234567890", "1000000014", "1000000014a", "١٠٠٠٠٠٠٠١٤٦"])
def test_tckn_known_invalid(v):
    assert not tckn_valid(v)


def test_ykn():
    first9 = "991234567"
    v = first9 + tckn_check_digits(first9)
    assert ykn_valid(v)
    assert not ykn_valid("10000000146")


@pytest.mark.parametrize("v", ["4540536920", "1234567890"])
def test_vkn_vectors_agree_with_reference(v):
    assert vkn_valid(v) == std_vkn.is_valid(v)


def test_vkn_known_valid():
    assert vkn_valid("4540536920")


@pytest.mark.parametrize(
    "v",
    [
        "TR330006100519786457841326",  # commonly published TR example
        "DE89370400440532013000",
        "GB82WEST12345698765432",
        "NL91ABNA0417164300",
    ],
)
def test_iban_known_valid(v):
    assert iban_valid(v)


@pytest.mark.parametrize(
    "v",
    [
        "TR330006100519786457841327",  # check digit off
        "TR33000610051978645784132",  # too short for TR
        "DE89370400440532013001",
        "TR33 0006 1005 1978 6457 8413 26",  # spaces: caller must normalize first
    ],
)
def test_iban_known_invalid(v):
    assert not iban_valid(v)


@pytest.mark.parametrize("v", ["4111111111111111", "5555555555554444", "378282246310005", "79927398713"])
def test_luhn_known_valid(v):
    assert luhn_valid(v)


@pytest.mark.parametrize("v", ["4111111111111112", "79927398710", "1"])
def test_luhn_known_invalid(v):
    assert not luhn_valid(v)


# --- property tests against the reference implementation ---------------------

@given(digits(11))
def test_tckn_matches_reference(v):
    assert tckn_valid(v) == std_tckn.is_valid(v)


@given(digits(10))
def test_vkn_matches_reference(v):
    assert vkn_valid(v) == std_vkn.is_valid(v)


@given(st.text("0123456789", min_size=2, max_size=19))
def test_luhn_matches_reference(v):
    assert luhn_valid(v) == std_luhn.is_valid(v)


@given(digits(9).filter(lambda s: s[0] != "0"))
def test_tckn_generated_is_valid(first9):
    v = first9 + tckn_check_digits(first9)
    assert tckn_valid(v) and std_tckn.is_valid(v)


@given(digits(9))
def test_vkn_generated_is_valid(first9):
    v = first9 + vkn_check_digit(first9)
    assert vkn_valid(v) and std_vkn.is_valid(v)


@given(digits(5), digits(16))
def test_tr_iban_generated_is_valid(bank, account):
    bban = bank + "0" + account
    v = "TR" + iban_check_digits("TR", bban) + bban
    assert iban_valid(v) and std_iban.is_valid(v)


@given(digits(15))
def test_luhn_generated_is_valid(payload):
    assert luhn_valid(payload + luhn_check_digit(payload))


@given(digits(15), st.integers(0, 14), st.integers(1, 9))
def test_luhn_detects_single_digit_error(payload, pos, delta):
    v = payload + luhn_check_digit(payload)
    changed = v[:pos] + str((int(v[pos]) + delta) % 10) + v[pos + 1 :]
    assert not luhn_valid(changed)
