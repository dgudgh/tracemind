"""未包含在本仓库的能力占位：调用即抛 EditionFeatureUnavailable。"""
from app.services._edition_stub import make_stub_getattr

__getattr__ = make_stub_getattr(__name__)
