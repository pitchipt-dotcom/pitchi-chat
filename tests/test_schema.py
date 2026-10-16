"""Local full Schema validation, with no external reference retrieval."""
import pytest
from backend.app.models.schema import normalized_format, validated_json, validator_for
from backend.app.models.base import ModelError

SCHEMA={"type":"object","properties":{"verdict":{"enum":["PASS","FAIL"]},"count":{"type":"integer","minimum":0},
       "items":{"type":"array","minItems":1,"items":{"type":"string"}}},"required":["verdict","count","items"],"additionalProperties":False}

@pytest.mark.parametrize('value',[
 '{"verdict":"OTHER","count":1,"items":["x"]}',
 '{"verdict":"PASS","count":true,"items":["x"]}',
 '{"verdict":"PASS","count":1,"items":[]}',
 '{"verdict":"PASS","count":1,"items":["x"],"extra":1}',
 '{"verdict":"PASS","items":["x"]}',
 '{"verdict":"PASS","count":1,"count":2,"items":["x"]}',
 '{"verdict":"PASS","count":NaN,"items":["x"]}',
])
def test_strict_invalid_output_rejected(value):
    with pytest.raises(ModelError):validated_json(value,SCHEMA)

def test_valid_local_ref_and_legacy_format():
    fmt,schema=normalized_format({'type':'json_object','schema':SCHEMA})
    assert fmt['type']=='json_schema'
    assert validated_json('{"verdict":"PASS","count":1,"items":["x"]}',schema)['count']==1
    ref={"$defs":{"number":{"type":"integer"}},"type":"object","properties":{"n":{"$ref":"#/$defs/number"}},"required":["n"]}
    assert validated_json('{"n":4}',ref)['n']==4

def test_remote_ref_blocked():
    with pytest.raises(ModelError):validator_for({'$ref':'https://example.invalid/schema'})
