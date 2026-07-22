from app.context.compression_layers import CompressionLayer
from app.context.compressor import apply_layer_c_middle_summary, build_layer_d_reject_result


def test_compression_layer_order():
    assert CompressionLayer.TAIL_TOOL_SUMMARY < CompressionLayer.OLD_TOOL_ONELINE
    assert CompressionLayer.OLD_TOOL_ONELINE < CompressionLayer.MIDDLE_SUMMARY
    assert CompressionLayer.MIDDLE_SUMMARY < CompressionLayer.REJECT
    assert CompressionLayer.EMERGENCY == 5


def test_layer_c_empty_middle():
    result = apply_layer_c_middle_summary([], existing_summary="已有摘要")
    assert result.layer == int(CompressionLayer.MIDDLE_SUMMARY)
    assert result.summary == "已有摘要"
    assert result.success is True


def test_layer_d_reject():
    result = build_layer_d_reject_result([], "summary")
    assert result.layer == int(CompressionLayer.REJECT)
    assert result.success is False
    assert "新建会话" in (result.message or "")
