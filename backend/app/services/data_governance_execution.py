"""企业版能力占位：开源版不含实现，调用即抛 EditionFeatureUnavailable。"""
from app.services._edition_stub import make_stub_getattr

__getattr__ = make_stub_getattr(__name__)
