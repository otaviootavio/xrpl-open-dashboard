import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import codec  # noqa: E402


def test_currency_code():
    assert codec.currency_code("524C555344000000000000000000000000000000") == "RLUSD"
    assert codec.currency_code("USD") == "USD"
    assert codec.currency_code("03ABCDEF00000000000000000000000000000000") == "LP"


def test_nft_id():
    nid = "00182710FA5AFA0BDEE958DE271CE74E345760A6243BA6517107AD9D058349A7"
    assert codec.nft_issuer(nid) == "rPFkv3gypzAg2CKnJpy9aui2Z8XWfoB8Hg"
    assert codec.nft_taxon(nid) == 2015
